"""Text-to-SQL over the structured sales table (the accurate-aggregation path).

Why this exists: embedding-based retrieval CANNOT reliably sum, count, or rank. For
"total 2025 revenue" or "top 5 stores" the agent must query the real table. This module
generates SQL from a question, VALIDATES it (read-only, single-statement, table
allowlist), and runs it through a read-only Athena workgroup with a scan cap.

Security: generated SQL is an injection surface. validate_sql() is the guard and is
unit-tested (tests/unit/test_athena_sql_guard.py). Never relax it.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

from .config import Settings, get_settings
from .generation import generate

# Schema handed to the model so it writes correct SQL. Mirror of the Glue/Athena table
# (the Hawaiian-electronics sales export; one row per transaction, single business day).
TABLE_SCHEMA = """
Table: {table}
Columns:
  transaction_id (string)      -- e.g. 'HI001-TX-0001'
  branch_id (string)           -- e.g. 'HI001'
  city (string)                -- e.g. 'Honolulu'
  island (string)              -- Oahu | Maui | Hawaii Island | Kauai | Molokai | Lanai
  state (string)               -- always 'HI'
  timestamp (string)           -- 'YYYY-MM-DD HH:MM:SS' (all rows are 2026-06-30)
  sku (string)                 -- e.g. 'ARD-UNO'
  product_description (string) -- e.g. 'Arduino Uno Compatible'
  category (string)            -- Microcontrollers | Sensors | Power | Semiconductors |
                               --   Connectors | Capacitors | LEDs | Tools | Resistors |
                               --   Marine Electronics | Batteries | Solar Power | Relays |
                               --   Integrated Circuits
  quantity (int)               -- units sold in the transaction
  unit_cost (double)
  unit_price (double)
  revenue (double)             -- quantity * unit_price
  cost (double)                -- quantity * unit_cost
  gross_margin (double)        -- revenue - cost
  salesperson (string)
  customer_type (string)       -- Commercial | Government | Education | Retail | Marine |
                               --   Maker/Hobbyist | Tourism/Hospitality
  payment_method (string)
  inventory_remaining (int)
