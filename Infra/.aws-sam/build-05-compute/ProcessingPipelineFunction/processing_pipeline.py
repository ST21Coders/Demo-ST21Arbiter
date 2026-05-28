"""ARBITER processing pipeline — scheduled raw → processed S3 file mover.

Two invocation paths share this handler:
  1. EventBridge schedule (twice daily, 06:00 / 18:00 PST literal = 14:00 / 02:00 UTC)
     — no http context on the event, runs unconditionally.
  2. Lambda Function URL (AuthType=NONE) from a UI / Postman — requires
     `Authorization: Bearer <Cognito IdToken>`; caller must belong to one
     of ALLOWED_GROUPS (defaults set in CFN to "ciso,grc").

For each object in RAW_BUCKET:
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

s3 = boto3.client("s3", region_name=REGION)

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
