"""ARBITER HR Specialist — runs on Bedrock AgentCore Runtime.

A semantic HR Agent answers about leave, benefits, compensation, conduct, payroll, perks.

Unlike the other specialists (which use a Strands tool-loop), the HR agent answers via the
shared arbiter_rag **streaming Converse** path — `retrieval.answer(..., stream=on_delta)`:

  * it retrieves the relevant policy passages from the S3 Vectors `hr-policies` index, then
  * streams a grounded answer with `generation.generate_stream` (Bedrock `converse_stream`),
    collecting text + metadata deltas as the answer forms (partial-display) and injecting
    INLINE source citations (`[1](HR-LEAVE-001)`) next to the text they support.

This is the SAME common helper the `hr_rag_lab` notebook validates, so notebook == agent.

IMPORTANT: this agent builds its arbiter_rag Settings from os.environ (see _settings()); it
must never call arbiter_rag.config.get_settings(). It passes the explicit HR_VECTOR_BUCKET to
retrieval.answer via `bucket=` (Settings' env-suffixed name property doesn't match ARBITER).

Environment variables:
  AWS_REGION           region (default us-east-1)
  MODEL_ID             Bedrock generation model (default Nova 2 Lite; the one-line swap point)
  EMBEDDING_MODEL_ID   Titan embed model (must match what the ingest step used)
  EMBEDDING_DIM        embedding dimension (must match the index; default 1024)
  HR_VECTOR_BUCKET     S3 Vectors bucket holding the hr-policies index
  HR_VECTOR_INDEX      S3 Vectors index name (default hr-policies)
  RETRIEVAL_TOP_K      passages to retrieve (default 4)
  RERANK_ENABLED       "true" to enable Bedrock rerank (needs bedrock:Rerank IAM; default off)
  HYBRID_ENABLED       "true" (default) to fuse S3 Vectors semantic + BM25 lexical via RRF
  BM25_TOP_K           BM25 candidates fused with the vector hits (default 10)
  RRF_K                Reciprocal Rank Fusion constant (default 60)
  GUARDRAIL_ID / GUARDRAIL_VERSION   optional Bedrock guardrail (applied to input + output)
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Any

from bedrock_agentcore.runtime import BedrockAgentCoreApp

from _shared.token_usage import record_usage
from arbiter_rag import lexical, retrieval, vectors
from arbiter_rag.config import Settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("hr_specialist")

REGION = os.environ.get("AWS_REGION", "us-east-1")
MODEL_ID = os.environ.get("MODEL_ID", "us.amazon.nova-2-lite-v1:0")
EMBEDDING_MODEL_ID = os.environ.get("EMBEDDING_MODEL_ID", "amazon.titan-embed-text-v2:0")
EMBEDDING_DIM = int(os.environ.get("EMBEDDING_DIM", "1024"))
HR_VECTOR_BUCKET = os.environ.get("HR_VECTOR_BUCKET", "")
HR_VECTOR_INDEX = os.environ.get("HR_VECTOR_INDEX", "hr-policies")
RETRIEVAL_TOP_K = int(os.environ.get("RETRIEVAL_TOP_K", "4"))
RERANK_ENABLED = os.environ.get("RERANK_ENABLED", "false").lower() == "true"
# Hybrid (semantic + BM25 lexical) retrieval — requirement 1c/1d. On by default; the BM25
# index is rebuilt once per container from the same S3 Vectors chunk texts (no extra infra).
HYBRID_ENABLED = os.environ.get("HYBRID_ENABLED", "true").lower() == "true"
BM25_TOP_K = int(os.environ.get("BM25_TOP_K", "10"))
RRF_K = int(os.environ.get("RRF_K", "60"))
# Above this many vectors, skip the cold-start BM25 rebuild and serve vector-only — bounds
# first-query latency + memory when this agent is retargeted at a large DocuSearch index.
BM25_MAX_RECORDS = int(os.environ.get("BM25_MAX_RECORDS", "20000"))
GUARDRAIL_ID = os.environ.get("GUARDRAIL_ID")
GUARDRAIL_VERSION = os.environ.get("GUARDRAIL_VERSION", "DRAFT")

HR_SYSTEM = """You are the HR policy assistant for Kai Components, a Hawaiian
electronics-components retailer. Answer employee questions about HR policy — leave/PTO,
benefits, sales compensation and commission, code of conduct, payroll and scheduling, and
employee perks — using ONLY the numbered context passages provided. After each sentence that
uses a passage, cite its number in square brackets, e.g. [1]. Quote the specific figure or
rule. If the passages do not contain the answer, say so plainly and suggest which policy or
HR contact to check — never invent a number, date, or entitlement. Be concise. These
policies are fictional sample data for a demo."""

# Used when the agent is retargeted per-request at a user-selected DocuSearch group instead of
# the built-in HR corpus — same grounded/cited answer style, no Kai-Components HR framing.
NEUTRAL_HR_SYSTEM = """You are a document analyst for ARBITER answering questions about a
user-selected document group. Answer ONLY from the numbered context passages provided. After
each sentence that uses a passage, cite its number in square brackets, e.g. [1]. Quote the
specific figure or wording. If the passages do not contain the answer, say so plainly — never
invent a fact, number, or date. Be concise."""

app = BedrockAgentCoreApp()


@lru_cache(maxsize=1)
def _settings() -> Settings:
    """Build an arbiter_rag Settings from os.environ (never reads settings.toml).

    Only the semantic query-path fields matter; the SQL/ingest fields get harmless
    defaults so the frozen dataclass constructs cleanly.
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
        rerank_top_k=RETRIEVAL_TOP_K,
        retrieval_top_k=RETRIEVAL_TOP_K,
        chunk_strategy="semantic",
        chunk_max_chars=1200,
        chunk_overlap_chars=200,
        chunking_version=os.environ.get("CHUNKING_VERSION", "v1"),
        vector_bucket=HR_VECTOR_BUCKET,
        hr_index=HR_VECTOR_INDEX,
        sales_index="sales-facts",
        distance_metric=os.environ.get("DISTANCE_METRIC", "cosine"),
        glue_database="",
        glue_table="",
        athena_workgroup="primary",
        athena_output_prefix="",
        max_scanned_bytes=int(os.environ.get("MAX_SCANNED_BYTES", str(1024 * 1024 * 1024))),
        guardrails_enabled=bool(GUARDRAIL_ID),
        guardrail_id=GUARDRAIL_ID or "",
        guardrail_version=GUARDRAIL_VERSION,
        ingest_batch_size=500,
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
    )


