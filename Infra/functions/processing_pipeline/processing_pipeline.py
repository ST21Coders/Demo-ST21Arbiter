"""ARBITER processing pipeline — raw → processed S3 file mover + F1 auto-detect chain.

Three invocation paths share this handler:
  1. EventBridge schedule (twice daily, 06:00 / 18:00 PST literal = 14:00 / 02:00 UTC)
     — no http context, no detail.bucket. Runs full batch sweep.
  2. Lambda Function URL (AuthType=NONE) from a UI / Postman — requires
     `Authorization: Bearer <Cognito IdToken>`; caller must belong to one
     of ALLOWED_GROUPS (defaults set in CFN to "ciso,grc"). Runs full batch sweep.
  3. EventBridge S3 ObjectCreated rule — fires on every new upload to RAW_BUCKET
     (excluding REPORTS_PREFIX). Processes ONE key, then if KB_ID is set:
        - Starts a Bedrock KB ingestion job
        - Polls until COMPLETE / FAILED (up to ~3 min)
        - On COMPLETE, async-invokes SCANNER_LAMBDA_NAME
     This is the F1 "as they happen" auto-detect chain documented in
     Documents/Feature_Coverage_Plan.md §3 steps 3-5.

For each object in RAW_BUCKET (batch path):
  1. Skip keys under REPORTS_PREFIX (our own audit CSVs) and folder markers.
  2. head_object on PROCESSED_BUCKET with the same key — if present, record SKIPPED_EXISTS.
  3. Otherwise copy_object → delete_object (true move), record MOVED.
  4. Write a CSV report of the run to s3://RAW_BUCKET/REPORTS_PREFIX.

Bucket names + report prefix are env vars so operators can repoint without redeploy.
"""
import base64
import csv
import io
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

RAW_BUCKET = os.environ["RAW_BUCKET"]
PROCESSED_BUCKET = os.environ["PROCESSED_BUCKET"]
REPORTS_PREFIX = os.environ.get("REPORTS_PREFIX", "File_Transfer_Reports/")
REGION = os.environ.get("AWS_REGION", "us-east-1")
COGNITO_ISSUER_URL = os.environ.get("COGNITO_ISSUER_URL", "")
# Comma-separated list of Cognito group names allowed to trigger a manual run.
# Empty means "any authenticated caller" (still requires a valid JWT).
ALLOWED_GROUPS = {g.strip() for g in os.environ.get("ALLOWED_GROUPS", "").split(",") if g.strip()}

# F1 auto-detect chain — empty values disable the post-process trigger gracefully.
KB_ID = os.environ.get("KB_ID", "").strip()
KB_DATA_SOURCE_ID = os.environ.get("KB_DATA_SOURCE_ID", "").strip()
SCANNER_LAMBDA_NAME = os.environ.get("SCANNER_LAMBDA_NAME", "").strip()
# Structured ingestion: .csv exports are preserved under their original
# processed key for Data Grouping, then mirrored to processed/<STRUCTURED_PREFIX>
# for Glue/Athena. Empty crawler name = catalog refresh skipped. The re-scan is
# NOT inline — Glue crawls take minutes; it runs via "Run AI Scan" or the future
# crawler-completed Step Functions rule (see Documents/policy_scan_flow.md).
STRUCTURED_PREFIX = os.environ.get("STRUCTURED_PREFIX", "structured/")
GLUE_CRAWLER_NAME = os.environ.get("GLUE_CRAWLER_NAME", "").strip()
# Polling budget for the ingestion job. Default 180s (Bedrock typically
# finishes a 5-doc KB in ~30-60s; 3min is a comfortable ceiling).
INGEST_POLL_TIMEOUT_S = int(os.environ.get("INGEST_POLL_TIMEOUT_S", "180"))
INGEST_POLL_INTERVAL_S = int(os.environ.get("INGEST_POLL_INTERVAL_S", "5"))
INGEST_START_MAX_ATTEMPTS = int(os.environ.get("INGEST_START_MAX_ATTEMPTS", "18"))

s3 = boto3.client("s3", region_name=REGION)
bedrock_agent = boto3.client("bedrock-agent", region_name=REGION)
lambda_client = boto3.client("lambda", region_name=REGION)
glue = boto3.client("glue", region_name=REGION)

