"""ARBITER Structured Specialist — runs on Bedrock AgentCore Runtime.

Bridges STRUCTURED enforcement exports (CSV in S3, catalogued by Glue) into the
deterministic scan. Two modes:

  - Scan mode (deterministic, no LLM): payload {"mode":"produce_observations",
    "source":"zscaler"} → runs an Athena SELECT over the Glue-catalogued table and
    returns observation dicts in the EXACT shape the rule-pack matchers consume
    (see agents/master_orchestrator/agent.py::_seed_zscaler_observations). The
    master swaps its zscaler fixtures for this when STRUCTURED_RUNTIME_ARN is set,
    and falls back to fixtures on any error so a bad query never blanks the scan.

  - Chat mode: a Strands agent with a SELECT-only run_athena_query tool, for the
    MCP/analyst path ("how many zscaler rules bypass SSL inspection?").

Environment variables:
  GLUE_DATABASE     Glue Data Catalog database (default <env>_<project>_structured)
  ATHENA_WORKGROUP  Athena workgroup with SSE-KMS results + byte cap
  ATHENA_OUTPUT     s3://<processed-bucket>/athena-results/  (results location)
  MODEL_ID / GUARDRAIL_ID / GUARDRAIL_VERSION  as the other specialists
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any
from urllib.parse import urlparse

import boto3
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent
from strands.models.bedrock import BedrockModel
from strands.types.exceptions import MaxTokensReachedException
from strands.tools import tool

from _shared.token_usage import record_from_agent_result
from observations import MAPPERS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("structured_specialist")

REGION = os.environ.get("AWS_REGION", "us-east-1")
MODEL_ID = os.environ.get("MODEL_ID", "us.amazon.nova-2-lite-v1:0")
GLUE_DATABASE = os.environ.get("GLUE_DATABASE", "")
ATHENA_WORKGROUP = os.environ.get("ATHENA_WORKGROUP", "primary")
ATHENA_OUTPUT = os.environ.get("ATHENA_OUTPUT", "")
GUARDRAIL_ID = os.environ.get("GUARDRAIL_ID")
GUARDRAIL_VERSION = os.environ.get("GUARDRAIL_VERSION", "DRAFT")

ATHENA_TIMEOUT_S = int(os.environ.get("ATHENA_TIMEOUT_S", "45"))
ATHENA_MAX_ROWS = int(os.environ.get("ATHENA_MAX_ROWS", "500"))
CHAT_TOOL_MAX_ROWS = int(os.environ.get("CHAT_TOOL_MAX_ROWS", "20"))
SESSION_GROUP_CONTEXTS: dict[str, dict[str, Any]] = {}

SYSTEM_PROMPT = """You are the Structured Data specialist for ARBITER. You answer
questions about technical-control exports, invoice batches, sales datasets, and
other CSV data that has been catalogued in AWS Glue and is queryable through
Amazon Athena.

Answer in a project-centric way. If the user asks what is available, asks for
projects, or asks for help getting started, first call list_projects. A project is
the analysis boundary: you may combine tables/files inside one project, but do not
combine data across multiple projects. If a request could match more than one
project, ask the user to choose one project before querying.

If the prompt starts with resolved project/group context, use the supplied table
hints directly. Do not call list_projects again for that request.

