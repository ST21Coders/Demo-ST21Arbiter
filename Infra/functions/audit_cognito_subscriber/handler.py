"""ARBITER audit subscriber — writes AUTH_FAILED rows from CloudTrail.

Triggered by an EventBridge rule that matches Cognito sign-in failures on the
dev user pool. Writes one row per event to <env>-<project>-audit-log using the
same row shape the api_handler's _audit(...) writer uses for in-process events.

Best-effort: any DDB error (throttle, KMS hiccup, etc.) is logged at WARNING and
the handler returns success anyway so EventBridge does not retry. EventBridge
retries on a 5xx, and we do not want a transient table issue to fan out into
duplicate audit rows.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# 90 days, matches the existing token-usage TTL convention and the spec.
_TTL_SECONDS = 7_776_000

# Read at module load so cold-start cost is paid once. Fail loudly if unset —
# that is a deploy-time misconfig (env var missing on the function), not a
# runtime issue, and we'd rather see it in CloudFormation than silently drop
# events.
_TABLE_NAME = os.environ.get("AUDIT_LOG_TABLE_NAME")
if not _TABLE_NAME:
    raise RuntimeError("AUDIT_LOG_TABLE_NAME env var is not set; refusing to import")

_ddb = boto3.resource("dynamodb")
_table = _ddb.Table(_TABLE_NAME)


def _extract_actor(detail: dict) -> str:
    """Best-available username for a failed Cognito sign-in.

    CloudTrail often redacts the attempted username on failed auth. Try the
    documented fallback chain and end at the literal ``unknown``.

    Defensive against CloudTrail's redaction shapes: any field expected to be
    a dict may instead be a literal string like
    ``"HIDDEN_DUE_TO_SECURITY_REASONS"``. We type-check before calling
    ``.get()`` so a redacted field doesn't crash the Lambda. Original bug
    surfaced 2026-06-15: real failed InitiateAuth events carry
    ``authParameters = "HIDDEN_DUE_TO_SECURITY_REASONS"`` (string) and the
    Lambda was crashing on every retry.
    """
    add = detail.get("additionalEventData")
    if isinstance(add, dict):
        user_identifier = add.get("userIdentifier")
        if user_identifier:
            return str(user_identifier)

    req = detail.get("requestParameters")
    if isinstance(req, dict):
        username = req.get("username")
        if username:
            return str(username)
        # Some CloudTrail events nest the attempted username here. Cognito
        # redacts this whole sub-object on failed auth, so guard the type.
        auth_params = req.get("authParameters")
        if isinstance(auth_params, dict):
            username = auth_params.get("USERNAME")
            if username:
                return str(username)

    return "unknown"


def handler(event, _context):
    """EventBridge → audit-log writer. Always returns 200.

    Extraction is wrapped in a broad try/except so a CloudTrail event with an
    unexpected shape (redacted field, missing key, etc.) never crashes the
    Lambda. EventBridge retries a 5xx, and we never want a malformed event to
    fan out into N retries.
    """
    detail = (event or {}).get("detail") or {}

    try:
        actor_id = _extract_actor(detail)
        source_ip = detail.get("sourceIPAddress") or "unknown"
        error_code = detail.get("errorCode") or "unknown"
        user_agent = detail.get("userAgent") or "unknown"
        event_id_cloudtrail = detail.get("eventID") or ""
        event_time = detail.get("eventTime") or datetime.now(timezone.utc).isoformat()
        event_name = detail.get("eventName") or ""
    except Exception:
        logger.warning(
            "AUTH_FAILED extraction failed for event %s",
            (event or {}).get("id"),
            exc_info=True,
        )
        return {
            "statusCode": 200,
            "body": json.dumps({"written": False, "action_type": "AUTH_FAILED",
                                "reason": "extraction_failed"}),
        }

    # The microsecond suffix keeps event_ids unique even when CloudTrail replays
    # the same eventID (extremely rare, but the DDB primary key is (event_id,
    # timestamp) so a collision silently overwrites). Mirrors the existing
    # _audit(...) writer's format.
    micro_suffix = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    item = {
        "event_id": f"AUTH_FAILED-{event_id_cloudtrail}-{micro_suffix}",
        "timestamp": event_time,
        "action_type": "AUTH_FAILED",
        "resource": actor_id,
        "user": actor_id,
        "status": error_code,
        "details": json.dumps(
            {
                "source_ip": source_ip,
                "error_code": error_code,
                "user_agent": user_agent,
                "event_id_cloudtrail": event_id_cloudtrail,
                "event_time": event_time,
                "event_name": event_name,
            }
        ),
        "ttl": int(time.time()) + _TTL_SECONDS,
    }

    written = False
    try:
        _table.put_item(Item=item)
        written = True
    except Exception:
        # Best-effort: log and swallow so EventBridge does not retry. The
        # CloudWatch log line is the operator's signal that something is wrong.
        logger.warning(
            "AUTH_FAILED audit write failed (actor=%s, cloudtrail_event=%s)",
            actor_id,
            event_id_cloudtrail,
            exc_info=True,
        )

    return {
        "statusCode": 200,
        "body": json.dumps({"written": written, "action_type": "AUTH_FAILED"}),
    }
