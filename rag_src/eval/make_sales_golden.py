"""Generate the Hawaii sales golden Q&A set from the dataset's real pandas ground truth.

Every `expected_value` is computed here from the actual CSVs, so the evaluation's numeric
accuracy check compares the agent's SQL answer against a true figure (not a guess). Semantic
cases carry `relevant_keys` — the branch×category fact keys a good retrieval must surface.

Rerun this whenever the dataset changes:
  python eval/make_sales_golden.py                 # uses data/Hawaii_Electronics_100
  DATASET=sample python eval/make_sales_golden.py  # uses data/Hawaii_Sample_Sales

Writes eval/golden/sales_hawaii_qa.jsonl.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import arbiter_rag
from arbiter_rag import loaders, serialization
from arbiter_rag.config import DATA_ROOT, RAG_ROOT

DATASET = os.environ.get("DATASET", "large").lower()
DATASET_DIR = DATA_ROOT / ("Hawaii_Electronics_100" if DATASET == "large" else "Hawaii_Sample_Sales")
OUT = RAG_ROOT / "eval" / "golden" / "sales_hawaii_qa.jsonl"


def _keys_for(facts, *, island=None, category=None, branch_id=None):
    """Fact keys whose metadata matches the given filters (the retrieval targets)."""
    out = []
    for f in facts:
        m = f["metadata"]
        if island and m["island"] != island:
            continue
        if category and m["category"] != category:
            continue
        if branch_id and m["branch_id"] != branch_id:
            continue
        out.append(f["key"])
    return out


def main() -> int:
    if not DATASET_DIR.is_dir():
        print(f"dataset dir not found: {DATASET_DIR}", file=sys.stderr)
        return 1
    df = loaders.load_hawaii_sales(DATASET_DIR)
    facts = serialization.build_sales_facts(df)

    top_cat = df.groupby("category").revenue.sum().idxmax()
    top_cat_rev = float(df.groupby("category").revenue.sum().max())
    top_branch = df.groupby(["branch_id", "city"]).quantity.sum().idxmax()  # (branch_id, city)
    top_branch_units = int(df.groupby("branch_id").quantity.sum().max())

    cases: list[dict] = [
        # ── Exact aggregation → expected_tool = sales_sql, numeric expected_value ──
        {"question": "What was the total revenue across all branches?",
         "expected_tool": "sales_sql", "expected_value": round(float(df.revenue.sum()), 2),
         "ground_truth": f"About ${df.revenue.sum():,.2f} in total revenue."},
        {"question": "How many units were sold in total across all branches?",
         "expected_tool": "sales_sql", "expected_value": int(df.quantity.sum()),
         "ground_truth": f"{int(df.quantity.sum()):,} units."},
        {"question": "What was the total gross margin across all branches?",
         "expected_tool": "sales_sql", "expected_value": round(float(df.gross_margin.sum()), 2),
         "ground_truth": f"About ${df.gross_margin.sum():,.2f} gross margin."},
        {"question": "How many transactions were recorded in total?",
         "expected_tool": "sales_sql", "expected_value": int(len(df)),
         "ground_truth": f"{len(df):,} transactions."},
        {"question": "What was the total revenue for the Microcontrollers category?",
         "expected_tool": "sales_sql",
         "expected_value": round(float(df[df.category == "Microcontrollers"].revenue.sum()), 2),
         "ground_truth": f"About ${df[df.category=='Microcontrollers'].revenue.sum():,.2f}."},
        {"question": "How many units of Marine Electronics were sold across all branches?",
         "expected_tool": "sales_sql",
         "expected_value": int(df[df.category == "Marine Electronics"].quantity.sum()),
         "ground_truth": f"{int(df[df.category=='Marine Electronics'].quantity.sum()):,} units."},
        {"question": "What was the total revenue on Maui?",
         "expected_tool": "sales_sql",
         "expected_value": round(float(df[df.island == "Maui"].revenue.sum()), 2),
         "ground_truth": f"About ${df[df.island=='Maui'].revenue.sum():,.2f} on Maui."},
        {"question": "Which product category generated the most revenue, and how much?",
         "expected_tool": "sales_sql", "expected_value": round(top_cat_rev, 2),
         "ground_truth": f"{top_cat}, with about ${top_cat_rev:,.2f}."},
        {"question": "Which branch sold the most units, and how many?",
         "expected_tool": "sales_sql", "expected_value": top_branch_units,
         "ground_truth": f"{top_branch[1]} ({top_branch[0]}), about {top_branch_units:,} units."},
        {"question": "What was the average revenue per transaction?",
         "expected_tool": "sales_sql", "expected_value": round(float(df.revenue.mean()), 2),
         "ground_truth": f"About ${df.revenue.mean():,.2f} per transaction."},

        # ── Fuzzy / descriptive → expected_tool = sales_semantic, relevant_keys ──
        {"question": "How did Marine Electronics perform at the Kailua branch?",
         "expected_tool": "sales_semantic", "expected_value": None,
         "relevant_keys": _keys_for(facts, branch_id="HI009", category="Marine Electronics"),
         "ground_truth": "A qualitative summary of Marine Electronics at the Kailua (HI009) branch."},
        {"question": "How are microcontrollers selling at the Honolulu branch?",
         "expected_tool": "sales_semantic", "expected_value": None,
         "relevant_keys": _keys_for(facts, branch_id="HI001", category="Microcontrollers"),
         "ground_truth": "A qualitative summary of Microcontrollers at the Honolulu (HI001) branch."},
        {"question": "Tell me about Solar Power sales on Maui.",
         "expected_tool": "sales_semantic", "expected_value": None,
         "relevant_keys": _keys_for(facts, island="Maui", category="Solar Power"),
         "ground_truth": "A qualitative summary of Solar Power performance across Maui branches."},
    ]

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w") as fh:
        for c in cases:
            fh.write(json.dumps(c) + "\n")
    n_sql = sum(1 for c in cases if c["expected_tool"] == "sales_sql")
    n_sem = len(cases) - n_sql
    print(f"wrote {OUT} — {len(cases)} cases ({n_sql} sql, {n_sem} semantic) from {DATASET_DIR.name}")
    # Sanity: warn on any semantic case whose retrieval targets don't exist.
    for c in cases:
        if c["expected_tool"] == "sales_semantic" and not c.get("relevant_keys"):
            print(f"  WARNING: no relevant_keys for: {c['question']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
