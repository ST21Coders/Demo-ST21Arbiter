"""ARBITER Sales Specialist — runs on Bedrock AgentCore Runtime.

A self-contained HYBRID RAG agent for a fictional Hawaiian electronics-components
retailer. It routes each question to one of two tools (the split that keeps structured-data
RAG from returning confidently-wrong numbers):

  * search_sales_facts  — semantic search over the S3 Vectors `sales-facts` index
                          (fuzzy / descriptive questions: "how did marine electronics do
                          at the Kailua branch?").
  * query_sales_sql     — validated read-only text-to-SQL over the Athena/Glue sales table
                          (exact aggregation: totals, counts, top-N, averages, rankings).

Both tools are implemented by the shared `arbiter_rag` library — the SAME code the
sales_rag_lab notebook validates, so notebook == production.

IMPORTANT: this agent builds its arbiter_rag Settings from os.environ (see _settings()).
It must never call arbiter_rag.config.get_settings(), whose on-disk settings.toml lookup and
env-suffixed resource-name properties do not apply inside the container.

Environment variables:
  AWS_REGION            region (default us-east-1)
  MODEL_ID             Bedrock generation model (default Nova 2 Lite; the one-line swap point)
  EMBEDDING_MODEL_ID   Titan embed model (must match what the ingest script used)
  EMBEDDING_DIM        embedding dimension (must match the index; default 1024)
  SALES_VECTOR_BUCKET  S3 Vectors bucket holding the sales-facts index
  SALES_VECTOR_INDEX   S3 Vectors index name (default sales-facts)
  GLUE_DATABASE        Athena/Glue database holding the sales table
  GLUE_TABLE           Athena table name (must equal the real table — the SQL allowlist)
  ATHENA_WORKGROUP     read-only Athena workgroup
  ATHENA_OUTPUT        s3:// results location for Athena
  RERANK_ENABLED       "true" to enable Bedrock rerank (needs bedrock:Rerank IAM; default off)
  GUARDRAIL_ID / GUARDRAIL_VERSION   optional Bedrock guardrail
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Any

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent
from strands.models.bedrock import BedrockModel
from strands.tools import tool

from _shared.token_usage import record_from_agent_result
from arbiter_rag import athena_sql, embeddings, vectors
from arbiter_rag.config import Settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sales_specialist")

REGION = os.environ.get("AWS_REGION", "us-east-1")
MODEL_ID = os.environ.get("MODEL_ID", "us.amazon.nova-2-lite-v1:0")
EMBEDDING_MODEL_ID = os.environ.get("EMBEDDING_MODEL_ID", "amazon.titan-embed-text-v2:0")
EMBEDDING_DIM = int(os.environ.get("EMBEDDING_DIM", "1024"))
SALES_VECTOR_BUCKET = os.environ.get("SALES_VECTOR_BUCKET", "")
SALES_VECTOR_INDEX = os.environ.get("SALES_VECTOR_INDEX", "sales-facts")
GLUE_DATABASE = os.environ.get("GLUE_DATABASE", "")
GLUE_TABLE = os.environ.get("GLUE_TABLE", "hawaii_sales")
ATHENA_WORKGROUP = os.environ.get("ATHENA_WORKGROUP", "primary")
ATHENA_OUTPUT = os.environ.get("ATHENA_OUTPUT", "")
RERANK_ENABLED = os.environ.get("RERANK_ENABLED", "false").lower() == "true"
GUARDRAIL_ID = os.environ.get("GUARDRAIL_ID")
GUARDRAIL_VERSION = os.environ.get("GUARDRAIL_VERSION", "DRAFT")

SYSTEM_PROMPT = """You are the Sales specialist for ARBITER — a retail sales analyst for a
Hawaiian electronics-components retailer (Arduino boards, sensors, MOSFETs, marine
electronics, solar, tools) across island branches.

You have exactly two tools. Choose the RIGHT one for each question:

