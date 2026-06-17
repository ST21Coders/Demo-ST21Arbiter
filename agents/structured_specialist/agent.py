"""ARBITER Structured Specialist — runs on Bedrock AgentCore Runtime.

Bridges STRUCTURED enforcement exports (CSV in S3, catalogued by Glue) into the
deterministic scan. Two modes:

  - Scan mode (deterministic, no LLM): payload {"mode":"produce_observations",
    "source":"zscaler"} → runs an Athena SELECT over the Glue-catalogued table and
    returns observation dicts in the EXACT shape the rule-pack matchers consume
    (see agents/master_orchestrator/agent.py::_seed_zscaler_observations). The
    master swaps its zscaler fixtures for this when STRUCTURED_RUNTIME_ARN is set,
    and falls back to fixtures on any error so a bad query never blanks the scan.

  - Chat mode: a Strands agent with a SELECT-only run_athena_query tool, for the
    MCP/analyst path ("how many zscaler rules bypass SSL inspection?").

Environment variables:
  GLUE_DATABASE     Glue Data Catalog database (default <env>_<project>_structured)
  ATHENA_WORKGROUP  Athena workgroup with SSE-KMS results + byte cap
  ATHENA_OUTPUT     s3://<processed-bucket>/athena-results/  (results location)
  MODEL_ID / GUARDRAIL_ID / GUARDRAIL_VERSION  as the other specialists
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import boto3
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent
from strands.models.bedrock import BedrockModel
from strands.tools import tool

from _shared.token_usage import record_from_agent_result
from observations import MAPPERS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("structured_specialist")

REGION = os.environ.get("AWS_REGION", "us-east-1")
MODEL_ID = os.environ.get("MODEL_ID", "us.amazon.nova-2-lite-v1:0")
GLUE_DATABASE = os.environ.get("GLUE_DATABASE", "")
ATHENA_WORKGROUP = os.environ.get("ATHENA_WORKGROUP", "primary")
ATHENA_OUTPUT = os.environ.get("ATHENA_OUTPUT", "")
GUARDRAIL_ID = os.environ.get("GUARDRAIL_ID")
GUARDRAIL_VERSION = os.environ.get("GUARDRAIL_VERSION", "DRAFT")

ATHENA_TIMEOUT_S = int(os.environ.get("ATHENA_TIMEOUT_S", "45"))
ATHENA_MAX_ROWS = int(os.environ.get("ATHENA_MAX_ROWS", "500"))
CHAT_TOOL_MAX_ROWS = int(os.environ.get("CHAT_TOOL_MAX_ROWS", "20"))

SYSTEM_PROMPT = """You are the Structured Data specialist for ARBITER. You answer
questions about technical-control exports, invoice batches, sales datasets, and
other CSV data that has been catalogued in AWS Glue and is queryable through
Amazon Athena.

