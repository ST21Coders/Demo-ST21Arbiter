"""ARBITER Master Orchestrator — runs on Bedrock AgentCore Runtime.

The master agent:
  1. Receives an analyst query
  2. Fans out to three specialist agents (sharepoint / awsconfig / zscaler),
     each running as its own AgentCore Runtime
  3. Aggregates their findings and asks Claude to produce a final conflict
     analysis + remediation recommendation
  4. Reads/writes AgentCore Memory (if MEMORY_ID is set) so follow-up turns
     within the same session see prior conversation summaries.

Environment variables (set via AgentCore Runtime configuration):
  SHAREPOINT_RUNTIME_ARN   ARN of the sharepoint_specialist runtime
  AWSCONFIG_RUNTIME_ARN    ARN of the awsconfig_specialist runtime
  ZSCALER_RUNTIME_ARN      ARN of the zscaler_specialist runtime
  MODEL_ID                 Bedrock model (default: Nova 2 Lite cross-region inference profile)
  GUARDRAIL_ID             Bedrock guardrail (optional)
  GUARDRAIL_VERSION        Guardrail version (default: DRAFT)
  MEMORY_ID                AgentCore Memory resource ID (empty = disabled)
  SESSIONS_TABLE           DynamoDB table indexing conversations by session_id
                           (the agent maintains the metadata; messages live in
                           AgentCore Memory).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import boto3
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent
from strands.models.bedrock import BedrockModel
from strands.tools import tool

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("master_orchestrator")

REGION = os.environ.get("AWS_REGION", "us-east-1")
MODEL_ID = os.environ.get("MODEL_ID", "us.amazon.nova-2-lite-v1:0")
GUARDRAIL_ID = os.environ.get("GUARDRAIL_ID")
GUARDRAIL_VERSION = os.environ.get("GUARDRAIL_VERSION", "DRAFT")
MEMORY_ID = os.environ.get("MEMORY_ID", "").strip()
SESSIONS_TABLE = os.environ.get("SESSIONS_TABLE", "").strip()

SHAREPOINT_RUNTIME_ARN = os.environ.get("SHAREPOINT_RUNTIME_ARN", "")
AWSCONFIG_RUNTIME_ARN = os.environ.get("AWSCONFIG_RUNTIME_ARN", "")
ZSCALER_RUNTIME_ARN = os.environ.get("ZSCALER_RUNTIME_ARN", "")

_missing = [
    name for name, val in [
        ("SHAREPOINT_RUNTIME_ARN", SHAREPOINT_RUNTIME_ARN),
        ("AWSCONFIG_RUNTIME_ARN", AWSCONFIG_RUNTIME_ARN),
        ("ZSCALER_RUNTIME_ARN", ZSCALER_RUNTIME_ARN),
    ] if not val
]
if _missing:
    log.warning(
        "Specialist runtime ARN env var(s) not set: %s — corresponding tools will return placeholder text.",
        ", ".join(_missing),
    )
if not MEMORY_ID:
    log.warning("MEMORY_ID not set — long-term memory disabled (per-invocation only).")

SYSTEM_PROMPT = """You are ARBITER, a compliance analysis assistant. You
inspect IT policy conflicts across SharePoint policy documents, AWS Config
rule findings, and Zscaler ZIA URL allowlists, and report results to
enterprise security analysts.

WORKFLOW
1. Call the relevant specialist tools (sharepoint_lookup, awsconfig_lookup,
   zscaler_lookup) to gather evidence. Run them in parallel when the query
   spans multiple domains. Skip a tool if the query clearly does not touch
   that source.
2. When the user asks about LIVE findings, the latest scan, or current
   compliance posture (rather than what a policy *says*), prefer the
   conflicts/scan-history tools (query_conflicts, query_scan_runs) so the
   answer is grounded in actual scan results from the conflicts-v2 DDB
   table — not in policy text alone. If those tools are not yet registered
   in the current build, fall back to specialist lookups and state plainly
   that live scan data is unavailable from this chat surface.
