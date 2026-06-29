"""ARBITER API handler — routes UI calls to backend services.

Routes:
  POST /chat                                  → master AgentCore runtime
                                                (forwards session_id + actor_id
                                                so the master maintains memory
                                                and the conversation index).
  GET  /conversations                         → list user's sessions (DDB GSI query)
  GET  /conversations/{id}/messages           → message history (AgentCore Memory
                                                list_events, chronological order)
  GET  /conversations/{id}                    → conversation metadata (DDB row)
  POST /uploads/presign                       → presigned S3 PUT URL into the
                                                raw bucket under
                                                users/<sub>/<ts>-<filename>.
                                                Browser PUTs directly to S3.
  GET  /uploads/list?bucket=raw|processed     → list the caller's files in the
                                                named bucket (scoped to
                                                users/<sub>/ prefix).
  GET  /uploads/status?key=<raw-key>          → per-upload processing/catalog
                                                status for Data Pipeline rows.
  POST /data-grouping/materialize             → copy selected processed files
                                                into a project prefix and write
                                                project metadata.
  POST /data-grouping/start-crawler           → start Glue crawler for structured data
  POST /data-grouping/analyze-documents       → deterministic portfolio analysis
                                                for project documents in a group.
  GET  /config-drift/security-groups/current  → current EC2 security group snapshot
  POST /config-drift/security-groups/baseline → capture live security group baseline
  POST /config-drift/security-groups/check    → compare current SGs to baseline
  POST /config-drift/security-groups/revert   → execute expired allowlisted SG revert
  GET  /health                                → unauth health check

Note: POST /conversations and POST /conversations/{id}/messages were removed —
the master orchestrator now owns conversation index writes (PutItem on the
first turn of a session, UpdateItem on each turn) and message persistence
(create_event in AgentCore Memory).

Env vars:
  MASTER_AGENT_RUNTIME_ARN   ARN of the master AgentCore runtime
                             (populated by scripts/deploy_agents.py).
  SESSIONS_TABLE             DynamoDB table indexing conversations.
  MEMORY_ID                  AgentCore Memory ID (for message history reads).
  RAW_BUCKET                 S3 bucket for browser uploads (raw zone).
  PROCESSED_BUCKET           S3 bucket for processed files (read-only list).
  GLUE_CRAWLER_NAME          Optional structured crawler to start after project
                             files are materialized.
  S3_KMS_KEY_ARN             Optional. CMK ARN that encrypts both buckets;
                             when set, the presigned PUT includes the SSE-KMS
                             headers so the browser PUT succeeds.
  UPLOAD_URL_EXPIRES_SECONDS Optional. Presigned URL lifetime. Default 900s.
"""
import base64
import csv
import io
import json
import logging
import os
import re
import time
import zlib
from typing import Any
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from urllib.parse import unquote

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

import report_catalog
import report_data
import report_generators

logger = logging.getLogger()
logger.setLevel(logging.INFO)

REGION = os.environ.get("AWS_REGION", "us-east-1")
MASTER_AGENT_RUNTIME_ARN = os.environ.get("MASTER_AGENT_RUNTIME_ARN", "").strip()
# Specialist runtime ARNs for direct (per-agent) chat routing from the MCP page.
# Patched onto this Lambda by scripts/deploy_agents.py alongside the master ARN.
# A "target" of master (or absent) keeps the orchestrator fan-out behaviour.
SPECIALIST_RUNTIME_ARNS = {
    "master":     MASTER_AGENT_RUNTIME_ARN,
    "sharepoint": os.environ.get("SHAREPOINT_RUNTIME_ARN", "").strip(),
    "awsconfig":  os.environ.get("AWSCONFIG_RUNTIME_ARN", "").strip(),
    "zscaler":    os.environ.get("ZSCALER_RUNTIME_ARN", "").strip(),
    "paloalto":   os.environ.get("PALOALTO_RUNTIME_ARN", "").strip(),
    "structured": os.environ.get("STRUCTURED_RUNTIME_ARN", "").strip(),
    "jira":       os.environ.get("JIRA_RUNTIME_ARN", "").strip(),
    "servicenow": os.environ.get("SERVICENOW_RUNTIME_ARN", "").strip(),
}
SESSIONS_TABLE = os.environ.get("SESSIONS_TABLE", "")
CONFLICTS_TABLE = os.environ.get("CONFLICTS_TABLE", "")
CONFLICTS_TABLE_V2 = os.environ.get("CONFLICTS_TABLE_V2", "")
SCAN_RUNS_TABLE = os.environ.get("SCAN_RUNS_TABLE", "")
CHANGE_REQUESTS_TABLE = os.environ.get("CHANGE_REQUESTS_TABLE", "")
AUDIT_TABLE = os.environ.get("AUDIT_TABLE", "")
TOKEN_USAGE_TABLE = os.environ.get("TOKEN_USAGE_TABLE", "")
MEMORY_ID = os.environ.get("MEMORY_ID", "").strip()
RAW_BUCKET = os.environ.get("RAW_BUCKET", "").strip()
PROCESSED_BUCKET = os.environ.get("PROCESSED_BUCKET", "").strip()
REPORTS_BUCKET = os.environ.get("REPORTS_BUCKET", "").strip()
GLUE_CRAWLER_NAME = (
    os.environ.get("GLUE_CRAWLER_NAME", "").strip()
    or f"{os.environ.get('ENVIRONMENT', 'dev')}-{os.environ.get('PROJECT_NAME', 'st21arbiter-poc')}-structured-crawler"
)
REPORT_URL_EXPIRES_SECONDS = int(os.environ.get("REPORT_URL_EXPIRES_SECONDS", "86400"))
ORG_NAME = os.environ.get("ORG_NAME", "Meridian Insurance Group")
S3_KMS_KEY_ARN = os.environ.get("S3_KMS_KEY_ARN", "").strip()
SCANNER_LAMBDA_NAME = os.environ.get("SCANNER_LAMBDA_NAME", "").strip()
MCP_ENDPOINTS = [u.strip() for u in os.environ.get("MCP_ENDPOINTS", "").split(",") if u.strip()]
UPLOAD_URL_EXPIRES_SECONDS = int(os.environ.get("UPLOAD_URL_EXPIRES_SECONDS", "900"))
UPLOAD_PREFIX = "users/"            # per-user folder root inside each bucket
MAX_LIST_KEYS = 200                 # cap list responses; bucket-listing isn't paginated to the UI
CONFIG_DRIFT_ALLOWED_REVERT_SECURITY_GROUPS = {
    group_id.strip()
    for group_id in os.environ.get("CONFIG_DRIFT_ALLOWED_REVERT_SECURITY_GROUPS", "sg-0ff2704a0e3189977").split(",")
    if group_id.strip()
}

# Module-level flag toggled per-invocation in handler(). Safe because Lambda
# runs at most one invocation per container at a time. The Function URL adds
# CORS headers itself (via FunctionUrlConfig); emitting them again from the
# Lambda response creates duplicate Access-Control-Allow-Origin headers that
# browsers reject with "CORS Allow Origin Not Matching Origin". API Gateway
# does NOT auto-inject CORS on success responses, so the Lambda still has to
# emit them when invoked through the Gateway.
_emit_cors_headers = True

agentcore = boto3.client("bedrock-agentcore", region_name=REGION)
# Control-plane client for runtime lifecycle/status (list_agent_runtimes).
agentcore_control = boto3.client("bedrock-agentcore-control", region_name=REGION)
lambda_client = boto3.client("lambda", region_name=REGION)
ddb = boto3.resource("dynamodb", region_name=REGION)
sessions_table = ddb.Table(SESSIONS_TABLE) if SESSIONS_TABLE else None
# Read prefers conflicts-v2 when configured; falls back to legacy.
conflicts_table = ddb.Table(CONFLICTS_TABLE) if CONFLICTS_TABLE else None
conflicts_v2_table = ddb.Table(CONFLICTS_TABLE_V2) if CONFLICTS_TABLE_V2 else None
scan_runs_table = ddb.Table(SCAN_RUNS_TABLE) if SCAN_RUNS_TABLE else None
crs_table = ddb.Table(CHANGE_REQUESTS_TABLE) if CHANGE_REQUESTS_TABLE else None
audit_table = ddb.Table(AUDIT_TABLE) if AUDIT_TABLE else None
token_usage_table = ddb.Table(TOKEN_USAGE_TABLE) if TOKEN_USAGE_TABLE else None


def _findings_table():
    """Prefer the v2 (idempotent) table; legacy table is the fallback during migration."""
    return conflicts_v2_table or conflicts_table
# SigV4 + virtual-host addressing so presigned URLs are usable from any browser
# origin without an explicit region in the host.
s3 = boto3.client(
    "s3",
    region_name=REGION,
    config=BotoConfig(signature_version="s3v4", s3={"addressing_style": "virtual"}),
)
glue = boto3.client("glue", region_name=REGION)
ec2 = boto3.client("ec2", region_name=REGION)

# Anything outside this character class is replaced with '_' in upload keys.
# S3 accepts a wider set but browsers + presigned URLs handle this subset cleanly.
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


# ──────────────────────────── router ────────────────────────────
def handler(event, context):
    # Function URL events carry requestContext.http; API Gateway events do not.
    # Skip emitting CORS headers from the Lambda when invoked via Function URL
    # — the URL layer adds them and duplicates break the browser.
    global _emit_cors_headers
    _emit_cors_headers = "http" not in (event.get("requestContext") or {})

    # Log path + header names so we can debug auth issues without dumping
    # the full event (which can include JWTs in headers).
    _path = event.get("path") or event.get("rawPath", "")
    _hdr_names = sorted((event.get("headers") or {}).keys())
    logger.info("api_handler invoked: path=%s method=%s headers=%s",
                _path,
                event.get("httpMethod") or event.get("requestContext", {}).get("http", {}).get("method"),
                _hdr_names)
    path = event.get("path") or event.get("rawPath", "")
    method = (event.get("httpMethod") or
              event.get("requestContext", {}).get("http", {}).get("method", "")).upper()

    if path == "/health":
        return _ok({"status": "healthy", "service": "arbiter-api"})

    if path == "/chat" and method == "POST":
        return _handle_chat(event)

    if path == "/findings" and method == "GET":
        return _handle_list_findings(event)

    if path == "/actions" and method == "GET":
        return _handle_list_actions(event)

    if path == "/audit" and method == "GET":
        return _handle_list_audit(event)

    if path == "/token-usage" and method == "GET":
        return _handle_list_token_usage(event)

    if path == "/token-usage/summary" and method == "GET":
        return _handle_token_usage_summary(event)

    if path == "/conversations" and method == "GET":
        return _handle_list_conversations(event)

    if path == "/uploads/presign" and method == "POST":
        return _handle_uploads_presign(event)

    if path == "/uploads/list" and method == "GET":
        return _handle_uploads_list(event)

    if path == "/uploads/status" and method == "GET":
        return _handle_uploads_status(event)

    if path == "/data-grouping/materialize" and method == "POST":
        return _handle_data_grouping_materialize(event)

    if path == "/data-grouping/project" and method == "GET":
        return _handle_data_grouping_project(event)

    if path == "/data-grouping/projects" and method == "GET":
        return _handle_data_grouping_projects(event)

    if path == "/data-grouping/start-crawler" and method == "POST":
        return _handle_data_grouping_start_crawler(event)

    if path == "/data-grouping/analyze-documents" and method == "POST":
        return _handle_data_grouping_analyze_documents(event)

    if path == "/config-drift/security-groups/current" and method == "GET":
        return _handle_config_drift_security_groups_current(event)

    if path == "/config-drift/security-groups/baseline" and method == "GET":
        return _handle_config_drift_security_groups_get_baseline(event)

    if path == "/config-drift/security-groups/baseline" and method == "POST":
        return _handle_config_drift_security_groups_baseline(event)

    if path == "/config-drift/security-groups/check" and method == "POST":
        return _handle_config_drift_security_groups_check(event)

    if path == "/config-drift/security-groups/revert" and method == "POST":
        return _handle_config_drift_security_groups_revert(event)

    # Exact-match bulk-delete must precede the /conversations/{id} path-param
    # branch below, otherwise "bulk-delete" would be parsed as a session_id.
    if path == "/conversations/bulk-delete" and method == "POST":
        return _handle_bulk_delete_conversations(event)

    # Path param routes under /conversations/{session_id}
    if path.startswith("/conversations/"):
        tail = path[len("/conversations/"):].split("/", 1)
        session_id = tail[0]
        sub = tail[1] if len(tail) > 1 else ""
        if sub == "messages" and method == "GET":
            return _handle_get_messages(event, session_id)
        if not sub and method == "GET":
            return _handle_get_conversation(event, session_id)
        if not sub and method == "DELETE":
            return _handle_delete_conversation(event, session_id)

    # ── Dashboard + scanner additions ────────────────────────────
    if path == "/dashboard" and method == "GET":
        return _handle_dashboard(event)

    if path == "/mcp-health" and method == "GET":
        return _handle_mcp_health(event)

    if path == "/agent-status" and method == "GET":
        return _handle_agent_status(event)

    if path == "/jira/tickets" and method == "POST":
        return _handle_jira_create(event)

    if path == "/jira/transition" and method == "POST":
        return _handle_jira_transition(event)

    if path == "/jira/comment" and method == "POST":
        return _handle_jira_comment(event)

    if path == "/servicenow/impact-analysis" and method == "POST":
        return _handle_servicenow_impact(event)

    if path == "/servicenow/drift-scan" and method == "POST":
        return _handle_servicenow_drift(event)

    if path == "/scan/dry-run" and method == "POST":
        return _handle_scan_dry_run(event)

    if path == "/scan" and method == "POST":
        return _handle_scan_trigger(event)

    if path == "/scan-runs" and method == "GET":
        return _handle_list_scan_runs(event)

    if path.startswith("/scan-runs/") and method == "GET":
        scan_run_id = _path_param(event, "scan_run_id", path, "/scan-runs/")
        return _handle_get_scan_run(event, scan_run_id)

    if path.startswith("/findings/") and method == "GET":
        conflict_id = _path_param(event, "conflict_id", path, "/findings/")
        return _handle_get_finding(event, conflict_id)

    # ── Reporting + compliance scores ────────────────────────────
    if path == "/reports/catalog" and method == "GET":
        return _handle_reports_catalog(event)

    if path == "/reports/generate" and method == "POST":
        return _handle_reports_generate(event)

    if path == "/compliance/scores" and method == "GET":
        return _handle_compliance_scores(event)

    if path == "/compliance/report" and method == "POST":
        return _handle_compliance_report(event)

    # ── Change-request workflow (Step 4) ─────────────────────────
    if path == "/actions" and method == "POST":
        return _handle_create_action(event)

    if path.startswith("/actions/") and method == "POST":
        tail = path[len("/actions/"):].split("/", 1)
        cr_id = tail[0]
        sub = tail[1] if len(tail) > 1 else ""
        if sub == "approve":
            return _handle_action_transition(event, cr_id, "approve")
        if sub == "reject":
            return _handle_action_transition(event, cr_id, "reject")
        if sub == "execute":
            return _handle_action_transition(event, cr_id, "execute")
        if sub == "escalate":
            return _handle_action_transition(event, cr_id, "escalate")

    return _ok({"status": "stub", "path": path, "method": method})


# ──────────────────────────── /chat ─────────────────────────────
def _handle_chat(event):
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _err(400, "Invalid JSON body")
    prompt = (body.get("prompt") or body.get("message") or "").strip()
    if not prompt:
        return _err(400, "Missing 'prompt' in request body")

    selected_data_group = _canonical_structured_group_name((body.get("data_group") or "").strip())

    # Direct per-agent routing: the MCP page sends a "target" naming the
    # specialist to invoke; the Analyst page sends none → master orchestrator.
    # An unknown target falls back to master for backward compatibility.
    target = (body.get("target") or "master").strip().lower()
    if selected_data_group or _looks_like_structured_inventory_prompt(prompt):
        target = "structured"
    runtime_arn = SPECIALIST_RUNTIME_ARNS.get(target) or MASTER_AGENT_RUNTIME_ARN
    if not runtime_arn:
        return _err(503, f"Runtime ARN for '{target}' not configured (run scripts/deploy_agents.py)")

    actor_id = _caller_user_id(event) or "anonymous"
    # Frontend generates session_id when starting a new chat; "adhoc" means
    # the agent should not persist (no DDB row, no memory writes).
    session_id = (body.get("session_id") or "adhoc").strip()
    # chat_type lets us separately list Analyst vs MCP sessions in the UI.
    chat_type = (body.get("chat_type") or "analyst").strip() or "analyst"
    # Persona forwarded into the agent payload so master + specialists can
    # attribute their token-usage rows. Most-privileged group wins (mirrors
    # PersonaContext.jsx's GROUP_PRIORITY); default to 'employee' so an
    # unauthenticated invocation still partitions cleanly.
    groups = _caller_groups(event)
    persona = next((g for g in ("ciso", "soc", "grc", "employee") if g in groups), "employee")
    # Forward the caller's real email from the Cognito IdToken claims so token
    # usage rows can attribute spend to a human, not just a Cognito `sub`.
    claims = _caller_claims(event)
    user_email = (claims.get("email") or "")[:200]

    runtime_prompt = prompt
    if target == "structured":
        selected_group = selected_data_group
        explicit_group = selected_group or _extract_structured_group_context(prompt)
        if explicit_group:
            _remember_structured_group_context(session_id, actor_id, chat_type, prompt, explicit_group)
            if selected_group and selected_group not in prompt:
                runtime_prompt = f"Use the {selected_group} group. {prompt}"
        elif session_id != "adhoc":
            stored_group = _load_structured_group_context(session_id)
            if stored_group:
                runtime_prompt = f"Use the {stored_group} group. {prompt}"

    try:
        resp = agentcore.invoke_agent_runtime(
            agentRuntimeArn=runtime_arn,
            payload=json.dumps({
                "prompt": runtime_prompt,
                "session_id": session_id,
                "actor_id": actor_id,
                "chat_type": chat_type,
                "persona": persona,
                "user_email": user_email,
            }).encode("utf-8"),
            contentType="application/json",
            accept="application/json",
        )
        raw = resp["response"].read().decode("utf-8")
        parsed = json.loads(raw)
        reply = parsed.get("result", raw)
        if target == "structured":
            reply_group = _extract_structured_group_context(reply)
            if reply_group:
                _remember_structured_group_context(session_id, actor_id, chat_type, prompt, reply_group)
        return _ok({
            "reply": reply,
            "session_id": session_id,  # echo so frontend can correlate
        })
    except Exception as e:
        logger.exception("AgentCore invocation failed")
        return _err(502, f"{type(e).__name__}: {e}")


def _extract_structured_group_context(text: str) -> str:
    """Return a Data Grouping project group name mentioned in user/model text."""
    if not text:
        return ""
    match = re.search(r"\b(Project_[A-Za-z0-9_]+)\b", text)
    if match:
        return _canonical_structured_group_name(match.group(1))
    match = re.search(r"Group:\s*([A-Za-z0-9_]+)", text)
    if match:
        return _canonical_structured_group_name(match.group(1))
    return ""