- query_sales_sql — for any question that needs EXACT NUMBERS computed over the whole
  dataset: totals, sums, counts, averages, "how many", "how much", top-N, rankings,
  "which branch/category/island had the most/least", percentages, breakdowns. This runs a
  validated read-only SQL query and returns the true figures. ALWAYS use it for aggregation.

- search_sales_facts — for FUZZY, descriptive, qualitative questions: "how did X perform at
  Y", "tell me about marine electronics sales", "what kinds of products sell on Maui".
  This returns pre-summarized branch × category facts from the semantic index.

Never answer a numeric/aggregation question from search_sales_facts — summing a few retrieved
facts is NOT the true total. When in doubt for a counting/ranking/total question, use
query_sales_sql. Cite the SQL or the retrieved fact sources. If a tool returns nothing, say so
plainly; never invent figures.
"""

app = BedrockAgentCoreApp()


@lru_cache(maxsize=1)
def _settings() -> Settings:
    """Build an arbiter_rag Settings from os.environ (never reads settings.toml).

    Only the query-path fields matter; ingest-only fields get harmless defaults so the
    frozen dataclass constructs cleanly.
    """
    return Settings(
        env=os.environ.get("ARBITER_ENV", "dev"),
        region=REGION,
        account="",
        expected_account_id="",
        generation_model_id=MODEL_ID,
        generation_max_tokens=int(os.environ.get("GENERATION_MAX_TOKENS", "1024")),
        generation_temperature=float(os.environ.get("GENERATION_TEMPERATURE", "0.2")),
        embedding_model_id=EMBEDDING_MODEL_ID,
        embedding_dim=EMBEDDING_DIM,
        rerank_enabled=RERANK_ENABLED,
        rerank_model_id=os.environ.get("RERANK_MODEL_ID", "amazon.rerank-v1:0"),
        rerank_candidates_k=20,
        rerank_top_k=5,
        retrieval_top_k=int(os.environ.get("RETRIEVAL_TOP_K", "5")),
        chunk_strategy="semantic",
        chunk_max_chars=1200,
        chunk_overlap_chars=200,
        chunking_version=os.environ.get("CHUNKING_VERSION", "v1"),
        vector_bucket=SALES_VECTOR_BUCKET,
        hr_index="hr-policies",
        sales_index=SALES_VECTOR_INDEX,
        distance_metric=os.environ.get("DISTANCE_METRIC", "cosine"),
        glue_database=GLUE_DATABASE,
        glue_table=GLUE_TABLE,
        athena_workgroup=ATHENA_WORKGROUP,
        athena_output_prefix=ATHENA_OUTPUT,
        max_scanned_bytes=int(os.environ.get("MAX_SCANNED_BYTES", str(1024 * 1024 * 1024))),
        guardrails_enabled=bool(GUARDRAIL_ID),
        guardrail_id=GUARDRAIL_ID or "",
        guardrail_version=GUARDRAIL_VERSION,
        ingest_batch_size=500,
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
    )


@tool
def search_sales_facts(query: str, max_results: int = 5) -> str:
    """Semantic search over the sales-facts vector index for FUZZY/descriptive questions.

    Use for qualitative lookups ("how did marine electronics do at the Kailua branch?"),
    NOT for exact totals/counts/rankings — route those to query_sales_sql.

    Args:
        query: Natural-language sales question.
        max_results: Number of facts to return (1-10).
    """
    if not SALES_VECTOR_BUCKET:
        return "(SALES_VECTOR_BUCKET not configured)"
    S = _settings()
    top_k = min(max(int(max_results), 1), 10)
    try:
        rt = embeddings.make_runtime_client(REGION)
        vx = vectors.make_client(REGION)
        q_vec = embeddings.embed_text(query, S, rt)
        hits = vectors.query(vx, SALES_VECTOR_BUCKET, SALES_VECTOR_INDEX, q_vec, top_k=top_k)
    except Exception as e:  # noqa: BLE001 — never crash the chat
        log.exception("sales semantic search failed")
        return f"(semantic search error: {type(e).__name__}: {e})"
    if not hits:
        return "No matching sales facts found."
    lines = []
    for i, h in enumerate(hits, 1):
        dist = f"{h.distance:.4f}" if h.distance is not None else "n/a"
        lines.append(f"[{i}] (distance={dist}, id={h.key})\n{h.text}")
    return "\n\n---\n\n".join(lines)


@tool
def query_sales_sql(question: str) -> str:
    """Answer EXACT-aggregation sales questions via validated read-only Athena SQL.

    Use for totals, counts, averages, top-N, rankings, breakdowns ("total revenue by
    category", "which branch sold the most units", "how many marine electronics were sold").
    The generated SQL is validated (single read-only SELECT over the sales table only) before
    it runs.

    Args:
        question: Natural-language question requiring precise numbers.
    """
    if not GLUE_DATABASE:
        return "(GLUE_DATABASE not configured)"
    S = _settings()
    try:
        sql = athena_sql.generate_sql(question, S)
    except Exception as e:  # noqa: BLE001
        log.exception("SQL generation failed")
        return f"(SQL generation error: {type(e).__name__}: {e})"
    try:
        athena_sql.validate_sql(sql, S.glue_table)  # explicit guard → clean error message
    except athena_sql.SqlValidationError as e:
        return f"(refused unsafe SQL: {e})\nSQL was: {sql}"
    try:
        result = athena_sql.run_query(
            sql, S, database=GLUE_DATABASE, workgroup=ATHENA_WORKGROUP,
            output_location=ATHENA_OUTPUT or None,
        )
    except Exception as e:  # noqa: BLE001
        log.exception("Athena query failed")
        return f"(query error: {type(e).__name__}: {e})\nSQL was: {sql}"
    header = " | ".join(result.columns)
    rows = "\n".join(" | ".join(str(r.get(c, "")) for c in result.columns) for r in result.rows[:50])
    return f"SQL:\n{result.sql}\n\nResult ({len(result.rows)} row(s)):\n{header}\n{rows or '(no rows)'}"


def build_agent() -> Agent:
    model_kwargs: dict[str, Any] = {"model_id": MODEL_ID, "region_name": REGION}
    if GUARDRAIL_ID:
        model_kwargs["guardrail_id"] = GUARDRAIL_ID
        model_kwargs["guardrail_version"] = GUARDRAIL_VERSION
    return Agent(
        model=BedrockModel(**model_kwargs),
        system_prompt=SYSTEM_PROMPT,
        tools=[search_sales_facts, query_sales_sql],
    )


@app.entrypoint
def invoke(payload: dict[str, Any]) -> dict[str, Any]:
    prompt = payload.get("prompt") or payload.get("input") or ""
    if not prompt:
        return {"error": "Missing 'prompt'"}
    # Attribution forwarded by master_orchestrator/_invoke_runtime. Defaults keep direct
    # invocations (curl, tests) from crashing the record path.
    actor_id = (payload.get("actor_id") or "anonymous")[:128]
    persona = (payload.get("persona") or "employee")[:16]
    session_id = (payload.get("session_id") or "adhoc")[:128]
    chat_type = (payload.get("chat_type") or "analyst")[:16]
    user_email = (payload.get("user_email") or "")[:200]
    log.info("Sales specialist: persona=%s session=%s prompt=%s", persona, session_id, prompt[:200])
    agent = build_agent()
    agent_result = agent(prompt)
    record_from_agent_result(
        agent_result, agent="sales", persona=persona, actor_id=actor_id,
        session_id=session_id, chat_type=chat_type, model_id=MODEL_ID,
        user_email=user_email,
    )
    return {"result": str(agent_result)}


if __name__ == "__main__":
    app.run()