# Toggled per-invocation in handler(). The Function URL's CORS layer adds
# Access-Control-Allow-Origin itself; emitting it again from the Lambda
# response produces duplicate headers which browsers reject. EventBridge
# scheduled invocations have no http context, so the flag stays False and
# CORS headers are omitted (the response is never sent to a browser).
_emit_cors_headers = False

CSV_HEADER = [
    "timestamp_utc",
    "source_bucket",
    "source_key",
    "destination_bucket",
    "destination_key",
    "action",
    "size_bytes",
    "source_etag",
    "error",
]

def _cors_headers() -> dict:
    headers = {"Content-Type": "application/json"}
    if _emit_cors_headers:
        headers["Access-Control-Allow-Origin"] = "*"
        headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization"
        headers["Access-Control-Allow-Methods"] = "POST,OPTIONS"
    return headers


def _resp(status: int, body: dict) -> dict:
    return {"statusCode": status, "headers": _cors_headers(), "body": json.dumps(body)}


def _caller_groups(event: dict) -> tuple[str | None, list[str]]:
    """Decode the Cognito IdToken from Authorization: Bearer <jwt>.

    Returns (sub, groups). Returns (None, []) when the header is missing or
    malformed. No signature verification — same trusted-issuer pattern as
    api_handler._caller_user_id (worst case, a tampered claim only impacts
    the caller's own request).
    """
    headers = event.get("headers") or {}
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    if not auth.startswith("Bearer "):
        return None, []
    try:
        payload_b64 = auth[7:].split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        sub = payload.get("sub") or payload.get("cognito:username")
        groups = payload.get("cognito:groups") or []
        return sub, list(groups)
    except Exception as e:
        logger.warning("JWT decode failed: %s", e)
        return None, []


def handler(event, context):
    # Detect HTTP invocation (Function URL) vs EventBridge / direct invoke.
    http = event.get("requestContext", {}).get("http") or {}
    method = (http.get("method") or "").upper()
    invoked_via_http = bool(http)

    # Function URL invocations: the URL layer handles CORS; suppress our
    # own ACAO so we don't duplicate the header. EventBridge: not a browser,
    # CORS headers are noise.
    global _emit_cors_headers
    _emit_cors_headers = False

    # Browser CORS preflight — always allow.
    if method == "OPTIONS":
        return _resp(200, {"ok": True})

    # F1 auto-detect path: EventBridge S3 ObjectCreated event arrives as
    # {source: "aws.s3", detail-type: "Object Created", detail: {bucket:{name},
    # object:{key,size,...}}}. Process exactly the uploaded key, then trigger
    # the KB ingestion + scanner chain. Skips the batch sweep entirely.
    if event.get("source") == "aws.s3" and event.get("detail-type") == "Object Created":
        return _handle_single_object_event(event)

    # AuthN/AuthZ gate — only applies to HTTP invocations. EventBridge runs free.
    if invoked_via_http:
        sub, groups = _caller_groups(event)
        if not sub:
            return _resp(401, {"error": "Missing or invalid Authorization header"})
        if ALLOWED_GROUPS and not (ALLOWED_GROUPS & set(groups)):
            return _resp(403, {
                "error": f"Caller not in allowed groups {sorted(ALLOWED_GROUPS)}",
                "caller_groups": groups,
            })
        logger.info("Manual HTTP trigger: sub=%s groups=%s", sub, groups)

    run_id = uuid.uuid4().hex
    started = _now_iso()
    logger.info(
        "processing_pipeline run started: run_id=%s raw=%s processed=%s reports_prefix=%s",
        run_id, RAW_BUCKET, PROCESSED_BUCKET, REPORTS_PREFIX,
    )

    rows: list[list] = []
    moved = skipped = failed = 0

    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=RAW_BUCKET):
            for obj in page.get("Contents") or []:
                src_key = obj["Key"]
                # Don't record CSV rows for our own reports or folder markers —
                # they're not transfer activity.
                if src_key.startswith(REPORTS_PREFIX) or src_key.endswith("/"):
                    continue
                action, error = _process_one(obj)
                rows.append(_row(obj, action, error))
                if action == "MOVED":
                    moved += 1
                elif action == "SKIPPED_EXISTS":
                    skipped += 1
                else:
                    failed += 1
    except ClientError as e:
        logger.exception("List operation on raw bucket failed")
        return _resp(502, {"run_id": run_id, "error": f"list_failed: {e}"})

    finished = _now_iso()
    report_key = f"{REPORTS_PREFIX}run-{started.replace(':', '-')}-{run_id[:8]}.csv"
    try:
        _write_report(report_key, rows)
    except ClientError as e:
        logger.exception("Failed to write report")
        return _resp(502, {
            "run_id": run_id,
            "started": started,
            "finished": finished,
            "moved": moved,
            "skipped": skipped,
            "failed": failed,
            "error": f"report_write_failed: {e}",
        })

    logger.info(
        "processing_pipeline run finished: run_id=%s moved=%d skipped=%d failed=%d report=s3://%s/%s",
        run_id, moved, skipped, failed, RAW_BUCKET, report_key,
    )
    return _resp(200, {
        "run_id": run_id,
        "started": started,
        "finished": finished,
        "moved": moved,
        "skipped": skipped,
        "failed": failed,
        "report_key": report_key,
    })


