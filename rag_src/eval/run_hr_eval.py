"""Evaluate the Kai Components HR (unstructured) RAG — retrieval quality at scale.

The HR path is semantic-only (PDF policies → chunk → embed → S3 Vectors → retrieve →
answer), so the objective signal is RETRIEVAL: does a question surface the right policy
document in the top-k? (see eval/golden/hr_qa.jsonl — each case tags the relevant_doc_ids).

Modes:
  python eval/run_hr_eval.py            # OFFLINE: golden-set integrity + corpus coverage
                                        #   + a deterministic lexical-retrieval floor. No AWS.
  python eval/run_hr_eval.py --live     # LIVE: real recall@k / hit-rate / MRR over the
                                        #   S3 Vectors hr-policies index. Needs creds +
                                        #   provisioned resources (see rag_instructions.md).

Live env (same names as the deployed agent): HR_VECTOR_BUCKET, HR_VECTOR_INDEX,
EMBEDDING_MODEL_ID, MODEL_ID.

Results are written to eval/results/hr_<mode>_<stamp>.json so you can trend runs across
enhancements (change chunking / top_k / model → re-run → compare).
"""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

import arbiter_rag  # noqa: F401  (import-smoke; also fails fast if the package isn't installed)
from arbiter_rag import chunking, evaluation, lexical, loaders
from arbiter_rag.config import DATA_ROOT, RAG_ROOT, Settings

GOLDEN = RAG_ROOT / "eval" / "golden" / "hr_qa.jsonl"
RESULTS_DIR = RAG_ROOT / "eval" / "results"
POLICY_DIR = DATA_ROOT / "Hawaii_HR_Policies"
TOP_K = int(os.environ.get("EVAL_TOP_K", "4"))
CHUNK_STRATEGY = os.environ.get("EVAL_CHUNK_STRATEGY", "semantic")
CHUNK_VERSION = os.environ.get("EVAL_CHUNK_VERSION", "v1")
RRF_K = int(os.environ.get("EVAL_RRF_K", "60"))


def load_cases() -> list[dict]:
    if not GOLDEN.exists():
        raise SystemExit(f"golden set missing: {GOLDEN}")
    return [json.loads(l) for l in GOLDEN.read_text().splitlines() if l.strip()]


def _load_chunks() -> list:
    """Chunk the corpus the same way the notebook / ingest does (doc_id on every chunk)."""
    docs = loaders.iter_hr_documents(POLICY_DIR)
    chunks = []
    for d in docs:
        meta = {"policy_category": d["policy_category"], "title": d["title"], "doc_id": d["doc_id"]}
        chunks += chunking.chunk_document(
            d["text"], d["doc_id"], strategy=CHUNK_STRATEGY,
            chunking_version=CHUNK_VERSION, metadata=meta,
        )
    return chunks


def _lexical_topk(question: str, chunks: list, k: int) -> list[str]:
    """Offline retrieval floor: keyword-overlap over chunk text → top-k doc_ids (deduped)."""
    qtok = set(re.findall(r"[a-z0-9]+", question.lower()))
    scored = []
    for c in chunks:
        ctok = set(re.findall(r"[a-z0-9]+", c.text.lower()))
        scored.append((len(qtok & ctok), c))
    scored.sort(key=lambda t: t[0], reverse=True)
    seen, out = set(), []
    for _, c in scored:
        if c.doc_id not in seen:
            seen.add(c.doc_id)
            out.append(c.doc_id)
        if len(out) >= k:
            break
    return out


def _bm25_index(chunks: list):
    """Real BM25 (arbiter_rag.lexical) over the chunk corpus — the deployed lexical half."""
    records = [{"key": c.id, "text": c.text, "metadata": {"doc_id": c.doc_id}} for c in chunks]
    return lexical.build_index(records)


def _dedup_doc_ids(hits, k: int) -> list[str]:
    """Chunk-level hits → ordered, de-duplicated top-k doc_ids."""
    seen, out = set(), []
    for h in hits:
        d = h.metadata.get("doc_id", "")
        if d and d not in seen:
            seen.add(d)
            out.append(d)
        if len(out) >= k:
            break
    return out


def _rrf_doc_ids(list_a: list[str], list_b: list[str], k: int, rrf_k: int = RRF_K) -> list[str]:
    """Fuse two ranked doc_id lists with Reciprocal Rank Fusion → top-k doc_ids."""
    score: dict[str, float] = {}
    for ranked in (list_a, list_b):
        for rank, d in enumerate(ranked):
            score[d] = score.get(d, 0.0) + 1.0 / (rrf_k + rank + 1)
    return sorted(score, key=lambda d: score[d], reverse=True)[:k]


