"""Document loaders — turn the raw corpus into text + metadata for ingestion.

Only used at INGEST time (notebooks, pipelines), never by the query-only agent, so
pypdf/pandas are imported lazily to keep the agent runtime dependency-light.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_EFFECTIVE_RE = re.compile(r"Effective\s+(\d{4}-\d{2}-\d{2})")
# Repeated page footer / subtitle noise we don't want in the retrievable text.
_FOOTER_RE = re.compile(r"FICTIONAL SAMPLE|Page\s+\d+", re.IGNORECASE)


def load_pdf_text(path: str | Path) -> str:
    """Extract all text from a PDF (requires the `data`/`notebook` extra: pypdf)."""
    from pypdf import PdfReader  # lazy

    reader = PdfReader(str(path))
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n\n".join(pages).strip()


def _clean_policy_text(text: str) -> str:
    """Drop the repeated page-footer lines so they don't pollute chunks/embeddings."""
    kept = [ln for ln in text.splitlines() if not _FOOTER_RE.search(ln)]
    # Collapse the runs of blank lines left behind so paragraph splitting stays clean.
    cleaned = "\n".join(kept)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def iter_hr_documents(pdf_dir: str | Path) -> list[dict[str, Any]]:
    """Load every HR-policy PDF into a record with text + filterable metadata.

    doc_id and policy_category come from the filename (e.g. HR-LEAVE-001_leave.pdf);
    title/effective_date are parsed from the document body. These map directly onto
    the S3 Vectors `hr-policies` index schema.
    """
    pdf_dir = Path(pdf_dir)
    docs: list[dict[str, Any]] = []
    for path in sorted(pdf_dir.glob("*.pdf")):
        stem = path.stem  # e.g. "HR-LEAVE-001_leave"
        doc_id, _, category = stem.partition("_")
        text = _clean_policy_text(load_pdf_text(path))
        # Title = first content line that isn't the "Document ... Category ..." subtitle.
        title = next(
            (
                ln.strip()
                for ln in text.splitlines()
                if ln.strip() and not ln.strip().startswith("Document ")
            ),
            doc_id,
        )
        eff = _EFFECTIVE_RE.search(text)
        docs.append(
            {
                "doc_id": doc_id,
                "title": title,
                "policy_category": category or "general",
                "effective_date": eff.group(1) if eff else "",
                "state": "US-ALL",          # policies apply to all US outlets in this demo
                "access_level": "employee",  # RBAC tag injected server-side on retrieval
                "source_file": path.name,
                "source_uri": f"file://{path}",
                "text": text,
            }
        )
    return docs


def load_sales_dataframe(path: str | Path, sheet: str = "Sales"):
    """Load a sheet of the sales workbook into a pandas DataFrame (requires pandas)."""
    import pandas as pd  # lazy

    return pd.read_excel(path, sheet_name=sheet)


# Expected column contract of the Hawaiian-electronics sales CSVs (one row per transaction).
HAWAII_SALES_COLUMNS = [
    "transaction_id", "branch_id", "city", "island", "state", "timestamp", "sku",
    "product_description", "category", "quantity", "unit_cost", "unit_price", "revenue",
    "cost", "gross_margin", "salesperson", "customer_type", "payment_method",
    "inventory_remaining",
]
_NUMERIC_COLS = [
    "quantity", "unit_cost", "unit_price", "revenue", "cost", "gross_margin",
    "inventory_remaining",
]


def load_hawaii_sales(csv_dir: str | Path):
    """Concatenate the per-branch Hawaii sales CSVs into one typed DataFrame.

    `csv_dir` holds files like `HI001_Honolulu_HI_sales_2026-06-30.csv` — one per branch,
    all sharing HAWAII_SALES_COLUMNS. Numeric columns are coerced so downstream
    aggregation (serialization + the pandas ground-truth used by eval) is exact. Requires
    pandas (ingest-time only; the query agent never imports this module).
    """
    import pandas as pd  # lazy

    csv_dir = Path(csv_dir)
    paths = sorted(csv_dir.glob("*.csv"))
    if not paths:
        raise FileNotFoundError(f"No CSV files found in {csv_dir}")
    frames = [pd.read_csv(p) for p in paths]
    df = pd.concat(frames, ignore_index=True)

    missing = [c for c in HAWAII_SALES_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"Sales CSVs missing expected columns: {missing}")
    for col in _NUMERIC_COLS:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df
