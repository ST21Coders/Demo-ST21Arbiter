"""Evaluate the Hawaii Sales RAG — routing, numeric accuracy, and retrieval — at scale.

Three signals (see eval/golden/sales_hawaii_qa.jsonl):
  * routing accuracy  — does a question get sent to the right tool (sql vs semantic)?
  * numeric accuracy  — does the SQL path's answer match the pandas ground truth?
  * retrieval quality  — recall@k / hit-rate of the semantic path on the right facts.

Modes:
  python eval/run_sales_eval.py                # OFFLINE: golden-set integrity + fact coverage
                                               #   + a deterministic routing-heuristic preview. No AWS.
  python eval/run_sales_eval.py --live         # LIVE: numeric accuracy over Athena + retrieval over
                                               #   the S3 Vectors sales-facts index. Needs creds +
                                               #   provisioned resources (see rag_instructions.md).

Live env (same names as the deployed agent): SALES_VECTOR_BUCKET, SALES_VECTOR_INDEX, GLUE_DATABASE,
GLUE_TABLE, ATHENA_WORKGROUP, ATHENA_OUTPUT, EMBEDDING_MODEL_ID, MODEL_ID.

Results are written to eval/results/sales_<mode>_<stamp>.json so you can trend runs across
enhancements (change fact grain / top_k / model → re-run → compare).
"""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

import arbiter_rag
from arbiter_rag import evaluation, loaders, serialization
from arbiter_rag.config import DATA_ROOT, RAG_ROOT, Settings

GOLDEN = RAG_ROOT / "eval" / "golden" / "sales_hawaii_qa.jsonl"
RESULTS_DIR = RAG_ROOT / "eval" / "results"
DATASET = os.environ.get("DATASET", "large").lower()
DATASET_DIR = DATA_ROOT / ("Hawaii_Electronics_100" if DATASET == "large" else "Hawaii_Sample_Sales")
TOP_K = int(os.environ.get("EVAL_TOP_K", "5"))

# Keyword heuristic for the OFFLINE routing preview. The LIVE agent uses the LLM router in
# its SYSTEM_PROMPT; this deterministic proxy is a cheap lower-bound sanity check + doc of
# the intended split.
_SQL_HINTS = re.compile(
    r"\b(how many|how much|total|sum|count|average|avg|most|least|top|highest|lowest|"
    r"number of|per transaction|which (branch|category|island).*(most|least|highest))\b", re.I)
_SEM_HINTS = re.compile(r"\b(how did|how are|tell me about|perform|performance|describe|what kind)\b", re.I)


def predict_tool(question: str) -> str:
    """Deterministic routing proxy: sql for aggregation cues, else semantic."""
    if _SEM_HINTS.search(question) and not _SQL_HINTS.search(question):
        return "sales_semantic"
    if _SQL_HINTS.search(question):
        return "sales_sql"
    return "sales_semantic"


def load_cases() -> list[dict]:
    if not GOLDEN.exists():
        raise SystemExit(f"golden set missing: {GOLDEN} (run eval/make_sales_golden.py)")
    return [json.loads(l) for l in GOLDEN.read_text().splitlines() if l.strip()]


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
        rerank_enabled=False, rerank_model_id="amazon.rerank-v1:0", rerank_candidates_k=20,
        rerank_top_k=5, retrieval_top_k=TOP_K, chunk_strategy="semantic", chunk_max_chars=1200,
        chunk_overlap_chars=200, chunking_version="v1",
        vector_bucket=os.environ.get("SALES_VECTOR_BUCKET", f"{PREFIX}-sales-vectors"),
        hr_index="hr-policies", sales_index=os.environ.get("SALES_VECTOR_INDEX", "sales-facts"),
        distance_metric="cosine",
        glue_database=os.environ.get("GLUE_DATABASE", f"{ENV}_{PROJECT}_structured".replace("-", "_")),
        glue_table=os.environ.get("GLUE_TABLE", "hawaii_sales"),
        athena_workgroup=os.environ.get("ATHENA_WORKGROUP", f"{PREFIX}-wg"),
        athena_output_prefix=os.environ.get("ATHENA_OUTPUT", ""),
        max_scanned_bytes=1024 * 1024 * 1024, guardrails_enabled=False, guardrail_id="",
        guardrail_version="DRAFT", ingest_batch_size=500, log_level="INFO",
    )


