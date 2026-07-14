"""Ingest the Hawaii sales facts into Amazon S3 Vectors (the semantic path).

Loads the per-branch sales CSVs, serializes them to branch × category natural-language
"facts" (arbiter_rag.serialization), embeds each with Titan v2, creates the S3 Vectors
bucket + `sales-facts` index if absent, and upserts the vectors. This is the script form
of the notebook's Path-A ingest, sized for the 100-branch dataset. The deployed
Sales_Specialist agent then queries this exact index (SALES_VECTOR_BUCKET / SALES_VECTOR_INDEX).

Idempotent: re-running upserts by deterministic key (bump chunking_version to re-key).

Prerequisites:
  - boto3 with the s3vectors client, Bedrock model access to Titan v2 in the region.
  - The `arbiter_rag` library importable (pip install -e rag_src, or run from the repo).

Usage:
  source scripts/.venv/bin/activate        # or: pip install -e rag_src[data]
  AWS_REGION=us-east-1 PROJECT=st21arbiter-poc python3 scripts/ingest_sales_vectors.py
  # options via env:
  #   DATASET=large|sample                 (default large: data/Hawaii_Electronics_100)
  #   SALES_VECTOR_BUCKET=...              (default <env>-<project>-sales-vectors)
  #   SALES_VECTOR_INDEX=sales-facts
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
# Make arbiter_rag importable without requiring an editable install.
_RAG_SRC = REPO_ROOT / "rag_src"
if (_RAG_SRC / "arbiter_rag").is_dir() and str(_RAG_SRC) not in sys.path:
    sys.path.insert(0, str(_RAG_SRC))

from arbiter_rag import embeddings, loaders, serialization, vectors  # noqa: E402
from arbiter_rag.config import Settings  # noqa: E402

REGION = os.environ.get("AWS_REGION", "us-east-1")
ENV = os.environ.get("ENVIRONMENT", "dev")
PROJECT = os.environ.get("PROJECT", "st21arbiter-poc")
PREFIX = f"{ENV}-{PROJECT}"

DATASET = os.environ.get("DATASET", "large").lower()
DATASET_DIR = REPO_ROOT / "data" / ("Hawaii_Electronics_100" if DATASET == "large" else "Hawaii_Sample_Sales")
BUCKET = os.environ.get("SALES_VECTOR_BUCKET", f"{PREFIX}-sales-vectors")
INDEX = os.environ.get("SALES_VECTOR_INDEX", "sales-facts")
EMBED_MODEL = os.environ.get("EMBEDDING_MODEL_ID", "amazon.titan-embed-text-v2:0")
EMBED_DIM = int(os.environ.get("EMBEDDING_DIM", "1024"))
CHUNK_VERSION = os.environ.get("CHUNKING_VERSION", "v1")


def _settings() -> Settings:
    """Minimal Settings for the ingest path (embeddings only read region/model/dim)."""
    return Settings(
        env=ENV, region=REGION, account="", expected_account_id="",
        generation_model_id="us.amazon.nova-2-lite-v1:0", generation_max_tokens=1024,
        generation_temperature=0.2, embedding_model_id=EMBED_MODEL, embedding_dim=EMBED_DIM,
        rerank_enabled=False, rerank_model_id="amazon.rerank-v1:0", rerank_candidates_k=20,
        rerank_top_k=5, retrieval_top_k=5, chunk_strategy="semantic", chunk_max_chars=1200,
        chunk_overlap_chars=200, chunking_version=CHUNK_VERSION, vector_bucket=BUCKET,
        hr_index="hr-policies", sales_index=INDEX, distance_metric="cosine",
        glue_database=f"{ENV}_{PROJECT}_structured".replace("-", "_"), glue_table="hawaii_sales",
        athena_workgroup=f"{PREFIX}-wg", athena_output_prefix="",
        max_scanned_bytes=1024 * 1024 * 1024, guardrails_enabled=False, guardrail_id="",
        guardrail_version="DRAFT", ingest_batch_size=500, log_level="INFO",
    )


def main() -> int:
    if not DATASET_DIR.is_dir():
        print(f"✗ dataset dir not found: {DATASET_DIR} "
              f"(run scripts/import_sales_data.py for the 'large' set)", file=sys.stderr)
        return 1
    S = _settings()
    print(f"dataset : {DATASET_DIR}")
    df = loaders.load_hawaii_sales(DATASET_DIR)
    print(f"loaded  : {len(df):,} rows | {df.branch_id.nunique()} branches")

    facts = serialization.build_sales_facts(df, chunking_version=CHUNK_VERSION)
    print(f"facts   : {len(facts)} (branch × category)")

    vx = vectors.make_client(REGION)
    print(f"vectors : ensuring bucket {BUCKET} + index {INDEX} ({EMBED_DIM}d, {S.distance_metric})")
    vectors.ensure_vector_bucket(vx, BUCKET)
    vectors.ensure_index(vx, BUCKET, INDEX, EMBED_DIM, S.distance_metric,
                         vectors.SALES_NON_FILTERABLE_KEYS)

    print(f"embed   : {len(facts)} facts with {EMBED_MODEL} …")
    vecs = embeddings.embed_texts([f["text"] for f in facts], S)
    recs = [{"key": f["key"], "embedding": v, "metadata": f["metadata"]}
            for f, v in zip(facts, vecs)]
    n = vectors.put_records(vx, BUCKET, INDEX, recs, batch_size=S.ingest_batch_size)
    print(f"✓ ingested {n} sales-fact vectors into {BUCKET}/{INDEX}")
    print("  The Sales_Specialist agent queries this index via SALES_VECTOR_BUCKET/SALES_VECTOR_INDEX.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