# ──────────────────────────── F1 single-event path ──────────────
def _handle_single_object_event(event: dict) -> dict:
    """Handle one EventBridge S3 ObjectCreated event end-to-end.

    Steps:
      1. Pull bucket+key from event.detail. Skip our own REPORTS_PREFIX (the
         EventBridge rule already filters but defense in depth).
      2. Copy raw → processed (or skip if dest exists).
      3. If KB_ID is configured, start a Bedrock KB ingestion job, poll until
         COMPLETE, then async-invoke SCANNER_LAMBDA_NAME so a fresh scan picks
         up the newly-indexed document. Each step that fails is logged and the
         response still returns 200 so EventBridge doesn't retry forever.
    """
    detail = event.get("detail") or {}
    bucket = (detail.get("bucket") or {}).get("name") or RAW_BUCKET
    key = (detail.get("object") or {}).get("key") or ""
    if not key or key.startswith(REPORTS_PREFIX) or key.endswith("/"):
        return _resp(200, {"skipped": True, "reason": "filtered key", "key": key})

    # Structured (.csv) exports take the Glue/Athena path, not the KB path.
    if _is_structured(key):
        return _handle_structured_object(bucket, key)

    logger.info("F1 auto-detect: bucket=%s key=%s", bucket, key)
    obj = {"Key": key, "Size": (detail.get("object") or {}).get("size", 0),
           "ETag": (detail.get("object") or {}).get("etag", "")}
    action, error = _process_one(obj)
    logger.info("File move result: action=%s error=%s", action, error or "-")

    summary: dict = {
        "key": key,
        "action": action,
        "error": error or None,
        "kb_triggered": False,
        "scanner_triggered": False,
    }

    if action == "FAILED":
        return _resp(502, summary)

    if not (KB_ID and KB_DATA_SOURCE_ID):
        logger.info("KB_ID / KB_DATA_SOURCE_ID unset — skipping ingestion + scan chain")
        return _resp(200, summary)

    job = _start_kb_ingestion()
    if job:
        summary["kb_triggered"] = True
        summary["ingestion_job_id"] = job
        final_status = _wait_for_ingestion(job)
        summary["ingestion_status"] = final_status
        if final_status == "COMPLETE":
            ok = _invoke_scanner(triggered_by=f"auto-ingest:{key}")
            summary["scanner_triggered"] = ok
    return _resp(200, summary)


