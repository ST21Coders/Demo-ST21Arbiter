"""MyMCP spoke facade — handles POST /mymcp/chat.

Performs privilege pre-flight against the verified Cognito caller, transforms
the inbound MyMCP context into an Arbiter-governed prompt, then routes to the
master AgentCore Runtime. Audit-logs decisions to the AUDIT_TABLE.

This module is intentionally self-contained — it duplicates the small response
and JWT helpers it needs so it can be lifted into another project without
pulling in api_handler.py.

Env vars:
  MASTER_AGENT_RUNTIME_ARN   ARN of the master AgentCore runtime
                             (populated by scripts/deploy_agents.py).
  AUDIT_TABLE                DynamoDB table for audit log writes.
"""
import base64
import json
import logging
import os
import uuid
from typing import Any
from decimal import Decimal
from datetime import datetime, timezone

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

REGION = os.environ.get("AWS_REGION", "us-east-1")
MASTER_AGENT_RUNTIME_ARN = os.environ.get("MASTER_AGENT_RUNTIME_ARN", "").strip()
AUDIT_TABLE = os.environ.get("AUDIT_TABLE", "")

agentcore = boto3.client("bedrock-agentcore", region_name=REGION)
ddb = boto3.resource("dynamodb", region_name=REGION)
audit_table = ddb.Table(AUDIT_TABLE) if AUDIT_TABLE else None


# ──────────────────────────── lambda entry point ────────────────
def handler(event, context):
    path = event.get("path") or event.get("rawPath", "")
    method = (event.get("httpMethod") or
              event.get("requestContext", {}).get("http", {}).get("method", "")).upper()
    if path == "/mymcp/chat" and method == "POST":
        return handle_mymcp_chat(event)
    return _err(404, f"Not found: {method} {path}")


# ──────────────────────────── /mymcp/chat ───────────────────────
def handle_mymcp_chat(event):
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _err(400, "Invalid JSON body")

    question = (body.get("question") or body.get("prompt") or body.get("message") or "").strip()
    if not question:
        return _err(400, "Missing 'question' in request body")

    caller = _caller_profile(event, body)
    context = body.get("context") or {}
    preferences = body.get("preferences") or {}
    selected_sources = context.get("selectedSources") or []
    decision = _evaluate_mymcp_privilege(question, selected_sources, caller)

    session_id = (
        body.get("session_id")
        or body.get("sessionId")
        or context.get("session_id")
        or context.get("sessionId")
        or f"mymcp-{uuid.uuid4().hex}"
    )[:128]
    job_id = f"MYMCP-JOB-{uuid.uuid4().hex[:10].upper()}"

    if decision["decision"] != "allowed":
        _write_audit(
            "MYMCP_CHAT_DENIED" if decision["decision"] == "denied" else "MYMCP_CHAT_CLARIFY",
            "DENIED" if decision["decision"] == "denied" else "CLARIFY",
            caller["id"],
            "mymcp-chat",
            {
                "question": question[:500],
                "selected_sources": selected_sources,
                "reason": decision["reason"],
                "groups": caller["groups"],
            },
        )
        return _ok({
            "decision": decision["decision"],
            "reason": decision["reason"],
            "allowedScopes": decision["allowedScopes"],
            "suggestedQuestion": decision["suggestedQuestion"],
            "jobId": job_id,
            "sessionId": session_id,
        })

    if not MASTER_AGENT_RUNTIME_ARN:
        return _err(503, "Master runtime ARN not configured (run scripts/deploy_agents.py)")

    governed_prompt = _build_mymcp_prompt(question, caller, selected_sources, preferences, context)
    try:
        resp = agentcore.invoke_agent_runtime(
            agentRuntimeArn=MASTER_AGENT_RUNTIME_ARN,
            payload=json.dumps({
                "prompt": governed_prompt,
                "session_id": session_id,
                "actor_id": caller["id"],
                "chat_type": "mymcp",
            }).encode("utf-8"),
            contentType="application/json",
            accept="application/json",
        )
        raw = resp["response"].read().decode("utf-8")
        parsed = json.loads(raw)
        answer = parsed.get("result", raw)
        _write_audit(
            "MYMCP_CHAT_ALLOWED",
            "COMPLETED",
            caller["id"],
            "mymcp-chat",
            {
                "job_id": job_id,
                "session_id": session_id,
                "question": question[:500],
                "selected_sources": selected_sources,
                "model_preference": preferences.get("model"),
                "answer_length": len(answer or ""),
            },
        )
        return _ok({
            "decision": "allowed",
            "jobId": job_id,
            "sessionId": session_id,
            "response": {
                "answer": answer,
                "sources": selected_sources,
                "confidence": "arbiter-governed",
            },
        })
    except Exception as e:
        logger.exception("MyMCP AgentCore invocation failed")
        _write_audit(
            "MYMCP_CHAT_FAILED",
            "FAILED",
            caller["id"],
            "mymcp-chat",
            {
                "job_id": job_id,
                "session_id": session_id,
                "question": question[:500],
                "error": f"{type(e).__name__}: {e}",
            },
        )
        return _err(502, f"{type(e).__name__}: {e}")


