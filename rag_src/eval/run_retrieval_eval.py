"""Retrieval evaluation — cheap, deterministic, no LLM. Run after any chunking/embedding change.

Queries the live HR index for every golden question and reports recall@k, MRR, hit-rate, and
mean top-distance. Writes a timestamped JSON to eval/results/ for the improvement-loop trend.

Usage:
  python eval/run_retrieval_eval.py                 # uses config + eval/golden/hr_qa.jsonl
  python eval/run_retrieval_eval.py --no-rerank
"""

from __future__ import annotations

import argparse
import json
import pathlib

import arbiter_rag
from arbiter_rag import evaluation, retrieval
from arbiter_rag.config import get_settings

REPO = pathlib.Path(arbiter_rag.config.RAG_ROOT)
GOLDEN = REPO / "eval" / "golden" / "hr_qa.jsonl"
RESULTS_DIR = REPO / "eval" / "results"


def load_cases(path: pathlib.Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description="HappyFeet retrieval evaluation")
    parser.add_argument("--golden", default=str(GOLDEN))
    parser.add_argument("--no-rerank", action="store_true", help="disable reranking")
    parser.add_argument("--timestamp", default="", help="stamp for the results filename (CI passes one)")
    args = parser.parse_args()

    settings = get_settings()
    cases = load_cases(pathlib.Path(args.golden))
    clients = retrieval.Clients.build(settings)
    use_rerank = not args.no_rerank

    per_case = []
    for c in cases:
        ctxs = retrieval.retrieve(
            c["question"], settings.hr_index_name, settings, clients=clients, use_rerank=use_rerank
        )
        per_case.append(
            {
                "retrieved_ids": [x.doc_id for x in ctxs],
                "relevant_ids": c["relevant_doc_ids"],
                "top_distance": ctxs[0].distance if ctxs else None,
            }
        )

    report = evaluation.aggregate_retrieval(per_case, settings.retrieval_top_k)
    print(f"env={settings.env} rerank={use_rerank} n={report.n} k={report.k}")
    print(f"  recall@k   : {report.recall_at_k:.3f}")
    print(f"  mrr        : {report.mrr:.3f}")
    print(f"  hit_rate   : {report.hit_rate:.3f}")
    print(f"  mean_dist  : {report.mean_top_distance}")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = {
        "type": "retrieval",
        "env": settings.env,
        "rerank": use_rerank,
        "embedding_model": settings.embedding_model_id,
        "chunk_strategy": settings.chunk_strategy,
        "recall_at_k": report.recall_at_k,
        "mrr": report.mrr,
        "hit_rate": report.hit_rate,
        "mean_top_distance": report.mean_top_distance,
    }
    stamp = args.timestamp or "latest"
    (RESULTS_DIR / f"retrieval_{settings.env}_{stamp}.json").write_text(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