def _start_kb_ingestion() -> str | None:
    for attempt in range(1, INGEST_START_MAX_ATTEMPTS + 1):
        try:
            resp = bedrock_agent.start_ingestion_job(
                knowledgeBaseId=KB_ID,
                dataSourceId=KB_DATA_SOURCE_ID,
                description=f"auto-trigger {_now_iso()}",
            )
            job_id = resp["ingestionJob"]["ingestionJobId"]
            logger.info("KB ingestion job started: %s (kb=%s ds=%s)", job_id, KB_ID, KB_DATA_SOURCE_ID)
            return job_id
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code == "ConflictException" and attempt < INGEST_START_MAX_ATTEMPTS:
                logger.info(
                    "KB ingestion already running; retrying start attempt %d/%d in %ds",
                    attempt,
                    INGEST_START_MAX_ATTEMPTS,
                    INGEST_POLL_INTERVAL_S,
                )
                time.sleep(INGEST_POLL_INTERVAL_S)
                continue
            logger.exception("StartIngestionJob failed (kb=%s ds=%s)", KB_ID, KB_DATA_SOURCE_ID)
            return None
    return None


def _wait_for_ingestion(job_id: str) -> str:
    """Poll the KB ingestion job until terminal status. Returns the final status."""
    deadline = time.time() + INGEST_POLL_TIMEOUT_S
    last_status = "IN_PROGRESS"
    while time.time() < deadline:
        try:
            resp = bedrock_agent.get_ingestion_job(
                knowledgeBaseId=KB_ID, dataSourceId=KB_DATA_SOURCE_ID, ingestionJobId=job_id,
            )
            last_status = resp["ingestionJob"]["status"]
            logger.info("Ingestion %s status: %s", job_id, last_status)
            if last_status in ("COMPLETE", "FAILED", "STOPPED"):
                return last_status
        except ClientError as e:
            logger.exception("GetIngestionJob failed")
            return "ERROR"
        time.sleep(INGEST_POLL_INTERVAL_S)
    logger.warning("Ingestion %s did not finish within %ds (last=%s)",
                   job_id, INGEST_POLL_TIMEOUT_S, last_status)
    return "TIMEOUT"


def _invoke_scanner(triggered_by: str) -> bool:
    if not SCANNER_LAMBDA_NAME:
        logger.info("SCANNER_LAMBDA_NAME unset — skipping scanner invoke")
        return False
    try:
        lambda_client.invoke(
            FunctionName=SCANNER_LAMBDA_NAME,
            InvocationType="Event",
            Payload=json.dumps({"triggered_by": triggered_by}).encode("utf-8"),
        )
        logger.info("Scanner async-invoked (triggered_by=%s)", triggered_by)
        return True
    except ClientError as e:
        logger.exception("Scanner invoke failed")
        return False


# ──────────────────────────── structured (.csv) path ────────────
def _is_structured(key: str) -> bool:
    return key.lower().endswith(".csv")


def _structured_dataset(key: str) -> str:
    """Classify a structured export by FILENAME (uploads aren't source-prefixed)."""
    name = key.rsplit("/", 1)[-1].lower()
    normalized = name.replace("-", "_").replace(" ", "_")
    if "ar_invoice" in normalized or "ar_invoices" in normalized:
        return "ar_invoices"
    if "ap_invoice" in normalized or "ap_invoices" in normalized:
        return "ap_invoices"
    if "aws_config" in normalized or "awsconfig" in normalized:
        return "aws_config"
    if "zscaler" in name:
        return "zscaler_rules"
    if "paloalto" in name or "pan-os" in name or "panos" in name:
        return "paloalto_rules"
    return "misc"


def _staged_structured_key(key: str, dataset: str) -> str:
    name = key.rsplit("/", 1)[-1]
    if dataset != "misc":
        # Known canonical datasets intentionally replace their one stable file.
        return f"{STRUCTURED_PREFIX}{dataset}/{dataset}.csv"

    # Unknown CSVs must not overwrite each other. Strip the upload timestamp
    # prefix added by /uploads/presign, then stage each CSV under its own folder.
    original = re.sub(r"^\d{8}T\d{6}Z-", "", name)
    stem = original.rsplit(".", 1)[0]
    safe_stem = re.sub(r"[^A-Za-z0-9_]+", "_", stem).strip("_").lower()[:120] or "dataset"
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", original).strip("._-")[:200] or f"{safe_stem}.csv"
    if not safe_name.lower().endswith(".csv"):
        safe_name = f"{safe_name}.csv"
    return f"{STRUCTURED_PREFIX}staged/{safe_stem}/{safe_name}"


