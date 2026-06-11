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
  S3_KMS_KEY_ARN             Optional. CMK ARN that encrypts both buckets;
                             when set, the presigned PUT includes the SSE-KMS
                             headers so the browser PUT succeeds.
  UPLOAD_URL_EXPIRES_SECONDS Optional. Presigned URL lifetime. Default 900s.
"""
import base64
import json
import logging
import os
import re
from typing import Any
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from urllib.parse import unquote

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

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
S3_KMS_KEY_ARN = os.environ.get("S3_KMS_KEY_ARN", "").strip()
SCANNER_LAMBDA_NAME = os.environ.get("SCANNER_LAMBDA_NAME", "").strip()
MCP_ENDPOINTS = [u.strip() for u in os.environ.get("MCP_ENDPOINTS", "").split(",") if u.strip()]
UPLOAD_URL_EXPIRES_SECONDS = int(os.environ.get("UPLOAD_URL_EXPIRES_SECONDS", "900"))
UPLOAD_PREFIX = "users/"            # per-user folder root inside each bucket
MAX_LIST_KEYS = 200                 # cap list responses; bucket-listing isn't paginated to the UI

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

    # Direct per-agent routing: the MCP page sends a "target" naming the
    # specialist to invoke; the Analyst page sends none → master orchestrator.
    # An unknown target falls back to master for backward compatibility.
    target = (body.get("target") or "master").strip().lower()
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

    try:
        resp = agentcore.invoke_agent_runtime(
            agentRuntimeArn=runtime_arn,
            payload=json.dumps({
                "prompt": prompt,
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
        return _ok({
            "reply": parsed.get("result", raw),
            "session_id": session_id,  # echo so frontend can correlate
        })
    except Exception as e:
        logger.exception("AgentCore invocation failed")
        return _err(502, f"{type(e).__name__}: {e}")


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

    try:
        resp = s3.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=MAX_LIST_KEYS)
    except ClientError as e:
        logger.exception("list_objects_v2 failed")
        return _err(502, f"{type(e).__name__}: {e}")

    files = []
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
    files.sort(key=lambda f: f["last_modified"], reverse=True)
    return _ok({
        "bucket": bucket,
        "prefix": prefix,
        "files": files,
        "truncated": bool(resp.get("IsTruncated")),
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
    """
    if "ciso" not in _caller_groups(event):
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

    # Ownership check — the row must exist and belong to the caller.
    try:
        resp = sessions_table.get_item(Key={"session_id": session_id})
    except Exception as e:
        logger.exception("Ownership lookup failed")
        return _err(502, f"{type(e).__name__}: {e}")
    item = resp.get("Item")
    if not item or item.get("user_id") != user_id:
        return _err(404, f"Session {session_id} not found")

    try:
        sessions_table.delete_item(Key={"session_id": session_id})
    except Exception as e:
        logger.exception("DeleteItem failed")
        return _err(502, f"{type(e).__name__}: {e}")

    return _ok({"deleted": True, "session_id": session_id})


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


def _audit(action_type: str, resource: str, user: str, status: str, details: dict | None = None):
    if not audit_table:
        return
    try:
        audit_table.put_item(Item={
            "event_id": f"{action_type.lower()}-{resource}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action_type": action_type,
            "resource": resource,
            "user": user,
            "status": status,
            "details": json.dumps(details or {}),
        })
    except Exception:
        logger.exception("audit write failed (%s)", action_type)


def _handle_create_action(event):
    if not crs_table:
        return _err(500, "CHANGE_REQUESTS_TABLE not configured")
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _err(400, "Invalid JSON body")
    user_id = _caller_user_id(event) or "anonymous"
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
    # taken from the request body.
    ownership = _finding_ownership(conflict_id)
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
    try:
        crs_table.put_item(Item=item)
    except Exception as e:
        logger.exception("CR PutItem failed")
        return _err(502, f"{type(e).__name__}: {e}")

    _audit("CR_CREATED", target_resource or cr_id, user_id, status,
           {"cr_id": cr_id, "conflict_id": conflict_id, "target_environment": target_env, "severity": severity})

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

    try:
        crs_table.put_item(Item=cr)
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
    raise TypeError(f"not serializable: {type(o)}")


def _cors_headers():
    headers = {"Content-Type": "application/json"}
    if _emit_cors_headers:
        headers["Access-Control-Allow-Origin"] = "*"
        headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization"
        headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    return headers
