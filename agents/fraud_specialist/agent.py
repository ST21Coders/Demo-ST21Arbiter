"""ARBITER Fraud Specialist — runs on Bedrock AgentCore Runtime.

Lightweight insurance-fraud advisory agent (Insurance_Assist catalog group).
Model + guardrail + domain system prompt only — no data backends yet; a fraud
signals/claims RAG source can be added later without changing the contract.

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
log = logging.getLogger("fraud_specialist")

REGION = os.environ.get("AWS_REGION", "us-east-1")
MODEL_ID = os.environ.get("MODEL_ID", "us.amazon.nova-2-lite-v1:0")
GUARDRAIL_ID = os.environ.get("GUARDRAIL_ID")
GUARDRAIL_VERSION = os.environ.get("GUARDRAIL_VERSION", "DRAFT")

SYSTEM_PROMPT = """You are the Fraud specialist for ARBITER — an insurance
fraud-detection analyst assistant for Meridian Insurance SIU and claims staff.

You help with: recognizing common fraud red flags (staged accidents, inflated
or duplicate billing, late reporting, prior-damage claims, provider patterns,
identity inconsistencies), structuring an investigation checklist, explaining
SIU referral criteria and thresholds, and summarizing fraud typologies
(opportunistic vs organized, hard vs soft fraud).

Hard rules:
- You surface INDICATORS, not verdicts. Never label a person or claim as
  fraudulent — say "these indicators warrant SIU review" instead.
- Never invent claim numbers, names, or case details. You have no access to
  live claims or fraud-scoring systems yet — say so when asked for records.
- Do not provide guidance that would help someone commit or conceal fraud;
  decline and restate your investigative purpose.
Keep answers concise, structured, and evidence-oriented.
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
    log.info("Fraud specialist: persona=%s session=%s prompt=%s",
             persona, session_id, prompt[:200])
    agent = build_agent()
    agent_result = agent(prompt)
    record_from_agent_result(
        agent_result, agent="fraud", persona=persona, actor_id=actor_id,
        session_id=session_id, chat_type=chat_type, model_id=MODEL_ID,
        user_email=user_email,
    )
    return {"result": str(agent_result)}


if __name__ == "__main__":
    app.run()
