"""ARBITER Zscaler Specialist — runs on Bedrock AgentCore Runtime.

Two-mode design:
  - When ZSCALER_API_BASE is set, queries the live Zscaler ZIA API
  - Otherwise, falls back to KB retrieval of Zscaler policy exports
    (snapshots ingested into s3://dev-lmarbiter-processed/zscaler/)

Environment variables:
  KB_ID              Bedrock Knowledge Base ID (for fallback mode)
  ZSCALER_API_BASE   e.g. https://zsapi.zscaler.net  (empty = use KB)
  ZSCALER_SECRET_ID  Secrets Manager secret with api_key/username/password
  MODEL_ID           Bedrock model (default: Nova 2 Lite cross-region inference profile)
  GUARDRAIL_ID       Optional guardrail
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("zscaler_specialist")

REGION = os.environ.get("AWS_REGION", "us-east-1")
MODEL_ID = os.environ.get("MODEL_ID", "us.amazon.nova-2-lite-v1:0")
KB_ID = os.environ.get("KB_ID", "")
ZSCALER_API_BASE = os.environ.get("ZSCALER_API_BASE", "")
ZSCALER_SECRET_ID = os.environ.get("ZSCALER_SECRET_ID", "")
GUARDRAIL_ID = os.environ.get("GUARDRAIL_ID")
GUARDRAIL_VERSION = os.environ.get("GUARDRAIL_VERSION", "DRAFT")

SYSTEM_PROMPT = """You are the Zscaler ZIA specialist for ARBITER. You answer
questions about URL allowlists, URL categorization, and policy assignments.

Use the retrieve_zscaler_policy tool to fetch evidence from the policy
knowledge base. If ZSCALER_API_BASE is configured, use lookup_url_category
to query the live Zscaler API instead.

Return concise findings with the source (file path or API endpoint). Do not
fabricate categorizations.
"""

app = BedrockAgentCoreApp()
kb_runtime = boto3.client("bedrock-agent-runtime", region_name=REGION)
secrets_client = boto3.client("secretsmanager", region_name=REGION)


def _load_zscaler_credentials() -> dict[str, str] | None:
    if not ZSCALER_SECRET_ID:
        return None
    try:
        resp = secrets_client.get_secret_value(SecretId=ZSCALER_SECRET_ID)
        return json.loads(resp["SecretString"])
    except Exception as e:
        log.warning("Could not load Zscaler secret: %s", e)
        return None


@tool
def retrieve_zscaler_policy(query: str, max_results: int = 5) -> str:
    """Retrieve Zscaler ZIA policy excerpts from the knowledge base.

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
    return "\n\n---\n\n".join(chunks) if chunks else "No matching Zscaler policy excerpts found."


@tool
def lookup_url_category(url: str) -> str:
    """Look up Zscaler's category classification for a URL (live API).

    Args:
        url: Full URL to classify, e.g. "https://github.com".
    """
    if not ZSCALER_API_BASE:
        return "(live Zscaler API not configured — use retrieve_zscaler_policy instead)"

    creds = _load_zscaler_credentials()
    if not creds:
        return "(Zscaler credentials missing — secret ID not set or unreadable)"

    # Real implementation requires JSESSIONID auth flow:
    # https://help.zscaler.com/zia/api-developer-reference-guide#/-1
    # Stubbed for now — fill in once we have live API access.
    return (
        f"(live lookup stub) URL={url}\n"
        "Implement Zscaler /authenticatedSession + /urlCategories/lookup once "
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
        tools=[retrieve_zscaler_policy, lookup_url_category],
    )


@app.entrypoint
def invoke(payload: dict[str, Any]) -> dict[str, Any]:
    prompt = payload.get("prompt") or payload.get("input") or ""
    if not prompt:
        return {"error": "Missing 'prompt'"}
    log.info("Zscaler specialist: %s", prompt[:200])
    agent = build_agent()
    return {"result": str(agent(prompt))}


if __name__ == "__main__":
    app.run()