def _settings_from_env() -> Settings:
    ENV = os.environ.get("ENVIRONMENT", "dev")
    PROJECT = os.environ.get("PROJECT", "st21arbiter-poc")
    PREFIX = f"{ENV}-{PROJECT}"
    return Settings(
        env=ENV, region=os.environ.get("AWS_REGION", "us-east-1"), account="", expected_account_id="",
        generation_model_id=os.environ.get("MODEL_ID", "us.amazon.nova-2-lite-v1:0"),
        generation_max_tokens=1024, generation_temperature=0.2,
        embedding_model_id=os.environ.get("EMBEDDING_MODEL_ID", "amazon.titan-embed-text-v2:0"),
        embedding_dim=int(os.environ.get("EMBEDDING_DIM", "1024")),
        rerank_enabled=os.environ.get("RERANK_ENABLED", "false").lower() == "true",
        rerank_model_id="amazon.rerank-v1:0", rerank_candidates_k=20, rerank_top_k=TOP_K,
        retrieval_top_k=TOP_K, chunk_strategy=CHUNK_STRATEGY, chunk_max_chars=1200,
        chunk_overlap_chars=200, chunking_version=CHUNK_VERSION,
        vector_bucket=os.environ.get("HR_VECTOR_BUCKET", f"{PREFIX}-hr-vectors"),
        hr_index=os.environ.get("HR_VECTOR_INDEX", "hr-policies"), sales_index="sales-facts",
        distance_metric="cosine", glue_database="", glue_table="", athena_workgroup="primary",
        athena_output_prefix="", max_scanned_bytes=1024 * 1024 * 1024, guardrails_enabled=False,
        guardrail_id="", guardrail_version="DRAFT", ingest_batch_size=500, log_level="INFO",
    )


def run_offline(cases: list[dict]) -> dict:
    """No AWS: golden integrity, corpus coverage, and a lexical retrieval floor."""
    chunks = _load_chunks()
    corpus_ids = {c.doc_id for c in chunks}
    missing = sorted({rid for c in cases for rid in c["relevant_doc_ids"] if rid not in corpus_ids})

    per = [{"retrieved_ids": _lexical_topk(c["question"], chunks, TOP_K),
            "relevant_ids": c["relevant_doc_ids"], "top_distance": None} for c in cases]
    report_r = evaluation.aggregate_retrieval(per, TOP_K)

    # Real BM25 (the deployed lexical half) — no AWS needed, so it exercises the shipped
    # arbiter_rag.lexical code the hybrid agent uses.
    bm25 = _bm25_index(chunks)
    per_bm25 = [{"retrieved_ids": _dedup_doc_ids(bm25.search(c["question"], k=TOP_K * 4), TOP_K),
                 "relevant_ids": c["relevant_doc_ids"], "top_distance": None} for c in cases]
    report_bm25 = evaluation.aggregate_retrieval(per_bm25, TOP_K)

    report = {
        "type": "hr_offline", "n_cases": len(cases), "n_docs": len(corpus_ids),
        "n_chunks": len(chunks), "top_k": TOP_K, "chunk_strategy": CHUNK_STRATEGY,
        "corpus_covers_relevant": not missing, "missing_relevant_docs": missing,
        "lexical_hit_rate": round(report_r.hit_rate, 3),
        "lexical_recall_at_k": round(report_r.recall_at_k, 3),
        "lexical_mrr": round(report_r.mrr, 3),
        "bm25_hit_rate": round(report_bm25.hit_rate, 3),
        "bm25_recall_at_k": round(report_bm25.recall_at_k, 3),
        "bm25_mrr": round(report_bm25.mrr, 3),
    }
    print(f"OFFLINE HR eval: {len(cases)} cases, {len(corpus_ids)} docs, {len(chunks)} chunks")
    print(f"  corpus covers relevant : {report['corpus_covers_relevant']}"
          + (f"  MISSING: {missing}" if missing else ""))
    print(f"  lexical(floor) hit@{TOP_K}  : {report['lexical_hit_rate']:.3f}"
          f"  recall@{TOP_K}: {report['lexical_recall_at_k']:.3f}  mrr: {report['lexical_mrr']:.3f}")
    print(f"  bm25 (shipped) hit@{TOP_K}  : {report['bm25_hit_rate']:.3f}"
          f"  recall@{TOP_K}: {report['bm25_recall_at_k']:.3f}  mrr: {report['bm25_mrr']:.3f}")
    return report