@lru_cache(maxsize=8)
def _bm25(bucket: str, index: str) -> Any:
    """Build a BM25 lexical index for (bucket, index), cached per distinct index so one warm
    container can serve many DocuSearch groups.

    Requirement 1c/1d: keyword search that merges with semantic hits. S3 Vectors has no native
    lexical index, so we scan the same vectors (chunk_text metadata) and build an in-memory BM25
    index — no extra store, consistent with what was ingested. Returns None past BM25_MAX_RECORDS
    (→ vector-only) to bound cold-start latency/memory on large corpora.
    """
    vx = vectors.make_client(REGION)
    records = []
    for rec in vectors.iter_all_records(vx, bucket, index):
        records.append(rec)
        if len(records) > BM25_MAX_RECORDS:
            log.info("HR specialist: %s/%s over BM25 cap (%d); serving vector-only",
                     bucket, index, BM25_MAX_RECORDS)
            return None
    log.info("HR specialist: built BM25 index over %d vectors for %s/%s", len(records), bucket, index)
    return lexical.build_index(records)


def _format_sources(citations: list[dict[str, Any]]) -> str:
    """Compact source list appended under the (already inline-cited) answer text."""
    parts = []
    for c in citations:
        label = f"[{c.get('marker')}] {c.get('doc_id', '?')}"
        if c.get("title"):
            label += f" — {c['title']}"
        parts.append(label)
    return "Sources: " + "; ".join(parts) if parts else ""


@app.entrypoint
def invoke(payload: dict[str, Any]) -> dict[str, Any]:
    prompt = payload.get("prompt") or payload.get("input") or ""
    if not prompt:
        return {"error": "Missing 'prompt'"}
    # Per-request target: api_handler forwards vector_bucket/index for a selected DocuSearch
    # group; absent → the built-in HR corpus. One warm container serves any group's index.
    req_bucket = (payload.get("vector_bucket") or "").strip()
    req_index = (payload.get("vector_index") or "").strip()
    custom = bool(req_bucket or req_index)
    vector_bucket = req_bucket or HR_VECTOR_BUCKET
    vector_index = req_index or HR_VECTOR_INDEX
    if not vector_bucket:
        return {"result": "(HR vector bucket not configured)"}
    # Attribution forwarded by master_orchestrator/_invoke_runtime. Defaults keep direct
    # invocations (curl, tests) from crashing the record path.
    actor_id = (payload.get("actor_id") or "anonymous")[:128]
    persona = (payload.get("persona") or "employee")[:16]
    session_id = (payload.get("session_id") or "adhoc")[:128]
    chat_type = (payload.get("chat_type") or "analyst")[:16]
    user_email = (payload.get("user_email") or "")[:200]
    log.info("HR specialist: persona=%s session=%s target=%s/%s prompt=%s",
             persona, session_id, vector_bucket, vector_index, prompt[:200])

    S = _settings()
    # Feature (a): collect streamed text deltas as the answer forms. In the runtime we
    # buffer them (a future SSE response path can forward each delta to the UI live).
    streamed = {"chars": 0}

    def on_delta(piece: str) -> None:
        streamed["chars"] += len(piece)

    # Build the BM25 lexical index for hybrid retrieval (falls back to vector-only if the
    # scan fails, so a lexical hiccup never takes the chat down).
    lex = None
    if HYBRID_ENABLED:
        try:
            lex = _bm25(vector_bucket, vector_index)
        except Exception:  # noqa: BLE001
            log.exception("BM25 build failed; falling back to vector-only retrieval")

    try:
        ans = retrieval.answer(
            prompt, vector_index, S, bucket=vector_bucket,
            system=NEUTRAL_HR_SYSTEM if custom else HR_SYSTEM, stream=on_delta,
            lexical=lex, rrf_k=RRF_K, lexical_top_k=BM25_TOP_K,
        )
    except Exception as e:  # noqa: BLE001 — never crash the chat
        log.exception("HR answer failed")
        return {"result": f"(HR lookup error: {type(e).__name__}: {e})"}

    log.info("HR specialist: streamed %d chars, %d citation(s), tokens in=%d out=%d",
             streamed["chars"], len(ans.citations), ans.input_tokens, ans.output_tokens)

    # Feature (b): ans.answer already carries inline [n](doc_id) citations; append a compact
    # source list for the distinct policies referenced.
    result = ans.answer
    footer = _format_sources(ans.citations)
    if footer:
        result = f"{result}\n\n{footer}"

    record_usage(
        agent="hr", persona=persona, actor_id=actor_id, session_id=session_id,
        chat_type=chat_type, model_id=MODEL_ID, input_tokens=ans.input_tokens,
        output_tokens=ans.output_tokens, guardrail_blocked=ans.guardrail_input_blocked,
        user_email=user_email,
    )
    return {"result": result}


if __name__ == "__main__":
    app.run()