3. Identify conflicts — points where two or more sources disagree on a
   policy. Cite the exact source (filename, rule name, allowlist entry,
   ARBITER-UC id when one applies).
4. Recommend a remediation that names the specific source to change.
5. If a specialist returns no data, state that explicitly. Never fabricate.
6. Never propose actions that expose secrets, delete production
   infrastructure, or escalate privileges — escalate those to a human.

OUTPUT RULES (strict — apply to every response)
- Write in a direct, professional tone suitable for a security analyst's
  ticket or incident report. No conversational filler ("Certainly",
  "I'd be happy to", "Great question", "Let me know if…", "I hope this
  helps", "As an AI…").
- No emojis, decorative symbols, or section dividers built from repeated
  characters. No bold/italic for emphasis on single words.
- Do not use markdown headers (no `#`, `##`, `###`, etc.) anywhere in the
  response. Section headers below are written as plain text on their own
  line, followed by a blank line and then the section body. Never prefix
  them with `#` or wrap them in `**…**`.
- Use short paragraphs or terse bullets. Prefer bullets when listing more
  than two items. Do not pad bullets with adjectives or restate the bullet
  topic.
- Use these section headers exactly when the corresponding content exists,
  in this order, and omit any section that has nothing to report:
      Summary
      Findings
      Conflicts
      Recommendation
      Sources
  Summary is one or two sentences. Sources is a flat list of the filename /
  rule name / allowlist entry citations referenced above — one per line, no
  prose.
- Quote source text only when the exact wording matters; otherwise
  paraphrase tightly. Do not restate the user's question.
- If the answer is a single fact, return just that fact plus a one-line
  citation. Do not force the full template onto trivial answers.
- Preserve every substantive finding, conflict, and citation the
  specialists returned. Concise does not mean omitting evidence — it means
  removing filler, hedging, and decoration.
"""

app = BedrockAgentCoreApp()
runtime_client = boto3.client("bedrock-agentcore", region_name=REGION)
ddb_client = boto3.client("dynamodb", region_name=REGION) if SESSIONS_TABLE else None


# ──────────────────────────── Specialist invocation ───────────────
def _invoke_runtime(runtime_arn: str, prompt: str) -> str:
    """Call a specialist AgentCore Runtime synchronously and return text."""
    if not runtime_arn:
        return "(specialist runtime not configured)"
    try:
        resp = runtime_client.invoke_agent_runtime(
            agentRuntimeArn=runtime_arn,
            payload=json.dumps({"prompt": prompt}).encode("utf-8"),
            contentType="application/json",
            accept="application/json",
        )
        body = resp["response"].read().decode("utf-8")
        parsed = json.loads(body)
        return parsed.get("result", body)
    except Exception as e:
        log.exception("Specialist invocation failed: %s", runtime_arn)
        return f"(specialist error: {type(e).__name__}: {e})"


@tool
def sharepoint_lookup(query: str) -> str:
    """Look up SharePoint policy documents for the given query.

    Args:
        query: Natural-language search query, e.g. "remote work URL policy".
    """
    return _invoke_runtime(SHAREPOINT_RUNTIME_ARN, query)


@tool
def awsconfig_lookup(query: str) -> str:
    """Look up AWS Config rule findings / compliance state.

    Args:
        query: Natural-language query, e.g. "S3 buckets without encryption".
    """
    return _invoke_runtime(AWSCONFIG_RUNTIME_ARN, query)


@tool
def zscaler_lookup(query: str) -> str:
    """Look up Zscaler ZIA URL allowlist / category policy.

    Args:
        query: Natural-language query, e.g. "is github.com allowed for engineering?".
    """
    return _invoke_runtime(ZSCALER_RUNTIME_ARN, query)


# ──────────────────────────── Memory helpers ──────────────────────
def _retrieve_history(actor_id: str, session_id: str, max_turns: int = 5) -> str:
    """Return prior turns for this (actor, session) formatted as plain text.

    Strategy: pull the most recent `max_turns` raw events via list_events,
    which is synchronous and gives immediate continuity. The summarization
    strategy runs asynchronously in the background — its output (in the
    /summaries/{actor}/{session} namespace) is then read as an additional
    layer for older sessions where the raw events have expired.

    Returns empty string when memory is disabled or any call fails — memory
    is best-effort and never fails the invocation.
    """
    if not MEMORY_ID:
        return ""

    # 1. Recent raw events — synchronous, available immediately after create_event.
    raw_lines: list[str] = []
    try:
        resp = runtime_client.list_events(
            memoryId=MEMORY_ID,
            actorId=actor_id,
            sessionId=session_id,
            maxResults=max_turns,
            includePayloads=True,
        )
        # Events come back newest-first; reverse for chronological order in the prompt.
        for ev in reversed(resp.get("events") or []):
            for item in ev.get("payload") or []:
                conv = item.get("conversational") or {}
                role = (conv.get("role") or "").lower()
                text = (conv.get("content") or {}).get("text") or ""
                if role and text:
                    raw_lines.append(f"{role}: {text}")
    except Exception as e:
        log.warning("list_events failed (%s); continuing without raw history", e)

    # 2. Older summary records (best-effort; empty until the strategy runs).
    summary_chunks: list[str] = []
    try:
        resp = runtime_client.retrieve_memory_records(
            memoryId=MEMORY_ID,
            namespace=f"/summaries/{actor_id}/{session_id}",
            searchCriteria={"searchQuery": "conversation summary", "topK": 3},
        )
        for r in resp.get("memoryRecordSummaries") or []:
            txt = (r.get("content") or {}).get("text") or ""
            if txt:
                summary_chunks.append(txt)
    except Exception as e:
        log.warning("retrieve_memory_records failed (%s); continuing without summaries", e)

    parts = []
    if summary_chunks:
        parts.append("Earlier summary:\n" + "\n\n".join(summary_chunks))
    if raw_lines:
        parts.append("Recent turns:\n" + "\n".join(raw_lines))
    if not parts:
        return ""
    log.info("Loaded history for %s/%s — %d raw turns, %d summary chunks",
             actor_id, session_id, len(raw_lines), len(summary_chunks))
    return "\n\n".join(parts)


def _save_turn(actor_id: str, session_id: str, user_text: str, assistant_text: str) -> None:
    """Persist this turn as a conversational event. Summarization strategy
    will asynchronously roll new events into the /summaries/... namespace."""
    if not MEMORY_ID:
        return
    try:
        runtime_client.create_event(
            memoryId=MEMORY_ID,
            actorId=actor_id,
            sessionId=session_id,
            eventTimestamp=datetime.now(timezone.utc),
            payload=[
                {"conversational": {"role": "USER", "content": {"text": user_text}}},
                {"conversational": {"role": "ASSISTANT", "content": {"text": assistant_text}}},
            ],
        )
    except Exception as e:
        log.warning("create_event failed (%s); continuing — turn not persisted", e)


# ──────────────────────────── Conversation index (DDB) ──────────
def _conversation_exists(actor_id: str, session_id: str) -> bool:
    """Cheap check via list_events: zero events = brand-new conversation.

    Using memory as the source of truth keeps the agent from racing with
    DDB writes during the same turn. If memory is disabled, fall back to
    assuming the conversation exists (no first-turn metadata write).
    """
    if not MEMORY_ID:
        return True
    try:
        resp = runtime_client.list_events(
            memoryId=MEMORY_ID,
            actorId=actor_id,
            sessionId=session_id,
            maxResults=1,
        )
        return bool(resp.get("events"))
    except Exception as e:
        log.warning("list_events probe failed (%s); assuming conversation exists", e)
        return True


def _index_new_conversation(actor_id: str, session_id: str, title: str, chat_type: str = "analyst") -> None:
    """Write a fresh row to the sessions index. Idempotent via attribute_not_exists.

    chat_type lets the UI list Analyst vs MCP sessions separately. Legacy rows
    without this attribute are treated as 'analyst' at read time.
    """
    if not ddb_client:
        return
    now = datetime.now(timezone.utc).isoformat()
    try:
        ddb_client.put_item(
            TableName=SESSIONS_TABLE,
            Item={
                "session_id": {"S": session_id},
                "user_id": {"S": actor_id},
                "title": {"S": title[:200]},
                "created_at": {"S": now},
                "last_message_at": {"S": now},
                "message_count": {"N": "0"},
                "chat_type": {"S": chat_type},
            },
            ConditionExpression="attribute_not_exists(session_id)",
        )
        log.info("Indexed new conversation %s for %s", session_id, actor_id)
    except ddb_client.exceptions.ConditionalCheckFailedException:
        # Row already present (race or retry) — nothing to do.
        pass
    except Exception as e:
        log.warning("Failed to write conversation row %s (%s); continuing", session_id, e)


def _bump_conversation(session_id: str, message_delta: int) -> None:
    """Update last_message_at + message_count on an existing row."""
    if not ddb_client:
        return
    try:
        ddb_client.update_item(
            TableName=SESSIONS_TABLE,
            Key={"session_id": {"S": session_id}},
            UpdateExpression="SET last_message_at = :ts ADD message_count :n",
            ExpressionAttributeValues={
                ":ts": {"S": datetime.now(timezone.utc).isoformat()},
                ":n": {"N": str(message_delta)},
            },
        )
    except Exception as e:
        log.warning("Failed to bump conversation %s (%s); continuing", session_id, e)


# ──────────────────────────── Agent factory ──────────────────────
def build_agent() -> Agent:
    model_kwargs: dict[str, Any] = {"model_id": MODEL_ID, "region_name": REGION}
    if GUARDRAIL_ID:
        model_kwargs["guardrail_id"] = GUARDRAIL_ID
        model_kwargs["guardrail_version"] = GUARDRAIL_VERSION
    return Agent(
        model=BedrockModel(**model_kwargs),
        system_prompt=SYSTEM_PROMPT,
        tools=[sharepoint_lookup, awsconfig_lookup, zscaler_lookup],
    )


# ──────────────────────────── Scan mode ──────────────────────────
def _run_scan(payload: dict[str, Any]) -> dict[str, Any]:
    """Deterministic rule-pack execution.

    Pulls structured observations from the three specialists (or, if they
    aren't reachable, from the fixture data the rule-pack falls back on),
    runs all 12 matchers, and returns a JSON array of findings + compliant
    rows. No Strands chat agent involved — the demo cannot tolerate LLM
    flakiness on this path.
    """
    from scan_rule_pack import run_rule_pack
    rule_pack_version = (payload.get("rule_pack") or "v1")
    scan_run_id = payload.get("scan_run_id") or "adhoc-scan"

    # Specialist observations. For Step 3 we don't yet have a structured
    # produce_findings() tool on each specialist — we synthesise minimal
    # observation shapes covering the 12 UCs so the rule-pack runs. When the
    # specialists ship structured tools (Step 6 polish), replace these with
    # invoke_agent_runtime calls.
    sharepoint = _seed_sharepoint_observations()
    zscaler    = _seed_zscaler_observations()
    awsconfig  = _seed_awsconfig_observations()

    findings = run_rule_pack(sharepoint, zscaler, awsconfig)
    for f in findings:
        f["scan_run_id"] = scan_run_id
    log.info("Scan complete: %d findings (rule_pack=%s, scan_run_id=%s)",
             len(findings), rule_pack_version, scan_run_id)
    # AgentCore stringifies entrypoint dicts with str(), which produces Python
    # repr (single-quoted, Decimal('0.05')) — not JSON. Explicitly serialize the
    # payload here and return it via the same {"result": ...} envelope chat uses.
    from decimal import Decimal
    def _json_default(o):
        if isinstance(o, Decimal):
            return float(o)
        raise TypeError(f"not serializable: {type(o)}")
    result_json = json.dumps(
        {"findings": findings, "scan_run_id": scan_run_id, "rule_pack_version": rule_pack_version},
        default=_json_default,
    )
    return {"result": result_json}


def _seed_sharepoint_observations() -> list[dict]:
    """Minimal SharePoint observation set covering all 12 UC clause needles.

    Each entry mirrors the shape the specialist's produce_findings() tool
    will eventually return (policy_doc, version, section, text). The rule
    matchers use case-insensitive substring search on `text`.
    """
    return [
        {"policy_doc": "MIG-POL-001", "version": "v3.4", "section": "2.1",
         "text": "Dropbox Business listed as approved. Passed vendor assessment Q3 2025."},
        {"policy_doc": "MIG-POL-001", "version": "v3.4", "section": "2.3",
         "text": "TeamViewer Corporate, AnyDesk Enterprise, BeyondTrust Remote Support are approved for authorised IT and MSP personnel."},
        {"policy_doc": "MIG-POL-001", "version": "v3.4", "section": "3",
         "text": "URL filtering controls must include exceptions for Marketing, Communications, HR, and Talent Acquisition."},
        {"policy_doc": "MIG-POL-001", "version": "v3.4", "section": "4",
         "text": "Chrome, Firefox, Edge, Safari, Brave are permitted on corporate devices without further approval."},
        {"policy_doc": "MIG-POL-002", "version": "v5.1", "section": "2.2",
         "text": "SSL/TLS inspection is mandatory on ALL web traffic. Exceptions only with documented CISO approval."},
        {"policy_doc": "MIG-POL-002", "version": "v5.1", "section": "4.1",
         "text": "MFA is required for ALL users — employees, contractors, vendors — regardless of privilege level."},
        {"policy_doc": "MIG-POL-002", "version": "v5.1", "section": "5.1",
         "text": "Monitoring-only mode is NOT acceptable for IoT external communication. Active blocking is required."},
        {"policy_doc": "MIG-POL-003", "version": "v2.2", "section": "2.1",
         "text": "Authorised actuarial data transfers: Milliman Inc., Willis Towers Watson, Verisk Analytics."},
        {"policy_doc": "MIG-POL-003", "version": "v2.2", "section": "3",
         "text": "All customer insurance data must remain within the continental United States. No exceptions."},
        {"policy_doc": "MIG-POL-003", "version": "v2.2", "section": "4",
         "text": "Approved vendor countries: US, India, UK, Singapore, Germany, Australia, Philippines, Canada."},
        {"policy_doc": "MIG-POL-004", "version": "v4.0", "section": "2",
         "text": "No production application resource shall be directly accessible from the public internet without AWS WAF + OWASP CRS."},
        {"policy_doc": "MIG-POL-004", "version": "v4.0", "section": "3",
         "text": "VPC peering between production and non-production environments is prohibited."},
        {"policy_doc": "MIG-POL-005", "version": "v2.8", "section": "5",
         "text": "ZTNA restrictions limited to India and US only are non-compliant."},
        {"policy_doc": "MIG-POL-005", "version": "v2.8", "section": "6",
         "text": "All vendor remote-support sessions must be logged to SIEM."},
    ]


def _seed_zscaler_observations() -> list[dict]:
    return [
        {"rule_id": "ZIA-URLCAT-CLOUD-BLK-042",       "action": "BLOCK",          "raw": {"category": "Cloud Storage", "domains": ["dropbox.com"]}},
        {"rule_id": "ZIA-APP-CTRL-REMOTE-BLOCK-007",  "action": "BLOCK",          "raw": {"apps": ["TeamViewer", "AnyDesk"]}},
        {"rule_id": "ZIA-APP-CTRL-BROWSER-FF-009",    "action": "BLOCK",          "raw": {"app": "Firefox"}},
        {"rule_id": "ZIA-SSL-BYPASS-FIN-DOMAINS",     "action": "BYPASS_INSPECT", "raw": {"domains_count": 47, "registered_exception": False}},
        {"rule_id": "ZPA-AUTHPOL-ADMIN-MFA-ONLY",     "action": "MFA_REQUIRED",   "raw": {"scope": "Privileged Admins", "non_admin_users_unprotected": 4200}},
        {"rule_id": "ZIA-IOT-MONITOR-ONLY-VLAN-19",   "action": "MONITOR",        "raw": {"vlan": 19, "devices": 43}},
        {"rule_id": "ZIA-DLP-PII-BLOCK-ALL-EXTERNAL", "action": "BLOCK",          "raw": {"exceptions": []}},
        {"rule_id": "ZPA-GEO-RESTRICT-INDIA-US-ONLY", "action": "ALLOW",          "raw": {"countries": ["IN", "US"]}},
        {"rule_id": "ZIA-URLCAT-SOCIAL-BLOCK-ALL",    "action": "BLOCK",          "raw": {"department_exceptions": []}},
    ]


def _seed_awsconfig_observations() -> list[dict]:
    return [
        {"resource_id": "alb-mig-prod-claims-api-001", "action": "NON_COMPLIANT",
         "raw": {"security_group": "sg-mig-prod-alb-open", "ingress": "0.0.0.0/0:443", "waf_attached": False, "age_days": 47}},
        {"resource_id": "pcx-mig-prod-dev-001", "action": "NON_COMPLIANT",
         "raw": {"prod_vpc": "vpc-mig-prod-001", "dev_vpc": "vpc-mig-dev-002", "age_days": 78}},
        {"resource_id": "mig-prod-claims-data-primary", "action": "NON_COMPLIANT",
         "raw": {"replication_target": "eu-west-1", "pii_tier": 1, "age_days": 134}},
        # Compliant guard resources — the rule-pack must NOT flag these.
        {"resource_id": "alb-mig-prod-api-002", "action": "COMPLIANT",
         "raw": {"waf_attached": True}},
        {"resource_id": "mig-prod-customer-data-secondary", "action": "COMPLIANT",
         "raw": {"replication_target": "us-west-2"}},
    ]


# ──────────────────────────── AgentCore entrypoint ───────────────
@app.entrypoint
def invoke(payload: dict[str, Any]) -> dict[str, Any]:
    # Scan-mode branch: the scanner Lambda invokes the Master with
    # {"scan": true, scan_run_id, rule_pack} and expects a JSON {"findings":[...]} back.
    if payload.get("scan") is True:
        return _run_scan(payload)

    prompt = payload.get("prompt") or payload.get("input") or ""
    if not prompt:
        return {"error": "Missing 'prompt' in request payload"}

    # Memory identifiers. If the caller didn't provide them we fall back to
    # 'anonymous' / 'adhoc' — those invocations get no cross-turn continuity.
    actor_id = (payload.get("actor_id") or payload.get("user_id") or "anonymous")[:128]
    session_id = (payload.get("session_id") or "adhoc")[:128]
    chat_type = (payload.get("chat_type") or "analyst")[:16]
    log.info("Orchestrator invoked: actor=%s session=%s chat_type=%s prompt=%s",
             actor_id, session_id, chat_type, prompt[:200])

    # New-conversation detection: if memory has no events for this session,
    # this is the first turn — index it in DDB so /conversations shows it.
    is_new = session_id != "adhoc" and not _conversation_exists(actor_id, session_id)
    if is_new:
        # Simple title heuristic: first 80 chars of the user's first prompt.
        # Cheaper than an extra Bedrock call; can swap to AI-generated later.
        title = prompt.strip().split("\n")[0][:80]
        _index_new_conversation(actor_id, session_id, title, chat_type=chat_type)

    history = _retrieve_history(actor_id, session_id)
    augmented_prompt = (
        f"Prior conversation context (the user may refer back to it):\n"
        f"{history}\n\n---\n\nCurrent question:\n{prompt}"
    ) if history else prompt

    agent = build_agent()
    response = str(agent(augmented_prompt))

    _save_turn(actor_id, session_id, prompt, response)
    if session_id != "adhoc":
        # One DDB write per turn covering both messages (user + assistant).
        _bump_conversation(session_id, message_delta=2)
    return {"result": response}


if __name__ == "__main__":
    # Local dev: `python agent.py` starts the AgentCore HTTP server on :8080
    app.run()