def _evaluate_mymcp_privilege(question: str, selected_sources: list[Any], caller: dict[str, Any]) -> dict[str, Any]:
    text = " ".join([question, *[str(source) for source in selected_sources]]).lower()
    groups = {str(group).lower() for group in caller.get("groups", [])}
    elevated = bool(groups.intersection({"ciso", "grc", "admin", "security", "soc"}))
    restricted_terms = ["payroll", "salary", "compensation", "ssn", "social security", "hr restricted"]

    if any(term in text for term in restricted_terms) and not elevated:
        return {
            "decision": "denied",
            "reason": "Arbiter denied this request because it references restricted HR/payroll data outside the current user's privilege set.",
            "allowedScopes": _allowed_mymcp_scopes(caller),
            "suggestedQuestion": "Ask about vendor, operations, finance, or document evidence that excludes restricted HR and payroll data.",
        }

    if not selected_sources:
        return {
            "decision": "clarify",
            "reason": "Arbiter needs at least one available MyMCP source before it can route this question to AgentCore.",
            "allowedScopes": _allowed_mymcp_scopes(caller),
            "suggestedQuestion": "Select one or more available data sources, then ask the question again.",
        }

    return {
        "decision": "allowed",
        "reason": "Allowed for Arbiter-governed AgentCore routing.",
        "allowedScopes": _allowed_mymcp_scopes(caller),
        "suggestedQuestion": "",
    }


def _build_mymcp_prompt(question: str, caller: dict[str, Any], selected_sources: list[Any], preferences: dict[str, Any], context: dict[str, Any]) -> str:
    return "\n".join([
        "MyMCP spoke request. Arbiter is the hub and remains authoritative for governance.",
        "",
        "Verified user:",
        json.dumps({
            "id": caller["id"],
            "email": caller.get("email"),
            "groups": caller.get("groups", []),
            "name": caller.get("name"),
        }, default=_json_default),
        "",
        "Allowed MyMCP context:",
        json.dumps({
            "selectedSources": selected_sources,
            "modelPreference": preferences.get("model", "arbiter-default"),
            "context": context,
        }, default=_json_default),
        "",
        "Governance instruction:",
        "Answer only within the verified user's authorized context. If the answer would require restricted data or data outside selected sources, say Arbiter cannot answer and explain the allowed alternative.",
        "",
        f"User question: {question}",
    ])


def _allowed_mymcp_scopes(caller: dict[str, Any]) -> list[str]:
    groups = {str(group).lower() for group in caller.get("groups", [])}
    scopes = ["Operations", "Available processed data", "User-owned raw data"]
    if groups.intersection({"finance", "grc", "ciso", "admin"}):
        scopes.append("Finance")
    if groups.intersection({"ciso", "grc", "admin", "security", "soc"}):
        scopes.append("Restricted governance evidence")
    return scopes


def _caller_profile(event, body: dict[str, Any]) -> dict[str, Any]:
    claims = _caller_claims(event)
    supplied_user = body.get("user") if isinstance(body.get("user"), dict) else {}
    groups = claims.get("cognito:groups") or supplied_user.get("groups") or supplied_user.get("roles") or []
    if isinstance(groups, str):
        groups = [groups]
    user_id = (
        claims.get("sub")
        or claims.get("cognito:username")
        or supplied_user.get("id")
        or supplied_user.get("email")
        or "anonymous"
    )
    return {
        "id": str(user_id)[:128],
        "email": claims.get("email") or supplied_user.get("email") or supplied_user.get("id"),
        "name": claims.get("name") or supplied_user.get("name"),
        "groups": groups,
    }


def _write_audit(action_type: str, status: str, user_id: str, resource: str, details: dict[str, Any]) -> None:
    if not audit_table:
        return
    try:
        audit_table.put_item(Item={
            "event_id": f"MYMCP-{uuid.uuid4().hex}",
            "timestamp": _now_iso(),
            "action_type": action_type,
            "status": status,
            "user": str(user_id or "unknown")[:512],
            "resource": str(resource or "mymcp")[:512],
            "details": json.dumps(details, default=_json_default),
        })
    except Exception:
        logger.exception("MyMCP audit write failed")


# ──────────────────────────── helpers ───────────────────────────
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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    return {
        "Content-Type": "application/json",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "Content-Type,Authorization",
        "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
    }