def _start_crawler() -> bool:
    if not GLUE_CRAWLER_NAME:
        logger.info("GLUE_CRAWLER_NAME unset — skipping crawler trigger")
        return False
    try:
        glue.start_crawler(Name=GLUE_CRAWLER_NAME)
        logger.info("Glue crawler started: %s", GLUE_CRAWLER_NAME)
        return True
    except glue.exceptions.CrawlerRunningException:
        logger.info("Glue crawler %s already running — refresh will pick up the new file", GLUE_CRAWLER_NAME)
        return True
    except ClientError:
        logger.exception("StartCrawler failed")
        return False


def _handle_structured_object(bucket: str, key: str) -> dict:
    """Preserve a .csv export in processed and mirror it for Glue.

    Re-scan is intentionally NOT inline: Glue crawls take minutes, and a Lambda
    poll would be the wait-billing anti-pattern. The scan runs via "Run AI Scan"
    or the future crawler-completed orchestration (Documents/policy_scan_flow.md).
    """
    dataset = _structured_dataset(key)
    dest_key = _staged_structured_key(key, dataset)
    try:
        s3.copy_object(
            Bucket=PROCESSED_BUCKET, Key=key,
            CopySource={"Bucket": bucket, "Key": key},
            ServerSideEncryption="aws:kms", MetadataDirective="COPY",
        )
        s3.copy_object(
            Bucket=PROCESSED_BUCKET, Key=dest_key,
            CopySource={"Bucket": bucket, "Key": key},
            ServerSideEncryption="aws:kms", MetadataDirective="COPY",
        )
        s3.delete_object(Bucket=bucket, Key=key)
    except ClientError as e:
        logger.exception("structured copy failed")
        return _resp(502, {"key": key, "structured": True, "error": f"copy:{e}"})

    started = _start_crawler()
    logger.info("Structured export catalogued: %s → s3://%s/%s (crawler_started=%s)",
                key, PROCESSED_BUCKET, dest_key, started)
    return _resp(200, {
        "key": key,
        "structured": True,
        "dataset": dataset,
        "processed_key": key,
        "dest_key": dest_key,
        "crawler_started": started,
        "note": "Run a scan after the crawler completes to pick up the new data.",
    })


# ──────────────────────────── per-object processing ─────────────
def _process_one(obj: dict) -> tuple[str, str]:
    """Returns (action, error). action ∈ {MOVED, SKIPPED_EXISTS, FAILED}.

    Caller is expected to have already filtered out REPORTS_PREFIX keys and
    folder markers.
    """
    src_key = obj["Key"]

    # Destination existence check
    try:
        s3.head_object(Bucket=PROCESSED_BUCKET, Key=src_key)
        return "SKIPPED_EXISTS", ""
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code not in ("404", "NoSuchKey", "NotFound"):
            return "FAILED", f"head:{code}:{e}"

    # Copy
    try:
        s3.copy_object(
            Bucket=PROCESSED_BUCKET,
            Key=src_key,
            CopySource={"Bucket": RAW_BUCKET, "Key": src_key},
            ServerSideEncryption="aws:kms",
            MetadataDirective="COPY",
        )
    except ClientError as e:
        return "FAILED", f"copy:{e}"

    # Delete source (true move). If this fails, the next run sees dest exists
    # and skips — idempotent.
    try:
        s3.delete_object(Bucket=RAW_BUCKET, Key=src_key)
    except ClientError as e:
        return "FAILED", f"delete:{e}"

    return "MOVED", ""


# ──────────────────────────── reporting ─────────────────────────
def _row(obj: dict, action: str, error: str) -> list:
    src_key = obj["Key"]
    return [
        _now_iso(),
        RAW_BUCKET,
        src_key,
        PROCESSED_BUCKET,
        src_key,                              # destination mirrors source key
        action,
        obj.get("Size", 0),
        (obj.get("ETag") or "").strip('"'),
        error,
    ]


def _write_report(key: str, rows: list[list]) -> None:
    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
    writer.writerow(CSV_HEADER)
    writer.writerows(rows)
    s3.put_object(
        Bucket=RAW_BUCKET,
        Key=key,
        Body=buf.getvalue().encode("utf-8"),
        ContentType="text/csv",
        ServerSideEncryption="aws:kms",
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
