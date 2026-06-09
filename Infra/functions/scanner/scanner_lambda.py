"""ARBITER scanner Lambda.

Invoked by:
  - EventBridge cron(0 6 * * ? *) UTC (02:00 PST daily)
  - api_handler `POST /scan` via lambda:Invoke (InvocationType=Event)

Flow:
  1. Write a scan-runs row (status=RUNNING).
  2. Invoke the Master AgentCore runtime with {"scan": true, "scan_run_id": ..., "rule_pack": "v1"}.
  3. Receive {"findings":[...]} — 12 conflicts + 14 compliant.
  4. BatchWriteItem to conflicts-v2 (idempotent — same conflict_id rewrites).
  5. Update scan-runs (status=COMPLETED, totals=...).
  6. Write audit-log SCAN_STARTED / SCAN_COMPLETED rows.

Env vars (set by 11-scanner.yaml):
  MASTER_AGENT_RUNTIME_ARN
  CONFLICTS_TABLE_V2
  SCAN_RUNS_TABLE
  AUDIT_TABLE
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.types import TypeDeserializer

from enrichment import load_ownership_rules, enrich_findings

logger = logging.getLogger()
logger.setLevel(logging.INFO)

REGION = os.environ.get("AWS_REGION", "us-east-1")
MASTER_AGENT_RUNTIME_ARN = os.environ.get("MASTER_AGENT_RUNTIME_ARN", "").strip()
CONFLICTS_TABLE_V2 = os.environ.get("CONFLICTS_TABLE_V2", "").strip()
SCAN_RUNS_TABLE = os.environ.get("SCAN_RUNS_TABLE", "").strip()
AUDIT_TABLE = os.environ.get("AUDIT_TABLE", "").strip()
OWNERSHIP_RULES_TABLE = os.environ.get("OWNERSHIP_RULES_TABLE", "").strip()
RULE_PACK_VERSION = os.environ.get("RULE_PACK_VERSION", "v1")

agentcore = boto3.client("bedrock-agentcore", region_name=REGION)
ddb = boto3.resource("dynamodb", region_name=REGION)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_decimal(v):
    if isinstance(v, float):
        return Decimal(str(v))
    if isinstance(v, list):
        return [_to_decimal(x) for x in v]
    if isinstance(v, dict):
        return {k: _to_decimal(x) for k, x in v.items()}
    return v


def _aggregate_totals(findings: list[dict]) -> dict:
    conflicts = [f for f in findings if not f.get("compliant")]
    compliant = [f for f in findings if f.get("compliant")]
    totals = {"conflicts": len(conflicts), "compliant": len(compliant),
              "critical": 0, "high": 0, "medium": 0, "low": 0}
    for c in conflicts:
        sev = (c.get("severity") or "").upper()
        if sev == "CRITICAL":
            totals["critical"] += 1
        elif sev == "HIGH":
            totals["high"] += 1
        elif sev == "MEDIUM":
            totals["medium"] += 1
        elif sev == "LOW":
            totals["low"] += 1
    return totals


def handler(event, context):
    triggered_by = (event or {}).get("triggered_by") or "schedule"
    scan_run_id = (event or {}).get("scan_run_id") or f"scan-{_now_iso()}-{uuid.uuid4().hex[:8]}"
    started_at = _now_iso()

    if not MASTER_AGENT_RUNTIME_ARN:
        logger.error("MASTER_AGENT_RUNTIME_ARN unset — cannot run scan")
        return {"status": "ERROR", "error": "MASTER_AGENT_RUNTIME_ARN unset"}
    if not CONFLICTS_TABLE_V2 or not SCAN_RUNS_TABLE:
        logger.error("CONFLICTS_TABLE_V2 / SCAN_RUNS_TABLE unset")
        return {"status": "ERROR", "error": "tables not configured"}

    scan_runs = ddb.Table(SCAN_RUNS_TABLE)
    conflicts = ddb.Table(CONFLICTS_TABLE_V2)
    audit = ddb.Table(AUDIT_TABLE) if AUDIT_TABLE else None

    # 1. Open scan-runs row.
    try:
        scan_runs.put_item(Item={
            "scan_run_id": scan_run_id, "started_at": started_at,
            "status": "RUNNING", "triggered_by": triggered_by,
            "rule_pack_version": RULE_PACK_VERSION,
        })
    except Exception:
        logger.exception("scan-runs RUNNING write failed")

    # SCAN_STARTED audit.
    if audit:
        try:
            audit.put_item(Item={
                "event_id": f"scan-start-{scan_run_id}",
                "timestamp": started_at,
                "action_type": "SCAN_STARTED",
                "resource": scan_run_id,
                "user": "system",
                "status": "RUNNING",
                "details": json.dumps({"triggered_by": triggered_by}),
            })
        except Exception:
            logger.exception("audit SCAN_STARTED write failed")

    # 2. Invoke Master in scan mode.
    try:
        payload = {"scan": True, "scan_run_id": scan_run_id, "rule_pack": RULE_PACK_VERSION}
        resp = agentcore.invoke_agent_runtime(
            agentRuntimeArn=MASTER_AGENT_RUNTIME_ARN,
            payload=json.dumps(payload).encode("utf-8"),
            contentType="application/json",
            accept="application/json",
        )
        raw = resp["response"].read().decode("utf-8")
        body = json.loads(raw)
        # AgentCore wraps the entrypoint return under {"result": ...} and may
        # stringify the inner dict. Unwrap and re-parse if needed.
        inner = body.get("result", body) if isinstance(body, dict) else body
        if isinstance(inner, str):
            try:
                inner = json.loads(inner)
            except json.JSONDecodeError:
                inner = {}
        findings = (inner or {}).get("findings") or []
    except Exception as e:
        logger.exception("Master scan invocation failed")
        finished_at = _now_iso()
        try:
            scan_runs.update_item(
                Key={"scan_run_id": scan_run_id, "started_at": started_at},
                UpdateExpression="SET #s = :s, finished_at = :f, #err = :err",
                ExpressionAttributeNames={"#s": "status", "#err": "error"},
                ExpressionAttributeValues={":s": "FAILED", ":f": finished_at, ":err": f"{type(e).__name__}: {e}"},
            )
        except Exception:
            logger.exception("scan-runs FAILED update failed")
        if audit:
            try:
                audit.put_item(Item={
                    "event_id": f"scan-fail-{scan_run_id}",
                    "timestamp": finished_at,
                    "action_type": "SCAN_FAILED",
                    "resource": scan_run_id,
                    "user": "system",
                    "status": "FAILED",
                    "details": json.dumps({"error": str(e)}),
                })
            except Exception:
                logger.exception("audit SCAN_FAILED write failed")
        return {"status": "FAILED", "error": str(e), "scan_run_id": scan_run_id}

    # 2b. Enrich findings with team/tag ownership (deterministic rules table).
    # The scanner role has NO project wildcard, so a missing IAM grant on the
    # ownership-rules table surfaces here as an AccessDenied — caught + logged
    # loudly rather than silently writing zero-ownership rows.
    rules = []
    if OWNERSHIP_RULES_TABLE:
        try:
            rules = load_ownership_rules(ddb.Table(OWNERSHIP_RULES_TABLE))
        except Exception:
            logger.exception(
                "ownership-rules load FAILED — findings will lack team ownership. "
                "Verify the scanner role has dynamodb:Scan/GetItem on %s",
                OWNERSHIP_RULES_TABLE,
            )
    else:
        logger.warning("OWNERSHIP_RULES_TABLE unset — skipping ownership enrichment")
    enrich_findings(findings, rules)

    # Sanity guard: a "green" scan that wrote nothing — or findings that loaded
    # rules but matched none — is almost always a silent failure (empty
    # observations, shape drift, or IAM denial). Log at ERROR so it is never
    # masked by a COMPLETED scan-run.
    enriched = sum(1 for f in findings if f.get("owner_team"))
    if not findings:
        logger.error("SANITY: scan produced ZERO findings — check observation shapes")
    elif rules and enriched == 0:
        logger.error(
            "SANITY: %d findings but NONE matched an ownership rule — check rule predicates",
            len(findings),
        )
    logger.info("Ownership enrichment: %d/%d findings tagged with owner_team", enriched, len(findings))

    # 3. BatchWrite findings to conflicts-v2.
    detected_at = _now_iso()
    written = 0
    try:
        with conflicts.batch_writer() as bw:
            for f in findings:
                row = dict(f)
                row["scan_run_id"] = scan_run_id
                row.setdefault("detected_at", detected_at)
                # DDB rejects float; coerce to Decimal recursively.
                row = _to_decimal(row)
                bw.put_item(Item=row)
                written += 1
    except Exception:
        logger.exception("conflicts BatchWrite partial failure (continuing)")

    totals = _aggregate_totals(findings)
    finished_at = _now_iso()

    # 4. Mark scan-runs COMPLETED.
    try:
        scan_runs.update_item(
            Key={"scan_run_id": scan_run_id, "started_at": started_at},
            UpdateExpression="SET #s = :s, finished_at = :f, totals = :t, written = :w",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": "COMPLETED", ":f": finished_at, ":t": totals, ":w": written,
            },
        )
    except Exception:
        logger.exception("scan-runs COMPLETED update failed")

    # 5. SCAN_COMPLETED audit.
    if audit:
        try:
            audit.put_item(Item={
                "event_id": f"scan-done-{scan_run_id}",
                "timestamp": finished_at,
                "action_type": "SCAN_COMPLETED",
                "resource": scan_run_id,
                "user": "system",
                "status": "COMPLETED",
                "details": json.dumps({"totals": totals, "written": written, "triggered_by": triggered_by}),
            })
        except Exception:
            logger.exception("audit SCAN_COMPLETED write failed")

    logger.info("Scan complete: scan_run_id=%s totals=%s written=%d", scan_run_id, totals, written)
    return {"status": "COMPLETED", "scan_run_id": scan_run_id, "totals": totals, "written": written}
