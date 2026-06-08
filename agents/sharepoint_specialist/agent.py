"""ARBITER SharePoint Specialist — runs on Bedrock AgentCore Runtime.

Retrieves relevant SharePoint policy excerpts from the Bedrock Knowledge Base
(ingested from s3://dev-lmarbiter-processed) and returns a concise, cited
summary.

Environment variables:
  KB_ID             Bedrock Knowledge Base ID
  MODEL_ID          Bedrock model (default: Nova 2 Lite cross-region inference profile)
  GUARDRAIL_ID      Optional guardrail
"""
from __future__ import annotations

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
log = logging.getLogger("sharepoint_specialist")

REGION = os.environ.get("AWS_REGION", "us-east-1")
MODEL_ID = os.environ.get("MODEL_ID", "us.amazon.nova-2-lite-v1:0")
KB_ID = os.environ.get("KB_ID", "")
GUARDRAIL_ID = os.environ.get("GUARDRAIL_ID")
GUARDRAIL_VERSION = os.environ.get("GUARDRAIL_VERSION", "DRAFT")

SYSTEM_PROMPT = """You are the SharePoint specialist for ARBITER. You retrieve
policy excerpts from the SharePoint document knowledge base and return them
verbatim with the source file path. Do not paraphrase or rewrite — the
orchestrator needs original citations to detect conflicts.

Use the retrieve_policies tool to fetch relevant chunks. If nothing relevant
is found, say so plainly — do not invent content.
"""

app = BedrockAgentCoreApp()
kb_runtime = boto3.client("bedrock-agent-runtime", region_name=REGION)


@tool
def retrieve_policies(query: str, max_results: int = 5) -> str:
    """Retrieve SharePoint policy excerpts matching the query from the KB.

    Args:
        query: Search query (natural language).
        max_results: How many chunks to return (1-10).
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

    return "\n\n---\n\n".join(chunks) if chunks else "No matching SharePoint policies found."


def build_agent() -> Agent:
    model_kwargs: dict[str, Any] = {"model_id": MODEL_ID, "region_name": REGION}
    if GUARDRAIL_ID:
        model_kwargs["guardrail_id"] = GUARDRAIL_ID
        model_kwargs["guardrail_version"] = GUARDRAIL_VERSION
    return Agent(
        model=BedrockModel(**model_kwargs),
        system_prompt=SYSTEM_PROMPT,
        tools=[retrieve_policies],
    )


@app.entrypoint
def invoke(payload: dict[str, Any]) -> dict[str, Any]:
    prompt = payload.get("prompt") or payload.get("input") or ""
    if not prompt:
        return {"error": "Missing 'prompt'"}
    # Attribution forwarded by master_orchestrator/_invoke_runtime. Defaults
    # keep direct invocations (curl, tests) from crashing the record path.
    actor_id   = (payload.get("actor_id")   or "anonymous")[:128]
    persona    = (payload.get("persona")    or "employee")[:16]
    session_id = (payload.get("session_id") or "adhoc")[:128]
    chat_type  = (payload.get("chat_type")  or "analyst")[:16]
    user_email = (payload.get("user_email") or "")[:200]
    log.info("SharePoint specialist: persona=%s session=%s prompt=%s",
             persona, session_id, prompt[:200])
    agent = build_agent()
    agent_result = agent(prompt)
    record_from_agent_result(
        agent_result, agent="sharepoint", persona=persona, actor_id=actor_id,
        session_id=session_id, chat_type=chat_type, model_id=MODEL_ID,
        user_email=user_email,
    )
    return {"result": str(agent_result)}


if __name__ == "__main__":
    app.run()
