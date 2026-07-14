"""Serialize STRUCTURED sales rows into natural-language 'facts' for vector storage.

This is the structured-vs-unstructured teaching point: tabular data must be turned into
prose before it can be embedded. The result answers FUZZY questions well ("how did marine
electronics do at the Kailua store?") but is UNRELIABLE for exact aggregation ("total
revenue across all branches") — for those, route to athena_sql. The agent picks the tool.

Facts are aggregated to branch x product_category (the Hawaii sample is a single business
day, so there is no fiscal-year dimension) so each vector is a meaningful summary rather
than a noisy single transaction.
"""

from __future__ import annotations

import re
from typing import Any

# Columns the fact grain aggregates on (kept in sync with the Glue table for reference).
GRAIN_COLUMNS = ["branch_id", "city", "island", "category"]


def _slug(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-") or "na"


def build_sales_facts(sales_df: Any, chunking_version: str = "v1") -> list[dict[str, Any]]:
    """Aggregate the raw Hawaii sales frame and return embeddable fact records.

    Each record: {"key", "text", "metadata"} where metadata matches the S3 Vectors
    `sales-facts` index schema (fact_text/title/source_uri are non-filterable).

    Grain = branch x category. Expects the transaction-level Hawaii schema with at least
    columns: branch_id, city, island, category, quantity, revenue, cost, gross_margin,
    timestamp.
    """
    # Single-day dataset: derive the business date from the data (don't hardcode it).
    try:
        sale_date = str(sales_df["timestamp"].iloc[0])[:10]
    except (KeyError, IndexError):
        sale_date = "unknown"

    agg = (
        sales_df.groupby(GRAIN_COLUMNS, as_index=False)
        .agg(
            units_sold=("quantity", "sum"),
            revenue=("revenue", "sum"),
            cost=("cost", "sum"),
            gross_margin=("gross_margin", "sum"),
            transactions=("transaction_id", "count"),
            distinct_skus=("sku", "nunique"),
        )
    )

    records: list[dict[str, Any]] = []
    for row in agg.itertuples(index=False):
        units = int(row.units_sold)
        revenue = float(row.revenue)
        cost = float(row.cost)
        margin = float(row.gross_margin)
        txns = int(row.transactions)
        skus = int(row.distinct_skus)
        fact = (
            f"On {sale_date}, the {row.city} branch ({row.branch_id}) on {row.island} sold "
            f"{units:,} units of {row.category} products across {txns} transactions "
            f"({skus} distinct SKUs), generating ${revenue:,.0f} in revenue at a gross "
            f"margin of ${margin:,.0f} (cost of goods ${cost:,.0f})."
        )
        cat_slug = str(row.category).lower().replace(" ", "-").replace("/", "-")
        key = f"sales-{str(row.branch_id).lower()}-{cat_slug}"
        records.append(
            {
                "key": key,
                "text": fact,
                "metadata": {
                    "domain": "sales",
                    "branch_id": row.branch_id,
                    "city": row.city,
                    "island": row.island,
                    "category": row.category,
                    "metric_type": "branch_category_day",
                    "chunking_version": chunking_version,
                    # non-filterable (declared in vectors.SALES_NON_FILTERABLE_KEYS):
                    "fact_text": fact,
                    "title": f"{row.city} ({row.branch_id}) — {row.category}",
                    "source_uri": f"s3://dev-st21arbiter-poc-processed/structured/hawaii_sales/",
                },
            }
        )
    return records


def build_row_facts(
    df: Any,
    dataset_id: str,
    *,
    grain: list[str] | None = None,
    source_uri: str = "",
    chunking_version: str = "v1",
    max_rows: int = 5000,
) -> list[dict[str, Any]]:
    """Serialize an ARBITRARY tabular frame into embeddable natural-language facts.

    Generalizes build_sales_facts for the Structured Analytics path (any schema):
      * grain=None      -> one fact per row (capped at max_rows).
      * grain=[cols]    -> group by those columns, SUM the numeric columns, one fact per group.

    Each record: {"key", "text", "metadata"} where fact_text/title/source_uri are the
    non-filterable keys (vectors.SALES_NON_FILTERABLE_KEYS) and the grain columns stay
    filterable. The Hawaii demo keeps build_sales_facts; this is the reusable default.
    """
    import pandas as pd  # lazy (ingest-time only)

    default_uri = source_uri or f"s3://structured/{dataset_id}/"

    def _record(key: str, fact: str, filterable: dict[str, Any], metric_type: str) -> dict[str, Any]:
        meta: dict[str, Any] = {
            "domain": dataset_id,
            "metric_type": metric_type,
            "chunking_version": chunking_version,
            # non-filterable (declared in vectors.SALES_NON_FILTERABLE_KEYS):
            "fact_text": fact,
            "title": fact[:80],
            "source_uri": default_uri,
        }
        for col, val in filterable.items():
            meta[col] = str(val)  # keep S3 Vectors metadata JSON-safe (stringify group keys)
        return {"key": key[:400], "text": fact, "metadata": meta}

    records: list[dict[str, Any]] = []
    if grain:
        present = [c for c in grain if c in df.columns]
        if not present:
            raise ValueError(f"None of grain={grain} present in columns {list(df.columns)}")
        numeric = [
            c for c in df.columns if c not in present and pd.api.types.is_numeric_dtype(df[c])
        ]
        frame = (
            df.groupby(present, as_index=False)[numeric].sum()
            if numeric
            else df[present].drop_duplicates().reset_index(drop=True)
        )
        for row in frame.to_dict("records"):
            group_desc = ", ".join(f"{c}={row[c]}" for c in present)
            metric_desc = "; ".join(f"total {c}={float(row[c]):,.2f}" for c in numeric)
            fact = (
                f"In dataset {dataset_id}, for {group_desc}: {metric_desc}."
                if numeric
                else f"In dataset {dataset_id}: {group_desc}."
            )
            key = f"{dataset_id}-" + "-".join(_slug(row[c]) for c in present)
            records.append(_record(key, fact, {c: row[c] for c in present}, "row_group"))
    else:
        for i, row in enumerate(df.head(max_rows).to_dict("records")):
            pairs = "; ".join(f"{c}: {row[c]}" for c in df.columns)
            fact = f"In dataset {dataset_id}, record {i}: {pairs}."
            records.append(_record(f"{dataset_id}-row-{i}", fact, {}, "row"))
    return records