Notes: there is no fiscal_year/quarter/month column (single-day snapshot). Aggregate
revenue with SUM(revenue), units with SUM(quantity), margin with SUM(gross_margin).
""".strip()

_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|GRANT|REVOKE|TRUNCATE|MERGE|"
    r"ATTACH|COPY|UNLOAD|EXECUTE|CALL|SET|USE|DESCRIBE|MSCK|LOAD)\b",
    re.IGNORECASE,
)
_TABLE_REF = re.compile(r"\b(?:FROM|JOIN)\s+([A-Za-z0-9_.\"]+)", re.IGNORECASE)
# CTE names defined by `WITH name AS (...)` or `, name AS (...)` — legitimate, not base tables.
_CTE_NAME = re.compile(r"(?:\bWITH|,)\s+([A-Za-z0-9_]+)\s+AS\s*\(", re.IGNORECASE)


class SqlValidationError(ValueError):
    """Raised when generated SQL fails the read-only / allowlist guard."""


@dataclass
class SqlResult:
    sql: str
    columns: list[str]
    rows: list[dict[str, Any]]
    scanned_bytes: int


def validate_sql(sql: str, allowed_table: str) -> str:
    """Return normalized SQL if it is a single read-only SELECT over the allowed table.

    Rejects DDL/DML, multi-statement input, and references to any other table.
    """
    cleaned = sql.strip().rstrip(";").strip()
    if not cleaned:
        raise SqlValidationError("empty SQL")
    if ";" in cleaned:
        raise SqlValidationError("multiple statements are not allowed")
    lowered = cleaned.lstrip("(").lower()
    if not (lowered.startswith("select") or lowered.startswith("with")):
        raise SqlValidationError("only SELECT/WITH queries are allowed")
    if _FORBIDDEN.search(cleaned):
        raise SqlValidationError("statement contains a forbidden (write/DDL) keyword")
    referenced = {m.group(1).split(".")[-1].strip('"').lower() for m in _TABLE_REF.finditer(cleaned)}
    cte_names = {m.group(1).lower() for m in _CTE_NAME.finditer(cleaned)}
    allowed = allowed_table.split(".")[-1].strip('"').lower()
    unknown = referenced - {allowed} - cte_names
    if unknown:
        raise SqlValidationError(f"query references non-allowlisted table(s): {sorted(unknown)}")
    return cleaned


def generate_sql(question: str, settings: Settings | None = None, *, schema: str | None = None) -> str:
    """Ask the LLM to translate a question into a single Athena SQL SELECT.

    `schema` overrides the frozen `hawaii_sales` `TABLE_SCHEMA` with a caller-supplied schema
    block (e.g. the real Glue columns of an arbitrary group table). When omitted, the default
    Hawaii schema is used so the existing sales route is unchanged (notebook == production).
    """
    settings = settings or get_settings()
    system = (
        "You are a careful analytics engineer. Translate the user's question into ONE "
        "read-only Presto/Trino SQL SELECT statement for Amazon Athena. Use ONLY the table "
        "and columns provided. When a column lists its distinct values (e.g. `-- values: …`), "
        "filter using one of those EXACT stored values — map the user's wording to the closest "
        "listed value (e.g. a state named in full → its 2-letter code if that is what is listed) "
        "and do not invent a literal that is not present. Return SQL ONLY — no prose, no markdown "
        "fences, no semicolon. Always add a sensible LIMIT (<= 100) unless the question asks for a "
        "single aggregate."
    )
    schema = schema or TABLE_SCHEMA.format(table=settings.glue_table)
    prompt = f"{schema}\n\nQuestion: {question}\n\nSQL:"
    raw = generate(prompt, system=system, settings=settings).text
    # Strip accidental markdown fences.
    return re.sub(r"^```(?:sql)?|```$", "", raw.strip(), flags=re.IGNORECASE | re.MULTILINE).strip()


def run_query(
    sql: str,
    settings: Settings | None = None,
    client: Any | None = None,
    timeout_s: int = 60,
    *,
    database: str | None = None,
    workgroup: str | None = None,
    output_location: str | None = None,
) -> SqlResult:
    """Execute validated SQL against the read-only Athena workgroup and return rows.

    `database`/`workgroup`/`output_location` override the env-suffixed Settings names
    (`glue_database_name`/`athena_workgroup_name`/`athena_output_prefix`). Callers running
    against resources whose names don't follow the `<name>-<env>` suffix convention (e.g.
    ARBITER's dash-prefixed `dev-st21arbiter-poc-wg`) pass explicit values here. Notebooks
    that omit them keep the original suffix-based behaviour.
    """
    import boto3  # lazy; the query-only agent path needs it, notebooks import at call time

    settings = settings or get_settings()
    validated = validate_sql(sql, settings.glue_table)
    client = client or boto3.client("athena", region_name=settings.region)

    start_kwargs: dict[str, Any] = {
        "QueryString": validated,
        "QueryExecutionContext": {"Database": database or settings.glue_database_name},
        "WorkGroup": workgroup or settings.athena_workgroup_name,
    }
    # The workgroup enforces its own result location; only send one if explicitly configured.
    out_loc = output_location or settings.athena_output_prefix
    if out_loc:
        start_kwargs["ResultConfiguration"] = {"OutputLocation": out_loc}

    qid = client.start_query_execution(**start_kwargs)["QueryExecutionId"]

    deadline = time.monotonic() + timeout_s
    while True:
        info = client.get_query_execution(QueryExecutionId=qid)["QueryExecution"]
        state = info["Status"]["State"]
        if state in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        if time.monotonic() > deadline:
            raise TimeoutError(f"Athena query {qid} did not finish within {timeout_s}s")
        time.sleep(1.0)

    if state != "SUCCEEDED":
        reason = info["Status"].get("StateChangeReason", "unknown")
        raise RuntimeError(f"Athena query {state}: {reason}")

    scanned = int(info.get("Statistics", {}).get("DataScannedInBytes", 0))
    if scanned > settings.max_scanned_bytes:
        raise RuntimeError(f"query scanned {scanned} bytes (> cap {settings.max_scanned_bytes})")

    result = client.get_query_results(QueryExecutionId=qid)
    rows_raw = result["ResultSet"]["Rows"]
    columns = [c["VarCharValue"] for c in rows_raw[0]["Data"]] if rows_raw else []
    data_rows = [
        {columns[i]: cell.get("VarCharValue") for i, cell in enumerate(r["Data"])}
        for r in rows_raw[1:]
    ]
    return SqlResult(sql=validated, columns=columns, rows=data_rows, scanned_bytes=scanned)


def answer_sales_question(question: str, settings: Settings | None = None) -> SqlResult:
    """Convenience: generate SQL for a question, validate, and run it."""
    settings = settings or get_settings()
    sql = generate_sql(question, settings)
    return run_query(sql, settings)