When the user gives a friendly or partial dataset name such as "daily sales zone 1",
first use list_projects to identify the owning project and table hints. If needed,
call list_glue_tables to find the exact table name. Then use run_athena_query with
a single SELECT statement to fetch evidence. Never issue anything but SELECT. Use
no more than one catalog lookup and one Athena query before answering unless the
user has already selected one project. Return concise findings naming the project,
table, and rows. Do not fabricate rows.
"""

app = BedrockAgentCoreApp()
athena = boto3.client("athena", region_name=REGION)
glue = boto3.client("glue", region_name=REGION)
s3 = boto3.client("s3", region_name=REGION)


# ── Athena helpers ────────────────────────────────────────────────────────────

def _athena_rows(sql: str) -> list[dict[str, str]]:
    """Run a SELECT and return rows as dicts of column→string (Athena gives strings).

    SELECT-only, capped. Raises on non-SELECT or query failure so callers can fall
    back. Returns [] for an empty result set.
    """
    stripped = sql.strip().rstrip(";").lstrip("(").strip()
    if not stripped.lower().startswith("select") and not stripped.lower().startswith("with"):
        raise ValueError("Only SELECT/WITH queries are permitted")
    if not ATHENA_OUTPUT:
        raise RuntimeError("ATHENA_OUTPUT not configured")

    start = athena.start_query_execution(
        QueryString=sql,
        QueryExecutionContext={"Database": GLUE_DATABASE} if GLUE_DATABASE else {},
        WorkGroup=ATHENA_WORKGROUP,
        ResultConfiguration={"OutputLocation": ATHENA_OUTPUT},
    )
    qid = start["QueryExecutionId"]
    deadline = time.time() + ATHENA_TIMEOUT_S
    while time.time() < deadline:
        info = athena.get_query_execution(QueryExecutionId=qid)
        state = info["QueryExecution"]["Status"]["State"]
        if state == "SUCCEEDED":
            break
        if state in ("FAILED", "CANCELLED"):
            reason = info["QueryExecution"]["Status"].get("StateChangeReason", state)
            raise RuntimeError(f"Athena query {state}: {reason}")
        time.sleep(1)
    else:
        raise TimeoutError(f"Athena query {qid} timed out after {ATHENA_TIMEOUT_S}s")

    res = athena.get_query_results(QueryExecutionId=qid, MaxResults=min(ATHENA_MAX_ROWS, 1000) + 1)
    rows = res.get("ResultSet", {}).get("Rows", [])
    if not rows:
        return []
    header = [c.get("VarCharValue", "") for c in rows[0].get("Data", [])]
    out: list[dict[str, str]] = []
    for r in rows[1:]:
        cells = r.get("Data", [])
        out.append({header[i]: (cells[i].get("VarCharValue") if i < len(cells) else None)
                    for i in range(len(header))})
    return out


# ── produce_observations: Athena query + pure mapping (from observations.py) ──

def produce_observations(source: str) -> list[dict[str, Any]]:
    """Query the catalogued table for `source` and return matcher-shaped observations."""
    if source not in MAPPERS:
        raise ValueError(f"unknown structured source: {source}")
    table, mapper = MAPPERS[source]
    rows = _athena_rows(f'SELECT * FROM "{table}" LIMIT {ATHENA_MAX_ROWS}')
    observations = mapper(rows)
    log.info("produce_observations(%s): %d rows → %d observations", source, len(rows), len(observations))
    return observations


# ── Chat tool ─────────────────────────────────────────────────────────────────

def _processed_bucket_from_athena_output() -> str:
    parsed = urlparse(ATHENA_OUTPUT or "")
    if parsed.scheme == "s3" and parsed.netloc:
        return parsed.netloc
    return os.environ.get("PROCESSED_BUCKET", "")


def _collect_table_hints(group: dict[str, Any]) -> list[str]:
    hints: set[str] = set()
    for key in ("structuredTableHint", "glueTableHint"):
        value = group.get(key)
        if isinstance(value, str) and value:
            hints.add(value)
    for value in group.get("structuredTableHints") or []:
        if isinstance(value, str) and value:
            hints.add(value)
    for file_info in group.get("files") or []:
        value = file_info.get("glueTableHint")
        if isinstance(value, str) and value:
            hints.add(value)
    return sorted(hints)


@tool
def list_projects() -> str:
    """List Arbiter Data Grouping projects available for structured analysis."""
    return _format_projects_for_selection(_load_projects())


def _load_projects() -> list[dict[str, Any]]:
    bucket = _processed_bucket_from_athena_output()
    if not bucket:
        raise RuntimeError("processed bucket not configured")
    paginator = s3.get_paginator("list_objects_v2")
    projects = []
    for page in paginator.paginate(Bucket=bucket, Prefix="projects/"):
        for obj in page.get("Contents", []):
            key = obj.get("Key", "")
            if not key.endswith("/metadata/project.json"):
                continue
            try:
                body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
                metadata = json.loads(body.decode("utf-8"))
            except Exception as e:
                projects.append({"metadataKey": key, "error": str(e)})
                continue

            groups = []
            project_table_hints: set[str] = set()
            for group in metadata.get("groups") or []:
                table_hints = _collect_table_hints(group)
                project_table_hints.update(table_hints)
                files = group.get("files") or []
                groups.append({
                    "name": group.get("name") or group.get("id"),
                    "type": group.get("type"),
                    "fileCount": len(files),
                    "csvCount": sum(1 for item in files if item.get("type") == "csv"),
                    "tableHints": table_hints[:25],
                    "files": [
                        {
                            "name": item.get("name") or item.get("filename") or item.get("key"),
                            "type": item.get("type"),
                            "glueTableHint": item.get("glueTableHint"),
                        }
                        for item in files[:100]
                    ],
                })

            projects.append({
                "projectId": metadata.get("projectId"),
                "projectName": metadata.get("projectName") or metadata.get("projectId"),
                "updatedAt": metadata.get("updatedAt"),
                "groupCount": len(groups),
                "tableCount": len(project_table_hints),
                "groups": groups[:50],
            })
    projects.sort(key=lambda item: item.get("updatedAt") or "", reverse=True)
    return projects[:100]


def _format_projects_for_selection(projects: list[dict[str, Any]]) -> str:
    if not projects:
        return "No Data Grouping projects found."
    lines = [
        "Available projects",
        "",
        "Select one group to continue. I will keep analysis inside that project's boundary.",
    ]
    for project in projects:
        if project.get("error"):
            lines.extend([
                "",
                f"Project metadata: {project.get('metadataKey', 'unknown')}",
                f"Status: {project['error']}",
            ])
            continue
        project_name = project.get("projectName") or project.get("projectId") or "Unnamed project"
        project_id = project.get("projectId") or project_name
        groups = project.get("groups") or []
        group_names = [group.get("name") or "Unnamed group" for group in groups]
        group_text = ", ".join(group_names) if group_names else "No groups yet"
        lines.extend([
            "",
            f"Project: {project_name} ({project_id})",
            f"Groups: {group_text}",
        ])
    lines.extend([
        "",
        "Reply with the group name you want to use, for example: Project_Helios_Ridge.",
    ])
    return "\n".join(lines)


def _list_projects_payload() -> str:
    try:
        return _format_projects_for_selection(_load_projects())
    except Exception as e:
        return f"(project catalog error: {e})"


def _normalize_lookup_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _project_group_aliases(project: dict[str, Any], group: dict[str, Any]) -> set[str]:
    aliases: set[str] = set()
    for value in (
        project.get("projectId"),
        project.get("projectName"),
        group.get("name"),
    ):
        normalized = _normalize_lookup_text(str(value or ""))
        if normalized:
            aliases.add(normalized)
            aliases.update(
                token for token in normalized.split()
                if len(token) >= 5 and token not in {"project", "group", "audit", "review"}
            )
    return aliases


def _resolve_single_group_context(prompt: str) -> dict[str, Any] | None:
    """Resolve a friendly/partial project-group name to one exact group context."""
    prompt_text = f" {_normalize_lookup_text(prompt)} "
    if not prompt_text.strip():
        return None
    try:
        projects = _load_projects()
    except Exception as e:
        log.warning("Group alias resolution skipped: %s", e)
        return None

    matches: list[dict[str, Any]] = []
    for project in projects:
        if project.get("error"):
            continue
        for group in project.get("groups") or []:
            aliases = _project_group_aliases(project, group)
            if any(f" {alias} " in prompt_text for alias in aliases):
                matches.append({"project": project, "group": group})

    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for match in matches:
        project = match["project"]
        group = match["group"]
        key = (
            str(project.get("projectId") or project.get("projectName") or ""),
            str(group.get("name") or ""),
        )
        unique[key] = match
    if len(unique) != 1:
        return None

    match = next(iter(unique.values()))
    project = match["project"]
    group = match["group"]
    return {
        "projectName": project.get("projectName") or project.get("projectId"),
        "projectId": project.get("projectId"),
        "groupName": group.get("name"),
        "groupType": group.get("type"),
        "tableHints": group.get("tableHints") or [],
        "files": group.get("files") or [],
    }


def _prompt_lookup_tokens(prompt: str) -> set[str]:
    generic = {
        "against", "amounts", "benchmark", "billed", "bills", "candidates",
        "charges", "claim", "claims", "compare", "duplicate", "group",
        "identify", "medical", "provider", "providers", "rates", "repeated",
        "project", "tables", "files", "available", "appears", "briefly",
        "explain", "units", "unusually",
    }
    return {
        token
        for token in _normalize_lookup_text(prompt).split()
        if len(token) >= 5 and token not in generic
    }


def _infer_group_prefix_from_table(table_name: str) -> str | None:
    match = re.match(r"^(.+?)_\d{2}_.+$", table_name)
    if match:
        return match.group(1)
    return None


def _resolve_group_context_from_glue(prompt: str) -> dict[str, Any] | None:
    """Fallback alias resolver using project-specific Glue table names."""
    tokens = _prompt_lookup_tokens(prompt)
    if not tokens:
        return None
    try:
        paginator = glue.get_paginator("get_tables")
        groups: dict[str, list[str]] = {}
        for page in paginator.paginate(DatabaseName=GLUE_DATABASE):
            for table in page.get("TableList", []):
                name = table.get("Name", "")
                normalized_name = _normalize_lookup_text(name)
                if not any(token in normalized_name.split() for token in tokens):
                    continue
                prefix = _infer_group_prefix_from_table(name)
                if not prefix:
                    continue
                groups.setdefault(prefix, []).append(name)
    except Exception as e:
        log.warning("Glue alias resolution skipped: %s", e)
        return None

    if len(groups) != 1:
        return None
    prefix, table_hints = next(iter(groups.items()))
    readable_group = prefix
    for stem in ("vendor_audit_june_2026_",):
        if readable_group.startswith(stem):
            readable_group = readable_group[len(stem):]
    return {
        "projectName": "Inferred from Glue catalog",
        "projectId": prefix,
        "groupName": readable_group,
        "groupType": "structured",
        "tableHints": sorted(table_hints),
        "files": [],
    }


def _prepend_resolved_group_context(prompt: str, context: dict[str, Any]) -> str:
    table_hints = context.get("tableHints") or []
    hints_text = "\n".join(f"- {hint}" for hint in table_hints[:25]) or "- No table hints found"
    return (
        "Resolved project/group context. Use this as the analysis boundary and do not "
        "invent alternate project names.\n"
        f"Project: {context.get('projectName')} ({context.get('projectId')})\n"
        f"Group: {context.get('groupName')}\n"
        f"Group type: {context.get('groupType') or 'unknown'}\n"
        "Allowed Glue table hints for this group:\n"
        f"{hints_text}\n\n"
        f"User request:\n{prompt}"
    )


def _looks_like_group_inventory_request(prompt: str) -> bool:
    text = _normalize_lookup_text(prompt)
    return (
        ("list" in text or "show" in text)
        and (
            "available files" in text
            or "available tables" in text
            or "files and tables" in text
            or "tables in this group" in text
        )
    )


def _describe_inventory_item(name: str) -> str:
    text = _normalize_lookup_text(name)
    if "claims master" in text:
        return "core claim records and claim-level identifiers"
    if "policyholders" in text:
        return "insured or policyholder reference data"
    if "accident reports" in text:
        return "accident severity, timing, and incident details"
    if "claimants" in text:
        return "claimant details tied to claims"
    if "provider directory" in text:
        return "medical provider reference information"
    if "provider bills" in text:
        return "medical billing lines, providers, billed amounts, and units"
    if "treatment sessions" in text:
        return "treatment dates, services, and utilization patterns"
    if "payments" in text:
        return "claim payment transactions or outcomes"
    if "adjuster assignments" in text:
        return "adjuster ownership and claim handling assignments"
    if "attorney directory" in text:
        return "attorney and law-firm reference data"
    if "call center logs" in text:
        return "intake/contact activity and timing signals"
    if "witness statements" in text:
        return "witness statement index and narrative evidence pointers"
    if "provider hours" in text:
        return "provider operating hours for service-date validation"
    if "benchmark rates" in text:
        return "medical benchmark rates for billed-service comparison"
    if "vehicle damage" in text:
        return "vehicle appraisal and damage-severity evidence"
    if "reserve changes" in text:
        return "reserve movements and claim valuation changes"
    if "siu referrals" in text:
        return "special investigation referrals and fraud indicators"
    if "litigation calendar" in text:
        return "legal dates, deadlines, and litigation milestones"
    if "mailroom document" in text:
        return "document intake index"
    if "claim note keywords" in text:
        return "keywords extracted from claim notes"
    if "prior claims" in text:
        return "claimant prior-claim history"
    if "portal access logs" in text:
        return "portal activity and payment/access timing signals"
    if "duplicate bill candidates" in text:
        return "candidate duplicate medical bills"
    if "closed claim outcomes" in text:
        return "closed-claim disposition and outcome data"
    if "data quality exceptions" in text:
        return "known data quality issues or exceptions"
    if "answer key" in text:
        return "demo ground truth and expected investigative pattern"
    if "readme" in text or "manifest" in text:
        return "project documentation or inventory metadata"
    if "pdf" in text or "guide" in text:
        return "supporting documentation or instructions"
    return "project dataset or supporting file"


def _format_group_inventory(context: dict[str, Any]) -> str:
    project_name = context.get("projectName") or context.get("projectId") or "Selected project"
    group_name = context.get("groupName") or "Selected group"
    lines = [
        f"Project: {project_name}",
        f"Group: {group_name}",
        "",
        "Available tables",
    ]
    table_hints = context.get("tableHints") or []
    if table_hints:
        for table in table_hints:
            lines.append(f"- `{table}`: {_describe_inventory_item(table)}.")
    else:
        lines.append("- No Glue tables found for this group.")

    lines.extend(["", "Available files"])
    files = context.get("files") or []
    if files:
        for file_info in files:
            name = file_info.get("name") or "Unnamed file"
            file_type = file_info.get("type") or "file"
            table = file_info.get("glueTableHint")
            table_text = f" Table: `{table}`." if table else ""
            lines.append(f"- `{name}` ({file_type}): {_describe_inventory_item(name)}.{table_text}")
    elif table_hints:
        lines.append("- File-level metadata is not available to this runtime, but the catalogued CSV tables above are available for Athena queries.")
    else:
        lines.append("- No file metadata found for this group.")
    return "\n".join(lines)


def _extract_table_fragment(prompt: str) -> str | None:
    ticked = re.search(r"`([^`]+)`", prompt)
    if ticked:
        return ticked.group(1).strip()
    match = re.search(r"\b\d{2}_[a-z0-9_]+\b", prompt.lower())
    if match:
        return match.group(0)
    match = re.search(r"\b[a-z0-9]+_master\b", prompt.lower())
    if match:
        return match.group(0)
    return None


def _extract_group_fragment(prompt: str) -> str | None:
    match = re.search(r"\b(Project_[A-Za-z0-9_]+)\b", prompt)
    if match:
        return match.group(1).strip()
    return None


def _matching_glue_tables(fragment: str, context: dict[str, Any] | None = None) -> list[str]:
    needle = (fragment or "").strip().strip('"`').lower()
    if not needle:
        return []
    group_fragment = _extract_group_fragment((context or {}).get("sourcePrompt") or "")
    group_needle = _normalize_lookup_text(group_fragment).replace(" ", "_") if group_fragment else ""
    context_hints = (context or {}).get("tableHints") or []
    scoped = [table for table in context_hints if needle in table.lower()]
    if scoped:
        return sorted(set(scoped))
    try:
        paginator = glue.get_paginator("get_tables")
        matches: list[str] = []
        for page in paginator.paginate(DatabaseName=GLUE_DATABASE):
            for table in page.get("TableList", []):
                name = table.get("Name", "")
                if group_needle and group_needle.lower() not in name.lower():
                    continue
                if needle in name.lower():
                    matches.append(name)
        return sorted(set(matches))
    except Exception as e:
        log.warning("Glue table fragment lookup failed: %s", e)
        return []


def _extract_requested_year(prompt: str) -> str | None:
    match = re.search(r"\b(20\d{2})\b", prompt)
    if match:
        return match.group(1)
    return None


def _looks_like_claims_loss_year_request(prompt: str) -> bool:
    text = _normalize_lookup_text(prompt)
    return (
        "claims" in text
        and "loss date" in text
        and _extract_requested_year(prompt) is not None
        and (_extract_table_fragment(prompt) or "claims master" in text)
    )


def _format_rows_as_markdown(rows: list[dict[str, str | None]], empty_message: str) -> str:
    if not rows:
        return empty_message
    columns = list(rows[0].keys())
    lines = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join(["---"] * len(columns)) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(column) or "") for column in columns) + " |")
    return "\n".join(lines)


def _handle_claims_loss_year_request(prompt: str, context: dict[str, Any] | None) -> str | None:
    if not _looks_like_claims_loss_year_request(prompt):
        return None
    year = _extract_requested_year(prompt)
    fragment = _extract_table_fragment(prompt) or "01_claims_master"
    lookup_context = {**(context or {}), "sourcePrompt": prompt}
    matches = _matching_glue_tables(fragment, lookup_context)
    if not matches:
        return f"I could not find a Glue table matching `{fragment}`."
    if len(matches) > 1:
        options = "\n".join(f"- `{table}`" for table in matches)
        return (
            f"`{fragment}` matches multiple tables, so I need the group/project before querying:\n"
            f"{options}\n\n"
            "Try: `Use the Project_Nightingale_Aurora_Indemnity group. "
            f"Give me all claims from {fragment} where the loss date is in {year}.`"
        )

    table = matches[0]
    next_year = str(int(year) + 1)
    sql = (
        "SELECT claim_id, policy_id, loss_date, report_date, city, state, "
        "claim_type, attorney_id, status, fraud_seed "
        f'FROM "{table}" '
        f"WHERE CAST(loss_date AS DATE) >= DATE '{year}-01-01' "
        f"AND CAST(loss_date AS DATE) < DATE '{next_year}-01-01' "
        "ORDER BY loss_date, claim_id "
        f"LIMIT {ATHENA_MAX_ROWS}"
    )
    try:
        rows = _athena_rows(sql)
    except Exception as e:
        return f"(query error: {e})"
    if not rows:
        return (
            f"I queried `{table}` for claims with `loss_date` in {year}. "
            "The table is accessible, but there are no matching rows."
        )
    return (
        f"Claims from `{table}` with `loss_date` in {year}:\n\n"
        f"{_format_rows_as_markdown(rows, 'No rows.')}"
    )


def _looks_like_natural_language_query_request(prompt: str) -> bool:
    text = _normalize_lookup_text(prompt)
    return "natural language query" in text or "natural language prompt" in text


def _looks_like_run_request(prompt: str) -> bool:
    text = _normalize_lookup_text(prompt)
    return (
        "run it" in text
        or "and run" in text
        or "execute it" in text
        or "then run" in text
    )


def _canonical_group_label(group: str) -> str:
    if not group:
        return ""
    parts = [part for part in group.split("_") if part]
    if parts and parts[0].lower() == "project":
        parts = parts[1:]
    if not parts:
        return group
    return "Project_" + "_".join(part[:1].upper() + part[1:] for part in parts)


def _extract_limit(prompt: str, default: int = 100) -> int:
    match = re.search(r"\bfirst\s+(\d{1,4})\b", prompt.lower())
    if match:
        return max(1, min(int(match.group(1)), ATHENA_MAX_ROWS))
    match = re.search(r"\blimit\s+(\d{1,4})\b", prompt.lower())
    if match:
        return max(1, min(int(match.group(1)), ATHENA_MAX_ROWS))
    return default


def _looks_like_first_records_request(prompt: str) -> bool:
    text = _normalize_lookup_text(prompt)
    return (
        ("first" in text or "preview" in text or "show" in text or "see" in text)
        and ("record" in text or "records" in text or "rows" in text)
        and (_extract_table_fragment(prompt) is not None or "claims master" in text)
    )


def _handle_first_records_request(prompt: str, context: dict[str, Any] | None) -> str | None:
    if not _looks_like_first_records_request(prompt):
        return None
    fragment = _extract_table_fragment(prompt) or "01_claims_master"
    limit = _extract_limit(prompt)
    group_name = (context or {}).get("groupName") or _extract_group_fragment(prompt)
    should_return_prompt = _looks_like_natural_language_query_request(prompt)
    should_run = _looks_like_run_request(prompt) or not should_return_prompt

    prompt_text = ""
    if should_return_prompt:
        if group_name:
            group_name = _canonical_group_label(group_name)
            prompt_text = (
                "Use this natural-language query:\n\n"
                f"`Use the {group_name} group. Show me the first {limit} records from {fragment}.`"
            )
        else:
            prompt_text = (
                "Use this natural-language query, replacing the group with the project you want:\n\n"
                f"`Use the <project group> group. Show me the first {limit} records from {fragment}.`"
            )
        if not should_run:
            return prompt_text

    lookup_context = {**(context or {}), "sourcePrompt": prompt}
    matches = _matching_glue_tables(fragment, lookup_context)
    if not matches:
        message = f"I could not find a Glue table matching `{fragment}`."
        return f"{prompt_text}\n\n{message}" if prompt_text else message
    if len(matches) > 1:
        options = "\n".join(f"- `{table}`" for table in matches)
        message = (
            f"`{fragment}` matches multiple tables, so I need the group/project before querying:\n"
            f"{options}"
        )
        return f"{prompt_text}\n\n{message}" if prompt_text else message

    table = matches[0]
    try:
        rows = _athena_rows(f'SELECT * FROM "{table}" LIMIT {limit}')
    except Exception as e:
        message = f"(query error: {e})"
        return f"{prompt_text}\n\n{message}" if prompt_text else message
    if not rows:
        message = f"`{table}` is accessible, but it returned no rows."
        return f"{prompt_text}\n\n{message}" if prompt_text else message
    message = f"First {min(limit, len(rows))} rows from `{table}`:\n\n{_format_rows_as_markdown(rows, 'No rows.')}"
    return f"{prompt_text}\n\n{message}" if prompt_text else message


def _looks_like_row_count_request(prompt: str) -> bool:
    text = _normalize_lookup_text(prompt)
    return (
        ("count rows" in text or "row count" in text or "counts rows" in text or "count records" in text)
        and any(name in text for name in (
            "claims master",
            "provider bills",
            "treatment sessions",
            "medical benchmark rates",
            "duplicate bill candidates",
            "siu referrals",
        ))
    )


def _requested_count_fragments(prompt: str) -> list[str]:
    text = _normalize_lookup_text(prompt)
    candidates = [
        ("claims_master", ("claims master", "claims_master")),
        ("provider_bills", ("provider bills", "provider_bills")),
        ("treatment_sessions", ("treatment sessions", "treatment_sessions")),
        ("medical_benchmark_rates", ("medical benchmark rates", "medical_benchmark_rates", "benchmark rates")),
        ("duplicate_bill_candidates", ("duplicate bill candidates", "duplicate_bill_candidates")),
        ("siu_referrals", ("siu referrals", "siu_referrals")),
    ]
    fragments: list[str] = []
    for fragment, aliases in candidates:
        if any(alias in text for alias in aliases):
            fragments.append(fragment)
    return fragments


def _handle_row_count_request(prompt: str, context: dict[str, Any] | None) -> str | None:
    if not _looks_like_row_count_request(prompt):
        return None
    fragments = _requested_count_fragments(prompt)
    if not fragments:
        return None

    lookup_context = {**(context or {}), "sourcePrompt": prompt}
    rows: list[dict[str, str]] = []
    errors: list[str] = []
    for fragment in fragments:
        matches = _matching_glue_tables(fragment, lookup_context)
        if not matches:
            errors.append(f"`{fragment}`: no matching Glue table")
            continue
        if len(matches) > 1:
            errors.append(f"`{fragment}`: matched multiple tables ({', '.join(matches)})")
            continue
        table = matches[0]
        try:
            count_rows = _athena_rows(f'SELECT COUNT(*) AS row_count FROM "{table}"')
        except Exception as e:
            errors.append(f"`{fragment}`: {e}")
            continue
        row_count = count_rows[0].get("row_count") if count_rows else "0"
        rows.append({
            "dataset": fragment,
            "table": table,
            "row_count": str(row_count or "0"),
        })

    message = _format_rows_as_markdown(rows, "No row counts returned.")
    if errors:
        message += "\n\nIssues:\n" + "\n".join(f"- {error}" for error in errors)
    return message


def _table_for_fragment(fragment: str, context: dict[str, Any] | None, prompt: str) -> str | None:
    matches = _matching_glue_tables(fragment, {**(context or {}), "sourcePrompt": prompt})
    return matches[0] if len(matches) == 1 else None


def _looks_like_nightingale_pattern_request(prompt: str, context: dict[str, Any] | None) -> bool:
    text = _normalize_lookup_text(prompt)
    context_text = _normalize_lookup_text(
        " ".join(str((context or {}).get(key) or "") for key in ("projectId", "projectName", "groupName"))
    )
    return (
        "nightingale" in f"{text} {context_text}"
        and (
            "billing" in text
            or "provider" in text
            or "attorney" in text
            or "duplicate" in text
            or "mri" in text
            or "fraud" in text
            or "suspicious" in text
        )
        and any(term in text for term in ("pattern", "investigative", "evidence", "combine", "summarize", "look for"))
    )


def _handle_nightingale_pattern_request(prompt: str, context: dict[str, Any] | None) -> str | None:
    if not _looks_like_nightingale_pattern_request(prompt, context):
        return None

    needed = {
        "bills": "provider_bills",
        "treatments": "treatment_sessions",
        "hours": "provider_hours",
        "duplicates": "duplicate_bill_candidates",
        "siu": "siu_referrals",
        "claims": "claims_master",
        "attorneys": "attorney_directory",
        "providers": "provider_directory",
        "accidents": "accident_reports",
    }
    tables = {key: _table_for_fragment(fragment, context, prompt) for key, fragment in needed.items()}
    missing = [fragment for key, fragment in needed.items() if not tables.get(key)]
    if missing:
        return (
            "I could not resolve all Nightingale tables needed for the deterministic pattern report. "
            f"Missing: {', '.join(missing)}."
        )

    provider_sql = f"""
        WITH provider_directory AS (
            SELECT col0 AS provider_id, col1 AS provider_name, col5 AS risk_hint
            FROM "{tables['providers']}"
            WHERE col0 <> 'provider_id'
        ),
        duplicate_counts AS (
            SELECT provider_id, COUNT(*) AS duplicate_candidates
            FROM "{tables['duplicates']}"
            GROUP BY provider_id
        )
        SELECT
            b.provider_id,
            COALESCE(p.provider_name, b.provider_id) AS provider_name,
            COALESCE(p.risk_hint, '') AS risk_hint,
            COUNT(*) AS bill_lines,
            COUNT(DISTINCT b.claim_id) AS claims,
            ROUND(SUM(CAST(b.billed_amount AS DOUBLE)), 2) AS billed_amount,
            SUM(CASE WHEN LOWER(b.description) LIKE '%mri%' OR LOWER(b.cpt_code) LIKE '%mri%' THEN 1 ELSE 0 END) AS mri_lines,
            SUM(CASE WHEN CAST(b.units AS DOUBLE) >= 4 THEN 1 ELSE 0 END) AS high_unit_lines,
            COALESCE(MAX(d.duplicate_candidates), 0) AS duplicate_candidates
        FROM "{tables['bills']}" b
        LEFT JOIN provider_directory p ON p.provider_id = b.provider_id
        LEFT JOIN duplicate_counts d ON d.provider_id = b.provider_id
        GROUP BY b.provider_id, p.provider_name, p.risk_hint
        ORDER BY
            CASE WHEN COALESCE(p.risk_hint, '') = 'fraud' THEN 0 ELSE 1 END,
            duplicate_candidates DESC,
            billed_amount DESC
        LIMIT 8
    """
    attorney_sql = f"""
        WITH attorney_directory AS (
            SELECT col0 AS attorney_id, col1 AS firm_name, col2 AS risk_hint
            FROM "{tables['attorneys']}"
            WHERE col0 <> 'attorney_id'
        )
        SELECT
            c.attorney_id,
            COALESCE(a.firm_name, c.attorney_id) AS firm_name,
            COALESCE(a.risk_hint, '') AS risk_hint,
            COUNT(DISTINCT c.claim_id) AS claims,
            ROUND(SUM(CAST(b.billed_amount AS DOUBLE)), 2) AS billed_amount,
            SUM(CASE WHEN LOWER(b.description) LIKE '%mri%' OR LOWER(b.cpt_code) LIKE '%mri%' THEN 1 ELSE 0 END) AS mri_lines
        FROM "{tables['claims']}" c
        JOIN "{tables['bills']}" b ON b.claim_id = c.claim_id
        LEFT JOIN attorney_directory a ON a.attorney_id = c.attorney_id
        GROUP BY c.attorney_id, a.firm_name, a.risk_hint
        ORDER BY
            CASE WHEN COALESCE(a.risk_hint, '') = 'fraud' THEN 0 ELSE 1 END,
            billed_amount DESC
        LIMIT 8
    """
    delayed_sql = f"""
        WITH first_treatment AS (
            SELECT claim_id, MIN(CAST(treatment_date AS DATE)) AS first_treatment_date
            FROM "{tables['treatments']}"
            GROUP BY claim_id
        ),
        claim_bills AS (
            SELECT
                claim_id,
                ROUND(SUM(CAST(billed_amount AS DOUBLE)), 2) AS billed_amount,
                SUM(CASE WHEN LOWER(description) LIKE '%mri%' OR LOWER(cpt_code) LIKE '%mri%' THEN 1 ELSE 0 END) AS mri_lines
            FROM "{tables['bills']}"
            GROUP BY claim_id
        )
        SELECT DISTINCT
            c.claim_id,
            c.attorney_id,
            CAST(c.loss_date AS VARCHAR) AS loss_date,
            CAST(ft.first_treatment_date AS VARCHAR) AS first_treatment_date,
            date_diff('day', CAST(c.loss_date AS DATE), ft.first_treatment_date) AS days_to_treatment,
            cb.billed_amount,
            cb.mri_lines
        FROM "{tables['claims']}" c
        JOIN first_treatment ft ON ft.claim_id = c.claim_id
        JOIN claim_bills cb ON cb.claim_id = c.claim_id
        WHERE date_diff('day', CAST(c.loss_date AS DATE), ft.first_treatment_date) BETWEEN 10 AND 15
        ORDER BY cb.billed_amount DESC
        LIMIT 8
    """
    low_severity_sql = f"""
        WITH claim_bills AS (
            SELECT claim_id, ROUND(SUM(CAST(billed_amount AS DOUBLE)), 2) AS billed_amount
            FROM "{tables['bills']}"
            GROUP BY claim_id
        )
        SELECT
            a.claim_id,
            a.damage_severity,
            a.airbags_deployed,
            a.tow_required,
            a.reported_speed_mph,
            cb.billed_amount
        FROM "{tables['accidents']}" a
        JOIN claim_bills cb ON cb.claim_id = a.claim_id
        WHERE LOWER(a.damage_severity) IN ('low', 'minor')
        ORDER BY cb.billed_amount DESC
        LIMIT 8
    """
    sunday_sql = f"""
        SELECT
            b.provider_id,
            COUNT(*) AS sunday_bill_lines,
            COUNT(DISTINCT b.claim_id) AS claims,
            ROUND(SUM(CAST(b.billed_amount AS DOUBLE)), 2) AS billed_amount
        FROM "{tables['bills']}" b
        WHERE day_of_week(CAST(b.service_date AS DATE)) = 7
        GROUP BY b.provider_id
        ORDER BY sunday_bill_lines DESC, billed_amount DESC
        LIMIT 8
    """
    siu_sql = f"""
        SELECT
            col2 AS reason,
            col3 AS triage_status,
            COUNT(*) AS referrals
        FROM "{tables['siu']}"
        WHERE col0 <> 'claim_id'
        GROUP BY col2, col3
        ORDER BY referrals DESC
        LIMIT 8
    """

    sections: list[tuple[str, str, list[dict[str, str | None]]]] = []
    errors: list[str] = []
    for title, sql in (
        ("Provider billing concentration", provider_sql),
        ("Attorney-linked billing concentration", attorney_sql),
        ("Delayed treatment starts, 10-15 days after loss", delayed_sql),
        ("Low accident severity with high medical billing", low_severity_sql),
        ("Sunday service-date billing", sunday_sql),
        ("SIU referral reasons", siu_sql),
    ):
        try:
            sections.append((title, sql, _athena_rows(sql)))
        except Exception as e:
            errors.append(f"{title}: {e}")

    lines = [
        "Project_Nightingale_Aurora_Indemnity suspicious billing pattern report",
        "",
        "Strongest pattern: the structured evidence points to a medical-billing cluster rather than ordinary claim severity. The signal is strongest where provider billing concentration, attorney-linked claims, delayed treatment starts, duplicate bill candidates, MRI-heavy billing, Sunday service dates, and low accident severity line up in the same project boundary.",
        "",
        "Document evidence note: the project documents/PDFs remain supporting evidence for narrative review. This deterministic report uses the Glue/Athena structured tables and the project-catalogued document/index tables to avoid unsafe cross-project mixing or fabricated document quotes.",
    ]
    for title, _sql, rows in sections:
        lines.extend(["", f"{title}", ""])
        lines.append(_format_rows_as_markdown(rows, "No matching rows."))
    if errors:
        lines.extend(["", "Query issues"])
        lines.extend(f"- {error}" for error in errors)
    lines.extend([
        "",
        "Next investigative actions",
        "- Pull the top provider IDs and attorney IDs above into a claim packet review.",
        "- Review duplicate bill candidates against original bill images and payment records.",
        "- Validate Sunday service dates against provider operating records and appointment logs.",
        "- Compare delayed-treatment claims against call-center intake logs and witness/document narratives.",
        "- Prioritize low-severity/high-billing claims for SIU review before payment escalation.",
    ])
    return "\n".join(lines)


def _looks_like_nightingale_benchmark_request(prompt: str, context: dict[str, Any] | None) -> bool:
    text = _normalize_lookup_text(prompt)
    context_text = _normalize_lookup_text(
        " ".join(str((context or {}).get(key) or "") for key in ("projectId", "projectName", "groupName"))
    )
    return (
        "nightingale" in f"{text} {context_text}"
        and "provider bills" in text
        and ("benchmark" in text or "medical benchmark rates" in text)
        and ("duplicate" in text or "high" in text or "suspicious" in text or "unusually" in text)
    )


def _handle_nightingale_benchmark_request(prompt: str, context: dict[str, Any] | None) -> str | None:
    if not _looks_like_nightingale_benchmark_request(prompt, context):
        return None

    needed = {
        "bills": "provider_bills",
        "benchmarks": "medical_benchmark_rates",
        "duplicates": "duplicate_bill_candidates",
        "providers": "provider_directory",
    }
    tables = {key: _table_for_fragment(fragment, context, prompt) for key, fragment in needed.items()}
    missing = [fragment for key, fragment in needed.items() if not tables.get(key)]
    if missing:
        return (
            "I could not resolve all Nightingale tables needed for benchmark comparison. "
            f"Missing: {', '.join(missing)}."
        )

    line_sql = f"""
        WITH provider_directory AS (
            SELECT col0 AS provider_id, col1 AS provider_name, col5 AS risk_hint
            FROM "{tables['providers']}"
            WHERE col0 <> 'provider_id'
        ),
        duplicate_flags AS (
            SELECT
                bill_id,
                claim_id,
                provider_id,
                MAX_BY(duplicate_reason, CAST(confidence AS DOUBLE)) AS duplicate_reason,
                MAX(CAST(confidence AS DOUBLE)) AS duplicate_confidence
            FROM "{tables['duplicates']}"
            GROUP BY bill_id, claim_id, provider_id
        ),
        scored AS (
            SELECT
                b.bill_id,
                b.provider_id,
                COALESCE(p.provider_name, b.provider_id) AS provider_name,
                b.claim_id,
                b.cpt_code,
                b.description AS service_procedure,
                CAST(b.units AS DOUBLE) AS units,
                CAST(b.billed_amount AS DOUBLE) AS billed_amount,
                CAST(m.median_allowed AS DOUBLE) AS benchmark_median,
                CAST(m.p90_allowed AS DOUBLE) AS benchmark_p90,
                CAST(m.utilization_norm_units AS DOUBLE) AS benchmark_units,
                d.duplicate_reason,
                d.duplicate_confidence,
                COALESCE(p.risk_hint, '') AS provider_risk_hint
            FROM "{tables['bills']}" b
            LEFT JOIN "{tables['benchmarks']}" m ON m.cpt_code = b.cpt_code
            LEFT JOIN duplicate_flags d
                ON d.bill_id = b.bill_id
            LEFT JOIN provider_directory p ON p.provider_id = b.provider_id
        )
        SELECT
            provider_id,
            provider_name,
            claim_id,
            bill_id,
            cpt_code,
            service_procedure,
            units,
            ROUND(billed_amount, 2) AS billed_amount,
            ROUND(benchmark_median, 2) AS benchmark_median,
            ROUND(benchmark_p90, 2) AS benchmark_p90,
            benchmark_units,
            COALESCE(duplicate_reason, '') AS duplicate_indicator,
            CASE
                WHEN benchmark_p90 IS NOT NULL AND billed_amount > benchmark_p90 THEN 'billed above p90 benchmark'
                WHEN benchmark_median IS NOT NULL AND billed_amount > benchmark_median THEN 'billed above median benchmark'
                ELSE ''
            END AS benchmark_signal,
            CASE
                WHEN benchmark_units IS NOT NULL AND units > benchmark_units THEN 'units above norm'
                ELSE ''
            END AS unit_signal,
            provider_risk_hint
        FROM scored
        WHERE
            (benchmark_p90 IS NOT NULL AND billed_amount > benchmark_p90)
            OR (benchmark_units IS NOT NULL AND units > benchmark_units)
            OR duplicate_reason IS NOT NULL
            OR provider_risk_hint = 'fraud'
        ORDER BY
            CASE WHEN provider_risk_hint = 'fraud' THEN 0 ELSE 1 END,
            CASE WHEN duplicate_reason IS NOT NULL THEN 0 ELSE 1 END,
            (billed_amount / NULLIF(benchmark_p90, 0)) DESC,
            billed_amount DESC
        LIMIT 12
    """
    provider_sql = f"""
        WITH provider_directory AS (
            SELECT col0 AS provider_id, col1 AS provider_name, col5 AS risk_hint
            FROM "{tables['providers']}"
            WHERE col0 <> 'provider_id'
        ),
        duplicate_counts AS (
            SELECT provider_id, COUNT(*) AS duplicate_candidates
            FROM "{tables['duplicates']}"
            GROUP BY provider_id
        ),
        bill_scores AS (
            SELECT
                b.provider_id,
                COUNT(*) AS bill_lines,
                COUNT(DISTINCT b.claim_id) AS claims,
                ROUND(SUM(CAST(b.billed_amount AS DOUBLE)), 2) AS billed_amount,
                SUM(CASE WHEN CAST(b.billed_amount AS DOUBLE) > CAST(m.p90_allowed AS DOUBLE) THEN 1 ELSE 0 END) AS above_p90_lines,
                SUM(CASE WHEN CAST(b.units AS DOUBLE) > CAST(m.utilization_norm_units AS DOUBLE) THEN 1 ELSE 0 END) AS high_unit_lines
            FROM "{tables['bills']}" b
            LEFT JOIN "{tables['benchmarks']}" m ON m.cpt_code = b.cpt_code
            GROUP BY b.provider_id
        )
        SELECT
            s.provider_id,
            COALESCE(p.provider_name, s.provider_id) AS provider_name,
            COALESCE(p.risk_hint, '') AS risk_hint,
            s.claims,
            s.bill_lines,
            s.billed_amount,
            s.above_p90_lines,
            s.high_unit_lines,
            COALESCE(d.duplicate_candidates, 0) AS duplicate_candidates
        FROM bill_scores s
        LEFT JOIN provider_directory p ON p.provider_id = s.provider_id
        LEFT JOIN duplicate_counts d ON d.provider_id = s.provider_id
        WHERE s.above_p90_lines > 0 OR s.high_unit_lines > 0 OR COALESCE(d.duplicate_candidates, 0) > 0 OR p.risk_hint = 'fraud'
        ORDER BY
            CASE WHEN COALESCE(p.risk_hint, '') = 'fraud' THEN 0 ELSE 1 END,
            duplicate_candidates DESC,
            above_p90_lines DESC,
            billed_amount DESC
        LIMIT 10
    """

    errors: list[str] = []
    try:
        line_rows = _athena_rows(line_sql)
    except Exception as e:
        line_rows = []
        errors.append(f"Bill-line benchmark comparison: {e}")
    try:
        provider_rows = _athena_rows(provider_sql)
    except Exception as e:
        provider_rows = []
        errors.append(f"Provider rollup: {e}")

    lines = [
        "Project_Nightingale_Aurora_Indemnity benchmark and duplicate billing comparison",
        "",
        "This deterministic comparison joins provider bills to medical benchmark rates by `cpt_code`, then overlays duplicate bill candidates and provider risk hints. A row is included when billed amount exceeds the benchmark, units exceed the utilization norm, a duplicate candidate exists, or the provider is marked as a known risk hint in the project data.",
        "",
        "Top suspicious bill lines",
        "",
        _format_rows_as_markdown(line_rows, "No matching bill lines found."),
        "",
        "Top providers by benchmark/duplicate signals",
        "",
        _format_rows_as_markdown(provider_rows, "No provider rollup rows found."),
        "",
        "Short explanation",
        "- `benchmark_signal` shows whether the billed amount is above the median or p90 allowed benchmark for that CPT/procedure.",
        "- `unit_signal` marks procedure lines where billed units exceed the benchmark utilization norm.",
        "- `duplicate_indicator` comes from the duplicate bill candidate table and should be reviewed against source bill images/payment records.",
    ]
    if errors:
        lines.extend(["", "Query issues"])
        lines.extend(f"- {error}" for error in errors)
    return "\n".join(lines)


@tool
def list_glue_tables(name_contains: str = "") -> str:
    """List Glue tables available to the structured-data specialist.

    Args:
        name_contains: Optional lowercase/partial filter, e.g. "daily_sales".
    """
    try:
        paginator = glue.get_paginator("get_tables")
        tables = []
        needle = (name_contains or "").strip().lower()
        for page in paginator.paginate(DatabaseName=GLUE_DATABASE):
            for table in page.get("TableList", []):
                name = table.get("Name", "")
                if needle and needle not in name.lower():
                    continue
                columns = [
                    column.get("Name", "")
                    for column in table.get("StorageDescriptor", {}).get("Columns", [])
                ]
                tables.append({"name": name, "columns": columns})
        if not tables:
            return "No matching Glue tables."
        return json.dumps(tables[:50], indent=2)
    except Exception as e:
        return f"(catalog error: {e})"


@tool
def run_athena_query(sql: str) -> str:
    """Run a single read-only SELECT against the structured-data catalog (Athena).

    Args:
        sql: One SELECT statement, e.g. "SELECT rule_id, action FROM zscaler_rules".
    """
    try:
        rows = _athena_rows(sql)
    except Exception as e:
        return f"(query error: {e})"
    if not rows:
        return "No rows."
    head = rows[:CHAT_TOOL_MAX_ROWS]
    return json.dumps(head, indent=2)


def build_agent() -> Agent:
    model_kwargs: dict[str, Any] = {"model_id": MODEL_ID, "region_name": REGION}
    if GUARDRAIL_ID:
        model_kwargs["guardrail_id"] = GUARDRAIL_ID
        model_kwargs["guardrail_version"] = GUARDRAIL_VERSION
    return Agent(
        model=BedrockModel(**model_kwargs),
        system_prompt=SYSTEM_PROMPT,
        tools=[list_projects, list_glue_tables, run_athena_query],
    )


@app.entrypoint
def invoke(payload: dict[str, Any]) -> dict[str, Any]:
    # Scan mode — deterministic, no LLM. Used by the master during _run_scan.
    if payload.get("mode") == "produce_observations":
        source = (payload.get("source") or "zscaler")[:32]
        try:
            return {"observations": produce_observations(source)}
        except Exception as e:
            log.exception("produce_observations failed for %s", source)
            return {"error": str(e), "observations": []}

    # Chat mode.
    prompt = payload.get("prompt") or payload.get("input") or ""
    if not prompt:
        return {"error": "Missing 'prompt'"}
    stripped_prompt = prompt.strip().rstrip(";").lstrip("(").strip()
    prompt_lower = stripped_prompt.lower()
    if prompt_lower in {
        "available projects",
        "show available projects",
        "show projects",
        "list available projects",
        "list projects",
        "projects",
    }:
        return {"result": _list_projects_payload()}
    if stripped_prompt.lower().startswith(("select", "with")):
        try:
            rows = _athena_rows(prompt)
            return {"result": json.dumps(rows[:ATHENA_MAX_ROWS], indent=2)}
        except Exception as e:
            return {"result": f"(query error: {e})"}
    actor_id   = (payload.get("actor_id")   or "anonymous")[:128]
    persona    = (payload.get("persona")    or "employee")[:16]
    session_id = (payload.get("session_id") or "adhoc")[:128]
    chat_type  = (payload.get("chat_type")  or "analyst")[:16]
    user_email = (payload.get("user_email") or "")[:200]
    explicit_context = _resolve_single_group_context(prompt) or _resolve_group_context_from_glue(prompt)
    if explicit_context and session_id != "adhoc":
        SESSION_GROUP_CONTEXTS[session_id] = explicit_context
    resolved_context = explicit_context or SESSION_GROUP_CONTEXTS.get(session_id)
    if resolved_context and _looks_like_group_inventory_request(prompt):
        return {"result": _format_group_inventory(resolved_context)}
    row_count_result = _handle_row_count_request(prompt, resolved_context)
    if row_count_result:
        return {"result": row_count_result}
    nightingale_pattern_result = _handle_nightingale_pattern_request(prompt, resolved_context)
    if nightingale_pattern_result:
        return {"result": nightingale_pattern_result}
    nightingale_benchmark_result = _handle_nightingale_benchmark_request(prompt, resolved_context)
    if nightingale_benchmark_result:
        return {"result": nightingale_benchmark_result}
    claims_year_result = _handle_claims_loss_year_request(prompt, resolved_context)
    if claims_year_result:
        return {"result": claims_year_result}
    first_records_result = _handle_first_records_request(prompt, resolved_context)
    if first_records_result:
        return {"result": first_records_result}
    agent_prompt = _prepend_resolved_group_context(prompt, resolved_context) if resolved_context else prompt
    if resolved_context:
        log.info(
            "Structured specialist resolved group: project=%s group=%s",
            resolved_context.get("projectId"),
            resolved_context.get("groupName"),
        )
    log.info("Structured specialist: persona=%s session=%s prompt=%s", persona, session_id, prompt[:200])
    agent = build_agent()
    try:
        agent_result = agent(agent_prompt)
    except MaxTokensReachedException:
        log.exception("Structured specialist hit max token loop for prompt=%s", prompt[:500])
        return {
            "result": (
                "The structured-data request matched too much catalog context and the "
                "agent stopped before it could finish safely. Please narrow the query "
                "with an exact table fragment, such as `storm_glass_01_01_claims_master`, "
                "`storm_glass_01_03_vendor_invoices`, `daily_sales_zone_5`, or ask for "
                "`list tables containing storm_glass_01` first."
            )
        }
    record_from_agent_result(
        agent_result, agent="structured", persona=persona, actor_id=actor_id,
        session_id=session_id, chat_type=chat_type, model_id=MODEL_ID, user_email=user_email,
    )
    return {"result": str(agent_result)}


if __name__ == "__main__":
    app.run()
