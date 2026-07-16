"""ARBITER data-ingest worker — async S3 folder -> chunk/serialize -> embed -> S3 Vectors.

Backs the DocuSearch (unstructured) and Structured Analytics (tabular) UI paths (points 5-6).
Invoked fire-and-forget (InvocationType="Event") by api_handler `POST /data-pipeline/ingest`,
mirroring the scanner pattern: api_handler pre-writes a QUEUED `data-jobs` row, this worker
flips it RUNNING -> SUCCEEDED/FAILED with counts so the UI can poll `GET /data-jobs`.

It is a CONTAINER-IMAGE Lambda because it carries heavy ingest deps (pandas/pyarrow/pypdf/
python-docx) plus the reusable `arbiter_rag` library COPY'd into the image — the same single
source of truth the notebooks and the specialist agents use, so ingestion never forks logic.

Job payload (from api_handler):
  {job_id, created_at, job_type, source_bucket, source_prefix,
   vector_bucket, vector_index, dataset_id, grain?}
"""

import json
import logging
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import boto3

from arbiter_rag import ingest
from arbiter_rag.config import Settings

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("data_ingest")

REGION = os.environ.get("AWS_REGION", "us-east-1")
ENVIRONMENT = os.environ.get("ENVIRONMENT", "dev")
DATA_JOBS_TABLE = os.environ.get("DATA_JOBS_TABLE", "")
EMBEDDING_MODEL_ID = os.environ.get("EMBEDDING_MODEL_ID", "amazon.titan-embed-text-v2:0")
EMBEDDING_DIM = int(os.environ.get("EMBEDDING_DIM", "1024"))
CHUNK_STRATEGY = os.environ.get("CHUNK_STRATEGY", "semantic")
CHUNK_VERSION = os.environ.get("CHUNKING_VERSION", "v1")
DISTANCE_METRIC = os.environ.get("DISTANCE_METRIC", "cosine")
INGEST_BATCH_SIZE = int(os.environ.get("INGEST_BATCH_SIZE", "500"))

# Same set arbiter_rag.loaders can parse (SUPPORTED_DOC_EXTS | TABULAR_EXTS).
SUPPORTED_EXTS = {".pdf", ".docx", ".txt", ".md", ".json", ".csv", ".xlsx", ".xls", ".parquet"}

s3 = boto3.client("s3", region_name=REGION)
ddb = boto3.resource("dynamodb", region_name=REGION)
jobs_table = ddb.Table(DATA_JOBS_TABLE) if DATA_JOBS_TABLE else None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _settings(vector_bucket: str) -> Settings:
    """arbiter_rag Settings from env (never settings.toml) — only the ingest fields matter."""
    return Settings(
        env=ENVIRONMENT, region=REGION, account="", expected_account_id="",
        generation_model_id="us.amazon.nova-2-lite-v1:0", generation_max_tokens=1024,
        generation_temperature=0.2, embedding_model_id=EMBEDDING_MODEL_ID, embedding_dim=EMBEDDING_DIM,
        rerank_enabled=False, rerank_model_id="amazon.rerank-v1:0", rerank_candidates_k=20,
        rerank_top_k=5, retrieval_top_k=5, chunk_strategy=CHUNK_STRATEGY, chunk_max_chars=1200,
        chunk_overlap_chars=200, chunking_version=CHUNK_VERSION, vector_bucket=vector_bucket,
        hr_index="hr-policies", sales_index="sales-facts", distance_metric=DISTANCE_METRIC,
        glue_database="", glue_table="", athena_workgroup="primary", athena_output_prefix="",
        max_scanned_bytes=1024 * 1024 * 1024, guardrails_enabled=False, guardrail_id="",
        guardrail_version="DRAFT", ingest_batch_size=INGEST_BATCH_SIZE, log_level="INFO",
    )


def _update_job(job_id: str, created_at: str, **fields) -> None:
    """Best-effort UpdateItem on the data-jobs row (never crash the worker on a status write)."""
    if not jobs_table:
        return
    names = {f"#{k}": k for k in fields}  # alias every key (status/result/ttl are reserved words)
    values = {f":{k}": v for k, v in fields.items()}
    expr = "SET " + ", ".join(f"#{k} = :{k}" for k in fields)
    try:
        jobs_table.update_item(
            Key={"job_id": job_id, "created_at": created_at},
            UpdateExpression=expr,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
    except Exception:
        log.exception("data-jobs update failed (continuing): %s", list(fields))


def _download_prefix(bucket: str, prefix: str, dest: Path) -> int:
    """Download every supported object under `prefix` into `dest`. Returns file count."""
    paginator = s3.get_paginator("list_objects_v2")
    n = 0
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/"):
                continue
            if Path(key).suffix.lower() not in SUPPORTED_EXTS:
                continue
            # Flatten to basename; disambiguate collisions with the object's index.
            local = dest / f"{n}_{Path(key).name}"
            s3.download_file(bucket, key, str(local))
            n += 1
    return n


def handler(event, context):
    """Run one ingest job end to end and record its terminal status in the data-jobs row."""
    job_id = event["job_id"]
    created_at = event["created_at"]
    job_type = (event.get("job_type") or "docusearch").strip().lower()
    source_bucket = event["source_bucket"]
    source_prefix = event["source_prefix"]
    vector_bucket = event["vector_bucket"]
    vector_index = event["vector_index"]
    dataset_id = event.get("dataset_id") or vector_index
    grain = event.get("grain") if isinstance(event.get("grain"), list) else None

    log.info("data-ingest start job=%s type=%s src=s3://%s/%s -> %s/%s",
             job_id, job_type, source_bucket, source_prefix, vector_bucket, vector_index)
    _update_job(job_id, created_at, status="RUNNING", started_at=_now_iso())

    tmp = Path(tempfile.mkdtemp(prefix="ingest-"))
    try:
        settings = _settings(vector_bucket)
        n_files = _download_prefix(source_bucket, source_prefix, tmp)
        if n_files == 0:
            _update_job(job_id, created_at, status="SUCCEEDED", finished_at=_now_iso(),
                        result={"files": 0, "vectors": 0, "message": "no supported files under prefix"})
            return {"status": "SUCCEEDED", "files": 0}

        if job_type == "structured_analytics":
            source_uri = f"s3://{source_bucket}/{source_prefix}"
            result = ingest.ingest_tabular(
                tmp, vector_bucket, vector_index, settings,
                dataset_id=dataset_id, grain=grain, source_uri=source_uri,
            )
        else:
            result = ingest.ingest_unstructured(tmp, vector_bucket, vector_index, settings)

        result = {"files": n_files, **result}
        _update_job(job_id, created_at, status="SUCCEEDED", finished_at=_now_iso(), result=result)
        log.info("data-ingest done job=%s result=%s", job_id, result)
        return {"status": "SUCCEEDED", "result": result}
    except Exception as e:  # noqa: BLE001 — record failure on the job row, don't just die
        log.exception("data-ingest job failed")
        _update_job(job_id, created_at, status="FAILED", finished_at=_now_iso(),
                    error=f"{type(e).__name__}: {e}")
        return {"status": "FAILED", "error": str(e)}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
