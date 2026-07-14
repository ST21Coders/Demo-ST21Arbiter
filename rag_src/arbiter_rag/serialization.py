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

from typing import Any

# Columns the fact grain aggregates on (kept in sync with the Glue table for reference).
GRAIN_COLUMNS = ["branch_id", "city", "island", "category"]


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
