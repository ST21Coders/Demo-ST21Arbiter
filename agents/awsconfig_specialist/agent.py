"""ARBITER AWS Config Specialist — runs on Bedrock AgentCore Runtime.

Two data sources:
  1. Live AWS Config API — current rule definitions and compliance state
  2. Bedrock Knowledge Base — historical compliance snapshots, conformance
     pack docs, and control mappings stored at s3://dev-lmarbiter-processed/
     under the `awsconfig/` prefix

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
log = logging.getLogger("awsconfig_specialist")

REGION = os.environ.get("AWS_REGION", "us-east-1")
MODEL_ID = os.environ.get("MODEL_ID", "us.amazon.nova-2-lite-v1:0")
KB_ID = os.environ.get("KB_ID", "")
GUARDRAIL_ID = os.environ.get("GUARDRAIL_ID")
GUARDRAIL_VERSION = os.environ.get("GUARDRAIL_VERSION", "DRAFT")

SYSTEM_PROMPT = """You are the AWS Config specialist for ARBITER. You have
two information sources:

  1. The live AWS Config API (list_config_rules, get_rule_compliance,
     list_noncompliant_resources) — for the CURRENT state of rules and
     compliance in this account.
  2. The Bedrock Knowledge Base (retrieve_awsconfig_docs) — for historical
     compliance snapshots, conformance-pack documentation, and control
     mappings stored in s3://dev-lmarbiter-processed/awsconfig/.

Prefer the live API for "what is the current state?" questions. Use the
KB for "what does this control mean?", "what did compliance look like at
date X?", and to pull conformance-pack rationale. Cite rule names verbatim.

Never fabricate. If neither source has the answer, say so.
"""

app = BedrockAgentCoreApp()
config = boto3.client("config", region_name=REGION)
kb_runtime = boto3.client("bedrock-agent-runtime", region_name=REGION)


@tool
def list_config_rules(name_contains: str = "") -> str:
    """List AWS Config rules, optionally filtered by name substring.

    Args:
        name_contains: Case-insensitive substring to filter rule names. Empty = all rules.
    """
    try:
        paginator = config.get_paginator("describe_config_rules")
        rules = []
        for page in paginator.paginate():
            for r in page.get("ConfigRules", []):
                name = r.get("ConfigRuleName", "")
                if name_contains and name_contains.lower() not in name.lower():
                    continue
                rules.append({
                    "name": name,
                    "description": (r.get("Description") or "")[:200],
                    "state": r.get("ConfigRuleState"),
                    "scope": r.get("Scope", {}).get("ComplianceResourceTypes", []),
                })
        if not rules:
            return "No matching AWS Config rules found."
        return "\n".join(f"- {r['name']} [{r['state']}]: {r['description']}" for r in rules[:50])
    except Exception as e:
        log.exception("list_config_rules failed")
        return f"(error listing config rules: {e})"


@tool
def get_rule_compliance(rule_name: str) -> str:
    """Return compliance summary for a specific AWS Config rule.

    Args:
        rule_name: Exact name of the Config rule.
    """
    try:
        resp = config.get_compliance_details_by_config_rule(
            ConfigRuleName=rule_name,
            ComplianceTypes=["COMPLIANT", "NON_COMPLIANT", "NOT_APPLICABLE"],
            Limit=100,
        )
        details = resp.get("EvaluationResults", [])
        if not details:
            return f"No compliance evaluations for rule '{rule_name}'."

        by_status: dict[str, list[str]] = {}
        for d in details:
            status = d.get("ComplianceType", "UNKNOWN")
            qualifier = d.get("EvaluationResultIdentifier", {}).get("EvaluationResultQualifier", {})
            resource = f"{qualifier.get('ResourceType', '?')}/{qualifier.get('ResourceId', '?')}"
            by_status.setdefault(status, []).append(resource)

        out_lines = [f"Compliance for '{rule_name}':"]
        for status, resources in by_status.items():
            out_lines.append(f"  {status}: {len(resources)}")
            for r in resources[:10]:
                out_lines.append(f"    - {r}")
        return "\n".join(out_lines)
    except Exception as e:
        log.exception("get_rule_compliance failed")
        return f"(error getting compliance for {rule_name}: {e})"


@tool
def list_noncompliant_resources(rule_name: str) -> str:
    """List the resources currently failing a given Config rule.

    Args:
        rule_name: Exact name of the Config rule.
    """
    try:
        resp = config.get_compliance_details_by_config_rule(
            ConfigRuleName=rule_name,
            ComplianceTypes=["NON_COMPLIANT"],
            Limit=100,
        )
        results = resp.get("EvaluationResults", [])
        if not results:
            return f"No non-compliant resources for rule '{rule_name}'."
        lines = []
        for r in results:
            q = r.get("EvaluationResultIdentifier", {}).get("EvaluationResultQualifier", {})
            lines.append(f"- {q.get('ResourceType', '?')}/{q.get('ResourceId', '?')} (annotation: {r.get('Annotation', 'n/a')})")
        return "\n".join(lines)
    except Exception as e:
        log.exception("list_noncompliant_resources failed")
        return f"(error: {e})"


@tool
def retrieve_awsconfig_docs(query: str, max_results: int = 5) -> str:
    """Retrieve AWS Config conformance-pack docs / historical compliance snapshots from the KB.

    Use this for control rationale, NIST/CIS mappings, and prior-period
    compliance reports that aren't available through the live Config API.

    Args:
        query: Natural-language search query.
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

    return "\n\n---\n\n".join(chunks) if chunks else "No matching AWS Config documents found."


def build_agent() -> Agent:
    model_kwargs: dict[str, Any] = {"model_id": MODEL_ID, "region_name": REGION}
    if GUARDRAIL_ID:
        model_kwargs["guardrail_id"] = GUARDRAIL_ID
        model_kwargs["guardrail_version"] = GUARDRAIL_VERSION
    return Agent(
        model=BedrockModel(**model_kwargs),
        system_prompt=SYSTEM_PROMPT,
        tools=[
            list_config_rules,
            get_rule_compliance,
            list_noncompliant_resources,
            retrieve_awsconfig_docs,
        ],
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
    log.info("AWS Config specialist: persona=%s session=%s prompt=%s",
             persona, session_id, prompt[:200])
    agent = build_agent()
    agent_result = agent(prompt)
    record_from_agent_result(
        agent_result, agent="awsconfig", persona=persona, actor_id=actor_id,
        session_id=session_id, chat_type=chat_type, model_id=MODEL_ID,
        user_email=user_email,
    )
    return {"result": str(agent_result)}


if __name__ == "__main__":
    app.run()
