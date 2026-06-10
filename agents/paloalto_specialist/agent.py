"""ARBITER Palo Alto Specialist — runs on Bedrock AgentCore Runtime.

Two-mode design:
  - When PALOALTO_API_BASE is set, queries the live PAN-OS / Panorama XML API
  - Otherwise, falls back to KB retrieval of Palo Alto rulebase exports
    (snapshots ingested into s3://<env>-<project>-processed/paloalto/)

Environment variables:
  KB_ID               Bedrock Knowledge Base ID (for fallback mode)
  PALOALTO_API_BASE   e.g. https://panorama.example.net  (empty = use KB)
  PALOALTO_SECRET_ID  Secrets Manager secret with api_key / username / password
  MODEL_ID            Bedrock model (default: Nova 2 Lite cross-region inference profile)
  GUARDRAIL_ID        Optional guardrail
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import boto3
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent
from strands.models.bedrock import BedrockModel
from strands.tools import tool

from _shared.token_usage import record_from_agent_result

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("paloalto_specialist")

REGION = os.environ.get("AWS_REGION", "us-east-1")
MODEL_ID = os.environ.get("MODEL_ID", "us.amazon.nova-2-lite-v1:0")
KB_ID = os.environ.get("KB_ID", "")
PALOALTO_API_BASE = os.environ.get("PALOALTO_API_BASE", "")
PALOALTO_SECRET_ID = os.environ.get("PALOALTO_SECRET_ID", "")
GUARDRAIL_ID = os.environ.get("GUARDRAIL_ID")
GUARDRAIL_VERSION = os.environ.get("GUARDRAIL_VERSION", "DRAFT")

SYSTEM_PROMPT = """You are the Palo Alto NGFW specialist for ARBITER. You answer
questions about perimeter firewall security rules, App-ID enforcement, egress
controls, and security zones on PAN-OS / Panorama.

Use the retrieve_paloalto_policy tool to fetch evidence from the rulebase
knowledge base. If PALOALTO_API_BASE is configured, use lookup_firewall_rule
to query the live PAN-OS XML API instead.

Return concise findings with the source (file path or API endpoint), naming the
specific security rule and its action (allow/deny). Do not fabricate rules.
"""

app = BedrockAgentCoreApp()
kb_runtime = boto3.client("bedrock-agent-runtime", region_name=REGION)
secrets_client = boto3.client("secretsmanager", region_name=REGION)


def _load_paloalto_credentials() -> dict[str, str] | None:
    if not PALOALTO_SECRET_ID:
        return None
    try:
        resp = secrets_client.get_secret_value(SecretId=PALOALTO_SECRET_ID)
        return json.loads(resp["SecretString"])
    except Exception as e:
        log.warning("Could not load Palo Alto secret: %s", e)
        return None


@tool
def retrieve_paloalto_policy(query: str, max_results: int = 5) -> str:
    """Retrieve Palo Alto firewall-rule excerpts from the knowledge base.

    Args:
        query: Natural language search.
        max_results: 1-10 chunks to return.
    """
    if not KB_ID:
        return "(KB_ID not configured)"
    retrieval_config: dict[str, Any] = {
        "vectorSearchConfiguration": {"numberOfResults": min(max(max_results, 1), 10)}
    }
    try:
        resp = kb_runtime.retrieve(
            knowledgeBaseId=KB_ID,
            retrievalQuery={"text": query},
            retrievalConfiguration=retrieval_config,
        )
    except Exception as e:
        log.exception("KB retrieve failed")
        return f"(retrieval error: {e})"

    chunks = []
    for i, item in enumerate(resp.get("retrievalResults", []), 1):
        text = item.get("content", {}).get("text", "")
        src = item.get("location", {}).get("s3Location", {}).get("uri", "unknown")
        score = item.get("score", 0)
        chunks.append(f"[{i}] (score={score:.3f}, src={src})\n{text}")
    return "\n\n---\n\n".join(chunks) if chunks else "No matching Palo Alto rule excerpts found."


@tool
def lookup_firewall_rule(rule_or_app: str) -> str:
    """Look up a Palo Alto security rule or App-ID action (live API).

    Args:
        rule_or_app: A rule name or App-ID, e.g. "PAN-SEC-APP-TOR-ALLOW-022" or "tor".
    """
    if not PALOALTO_API_BASE:
        return "(live PAN-OS API not configured — use retrieve_paloalto_policy instead)"

    creds = _load_paloalto_credentials()
    if not creds:
        return "(Palo Alto credentials missing — secret ID not set or unreadable)"

    # Real implementation requires the PAN-OS XML API keygen flow:
    # https://docs.paloaltonetworks.com/pan-os/pan-os/pan-os-panorama-api
    # Stubbed for now — fill in once we have live API access.
    return (
        f"(live lookup stub) rule_or_app={rule_or_app}\n"
        "Implement PAN-OS /api/?type=keygen + /api/?type=config (get rulebase) once "
        "real API credentials are provisioned."
    )


def build_agent() -> Agent:
    model_kwargs: dict[str, Any] = {"model_id": MODEL_ID, "region_name": REGION}
    if GUARDRAIL_ID:
        model_kwargs["guardrail_id"] = GUARDRAIL_ID
        model_kwargs["guardrail_version"] = GUARDRAIL_VERSION
    return Agent(
        model=BedrockModel(**model_kwargs),
        system_prompt=SYSTEM_PROMPT,
        tools=[retrieve_paloalto_policy, lookup_firewall_rule],
    )


@app.entrypoint
def invoke(payload: dict[str, Any]) -> dict[str, Any]:
    prompt = payload.get("prompt") or payload.get("input") or ""
    if not prompt:
        return {"error": "Missing 'prompt'"}
    actor_id   = (payload.get("actor_id")   or "anonymous")[:128]
    persona    = (payload.get("persona")    or "employee")[:16]
    session_id = (payload.get("session_id") or "adhoc")[:128]
    chat_type  = (payload.get("chat_type")  or "analyst")[:16]
    user_email = (payload.get("user_email") or "")[:200]
    log.info("Palo Alto specialist: persona=%s session=%s prompt=%s",
             persona, session_id, prompt[:200])
    agent = build_agent()
    agent_result = agent(prompt)
    record_from_agent_result(
        agent_result, agent="paloalto", persona=persona, actor_id=actor_id,
        session_id=session_id, chat_type=chat_type, model_id=MODEL_ID,
        user_email=user_email,
    )
    return {"result": str(agent_result)}


if __name__ == "__main__":
    app.run()