def run_live(cases: list[dict]) -> dict:
    """AWS: compare vector-only / BM25-only / hybrid(RRF) recall@k over the live index.

    Vector search hits S3 Vectors; the BM25 index is rebuilt from the same chunk corpus (the
    exact cold-start path the deployed hybrid agent uses). Reporting all three side by side is
    the "evals for improvements" signal — hybrid should match or beat vector-only, and win on
    keyword/code-heavy questions.
    """
    from arbiter_rag import embeddings, vectors  # lazy (need creds)
    S = _settings_from_env()
    rt = embeddings.make_runtime_client(S.region)
    vx = vectors.make_client(S.region)
    bm25 = _bm25_index(_load_chunks())
    over_k = TOP_K * 4  # over-fetch candidates from each retriever before fusing/truncating

    per_v, per_b, per_h = [], [], []
    for c in cases:
        q_vec = embeddings.embed_text(c["question"], S, rt)
        vhits = vectors.query(vx, S.vector_bucket, S.hr_index, q_vec, top_k=over_k)
        bhits = bm25.search(c["question"], k=over_k)
        v_ids = _dedup_doc_ids(vhits, TOP_K)
        b_ids = _dedup_doc_ids(bhits, TOP_K)
        # fuse the fuller candidate lists, then truncate — mirrors retrieval._rrf_merge
        h_ids = _rrf_doc_ids(_dedup_doc_ids(vhits, over_k), _dedup_doc_ids(bhits, over_k), TOP_K)
        top_distance = vhits[0].distance if vhits else None
        per_v.append({"retrieved_ids": v_ids, "relevant_ids": c["relevant_doc_ids"], "top_distance": top_distance})
        per_b.append({"retrieved_ids": b_ids, "relevant_ids": c["relevant_doc_ids"], "top_distance": None})
        per_h.append({"retrieved_ids": h_ids, "relevant_ids": c["relevant_doc_ids"], "top_distance": top_distance})
        ok = "PASS" if set(h_ids) & set(c["relevant_doc_ids"]) else "MISS"
        print(f"  [{ok}] {c['question'][:60]}")

    rv = evaluation.aggregate_retrieval(per_v, TOP_K)
    rb = evaluation.aggregate_retrieval(per_b, TOP_K)
    rh = evaluation.aggregate_retrieval(per_h, TOP_K)
    report = {
        "type": "hr_live", "n_cases": len(cases), "top_k": TOP_K, "rrf_k": RRF_K,
        "index": f"{S.vector_bucket}/{S.hr_index}", "embedding_model": S.embedding_model_id,
        # hybrid is the shipped retriever → keep top-level keys as the headline metrics
        "recall_at_k": round(rh.recall_at_k, 3), "hit_rate": round(rh.hit_rate, 3), "mrr": round(rh.mrr, 3),
        "mean_top_distance": round(rh.mean_top_distance, 4) if rh.mean_top_distance is not None else None,
        "vector": {"recall_at_k": round(rv.recall_at_k, 3), "hit_rate": round(rv.hit_rate, 3), "mrr": round(rv.mrr, 3)},
        "bm25": {"recall_at_k": round(rb.recall_at_k, 3), "hit_rate": round(rb.hit_rate, 3), "mrr": round(rb.mrr, 3)},
        "hybrid": {"recall_at_k": round(rh.recall_at_k, 3), "hit_rate": round(rh.hit_rate, 3), "mrr": round(rh.mrr, 3)},
    }
    print(f"\nLIVE recall@{TOP_K} | hit@{TOP_K} | mrr")
    print(f"  vector : {rv.recall_at_k:.3f} | {rv.hit_rate:.3f} | {rv.mrr:.3f}")
    print(f"  bm25   : {rb.recall_at_k:.3f} | {rb.hit_rate:.3f} | {rb.mrr:.3f}")
    print(f"  hybrid : {rh.recall_at_k:.3f} | {rh.hit_rate:.3f} | {rh.mrr:.3f}  (shipped, RRF k={RRF_K})")
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description="Kai Components HR RAG retrieval evaluation")
    ap.add_argument("--live", action="store_true", help="run live (AWS) retrieval eval")
    ap.add_argument("--stamp", default="latest", help="results filename stamp (CI passes a git sha)")
    args = ap.parse_args()

    cases = load_cases()
    report = run_live(cases) if args.live else run_offline(cases)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f"{report['type']}_{args.stamp}.json"
    out.write_text(json.dumps(report, indent=2))
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
