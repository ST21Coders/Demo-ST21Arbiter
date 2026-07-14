"""Answer-quality evaluation — LLM-as-judge (HR) + deterministic numeric check (Sales).

For HR questions, generates a grounded answer and scores it 1-5 on faithfulness/correctness/
relevance with a judge model. For Sales aggregation questions, checks the numeric answer against
the known ground-truth value within tolerance (no judge needed for arithmetic).

Usage:
  python eval/run_answer_eval.py            # HR answer quality
  python eval/run_answer_eval.py --sales    # Sales numeric accuracy (needs Athena deployed)
"""

from __future__ import annotations

import argparse
import json
import pathlib

import arbiter_rag
from arbiter_rag import athena_sql, evaluation, retrieval
from arbiter_rag.config import get_settings

REPO = pathlib.Path(arbiter_rag.config.RAG_ROOT)
GOLDEN_DIR = REPO / "eval" / "golden"
RESULTS_DIR = REPO / "eval" / "results"


def eval_hr(settings) -> dict:
    cases = [json.loads(x) for x in (GOLDEN_DIR / "hr_qa.jsonl").read_text().splitlines() if x.strip()]
    clients = retrieval.Clients.build(settings)
    scores = {"faithfulness": [], "correctness": [], "relevance": []}
    for c in cases:
        ans = retrieval.answer(c["question"], settings.hr_index_name, settings, clients=clients)
        j = evaluation.judge_answer(c["question"], ans.answer, c["ground_truth"], settings)
        for k in scores:
            scores[k].append(float(j.get(k, 0)))
        print(f"  [{j.get('correctness','?')}/5] {c['question'][:60]}")
    return {"type": "answer_hr", "n": len(cases), **{k: sum(v) / len(v) for k, v in scores.items()}}


def eval_sales(settings) -> dict:
    cases = [json.loads(x) for x in (GOLDEN_DIR / "sales_qa.jsonl").read_text().splitlines() if x.strip()]
    numeric = [c for c in cases if c.get("expected_value") is not None]
    correct = 0
    for c in numeric:
        result = athena_sql.answer_sales_question(c["question"], settings)
        answer_text = " ".join(str(v) for row in result.rows for v in row.values())
        ok = evaluation.numeric_match(answer_text, float(c["expected_value"]), tolerance=0.02)
        correct += ok
        print(f"  [{'PASS' if ok else 'FAIL'}] {c['question'][:60]}")
    return {"type": "answer_sales", "n": len(numeric), "numeric_accuracy": correct / len(numeric) if numeric else 0.0}


def main() -> int:
    parser = argparse.ArgumentParser(description="HappyFeet answer-quality evaluation")
    parser.add_argument("--sales", action="store_true", help="run sales numeric eval (needs Athena)")
    parser.add_argument("--timestamp", default="latest")
    args = parser.parse_args()

    settings = get_settings()
    report = eval_sales(settings) if args.sales else eval_hr(settings)
    print(json.dumps(report, indent=2))

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / f"{report['type']}_{settings.env}_{args.timestamp}.json").write_text(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