def run_offline(cases: list[dict]) -> dict:
    """No AWS: golden-set integrity, fact coverage, routing-heuristic accuracy."""
    df = loaders.load_hawaii_sales(DATASET_DIR)
    fact_keys = {f["key"] for f in serialization.build_sales_facts(df)}

    coverage_ok, coverage_total, missing = 0, 0, []
    routing_correct = 0
    for c in cases:
        if predict_tool(c["question"]) == c["expected_tool"]:
            routing_correct += 1
        if c["expected_tool"] == "sales_semantic":
            for k in c.get("relevant_keys", []):
                coverage_total += 1
                if k in fact_keys:
                    coverage_ok += 1
                else:
                    missing.append(k)

    report = {
        "type": "sales_offline", "dataset": DATASET_DIR.name, "n_cases": len(cases),
        "routing_heuristic_accuracy": round(routing_correct / len(cases), 3) if cases else 0.0,
        "fact_coverage": round(coverage_ok / coverage_total, 3) if coverage_total else 1.0,
        "missing_relevant_keys": missing,
        "n_facts": len(fact_keys),
    }
    print(f"OFFLINE eval on {DATASET_DIR.name}: {len(cases)} cases, {len(fact_keys)} facts")
    print(f"  routing-heuristic accuracy : {report['routing_heuristic_accuracy']:.3f}")
    print(f"  fact coverage (semantic)   : {report['fact_coverage']:.3f}"
          + (f"  MISSING: {missing}" if missing else ""))
    return report


def run_live(cases: list[dict]) -> dict:
    """AWS: numeric accuracy over Athena + retrieval recall@k over S3 Vectors."""
    from arbiter_rag import athena_sql, embeddings, vectors  # lazy (need creds)
    S = _settings_from_env()
    rt = embeddings.make_runtime_client(S.region)
    vx = vectors.make_client(S.region)

    sql_cases = [c for c in cases if c["expected_tool"] == "sales_sql" and c.get("expected_value") is not None]
    sem_cases = [c for c in cases if c["expected_tool"] == "sales_semantic"]

    numeric_correct = 0
    for c in sql_cases:
        try:
            sql = athena_sql.generate_sql(c["question"], S)
            res = athena_sql.run_query(sql, S, database=S.glue_database, workgroup=S.athena_workgroup,
                                       output_location=S.athena_output_prefix or None)
            answer = " ".join(str(v) for row in res.rows for v in row.values())
            ok = evaluation.numeric_match(answer, float(c["expected_value"]), tolerance=0.02)
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"  [ERR] {c['question'][:50]}: {e}")
        numeric_correct += ok
        print(f"  [{'PASS' if ok else 'FAIL'}] {c['question'][:60]}")

    recalls, hits = [], []
    for c in sem_cases:
        q_vec = embeddings.embed_text(c["question"], S, rt)
        hlist = vectors.query(vx, S.vector_bucket, S.sales_index, q_vec, top_k=TOP_K)
        retrieved = [h.key for h in hlist]
        rel = c.get("relevant_keys", [])
        recalls.append(evaluation.recall_at_k(retrieved, rel, TOP_K))
        hits.append(evaluation.hit_at_k(retrieved, rel, TOP_K))
        print(f"  recall@{TOP_K}={recalls[-1]:.2f}  {c['question'][:55]}")

    report = {
        "type": "sales_live", "dataset": DATASET_DIR.name, "top_k": TOP_K,
        "model": S.generation_model_id, "embedding_model": S.embedding_model_id,
        "numeric_accuracy": round(numeric_correct / len(sql_cases), 3) if sql_cases else None,
        "n_sql": len(sql_cases),
        "retrieval_recall_at_k": round(sum(recalls) / len(recalls), 3) if recalls else None,
        "retrieval_hit_rate": round(sum(hits) / len(hits), 3) if hits else None,
        "n_semantic": len(sem_cases),
    }
    print(f"\nLIVE: numeric_accuracy={report['numeric_accuracy']} "
          f"recall@{TOP_K}={report['retrieval_recall_at_k']} hit_rate={report['retrieval_hit_rate']}")
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description="Hawaii Sales RAG evaluation")
    ap.add_argument("--live", action="store_true", help="run live (AWS) numeric + retrieval eval")
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
