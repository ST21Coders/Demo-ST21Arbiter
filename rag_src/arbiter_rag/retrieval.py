"""End-to-end retrieval orchestration: embed -> vector search -> rerank -> answer.

This is the public API the notebooks and the AgentCore tools call, so the deployed
chatbot uses the exact retrieval + prompt logic validated in the notebook.

Every retrieval logs its top distance so you can alert when the best match is still
far away (a "no good answer in the corpus" signal).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from . import embeddings, generation, guardrails, rerank, vectors
from .config import Settings, get_settings
from .observability import get_logger, log_event


@dataclass
class Clients:
    """Reusable boto3 clients so a chatty session does not rebuild them per call."""

    runtime: Any = None       # bedrock-runtime (embed + generate + guardrail)
    s3vectors: Any = None     # s3vectors
    rerank: Any = None        # bedrock-agent-runtime (rerank)

    @classmethod
    def build(cls, settings: Settings) -> Clients:
        return cls(
            runtime=embeddings.make_runtime_client(settings.region),
            s3vectors=vectors.make_client(settings.region),
            rerank=rerank.make_rerank_client(settings.region),
        )


@dataclass
class RetrievedContext:
    text: str
    doc_id: str
    distance: float | None
    rerank_score: float | None
    metadata: dict[str, Any] = field(default_factory=dict)
    key: str = ""                       # chunk/vector key (used to fuse vector + lexical)
    fusion_score: float | None = None   # RRF score when hybrid retrieval is used

    def as_prompt_ctx(self) -> dict[str, Any]:
        return {"text": self.text, "doc_id": self.doc_id}


@dataclass
class RagAnswer:
    answer: str
    contexts: list[RetrievedContext]
    input_tokens: int
    output_tokens: int
    guardrail_input_blocked: bool = False
    guardrail_output_intervened: bool = False
    citations: list[dict[str, Any]] = field(default_factory=list)


def _rrf_merge(
    vector_hits: list[Any], lexical_hits: list[Any], rrf_k: int
) -> tuple[list[str], dict[str, dict], dict[str, float | None], dict[str, float]]:
    """Reciprocal Rank Fusion of two ranked hit lists (each item exposes `.key`).

    RRF score for a key = sum over lists of 1 / (rrf_k + rank). It needs only ranks, so
    it fuses cosine distances and BM25 scores without normalizing their different scales.
    Returns (fused key order, metadata-by-key, vector-distance-by-key, rrf-score-by-key).
    """
    rrf: dict[str, float] = {}
    meta: dict[str, dict] = {}
    dist: dict[str, float | None] = {}
    for rank, h in enumerate(vector_hits):
        rrf[h.key] = rrf.get(h.key, 0.0) + 1.0 / (rrf_k + rank + 1)
        meta.setdefault(h.key, h.metadata)
        dist[h.key] = h.distance
    for rank, h in enumerate(lexical_hits):
        rrf[h.key] = rrf.get(h.key, 0.0) + 1.0 / (rrf_k + rank + 1)
        meta.setdefault(h.key, h.metadata)
    order = sorted(rrf, key=lambda k: rrf[k], reverse=True)
    return order, meta, dist, rrf


def retrieve(
    question: str,
    index_name: str,
    settings: Settings | None = None,
    *,
    metadata_filter: dict[str, Any] | None = None,
    top_k: int | None = None,
    use_rerank: bool | None = None,
    clients: Clients | None = None,
    bucket: str | None = None,
    lexical: Any | None = None,
    rrf_k: int = 60,
    lexical_top_k: int | None = None,
) -> list[RetrievedContext]:
    """Embed the question, search S3 Vectors, optionally fuse BM25 + rerank, return contexts.

    `bucket` overrides `settings.vector_bucket_name` — the deployed agents pass their
    explicit ARBITER bucket (whose name doesn't carry the notebook's env suffix).

    When `lexical` is a BM25 index (arbiter_rag.lexical.BM25Index), retrieval is HYBRID:
    the vector hits and the BM25 hits (over the same chunk texts) are merged with Reciprocal
    Rank Fusion before the optional rerank/truncate. `lexical=None` keeps pure vector search,
    so callers opt in without a forked code path (notebook == production).
    """
    settings = settings or get_settings()
    clients = clients or Clients.build(settings)
    logger = get_logger(level=settings.log_level)

    final_k = top_k or settings.retrieval_top_k
    do_rerank = settings.rerank_enabled if use_rerank is None else use_rerank
    candidate_k = settings.rerank_candidates_k if do_rerank else final_k
    hybrid = lexical is not None
    vector_k = max(candidate_k, lexical_top_k or 0) if hybrid else candidate_k

    q_vec = embeddings.embed_text(question, settings, clients.runtime)
    hits = vectors.query(
        clients.s3vectors,
        bucket or settings.vector_bucket_name,
        index_name,
        q_vec,
        top_k=vector_k,
        metadata_filter=metadata_filter,
    )

    if hybrid:
        lex_hits = lexical.search(question, k=lexical_top_k or vector_k)
        order, meta_by_key, dist_by_key, rrf_by_key = _rrf_merge(hits, lex_hits, rrf_k)
        contexts = [
            RetrievedContext(
                text=(meta_by_key[k].get("chunk_text") or meta_by_key[k].get("fact_text") or ""),
                doc_id=meta_by_key[k].get("doc_id", ""), distance=dist_by_key.get(k),
                rerank_score=None, metadata=meta_by_key[k], key=k, fusion_score=rrf_by_key[k],
            )
            for k in order
        ]
        log_event(
            logger, "retrieval_hybrid", index=index_name, vector_hits=len(hits),
            lexical_hits=len(lex_hits), fused=len(order),
            top_distance=hits[0].distance if hits else None, filtered=bool(metadata_filter),
        )
    else:
        contexts = [
            RetrievedContext(
                text=h.text, doc_id=h.metadata.get("doc_id", ""), distance=h.distance,
                rerank_score=None, metadata=h.metadata, key=h.key,
            )
            for h in hits
        ]
        log_event(
            logger, "retrieval", index=index_name, candidates=len(hits),
            top_distance=hits[0].distance if hits else None, filtered=bool(metadata_filter),
        )

    if not contexts:
        return []

    if do_rerank:
        ranked = rerank.rerank(
            question, [c.text for c in contexts], top_k=final_k, settings=settings, client=clients.rerank
        )
        reordered: list[RetrievedContext] = []
        for r in ranked:
            ctx = contexts[r.index]
            ctx.rerank_score = r.relevance_score
            reordered.append(ctx)
        return reordered

    return contexts[:final_k]


def answer(
    question: str,
    index_name: str,
    settings: Settings | None = None,
    *,
    metadata_filter: dict[str, Any] | None = None,
    system: str | None = None,
    clients: Clients | None = None,
    bucket: str | None = None,
    stream: Callable[[str], None] | None = None,
    lexical: Any | None = None,
    rrf_k: int = 60,
    lexical_top_k: int | None = None,
) -> RagAnswer:
    """Full RAG turn: guardrail(in) -> retrieve -> generate -> guardrail(out).

    `bucket` overrides the vector bucket name (for the deployed agents). When `stream` is a
    callback, generation uses `converse_stream` and calls it with each text delta as the
    answer forms (partial-display); either way the answer's `[n]` markers are rewritten
    inline with their source and returned in `RagAnswer.citations`.

    Pass `lexical` (a BM25 index) to make retrieval HYBRID (vector + BM25 via RRF); omit it
    for pure vector search. Same retrieval/generation path either way (notebook == production).
    """
    settings = settings or get_settings()
    clients = clients or Clients.build(settings)

    g_in = guardrails.apply(question, "INPUT", settings, clients.runtime)
    if g_in.intervened:
        return RagAnswer(
            answer="Your request could not be processed by the content policy.",
            contexts=[], input_tokens=0, output_tokens=0, guardrail_input_blocked=True,
        )

    contexts = retrieve(
        g_in.text, index_name, settings, metadata_filter=metadata_filter,
        clients=clients, bucket=bucket, lexical=lexical, rrf_k=rrf_k, lexical_top_k=lexical_top_k,
    )
    prompt = generation.build_rag_prompt(g_in.text, [c.as_prompt_ctx() for c in contexts])
    # 1-based context number -> its source, so [n] markers can be resolved inline.
    sources = {
        i: {"doc_id": c.doc_id, "title": c.metadata.get("title", "")}
        for i, c in enumerate(contexts, start=1)
    }

    if stream is not None:
        gen = generation.generate_stream(
            prompt, system=system, settings=settings, client=clients.runtime,
            on_delta=stream, sources=sources,
        )
        answer_text, citations = gen.cited_text, gen.citations
    else:
        raw = generation.generate(prompt, system=system, settings=settings, client=clients.runtime)
        answer_text, citations = generation.inject_citations(raw.text, sources)
        gen = raw

    g_out = guardrails.apply(answer_text, "OUTPUT", settings, clients.runtime)
    return RagAnswer(
        answer=g_out.text,
        contexts=contexts,
        input_tokens=gen.input_tokens,
        output_tokens=gen.output_tokens,
        guardrail_output_intervened=g_out.intervened,
        citations=citations,
    )