def _looks_like_structured_inventory_prompt(text: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()
    return (
        ("available" in normalized or "list" in normalized)
        and "tables" in normalized
        and "files" in normalized
        and ("group" in normalized or "project" in normalized)
    )


def _canonical_structured_group_name(group: str) -> str:
    if not group:
        return ""
    parts = [part for part in re.split(r"[^A-Za-z0-9]+", group) if part]
    if not parts:
        return ""
    if parts[0].lower() == "project":
        parts = parts[1:]
    return "Project_" + "_".join(part[:1].upper() + part[1:] for part in parts)


def _load_structured_group_context(session_id: str) -> str:
    if not sessions_table or not session_id or session_id == "adhoc":
        return ""
    try:
        resp = sessions_table.get_item(Key={"session_id": session_id})
    except Exception:
        logger.exception("Structured group context lookup failed")
        return ""
    item = resp.get("Item") or {}
    return (item.get("structured_group_context") or "").strip()


def _remember_structured_group_context(session_id: str, user_id: str, chat_type: str, prompt: str, group: str) -> None:
    if not sessions_table or not session_id or session_id == "adhoc" or not group:
        return
    now = datetime.now(timezone.utc).isoformat()
    title = (prompt or group).strip()[:80] or group
    try:
        sessions_table.update_item(
            Key={"session_id": session_id},
            UpdateExpression=(
                "SET structured_group_context = :group, "
                "user_id = if_not_exists(user_id, :user_id), "
                "#title = if_not_exists(#title, :title), "
                "chat_type = if_not_exists(chat_type, :chat_type), "
                "created_at = if_not_exists(created_at, :now), "
                "last_message_at = :now"
            ),
            ExpressionAttributeNames={"#title": "title"},
            ExpressionAttributeValues={
                ":group": group,
                ":user_id": user_id or "anonymous",
                ":title": title,
                ":chat_type": chat_type or "mcp",
                ":now": now,
            },
        )
    except Exception:
        logger.exception("Structured group context update failed")


# ──────────────────────────── /uploads ──────────────────────────
def _handle_uploads_presign(event):
    """Return a presigned PUT URL into the raw bucket.

    Body: {"filename": "...", "contentType": "application/octet-stream"}
    Response: {"url", "method": "PUT", "key", "bucket", "expires_in", "headers"}

    The browser then does:
        fetch(url, { method: "PUT", headers, body: file })

    Keys are namespaced per caller (users/<sub>/<ts>-<safe-filename>) so the
    /uploads/list endpoint can return only what the caller uploaded.
    """
    if not RAW_BUCKET:
        return _err(500, "RAW_BUCKET not configured")
    user_id = _caller_user_id(event)
    if not user_id:
        return _err(401, "Could not resolve caller identity")
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _err(400, "Invalid JSON body")

    raw_name = (body.get("filename") or "").strip()
    if not raw_name:
        return _err(400, "Missing 'filename' in request body")
    content_type = (body.get("contentType") or "application/octet-stream").strip()

    safe_name = _SAFE_FILENAME_RE.sub("_", raw_name)[-200:].lstrip("_") or "file"
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    key = f"{UPLOAD_PREFIX}{user_id}/{ts}-{safe_name}"

    put_params: dict[str, Any] = {
        "Bucket": RAW_BUCKET,
        "Key": key,
        "ContentType": content_type,
    }
    headers: dict[str, str] = {"Content-Type": content_type}
    if S3_KMS_KEY_ARN:
        put_params["ServerSideEncryption"] = "aws:kms"
        put_params["SSEKMSKeyId"] = S3_KMS_KEY_ARN
        # Browser must echo the same SSE headers it agreed to in the signature.
        headers["x-amz-server-side-encryption"] = "aws:kms"
        headers["x-amz-server-side-encryption-aws-kms-key-id"] = S3_KMS_KEY_ARN

    try:
        url = s3.generate_presigned_url(
            ClientMethod="put_object",
            Params=put_params,
            ExpiresIn=UPLOAD_URL_EXPIRES_SECONDS,
            HttpMethod="PUT",
        )
    except ClientError as e:
        logger.exception("presign failed")
        return _err(502, f"{type(e).__name__}: {e}")

    return _ok({
        "url": url,
        "method": "PUT",
        "bucket": RAW_BUCKET,
        "key": key,
        "expires_in": UPLOAD_URL_EXPIRES_SECONDS,
        "headers": headers,
    })


def _handle_uploads_list(event):
    """List the caller's files in the raw or processed bucket.

    Query string: ?bucket=raw|processed
    Response: {"bucket", "prefix", "files": [{key, name, size, last_modified}], "truncated"}
    """
    qs = event.get("queryStringParameters") or {}
    which = (qs.get("bucket") or "raw").strip().lower()
    if which == "raw":
        bucket = RAW_BUCKET
    elif which == "processed":
        bucket = PROCESSED_BUCKET
    else:
        return _err(400, "bucket must be 'raw' or 'processed'")
    if not bucket:
        return _err(500, f"{which.upper()}_BUCKET not configured")

    user_id = _caller_user_id(event)
    if not user_id:
        return _err(401, "Could not resolve caller identity")
    prefix = f"{UPLOAD_PREFIX}{user_id}/"

    files = []
    truncated = False
    continuation_token = None
    max_keys = 2000 if which == "processed" else MAX_LIST_KEYS
    try:
        while len(files) < max_keys:
            kwargs = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": min(1000, max_keys - len(files))}
            if continuation_token:
                kwargs["ContinuationToken"] = continuation_token
            resp = s3.list_objects_v2(**kwargs)
            for obj in resp.get("Contents") or []:
                key = obj["Key"]
                if key.endswith("/"):
                    continue  # folder marker
                lm = obj.get("LastModified")
                files.append({
                    "key": key,
                    "name": key[len(prefix):],   # strip the user-namespace prefix
                    "size": int(obj.get("Size") or 0),
                    "last_modified": lm.isoformat() if hasattr(lm, "isoformat") else str(lm or ""),
                })
            if not resp.get("IsTruncated"):
                break
            continuation_token = resp.get("NextContinuationToken")
            if not continuation_token:
                break
        truncated = bool(resp.get("IsTruncated")) if "resp" in locals() else False
    except ClientError as e:
        logger.exception("list_objects_v2 failed")
        return _err(502, f"{type(e).__name__}: {e}")

    files.sort(key=lambda f: f["last_modified"], reverse=True)
    return _ok({
        "bucket": bucket,
        "prefix": prefix,
        "files": files,
        "truncated": truncated,
        "max_keys": max_keys,
    })


def _upload_status_head(bucket: str, key: str) -> dict[str, Any]:
    try:
        obj = s3.head_object(Bucket=bucket, Key=key)
        return {
            "exists": True,
            "size": int(obj.get("ContentLength") or 0),
            "lastModified": obj.get("LastModified"),
        }
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in {"404", "NoSuchKey", "NotFound"}:
            return {"exists": False}
        raise


def _structured_upload_dataset(key: str) -> str:
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


def _structured_status_key(key: str, dataset: str) -> str:
    if dataset != "misc":
        return f"structured/{dataset}/{dataset}.csv"
    name = key.rsplit("/", 1)[-1]
    original = re.sub(r"^\d{8}T\d{6}Z-", "", name)
    stem = original.rsplit(".", 1)[0]
    safe_stem = re.sub(r"[^A-Za-z0-9_]+", "_", stem).strip("_").lower()[:120] or "dataset"
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", original).strip("._-")[:200] or f"{safe_stem}.csv"
    if not safe_name.lower().endswith(".csv"):
        safe_name = f"{safe_name}.csv"
    return f"structured/staged/{safe_stem}/{safe_name}"


def _handle_uploads_status(event):
    qs = event.get("queryStringParameters") or {}
    key = unquote((qs.get("key") or "").strip())
    if not key:
        return _err(400, "Missing key")
    user_id = _caller_user_id(event)
    if not user_id:
        return _err(401, "Could not resolve caller identity")
    caller_prefix = f"{UPLOAD_PREFIX}{user_id}/"
    if not key.startswith(caller_prefix):
        return _err(403, "key is outside caller upload prefix")
    if not RAW_BUCKET or not PROCESSED_BUCKET:
        return _err(500, "RAW_BUCKET or PROCESSED_BUCKET not configured")

    is_csv = key.lower().endswith(".csv")
    try:
        raw = _upload_status_head(RAW_BUCKET, key)
        processed = _upload_status_head(PROCESSED_BUCKET, key)
        structured = None
        crawler = None
        status = "processing"
        message = "Waiting for processing pipeline"

        if is_csv:
            dataset = _structured_upload_dataset(key)
            structured_key = _structured_status_key(key, dataset)
            structured = {
                "dataset": dataset,
                "key": structured_key,
                **_upload_status_head(PROCESSED_BUCKET, structured_key),
            }
            if structured.get("exists"):
                status = "catalog_running"
                message = "Structured CSV staged; Glue crawler is refreshing"
            if GLUE_CRAWLER_NAME:
                crawler_obj = glue.get_crawler(Name=GLUE_CRAWLER_NAME).get("Crawler", {})
                last_crawl = crawler_obj.get("LastCrawl") or {}
                crawler = {
                    "name": GLUE_CRAWLER_NAME,
                    "state": crawler_obj.get("State") or "UNKNOWN",
                    "lastCrawl": last_crawl,
                }
                last_status = last_crawl.get("Status")
                if structured.get("exists") and crawler["state"] == "RUNNING":
                    status = "catalog_running"
                    message = "Structured CSV staged; Glue crawler is running"
                elif structured.get("exists") and last_status == "SUCCEEDED":
                    status = "catalog_done"
                    message = "Structured CSV staged and latest crawler run succeeded"
                elif structured.get("exists") and last_status == "FAILED":
                    status = "catalog_failed"
                    message = last_crawl.get("ErrorMessage") or "Latest crawler run failed"
                elif structured.get("exists"):
                    status = "catalog_waiting"
                    message = "Structured CSV staged; waiting for crawler result"
            elif structured.get("exists"):
                status = "catalog_waiting"
                message = "Structured CSV staged; Glue crawler is not configured"
        elif processed.get("exists"):
            status = "processed"
            message = "File moved to processed storage"

        return _ok({
            "key": key,
            "isCsv": is_csv,
            "status": status,
            "message": message,
            "raw": raw,
            "processed": processed,
            "structured": structured,
            "crawler": crawler,
        })
    except ClientError as e:
        logger.exception("upload status failed")
        return _err(502, f"{type(e).__name__}: {e}")


# ──────────────────────────── /data-grouping ────────────────────
def _s3_segment(value: str, default: str = "item") -> str:
    """S3-prefix-safe path segment for project/group/table names."""
    cleaned = _SAFE_FILENAME_RE.sub("_", (value or "").strip()).strip("._-")
    return cleaned[:160] or default


def _table_segment(value: str, default: str = "dataset") -> str:
    """Glue/Athena-friendly lowercase identifier segment."""
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", (value or "").strip()).strip("_").lower()
    return cleaned[:120] or default


def _dataset_name_from_file(filename: str) -> str:
    stem = re.sub(r"\.[^.]+$", "", filename or "")
    return re.sub(r"^\d{8}T\d{6}Z[-_]+", "", stem, flags=re.IGNORECASE)


def _csv_header_signature(bucket: str, key: str) -> tuple[str, ...]:
    try:
        obj = s3.get_object(Bucket=bucket, Key=key, Range="bytes=0-65535")
        sample = obj.get("Body").read().decode("utf-8-sig", errors="replace")
    except ClientError:
        logger.exception("csv header read failed: %s", key)
        return ()

    reader = csv.reader(io.StringIO(sample))
    for row in reader:
        cleaned = tuple(str(cell or "").strip().lower() for cell in row)
        if any(cleaned):
            return cleaned
    return ()


def _csv_structured_table_hints(project_id: str, group_name: str, files: list[dict[str, Any]]) -> dict[str, str]:
    csv_files = [
        file for file in files
        if str(file.get("name") or file.get("key") or "").lower().endswith(".csv")
    ]
    group_table = _table_segment(f"{project_id}_{group_name}", f"{project_id}_{group_name}")
    if len(csv_files) <= 1:
        return {
            str(file.get("key") or ""): _table_segment(
                f"{project_id}_{group_name}_{_dataset_name_from_file(file.get('name') or file.get('key') or 'csv')}",
                group_table,
            )
            for file in csv_files
            if file.get("key")
        }

    signatures = {
        str(file.get("key") or ""): _csv_header_signature(PROCESSED_BUCKET, str(file.get("key") or ""))
        for file in csv_files
        if file.get("key")
    }
    unique_signatures = {signature for signature in signatures.values() if signature}
    if len(unique_signatures) == 1 and len(signatures) == len(csv_files):
        return {str(file.get("key") or ""): group_table for file in csv_files if file.get("key")}

    return {
        str(file.get("key") or ""): _table_segment(
            f"{project_id}_{group_name}_{_dataset_name_from_file(file.get('name') or file.get('key') or 'csv')}",
            group_table,
        )
        for file in csv_files
        if file.get("key")
    }


def _delete_s3_prefix(bucket: str, prefix: str) -> list[str]:
    deleted: list[str] = []
    token = None
    while True:
        params = {"Bucket": bucket, "Prefix": prefix}
        if token:
            params["ContinuationToken"] = token
        resp = s3.list_objects_v2(**params)
        keys = [obj["Key"] for obj in resp.get("Contents") or [] if obj.get("Key")]
        for start in range(0, len(keys), 1000):
            batch = keys[start:start + 1000]
            if not batch:
                continue
            s3.delete_objects(
                Bucket=bucket,
                Delete={"Objects": [{"Key": key} for key in batch], "Quiet": True},
            )
            deleted.extend(batch)
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")
    return deleted


def _load_data_grouping_metadata(bucket: str, metadata_key: str) -> dict[str, Any] | None:
    try:
        obj = s3.get_object(Bucket=bucket, Key=metadata_key)
        return json.loads(obj.get("Body").read().decode("utf-8"))
    except s3.exceptions.NoSuchKey:
        return None
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return None
        raise
    except Exception:
        logger.exception("data grouping metadata read failed")
        return None


def _handle_data_grouping_project(event):
    """Return persisted S3 project metadata for Data Grouping.

    The UI uses this as the assignment source of truth so a processed source
    file that was already materialized into a project group cannot be added to
    a second group after a browser refresh or another session.
    """
    if not PROCESSED_BUCKET:
        return _err(500, "PROCESSED_BUCKET not configured")
    user_id = _caller_user_id(event)
    if not user_id:
        return _err(401, "Could not resolve caller identity")

    qs = event.get("queryStringParameters") or {}
    project_id = _s3_segment(qs.get("projectId") or qs.get("projectName"), "project")
    metadata_key = f"projects/{project_id}/metadata/project.json"
    try:
        metadata = _load_data_grouping_metadata(PROCESSED_BUCKET, metadata_key)
    except ClientError as e:
        logger.exception("project metadata read failed")
        return _err(502, f"metadata read: {type(e).__name__}: {e}")

    if not metadata:
        return _ok({
            "projectId": project_id,
            "metadataKey": metadata_key,
            "exists": False,
            "groups": [],
            "assignedSourceKeys": [],
        })

    caller_prefix = f"{UPLOAD_PREFIX}{user_id}/"
    groups = [group for group in metadata.get("groups", []) if isinstance(group, dict)]
    assigned_source_keys: list[str] = []
    for group in groups:
        for file in group.get("files") or []:
            if not isinstance(file, dict):
                continue
            source_key = str(file.get("sourceKey") or "")
            if source_key.startswith(caller_prefix):
                assigned_source_keys.append(source_key)

    return _ok({
        "projectId": metadata.get("projectId") or project_id,
        "projectName": metadata.get("projectName") or "",
        "metadataKey": metadata_key,
        "exists": True,
        "updatedAt": metadata.get("updatedAt"),
        "groups": groups,
        "assignedSourceKeys": sorted(set(assigned_source_keys)),
    })


def _collect_data_grouping_table_hints(group: dict[str, Any]) -> set[str]:
    hints: set[str] = set()
    for key in ("structuredTableHint", "glueTableHint"):
        value = group.get(key)
        if isinstance(value, str) and value:
            hints.add(value)
    for value in group.get("structuredTableHints") or []:
        if isinstance(value, str) and value:
            hints.add(value)
    for file_info in group.get("files") or []:
        value = file_info.get("glueTableHint") if isinstance(file_info, dict) else None
        if isinstance(value, str) and value:
            hints.add(value)
    return hints


def _handle_data_grouping_projects(event):
    """Return compact Data Grouping project/group options for chat scoping."""
    if not PROCESSED_BUCKET:
        return _err(500, "PROCESSED_BUCKET not configured")
    user_id = _caller_user_id(event)
    if not user_id:
        return _err(401, "Could not resolve caller identity")

    groups: list[dict[str, Any]] = []
    token = None
    try:
        while True:
            params = {"Bucket": PROCESSED_BUCKET, "Prefix": "projects/", "MaxKeys": 1000}
            if token:
                params["ContinuationToken"] = token
            resp = s3.list_objects_v2(**params)
            for obj in resp.get("Contents") or []:
                key = obj.get("Key", "")
                if not key.endswith("/metadata/project.json"):
                    continue
                metadata = _load_data_grouping_metadata(PROCESSED_BUCKET, key)
                if not metadata:
                    continue
                project_id = metadata.get("projectId") or ""
                project_name = metadata.get("projectName") or project_id or "Unnamed project"
                for group in metadata.get("groups") or []:
                    if not isinstance(group, dict):
                        continue
                    group_name = group.get("name") or group.get("id")
                    if not group_name:
                        continue
                    files = group.get("files") or []
                    table_hints = _collect_data_grouping_table_hints(group)
                    groups.append({
                        "id": f"{project_id}::{group_name}",
                        "projectId": project_id,
                        "projectName": project_name,
                        "groupName": group_name,
                        "label": f"{project_name} / {group_name}",
                        "value": group_name,
                        "fileCount": len(files),
                        "csvCount": sum(1 for item in files if item.get("type") == "csv"),
                        "tableCount": len(table_hints),
                        "tableHints": sorted(table_hints)[:40],
                        "files": [
                            {
                                "name": item.get("name") or item.get("filename") or item.get("key"),
                                "type": item.get("type") or "file",
                                "glueTableHint": item.get("glueTableHint"),
                            }
                            for item in files[:100]
                            if isinstance(item, dict)
                        ],
                        "updatedAt": metadata.get("updatedAt") or metadata.get("createdAt") or "",
                    })
            if not resp.get("IsTruncated"):
                break
            token = resp.get("NextContinuationToken")
            if not token:
                break
    except ClientError as e:
        logger.exception("project group list failed")
        return _err(502, f"project list: {type(e).__name__}: {e}")

    groups.sort(key=lambda item: (item.get("updatedAt") or "", item.get("label") or ""), reverse=True)
    return _ok({"groups": groups[:250], "truncated": len(groups) > 250})


def _handle_data_grouping_materialize(event):
    """Materialize locally-defined project groups into S3.

    Body:
      {
        "projectId": "vendor-audit-june-2026",
        "projectName": "Vendor Audit June 2026",
        "groups": [
          {"id": "...", "name": "AR_Invoices", "type": "audit",
           "files": [{"key": "users/<sub>/...", "name": "AR_...csv"}]}
        ],
        "move": true
      }

    Copies each selected processed object to:
      projects/<projectId>/<groupName>/<filename>
    and copies CSVs to:
      structured/<projectId>_<groupName>/<filename>
    then writes:
      projects/<projectId>/metadata/project.json
    """
    if not PROCESSED_BUCKET:
        return _err(500, "PROCESSED_BUCKET not configured")
    user_id = _caller_user_id(event)
    if not user_id:
        return _err(401, "Could not resolve caller identity")
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _err(400, "Invalid JSON body")

    project_id = _s3_segment(body.get("projectId") or body.get("projectName"), "project")
    project_name = (body.get("projectName") or project_id).strip()[:200]
    groups = body.get("groups") or []
    delete_groups = body.get("deleteGroups") or []
    if not isinstance(groups, list):
        return _err(400, "groups must be a list")
    if not isinstance(delete_groups, list):
        return _err(400, "deleteGroups must be a list")
    if not groups and not delete_groups:
        return _err(400, "At least one group or deleteGroups entry is required")

    caller_prefix = f"{UPLOAD_PREFIX}{user_id}/"
    copied: list[dict[str, Any]] = []
    structured_copies: list[dict[str, Any]] = []
    moved_sources: set[str] = set()
    materialized_groups: list[dict[str, Any]] = []
    move_sources = bool(body.get("move", True))
    deleted: list[str] = []

    metadata_key = f"projects/{project_id}/metadata/project.json"
    existing_metadata = _load_data_grouping_metadata(PROCESSED_BUCKET, metadata_key) or {}
    existing_groups = [
        group for group in existing_metadata.get("groups", [])
        if isinstance(group, dict)
    ]
    groups_to_remove = []
    incoming_ids = {str(group.get("id") or "") for group in groups if isinstance(group, dict)}
    incoming_names = {
        _s3_segment(group.get("name") or "", "")
        for group in groups
        if isinstance(group, dict) and group.get("name")
    }
    delete_ids = {str(group.get("id") or "") for group in delete_groups if isinstance(group, dict)}
    delete_names = {
        _s3_segment(group.get("name") or "", "")
        for group in delete_groups
        if isinstance(group, dict) and group.get("name")
    }
    for existing in existing_groups:
        existing_id = str(existing.get("id") or "")
        existing_name = _s3_segment(existing.get("name") or "", "")
        if (
            existing_id in incoming_ids
            or existing_name in incoming_names
            or existing_id in delete_ids
            or existing_name in delete_names
        ):
            groups_to_remove.append(existing)

    removed_table_hints = set()
    for group in groups_to_remove:
        if group.get("structuredTableHint"):
            removed_table_hints.add(str(group.get("structuredTableHint")))
        removed_table_hints.update(str(hint) for hint in (group.get("structuredTableHints") or []) if hint)
    structured_deleted = False
    for existing in groups_to_remove:
        structured_prefixes = [
            f"structured/{hint}/"
            for hint in (
                [existing.get("structuredTableHint")] + list(existing.get("structuredTableHints") or [])
            )
            if hint
        ]
        for prefix in (existing.get("targetPrefix"), *structured_prefixes):
            if not prefix:
                continue
            try:
                deleted.extend(_delete_s3_prefix(PROCESSED_BUCKET, str(prefix)))
                if str(prefix).startswith("structured/"):
                    structured_deleted = True
            except ClientError as e:
                logger.exception("group prefix cleanup failed")
                return _err(502, f"delete {prefix}: {type(e).__name__}: {e}")

    retained_groups = [
        group for group in existing_groups
        if group not in groups_to_remove
    ]

    for index, group in enumerate(groups):
        group_name = _s3_segment(group.get("name") or f"group_{index + 1}", f"group_{index + 1}")
        group_table_name = _table_segment(f"{project_id}_{group_name}", f"{project_id}_group_{index + 1}")
        files = [f for f in (group.get("files") or []) if isinstance(f, dict) and f.get("key")]
        structured_table_hints_by_key = _csv_structured_table_hints(project_id, group_name, files)
        structured_table_hints = sorted(set(structured_table_hints_by_key.values()))
        cleanup_prefixes = [
            f"projects/{project_id}/{group_name}/",
            f"structured/{group_table_name}/",
            *[f"structured/{hint}/" for hint in structured_table_hints],
        ]
        for prefix in dict.fromkeys(cleanup_prefixes):
            try:
                deleted.extend(_delete_s3_prefix(PROCESSED_BUCKET, prefix))
                if prefix.startswith("structured/"):
                    structured_deleted = True
            except ClientError as e:
                logger.exception("group prefix cleanup failed")
                return _err(502, f"delete {prefix}: {type(e).__name__}: {e}")
        materialized_files: list[dict[str, Any]] = []
        for file in files:
            source_key = str(file.get("key") or "")
            if not source_key.startswith(caller_prefix) and not source_key.startswith("structured/"):
                return _err(403, f"Source key is outside allowed processed prefixes: {source_key}")
            filename = _s3_segment(file.get("name") or source_key.rsplit("/", 1)[-1], "file")
            project_key = f"projects/{project_id}/{group_name}/{filename}"
            copy_source = {"Bucket": PROCESSED_BUCKET, "Key": source_key}
            try:
                s3.copy_object(
                    Bucket=PROCESSED_BUCKET,
                    Key=project_key,
                    CopySource=copy_source,
                    MetadataDirective="COPY",
                )
            except ClientError as e:
                logger.exception("project materialize copy failed")
                return _err(502, f"copy {source_key}: {type(e).__name__}: {e}")
            copied.append({"sourceKey": source_key, "destinationKey": project_key})
            file_entry = {
                "name": filename,
                "sourceKey": source_key,
                "projectKey": project_key,
                "type": "csv" if filename.lower().endswith(".csv") else "file",
            }

            if filename.lower().endswith(".csv"):
                structured_table_hint = structured_table_hints_by_key.get(source_key, group_table_name)
                structured_key = f"structured/{structured_table_hint}/{filename}"
                try:
                    s3.copy_object(
                        Bucket=PROCESSED_BUCKET,
                        Key=structured_key,
                        CopySource=copy_source,
                        MetadataDirective="COPY",
                    )
                except ClientError as e:
                    logger.exception("structured materialize copy failed")
                    return _err(502, f"copy {source_key} to structured: {type(e).__name__}: {e}")
                structured_copies.append({
                    "sourceKey": source_key,
                    "destinationKey": structured_key,
                    "glueTableHint": structured_table_hint,
                })
                file_entry["structuredKey"] = structured_key
                file_entry["glueTableHint"] = structured_table_hint

            materialized_files.append(file_entry)
            if move_sources and source_key.startswith(caller_prefix):
                moved_sources.add(source_key)

        materialized_groups.append({
            "id": group.get("id") or group_name,
            "name": group_name,
            "type": group.get("type") or "",
            "targetPrefix": f"projects/{project_id}/{group_name}/",
            "structuredTableHint": group_table_name if len(structured_table_hints) <= 1 else None,
            "structuredTableHints": structured_table_hints,
            "files": materialized_files,
        })

    metadata = {
        "projectId": project_id,
        "projectName": project_name,
        "createdAt": existing_metadata.get("createdAt") or datetime.now(timezone.utc).isoformat(),
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "createdBy": user_id,
        "bucket": PROCESSED_BUCKET,
        "projectPrefix": f"projects/{project_id}/",
        "metadataKey": metadata_key,
        "groups": [*retained_groups, *materialized_groups],
        "glue": {
            "crawlerName": GLUE_CRAWLER_NAME or None,
            "structuredCopies": [
                *[
                    copy for copy in existing_metadata.get("glue", {}).get("structuredCopies", [])
                    if copy.get("glueTableHint") not in removed_table_hints
                ],
                *structured_copies,
            ],
        },
    }
    try:
        s3.put_object(
            Bucket=PROCESSED_BUCKET,
            Key=metadata_key,
            Body=json.dumps(metadata, default=str, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
    except ClientError as e:
        logger.exception("metadata write failed")
        return _err(502, f"metadata write: {type(e).__name__}: {e}")

    for source_key in sorted(moved_sources):
        try:
            s3.delete_object(Bucket=PROCESSED_BUCKET, Key=source_key)
            deleted.append(source_key)
        except ClientError:
            logger.exception("source delete failed after materialize: %s", source_key)

    crawler_started = False
    crawler_message = ""
    if GLUE_CRAWLER_NAME and (structured_copies or structured_deleted):
        try:
            glue.start_crawler(Name=GLUE_CRAWLER_NAME)
            crawler_started = True
            crawler_message = "started"
        except glue.exceptions.CrawlerRunningException:
            crawler_started = True
            crawler_message = "already_running"
        except ClientError as e:
            logger.exception("Glue crawler start failed")
            crawler_message = f"{type(e).__name__}: {e}"

    return _ok({
        "bucket": PROCESSED_BUCKET,
        "projectPrefix": f"projects/{project_id}/",
        "metadataKey": metadata_key,
        "copied": copied,
        "structuredCopies": structured_copies,
        "deletedSources": deleted,
        "crawlerStarted": crawler_started,
        "crawlerMessage": crawler_message,
        "metadata": metadata,
    })


_DOCUMENT_ANALYSIS_EXTENSIONS = (".txt", ".md", ".json", ".pdf")
_ANALYSIS_STOPWORDS = {
    "about", "across", "after", "against", "also", "and", "are", "because",
    "been", "between", "both", "business", "can", "data", "each", "engineering",
    "from", "goal", "goals", "has", "have", "into", "its", "may", "more",
    "must", "need", "needs", "objective", "objectives", "only", "other",
    "problem", "problems", "project", "projects", "risk", "risks", "should",
    "system", "team", "that", "the", "their", "this", "through", "with",
}


def _sentences(text: str) -> list[str]:
    chunks = re.split(r"(?<=[.!?])\s+|\n+", text or "")
    return [re.sub(r"\s+", " ", chunk).strip(" -\t") for chunk in chunks if chunk.strip()]


def _pdf_stream_text(raw: bytes) -> str:
    """Best-effort PDF text extraction without adding Lambda dependencies.

    This is intentionally deterministic and modest: it handles the small,
    text-based project PDFs used in the demo by reading stream objects, inflating
    Flate streams when present, and extracting literal/string-array text tokens.
    """
    chunks: list[bytes] = []
    for match in re.finditer(rb"stream\r?\n(.*?)endstream", raw, flags=re.DOTALL):
        stream = match.group(1).strip(b"\r\n")
        candidates = [stream]
        if stream.endswith(b"~>"):
            try:
                candidates.insert(0, base64.a85decode(stream, adobe=True))
            except ValueError:
                pass
        for candidate in candidates:
            try:
                chunks.append(zlib.decompress(candidate))
                break
            except zlib.error:
                if candidate is stream:
                    chunks.append(candidate)
    if not chunks:
        chunks = [raw]

    text_parts: list[str] = []
    for chunk in chunks:
        text = chunk.decode("latin-1", errors="ignore")
        for value in re.findall(r"\(([^()]*)\)", text):
            cleaned = (
                value
                .replace(r"\(", "(")
                .replace(r"\)", ")")
                .replace(r"\\", "\\")
            )
            if cleaned.strip():
                text_parts.append(cleaned)
    extracted = " ".join(text_parts)
    extracted = re.sub(r"\\[nrbtf]", " ", extracted)
    extracted = re.sub(r"\s+", " ", extracted).strip()
    return extracted


def _document_text_from_bytes(name: str, raw: bytes) -> str:
    if name.lower().endswith(".pdf"):
        return _pdf_stream_text(raw)
    return raw.decode("utf-8", errors="replace")


def _matching_sentences(sentences: list[str], terms: tuple[str, ...], limit: int = 3) -> list[str]:
    matches = []
    for sentence in sentences:
        lowered = sentence.lower()
        if any(term in lowered for term in terms):
            matches.append(sentence[:320])
        if len(matches) >= limit:
            break
    return matches


def _keyword_list(text: str, limit: int = 8) -> list[str]:
    counts: dict[str, int] = {}
    for raw in re.findall(r"[A-Za-z][A-Za-z0-9_-]{3,}", text or ""):
        word = raw.lower().strip("_-")
        if word in _ANALYSIS_STOPWORDS:
            continue
        counts[word] = counts.get(word, 0) + 1
    return [word for word, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]]


def _project_title(name: str, text: str) -> str:
    for line in (text or "").splitlines():
        cleaned = line.strip().strip("#").strip()
        if cleaned and len(cleaned) <= 120:
            return cleaned
    return re.sub(r"[_-]+", " ", name.rsplit(".", 1)[0]).strip() or name


def _document_analysis(name: str, key: str, text: str) -> dict[str, Any]:
    sentences = _sentences(text)
    goals = _matching_sentences(sentences, ("goal", "objective", "aim", "deliver", "build", "modernize", "improve"))
    problems = _matching_sentences(sentences, ("problem", "issue", "pain", "challenge", "gap", "failure", "bottleneck"))
    risks = _matching_sentences(sentences, ("risk", "blocked", "blocker", "constraint", "dependency", "delay", "security", "compliance"))
    dependencies = _matching_sentences(sentences, ("depend", "requires", "integration", "upstream", "downstream", "vendor", "team", "api"))
    metrics = _matching_sentences(sentences, ("success", "metric", "kpi", "measure", "target", "outcome"))
    missing = []
    if not goals:
        missing.append("goal/objective")
    if not problems:
        missing.append("problem statement")
    if not dependencies:
        missing.append("dependencies")
    if not metrics:
        missing.append("success metrics")
    if not risks:
        missing.append("risks")
    return {
        "name": name,
        "key": key,
        "title": _project_title(name, text),
        "keywords": _keyword_list(text),
        "goals": goals,
        "problems": problems,
        "risks": risks,
        "dependencies": dependencies,
        "successSignals": metrics,
        "missingInformation": missing,
        "riskLevel": "high" if len(risks) >= 3 or any("security" in item.lower() or "compliance" in item.lower() for item in risks) else "medium" if risks else "low",
        "recommendedAction": (
            "Clarify scope, owner, dependencies, and success metrics before sequencing."
            if missing else "Ready for portfolio sequencing and dependency review."
        ),
    }


def _portfolio_overlap(projects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    overlaps = []
    for left_index, left in enumerate(projects):
        left_words = set(left.get("keywords") or [])
        for right in projects[left_index + 1:]:
            shared = sorted(left_words.intersection(set(right.get("keywords") or [])))
            if len(shared) >= 2:
                overlaps.append({
                    "projects": [left["title"], right["title"]],
                    "sharedKeywords": shared[:6],
                    "note": "Potential overlap or shared dependency; review for consolidation or sequencing.",
                })
    return overlaps[:12]


def _portfolio_action_plan(projects: list[dict[str, Any]], overlaps: list[dict[str, Any]]) -> list[str]:
    high_risk = [p["title"] for p in projects if p.get("riskLevel") == "high"]
    unclear = [p["title"] for p in projects if p.get("missingInformation")]
    plan = []
    if high_risk:
        plan.append(f"Review high-risk projects first: {', '.join(high_risk[:5])}.")
    if overlaps:
        plan.append("Resolve overlap before funding parallel workstreams.")
    if unclear:
        plan.append(f"Request missing project details for: {', '.join(unclear[:5])}.")
    plan.append("Sequence projects after dependencies and success metrics are documented.")
    return plan


def _portfolio_markdown(group_name: str, projects: list[dict[str, Any]], overlaps: list[dict[str, Any]], action_plan: list[str]) -> str:
    lines = [f"# Portfolio Analysis: {group_name}", "", f"Documents analyzed: {len(projects)}", ""]
    lines.extend(["## Project Inventory", ""])
    for project in projects:
        lines.extend([
            f"### {project['title']}",
            f"- File: {project['name']}",
            f"- Risk level: {project['riskLevel']}",
            f"- Keywords: {', '.join(project['keywords']) or 'None detected'}",
            f"- Goal/objective: {project['goals'][0] if project['goals'] else 'Not stated'}",
            f"- Problem: {project['problems'][0] if project['problems'] else 'Not stated'}",
            f"- Dependencies: {project['dependencies'][0] if project['dependencies'] else 'Not stated'}",
            f"- Missing: {', '.join(project['missingInformation']) or 'None detected'}",
            f"- Recommended action: {project['recommendedAction']}",
            "",
        ])
    lines.extend(["## Overlap And Conflict Scan", ""])
    if overlaps:
        for item in overlaps:
            lines.append(f"- {' / '.join(item['projects'])}: shared {', '.join(item['sharedKeywords'])}. {item['note']}")
    else:
        lines.append("- No strong keyword overlap detected.")
    lines.extend(["", "## Recommended Action Plan", ""])
    lines.extend([f"- {item}" for item in action_plan])
    lines.append("")
    return "\n".join(lines)


def _handle_data_grouping_analyze_documents(event):
    if not PROCESSED_BUCKET:
        return _err(500, "PROCESSED_BUCKET not configured")
    user_id = _caller_user_id(event)
    if not user_id:
        return _err(401, "Could not resolve caller identity")
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _err(400, "Invalid JSON body")

    group_name = (body.get("groupName") or "Project group").strip()[:200]
    files = [f for f in (body.get("files") or []) if isinstance(f, dict) and f.get("key")]
    caller_prefix = f"{UPLOAD_PREFIX}{user_id}/"
    documents = []
    skipped = []
    for file in files[:25]:
        key = str(file.get("key") or "")
        name = str(file.get("name") or key.rsplit("/", 1)[-1])
        if not key.startswith(caller_prefix) and not key.startswith("projects/"):
            return _err(403, f"Source key is outside allowed processed prefixes: {key}")
        if not name.lower().endswith(_DOCUMENT_ANALYSIS_EXTENSIONS):
            skipped.append({"name": name, "key": key, "reason": "unsupported_file_type"})
            continue
        try:
            obj = s3.get_object(Bucket=PROCESSED_BUCKET, Key=key)
            raw = obj["Body"].read(250_000)
            text = _document_text_from_bytes(name, raw)
        except ClientError as e:
            logger.exception("document analysis read failed")
            skipped.append({"name": name, "key": key, "reason": f"{type(e).__name__}: {e}"})
            continue
        if not text.strip():
            skipped.append({"name": name, "key": key, "reason": "no_extractable_text"})
            continue
        documents.append(_document_analysis(name, key, text))

    if not documents:
        return _err(400, "No readable .txt, .md, .json, or .pdf files were found in this group")
    overlaps = _portfolio_overlap(documents)
    action_plan = _portfolio_action_plan(documents, overlaps)
    return _ok({
        "groupName": group_name,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "documentCount": len(documents),
        "skipped": skipped,
        "projects": documents,
        "overlaps": overlaps,
        "actionPlan": action_plan,
        "markdown": _portfolio_markdown(group_name, documents, overlaps, action_plan),
    })


def _handle_data_grouping_start_crawler(event):
    if not _caller_user_id(event):
        return _err(401, "Could not resolve caller identity")
    if not GLUE_CRAWLER_NAME:
        return _err(500, "GLUE_CRAWLER_NAME not configured")
    try:
        crawler = glue.get_crawler(Name=GLUE_CRAWLER_NAME).get("Crawler", {})
        state = crawler.get("State") or "UNKNOWN"
        if state == "RUNNING":
            return _ok({
                "crawlerName": GLUE_CRAWLER_NAME,
                "crawlerStarted": True,
                "crawlerMessage": "already_running",
                "state": state,
                "lastCrawl": crawler.get("LastCrawl"),
            })
        glue.start_crawler(Name=GLUE_CRAWLER_NAME)
        return _ok({
            "crawlerName": GLUE_CRAWLER_NAME,
            "crawlerStarted": True,
            "crawlerMessage": "started",
            "state": "RUNNING",
            "lastCrawl": crawler.get("LastCrawl"),
        })
    except glue.exceptions.CrawlerRunningException:
        return _ok({
            "crawlerName": GLUE_CRAWLER_NAME,
            "crawlerStarted": True,
            "crawlerMessage": "already_running",
            "state": "RUNNING",
        })
    except ClientError as e:
        logger.exception("Glue crawler start failed")
        return _err(502, f"{type(e).__name__}: {e}")


# ──────────────────────────── /config-drift ─────────────────────
def _config_drift_baseline_key(user_id: str) -> str:
    return f"config-drift/{user_id}/security-groups/baseline.json"


def _config_drift_check_key(user_id: str, check_id: str) -> str:
    return f"config-drift/{user_id}/security-groups/checks/{check_id}.json"


def _load_config_drift_check(user_id: str, check_id: str) -> dict[str, Any] | None:
    if not PROCESSED_BUCKET or not check_id:
        return None
    try:
        obj = s3.get_object(Bucket=PROCESSED_BUCKET, Key=_config_drift_check_key(user_id, check_id))
        return json.loads(obj["Body"].read().decode("utf-8"))
    except s3.exceptions.NoSuchKey:
        return None
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return None
        raise


def _sg_tag(tags: list[dict[str, Any]] | None, key: str) -> str:
    for tag in tags or []:
        if tag.get("Key") == key:
            return str(tag.get("Value") or "")
    return ""


def _sg_rule_sources(permission: dict[str, Any]) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    for item in permission.get("IpRanges") or []:
        value = item.get("CidrIp")
        if value:
            sources.append({
                "sourceType": "cidr",
                "source": value,
                "description": item.get("Description") or "",
            })
    for item in permission.get("Ipv6Ranges") or []:
        value = item.get("CidrIpv6")
        if value:
            sources.append({
                "sourceType": "cidr6",
                "source": value,
                "description": item.get("Description") or "",
            })
    for item in permission.get("PrefixListIds") or []:
        value = item.get("PrefixListId")
        if value:
            sources.append({
                "sourceType": "prefixList",
                "source": value,
                "description": item.get("Description") or "",
            })
    for item in permission.get("UserIdGroupPairs") or []:
        group_id = item.get("GroupId")
        if group_id:
            sources.append({
                "sourceType": "securityGroup",
                "source": group_id,
                "description": item.get("Description") or "",
            })
    return sources or [{"sourceType": "unknown", "source": "unknown", "description": ""}]


def _normalize_sg_rule(permission: dict[str, Any], source: dict[str, Any], direction: str) -> dict[str, Any]:
    protocol = str(permission.get("IpProtocol") or "-1")
    from_port = permission.get("FromPort")
    to_port = permission.get("ToPort")
    if protocol == "-1":
        from_port = -1
        to_port = -1
    return {
        "direction": direction,
        "protocol": protocol,
        "fromPort": int(from_port) if from_port is not None else -1,
        "toPort": int(to_port) if to_port is not None else -1,
        "sourceType": source.get("sourceType") or "unknown",
        "source": source.get("source") or "unknown",
        "description": source.get("description") or "",
    }


def _normalize_security_group(group: dict[str, Any]) -> dict[str, Any]:
    ingress = [
        _normalize_sg_rule(permission, source, "ingress")
        for permission in group.get("IpPermissions") or []
        for source in _sg_rule_sources(permission)
    ]
    egress = [
        _normalize_sg_rule(permission, source, "egress")
        for permission in group.get("IpPermissionsEgress") or []
        for source in _sg_rule_sources(permission)
    ]
    return {
        "resourceId": group.get("GroupId"),
        "resourceName": group.get("GroupName") or _sg_tag(group.get("Tags"), "Name") or group.get("GroupId"),
        "description": group.get("Description") or "",
        "vpcId": group.get("VpcId") or "",
        "ownerId": group.get("OwnerId") or "",
        "environment": _sg_tag(group.get("Tags"), "Environment") or _sg_tag(group.get("Tags"), "environment") or "",
        "tags": {str(tag.get("Key")): str(tag.get("Value") or "") for tag in group.get("Tags") or [] if tag.get("Key")},
        "ingress": sorted(ingress, key=_security_group_rule_key),
        "egress": sorted(egress, key=_security_group_rule_key),
    }


def _security_group_rule_key(rule: dict[str, Any]) -> str:
    return "|".join([
        str(rule.get("direction") or ""),
        str(rule.get("protocol") or ""),
        str(rule.get("fromPort") if rule.get("fromPort") is not None else ""),
        str(rule.get("toPort") if rule.get("toPort") is not None else ""),
        str(rule.get("sourceType") or ""),
        str(rule.get("source") or ""),
    ])


def _ec2_permission_for_rule(rule: dict[str, Any]) -> dict[str, Any]:
    permission = {
        "IpProtocol": str(rule.get("protocol") or "-1"),
    }
    if permission["IpProtocol"] != "-1":
        permission["FromPort"] = int(rule.get("fromPort"))
        permission["ToPort"] = int(rule.get("toPort"))
    source_type = rule.get("sourceType")
    source = rule.get("source")
    if source_type == "cidr":
        permission["IpRanges"] = [{"CidrIp": source}]
    elif source_type == "cidr6":
        permission["Ipv6Ranges"] = [{"CidrIpv6": source}]
    elif source_type == "prefixList":
        permission["PrefixListIds"] = [{"PrefixListId": source}]
    elif source_type == "securityGroup":
        permission["UserIdGroupPairs"] = [{"GroupId": source}]
    else:
        raise ValueError(f"Unsupported security group rule source type: {source_type}")
    return permission


def _format_sg_rule(rule: dict[str, Any]) -> str:
    protocol = rule.get("protocol") or "any"
    if protocol == "-1":
        port = "All"
    elif rule.get("fromPort") == rule.get("toPort"):
        port = str(rule.get("fromPort"))
    else:
        port = f"{rule.get('fromPort')}-{rule.get('toPort')}"
    direction = rule.get("direction") or "rule"
    source = rule.get("source") or "unknown"
    return f"{direction} {protocol} {port} from {source}"


def _severity_for_sg_change(rule: dict[str, Any] | None, drift_type: str) -> str:
    if not rule:
        return "MEDIUM" if drift_type == "New security group" else "HIGH"
    if rule.get("direction") == "ingress" and rule.get("source") in ("0.0.0.0/0", "::/0"):
        if rule.get("protocol") == "-1" or rule.get("fromPort") in (22, 3389):
            return "CRITICAL"
        return "HIGH"
    if rule.get("direction") == "egress":
        return "LOW"
    return "MEDIUM"


def _recommendation_for_sg_change(rule: dict[str, Any] | None, drift_type: str) -> str:
    if drift_type == "Ingress rule added" and rule and rule.get("source") in ("0.0.0.0/0", "::/0"):
        return "Open a HITL exception immediately; revoke this public ingress if no approval is received before the deadline."
    if drift_type.endswith("removed"):
        return "Confirm the removal was intentional before updating baseline."
    if drift_type == "New security group":
        return "Confirm owner, purpose, and tags before accepting this security group into the baseline."
    return "Review the rule change; update baseline only if the change is approved."


def _pending_revert_for_sg_change(finding: dict[str, Any]) -> dict[str, Any] | None:
    rule = finding.get("rule") or {}
    if finding.get("driftType") == "Ingress rule added":
        return {
            "action": "revoke_security_group_ingress",
            "resourceId": finding.get("resourceId"),
            "rule": rule,
            "status": "PENDING_HITL",
        }
    if finding.get("driftType") == "Ingress rule removed":
        return {
            "action": "authorize_security_group_ingress",
            "resourceId": finding.get("resourceId"),
            "rule": rule,
            "status": "PENDING_HITL",
        }
    return None


def _compare_security_groups(baseline: list[dict[str, Any]], latest: list[dict[str, Any]]) -> list[dict[str, Any]]:
    baseline_by_id = {group.get("resourceId"): group for group in baseline if group.get("resourceId")}
    latest_by_id = {group.get("resourceId"): group for group in latest if group.get("resourceId")}
    findings: list[dict[str, Any]] = []

    for group in latest:
        resource_id = group.get("resourceId")
        prior = baseline_by_id.get(resource_id)
        if not prior:
            findings.append({
                "id": f"{resource_id}-new-group",
                "resourceId": resource_id,
                "resourceName": group.get("resourceName") or resource_id,
                "driftType": "New security group",
                "before": "Not present in baseline",
                "after": group.get("resourceName") or resource_id,
                "severity": "MEDIUM",
                "recommendation": _recommendation_for_sg_change(None, "New security group"),
            })
            continue

        for direction in ("ingress", "egress"):
            drift_label = "Ingress" if direction == "ingress" else "Egress"
            prior_rules = {_security_group_rule_key(rule): rule for rule in prior.get(direction) or []}
            latest_rules = {_security_group_rule_key(rule): rule for rule in group.get(direction) or []}
            for key, rule in latest_rules.items():
                if key in prior_rules:
                    continue
                drift_type = f"{drift_label} rule added"
                finding = {
                    "id": f"{resource_id}-{direction}-added-{abs(hash(key))}",
                    "resourceId": resource_id,
                    "resourceName": group.get("resourceName") or resource_id,
                    "driftType": drift_type,
                    "before": "No matching baseline rule",
                    "after": _format_sg_rule(rule),
                    "severity": _severity_for_sg_change(rule, drift_type),
                    "recommendation": _recommendation_for_sg_change(rule, drift_type),
                    "rule": rule,
                }
                pending = _pending_revert_for_sg_change(finding)
                if pending:
                    finding["pendingRevert"] = pending
                findings.append(finding)
            for key, rule in prior_rules.items():
                if key in latest_rules:
                    continue
                drift_type = f"{drift_label} rule removed"
                finding = {
                    "id": f"{resource_id}-{direction}-removed-{abs(hash(key))}",
                    "resourceId": resource_id,
                    "resourceName": group.get("resourceName") or resource_id,
                    "driftType": drift_type,
                    "before": _format_sg_rule(rule),
                    "after": "Missing from latest AWS observation",
                    "severity": _severity_for_sg_change(rule, drift_type),
                    "recommendation": _recommendation_for_sg_change(rule, drift_type),
                    "rule": rule,
                }
                pending = _pending_revert_for_sg_change(finding)
                if pending:
                    finding["pendingRevert"] = pending
                findings.append(finding)

    for group in baseline:
        resource_id = group.get("resourceId")
        if resource_id in latest_by_id:
            continue
        findings.append({
            "id": f"{resource_id}-missing-group",
            "resourceId": resource_id,
            "resourceName": group.get("resourceName") or resource_id,
            "driftType": "Security group missing",
            "before": group.get("resourceName") or resource_id,
            "after": "Not present in latest AWS observation",
            "severity": "HIGH",
            "recommendation": "Treat as high-risk; require human review before attempting recreation.",
        })

    severity_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    findings.sort(key=lambda item: (severity_order.get(item.get("severity"), 9), item.get("resourceName") or ""))
    return findings


def _load_security_group_snapshot(group_ids: list[str] | None = None) -> list[dict[str, Any]]:
    paginator = ec2.get_paginator("describe_security_groups")
    params = {"GroupIds": group_ids} if group_ids else {}
    groups: list[dict[str, Any]] = []
    for page in paginator.paginate(**params):
        groups.extend(_normalize_security_group(group) for group in page.get("SecurityGroups") or [])
    groups.sort(key=lambda item: (item.get("resourceName") or "", item.get("resourceId") or ""))
    return groups


def _load_config_drift_baseline(user_id: str) -> dict[str, Any] | None:
    if not PROCESSED_BUCKET:
        return None
    try:
        obj = s3.get_object(Bucket=PROCESSED_BUCKET, Key=_config_drift_baseline_key(user_id))
        return json.loads(obj["Body"].read().decode("utf-8"))
    except s3.exceptions.NoSuchKey:
        return None
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return None
        raise


def _handle_config_drift_security_groups_current(event):
    user_id = _caller_user_id(event)
    if not user_id:
        return _err(401, "Could not resolve caller identity")
    try:
        resources = _load_security_group_snapshot()
    except ClientError as e:
        logger.exception("security group current snapshot failed")
        return _err(502, f"{type(e).__name__}: {e}")
    return _ok({
        "source": "live_ec2_describe_security_groups",
        "observedAt": datetime.now(timezone.utc).isoformat(),
        "resources": resources,
        "count": len(resources),
    })


def _handle_config_drift_security_groups_get_baseline(event):
    if not PROCESSED_BUCKET:
        return _err(500, "PROCESSED_BUCKET not configured")
    user_id = _caller_user_id(event)
    if not user_id:
        return _err(401, "Could not resolve caller identity")
    try:
        baseline = _load_config_drift_baseline(user_id)
    except ClientError as e:
        logger.exception("security group baseline read failed")
        return _err(502, f"{type(e).__name__}: {e}")
    if not baseline:
        return _ok({
            "captured": False,
            "resourceType": "AWS::EC2::SecurityGroup",
            "resources": [],
        })
    baseline["captured"] = True
    baseline["resourceCount"] = len(baseline.get("resources") or [])
    return _ok(baseline)


def _handle_config_drift_security_groups_baseline(event):
    if not PROCESSED_BUCKET:
        return _err(500, "PROCESSED_BUCKET not configured")
    user_id = _caller_user_id(event)
    if not user_id:
        return _err(401, "Could not resolve caller identity")
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _err(400, "Invalid JSON body")
    group_ids = body.get("groupIds") or None
    if group_ids is not None and not isinstance(group_ids, list):
        return _err(400, "groupIds must be a list")
    group_ids = [str(group_id).strip() for group_id in (group_ids or []) if str(group_id).strip()] or None
    try:
        resources = _load_security_group_snapshot(group_ids)
        baseline = {
            "capturedAt": datetime.now(timezone.utc).isoformat(),
            "capturedBy": user_id,
            "source": "live_ec2_describe_security_groups",
            "resourceType": "AWS::EC2::SecurityGroup",
            "resources": resources,
        }
        s3.put_object(
            Bucket=PROCESSED_BUCKET,
            Key=_config_drift_baseline_key(user_id),
            Body=json.dumps(baseline, default=str, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
    except ClientError as e:
        logger.exception("security group baseline capture failed")
        return _err(502, f"{type(e).__name__}: {e}")
    _audit("CONFIG_DRIFT_BASELINE_CAPTURED", "AWS::EC2::SecurityGroup", user_id, "COMPLETED",
           {"resource_count": len(resources)})
    return _ok(baseline)


def _handle_config_drift_security_groups_check(event):
    if not PROCESSED_BUCKET:
        return _err(500, "PROCESSED_BUCKET not configured")
    user_id = _caller_user_id(event)
    if not user_id:
        return _err(401, "Could not resolve caller identity")
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _err(400, "Invalid JSON body")
    timeout_minutes = int(body.get("hitlTimeoutMinutes") or 10)
    timeout_minutes = max(1, min(timeout_minutes, 60))
    baseline = _load_config_drift_baseline(user_id)
    if not baseline:
        return _err(404, "No security group baseline has been captured")
    try:
        latest = _load_security_group_snapshot()
        findings = _compare_security_groups(baseline.get("resources") or [], latest)
        now = datetime.now(timezone.utc)
        deadline = now + timedelta(minutes=timeout_minutes)
        pending_reverts = [finding["pendingRevert"] for finding in findings if finding.get("pendingRevert")]
        result = {
            "checkId": f"sg-drift-{now.strftime('%Y%m%d%H%M%S')}",
            "checkedAt": now.isoformat(),
            "source": "live_ec2_describe_security_groups",
            "baselineCapturedAt": baseline.get("capturedAt"),
            "baselineResourceCount": len(baseline.get("resources") or []),
            "latestResourceCount": len(latest),
            "hitl": {
                "status": "PENDING" if findings else "NOT_REQUIRED",
                "deadlineAt": deadline.isoformat() if findings else None,
                "timeoutMinutes": timeout_minutes if findings else 0,
                "note": "Remediation is staged until the HITL deadline expires.",
            },
            "findings": findings,
            "pendingReverts": pending_reverts,
            "latest": latest,
        }
        s3.put_object(
            Bucket=PROCESSED_BUCKET,
            Key=_config_drift_check_key(user_id, result["checkId"]),
            Body=json.dumps(result, default=str, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
    except ClientError as e:
        logger.exception("security group drift check failed")
        return _err(502, f"{type(e).__name__}: {e}")
    _audit("CONFIG_DRIFT_CHECKED", "AWS::EC2::SecurityGroup", user_id, "PENDING_HITL" if findings else "COMPLETED",
           {"finding_count": len(findings), "pending_revert_count": len(pending_reverts)})
    return _ok(result)


def _handle_config_drift_security_groups_revert(event):
    if not PROCESSED_BUCKET:
        return _err(500, "PROCESSED_BUCKET not configured")
    user_id = _caller_user_id(event)
    if not user_id:
        return _err(401, "Could not resolve caller identity")
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _err(400, "Invalid JSON body")

    check_id = (body.get("checkId") or "").strip()
    if not check_id:
        return _err(400, "Missing checkId")
    try:
        check = _load_config_drift_check(user_id, check_id)
    except ClientError as e:
        logger.exception("security group drift check read failed")
        return _err(502, f"{type(e).__name__}: {e}")
    if not check:
        return _err(404, f"Drift check {check_id} not found")

    deadline_raw = (check.get("hitl") or {}).get("deadlineAt")
    if not deadline_raw:
        return _err(409, "This drift check has no pending HITL deadline")
    deadline = datetime.fromisoformat(str(deadline_raw).replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    if now < deadline:
        return _err(409, f"HITL deadline has not expired ({deadline.isoformat()})")

    actions = check.get("pendingReverts") or []
    if not actions:
        return _err(409, "No pending revert actions exist for this drift check")

    applied = []
    skipped = []
    for action in actions:
        resource_id = action.get("resourceId")
        rule = action.get("rule") or {}
        if action.get("status") == "COMPLETED":
            skipped.append({"resourceId": resource_id, "reason": "already_completed"})
            continue
        if action.get("action") != "revoke_security_group_ingress":
            skipped.append({"resourceId": resource_id, "reason": "unsupported_action"})
            continue
        if resource_id not in CONFIG_DRIFT_ALLOWED_REVERT_SECURITY_GROUPS:
            skipped.append({"resourceId": resource_id, "reason": "security_group_not_allowlisted"})
            continue
        if rule.get("direction") != "ingress":
            skipped.append({"resourceId": resource_id, "reason": "not_ingress"})
            continue

        try:
            current_groups = _load_security_group_snapshot([resource_id])
        except ClientError as e:
            logger.exception("security group current snapshot failed before revert")
            skipped.append({"resourceId": resource_id, "reason": f"{type(e).__name__}: {e}"})
            continue
        current_rules = {
            _security_group_rule_key(current_rule)
            for group in current_groups
            for current_rule in group.get("ingress") or []
        }
        if _security_group_rule_key(rule) not in current_rules:
            action["status"] = "COMPLETED"
            action["completedAt"] = now.isoformat()
            action["note"] = "Rule was already absent at execution time."
            skipped.append({"resourceId": resource_id, "reason": "rule_already_absent"})
            continue

        try:
            ec2.revoke_security_group_ingress(
                GroupId=resource_id,
                IpPermissions=[_ec2_permission_for_rule(rule)],
            )
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code")
            if code == "InvalidPermission.NotFound":
                action["status"] = "COMPLETED"
                action["completedAt"] = now.isoformat()
                action["note"] = "Rule was already absent at execution time."
                skipped.append({"resourceId": resource_id, "reason": "rule_already_absent"})
                continue
            logger.exception("security group ingress revoke failed")
            action["status"] = "FAILED"
            action["error"] = f"{type(e).__name__}: {e}"
            skipped.append({"resourceId": resource_id, "reason": action["error"]})
            continue

        action["status"] = "COMPLETED"
        action["completedAt"] = now.isoformat()
        action["note"] = "Expired HITL drift reverted by Arbiter."
        applied.append({
            "resourceId": resource_id,
            "action": action.get("action"),
            "rule": rule,
        })

    remaining = [a for a in actions if a.get("status") not in ("COMPLETED",)]
    completed_count = len(actions) - len(remaining)
    check["pendingReverts"] = actions
    check["revertedAt"] = now.isoformat() if completed_count else check.get("revertedAt")
    if actions and not remaining:
        check["revertStatus"] = "COMPLETED"
    elif completed_count:
        check["revertStatus"] = "PARTIAL"
    else:
        check["revertStatus"] = "NO_ACTION"
    try:
        s3.put_object(
            Bucket=PROCESSED_BUCKET,
            Key=_config_drift_check_key(user_id, check_id),
            Body=json.dumps(check, default=str, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
    except ClientError as e:
        logger.exception("security group drift revert state write failed")
        return _err(502, f"{type(e).__name__}: {e}")

    _audit("CONFIG_DRIFT_REVERT_EXECUTED", "AWS::EC2::SecurityGroup", user_id,
           "COMPLETED" if applied else "NO_ACTION",
           {"check_id": check_id, "applied_count": len(applied), "skipped": skipped})
    return _ok({
        "checkId": check_id,
        "status": check["revertStatus"],
        "applied": applied,
        "skipped": skipped,
        "check": check,
    })


# ──────────────────────────── /findings ─────────────────────────
def _latest_completed_scan_run_id():
    """scan_run_id of the most recent COMPLETED scan-run, or None.

    Used to reconcile the findings/dashboard views: the scanner upserts conflicts-v2
    by conflict_id and never deletes, so scoping to the latest completed run is what
    makes a resolved conflict disappear.

    MUST be reliable regardless of table size. A plain scan(Limit=50) samples an
    arbitrary 50 rows in no order — once scan-runs grows past a page (one row per
    invocation) it misses the true latest and returns a STALE id, which scopes the
    UI to an all-resolved run and shows zero conflicts. Query the by-status GSI
    (status=COMPLETED, started_at DESC, Limit=1) instead — O(1), always correct.
    Returns None (callers fall back to the full set) on empty/error.
    """
    if not scan_runs_table:
        return None
    try:
        resp = scan_runs_table.query(
            IndexName="by-status",
            KeyConditionExpression=Key("status").eq("COMPLETED"),
            ScanIndexForward=False,   # newest started_at first
            Limit=1,
        )
        items = resp.get("Items", [])
    except Exception:
        logger.exception("latest scan-run lookup failed")
        return None
    return items[0].get("scan_run_id") if items else None


def _handle_list_findings(event):
    """Return all conflicts. UI shape: {findings: [...]}.

    Scans the whole table — fine at demo scale. Supports optional severity
    and status query-string filters (server-side narrowing keeps the
    response small even as the table grows).
    """
    tbl = _findings_table()
    if not tbl:
        return _err(500, "CONFLICTS_TABLE / CONFLICTS_TABLE_V2 not configured")
    qs = event.get("queryStringParameters") or {}
    try:
        resp = tbl.scan(Limit=200)
        items = resp.get("Items", [])
    except Exception as e:
        logger.exception("findings scan failed")
        return _err(502, f"{type(e).__name__}: {e}")
    # Reconcile resolved conflicts. The scanner never deletes rows, so a
    # conflict that is no longer detected lingers with its OLD scan_run_id.
    # Scope to the latest COMPLETED run so resolved conflicts disappear. Done
    # over the full item set (incl the compliant rows every scan also writes)
    # so an all-resolved scan correctly shows zero conflicts. Safety: only
    # apply when that run actually wrote rows here, so a scan_run_id mismatch
    # or a fresh pre-scan seed never blanks the UI. Opt out with ?latest_only=false.
    latest_only = (qs.get("latest_only") or "true").lower() in ("1", "true", "yes")
    if latest_only:
        latest_run = _latest_completed_scan_run_id()
        if latest_run:
            scoped = [i for i in items if i.get("scan_run_id") == latest_run]
            if scoped:
                items = scoped
    sev = (qs.get("severity") or "").strip().upper()
    status = (qs.get("status") or "").strip().upper()
    domain = (qs.get("domain") or "").strip().upper()
    include_compliant = (qs.get("include_compliant") or "false").lower() in ("1", "true", "yes")
    if not include_compliant:
        items = [i for i in items if not i.get("compliant")]
    if sev:
        items = [i for i in items if (i.get("severity") or "").upper() == sev]
    if status:
        items = [i for i in items if (i.get("status") or "").upper() == status]
    if domain:
        items = [i for i in items if (i.get("domain") or "").upper() == domain]
    # Newest first
    items.sort(key=lambda i: i.get("detected_at") or "", reverse=True)
    return _ok({"findings": items})


# ──────────────────────────── /actions ──────────────────────────
def _handle_list_actions(event):
    """Return all change requests. UI shape: {change_requests: [...]}."""
    if not crs_table:
        return _err(500, "CHANGE_REQUESTS_TABLE not configured")
    try:
        resp = crs_table.scan(Limit=200)
        items = resp.get("Items", [])
    except Exception as e:
        logger.exception("change-requests scan failed")
        return _err(502, f"{type(e).__name__}: {e}")
    items.sort(key=lambda i: i.get("created_at") or "", reverse=True)
    return _ok({"change_requests": items})


# ──────────────────────────── /audit ────────────────────────────
def _handle_list_audit(event):
    """Return audit log entries. UI shape: {logs: [...]}."""
    if not audit_table:
        return _err(500, "AUDIT_TABLE not configured")
    try:
        resp = audit_table.scan(Limit=200)
        items = resp.get("Items", [])
    except Exception as e:
        logger.exception("audit scan failed")
        return _err(502, f"{type(e).__name__}: {e}")
    items.sort(key=lambda i: i.get("timestamp") or "", reverse=True)
    return _ok({"logs": items})


# ──────────────────────────── /reports + /compliance ───────────
# Synchronous report generation: build the file from the same conflicts / audit /
# framework data the UI shows, store it in the reports bucket, and return a
# presigned GET URL the browser downloads directly. Fast (a few seconds) so it
# runs inline within the API Gateway request window — no job table / polling.

def _load_report_findings():
    """Non-compliant findings scoped to the latest completed scan run — the same
    set Findings/Governance show. Mirrors _handle_list_findings' scoping."""
    tbl = _findings_table()
    if not tbl:
        return []
    try:
        items = tbl.scan(Limit=200).get("Items", [])
    except Exception:
        logger.exception("report findings scan failed")
        return []
    latest = _latest_completed_scan_run_id()
    if latest:
        scoped = [i for i in items if i.get("scan_run_id") == latest]
        if scoped:
            items = scoped
    items = [i for i in items if not i.get("compliant")]
    items.sort(key=lambda i: i.get("detected_at") or "", reverse=True)
    return items


def _load_report_audit(limit=500):
    if not audit_table:
        return []
    try:
        items = audit_table.scan(Limit=limit).get("Items", [])
    except Exception:
        logger.exception("report audit scan failed")
        return []
    items.sort(key=lambda i: i.get("timestamp") or "", reverse=True)
    return items


def _load_report_crs():
    if not crs_table:
        return []
    try:
        return crs_table.scan(Limit=200).get("Items", [])
    except Exception:
        logger.exception("report CR scan failed")
        return []


def _report_bundle(params):
    findings = _load_report_findings()
    summaries = report_data.framework_summaries(findings)
    return {
        "conflicts": findings,
        "audit": _load_report_audit(),
        "change_requests": _load_report_crs(),
        "summaries": summaries,
        "overall": report_data.overall_score(summaries),
        "org_name": ORG_NAME,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "params": params or {},
    }


def _generate_and_store(report_id, fmt, spec, params):
    if not REPORTS_BUCKET:
        return _err(500, "REPORTS_BUCKET not configured")
    bundle = _report_bundle(params)
    try:
        payload, content_type, ext = report_generators.generate(report_id, fmt, bundle)
    except RuntimeError as e:           # reportlab/openpyxl unavailable in bundle
        return _err(501, str(e))
    except ValueError as e:
        return _err(400, str(e))
    except Exception as e:
        logger.exception("report generation failed")
        return _err(500, f"report generation failed: {e}")

    now = datetime.now(timezone.utc)
    filename = f"{report_id}-{now:%Y%m%dT%H%M%SZ}.{ext}"
    key = f"reports/{now:%Y}/{now:%m}/{filename}"
    try:
        s3.put_object(Bucket=REPORTS_BUCKET, Key=key, Body=payload,
                      ContentType=content_type,
                      Metadata={"report_type": report_id, "format": fmt})
    except ClientError as e:
        logger.exception("report put_object failed")
        return _err(502, f"{type(e).__name__}: {e}")
    try:
        url = s3.generate_presigned_url(
            ClientMethod="get_object",
            Params={"Bucket": REPORTS_BUCKET, "Key": key,
                    "ResponseContentDisposition": f'attachment; filename="{filename}"'},
            ExpiresIn=REPORT_URL_EXPIRES_SECONDS,
            HttpMethod="GET",
        )
    except ClientError as e:
        logger.exception("report presign failed")
        return _err(502, f"{type(e).__name__}: {e}")

    return _ok({
        "report_type": report_id, "report_title": spec["title"], "format": fmt,
        "filename": filename, "s3_key": key, "bucket": REPORTS_BUCKET,
        "size_bytes": len(payload),
        "download_url": url, "report_url": url,   # download_url=Reports page, report_url=Governance buttons
        "expires_in": REPORT_URL_EXPIRES_SECONDS,
        "generated_at": bundle["generated_at"],
    })


def _handle_reports_catalog(event):
    return _ok({"catalog": report_catalog.REPORT_CATALOG, "categories": report_catalog.CATEGORIES})


def _handle_reports_generate(event):
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _err(400, "Invalid JSON body")
    report_id = (body.get("report_type") or "").strip()
    spec = report_catalog.catalog_by_id(report_id)
    if not spec:
        return _err(400, f"Unknown report_type '{report_id}'")
    fmt = (body.get("format") or spec["default_format"]).strip().lower()
    if fmt not in spec["formats"]:
        return _err(400, f"format '{fmt}' not supported for {report_id}; allowed: {spec['formats']}")
    return _generate_and_store(report_id, fmt, spec, body.get("params") or {})


def _handle_compliance_scores(event):
    findings = _load_report_findings()
    summaries = report_data.framework_summaries(findings)
    return _ok({
        "frameworks": summaries,
        "overall": report_data.overall_score(summaries),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    })


# Legacy/simple mapping for the Governance "Generate report" buttons.
_LEGACY_REPORT_MAP = {
    "executive": ("executive_compliance", "pdf"),
    "technical": ("technical_compliance", "pdf"),
    "evidence_package": ("evidence_package", "zip"),
}


def _handle_compliance_report(event):
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _err(400, "Invalid JSON body")
    rid = (body.get("report_type") or "executive").strip().lower()
    mapped = _LEGACY_REPORT_MAP.get(rid)
    if not mapped:
        spec = report_catalog.catalog_by_id(rid)   # also accept a catalog id directly
        if spec:
            mapped = (rid, spec["default_format"])
    if not mapped:
        return _err(400, f"Unknown report_type '{rid}'")
    report_id, fmt = mapped
    spec = report_catalog.catalog_by_id(report_id)
    params = {}
    if body.get("frameworks"):
        params["frameworks"] = body["frameworks"]
    return _generate_and_store(report_id, fmt, spec, params)


# ──────────────────────────── /token-usage (CISO only) ─────────
# Token Tracking page on the CISO Governance tab. Reads the
# <env>-<project>-token-usage DDB table written by the four AgentCore
# Runtimes (see agents/_shared/token_usage.py). Frontend gates the menu
# and the route, but this is the security boundary — the page is callable
# with any valid IdToken; we must 403 here for non-CISO callers.
_VALID_PERSONAS_FOR_QUERY = ("ciso", "soc", "grc", "employee")
_VALID_AGENTS_FOR_QUERY = ("master", "sharepoint", "awsconfig", "zscaler", "paloalto")


def _require_ciso(event):
    """Return None if the caller's Cognito groups include 'ciso', else a 403 response.

    Uses the existing _caller_groups() helper which tolerates both the list
    form (Cognito JWT) and the comma-separated form (API Gateway authorizer
    flattening). Frontend gating is insufficient — the API is reachable with
    any valid IdToken, so this is the actual security check.

    On a non-CISO caller, a best-effort CROSS_PERSONA audit row is written
    before the 403. The forged-`cognito:groups` scenario lands here too — we
    cannot tell a forged token from a real one at the API layer (no JWT
    signature verification on the Function URL path), so the canary value
    appears in details.caller_groups and that is enough signal for an auditor.
    """
    caller_groups = _caller_groups(event)
    if "ciso" not in caller_groups:
        # Best-effort audit write. Wrapped so any failure in claim extraction
        # or in _audit itself never blocks the 403 response.
        try:
            claims = _caller_claims(event)
            req_ctx = event.get("requestContext", {}) or {}
            http_ctx = req_ctx.get("http", {}) or {}
            path = (event.get("rawPath")
                    or event.get("path")
                    or http_ctx.get("path")
                    or "unknown")
            method = (event.get("httpMethod")
                      or http_ctx.get("method")
                      or "")
            source_ip = (http_ctx.get("sourceIp")
                         or (req_ctx.get("identity", {}) or {}).get("sourceIp")
                         or "unknown")
            user_label = (claims.get("email")
                          or claims.get("cognito:username")
                          or _caller_user_id(event)
                          or "unknown")
            _audit(
                "CROSS_PERSONA",
                path,
                user_label,
                "DENIED",
                {
                    "path": path,
                    "method": method,
                    "required_group": "ciso",
                    "caller_groups": caller_groups,
                    "caller_sub": claims.get("sub"),
                    "source_ip": source_ip,
                },
                ttl_seconds=90 * 24 * 60 * 60,  # 90 days
            )
        except Exception as e:
            logger.warning("audit CROSS_PERSONA write failed: %s", e)
        return _err(403, "Token Tracking is restricted to the CISO persona")
    return None


def _parse_token_usage_filters(event, default_range: str = "7d") -> dict:
    """Extract from/to/agent/persona from the query string.

    Accepts explicit ISO-8601 from/to OR a range= shortcut (today|7d|30d) which
    derives from/to relative to the current time. agent/persona values outside
    the known set are dropped (defense against typos producing empty queries).
    """
    qs = event.get("queryStringParameters") or {}
    from_iso = qs.get("from")
    to_iso = qs.get("to")
    if not from_iso or not to_iso:
        now = datetime.now(timezone.utc)
        rng = (qs.get("range") or default_range).lower()
        if rng == "today":
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elif rng == "30d":
            start = now - timedelta(days=30)
        else:  # 7d default; matches the page's initial state
            start = now - timedelta(days=7)
        from_iso = from_iso or start.isoformat()
        to_iso = to_iso or now.isoformat()
    agent = qs.get("agent")
    if agent and agent not in _VALID_AGENTS_FOR_QUERY:
        agent = None
    persona = qs.get("persona")
    if persona and persona not in _VALID_PERSONAS_FOR_QUERY:
        persona = None
    return {"from": from_iso, "to": to_iso, "agent": agent, "persona": persona}


def _query_token_usage_records(filters: dict, max_items: int = 5000) -> list:
    """Pick the right index for the filter combination and return matched rows.

    Decision matrix:
      persona set        → query persona-time-index (optional FilterExpression for agent)
      agent set only     → query agent-time-index
      neither set        → fan out across all 4 personas on persona-time-index,
                           merge — keeps everything as GSI Query (no scan, low cost)
    """
    if not token_usage_table:
        return []
    from_iso = filters["from"]
    to_iso = filters["to"]
    agent = filters.get("agent")
    persona = filters.get("persona")

    def _q(index_name: str, key_expr, filter_expr=None) -> list:
        params = {"IndexName": index_name, "KeyConditionExpression": key_expr}
        if filter_expr is not None:
            params["FilterExpression"] = filter_expr
        return _ddb_query_all(token_usage_table, params, max_items - len(items))

    items: list = []
    if persona:
        items.extend(_q(
            "persona-time-index",
            Key("persona").eq(persona) & Key("timestamp").between(from_iso, to_iso),
            Attr("agent").eq(agent) if agent else None,
        ))
    elif agent:
        items.extend(_q(
            "agent-time-index",
            Key("agent").eq(agent) & Key("timestamp").between(from_iso, to_iso),
        ))
    else:
        for p in _VALID_PERSONAS_FOR_QUERY:
            if len(items) >= max_items:
                break
            items.extend(_q(
                "persona-time-index",
                Key("persona").eq(p) & Key("timestamp").between(from_iso, to_iso),
            ))

    items.sort(key=lambda r: r.get("timestamp") or "", reverse=True)
    return items[:max_items]


def _ddb_query_all(table, params: dict, remaining: int) -> list:
    """Drive DDB query pagination until LastEvaluatedKey is gone or remaining is met."""
    out: list = []
    while remaining > 0:
        resp = table.query(**params)
        out.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        if len(out) >= remaining:
            break
        params["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return out[:remaining]


def _compute_token_summary(records: list) -> dict:
    """Aggregate records into the KPI shape the page's hook expects.

    Mirrors the JS-side _computeTokenSummary in ui/src/hooks/useApi.js so mock
    and live mode produce the same KPI numbers given the same records.

    Also emits three breakdown maps consumed by the Token Tracking page (and
    future "top spenders" surfaces): by_agent, by_persona, by_user. For
    by_user, `persona` is the persona on the user's most-recent row in the
    window (latest `ts` wins).
    """
    input_t = 0
    output_t = 0
    cost = 0.0
    blocked = 0
    sessions: set = set()
    # Breakdown buckets. Cost accumulates as float to match the existing
    # totalCost pattern above; the per-bucket float is rounded to 6 dp when
    # the response dict is assembled so json.dumps stays clean.
    by_agent: dict = {}
    by_persona: dict = {}
    by_user: dict = {}

    for r in records:
        r_input = _to_int_or_zero(r.get("input_tokens", 0))
        r_output = _to_int_or_zero(r.get("output_tokens", 0))
        input_t += r_input
        output_t += r_output
        r_total = r_input + r_output
        r_cost = 0.0
        c = r.get("estimated_cost")
        if c is not None:
            try:
                r_cost = float(c)
                cost += r_cost
            except (TypeError, ValueError):
                r_cost = 0.0
        if r.get("guardrail_blocked"):
            blocked += 1
        sid = r.get("session_id")
        if sid:
            sessions.add(sid)

        agent_id = r.get("agent") or ""
        persona = r.get("persona") or ""
        user_email = r.get("user_email") or ""
        ts = r.get("timestamp") or ""

        if agent_id:
            bucket = by_agent.setdefault(agent_id, {"tokens": 0, "cost": 0.0, "count": 0})
            bucket["tokens"] += r_total
            bucket["cost"] += r_cost
            bucket["count"] += 1

        if persona:
            bucket = by_persona.setdefault(persona, {"tokens": 0, "cost": 0.0, "count": 0})
            bucket["tokens"] += r_total
            bucket["cost"] += r_cost
            bucket["count"] += 1

        if user_email:
            bucket = by_user.setdefault(
                user_email,
                {"tokens": 0, "cost": 0.0, "count": 0, "persona": persona, "_latest_ts": ts},
            )
            bucket["tokens"] += r_total
            bucket["cost"] += r_cost
            bucket["count"] += 1
            # Track the persona on the user's most recent row in the window.
            if ts and ts >= bucket.get("_latest_ts", ""):
                bucket["_latest_ts"] = ts
                bucket["persona"] = persona

    total = input_t + output_t
    chats = len(sessions)

    by_agent_out = {
        k: {"tokens": v["tokens"], "cost": round(v["cost"], 6), "count": v["count"]}
        for k, v in by_agent.items()
    }
    by_persona_out = {
        k: {"tokens": v["tokens"], "cost": round(v["cost"], 6), "count": v["count"]}
        for k, v in by_persona.items()
    }
    by_user_out = {
        k: {
            "tokens": v["tokens"],
            "cost": round(v["cost"], 6),
            "count": v["count"],
            "persona": v.get("persona", ""),
        }
        for k, v in by_user.items()
    }

    return {
        "totalTokens": total,
        "inputTokens": input_t,
        "outputTokens": output_t,
        "totalCost": round(cost, 6),
        "avgPerChat": (total // chats) if chats else 0,
        "chats": chats,
        "blocked": blocked,
        "by_agent": by_agent_out,
        "by_persona": by_persona_out,
        "by_user": by_user_out,
    }


def _to_int_or_zero(v) -> int:
    if v is None:
        return 0
    if isinstance(v, Decimal):
        return int(v)
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _handle_list_token_usage(event):
    """GET /token-usage?from=&to=&agent=&persona= — returns {records, count, filters}."""
    denied = _require_ciso(event)
    if denied:
        return denied
    if not token_usage_table:
        return _err(500, "TOKEN_USAGE_TABLE not configured")
    filters = _parse_token_usage_filters(event)
    try:
        items = _query_token_usage_records(filters)
    except Exception as e:
        logger.exception("token-usage query failed")
        return _err(502, f"{type(e).__name__}: {e}")
    return _ok({"records": items, "count": len(items), "filters": filters})


def _handle_token_usage_summary(event):
    """GET /token-usage/summary?range=today|7d|30d&agent=&persona= — returns KPI shape."""
    denied = _require_ciso(event)
    if denied:
        return denied
    if not token_usage_table:
        return _err(500, "TOKEN_USAGE_TABLE not configured")
    # Summary defaults to today (matches KPI "tokens today" framing on the page);
    # explicit ?range= or ?from=/?to= still wins.
    filters = _parse_token_usage_filters(event, default_range="today")
    try:
        items = _query_token_usage_records(filters)
    except Exception as e:
        logger.exception("token-usage summary failed")
        return _err(502, f"{type(e).__name__}: {e}")
    return _ok(_compute_token_summary(items))


# ──────────────────────────── /conversations (list) ─────────────
def _handle_list_conversations(event):
    if not sessions_table:
        return _err(500, "SESSIONS_TABLE not configured")
    user_id = _caller_user_id(event)
    if not user_id:
        return _err(401, "Could not resolve caller identity")

    qs = event.get("queryStringParameters") or {}
    requested_type = (qs.get("type") or "").strip().lower() or None

    try:
        resp = sessions_table.query(
            IndexName="user-sessions-index",
            KeyConditionExpression=Key("user_id").eq(user_id),
            ScanIndexForward=False,  # newest first
            Limit=50,
        )
    except Exception as e:
        logger.exception("Query failed")
        return _err(502, f"{type(e).__name__}: {e}")

    sessions = [_session_summary(item) for item in resp.get("Items", [])]
    if requested_type in ("analyst", "mcp"):
        sessions = [s for s in sessions if (s.get("chat_type") or "analyst") == requested_type]
    return _ok({"sessions": sessions})


# ──────────────────────────── /conversations/{id} ───────────────
def _handle_get_conversation(event, session_id: str):
    """Returns conversation metadata only (no messages). Use /messages for those."""
    if not sessions_table:
        return _err(500, "SESSIONS_TABLE not configured")
    user_id = _caller_user_id(event)
    if not user_id:
        return _err(401, "Could not resolve caller identity")
    if not session_id:
        return _err(400, "Missing session_id")

    try:
        resp = sessions_table.get_item(Key={"session_id": session_id})
    except Exception as e:
        logger.exception("GetItem failed")
        return _err(502, f"{type(e).__name__}: {e}")

    item = resp.get("Item")
    if not item or item.get("user_id") != user_id:
        return _err(404, f"Session {session_id} not found")
    return _ok(_session_summary(item))


# ──────────────────────────── DELETE /conversations/{id} ────────
def _handle_delete_conversation(event, session_id: str):
    """Hard-delete a conversation's DDB index row (per-chat trash button).

    Ownership is enforced first: session_id is the table's only key, so
    without the check any authenticated caller could delete any chat by id.
    We delete the DDB row only — that makes the chat unreachable from every
    surface (both /messages and /{id} gate on the row existing). The raw
    events stay in AgentCore Memory until its retention expires.
    """
    if not sessions_table:
        return _err(500, "SESSIONS_TABLE not configured")
    user_id = _caller_user_id(event)
    if not user_id:
        return _err(401, "Could not resolve caller identity")
    if not session_id:
        return _err(400, "Missing session_id")

    # Fast-path ownership lookup — gives a clean 404 for legitimately-missing
    # rows without burning a conditional write. Race safety is enforced below.
    try:
        resp = sessions_table.get_item(Key={"session_id": session_id})
    except Exception as e:
        logger.exception("Ownership lookup failed")
        return _err(502, f"{type(e).__name__}: {e}")
    item = resp.get("Item")
    if not item or item.get("user_id") != user_id:
        return _err(404, f"Session {session_id} not found")

    # Conditional delete. DDB DeleteItem is otherwise idempotent — three
    # concurrent callers that all passed the check above would otherwise all
    # get 200 (TOCTOU race). The ConditionExpression makes DDB serialize
    # contenders: the first wins (200), the rest get ConditionalCheckFailed
    # → 404, matching the "already gone" semantics.
    try:
        sessions_table.delete_item(
            Key={"session_id": session_id},
            ConditionExpression="attribute_exists(session_id) AND user_id = :uid",
            ExpressionAttributeValues={":uid": user_id},
        )
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return _err(404, f"Session {session_id} not found")
        logger.exception("DeleteItem failed")
        return _err(502, f"{type(e).__name__}: {e}")
    except Exception as e:
        logger.exception("DeleteItem failed")
        return _err(502, f"{type(e).__name__}: {e}")

    return _ok({"deleted": True, "session_id": session_id})


# Prefixes the adversarial harness uses when minting session ids — must mirror
# HARNESS_PREFIXES in ui/src/components/ClearChatsButton.jsx so the "harness"
# scope deletes server-side exactly what the UI counts client-side.
HARNESS_SESSION_PREFIXES = ("harness-", "features-", "logic-race-")

# Upper bound on how many of the caller's sessions one scope call may sweep.
# A user with more than this in a single scope must clear in batches; the
# response sets truncated=true so the UI knows to re-invoke.
BULK_DELETE_SCOPE_CAP = 1000


def _iter_user_sessions(user_id: str):
    """Yield every session item for `user_id`, paginating the GSI query."""
    last_key = None
    while True:
        kwargs = {
            "IndexName": "user-sessions-index",
            "KeyConditionExpression": Key("user_id").eq(user_id),
            "ScanIndexForward": False,
        }
        if last_key:
            kwargs["ExclusiveStartKey"] = last_key
        resp = sessions_table.query(**kwargs)
        for item in resp.get("Items", []):
            yield item
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            return


def _session_matches_scope(item: dict, scope: str, cutoff_ms: int | None) -> bool:
    sid = item.get("session_id") or ""
    if scope == "all":
        return True
    if scope == "harness":
        return any(sid.startswith(p) for p in HARNESS_SESSION_PREFIXES)
    if scope == "older_than_days":
        raw = item.get("created_at")
        if not raw:
            return False
        try:
            # tolerate trailing Z by replacing with +00:00 for fromisoformat
            ts_ms = int(datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp() * 1000)
        except (TypeError, ValueError):
            return False
        return cutoff_ms is not None and ts_ms < cutoff_ms
    return False


# ──────────────────────────── POST /conversations/bulk-delete ───
def _handle_bulk_delete_conversations(event):
    """Hard-delete the caller's conversations in one round trip.

    Two request shapes are accepted:

    1. Explicit ids — Body: {"session_ids": [<str>, ...]} with 1 <= len <= 100.
       Per-id ownership check + delete_item. Per-id outcomes route into
       `deleted` or `failed[{session_id, reason}]` (`not_found` / `forbidden`
       / `error`).

    2. Server-side scope — Body: {"scope": "all" | "harness" | "older_than_days",
       "days": N}. Paginates the user-sessions GSI, filters to rows matching
       the scope, deletes each. Up to BULK_DELETE_SCOPE_CAP rows per call;
       any beyond that come back as `truncated=true` and the UI re-invokes.

    Returns 200 with the summary even when some ids fail; only malformed
    body / oversized list / missing caller / missing table return a non-200.
    """
    if not sessions_table:
        return _err(500, "sessions table not configured")

    try:
        body = json.loads(event.get("body") or "")
    except (ValueError, TypeError):
        return _err(400, "missing session_ids")
    if not isinstance(body, dict):
        return _err(400, "missing session_ids")

    user_id = _caller_user_id(event)
    if not user_id:
        return _err(401, "unauthorized")

    scope = body.get("scope")
    if scope is not None:
        return _bulk_delete_by_scope(user_id, body, scope)
    return _bulk_delete_by_ids(user_id, body.get("session_ids"))


def _bulk_delete_by_ids(user_id: str, session_ids):
    if not isinstance(session_ids, list) or not session_ids:
        return _err(400, "missing session_ids")
    if len(session_ids) > 100:
        return _err(400, "too many ids")

    deleted: list[str] = []
    failed: list[dict] = []
    for sid in session_ids:
        try:
            # Fast-path get distinguishes not_found from forbidden in the
            # response shape (the API contract callers rely on). Race safety
            # comes from the conditional delete below — without it, two
            # parallel bulk-delete calls overlapping on the same ids would
            # both report "deleted" for each shared id (DDB DeleteItem is
            # idempotent, TOCTOU race).
            resp = sessions_table.get_item(Key={"session_id": sid})
            item = resp.get("Item")
            if not item:
                failed.append({"session_id": sid, "reason": "not_found"})
                continue
            if item.get("user_id") != user_id:
                failed.append({"session_id": sid, "reason": "forbidden"})
                continue
            try:
                sessions_table.delete_item(
                    Key={"session_id": sid},
                    ConditionExpression="attribute_exists(session_id) AND user_id = :uid",
                    ExpressionAttributeValues={":uid": user_id},
                )
            except ClientError as ce:
                if ce.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                    # Lost the race to a concurrent caller — the row is gone
                    # from someone else's delete. Report not_found, not error.
                    failed.append({"session_id": sid, "reason": "not_found"})
                    continue
                raise
            deleted.append(sid)
        except ClientError:
            logger.exception("Bulk delete: DDB error on session_id=%s", sid)
            failed.append({"session_id": sid, "reason": "error"})
        except Exception:
            logger.exception("Bulk delete: unexpected error on session_id=%s", sid)
            failed.append({"session_id": sid, "reason": "error"})

    return _ok({"deleted": deleted, "failed": failed})


def _bulk_delete_by_scope(user_id: str, body: dict, scope):
    if scope not in ("all", "harness", "older_than_days"):
        return _err(400, "invalid scope")
    cutoff_ms = None
    if scope == "older_than_days":
        raw_days = body.get("days")
        try:
            days = int(raw_days)
        except (TypeError, ValueError):
            return _err(400, "days must be a positive integer")
        if days <= 0:
            return _err(400, "days must be a positive integer")
        cutoff_ms = int((datetime.now(timezone.utc).timestamp() - days * 86400) * 1000)

    deleted: list[str] = []
    failed: list[dict] = []
    truncated = False
    try:
        for item in _iter_user_sessions(user_id):
            if not _session_matches_scope(item, scope, cutoff_ms):
                continue
            if len(deleted) + len(failed) >= BULK_DELETE_SCOPE_CAP:
                truncated = True
                break
            sid = item.get("session_id")
            if not sid:
                continue
            try:
                sessions_table.delete_item(Key={"session_id": sid})
                deleted.append(sid)
            except ClientError:
                logger.exception("Bulk delete by scope: DDB error on session_id=%s", sid)
                failed.append({"session_id": sid, "reason": "error"})
            except Exception:
                logger.exception("Bulk delete by scope: unexpected error on session_id=%s", sid)
                failed.append({"session_id": sid, "reason": "error"})
    except ClientError as e:
        logger.exception("Bulk delete by scope: query failed")
        return _err(502, f"{type(e).__name__}: {e}")

    return _ok({"deleted": deleted, "failed": failed, "truncated": truncated})


# ──────────────────────────── /conversations/{id}/messages ──────
def _handle_get_messages(event, session_id: str):
    """Stream messages from AgentCore Memory in chronological order.

    Verifies ownership against the DDB index row first so we don't leak
    one user's messages to another. Memory itself doesn't enforce per-user
    isolation — it's scoped by (actorId, sessionId).
    """
    if not MEMORY_ID:
        return _err(500, "MEMORY_ID not configured")
    if not sessions_table:
        return _err(500, "SESSIONS_TABLE not configured")
    user_id = _caller_user_id(event)
    if not user_id:
        return _err(401, "Could not resolve caller identity")
    if not session_id:
        return _err(400, "Missing session_id")

    # Ownership check — the row must exist and belong to the caller.
    try:
        resp = sessions_table.get_item(Key={"session_id": session_id})
    except Exception as e:
        logger.exception("Ownership lookup failed")
        return _err(502, f"{type(e).__name__}: {e}")
    item = resp.get("Item")
    if not item or item.get("user_id") != user_id:
        return _err(404, f"Session {session_id} not found")

    # Fetch events from memory. AgentCore returns newest-first; we reverse
    # so the UI gets chronological order.
    messages: list[dict[str, Any]] = []
    try:
        ev_resp = agentcore.list_events(
            memoryId=MEMORY_ID,
            actorId=user_id,
            sessionId=session_id,
            maxResults=100,
            includePayloads=True,
        )
    except Exception as e:
        logger.exception("list_events failed")
        return _err(502, f"{type(e).__name__}: {e}")

    for ev in reversed(ev_resp.get("events") or []):
        ts = ev.get("eventTimestamp")
        ts_iso = ts.isoformat() if hasattr(ts, "isoformat") else (ts or "")
        for part in ev.get("payload") or []:
            conv = part.get("conversational") or {}
            role = (conv.get("role") or "").lower()
            text = (conv.get("content") or {}).get("text") or ""
            if role and text:
                messages.append({"role": role, "content": text, "ts": ts_iso})

    return _ok({"session_id": session_id, "messages": messages})


# ──────────────────────────── /dashboard ────────────────────────
def _handle_dashboard(event):
    """Aggregate KPI / heatmap / last-scan / activity / trend.

    One round-trip drives the Governance Overview landing page. We perform
    three scans (conflicts-v2, scan-runs, audit-log) and one CR scan and
    fold them into the documented shape. Demo scale is small enough that
    Scan-based reads are fine; production would switch to GSI queries.
    """
    tbl = _findings_table()
    findings = []
    if tbl:
        try:
            findings = tbl.scan(Limit=200).get("Items", [])
        except Exception as e:
            logger.exception("dashboard findings scan failed")

    conflicts = [f for f in findings if not f.get("compliant")]
    # Scope active conflicts to the latest COMPLETED scan + OPEN status so the KPIs,
    # heatmap, and trend match the Findings view. The scanner upserts + never deletes,
    # so resolved/stale rows (older scan_run_ids) must NOT count as active — otherwise
    # the dashboard inflates (e.g. 14) vs Findings (10). Safety guard mirrors
    # _handle_list_findings: only narrow when that run actually wrote rows here.
    latest_run = _latest_completed_scan_run_id()
    if latest_run:
        scoped = [c for c in conflicts if c.get("scan_run_id") == latest_run]
        if scoped:
            conflicts = scoped
    conflicts = [c for c in conflicts if (c.get("status") or "OPEN").upper() == "OPEN"]
    crs = []
    if crs_table:
        try:
            crs = crs_table.scan(Limit=200).get("Items", [])
        except Exception as e:
            logger.exception("dashboard CR scan failed")
    audit = []
    if audit_table:
        try:
            audit = audit_table.scan(Limit=200).get("Items", [])
            audit.sort(key=lambda i: i.get("timestamp") or "", reverse=True)
        except Exception as e:
            logger.exception("dashboard audit scan failed")
    scan_runs = []
    if scan_runs_table:
        try:
            scan_runs = scan_runs_table.scan(Limit=200).get("Items", [])
            scan_runs.sort(key=lambda r: r.get("started_at") or "", reverse=True)
        except Exception as e:
            logger.exception("dashboard scan-runs scan failed")

    # KPIs
    sev_counts = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0}
    for c in conflicts:
        sev = (c.get("severity") or "").upper()
        if sev in sev_counts:
            sev_counts[sev] += 1
    pending_approvals = sum(1 for cr in crs if (cr.get("status") or "") == "PENDING_APPROVAL")

    # Heatmap: 6 domains × 2 source pairs, counts of conflicts only.
    domain_keys = ["ACCESS_MGMT", "NETWORK_SECURITY", "DATA_GOVERNANCE",
                   "CLOUD_SECURITY", "COMPLIANCE", "VENDOR_MGMT"]
    source_pairs = ["SharePoint+Zscaler", "SharePoint+AWS Config"]
    cells = [[0 for _ in source_pairs] for _ in domain_keys]
    for c in conflicts:
        try:
            di = domain_keys.index(c.get("domain"))
            si = source_pairs.index(c.get("source_pair"))
        except ValueError:
            continue
        cells[di][si] += 1
    domain_labels = {
        "ACCESS_MGMT": "Access Mgmt", "NETWORK_SECURITY": "Network Security",
        "DATA_GOVERNANCE": "Data Governance", "CLOUD_SECURITY": "Cloud Security",
        "COMPLIANCE": "Compliance", "VENDOR_MGMT": "Vendor Mgmt",
    }

    last_scan = None
    completed = [r for r in scan_runs if (r.get("status") or "") == "COMPLETED"]
    if completed:
        r = completed[0]
        last_scan = {
            "scan_run_id": r.get("scan_run_id"),
            "started_at": r.get("started_at"),
            "finished_at": r.get("finished_at"),
            "totals": r.get("totals") or {},
        }

    # Trend: 30 buckets, per-day per-severity totals derived from scan-runs history.
    # For each day, we pick the latest COMPLETED scan that started on or before
    # end-of-that-day; if none exists yet, we fall back to the current open counts
    # so the chart renders something meaningful on day-one demos.
    open_now_by_sev = {"critical": sev_counts.get("CRITICAL", 0),
                       "high":     sev_counts.get("HIGH", 0),
                       "medium":   sev_counts.get("MEDIUM", 0),
                       "low":      sev_counts.get("LOW", 0)}

    completed_runs = []
    for r in scan_runs:
        if (r.get("status") or "") != "COMPLETED":
            continue
        try:
            run_dt = datetime.fromisoformat((r.get("started_at") or "").replace("Z", "+00:00"))
        except Exception:
            continue
        completed_runs.append((run_dt, r))
    completed_runs.sort(key=lambda x: x[0])  # ascending

    now_utc = datetime.now(timezone.utc)
    today_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    trend = []
    cursor_idx = 0  # walk completed_runs forward in lockstep with the day loop
    latest_totals = None
    for d in range(29, -1, -1):
        day_start = today_start - timedelta(days=d)
        day_end = day_start + timedelta(days=1)
        # Advance cursor through any scans that started before this day's end.
        while cursor_idx < len(completed_runs) and completed_runs[cursor_idx][0] < day_end:
            latest_totals = completed_runs[cursor_idx][1].get("totals") or {}
            cursor_idx += 1
        if latest_totals is not None:
            trend.append({
                "date":     day_start.strftime("%Y-%m-%d"),
                "critical": int(latest_totals.get("critical") or 0),
                "high":     int(latest_totals.get("high") or 0),
                "medium":   int(latest_totals.get("medium") or 0),
                "low":      int(latest_totals.get("low") or 0),
                # keep `open` for backward compatibility with any older client
                "open":     int((latest_totals.get("critical") or 0)
                                + (latest_totals.get("high") or 0)
                                + (latest_totals.get("medium") or 0)
                                + (latest_totals.get("low") or 0)),
            })
        else:
            trend.append({
                "date":     day_start.strftime("%Y-%m-%d"),
                "critical": open_now_by_sev["critical"],
                "high":     open_now_by_sev["high"],
                "medium":   open_now_by_sev["medium"],
                "low":      open_now_by_sev["low"],
                "open":     sum(open_now_by_sev.values()),
            })

    # Policies indexed — count distinct policy docs from citations.
    indexed = set()
    for c in conflicts + [f for f in findings if f.get("compliant")]:
        for cit in (c.get("policy_citations") or []):
            doc = (cit.get("doc") if isinstance(cit, dict) else None)
            if doc:
                indexed.add(doc)
    if not indexed:
        # Fall back to legacy source_policy field prefix.
        for c in conflicts:
            sp = c.get("source_policy") or ""
            if sp.startswith("MIG-POL-"):
                indexed.add(sp.split(" ")[0].split("-CS")[0].split("-RA")[0]
                              .split("-WB")[0].split("-SSL")[0].split("-MFA")[0]
                              .split("-IOT")[0].split("-WAF")[0].split("-SEG")[0]
                              .split("-DR")[0].split("-DT")[0].split("-VA")[0]
                              .split("-SM")[0])
    policies_indexed = len(indexed) or 5  # known demo corpus

    mcp_health = "UNKNOWN"
    # If no endpoints configured, surface UP for demo so the tile renders green;
    # the dedicated /mcp-health endpoint returns the per-endpoint breakdown.
    if not MCP_ENDPOINTS:
        mcp_health = "UP"

    return _ok({
        "kpis": {
            "policies_indexed": policies_indexed,
            "active_conflicts": sev_counts,
            "pending_approvals": pending_approvals,
            "mcp_health": mcp_health,
        },
        "heatmap": {
            "rows": [domain_labels[k] for k in domain_keys],
            "row_keys": domain_keys,
            "cols": source_pairs,
            "cells": cells,
        },
        "last_scan": last_scan,
        "recent_activity": audit[:5],
        "trend": trend,
    })


# ──────────────────────────── /scan-runs ────────────────────────
def _handle_list_scan_runs(event):
    if not scan_runs_table:
        return _err(500, "SCAN_RUNS_TABLE not configured")
    try:
        resp = scan_runs_table.scan(Limit=50)
        items = resp.get("Items", [])
    except Exception as e:
        logger.exception("scan-runs scan failed")
        return _err(502, f"{type(e).__name__}: {e}")
    items.sort(key=lambda r: r.get("started_at") or "", reverse=True)
    return _ok({"scan_runs": items[:10]})


def _handle_get_scan_run(event, scan_run_id: str):
    if not scan_runs_table:
        return _err(500, "SCAN_RUNS_TABLE not configured")
    if not scan_run_id:
        return _err(400, "Missing scan_run_id")
    try:
        # PK + SK schema — Query, return latest row for this scan_run_id.
        resp = scan_runs_table.query(
            KeyConditionExpression=Key("scan_run_id").eq(scan_run_id),
            ScanIndexForward=False,
            Limit=1,
        )
        items = resp.get("Items", [])
    except Exception as e:
        logger.exception("scan-runs GetItem failed")
        return _err(502, f"{type(e).__name__}: {e}")
    if not items:
        return _err(404, f"scan_run {scan_run_id} not found")
    return _ok(items[0])


# ──────────────────────────── /findings/{id} ────────────────────
def _handle_get_finding(event, conflict_id: str):
    tbl = _findings_table()
    if not tbl:
        return _err(500, "CONFLICTS_TABLE / CONFLICTS_TABLE_V2 not configured")
    if not conflict_id:
        return _err(400, "Missing conflict_id")
    try:
        # v2 has PK-only; legacy has PK+SK. Prefer Query on PK (works for both).
        resp = tbl.query(
            KeyConditionExpression=Key("conflict_id").eq(conflict_id),
            Limit=1,
            ScanIndexForward=False,
        )
        items = resp.get("Items", [])
    except Exception as e:
        logger.exception("finding GetItem failed")
        return _err(502, f"{type(e).__name__}: {e}")
    if not items:
        return _err(404, f"conflict {conflict_id} not found")
    return _ok(items[0])


# ──────────────────────────── /mcp-health ───────────────────────
def _handle_mcp_health(event):
    """Demo-friendly MCP health surface.

    Returns one entry per configured MCP_ENDPOINTS URL. When no URLs are
    configured, returns the known demo MCP servers as healthy so the UI
    tile renders something meaningful. Production would replace this with
    real ping logic against each MCP server.
    """
    if not MCP_ENDPOINTS:
        servers = [
            {"name": "SharePoint MCP",  "status": "UP", "latency_ms": 320, "detail": "Graph API · policy docs"},
            {"name": "Zscaler MCP",     "status": "UP", "latency_ms": 410, "detail": "ZIA REST · rules"},
            {"name": "AWS Config MCP",  "status": "UP", "latency_ms": 95,  "detail": "EventBridge stream"},
            {"name": "ServiceNow MCP",  "status": "DEGRADED", "latency_ms": 1240, "detail": "API gateway latency 1240ms (SLA 300ms)"},
            {"name": "Atlassian MCP",   "status": "UP", "latency_ms": 280, "detail": "JIRA Cloud (via Claude session)"},
        ]
    else:
        # Best-effort: don't actually ping (would require urllib3 timeouts and
        # network ACLs). Report endpoints as "UP" with a 0 latency placeholder.
        servers = [{"name": u, "status": "UP", "latency_ms": 0, "detail": "configured endpoint"} for u in MCP_ENDPOINTS]
    summary = "UP" if all(s["status"] == "UP" for s in servers) else ("DEGRADED" if any(s["status"] == "DEGRADED" for s in servers) else "DOWN")
    return _ok({"summary": summary, "servers": servers})


# ──────────────────────────── /agent-status ─────────────────────
_AGENT_DISPLAY_NAMES = {
    "sharepoint": "SharePoint Specialist",
    "awsconfig":  "AWS Config Specialist",
    "zscaler":    "Zscaler ZIA Specialist",
    "structured": "Structured Data Specialist",
    "paloalto":   "Palo Alto NGFW Specialist",
    "jira":       "JIRA Specialist",
    "servicenow": "ServiceNow Specialist",
}


def _handle_agent_status(event):
    """Live status of the ARBITER specialist runtimes for the MCP page.

    Calls bedrock-agentcore-control list_agent_runtimes and matches each
    specialist by its configured ARN (patched onto this Lambda by
    deploy_agents.py) → {id, name, status}. Agents not yet deployed (no ARN, or
    the ARN isn't in the live list) report PLACEHOLDER. Matching by ARN avoids
    hardcoding the project name.
    """
    arn_to_status: dict[str, str] = {}
    try:
        paginator = agentcore_control.get_paginator("list_agent_runtimes")
        for page in paginator.paginate():
            for r in page.get("agentRuntimes", []):
                arn_to_status[r.get("agentRuntimeArn", "")] = r.get("status", "UNKNOWN")
    except Exception as e:
        logger.warning("list_agent_runtimes failed: %s", e)

    servers = []
    for agent_id, name in _AGENT_DISPLAY_NAMES.items():
        arn = SPECIALIST_RUNTIME_ARNS.get(agent_id, "")
        status = arn_to_status.get(arn, "PLACEHOLDER") if arn else "PLACEHOLDER"
        servers.append({"id": agent_id, "name": name, "status": status})
    return _ok({"servers": servers})


# ──────────────────────────── team ownership / routing ──────────
# Maps an owning team → its JIRA destination. For the demo every team routes to
# the one real project (DEVARBITER) so a ticket never fails on a non-existent
# project; the owning team is surfaced in the issue body and on the CR. Swap to
# per-team projects/components once Meridian's JIRA structure is confirmed.
TEAM_ROUTING = {
    "platform-security": {"project_key": "DEVARBITER", "component": "Security Platform"},
    "network-eng":       {"project_key": "DEVARBITER", "component": "Network Engineering"},
    "cloud-infra":       {"project_key": "DEVARBITER", "component": "Cloud Infrastructure"},
    "data-governance":   {"project_key": "DEVARBITER", "component": "Data Governance"},
    "app-dev":           {"project_key": "DEVARBITER", "component": "Application Development"},
    "vendor-mgmt":       {"project_key": "DEVARBITER", "component": "Vendor Management"},
}
_DEFAULT_ROUTING = {"project_key": "DEVARBITER", "component": None}


def _route_for_team(owner_team: str) -> dict:
    return TEAM_ROUTING.get((owner_team or "").strip(), _DEFAULT_ROUTING)


def _finding_ownership(conflict_id: str) -> dict:
    """Server-side lookup of a finding's team ownership.

    Never trust the client for this — owner_team drives ticket routing (and,
    post-demo, RBAC scoping). Returns blanks when the finding or its ownership
    can't be resolved, so callers degrade gracefully to the DEVARBITER default.
    """
    out = {"owner_team": "", "consumer_team": "", "platform_team": "", "tags": []}
    tbl = _findings_table()
    if not tbl or not conflict_id:
        return out
    try:
        resp = tbl.query(KeyConditionExpression=Key("conflict_id").eq(conflict_id),
                         Limit=1, ScanIndexForward=False)
        items = resp.get("Items", [])
    except Exception:
        logger.exception("finding ownership lookup failed")
        return out
    if items:
        f = items[0]
        out["owner_team"] = f.get("owner_team") or ""
        out["consumer_team"] = f.get("consumer_team") or ""
        out["platform_team"] = f.get("platform_team") or ""
        out["tags"] = list(f.get("tags") or [])
    return out


# ──────────────────────────── /jira/tickets ─────────────────────
def _audit_jira(event, *, key, summary, severity, conflict_id, status, mock):
    """Best-effort audit row for a JIRA link/create."""
    if not audit_table:
        return
    try:
        audit_table.put_item(Item={
            "event_id": f"jira-{key}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action_type": "JIRA_LINKED",
            "resource": conflict_id or summary,
            "user": (_caller_user_id(event) or "anonymous"),
            "status": status,
            "details": json.dumps({"jira_ticket_key": key, "summary": summary,
                                   "severity": severity, "mock": mock}),
        })
    except Exception:
        logger.exception("JIRA audit write failed")


def _handle_jira_create(event):
    """Create a real JIRA issue via the jira_specialist AgentCore runtime.

    The ActionCenter UI sends an editable summary + description and a project
    key (DEVARBITER). We invoke the JIRA runtime's deterministic create path
    ({"action": "create_issue", ...}) which calls mcp-atlassian directly and
    returns {key, url}. If the runtime isn't deployed, fall back to a mock key
    so the demo flow still renders.
    """
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _err(400, "Invalid JSON body")
    conflict_id = (body.get("conflict_id") or "").strip()
    cr_id = (body.get("cr_id") or "").strip()
    summary = (body.get("summary") or body.get("title") or "").strip() or f"ARBITER finding {conflict_id or 'unspecified'}"
    description = (body.get("description") or "").strip()
    project_key = (body.get("project_key") or "DEVARBITER").strip()
    severity = (body.get("severity") or "MEDIUM").strip()

    # Route by the finding's OWNING team (server-derived, never client-supplied).
    # Component routing requires the component to pre-exist in JIRA, so we
    # annotate the issue body with the team rather than risk a create failure.
    ownership = _finding_ownership(conflict_id)
    routing = _route_for_team(ownership["owner_team"])
    project_key = routing["project_key"] or project_key
    if ownership["owner_team"]:
        description = (description + f"\n\nRouted to team: {ownership['owner_team']}").strip()

    jira_arn = SPECIALIST_RUNTIME_ARNS.get("jira")
    if not jira_arn:
        # JIRA runtime not deployed yet — mock so the UI flow still works.
        import hashlib
        suffix = int(hashlib.sha256(summary.encode("utf-8")).hexdigest()[:6], 16) % 100000
        mock_key = f"{project_key}-MOCK-{suffix:05d}"
        _audit_jira(event, key=mock_key, summary=summary, severity=severity,
                    conflict_id=conflict_id, status="MOCK", mock=True)
        return _ok({"status": "mock", "jira_ticket_key": mock_key,
                    "note": "JIRA runtime not configured — run scripts/deploy_agents.py."})

    try:
        resp = agentcore.invoke_agent_runtime(
            agentRuntimeArn=jira_arn,
            payload=json.dumps({
                "action": "create_issue",
                "project_key": project_key,
                "summary": summary,
                "description": description,
                "issue_type": "Task",
            }).encode("utf-8"),
            contentType="application/json",
            accept="application/json",
        )
        parsed = json.loads(resp["response"].read().decode("utf-8"))
    except Exception as e:
        logger.exception("JIRA runtime invocation failed")
        return _err(502, f"{type(e).__name__}: {e}")

    key = parsed.get("key")
    if not key:
        return _err(502, f"JIRA create failed: {parsed.get('error') or parsed.get('result') or 'no issue key returned'}")

    _audit_jira(event, key=key, summary=summary, severity=severity,
                conflict_id=conflict_id, status="COMPLETED", mock=False)
    _link_jira_to_cr(cr_id, key, parsed.get("url"))
    return _ok({"status": "created", "jira_ticket_key": key, "url": parsed.get("url")})


def _link_jira_to_cr(cr_id: str, key: str, url: str | None) -> None:
    """Persist a created/linked JIRA ticket onto its change-request row so the
    Action Center renders the ticket (and its L1 controls) across reloads."""
    if not cr_id or not crs_table:
        return
    try:
        crs_table.update_item(
            Key={"cr_id": cr_id},
            UpdateExpression="SET jira_ticket_key = :k, jira_ticket_url = :u",
            ExpressionAttributeValues={":k": key, ":u": url or ""},
        )
    except Exception:
        logger.exception("CR %s JIRA-link update failed", cr_id)


def _invoke_jira_action(action: str, args: dict) -> dict:
    """Invoke the jira_specialist runtime for a deterministic action path.

    Returns the parsed runtime response, or {"error": ...} when the runtime is
    unconfigured / the invocation fails. Callers decide how to surface it.
    """
    jira_arn = SPECIALIST_RUNTIME_ARNS.get("jira")
    if not jira_arn:
        return {"error": "JIRA runtime not configured (run scripts/deploy_agents.py)"}
    try:
        resp = agentcore.invoke_agent_runtime(
            agentRuntimeArn=jira_arn,
            payload=json.dumps({"action": action, **args}).encode("utf-8"),
            contentType="application/json",
            accept="application/json",
        )
        return json.loads(resp["response"].read().decode("utf-8"))
    except Exception as e:
        logger.exception("JIRA %s invocation failed", action)
        return {"error": f"{type(e).__name__}: {e}"}


def _handle_jira_transition(event):
    """Transition a JIRA issue (L1 resolution), optionally with a comment.

    Body: {jira_key, transition?, comment?, cr_id?}. transition defaults to
    "Done"; the agent resolves it by name → workflow id defensively.
    """
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _err(400, "Invalid JSON body")
    jira_key = (body.get("jira_key") or body.get("jira_ticket_key") or "").strip()
    transition = (body.get("transition") or "Done").strip()
    comment = (body.get("comment") or "").strip()
    cr_id = (body.get("cr_id") or "").strip()
    if not jira_key:
        return _err(400, "Missing jira_key")

    parsed = _invoke_jira_action("transition_issue", {
        "issue_key": jira_key, "transition": transition, "comment": comment})
    if parsed.get("error"):
        avail = parsed.get("available_transitions")
        detail = parsed["error"] + (f" (available: {', '.join(avail)})" if avail else "")
        return _err(502, f"JIRA transition failed: {detail}")

    user_id = _caller_user_id(event) or "anonymous"
    applied = parsed.get("transitioned_to") or transition
    _audit("JIRA_TRANSITIONED", cr_id or jira_key, user_id, "COMPLETED",
           {"jira_ticket_key": jira_key, "transition": applied, "comment": comment, "cr_id": cr_id})
    if cr_id and crs_table:
        try:
            crs_table.update_item(
                Key={"cr_id": cr_id},
                UpdateExpression="SET jira_ticket_key = :k, jira_status = :s",
                ExpressionAttributeValues={":k": jira_key, ":s": applied},
            )
        except Exception:
            logger.exception("CR %s jira_status update failed", cr_id)
    return _ok({"status": "transitioned", "jira_ticket_key": jira_key,
                "transitioned_to": applied, "url": parsed.get("url")})


def _handle_jira_comment(event):
    """Add a comment to a JIRA issue. Body: {jira_key, comment, cr_id?}."""
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _err(400, "Invalid JSON body")
    jira_key = (body.get("jira_key") or body.get("jira_ticket_key") or "").strip()
    comment = (body.get("comment") or "").strip()
    cr_id = (body.get("cr_id") or "").strip()
    if not jira_key:
        return _err(400, "Missing jira_key")
    if not comment:
        return _err(400, "Missing comment")

    parsed = _invoke_jira_action("add_comment", {"issue_key": jira_key, "comment": comment})
    if parsed.get("error"):
        return _err(502, f"JIRA comment failed: {parsed['error']}")

    user_id = _caller_user_id(event) or "anonymous"
    _audit("JIRA_COMMENTED", cr_id or jira_key, user_id, "COMPLETED",
           {"jira_ticket_key": jira_key, "comment": comment, "cr_id": cr_id})
    return _ok({"status": "commented", "jira_ticket_key": jira_key, "url": parsed.get("url")})


# ──────────────────────────── /servicenow/impact-analysis ───────
def _handle_servicenow_impact(event):
    """IT-asset change-impact analysis via the servicenow_specialist runtime.

    Body: {resource, target_environment?, severity?, draft_change?}. The runtime
    resolves the CMDB CI, walks cmdb_rel_ci for the blast radius, finds the
    owning team, and (when draft_change) drafts a change_request with the
    affected CIs attached. We graft the recommended approver chain here via
    _build_approver_chain (single source of truth, reused from the CR workflow)
    so "who approves" is consistent with ARBITER's own change requests.

    Falls back to a structured mock when the runtime isn't deployed, so the
    Impact Analysis page still renders (mirrors _handle_jira_create).
    """
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _err(400, "Invalid JSON body")
    resource = (body.get("resource") or body.get("target_resource") or "").strip()
    if not resource:
        return _err(400, "Missing 'resource'")
    target_env = (body.get("target_environment") or "PROD").strip().upper()
    severity = (body.get("severity") or "HIGH").strip().upper()
    draft_change = bool(body.get("draft_change"))
    approver_chain = _build_approver_chain(target_env, severity)

    sn_arn = SPECIALIST_RUNTIME_ARNS.get("servicenow")
    if not sn_arn:
        # Runtime not deployed — return the structure so the UI page works.
        return _ok({
            "configured": False, "changed_resource": {"input": resource},
            "affected_cis": [], "owner_team": "", "cab_required": target_env == "PROD",
            "approver_chain": approver_chain, "target_environment": target_env,
            "severity": severity,
            "note": "ServiceNow runtime not configured — run scripts/deploy_agents.py.",
        })

    try:
        resp = agentcore.invoke_agent_runtime(
            agentRuntimeArn=sn_arn,
            payload=json.dumps({
                "action": "impact_analysis",
                "resource": resource,
                "target_environment": target_env,
                "severity": severity,
                "draft_change": draft_change,
            }).encode("utf-8"),
            contentType="application/json",
            accept="application/json",
        )
        result = json.loads(resp["response"].read().decode("utf-8"))
    except Exception as e:
        logger.exception("ServiceNow impact-analysis invocation failed")
        return _err(502, f"{type(e).__name__}: {e}")

    if result.get("error"):
        return _err(502, f"ServiceNow impact analysis failed: {result['error']}")

    # Graft the approver chain (reused from the CR workflow) onto the CMDB facts.
    result["approver_chain"] = approver_chain
    user_id = _caller_user_id(event) or "anonymous"
    change = (result.get("change") or {})
    _audit("SERVICENOW_IMPACT_ANALYSIS", resource, user_id, "COMPLETED", {
        "affected_count": len(result.get("affected_cis") or []),
        "owner_team": result.get("owner_team"),
        "drafted_change": change.get("number"),
    })
    return _ok(result)


# ──────────────────────────── /servicenow/drift-scan ─────────────
def _handle_servicenow_drift(event):
    """CMDB / Asset drift report: reconcile the live ServiceNow CMDB against AWS.

    Invokes the master in `servicenow_drift_scan` mode — it pulls the CMDB+asset
    snapshot from the servicenow specialist, compares it to the AWS inventory, and
    returns only the DRIFT findings (unmanaged resources, stale CIs, ownership and
    asset drift) for the Drift Scan dashboard. The same drift also surfaces in the
    main /scan run as DRIFT findings. Falls back to a structured mock when the
    master runtime isn't deployed (mirrors _handle_servicenow_impact).
    """
    user_id = _caller_user_id(event) or "anonymous"
    if not MASTER_AGENT_RUNTIME_ARN:
        return _ok({
            "configured": False, "drift_items": [],
            "summary": {"total": 0, "by_kind": {}, "by_severity": {}},
            "note": "Master runtime not configured — run scripts/deploy_agents.py.",
        })
    try:
        resp = agentcore.invoke_agent_runtime(
            agentRuntimeArn=MASTER_AGENT_RUNTIME_ARN,
            payload=json.dumps({"servicenow_drift_scan": True}).encode("utf-8"),
            contentType="application/json",
            accept="application/json",
        )
        body = json.loads(resp["response"].read().decode("utf-8"))
        # The master returns {"result": "<json string>"} (same envelope as /scan).
        inner = body.get("result", body) if isinstance(body, dict) else body
        result = json.loads(inner) if isinstance(inner, str) else inner
    except Exception as e:
        logger.exception("ServiceNow drift-scan invocation failed")
        return _err(502, f"{type(e).__name__}: {e}")

    summary = result.get("summary") or {}
    _audit("SERVICENOW_DRIFT_SCAN", "cmdb", user_id, "COMPLETED", {
        "drift_total": summary.get("total", 0),
        "by_kind": summary.get("by_kind", {}),
    })
    return _ok(result)


# ──────────────────────────── POST /scan ────────────────────────
def _handle_scan_trigger(event):
    """Pre-write a RUNNING scan-runs row and async-invoke the scanner Lambda.

    Returning a scan_run_id before any row exists creates a polling race —
    the UI's first GET /scan-runs/{id} hits 404 because the scanner hasn't
    cold-started yet. We avoid that by writing the RUNNING row from the
    api_handler synchronously, then handing off to the scanner Lambda which
    writes its own row(s) with later started_at values. GET /scan-runs/{id}
    sorts by started_at DESC so the scanner's progress entries supersede
    this one as it runs.

    When SCANNER_LAMBDA_NAME is unset (pre-Step-3 deploy), the route flips
    the row straight to COMPLETED with synthetic totals so the demo still
    works without the scanner Lambda.
    """
    actor_id = _caller_user_id(event) or "anonymous"
    now_utc = datetime.now(timezone.utc)
    scan_run_id = f"scan-{now_utc.strftime('%Y-%m-%dT%H:%M:%SZ')}-{actor_id[:8]}"
    started_at = now_utc.isoformat()
    triggered_by = f"manual:{actor_id}"

    # Pre-write RUNNING row so the polling endpoint never sees a 404 race.
    if scan_runs_table:
        try:
            scan_runs_table.put_item(Item={
                "scan_run_id":       scan_run_id,
                "started_at":        started_at,
                "status":            "RUNNING",
                "triggered_by":      triggered_by,
                "rule_pack_version": "v1",
            })
        except Exception:
            logger.exception("scan-runs RUNNING pre-write failed (continuing)")

    if not SCANNER_LAMBDA_NAME:
        if scan_runs_table:
            try:
                scan_runs_table.update_item(
                    Key={"scan_run_id": scan_run_id, "started_at": started_at},
                    UpdateExpression="SET #s = :s, finished_at = :f, totals = :t",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={
                        ":s": "COMPLETED",
                        ":f": datetime.now(timezone.utc).isoformat(),
                        ":t": {"conflicts": 12, "compliant": 14, "critical": 4, "high": 4, "medium": 4, "low": 0},
                    },
                )
            except Exception:
                logger.exception("stub COMPLETED update failed")
        return _ok({"scan_run_id": scan_run_id, "status": "COMPLETED", "stub": True})

    try:
        lambda_client.invoke(
            FunctionName=SCANNER_LAMBDA_NAME,
            InvocationType="Event",
            Payload=json.dumps({
                "scan_run_id":  scan_run_id,
                "triggered_by": triggered_by,
            }).encode("utf-8"),
        )
    except Exception as e:
        logger.exception("scanner Lambda invoke failed")
        return _err(502, f"{type(e).__name__}: {e}")
    return _ok({"scan_run_id": scan_run_id, "status": "RUNNING"})


# ──────────────────────────── POST /scan/dry-run (What-If) ───────
def _handle_scan_dry_run(event):
    """What-If: run the rule pack against hypothetical observations and return the
    resulting findings WITHOUT persisting anything.

    The master is invoked DIRECTLY (not the scanner Lambda) so neither
    conflicts-v2 nor scan-runs is ever written — a What-If touches no finding
    state. Body: {observations: {<source>: [obs...]}}. Sources omitted from
    `observations` seed normally inside the master.
    """
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _err(400, "Invalid JSON body")
    observations = body.get("observations") or {}
    if not isinstance(observations, dict):
        return _err(400, "'observations' must be an object keyed by source")
    if not MASTER_AGENT_RUNTIME_ARN:
        return _err(503, "Master runtime ARN not configured (run scripts/deploy_agents.py)")

    actor_id = _caller_user_id(event) or "anonymous"
    run_id = f"whatif-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{actor_id[:8]}"
    try:
        resp = agentcore.invoke_agent_runtime(
            agentRuntimeArn=MASTER_AGENT_RUNTIME_ARN,
            payload=json.dumps({
                "scan": True,
                "dry_run": True,
                "scan_run_id": run_id,
                "rule_pack": "v1",
                "observations": observations,
            }).encode("utf-8"),
            contentType="application/json",
            accept="application/json",
        )
        raw = resp["response"].read().decode("utf-8")
        b = json.loads(raw)
        inner = b.get("result", b) if isinstance(b, dict) else b
        if isinstance(inner, str):
            inner = json.loads(inner)
        findings = (inner or {}).get("findings") or []
    except Exception as e:
        logger.exception("dry-run scan invocation failed")
        return _err(502, f"{type(e).__name__}: {e}")

    conflicts = [f for f in findings if not f.get("compliant")]
    totals = {"conflicts": len(conflicts), "compliant": len(findings) - len(conflicts)}
    for sev in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
        totals[sev.lower()] = sum(1 for c in conflicts if (c.get("severity") or "").upper() == sev)

    _audit("WHATIF_RUN", run_id, actor_id, "COMPLETED",
           {"sources_overridden": sorted(observations.keys()), "conflicts": len(conflicts)})
    return _ok({"dry_run": True, "scan_run_id": run_id, "findings": findings, "totals": totals})


# ──────────────────────────── CR workflow (Step 4) ──────────────
# Approver chain rules from the use-case doc:
#   DEV       → auto-approved (no human approvers)
#   STAGING   → Team Lead
#   PRE_PROD  → Manager + Owning Team Lead
#   PROD      → CISO + VP Security + Legal (notification)
def _build_approver_chain(target_env: str, severity: str) -> list[dict]:
    env = (target_env or "").upper()
    sev = (severity or "").upper()
    if env == "DEV":
        return []
    if env == "STAGING":
        return [{"role": "team_lead", "email": "team-lead@meridianinsurance.com", "status": "PENDING",
                 "description": "Team Lead approval required for STAGING"}]
    if env == "PRE_PROD":
        return [
            {"role": "manager",         "email": "manager@meridianinsurance.com",         "status": "PENDING", "description": "Manager approval required for PRE_PROD"},
            {"role": "owning_team_lead","email": "owning-team-lead@meridianinsurance.com","status": "PENDING", "description": "Owning Team Lead approval required for PRE_PROD"},
        ]
    # PROD (default for unknown environments — fail safe)
    chain = [
        {"role": "ciso",        "email": "ciso_diana@meridianinsurance.com",      "status": "PENDING",  "description": "CISO approval required for PROD"},
        {"role": "vp_security", "email": "vp-security@meridianinsurance.com",    "status": "PENDING",  "description": "VP Security approval required for PROD"},
    ]
    if sev in ("CRITICAL", "HIGH"):
        chain.append({"role": "legal", "type": "NOTIFICATION", "email": "legal@meridianinsurance.com",
                      "status": "NOTIFIED", "description": "Legal notified of regulatory impact"})
    return chain


def _audit(action_type: str, resource: str, user: str, status: str, details: dict | None = None, ttl_seconds: int | None = None):
    if not audit_table:
        return
    try:
        item = {
            "event_id": f"{action_type.lower()}-{resource}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action_type": action_type,
            "resource": resource,
            "user": user,
            "status": status,
            "details": json.dumps(details or {}),
        }
        # Opt-in TTL: only the new security-event call sites (CROSS_PERSONA,
        # AUTH_FAILED) pass ttl_seconds; existing callers keep their no-TTL
        # behaviour so the back-compat contract is preserved.
        if ttl_seconds is not None:
            item["ttl"] = int(time.time()) + ttl_seconds
        audit_table.put_item(Item=item)
    except Exception:
        logger.exception("audit write failed (%s)", action_type)


# Note on the split below: _handle_create_action used to be a single
# function. It was split into a pure builder + a persister + a thin REST
# shim so the autonomy layer's create_change_request action can call
# _persist_cr(_build_cr_item(...)) directly without going through HTTP.
# The split is purely structural — the REST response is byte-identical
# to the pre-refactor behaviour.
def _build_cr_item(
    body: dict,
    user_id: str,
    user_email: str | None = None,
    persona: str | None = None,
    claims: dict | None = None,
    ownership: dict | None = None,
    routing: dict | None = None,
) -> dict:
    """Build the DDB CR item from a request body and caller identity.

    Pure with respect to the CR and audit tables — no put_item, no audit
    write. The function denormalizes the linked finding's team ownership
    onto the CR row. Callers may pass `ownership` and `routing` to skip
    the DDB lookup (the autonomy binding does this because its
    `read_finding` step has already loaded the row); when omitted, the
    function looks them up via `_finding_ownership` + `_route_for_team`.

    The `user_email`, `persona`, and `claims` arguments are placeholders
    that the autonomy binding caller (Task 2) can populate from the
    AgentCore execution context. The REST shim does not pass them today
    and the CR shape does not depend on them, so adding them is a no-op
    for the existing /actions caller.
    """
    # Unused for now; reserved for future identity-aware fields on the CR.
    del user_email, persona, claims

    requested_by = (body.get("requested_by") or user_id)
    conflict_id = (body.get("conflict_id") or "").strip()
    target_env = (body.get("target_environment") or "PROD").strip().upper()
    severity = (body.get("severity") or "HIGH").strip().upper()
    action_type = (body.get("action_type") or "SECURITY_FIX").strip()
    target_resource = (body.get("target_resource") or "").strip()
    description = (body.get("description") or "").strip()
    justification = (body.get("justification") or "").strip()

    chain = _build_approver_chain(target_env, severity)
    auto = (target_env == "DEV") or not chain
    status = "AUTO_APPROVED" if auto else "PENDING_APPROVAL"

    # Denormalize the linked finding's team ownership onto the CR so the Action
    # Center can show where the work routes. Server-derived — owner_team is never
    # taken from the request body. The autonomy binding can pass a pre-resolved
    # ownership dict to avoid a second DDB lookup on the same finding.
    if ownership is None:
        ownership = _finding_ownership(conflict_id)
    if routing is None:
        routing = _route_for_team(ownership["owner_team"])

    cr_id = f"CR-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{(conflict_id or 'NEW')[-6:].upper()}"
    now_iso = datetime.now(timezone.utc).isoformat()
    item = {
        "cr_id": cr_id,
        "status": status,
        "conflict_id": conflict_id,
        "linked_conflict_id": conflict_id,
        "action_type": action_type,
        "target_resource": target_resource,
        "target_environment": target_env,
        "severity": severity,
        "description": description or f"Remediate {conflict_id}",
        "requested_by": requested_by,
        "justification": justification,
        "owner_team": ownership["owner_team"],
        "consumer_team": ownership["consumer_team"],
        "platform_team": ownership["platform_team"],
        "routed_team": ownership["owner_team"],
        "tags": ownership["tags"],
        "jira_project_key": routing["project_key"],
        "created_at": now_iso,
        "approvers": chain,
        "total_approvers_needed": sum(1 for a in chain if a.get("type") != "NOTIFICATION"),
        "total_approvals_received": 0,
        "state_transitions": [
            {"ts": now_iso, "actor": user_id, "from_status": "—", "to_status": status, "comment": "Created"},
        ],
    }
    if routing.get("component"):
        item["jira_component"] = routing["component"]
    return item


def _persist_cr(item: dict, user_id: str) -> dict:
    """Persist the built CR item to DDB and write the CR_CREATED audit row.

    Returns the same `item` dict so the REST shim can hand it to `_ok`
    without re-reading from DDB. Raises the original boto3 exception on
    put_item failure so the caller (REST or autonomy binding) decides
    how to surface it.
    """
    if not crs_table:
        raise RuntimeError("CHANGE_REQUESTS_TABLE not configured")
    crs_table.put_item(Item=item)
    _audit(
        "CR_CREATED",
        item.get("target_resource") or item["cr_id"],
        user_id,
        item["status"],
        {
            "cr_id": item["cr_id"],
            "conflict_id": item.get("conflict_id", ""),
            "target_environment": item.get("target_environment", ""),
            "severity": item.get("severity", ""),
        },
    )
    return item


def _handle_create_action(event):
    """REST shim — parse the request, build the CR, persist it.

    Response shape is identical to the pre-refactor handler so the UI
    and any existing curl callers see no change.
    """
    if not crs_table:
        return _err(500, "CHANGE_REQUESTS_TABLE not configured")
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _err(400, "Invalid JSON body")
    user_id = _caller_user_id(event) or "anonymous"
    item = _build_cr_item(body, user_id)
    try:
        _persist_cr(item, user_id)
    except Exception as e:
        logger.exception("CR PutItem failed")
        return _err(502, f"{type(e).__name__}: {e}")
    return _ok(item)


def _handle_action_transition(event, cr_id: str, action: str):
    if not crs_table:
        return _err(500, "CHANGE_REQUESTS_TABLE not configured")
    if not cr_id:
        return _err(400, "Missing cr_id")
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        body = {}
    user_id = _caller_user_id(event) or "anonymous"
    actor_email = (body.get("approver_email") or body.get("actor_email") or user_id)
    actor_role = (body.get("approver_role") or body.get("actor_role") or "")
    comment = (body.get("comment") or body.get("reason") or "")

    try:
        cr = crs_table.get_item(Key={"cr_id": cr_id}).get("Item")
    except Exception as e:
        logger.exception("CR GetItem failed")
        return _err(502, f"{type(e).__name__}: {e}")
    if not cr:
        return _err(404, f"CR {cr_id} not found")

    prior_status = cr.get("status")
    approvers = list(cr.get("approvers") or [])
    transitions = list(cr.get("state_transitions") or [])
    # Optimistic-locking version. Snapshot the prior value here; the
    # ConditionExpression on put_item below requires the row to still match
    # this value at write time. Two concurrent transitions on the same CR
    # would otherwise be last-writer-wins on the full Item — silently
    # dropping one approver flip OR moving the CR to two different statuses
    # depending on read order. Existing rows without `version` are treated
    # as version 0, and the first write through this path adds the field.
    prior_version = int(cr.get("version") or 0)
    cr["version"] = prior_version + 1
    now_iso = datetime.now(timezone.utc).isoformat()

    new_status = prior_status
    audit_action = ""
    extra: dict = {}

    if action == "approve":
        groups = _caller_groups(event)
        is_ciso = "ciso" in groups
        matched = False
        override_applied = False

        # Standard match — approve the caller's own row in the chain.
        for a in approvers:
            if a.get("type") == "NOTIFICATION":
                continue
            if a.get("status") == "APPROVED":
                continue
            if (actor_email and a.get("email") == actor_email) or \
               (actor_role and a.get("role") == actor_role):
                a["status"] = "APPROVED"
                a["approved_at"] = now_iso
                a["comment"] = comment
                matched = True
                break

        # CISO override — one click satisfies every remaining approver.
        # Keeps the chain visible in the UI but unblocks single-user demo flows.
        if is_ciso:
            for a in approvers:
                if a.get("type") == "NOTIFICATION":
                    continue
                if a.get("status") == "PENDING":
                    a["status"] = "APPROVED"
                    a["approved_at"] = now_iso
                    a["comment"] = (comment + " [CISO override]").strip()
                    matched = True
                    override_applied = True

        if not matched:
            return _err(403, "Caller not an approver for this CR")
        cr["approvers"] = approvers
        needed = [a for a in approvers if a.get("type") != "NOTIFICATION"]
        approved = [a for a in needed if a.get("status") == "APPROVED"]
        cr["total_approvals_received"] = len(approved)
        if len(approved) >= len(needed) and needed:
            new_status = "APPROVED"
        audit_action = "CR_APPROVED"
        extra = {"approver_email": actor_email, "approver_role": actor_role,
                 "ciso_override": override_applied}
    elif action == "reject":
        new_status = "REJECTED"
        audit_action = "CR_REJECTED"
        extra = {"actor_email": actor_email, "reason": comment}
    elif action == "escalate":
        new_status = "ESCALATED"
        audit_action = "CR_ESCALATED"
        extra = {"actor_email": actor_email, "reason": comment}
    elif action == "execute":
        # Only APPROVED or AUTO_APPROVED CRs can execute.
        if prior_status not in ("APPROVED", "AUTO_APPROVED"):
            return _err(409, f"CR in status {prior_status} cannot execute")
        log_lines = [
            f"[{now_iso}] Execution started by {actor_email}",
            f"[{now_iso}] Locating target resource: {cr.get('target_resource')}",
            f"[{now_iso}] SIMULATION: remediation action applied",
            f"[{now_iso}] Audit log entry written",
        ]
        cr["execution_log"] = log_lines
        new_status = "COMPLETED"
        audit_action = "CR_EXECUTED"
        extra = {"actor_email": actor_email}
        # Flip linked conflict to RESOLVED if v2 table available.
        link_cid = cr.get("linked_conflict_id") or cr.get("conflict_id")
        tbl = _findings_table()
        if link_cid and tbl:
            try:
                tbl.update_item(
                    Key={"conflict_id": link_cid},
                    UpdateExpression="SET #s = :s",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={":s": "RESOLVED"},
                )
            except Exception:
                logger.exception("conflict RESOLVED update failed for %s", link_cid)
    else:
        return _err(400, f"Unknown action {action}")

    transitions.append({"ts": now_iso, "actor": user_id, "from_status": prior_status,
                        "to_status": new_status, "comment": comment})
    cr["state_transitions"] = transitions
    cr["status"] = new_status

    # Conditional write. The CR must still exist AND its version must match
    # what we read above. Rows without a version field yet (first transition
    # since this patch shipped) are treated as version 0 — `attribute_not_
    # exists(version) OR version = :pv` covers both the migration case and
    # the steady state.
    try:
        crs_table.put_item(
            Item=cr,
            ConditionExpression=(
                "attribute_exists(cr_id) AND "
                "(attribute_not_exists(version) OR version = :pv)"
            ),
            ExpressionAttributeValues={":pv": prior_version},
        )
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return _err(409, f"CR {cr_id} was modified concurrently; please retry")
        logger.exception("CR transition write failed")
        return _err(502, f"{type(e).__name__}: {e}")
    except Exception as e:
        logger.exception("CR transition write failed")
        return _err(502, f"{type(e).__name__}: {e}")

    _audit(audit_action, cr_id, user_id, new_status, {"cr_id": cr_id, **extra})
    return _ok(cr)


# ──────────────────────────── helpers ───────────────────────────
def _path_param(event, name: str, path: str, prefix: str) -> str:
    """Extract a path parameter from the event and ALWAYS URL-decode it.

    AWS REST API docs claim `pathParameters` values are URL-decoded, but in
    practice characters like `:` are sometimes left encoded as `%3A`. The DDB
    rows are keyed on the decoded form (raw colons), so we unquote both the
    pathParameters value and the path-slice fallback to be safe. unquote is
    idempotent on already-decoded strings, so this is harmless when the
    gateway did decode.
    """
    pp = event.get("pathParameters") or {}
    raw = pp.get(name)
    if not raw:
        raw = path[len(prefix):].split("/", 1)[0]
    return unquote(raw)


def _caller_user_id(event) -> str | None:
    claims = _caller_claims(event)
    user_id = claims.get("sub") or claims.get("cognito:username")
    if user_id:
        return user_id
    return event.get("user_id") or event.get("requestContext", {}).get("user_id")


def _caller_groups(event) -> list[str]:
    """Return the Cognito `cognito:groups` claim as a list of strings.

    The claim is a list when decoded from a JWT but API Gateway authorizers
    sometimes flatten it to a comma-separated string. Tolerate both.
    """
    claims = _caller_claims(event)
    raw = claims.get("cognito:groups") or claims.get("groups") or []
    if isinstance(raw, str):
        return [g.strip() for g in raw.split(",") if g.strip()]
    if isinstance(raw, list):
        return [str(g) for g in raw]
    return []


def _caller_claims(event) -> dict[str, Any]:
    # 1. API Gateway with Cognito authorizer — claims come pre-validated.
    claims = (event.get("requestContext", {})
              .get("authorizer", {})
              .get("claims") or {})
    if claims:
        return claims
    # 2. Lambda Function URL (AuthType=NONE) — the UI still sends the Cognito
    #    IdToken in Authorization: Bearer <jwt>. Decode the payload (no
    #    signature verification — fine for the demo since Cognito issued it
    #    and worst case a tampered claim only impacts the caller's own data).
    headers = event.get("headers") or {}
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    if auth.startswith("Bearer "):
        try:
            payload_b64 = auth[7:].split(".")[1]
            payload_b64 += "=" * (-len(payload_b64) % 4)  # pad to multiple of 4
            payload = json.loads(base64.urlsafe_b64decode(payload_b64))
            return payload if isinstance(payload, dict) else {}
        except Exception as e:
            logger.warning("Failed to decode JWT from Authorization header: %s", e)
    return {}


def _session_summary(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": item.get("session_id"),
        "title": item.get("title"),
        "created_at": item.get("created_at"),
        "last_message_at": item.get("last_message_at"),
        "message_count": _to_int(item.get("message_count")),
        # Legacy rows have no chat_type; treat them as 'analyst' (the only chat
        # that persisted sessions before MCP Chat shipped).
        "chat_type": item.get("chat_type") or "analyst",
    }


def _to_int(v):
    if v is None:
        return None
    if isinstance(v, Decimal):
        return int(v)
    return int(v)


# ──────────────────────────── responses ─────────────────────────
def _ok(body):
    return {
        "statusCode": 200,
        "headers": _cors_headers(),
        "body": json.dumps(body, default=_json_default),
    }


def _err(status, message):
    return {
        "statusCode": status,
        "headers": _cors_headers(),
        "body": json.dumps({"error": message}),
    }


def _json_default(o):
    if isinstance(o, Decimal):
        return int(o) if o == o.to_integral_value() else float(o)
    if isinstance(o, datetime):
        return o.isoformat()
    raise TypeError(f"not serializable: {type(o)}")


def _cors_headers():
    headers = {"Content-Type": "application/json"}
    if _emit_cors_headers:
        headers["Access-Control-Allow-Origin"] = "*"
        headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization"
        headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    return headers
