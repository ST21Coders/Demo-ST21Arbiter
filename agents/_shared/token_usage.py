"""Shared helper for capturing per-invocation token usage and writing rows
to the <env>-<project>-token-usage DynamoDB table.

Imported by the four AgentCore Runtimes (master + 3 specialists). Every Bedrock
model call performed by these agents should be followed by a call to
record_from_agent_result(...) — best-effort, never raises, never breaks chat.

Schema mirrors Infra/templates/04-storage.yaml::TokenUsageTable and the JS
fixture in ui/src/mockData.js so the page is byte-compatible across mock and
live mode.
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import boto3

log = logging.getLogger("token_usage")

REGION = os.environ.get("AWS_REGION", "us-east-1")
TOKEN_USAGE_TABLE = os.environ.get("TOKEN_USAGE_TABLE", "").strip()

# Bedrock list pricing (USD per 1M tokens). Single source of truth for the
# agent-side cost calculation; the UI mirrors these values in MODEL_PRICING in
# ui/src/mockData.js for the mock-mode KPI math. Keep both in sync when adding
# a second model or when AWS publishes new pricing.
#
# Each model is keyed by every model_id form an agent might write to the table:
#   - "us.<provider>.<model>"        — cross-region inference profile ID
#                                       (this is what MODEL_ID env var holds
#                                       on the live runtimes for Claude)
#   - "<provider>.<model>-<rev>-v1:0" — foundation-model ID
#                                       (this is what config.js MODELS uses)
# Both variants get the same rate so cost calc never falls through to zero
# just because the model_id used a different naming convention.
MODEL_PRICING: dict[str, dict[str, float]] = {
    # Amazon Nova 2 Lite — specialist agents (sharepoint / awsconfig / zscaler)
    "us.amazon.nova-2-lite-v1:0":                  {"input": 0.06, "output": 0.24},
    # Anthropic Claude Sonnet 4.6 — this deploy's master_orchestrator is wired
    # to this model via MASTER_MODEL_ID. Specialists may follow if Marketplace
    # subscription stays approved and the operator overrides their MODEL_ID.
    "us.anthropic.claude-sonnet-4-6":              {"input": 3.00, "output": 15.00},
    "anthropic.claude-sonnet-4-6-20251006-v1:0":   {"input": 3.00, "output": 15.00},
}

TTL_DAYS = 90

_VALID_PERSONAS = ("ciso", "soc", "grc", "employee")
_VALID_AGENTS = ("master", "sharepoint", "awsconfig", "zscaler")

# Lazy table handle — avoids a boto3 client construction when the env var is
# unset (e.g. an agent running before deploy_agents.py has been re-run).
_ddb_table = None


def _table():
    global _ddb_table
    if _ddb_table is not None:
        return _ddb_table
    if not TOKEN_USAGE_TABLE:
        return None
    _ddb_table = boto3.resource("dynamodb", region_name=REGION).Table(TOKEN_USAGE_TABLE)
    return _ddb_table


def compute_cost(model_id: str, input_tokens: int, output_tokens: int) -> Decimal:
    """Return the estimated cost in USD as a Decimal (DDB-safe)."""
    price = MODEL_PRICING.get(model_id) or {"input": 0.0, "output": 0.0}
    # str() then Decimal avoids float-precision drift on the way into DDB.
    raw = (max(0, input_tokens) * price["input"] + max(0, output_tokens) * price["output"]) / 1_000_000.0
    return Decimal(f"{raw:.6f}")


def extract_usage(agent_result: Any) -> tuple[int, int]:
    """Pull (input_tokens, output_tokens) out of whatever Strands surfaces.

    Strands' AgentResult exposes usage in a couple of attribute paths depending
    on the SDK version. Try the known locations in order, return (0, 0) if none
    yield numbers, and never raise — the caller's chat must succeed even if the
    usage layer is broken.
    """
    if agent_result is None:
        return (0, 0)
    # Path 1: result.metrics.accumulated_usage (recent Strands releases — the
    # event-loop metrics roll up usage across nested tool calls into one dict)
    try:
        metrics = getattr(agent_result, "metrics", None)
        usage = getattr(metrics, "accumulated_usage", None) if metrics else None
        if isinstance(usage, dict):
            return (
                int(usage.get("inputTokens") or usage.get("input_tokens") or 0),
                int(usage.get("outputTokens") or usage.get("output_tokens") or 0),
            )
    except Exception:
        pass
    # Path 2: result.usage (Bedrock raw response shape, older Strands)
    try:
        usage = getattr(agent_result, "usage", None)
        if isinstance(usage, dict):
            return (
                int(usage.get("inputTokens") or usage.get("input_tokens") or 0),
                int(usage.get("outputTokens") or usage.get("output_tokens") or 0),
            )
    except Exception:
        pass
    return (0, 0)


def record_usage(
    *,
    agent: str,
    persona: str,
    actor_id: str,
    session_id: str,
    chat_type: str,
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    guardrail_blocked: bool = False,
) -> None:
    """Best-effort write of one usage record to DDB. Never raises.

    Skips records with zero tokens and no guardrail flag — those represent
    pre-Bedrock failures and would dilute the KPI averages without signal.
    Defensively clamps persona to the known set so a typo upstream doesn't
    bypass the GSI partitioning.
    """
    if not _table():
        return
    input_t = max(0, int(input_tokens or 0))
    output_t = max(0, int(output_tokens or 0))
    if input_t == 0 and output_t == 0 and not guardrail_blocked:
        return

    persona_safe = persona if persona in _VALID_PERSONAS else "employee"
    agent_safe = agent if agent in _VALID_AGENTS else agent

    now = datetime.now(timezone.utc)
    timestamp = now.isoformat()
    session_id_safe = (session_id or "adhoc")[:128]
    actor_id_safe = (actor_id or "anonymous")[:128]
    # Mix a short uuid into the SK so master + specialists with sub-millisecond
    # timestamps never collide on identical (timestamp, session, agent).
    sk = f"ts#{timestamp}#{session_id_safe}#{agent_safe}#{uuid.uuid4().hex[:6]}"

    item = {
        "pk": f"persona#{persona_safe}",
        "sk": sk,
        "timestamp": timestamp,
        "agent": agent_safe,
        "persona": persona_safe,
        "user_email": actor_id_safe,
        "session_id": session_id_safe,
        "model_id": model_id or "",
        "input_tokens": input_t,
        "output_tokens": output_t,
        "total_tokens": input_t + output_t,
        "estimated_cost": compute_cost(model_id, input_t, output_t),
        "guardrail_blocked": bool(guardrail_blocked),
        "chat_type": (chat_type or "analyst")[:16],
        "ttl": int(time.time()) + TTL_DAYS * 24 * 3600,
    }
    try:
        _table().put_item(Item=item)
    except Exception as e:
        log.warning("token_usage put_item failed (%s); continuing — usage not recorded", e)


def record_from_agent_result(
    agent_result: Any,
    *,
    agent: str,
    persona: str,
    actor_id: str,
    session_id: str,
    chat_type: str,
    model_id: str | None = None,
) -> None:
    """Convenience wrapper: extract usage from a Strands AgentResult and write."""
    input_t, output_t = extract_usage(agent_result)
    record_usage(
        agent=agent,
        persona=persona,
        actor_id=actor_id,
        session_id=session_id,
        chat_type=chat_type,
        model_id=model_id or os.environ.get("MODEL_ID", "us.amazon.nova-2-lite-v1:0"),
        input_tokens=input_t,
        output_tokens=output_t,
        guardrail_blocked=False,
    )