When the user gives a friendly or partial dataset name such as "daily sales zone 1",
first call list_glue_tables to find the exact table name. Then use run_athena_query
with a single SELECT statement to fetch evidence. Never issue anything but SELECT.
Return concise findings naming the table and the rows. Do not fabricate rows.
"""

app = BedrockAgentCoreApp()
athena = boto3.client("athena", region_name=REGION)
glue = boto3.client("glue", region_name=REGION)


# ── Athena helpers ────────────────────────────────────────────────────────────

def _athena_rows(sql: str) -> list[dict[str, str]]:
    """Run a SELECT and return rows as dicts of column→string (Athena gives strings).

    SELECT-only, capped. Raises on non-SELECT or query failure so callers can fall
    back. Returns [] for an empty result set.
    """
    stripped = sql.strip().rstrip(";").lstrip("(").strip()
    if not stripped.lower().startswith("select") and not stripped.lower().startswith("with"):
        raise ValueError("Only SELECT/WITH queries are permitted")
    if not ATHENA_OUTPUT:
        raise RuntimeError("ATHENA_OUTPUT not configured")

    start = athena.start_query_execution(
        QueryString=sql,
        QueryExecutionContext={"Database": GLUE_DATABASE} if GLUE_DATABASE else {},
        WorkGroup=ATHENA_WORKGROUP,
        ResultConfiguration={"OutputLocation": ATHENA_OUTPUT},
    )
    qid = start["QueryExecutionId"]
    deadline = time.time() + ATHENA_TIMEOUT_S
    while time.time() < deadline:
        info = athena.get_query_execution(QueryExecutionId=qid)
        state = info["QueryExecution"]["Status"]["State"]
        if state == "SUCCEEDED":
            break
        if state in ("FAILED", "CANCELLED"):
            reason = info["QueryExecution"]["Status"].get("StateChangeReason", state)
            raise RuntimeError(f"Athena query {state}: {reason}")
        time.sleep(1)
    else:
        raise TimeoutError(f"Athena query {qid} timed out after {ATHENA_TIMEOUT_S}s")

    res = athena.get_query_results(QueryExecutionId=qid, MaxResults=min(ATHENA_MAX_ROWS, 1000) + 1)
    rows = res.get("ResultSet", {}).get("Rows", [])
    if not rows:
        return []
    header = [c.get("VarCharValue", "") for c in rows[0].get("Data", [])]
    out: list[dict[str, str]] = []
    for r in rows[1:]:
        cells = r.get("Data", [])
        out.append({header[i]: (cells[i].get("VarCharValue") if i < len(cells) else None)
                    for i in range(len(header))})
    return out


# ── produce_observations: Athena query + pure mapping (from observations.py) ──

def produce_observations(source: str) -> list[dict[str, Any]]:
    """Query the catalogued table for `source` and return matcher-shaped observations."""
    if source not in MAPPERS:
        raise ValueError(f"unknown structured source: {source}")
    table, mapper = MAPPERS[source]
    rows = _athena_rows(f'SELECT * FROM "{table}" LIMIT {ATHENA_MAX_ROWS}')
    observations = mapper(rows)
    log.info("produce_observations(%s): %d rows → %d observations", source, len(rows), len(observations))
    return observations


# ── Chat tool ─────────────────────────────────────────────────────────────────

@tool
def list_glue_tables(name_contains: str = "") -> str:
    """List Glue tables available to the structured-data specialist.

    Args:
        name_contains: Optional lowercase/partial filter, e.g. "daily_sales".
    """
    try:
        paginator = glue.get_paginator("get_tables")
        tables = []
        needle = (name_contains or "").strip().lower()
        for page in paginator.paginate(DatabaseName=GLUE_DATABASE):
            for table in page.get("TableList", []):
                name = table.get("Name", "")
                if needle and needle not in name.lower():
                    continue
                columns = [
                    column.get("Name", "")
                    for column in table.get("StorageDescriptor", {}).get("Columns", [])
                ]
                tables.append({"name": name, "columns": columns})
        if not tables:
            return "No matching Glue tables."
        return json.dumps(tables[:50], indent=2)
    except Exception as e:
        return f"(catalog error: {e})"


@tool
def run_athena_query(sql: str) -> str:
    """Run a single read-only SELECT against the structured-data catalog (Athena).

    Args:
        sql: One SELECT statement, e.g. "SELECT rule_id, action FROM zscaler_rules".
    """
    try:
        rows = _athena_rows(sql)
    except Exception as e:
        return f"(query error: {e})"
    if not rows:
        return "No rows."
    head = rows[:CHAT_TOOL_MAX_ROWS]
    return json.dumps(head, indent=2)


def build_agent() -> Agent:
    model_kwargs: dict[str, Any] = {"model_id": MODEL_ID, "region_name": REGION}
    if GUARDRAIL_ID:
        model_kwargs["guardrail_id"] = GUARDRAIL_ID
        model_kwargs["guardrail_version"] = GUARDRAIL_VERSION
    return Agent(
        model=BedrockModel(**model_kwargs),
        system_prompt=SYSTEM_PROMPT,
        tools=[list_glue_tables, run_athena_query],
    )


@app.entrypoint
def invoke(payload: dict[str, Any]) -> dict[str, Any]:
    # Scan mode — deterministic, no LLM. Used by the master during _run_scan.
    if payload.get("mode") == "produce_observations":
        source = (payload.get("source") or "zscaler")[:32]
        try:
            return {"observations": produce_observations(source)}
        except Exception as e:
            log.exception("produce_observations failed for %s", source)
            return {"error": str(e), "observations": []}

    # Chat mode.
    prompt = payload.get("prompt") or payload.get("input") or ""
    if not prompt:
        return {"error": "Missing 'prompt'"}
    stripped_prompt = prompt.strip().rstrip(";").lstrip("(").strip()
    if stripped_prompt.lower().startswith(("select", "with")):
        try:
            rows = _athena_rows(prompt)
            return {"result": json.dumps(rows[:ATHENA_MAX_ROWS], indent=2)}
        except Exception as e:
            return {"result": f"(query error: {e})"}
    actor_id   = (payload.get("actor_id")   or "anonymous")[:128]
    persona    = (payload.get("persona")    or "employee")[:16]
    session_id = (payload.get("session_id") or "adhoc")[:128]
    chat_type  = (payload.get("chat_type")  or "analyst")[:16]
    user_email = (payload.get("user_email") or "")[:200]
    log.info("Structured specialist: persona=%s session=%s prompt=%s", persona, session_id, prompt[:200])
    agent = build_agent()
    agent_result = agent(prompt)
    record_from_agent_result(
        agent_result, agent="structured", persona=persona, actor_id=actor_id,
        session_id=session_id, chat_type=chat_type, model_id=MODEL_ID, user_email=user_email,
    )
    return {"result": str(agent_result)}


if __name__ == "__main__":
    app.run()
