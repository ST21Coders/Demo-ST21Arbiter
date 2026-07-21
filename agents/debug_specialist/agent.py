"""ARBITER Debug Specialist — runs on Bedrock AgentCore Runtime.

Lightweight on-call debugging assistant (OnCall_Assist catalog group).
Model + guardrail + domain system prompt only — no live log/telemetry access
yet; CloudWatch/X-Ray tooling can be added later without changing the contract.

Environment variables:
  MODEL_ID          Bedrock model (default: Nova 2 Lite cross-region inference profile)
  GUARDRAIL_ID      Optional guardrail
  GUARDRAIL_VERSION Guardrail version (default: DRAFT)
"""
from __future__ import annotations

import logging
import os
from typing import Any

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent
from strands.models.bedrock import BedrockModel

from _shared.token_usage import record_from_agent_result

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("debug_specialist")

REGION = os.environ.get("AWS_REGION", "us-east-1")
MODEL_ID = os.environ.get("MODEL_ID", "us.amazon.nova-2-lite-v1:0")
GUARDRAIL_ID = os.environ.get("GUARDRAIL_ID")
GUARDRAIL_VERSION = os.environ.get("GUARDRAIL_VERSION", "DRAFT")

SYSTEM_PROMPT = """You are the Debug specialist for ARBITER — an on-call
engineering assistant for incident triage and debugging.

You help with: interpreting stack traces, error messages, and log excerpts the
user pastes in; suggesting a structured triage path (blast radius → recent
changes → dependencies → data); proposing next diagnostic steps (which logs,
metrics, or traces to pull); drafting incident-status updates; and outlining
runbook steps for common failure classes (timeouts, throttling, OOM, DNS/TLS,
permission errors, bad deploys).

Hard rules:
- Work only from what the user pastes — you have no live access to logs,
  metrics, or systems yet. Say so rather than guessing at system state.
- Be blameless: describe failures in terms of systems and safeguards, never
  people.
- Prefer reversible, low-risk diagnostic steps first; call out clearly when a
  suggested action could mutate state (restarts, rollbacks, config changes).
Keep answers concise and actionable — numbered steps over prose walls.
"""

app = BedrockAgentCoreApp()


def build_agent() -> Agent:
    model_kwargs: dict[str, Any] = {"model_id": MODEL_ID, "region_name": REGION}
    if GUARDRAIL_ID:
        model_kwargs["guardrail_id"] = GUARDRAIL_ID
        model_kwargs["guardrail_version"] = GUARDRAIL_VERSION
    return Agent(
        model=BedrockModel(**model_kwargs),
        system_prompt=SYSTEM_PROMPT,
    )


@app.entrypoint
def invoke(payload: dict[str, Any]) -> dict[str, Any]:
    prompt = payload.get("prompt") or payload.get("input") or ""
    if not prompt:
        return {"error": "Missing 'prompt'"}
    # Attribution forwarded by api_handler/_handle_chat. Defaults keep direct
    # invocations (curl, tests) from crashing the record path.
    actor_id   = (payload.get("actor_id")   or "anonymous")[:128]
    persona    = (payload.get("persona")    or "employee")[:16]
    session_id = (payload.get("session_id") or "adhoc")[:128]
    chat_type  = (payload.get("chat_type")  or "analyst")[:16]
    user_email = (payload.get("user_email") or "")[:200]
    log.info("Debug specialist: persona=%s session=%s prompt=%s",
             persona, session_id, prompt[:200])
    agent = build_agent()
    agent_result = agent(prompt)
    record_from_agent_result(
        agent_result, agent="debug", persona=persona, actor_id=actor_id,
        session_id=session_id, chat_type=chat_type, model_id=MODEL_ID,
        user_email=user_email,
    )
    return {"result": str(agent_result)}


if __name__ == "__main__":
    app.run()
