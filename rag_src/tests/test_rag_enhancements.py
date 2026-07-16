"""Offline tests for the reusable-module enhancements (no AWS required).

Covers the new engine surface added for the Unstructured + Structured specialists:
  * multi-format loaders  — iter_documents (txt/md/json/docx) + load_tabular (csv/xlsx/parquet)
  * BM25 lexical search   — arbiter_rag.lexical (ranks the exact-keyword doc first)
  * hybrid RRF fusion     — retrieval._rrf_merge (vector + lexical rank fusion)
  * generic serializer    — serialization.build_row_facts (per-row and grain modes)

Runnable two ways:
    rag_src/.venv/bin/python -m pytest rag_src/tests/test_rag_enhancements.py
    rag_src/.venv/bin/python rag_src/tests/test_rag_enhancements.py     # no pytest needed
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

_RAG_SRC = Path(__file__).resolve().parents[1]
if str(_RAG_SRC) not in sys.path:
    sys.path.insert(0, str(_RAG_SRC))

from arbiter_rag import lexical, loaders, retrieval, serialization  # noqa: E402


def test_iter_documents_multiformat() -> None:
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "a.txt").write_text("Plain text about leave policy and PTO accrual.")
        (root / "b.md").write_text("# Benefits\n\n401(k) match and HSA contribution.")
        (root / "c.json").write_text(json.dumps({"policy": {"name": "conduct", "esd": "wrist strap"}}))
        docs = loaders.iter_documents(root)
        by_id = {x["doc_id"]: x for x in docs}
        assert set(by_id) == {"a", "b", "c"}, by_id
        assert "PTO" in by_id["a"]["text"]
        assert "HSA" in by_id["b"]["text"]
        # JSON is flattened to dotted key: value lines
        assert "policy.esd: wrist strap" in by_id["c"]["text"]
        assert {x["doc_type"] for x in docs} == {"txt", "md", "json"}


def test_load_docx_if_available() -> None:
    try:
        import docx  # noqa: F401
    except ImportError:
        print("  (skipped docx: python-docx not installed)")
        return
    from docx import Document
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "policy.docx"
        doc = Document()
        doc.add_paragraph("Overtime is 1.5x over 40 hours per week.")
        doc.save(p)
        text = loaders.load_docx_text(p)
        assert "Overtime" in text and "1.5x" in text, text


def test_load_tabular_csv_parquet_xlsx() -> None:
    import pandas as pd
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        df = pd.DataFrame({"branch": ["HI001", "HI002"], "revenue": [10.0, 20.0]})
        df.to_csv(root / "a.csv", index=False)
        df.to_parquet(root / "b.parquet")
        df.to_excel(root / "c.xlsx", index=False)
        out = loaders.load_tabular(root)
        assert len(out) == 6, len(out)  # 3 files x 2 rows
        assert set(out.columns) == {"branch", "revenue"}
        # single-file path also works
        one = loaders.load_tabular(root / "a.csv")
        assert len(one) == 2


def test_bm25_ranks_keyword_first() -> None:
    records = [
        {"key": "k1", "text": "Paid time off and vacation accrual for associates.",
         "metadata": {"doc_id": "LEAVE"}},
        {"key": "k2", "text": "The company pays a SPIFF for each solar power kit sold.",
         "metadata": {"doc_id": "COMP"}},
        {"key": "k3", "text": "Medical and dental premium contributions.",
         "metadata": {"doc_id": "BEN"}},
    ]
    idx = lexical.build_index(records)
    hits = idx.search("What is the SPIFF for solar kits?", k=3)
    assert hits, "BM25 returned no hits"
    assert hits[0].key == "k2", [h.key for h in hits]  # exact rare token wins
    assert hits[0].metadata["doc_id"] == "COMP"
    # a query with no shared tokens returns nothing (score>0 filter)
    assert idx.search("zzz nonexistent qqq", k=3) == []


def test_rrf_merge_fuses_both_lists() -> None:
    class Hit:
        def __init__(self, key, metadata, distance=None):
            self.key, self.metadata, self.distance = key, metadata, distance

    vector = [Hit("a", {"doc_id": "A"}, 0.1), Hit("b", {"doc_id": "B"}, 0.2)]
    lex = [lexical.LexicalHit("b", 3.0, {"doc_id": "B"}), lexical.LexicalHit("c", 1.0, {"doc_id": "C"})]
    order, meta, dist, rrf = retrieval._rrf_merge(vector, lex, rrf_k=60)
    # 'b' appears in both lists → highest fused score, ranks first
    assert order[0] == "b", (order, rrf)
    assert set(order) == {"a", "b", "c"}
    assert dist["a"] == 0.1 and meta["c"]["doc_id"] == "C"


def test_build_row_facts_per_row_and_grain() -> None:
    import pandas as pd
    df = pd.DataFrame({
        "branch": ["HI001", "HI001", "HI002"],
        "category": ["solar", "solar", "marine"],
        "revenue": [10.0, 5.0, 8.0],
    })
    per_row = serialization.build_row_facts(df, "demo")
    assert len(per_row) == 3
    assert per_row[0]["metadata"]["metric_type"] == "row"
    assert "fact_text" in per_row[0]["metadata"] and "source_uri" in per_row[0]["metadata"]

    grouped = serialization.build_row_facts(df, "demo", grain=["branch", "category"])
    assert len(grouped) == 2  # (HI001,solar) aggregated + (HI002,marine)
    hi001 = next(r for r in grouped if r["key"].endswith("solar"))
    assert "15.00" in hi001["text"], hi001["text"]  # 10 + 5 summed
    assert hi001["metadata"]["branch"] == "HI001"  # grain columns stay filterable (stringified)
    assert hi001["metadata"]["metric_type"] == "row_group"


def _main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_main())
