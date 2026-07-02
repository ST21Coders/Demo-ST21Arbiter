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
                    "tableCount": len(table_hints),
                    "tableHints": table_hints[:12],
                    "groupProfile": group.get("groupProfile") or {},
                    "structuredFacts": {
                        "counts": (group.get("structuredFacts") or {}).get("counts") or {},
                    },
                    "files": [],
                })

            projects.append({
                "projectId": metadata.get("projectId"),
                "projectName": metadata.get("projectName") or metadata.get("projectId"),
                "updatedAt": metadata.get("updatedAt"),
                "groupCount": len(groups),
                "tableCount": len(project_table_hints),
                "groups": groups[:25],
            })
    projects.sort(key=lambda item: item.get("updatedAt") or "", reverse=True)
    return projects[:50]


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
        lines.extend([
            "",
            f"Project: {project_name} ({project_id})",
        ])
        if groups:
            lines.append("Groups:")
            for group in groups[:12]:
                lines.append(
                    f"- {group.get('name') or 'Unnamed group'} "
                    f"({group.get('csvCount', 0)} CSV, {group.get('tableCount', 0)} tables)"
                )
            if len(groups) > 12:
                lines.append(f"- ... {len(groups) - 12} more groups")
        else:
            lines.append("Groups: No groups yet")
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

    explicit_group = _extract_group_fragment(prompt)
    if explicit_group:
        explicit_normalized = _normalize_lookup_text(_canonical_group_label(explicit_group))
        exact_matches: list[dict[str, Any]] = []
        for project in projects:
            if project.get("error"):
                continue
            for group in project.get("groups") or []:
                group_normalized = _normalize_lookup_text(str(group.get("name") or ""))
                if group_normalized == explicit_normalized:
                    exact_matches.append({"project": project, "group": group})
        if len(exact_matches) == 1:
            match = exact_matches[0]
            project = match["project"]
            group = match["group"]
            return {
                "projectName": project.get("projectName") or project.get("projectId"),
                "projectId": project.get("projectId"),
                "groupName": group.get("name"),
                "groupType": group.get("type"),
                "tableHints": group.get("tableHints") or [],
                "groupProfile": group.get("groupProfile") or {},
                "structuredFacts": group.get("structuredFacts") or {},
                "files": group.get("files") or [],
            }

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
        "groupProfile": group.get("groupProfile") or {},
        "structuredFacts": group.get("structuredFacts") or {},
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


def _context_from_ui_selector_prompt(prompt: str) -> dict[str, Any] | None:
    if "Resolved project/group context from the UI selector." not in prompt:
        return None
    project_match = re.search(r"^Project:\s*(.+?)(?:\s+\((.*?)\))?$", prompt, re.MULTILINE)
    group_match = re.search(r"^Group:\s*([^\n]+)$", prompt, re.MULTILINE)
    if not group_match:
        return None

    table_hints: list[str] = []
    in_tables = False
    for line in prompt.splitlines():
        if line.startswith("Allowed Glue table hints:"):
            in_tables = True
            continue
        if in_tables and line.startswith("Available files for selected group:"):
            break
        if in_tables and line.startswith("- "):
            table_hints.append(line[2:].strip())

    files: list[dict[str, str]] = []
    in_files = False
    for line in prompt.splitlines():
        if line.startswith("Available files for selected group:"):
            in_files = True
            continue
        if in_files and line.startswith("Group setup profile:"):
            break
        if in_files and line.startswith("User request:"):
            break
        if in_files and line.startswith("- "):
            raw = line[2:].strip()
            name, _, rest = raw.partition(" (")
            file_type = rest.split(",", 1)[0].rstrip(")") if rest else "file"
            table_match = re.search(r"table:\s*([A-Za-z0-9_]+)", raw)
            files.append({
                "name": name,
                "type": file_type or "file",
                "glueTableHint": table_match.group(1) if table_match else "",
            })

    group_profile: dict[str, Any] = {}
    profile_match = re.search(
        r"Group setup profile:\s*(.*?)(?:\n\nUser request:|\nUser request:|\Z)",
        prompt,
        re.DOTALL,
    )
    if profile_match:
        profile_text = profile_match.group(1)
        kind_match = re.search(r"(?:^|[;\n-])\s*kind[:=]\s*([^;\n]+)", profile_text, re.MULTILINE)
        columns_match = re.search(r"(?:^|[;\n-])\s*columns[:=]\s*([^;\n]+)", profile_text, re.MULTILINE)
        if kind_match:
            group_profile["kind"] = kind_match.group(1).strip()
        if columns_match:
            group_profile["columns"] = [
                item.strip()
                for item in columns_match.group(1).split(",")
                if item.strip()
            ]

    structured_facts: dict[str, Any] = {}
    facts_match = re.search(
        r"^Structured text facts:\s*sources=(\d+);\s*lookupKeys=(\d+);\s*types=([^\n]+)$",
        prompt,
        re.MULTILINE,
    )
    if facts_match:
        fact_types = [
            item.strip()
            for item in facts_match.group(3).split(",")
            if item.strip()
        ]
        structured_facts = {
            "counts": {
                "factSources": int(facts_match.group(1)),
                "lookupKeys": int(facts_match.group(2)),
                "types": {fact_type: 1 for fact_type in fact_types},
            },
        }

    return {
        "projectName": project_match.group(1).strip() if project_match else "Selected project",
        "projectId": project_match.group(2).strip() if project_match and project_match.group(2) else "",
        "groupName": group_match.group(1).strip(),
        "groupType": "selected",
        "tableHints": table_hints,
        "files": files,
        "groupProfile": group_profile,
        "structuredFacts": structured_facts,
    }


def _user_request_from_scoped_prompt(prompt: str) -> str:
    match = re.search(r"(?:^|\n)User request:\n(.*)\Z", prompt or "", re.DOTALL)
    if match:
        return match.group(1).strip()
    return prompt


def _looks_like_group_inventory_request(prompt: str) -> bool:
    text = _normalize_lookup_text(prompt)
    return (
        ("list" in text or "show" in text or "summarize" in text or "describe" in text or "explain" in text)
        and (
            "available files" in text
            or "available tables" in text
            or ("available" in text and "tables" in text)
            or "files and tables" in text
            or "files found" in text
            or "tables in this group" in text
            or ("files" in text and "likely intent" in text)
            or ("files" in text and "intent" in text)
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
    group_profile = context.get("groupProfile") or {}
    fact_counts = (context.get("structuredFacts") or {}).get("counts") or group_profile.get("factIndex") or {}
    if fact_counts.get("factSources") or fact_counts.get("sourceCount"):
        type_counts = fact_counts.get("types") or {}
        type_text = ", ".join(str(key) for key in type_counts.keys()) or "generic text"
        lines.extend([
            "",
            "Structured text facts",
            f"- Indexed {fact_counts.get('factSources') or fact_counts.get('sourceCount')} text fact sources with {fact_counts.get('lookupKeys') or fact_counts.get('lookupKeyCount') or 0} lookup keys. Types: {type_text}.",
        ])
    starter_prompts = group_profile.get("starterPrompts") or []
    if starter_prompts:
        lines.extend(["", "Suggested starter prompts"])
        lines.extend(f"- {prompt}" for prompt in starter_prompts[:8])
    return "\n".join(lines)


def _group_name_to_table_token(group_name: str) -> str:
    normalized = _normalize_lookup_text(group_name).replace(" ", "_")
    if normalized.startswith("project_"):
        normalized = normalized[len("project_"):]
    return normalized


def _context_with_glue_table_hints(context: dict[str, Any] | None) -> dict[str, Any] | None:
    if not context:
        return None
    if context.get("tableHints"):
        return context
    group_name = str(context.get("groupName") or "")
    token = _group_name_to_table_token(group_name)
    if not token:
        return context
    try:
        paginator = glue.get_paginator("get_tables")
        hints: list[str] = []
        for page in paginator.paginate(DatabaseName=GLUE_DATABASE):
            for table in page.get("TableList", []):
                name = table.get("Name", "")
                if token in name.lower():
                    hints.append(name)
        if hints:
            return {**context, "tableHints": sorted(set(hints))}
    except Exception as e:
        log.warning("Glue table hint fill failed: %s", e)
    return context


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


def _table_for_fragments(fragments: tuple[str, ...], context: dict[str, Any] | None, prompt: str) -> str | None:
    context_hints = (context or {}).get("tableHints") or []
    for fragment in fragments:
        scoped = [table for table in context_hints if fragment.lower() in str(table).lower()]
        if len(scoped) == 1:
            return scoped[0]
    for fragment in fragments:
        table = _table_for_fragment(fragment, context, prompt)
        if table:
            return table
    return None


def _glue_columns_for_table(table_name: str | None) -> set[str]:
    if not table_name or not GLUE_DATABASE:
        return set()
    try:
        table = glue.get_table(DatabaseName=GLUE_DATABASE, Name=table_name).get("Table", {})
        columns = table.get("StorageDescriptor", {}).get("Columns", [])
        return {str(column.get("Name") or "").lower() for column in columns}
    except Exception as e:
        log.warning("Glue column lookup failed for %s: %s", table_name, e)
        return set()


def _row_value(row: dict[str, str | None], aliases: tuple[str, ...]) -> str:
    normalized = {_normalize_lookup_text(key).replace(" ", "_"): value for key, value in row.items()}
    for alias in aliases:
        value = normalized.get(_normalize_lookup_text(alias).replace(" ", "_"))
        if value not in (None, ""):
            return str(value)
    return ""


def _to_float(value: str | None) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(re.sub(r"[^0-9.\-]+", "", str(value)))
    except ValueError:
        return None


def _preview_table(fragment: str, context: dict[str, Any] | None, prompt: str, limit: int = 200) -> tuple[str | None, list[dict[str, str | None]], str | None]:
    table = _table_for_fragment(fragment, context, prompt)
    if not table:
        return None, [], f"`{fragment}`: no single matching Glue table"
    try:
        return table, _athena_rows(f'SELECT * FROM "{table}" LIMIT {limit}'), None
    except Exception as e:
        return table, [], f"`{table}`: {e}"


def _looks_like_helios_project_risk_request(prompt: str, context: dict[str, Any] | None) -> bool:
    text = _normalize_lookup_text(prompt)
    context_text = _normalize_lookup_text(
        " ".join(str((context or {}).get(key) or "") for key in ("projectId", "projectName", "groupName"))
    )
    scope = f"{text} {context_text}"
    return (
        ("helios" in scope or "ridge" in scope)
        and any(term in text for term in (
            "analyze",
            "risk",
            "risks",
            "problems",
            "budget",
            "workstream",
            "sensor",
            "vendor",
            "commitments",
            "action plan",
        ))
    )


def _handle_helios_project_risk_request(prompt: str, context: dict[str, Any] | None) -> str | None:
    if not _looks_like_helios_project_risk_request(prompt, context):
        return None

    table_specs = {
        "Budget ledger": "budget_ledger",
        "Workstream status": "workstream_status",
        "Sensor test results": "sensor_test_results",
        "Vendor commitments": "vendor_commitments",
        "Risk register": "risk_register",
    }
    loaded: dict[str, tuple[str, list[dict[str, str | None]]]] = {}
    issues: list[str] = []
    for title, fragment in table_specs.items():
        table, rows, error = _preview_table(fragment, context, prompt)
        if error:
            issues.append(error)
        if table:
            loaded[title] = (table, rows)

    budget_rows: list[dict[str, str]] = []
    for row in loaded.get("Budget ledger", ("", []))[1]:
        original = _to_float(_row_value(row, ("original_budget", "budget", "approved_budget")))
        actual = _to_float(_row_value(row, ("actual_or_forecast_cost", "actual_cost", "forecast_cost")))
        risk = _row_value(row, ("risk_level", "rag_status", "status")).lower()
        if original is None or actual is None:
            continue
        variance = actual - original
        if variance > 0 or risk in {"high", "red"}:
            budget_rows.append({
                "cost_id": _row_value(row, ("cost_id", "id")),
                "category": _row_value(row, ("cost_category", "category", "cost_item")),
                "original_budget": f"{original:.2f}",
                "actual_or_forecast": f"{actual:.2f}",
                "variance": f"{variance:.2f}",
                "risk_level": _row_value(row, ("risk_level", "rag_status", "status")),
                "notes": _row_value(row, ("notes", "description")),
            })
    budget_rows.sort(key=lambda row: _to_float(row.get("variance")) or 0, reverse=True)

    workstream_rows: list[dict[str, str]] = []
    for row in loaded.get("Workstream status", ("", []))[1]:
        rag = _row_value(row, ("rag_status", "risk_level")).lower()
        status = _row_value(row, ("status", "workstream_status")).lower()
        if rag in {"red", "yellow", "high"} or status not in {"", "complete", "completed", "done"}:
            workstream_rows.append({
                "workstream_id": _row_value(row, ("workstream_id", "id")),
                "workstream": _row_value(row, ("workstream_name", "name")),
                "owner": _row_value(row, ("owner_group", "owner")),
                "status": _row_value(row, ("status", "workstream_status")),
                "percent_complete": _row_value(row, ("percent_complete", "completion_percent")),
                "original_due": _row_value(row, ("original_due_date", "due_date")),
                "forecast": _row_value(row, ("current_forecast_date", "forecast_date")),
                "rag": _row_value(row, ("rag_status", "risk_level")),
                "notes": _row_value(row, ("notes", "description")),
            })

    sensor_rows: list[dict[str, str]] = []
    for row in loaded.get("Sensor test results", ("", []))[1]:
        status = _row_value(row, ("test_status", "status")).lower()
        variance = _to_float(_row_value(row, ("calibration_variance_percent", "variance_percent", "variance")))
        if status in {"fail", "failed", "marginal"} or (variance is not None and variance >= 3):
            sensor_rows.append({
                "sensor_id": _row_value(row, ("sensor_id", "id")),
                "location": _row_value(row, ("field_location", "location")),
                "type": _row_value(row, ("sensor_type", "type")),
                "test_status": _row_value(row, ("test_status", "status")),
                "variance_percent": "" if variance is None else f"{variance:.2f}",
                "hardware_batch": _row_value(row, ("hardware_batch", "batch")),
            })

    vendor_rows: list[dict[str, str]] = []
    for row in loaded.get("Vendor commitments", ("", []))[1]:
        row_text = _normalize_lookup_text(" ".join(str(value or "") for value in row.values()))
        if any(term in row_text for term in ("delayed", "delay", "at risk", "blocked", "red", "yellow", "late")):
            vendor_rows.append({
                "vendor": _row_value(row, ("vendor_name", "vendor")),
                "commitment": _row_value(row, ("commitment", "deliverable", "description", "item")),
                "status": _row_value(row, ("delivery_status", "status", "rag_status")),
                "due_date": _row_value(row, ("due_date", "target_date", "commitment_date")),
                "owner": _row_value(row, ("owner", "owner_group")),
                "notes": _row_value(row, ("notes", "risk_notes", "description")),
            })

    risk_rows: list[dict[str, str]] = []
    for row in loaded.get("Risk register", ("", []))[1]:
        row_text = _normalize_lookup_text(" ".join(str(value or "") for value in row.values()))
        status = _row_value(row, ("status", "risk_status")).lower()
        if any(term in row_text for term in ("high", "red", "open", "unresolved", "mitigation", "dependency")) and status not in {"closed", "resolved"}:
            risk_rows.append({
                "risk_id": _row_value(row, ("risk_id", "id")),
                "risk": _row_value(row, ("risk_description", "description", "risk")),
                "owner": _row_value(row, ("owner", "owner_group")),
                "likelihood": _row_value(row, ("likelihood", "probability")),
                "impact": _row_value(row, ("impact", "severity")),
                "status": _row_value(row, ("status", "risk_status")),
                "notes": _row_value(row, ("mitigation", "notes", "next_steps")),
            })

    loaded_table_lines = [
        f"- {title}: `{table}` ({len(rows)} preview rows)"
        for title, (table, rows) in loaded.items()
    ]
    lines = [
        "Project_Helios_Ridge deterministic project-risk report",
        "",
        "Scope",
        *loaded_table_lines,
        "",
        "Highest-priority issues",
        "- Budget pressure: review the largest positive cost variances and high-risk budget rows first.",
        "- Schedule pressure: red/yellow or incomplete workstreams should be handled as the near-term execution risk queue.",
        "- Technical quality: failed or high-variance sensor tests need retest/root-cause work before dashboard or operations sign-off.",
        "- Vendor delivery: delayed or at-risk commitments are likely dependencies for the schedule and risk register.",
        "- Risk governance: unresolved high-impact risks should get named owners and due dates.",
        "",
        "Budget overruns and high-risk costs",
        "",
        _format_rows_as_markdown(budget_rows[:10], "No budget overruns or high-risk budget rows found in the preview."),
        "",
        "Delayed or at-risk workstreams",
        "",
        _format_rows_as_markdown(workstream_rows[:10], "No delayed or at-risk workstreams found in the preview."),
        "",
        "Failed or marginal sensor tests",
        "",
        _format_rows_as_markdown(sensor_rows[:10], "No failed or marginal sensor tests found in the preview."),
        "",
        "Vendor delivery issues",
        "",
        _format_rows_as_markdown(vendor_rows[:10], "No delayed or at-risk vendor commitments found in the preview."),
        "",
        "Open risk-register items",
        "",
        _format_rows_as_markdown(risk_rows[:10], "No open high-priority risk-register rows found in the preview."),
        "",
        "Action plan",
        "- Engineering: triage red/yellow workstreams and failed sensor tests; publish revised recovery dates.",
        "- Data Platform: confirm ingestion dependencies and unblock any project data-path issues.",
        "- Operations: pressure-test vendor commitments against field readiness and due dates.",
        "- Finance/PMO: review budget variances and decide whether to reforecast or hold spending.",
        "- Project owner: assign one owner per unresolved risk and review progress daily until the red/yellow queue clears.",
    ]
    if issues:
        lines.extend(["", "Query issues"])
        lines.extend(f"- {issue}" for issue in issues)
    return "\n".join(lines)


def _looks_like_daily_sales_multi_zone_request(prompt: str, context: dict[str, Any] | None) -> bool:
    text = _normalize_lookup_text(prompt)
    context_text = _normalize_lookup_text(
        " ".join(str((context or {}).get(key) or "") for key in ("projectId", "projectName", "groupName"))
    )
    scope = f"{text} {context_text}"
    profile = (context or {}).get("groupProfile") or {}
    table_text = _normalize_lookup_text(" ".join(str(table) for table in ((context or {}).get("tableHints") or [])))
    sales_scope = (
        "daily sales" in scope
        or profile.get("kind") == "sales"
        or "mountain west electronics" in scope
        or "midwest electronics" in scope
        or ("electronics" in scope and ("sales" in scope or "line revenue" in table_text))
        or "line revenue" in table_text
        or "sales" in table_text
    )
    return (
        sales_scope
        and any(term in text for term in (
            "branch", "store", "stores", "product", "category", "channel",
            "best", "worst", "highest", "lowest", "sales", "revenue",
            "quantity", "rank", "margin", "management", "business",
            "notes", "performance", "strong", "weak",
        ))
    )


def _table_has_sales_columns(table: str) -> bool:
    columns = _glue_columns_for_table(table)
    aliases = _sales_column_aliases(columns)
    required = ("branch_city", "branch_state", "part_sku", "part_name", "part_category", "quantity_sold", "line_revenue")
    return all(aliases.get(key) for key in required)


def _sales_column_aliases(columns: set[str]) -> dict[str, str]:
    candidates = {
        "branch_city": ("branch_city", "city", "store_city"),
        "branch_state": ("branch_state", "state", "branch_st", "store_state"),
        "part_sku": ("part_sku", "product_sku", "sku", "item_sku"),
        "part_name": ("part_name", "product_name", "item_name", "name"),
        "part_category": ("part_category", "category", "product_category", "item_category"),
        "sales_channel": ("sales_channel", "channel"),
        "customer_type": ("customer_type", "customer_segment"),
        "quantity_sold": ("quantity_sold", "quantity", "qty", "units_sold", "units"),
        "unit_cost": ("unit_cost", "cost"),
        "unit_price": ("unit_price", "price"),
        "line_revenue": ("line_revenue", "net_sales", "gross_sales", "revenue", "sales_amount", "amount"),
        "estimated_margin": ("estimated_margin", "margin", "gross_margin"),
    }
    aliases: dict[str, str] = {}
    for canonical, options in candidates.items():
        match = next((option for option in options if option in columns), "")
        if match:
            aliases[canonical] = match
    return aliases


def _sales_select_expr(table: str, label: str) -> str:
    columns = _glue_columns_for_table(table)
    aliases = _sales_column_aliases(columns)

    def ident(canonical: str, default: str = "NULL") -> str:
        column = aliases.get(canonical)
        return f'"{column}"' if column else default

    quantity = ident("quantity_sold", "0")
    revenue = ident("line_revenue", "0")
    unit_cost = ident("unit_cost", "NULL")
    unit_price = ident("unit_price", "NULL")
    estimated_margin = aliases.get("estimated_margin")
    margin_expr = (
        f'CAST("{estimated_margin}" AS DOUBLE)'
        if estimated_margin
        else f"CAST(({unit_price} - {unit_cost}) * {quantity} AS DOUBLE)"
        if unit_cost != "NULL" and unit_price != "NULL"
        else "NULL"
    )
    return f"""
        SELECT
            '{label}' AS source_table,
            CAST({ident("branch_city")} AS VARCHAR) AS branch_city,
            CAST({ident("branch_state")} AS VARCHAR) AS branch_state,
            CAST({ident("part_sku")} AS VARCHAR) AS part_sku,
            CAST({ident("part_name")} AS VARCHAR) AS part_name,
            CAST({ident("part_category")} AS VARCHAR) AS part_category,
            CAST({ident("sales_channel", "'unknown'")} AS VARCHAR) AS sales_channel,
            CAST({ident("customer_type", "'unknown'")} AS VARCHAR) AS customer_type,
            CAST({quantity} AS DOUBLE) AS quantity_sold,
            CAST({unit_cost} AS DOUBLE) AS unit_cost,
            CAST({unit_price} AS DOUBLE) AS unit_price,
            CAST({revenue} AS DOUBLE) AS line_revenue,
            {margin_expr} AS estimated_margin
        FROM "{table}"
    """


def _daily_sales_tables(prompt: str, context: dict[str, Any] | None = None) -> list[tuple[str, str]]:
    context_hints = (context or {}).get("tableHints") or []
    scoped_sales = [
        str(table)
        for table in context_hints
        if _table_has_sales_columns(str(table))
    ]
    if scoped_sales:
        return [(str(index + 1), table) for index, table in enumerate(scoped_sales)]
    if (context or {}).get("groupName"):
        return []

    text = _normalize_lookup_text(prompt)
    requested = sorted({int(zone) for zone in re.findall(r"\bzone\s+([1-6])\b", text)})
    if "1 through daily sales zone 6" in text or "1 through 6" in text or "zones 1 through 6" in text:
        requested = [1, 2, 3, 4, 5, 6]
    if not requested:
        requested = [1, 2, 3, 4, 5, 6]

    tables: list[tuple[str, str]] = []
    for zone in requested:
        matches = _matching_glue_tables(f"daily_sales_zone_{zone}", {"sourcePrompt": prompt})
        exact = [name for name in matches if name.endswith(f"daily_sales_zone_{zone}")]
        selected = exact[0] if len(exact) == 1 else matches[0] if len(matches) == 1 else None
        if selected:
            tables.append((str(zone), selected))
    return tables


def _handle_daily_sales_multi_zone_request(prompt: str, context: dict[str, Any] | None) -> str | None:
    if not _looks_like_daily_sales_multi_zone_request(prompt, context):
        return None

    tables = _daily_sales_tables(prompt, context)
    if not tables:
        group_name = (context or {}).get("groupName")
        if group_name:
            return (
                f"I could not resolve sales-shaped Glue tables for the selected `{group_name}` group yet. "
                "I did not use tables from any other group. The group may still be materializing/indexing, or its table hints may not have been published yet. "
                f"Try: `List the available files and tables in this {group_name} group and briefly explain what each one appears to contain.`"
            )
        return "I could not resolve any sales-shaped Glue tables for this request."

    union_sql = "\nUNION ALL\n".join(_sales_select_expr(table, label) for label, table in tables)
    branch_sql = f"""
        WITH sales AS ({union_sql})
        SELECT
            CONCAT(branch_city, ', ', branch_state) AS branch,
            COUNT(*) AS transaction_lines,
            SUM(quantity_sold) AS quantity_sold,
            ROUND(SUM(line_revenue), 2) AS total_revenue,
            MAX_BY(part_category, line_revenue) AS top_category
        FROM sales
        GROUP BY branch_city, branch_state
        ORDER BY total_revenue DESC
        LIMIT 12
    """
    product_sql = f"""
        WITH sales AS ({union_sql})
        SELECT
            part_sku,
            part_name,
            part_category,
            SUM(quantity_sold) AS quantity_sold,
            ROUND(SUM(line_revenue), 2) AS total_revenue
        FROM sales
        GROUP BY part_sku, part_name, part_category
        ORDER BY total_revenue DESC
        LIMIT 10
    """
    product_bottom_sql = f"""
        WITH sales AS ({union_sql})
        SELECT
            part_sku,
            part_name,
            part_category,
            SUM(quantity_sold) AS quantity_sold,
            ROUND(SUM(line_revenue), 2) AS total_revenue
        FROM sales
        GROUP BY part_sku, part_name, part_category
        HAVING SUM(quantity_sold) > 0
        ORDER BY total_revenue ASC
        LIMIT 10
    """
    channel_sql = f"""
        WITH sales AS ({union_sql})
        SELECT
            sales_channel,
            customer_type,
            COUNT(*) AS sales_lines,
            COUNT(DISTINCT CONCAT(branch_city, '|', branch_state)) AS branches,
            COUNT(DISTINCT part_sku) AS products,
            SUM(quantity_sold) AS quantity_sold,
            ROUND(SUM(line_revenue), 2) AS total_revenue
        FROM sales
        GROUP BY sales_channel, customer_type
        ORDER BY total_revenue DESC
        LIMIT 12
    """
    margin_sql = f"""
        WITH sales AS ({union_sql})
        SELECT
            CONCAT(branch_city, ', ', branch_state) AS branch,
            ROUND(SUM(COALESCE(estimated_margin, (unit_price - unit_cost) * quantity_sold)), 2) AS estimated_margin,
            ROUND(100 * SUM(COALESCE(estimated_margin, (unit_price - unit_cost) * quantity_sold)) / NULLIF(SUM(line_revenue), 0), 2) AS margin_percent,
            ROUND(SUM(line_revenue), 2) AS total_revenue
        FROM sales
        GROUP BY branch_city, branch_state
        ORDER BY estimated_margin DESC
        LIMIT 12
    """

    errors: list[str] = []
    try:
        branch_rows = _athena_rows(branch_sql)
    except Exception as e:
        branch_rows = []
        errors.append(f"Branch ranking: {e}")
    try:
        product_rows = _athena_rows(product_sql)
    except Exception as e:
        product_rows = []
        errors.append(f"Top product ranking: {e}")
    try:
        product_bottom_rows = _athena_rows(product_bottom_sql)
    except Exception as e:
        product_bottom_rows = []
        errors.append(f"Bottom product ranking: {e}")
    try:
        channel_rows = _athena_rows(channel_sql)
    except Exception as e:
        channel_rows = []
        errors.append(f"Channel summary: {e}")
    try:
        margin_rows = _athena_rows(margin_sql)
    except Exception as e:
        margin_rows = []
        errors.append(f"Margin summary: {e}")

    top_branch = branch_rows[0] if branch_rows else {}
    top_product = product_rows[0] if product_rows else {}
    bottom_product = product_bottom_rows[0] if product_bottom_rows else {}
    group_name = (context or {}).get("groupName") or "sales group"
    table_lines = [f"- `{table}`" for _, table in tables]
    lines = [
        f"{group_name} sales discovery report",
        "",
        "Scope",
        *table_lines,
        "",
        "Summary",
        f"- Top branch: {top_branch.get('branch', 'not available')} with ${top_branch.get('total_revenue', '0')} revenue and {top_branch.get('quantity_sold', '0')} units sold.",
        f"- Best-selling product by revenue: {top_product.get('part_name', 'not available')} ({top_product.get('part_sku', '')}) with ${top_product.get('total_revenue', '0')} revenue and {top_product.get('quantity_sold', '0')} units sold.",
        f"- Lowest-selling product by revenue: {bottom_product.get('part_name', 'not available')} ({bottom_product.get('part_sku', '')}) with ${bottom_product.get('total_revenue', '0')} revenue and {bottom_product.get('quantity_sold', '0')} units sold.",
        "",
        "Branch revenue ranking",
        "",
        _format_rows_as_markdown(branch_rows, "No branch rows returned."),
        "",
        "Top products by revenue",
        "",
        _format_rows_as_markdown(product_rows, "No product rows returned."),
        "",
        "Lowest products by revenue",
        "",
        _format_rows_as_markdown(product_bottom_rows, "No bottom-product rows returned."),
        "",
        "Sales channel and customer type mix",
        "",
        _format_rows_as_markdown(channel_rows, "No channel rows returned."),
        "",
        "Estimated margin by branch",
        "",
        _format_rows_as_markdown(margin_rows, "No margin rows returned."),
        "",
        "Suggested follow-up prompts",
        f"- For this {group_name} group, rank stores from highest to lowest total sales. Include branch city, branch state, total revenue, units sold, transaction count, top category, and a short explanation.",
        f"- For this {group_name} group, rank product categories by revenue and units sold. Include part category, total revenue, units sold, average unit price, and the leading branch if available.",
        f"- For this {group_name} group, compare sales channels by revenue, units sold, transaction count, and average line revenue. Include a short explanation of channel mix.",
        f"- For this {group_name} group, analyze gross margin using Unit_Cost and Unit_Price. Rank stores or products by estimated margin dollars and margin percent.",
        f"- For this {group_name} group, find underperforming stores by total revenue and units sold. Include branch city, branch state, total revenue, units sold, transaction count, and likely next review question.",
    ]
    if errors:
        lines.extend(["", "Query issues"])
        lines.extend(f"- {error}" for error in errors)
    return "\n".join(lines)


def _looks_like_storm_glass_claim_review_request(prompt: str, context: dict[str, Any] | None) -> bool:
    text = _normalize_lookup_text(prompt)
    context_text = _normalize_lookup_text(
        " ".join(str((context or {}).get(key) or "") for key in ("projectId", "projectName", "groupName"))
    )
    table_text = _normalize_lookup_text(" ".join(str(table) for table in ((context or {}).get("tableHints") or [])))
    scope = f"{text} {context_text} {table_text}"
    cross_evidence_terms = (
        "invoice",
        "invoices",
        "benchmark",
        "benchmarks",
        "weather",
        "policy",
        "upgrade",
        "upgrades",
        "call",
        "logs",
        "notes",
        "siu",
    )
    followup_terms = (
        "claim ids above",
        "claim id above",
        "top storm glass claim",
        "claim packet",
        "claim level",
        "evidence summary",
    )
    if (
        ("list" in text or "show" in text or "summarize" in text or "describe" in text or "explain" in text)
        and ("available" in text or "tables" in text or "files" in text)
    ):
        return False
    storm_scope = "storm glass" in scope or "storm_glass" in scope
    has_claim = "claim" in text or "claims" in text
    cross_evidence_count = sum(1 for term in cross_evidence_terms if term in text)
    return (
        storm_scope
        and has_claim
        and (
            (
                ("normal" in text or "combined" in text or "cross" in text or "review" in text or "suspicious" in text)
                and cross_evidence_count >= 3
            )
            or (
                any(term in text for term in followup_terms)
                and cross_evidence_count >= 2
            )
        )
    )


def _storm_glass_token(prompt: str, context: dict[str, Any] | None) -> str:
    text = " ".join([
        str((context or {}).get("groupName") or ""),
        str((context or {}).get("projectName") or ""),
        str((context or {}).get("projectId") or ""),
        " ".join(str(table) for table in ((context or {}).get("tableHints") or [])),
        prompt or "",
    ]).lower()
    match = re.search(r"storm[\s_-]*glass[\s_-]*0?(\d+)", text)
    if match:
        return f"storm_glass_{int(match.group(1)):02d}"
    return "storm_glass_01"


def _storm_glass_tables(token: str, fragment: str, context: dict[str, Any] | None, prompt: str) -> list[str]:
    return _matching_glue_tables(f"{token}_{fragment}", {**(context or {}), "sourcePrompt": prompt})


def _union_from_tables(tables: list[str], select_body: str, where: str = "") -> str:
    return "\nUNION ALL\n".join(
        f"SELECT {select_body} FROM \"{table}\"{(' WHERE ' + where) if where else ''}"
        for table in tables
    )


def _storm_glass_02_claims_select(table: str) -> str:
    columns = _glue_columns_for_table(table)

    def col(*names: str, default: str = "NULL") -> str:
        match = next((name for name in names if name in columns), "")
        return f'"{match}"' if match else default

    return f"""
        SELECT
            CAST({col("claim_id")} AS VARCHAR) AS claim_id,
            CAST({col("policy_id")} AS VARCHAR) AS policy_id,
            CAST({col("loss_date")} AS VARCHAR) AS loss_date,
            CAST({col("city")} AS VARCHAR) AS city,
            CAST({col("zip")} AS VARCHAR) AS zip,
            CAST({col("claim_type", "line", "peril", "loss_type")} AS VARCHAR) AS claim_type,
            CAST({col("status", default="'open'")} AS VARCHAR) AS claim_status,
            CAST({col("assigned_adjuster", "adjuster", "adjuster_id")} AS VARCHAR) AS assigned_adjuster,
            TRY_CAST({col("claimed_amount", "amount", "reserve_amount", default="0")} AS DOUBLE) AS estimated_loss,
            CAST({col("primary_vendor", "vendor", "contractor", "vendor_id")} AS VARCHAR) AS primary_vendor,
            CAST({col("fraud_cluster", "suspicious", "suspicion_hint", "fraud_seed_flag", default="''")} AS VARCHAR) AS fraud_cluster,
            CAST({col("embedded_flags", "flags", default="''")} AS VARCHAR) AS embedded_flags
        FROM "{table}"
        WHERE CAST({col("claim_id")} AS VARCHAR) <> 'claim_id'
    """


def _handle_storm_glass_02_claim_review_request(prompt: str, context: dict[str, Any] | None, storm_token: str, storm_label: str) -> str:
    claims_tables = _storm_glass_tables(storm_token, "claims_batch", context, prompt)
    invoice_tables = _storm_glass_tables(storm_token, "contractor_invoice_detail", context, prompt)
    policy_tables = _storm_glass_tables(storm_token, "policy_master_coverage_changes", context, prompt)
    weather_tables = _storm_glass_tables(storm_token, "weather_claim_match", context, prompt)
    call_tables = _storm_glass_tables(storm_token, "call_center_logs", context, prompt)
    siu_tables = _storm_glass_tables(storm_token, "fraud_scoring_export", context, prompt)
    benchmark_tables = _storm_glass_tables(storm_token, "regional_cost_benchmarks", context, prompt)

    missing = []
    if not claims_tables:
        missing.append("claims_batch")
    if not invoice_tables:
        missing.append("contractor_invoice_detail")
    if not weather_tables:
        missing.append("weather_claim_match")
    if missing:
        return (
            f"I could not resolve all {storm_label} tables needed for the cross-evidence claim review. "
            f"Missing: {', '.join(missing)}."
        )

    claims_union = "\nUNION ALL\n".join(_storm_glass_02_claims_select(table) for table in claims_tables)
    invoice_union = _union_from_tables(
        invoice_tables,
        """
            CAST(claim_id AS VARCHAR) AS claim_id,
            CAST(vendor AS VARCHAR) AS vendor,
            CAST(invoice_date AS VARCHAR) AS invoice_date,
            CAST(description AS VARCHAR) AS description,
            TRY_CAST(total_amount AS DOUBLE) AS invoice_amount
        """,
        "CAST(claim_id AS VARCHAR) <> 'claim_id'",
    )
    weather_union = _union_from_tables(
        weather_tables,
        """
            CAST(claim_id AS VARCHAR) AS claim_id,
            CAST(zip AS VARCHAR) AS zip,
            CAST(event_date AS VARCHAR) AS event_date,
            TRY_CAST(hail_inches AS DOUBLE) AS hail_inches,
            TRY_CAST(wind_mph AS DOUBLE) AS wind_mph,
            TRY_CAST(storm_cell_distance_miles AS DOUBLE) AS storm_cell_distance_miles,
            CAST(supports_reported_loss AS VARCHAR) AS supports_reported_loss
        """,
        "CAST(claim_id AS VARCHAR) <> 'claim_id'",
    )
    policy_union = _union_from_tables(
        policy_tables,
        """
            CAST(policy_id AS VARCHAR) AS policy_id,
            CAST(insured_id AS VARCHAR) AS insured_id,
            CAST(coverage AS VARCHAR) AS coverage,
            TRY_CAST(limit AS DOUBLE) AS coverage_limit,
            TRY_CAST(deductible AS DOUBLE) AS deductible,
            CAST(effective_date AS VARCHAR) AS effective_date,
            CAST(recent_upgrade AS VARCHAR) AS recent_upgrade
        """,
        "CAST(policy_id AS VARCHAR) <> 'policy_id'",
    ) if policy_tables else "SELECT CAST(NULL AS VARCHAR) AS policy_id, CAST(NULL AS VARCHAR) AS insured_id, CAST(NULL AS VARCHAR) AS coverage, CAST(NULL AS DOUBLE) AS coverage_limit, CAST(NULL AS DOUBLE) AS deductible, CAST(NULL AS VARCHAR) AS effective_date, CAST(NULL AS VARCHAR) AS recent_upgrade"
    call_union = _union_from_tables(
        call_tables,
        """
            CAST(col1 AS VARCHAR) AS claim_id,
            CAST(col2 AS VARCHAR) AS call_date,
            CAST(col3 AS VARCHAR) AS caller_type,
            CAST(col4 AS VARCHAR) AS summary,
            CAST(col5 AS VARCHAR) AS early_contact_flag
        """,
        "col0 <> 'call_id'",
    ) if call_tables else "SELECT CAST(NULL AS VARCHAR) AS claim_id, CAST(NULL AS VARCHAR) AS call_date, CAST(NULL AS VARCHAR) AS caller_type, CAST(NULL AS VARCHAR) AS summary, CAST(NULL AS VARCHAR) AS early_contact_flag"
    siu_union = _union_from_tables(
        siu_tables,
        """
            CAST(claim_id AS VARCHAR) AS claim_id,
            TRY_CAST(fraud_score AS DOUBLE) AS fraud_score,
            CAST(score_band AS VARCHAR) AS score_band,
            CAST(drivers AS VARCHAR) AS drivers,
            CAST(siu_referral_status AS VARCHAR) AS siu_referral_status
        """,
        "CAST(claim_id AS VARCHAR) <> 'claim_id'",
    ) if siu_tables else "SELECT CAST(NULL AS VARCHAR) AS claim_id, CAST(NULL AS DOUBLE) AS fraud_score, CAST(NULL AS VARCHAR) AS score_band, CAST(NULL AS VARCHAR) AS drivers, CAST(NULL AS VARCHAR) AS siu_referral_status"
    benchmark_union = _union_from_tables(
        benchmark_tables,
        """
            CAST(city AS VARCHAR) AS city,
            CAST(zip AS VARCHAR) AS zip,
            TRY_CAST(roof_repair_benchmark_high AS DOUBLE) AS benchmark_high
        """,
        "CAST(zip AS VARCHAR) <> 'zip'",
    ) if benchmark_tables else "SELECT CAST(NULL AS VARCHAR) AS city, CAST(NULL AS VARCHAR) AS zip, CAST(NULL AS DOUBLE) AS benchmark_high"

    review_sql = f"""
        WITH claims AS (
            SELECT DISTINCT * FROM ({claims_union})
        ),
        invoices AS ({invoice_union}),
        weather AS ({weather_union}),
        policies AS ({policy_union}),
        calls AS ({call_union}),
        siu AS ({siu_union}),
        benchmarks AS ({benchmark_union}),
        invoice_rollup AS (
            SELECT
                claim_id,
                COUNT(*) AS invoice_count,
                ROUND(SUM(COALESCE(invoice_amount, 0)), 2) AS invoice_total,
                MAX_BY(vendor, invoice_amount) AS top_vendor,
                ARRAY_JOIN(SLICE(ARRAY_AGG(description), 1, 2), ' | ') AS invoice_signals
            FROM invoices
            WHERE claim_id IS NOT NULL
            GROUP BY claim_id
        ),
        weather_rollup AS (
            SELECT
                claim_id,
                MAX(hail_inches) AS max_hail_inches,
                MAX(wind_mph) AS max_wind_mph,
                MIN(storm_cell_distance_miles) AS nearest_storm_miles,
                SUM(CASE WHEN LOWER(COALESCE(supports_reported_loss, '')) IN ('n', 'no', 'false') THEN 1 ELSE 0 END) AS unsupported_weather_rows,
                COUNT(*) AS nearby_weather_events
            FROM weather
            WHERE claim_id IS NOT NULL
            GROUP BY claim_id
        ),
        call_rollup AS (
            SELECT
                claim_id,
                COUNT(*) AS call_count,
                SUM(CASE WHEN LOWER(COALESCE(early_contact_flag, '')) IN ('y', 'yes', 'true') THEN 1 ELSE 0 END) AS early_contact_calls,
                ARRAY_JOIN(SLICE(ARRAY_AGG(summary), 1, 2), ' | ') AS call_signals
            FROM calls
            WHERE claim_id IS NOT NULL
            GROUP BY claim_id
        ),
        scored AS (
            SELECT
                c.claim_id,
                c.policy_id,
                c.loss_date,
                c.zip,
                c.city,
                c.claim_type AS loss_type,
                c.claim_status,
                c.assigned_adjuster,
                c.estimated_loss,
                c.primary_vendor,
                COALESCE(i.invoice_total, 0) AS invoice_total,
                COALESCE(i.invoice_count, 0) AS invoice_count,
                COALESCE(i.top_vendor, c.primary_vendor, '') AS top_vendor,
                i.invoice_signals,
                MAX(b.benchmark_high) AS benchmark_high,
                MAX(p.recent_upgrade) AS recent_upgrade,
                COALESCE(w.max_hail_inches, 0) AS max_hail_inches,
                COALESCE(w.max_wind_mph, 0) AS max_wind_mph,
                COALESCE(w.nearest_storm_miles, 999) AS nearest_storm_miles,
                COALESCE(w.nearby_weather_events, 0) AS nearby_weather_events,
                COALESCE(w.unsupported_weather_rows, 0) AS unsupported_weather_rows,
                COALESCE(cr.call_count, 0) AS call_count,
                COALESCE(cr.early_contact_calls, 0) AS early_contact_calls,
                COALESCE(cr.call_signals, '') AS call_signals,
                MAX(s.fraud_score) AS siu_risk_score,
                MAX(s.drivers) AS siu_drivers,
                c.fraud_cluster,
                c.embedded_flags
            FROM claims c
            LEFT JOIN invoice_rollup i ON i.claim_id = c.claim_id
            LEFT JOIN policies p ON p.policy_id = c.policy_id
            LEFT JOIN weather_rollup w ON w.claim_id = c.claim_id
            LEFT JOIN call_rollup cr ON cr.claim_id = c.claim_id
            LEFT JOIN siu s ON s.claim_id = c.claim_id
            LEFT JOIN benchmarks b ON CAST(b.zip AS VARCHAR) = CAST(c.zip AS VARCHAR)
            GROUP BY
                c.claim_id, c.policy_id, c.loss_date, c.zip, c.city, c.claim_type,
                c.claim_status, c.assigned_adjuster, c.estimated_loss, c.primary_vendor,
                i.invoice_total, i.invoice_count, i.top_vendor, i.invoice_signals,
                w.max_hail_inches, w.max_wind_mph, w.nearest_storm_miles,
                w.nearby_weather_events, w.unsupported_weather_rows,
                cr.call_count, cr.early_contact_calls, cr.call_signals,
                c.fraud_cluster, c.embedded_flags
        )
        SELECT
            claim_id,
            claim_status,
            loss_date,
            zip,
            loss_type,
            assigned_adjuster,
            ROUND(estimated_loss, 2) AS estimated_loss,
            ROUND(invoice_total, 2) AS invoice_total,
            ROUND(benchmark_high, 2) AS benchmark_high,
            top_vendor,
            recent_upgrade,
            max_hail_inches,
            max_wind_mph,
            nearest_storm_miles,
            nearby_weather_events,
            early_contact_calls,
            siu_risk_score,
            (
                CASE WHEN invoice_total > estimated_loss * 1.35 THEN 1 ELSE 0 END
                + CASE WHEN benchmark_high IS NOT NULL AND invoice_total > benchmark_high THEN 1 ELSE 0 END
                + CASE WHEN LOWER(COALESCE(recent_upgrade, '')) IN ('y', 'yes', 'true') THEN 1 ELSE 0 END
                + CASE WHEN unsupported_weather_rows > 0 OR nearest_storm_miles > 20 THEN 1 ELSE 0 END
                + CASE WHEN early_contact_calls > 0 THEN 1 ELSE 0 END
                + CASE WHEN siu_risk_score >= 70 THEN 1 ELSE 0 END
                + CASE WHEN LOWER(COALESCE(fraud_cluster, '')) NOT IN ('', 'none', 'low') THEN 1 ELSE 0 END
            ) AS review_signal_count,
            ARRAY_JOIN(
                FILTER(
                    ARRAY[
                        CASE WHEN invoice_total > estimated_loss * 1.35 THEN 'invoice total materially above estimate' END,
                        CASE WHEN benchmark_high IS NOT NULL AND invoice_total > benchmark_high THEN 'invoice total above regional benchmark' END,
                        CASE WHEN LOWER(COALESCE(recent_upgrade, '')) IN ('y', 'yes', 'true') THEN 'recent policy coverage change' END,
                        CASE WHEN unsupported_weather_rows > 0 OR nearest_storm_miles > 20 THEN 'weather support is weak or distant' END,
                        CASE WHEN early_contact_calls > 0 THEN 'early call-center contact signal' END,
                        CASE WHEN siu_risk_score >= 70 THEN 'SIU/fraud score >= 70' END,
                        CASE WHEN LOWER(COALESCE(fraud_cluster, '')) NOT IN ('', 'none', 'low') THEN 'fraud cluster flag present' END
                    ],
                    item -> item IS NOT NULL
                ),
                '; '
            ) AS review_explanation,
            invoice_signals,
            call_signals,
            siu_drivers
        FROM scored
        WHERE
            invoice_total > estimated_loss * 1.35
            OR (benchmark_high IS NOT NULL AND invoice_total > benchmark_high)
            OR LOWER(COALESCE(recent_upgrade, '')) IN ('y', 'yes', 'true')
            OR unsupported_weather_rows > 0
            OR nearest_storm_miles > 20
            OR early_contact_calls > 0
            OR siu_risk_score >= 70
            OR LOWER(COALESCE(fraud_cluster, '')) NOT IN ('', 'none', 'low')
        ORDER BY review_signal_count DESC, invoice_total DESC, claim_id
        LIMIT 12
    """
    try:
        rows = _athena_rows(review_sql)
    except Exception as e:
        return f"(query error: {e})"

    return "\n".join([
        f"{storm_label} cross-evidence claim review",
        "",
        "This deterministic review uses the selected Storm Glass group only. It joins claim batches with contractor invoice details, weather-claim matches, policy coverage changes, call-center logs, regional benchmarks, and SIU/fraud scores.",
        "",
        "Top claim review candidates",
        "",
        _format_rows_as_markdown(rows, "No cross-evidence claim review candidates found."),
        "",
        "How to read this",
        "- `review_explanation` lists which joined evidence signals put the claim into the review set.",
        "- Weather signals use the Storm Glass 02 weather-claim match tables and storm-cell distance, not Storm Glass 01 weather tables.",
        "- Invoice and call signals are short table excerpts; review source records before drawing conclusions.",
        "",
        "Suggested follow-up prompts",
        f"- Create a neutral claim-level evidence summary for the top {storm_label} claim IDs above. Include claim_id, policy_id, loss date, invoice total, benchmark high, weather support, recent upgrade flag, call context, SIU score if available, and joined data factors.",
        f"- Review {storm_label} claims where contractor invoice totals exceed regional benchmarks. Include claim_id, vendor, invoice total, benchmark high, estimated loss, and amount above benchmark.",
        f"- Validate {storm_label} weather support for the highest-value claims. Include claim_id, ZIP, loss date, invoice total, nearest storm miles, max hail inches, max wind mph, and whether weather supports the reported loss.",
    ])


def _handle_storm_glass_claim_review_request(prompt: str, context: dict[str, Any] | None) -> str | None:
    if not _looks_like_storm_glass_claim_review_request(prompt, context):
        return None

    storm_token = _storm_glass_token(prompt, context)
    storm_label = "Project_" + "_".join(part.capitalize() for part in storm_token.split("_"))
    if storm_token != "storm_glass_01":
        return _handle_storm_glass_02_claim_review_request(prompt, context, storm_token, storm_label)

    needed = {
        "claims": f"{storm_token}_01_claims_master",
        "policyholders": f"{storm_token}_02_policyholders",
        "invoices": f"{storm_token}_03_vendor_invoices",
        "calls": f"{storm_token}_05_customer_call_logs",
        "notes": f"{storm_token}_06_adjuster_notes_export",
        "benchmarks": f"{storm_token}_11_repair_cost_benchmarks",
        "weather": f"{storm_token}_12_weather_events_by_zip",
        "siu": f"{storm_token}_21_siu_risk_scores",
    }
    tables = {key: _table_for_fragment(fragment, context, prompt) for key, fragment in needed.items()}
    missing = [fragment for key, fragment in needed.items() if not tables.get(key)]
    if missing:
        return (
            "I could not resolve all Storm Glass tables needed for the cross-evidence claim review. "
            f"Missing: {', '.join(missing)}."
        )

    review_sql = f"""
        WITH invoice_rollup AS (
            SELECT
                claim_id,
                COUNT(*) AS invoice_count,
                ROUND(SUM(CAST(invoice_total AS DOUBLE)), 2) AS invoice_total,
                MAX_BY(vendor_name, CAST(invoice_total AS DOUBLE)) AS top_vendor,
                MAX(CAST(invoice_total AS DOUBLE)) AS largest_invoice
            FROM "{tables['invoices']}"
            GROUP BY claim_id
        ),
        call_rollup AS (
            SELECT
                col1 AS claim_id,
                COUNT(*) AS call_count,
                SUM(CASE WHEN LOWER(col5) IN ('frustrated', 'negative') THEN 1 ELSE 0 END) AS concern_calls,
                ARRAY_JOIN(SLICE(ARRAY_AGG(col4), 1, 2), ' | ') AS call_signals
            FROM "{tables['calls']}"
            WHERE col0 <> 'call_id'
            GROUP BY col1
        ),
        note_rollup AS (
            SELECT
                col1 AS claim_id,
                COUNT(*) AS note_count,
                SUM(
                    CASE
                        WHEN LOWER(col4) LIKE '%pre-existing%'
                          OR LOWER(col4) LIKE '%additional photos%'
                          OR LOWER(col4) LIKE '%inconsistent%'
                          OR LOWER(col4) LIKE '%question%'
                        THEN 1 ELSE 0
                    END
                ) AS review_notes,
                ARRAY_JOIN(SLICE(ARRAY_AGG(col4), 1, 2), ' | ') AS note_signals
            FROM "{tables['notes']}"
            WHERE col0 <> 'note_id'
            GROUP BY col1
        ),
        weather_rollup AS (
            SELECT
                c.claim_id,
                MAX(CAST(w.hail_inches AS DOUBLE)) AS max_hail_inches,
                MAX(CAST(w.wind_mph AS DOUBLE)) AS max_wind_mph,
                COUNT(w.storm_event_code) AS nearby_weather_events
            FROM "{tables['claims']}" c
            LEFT JOIN "{tables['weather']}" w
              ON w.zip = c.zip
             AND ABS(date_diff('day', TRY_CAST(w.weather_date AS DATE), TRY_CAST(c.loss_date AS DATE))) <= 3
            GROUP BY c.claim_id
        ),
        scored AS (
            SELECT
                c.claim_id,
                c.policy_id,
                c.loss_date,
                c.reported_date,
                c.zip,
                c.city,
                c.state,
                c.loss_type,
                c.claim_status,
                c.assigned_adjuster,
                c.estimated_loss,
                COALESCE(i.invoice_total, 0) AS invoice_total,
                COALESCE(i.invoice_count, 0) AS invoice_count,
                COALESCE(i.top_vendor, '') AS top_vendor,
                CAST(b.benchmark_high AS DOUBLE) AS benchmark_high,
                p.recent_upgrade_date,
                COALESCE(w.max_hail_inches, 0) AS max_hail_inches,
                COALESCE(w.max_wind_mph, 0) AS max_wind_mph,
                COALESCE(w.nearby_weather_events, 0) AS nearby_weather_events,
                COALESCE(cr.call_count, 0) AS call_count,
                COALESCE(cr.concern_calls, 0) AS concern_calls,
                COALESCE(cr.call_signals, '') AS call_signals,
                COALESCE(n.review_notes, 0) AS review_notes,
                COALESCE(n.note_signals, '') AS note_signals,
                TRY_CAST(s.risk_score AS DOUBLE) AS siu_risk_score,
                COALESCE(s.model_reason_1, '') AS siu_reason_1,
                COALESCE(s.model_reason_2, '') AS siu_reason_2,
                c.hidden_pattern_flag
            FROM "{tables['claims']}" c
            LEFT JOIN invoice_rollup i ON i.claim_id = c.claim_id
            LEFT JOIN "{tables['policyholders']}" p ON p.policy_id = c.policy_id
            LEFT JOIN "{tables['benchmarks']}" b ON b.state = c.state AND b.loss_type = c.loss_type
            LEFT JOIN weather_rollup w ON w.claim_id = c.claim_id
            LEFT JOIN call_rollup cr ON cr.claim_id = c.claim_id
            LEFT JOIN note_rollup n ON n.claim_id = c.claim_id
            LEFT JOIN "{tables['siu']}" s ON s.claim_id = c.claim_id
        )
        SELECT
            claim_id,
            claim_status,
            loss_date,
            zip,
            loss_type,
            assigned_adjuster,
            ROUND(CAST(estimated_loss AS DOUBLE), 2) AS estimated_loss,
            ROUND(invoice_total, 2) AS invoice_total,
            ROUND(benchmark_high, 2) AS benchmark_high,
            top_vendor,
            recent_upgrade_date,
            max_hail_inches,
            max_wind_mph,
            nearby_weather_events,
            concern_calls,
            review_notes,
            siu_risk_score,
            (
                CASE WHEN invoice_total > CAST(estimated_loss AS DOUBLE) * 1.35 THEN 1 ELSE 0 END
                + CASE WHEN benchmark_high IS NOT NULL AND invoice_total > benchmark_high THEN 1 ELSE 0 END
                + CASE WHEN recent_upgrade_date IS NOT NULL
                         AND TRY_CAST(recent_upgrade_date AS DATE) <= TRY_CAST(loss_date AS DATE)
                         AND date_diff('day', TRY_CAST(recent_upgrade_date AS DATE), TRY_CAST(loss_date AS DATE)) <= 45
                       THEN 1 ELSE 0 END
                + CASE WHEN nearby_weather_events = 0 THEN 1 ELSE 0 END
                + CASE WHEN concern_calls > 0 THEN 1 ELSE 0 END
                + CASE WHEN review_notes > 0 THEN 1 ELSE 0 END
                + CASE WHEN siu_risk_score >= 70 THEN 1 ELSE 0 END
                + CASE WHEN hidden_pattern_flag = 'true' THEN 1 ELSE 0 END
            ) AS review_signal_count,
            ARRAY_JOIN(
                FILTER(
                    ARRAY[
                        CASE WHEN invoice_total > CAST(estimated_loss AS DOUBLE) * 1.35 THEN 'invoice total materially above estimate' END,
                        CASE WHEN benchmark_high IS NOT NULL AND invoice_total > benchmark_high THEN 'invoice total above benchmark high' END,
                        CASE WHEN recent_upgrade_date IS NOT NULL
                              AND TRY_CAST(recent_upgrade_date AS DATE) <= TRY_CAST(loss_date AS DATE)
                              AND date_diff('day', TRY_CAST(recent_upgrade_date AS DATE), TRY_CAST(loss_date AS DATE)) <= 45
                             THEN 'recent policy upgrade before loss' END,
                        CASE WHEN nearby_weather_events = 0 THEN 'no nearby weather event in +/-3 days' END,
                        CASE WHEN concern_calls > 0 THEN 'customer/caller concern signal' END,
                        CASE WHEN review_notes > 0 THEN 'adjuster note requests review' END,
                        CASE WHEN siu_risk_score >= 70 THEN 'SIU model score >= 70' END,
                        CASE WHEN hidden_pattern_flag = 'true' THEN 'project hidden-pattern flag' END
                    ],
                    item -> item IS NOT NULL
                ),
                '; '
            ) AS review_explanation,
            call_signals,
            note_signals
        FROM scored
        WHERE
            invoice_total > CAST(estimated_loss AS DOUBLE) * 1.35
            OR (benchmark_high IS NOT NULL AND invoice_total > benchmark_high)
            OR (
                recent_upgrade_date IS NOT NULL
                AND TRY_CAST(recent_upgrade_date AS DATE) <= TRY_CAST(loss_date AS DATE)
                AND date_diff('day', TRY_CAST(recent_upgrade_date AS DATE), TRY_CAST(loss_date AS DATE)) <= 45
            )
            OR nearby_weather_events = 0
            OR concern_calls > 0
            OR review_notes > 0
            OR siu_risk_score >= 70
            OR hidden_pattern_flag = 'true'
        ORDER BY
            review_signal_count DESC,
            invoice_total DESC,
            claim_id
        LIMIT 12
    """
    try:
        rows = _athena_rows(review_sql)
    except Exception as e:
        return f"(query error: {e})"

    return "\n".join([
        f"{storm_label} cross-evidence claim review",
        "",
        "This deterministic review starts from the claims master and adds invoice totals, repair benchmarks, nearby weather events, recent policy upgrades, customer call logs, adjuster notes, and SIU scores. These rows are review candidates where a claim can look ordinary in the claim record but gain additional audit signals when joined to the surrounding evidence.",
        "",
        "Top claim review candidates",
        "",
        _format_rows_as_markdown(rows, "No cross-evidence claim review candidates found."),
        "",
        "How to read this",
        "- `review_explanation` lists which joined evidence signals put the claim into the review set.",
        "- `nearby_weather_events = 0` means no matching weather row for the claim ZIP within three days of loss date.",
        "- Call and note signals are short excerpts from the project tables; they should be reviewed against source records before drawing conclusions.",
        "",
        "Suggested follow-up prompts",
        "- Create a neutral claim-level evidence summary for the top Storm Glass claim IDs above. Include claim_id, policy_id, loss date, invoice total, benchmark high, weather match status, recent upgrade date, call context, note context, SIU score if available, and joined data factors.",
        "- Review Storm Glass claims where invoice totals exceed repair benchmarks. Include claim_id, loss_type, state, vendor, invoice total, benchmark high, estimated loss, and the amount above benchmark.",
        "- Find Storm Glass claims with recent policy upgrades before the loss date and supporting invoice or call-log signals. Include claim_id, policy_id, upgrade date, loss date, days between upgrade and loss, invoice total, and call or note evidence.",
        "- Validate Storm Glass weather support for the highest-value claims. Include claim_id, ZIP, loss date, loss type, invoice total, nearby weather event count, max hail inches, max wind mph, and whether weather evidence supports the claim.",
        "- Summarize Storm Glass claims using combined invoice, weather, policy upgrade, call-log, adjuster-note, and SIU-score context. Include claim_id, review_signal_count, SIU score if available, top vendor, invoice total, and concise joined data factors.",
    ])


def _looks_like_legal_department_enterprise_review(prompt: str, context: dict[str, Any] | None) -> bool:
    text = _normalize_lookup_text(prompt)
    context_text = _normalize_lookup_text(
        " ".join(str((context or {}).get(key) or "") for key in ("projectId", "projectName", "groupName"))
    )
    table_text = _normalize_lookup_text(" ".join(str(table) for table in ((context or {}).get("tableHints") or [])))
    scope = f"{text} {context_text} {table_text}"
    return (
        "legal department" in scope
        and any(term in text for term in (
            "enterprise", "repository", "review", "governance", "financial",
            "operational", "compliance", "issue", "issues", "management",
            "business observation", "observations", "compare", "marketing",
            "budget", "variance", "campaign", "vendor", "invoice", "contract",
            "claim", "claims", "relationship", "relationships", "expired",
        ))
    )


def _legal_department_tables(prompt: str, context: dict[str, Any] | None) -> dict[str, str | None]:
    return {
        "contracts": _table_for_fragment("blackstone_contract_registry", context, prompt),
        "engineering": _table_for_fragment("engineering_vendor_registry", context, prompt),
        "relationships": _table_for_fragment("enterprise_relationship_index", context, prompt),
        "invoices": _table_for_fragment("ledger_ap_invoice_export", context, prompt),
        "marketing": _table_for_fragment("marketing_budget_export", context, prompt),
        "claims": _table_for_fragment("stormglass_claim_master", context, prompt),
    }


def _missing_legal_tables(tables: dict[str, str | None], required: tuple[str, ...]) -> str | None:
    missing = [name for name in required if not tables.get(name)]
    if missing:
        return (
            "I could not resolve all Legal_Department tables needed for this deterministic review. "
            f"Missing: {', '.join(missing)}."
        )
    return None


def _handle_legal_department_specific_review(prompt: str, context: dict[str, Any] | None) -> str | None:
    if not _looks_like_legal_department_enterprise_review(prompt, context):
        return None

    text = _normalize_lookup_text(prompt)
    tables = _legal_department_tables(prompt, context)

    if "marketing" in text or "budget" in text or "variance" in text or "campaign" in text:
        missing = _missing_legal_tables(tables, ("marketing",))
        if missing:
            return missing
        sql = f"""
            SELECT
                campaign,
                vendor,
                ROUND(TRY_CAST(budget AS DOUBLE), 2) AS budget,
                ROUND(TRY_CAST(actual AS DOUBLE), 2) AS actual,
                ROUND(TRY_CAST(variance AS DOUBLE), 2) AS variance,
                CASE WHEN TRY_CAST(variance AS DOUBLE) > 0 THEN 'Marketing; Finance; Procurement' ELSE 'Marketing' END AS affected_department,
                'Review budget approval, change authorization, vendor scope, and invoice support for campaign overrun.' AS recommended_action
            FROM "{tables['marketing']}"
            WHERE campaign <> 'campaign'
            ORDER BY TRY_CAST(variance AS DOUBLE) DESC, vendor, campaign
            LIMIT 20
        """
        try:
            rows = _athena_rows(sql)
        except Exception as e:
            return f"(query error: {e})"
        return "\n".join([
            "Legal_Department marketing budget variance review",
            "",
            "This deterministic review uses the selected Legal_Department marketing budget table and ranks campaign/vendor rows by positive budget variance.",
            "",
            _format_rows_as_markdown(rows, "No marketing budget variance rows found."),
            "",
            "Suggested follow-up prompts",
            "- For this Legal_Department group, compare AP invoices for the same marketing vendors. Include invoice_id, vendor, amount, invoice_date, related_group, and issue_hint.",
            "- For this Legal_Department group, review cross-department relationship signals for the top marketing vendors. Include source object, relationship, target, and follow-up question.",
        ])

    if "invoice" in text or "ap " in f"{text} ":
        missing = _missing_legal_tables(tables, ("invoices",))
        if missing:
            return missing
        sql = f"""
            SELECT
                invoice_id,
                vendor,
                ROUND(TRY_CAST(amount AS DOUBLE), 2) AS amount,
                invoice_date,
                related_group,
                COALESCE(issue_hint, '') AS issue_hint,
                CASE
                    WHEN LOWER(COALESCE(issue_hint, '')) = 'review'
                    THEN 'Review invoice approval trail, vendor master record, and receiving documentation before payment escalation.'
                    ELSE 'Confirm ordinary invoice support and approval trail.'
                END AS recommended_action
            FROM "{tables['invoices']}"
            WHERE invoice_id <> 'invoice_id'
            ORDER BY
                CASE WHEN LOWER(COALESCE(issue_hint, '')) = 'review' THEN 0 ELSE 1 END,
                TRY_CAST(amount AS DOUBLE) DESC,
                vendor
            LIMIT 20
        """
        try:
            rows = _athena_rows(sql)
        except Exception as e:
            return f"(query error: {e})"
        return "\n".join([
            "Legal_Department AP invoice review",
            "",
            _format_rows_as_markdown(rows, "No AP invoice rows found."),
        ])

    if "contract" in text or "expired" in text:
        missing = _missing_legal_tables(tables, ("contracts",))
        if missing:
            return missing
        sql = f"""
            SELECT
                col0 AS contract_id,
                col1 AS vendor,
                col2 AS start_date,
                col3 AS end_date,
                col4 AS status,
                col5 AS hourly_rate,
                'Confirm active authorization, renewal status, billing rate approval, and whether related invoices are properly supported.' AS recommended_action
            FROM "{tables['contracts']}"
            WHERE col0 <> 'contract_id'
            ORDER BY CASE WHEN LOWER(COALESCE(col4, '')) = 'expired' THEN 0 ELSE 1 END, col3, col1
            LIMIT 20
        """
        try:
            rows = _athena_rows(sql)
        except Exception as e:
            return f"(query error: {e})"
        return "\n".join([
            "Legal_Department contract review",
            "",
            _format_rows_as_markdown(rows, "No contract rows found."),
        ])

    if "relationship" in text or "cross department" in text or "cross-department" in text:
        missing = _missing_legal_tables(tables, ("relationships",))
        if missing:
            return missing
        sql = f"""
            SELECT
                col0 AS source_group,
                col1 AS source_object,
                col2 AS relationship,
                col3 AS target,
                CASE
                    WHEN LOWER(col2) = 'duplicate_identity' THEN 'Finance; Procurement'
                    WHEN LOWER(col2) = 'reports_concern_about' THEN 'HR; Legal; Management'
                    ELSE col0
                END AS affected_departments,
                'Trace the relationship across source records and assign an owner to validate the connection.' AS follow_up_question
            FROM "{tables['relationships']}"
            WHERE col0 <> 'source_group'
            ORDER BY source_group, relationship, target
            LIMIT 20
        """
        try:
            rows = _athena_rows(sql)
        except Exception as e:
            return f"(query error: {e})"
        return "\n".join([
            "Legal_Department relationship signal review",
            "",
            _format_rows_as_markdown(rows, "No relationship rows found."),
        ])

    return None


def _handle_legal_department_enterprise_review(prompt: str, context: dict[str, Any] | None) -> str | None:
    if not _looks_like_legal_department_enterprise_review(prompt, context):
        return None

    specific = _handle_legal_department_specific_review(prompt, context)
    if specific:
        return specific

    tables = _legal_department_tables(prompt, context)
    missing = _missing_legal_tables(tables, ("contracts", "engineering", "relationships", "invoices", "marketing", "claims"))
    if missing:
        return missing

    review_sql = f"""
        WITH findings AS (
            SELECT
                94 AS enterprise_rank_score,
                'Financial / vendor oversight' AS risk_area,
                related_group AS affected_departments,
                vendor AS primary_entity,
                CONCAT('AP invoice ', invoice_id, ' is marked for review at $', CAST(amount AS VARCHAR)) AS supporting_evidence,
                'Review invoice approval trail, vendor master record, and receiving documentation before payment escalation.' AS recommended_action
            FROM "{tables['invoices']}"
            WHERE LOWER(COALESCE(issue_hint, '')) = 'review'

            UNION ALL
            SELECT
                91 AS enterprise_rank_score,
                'Claims operations / vendor concentration' AS risk_area,
                'Claims; Legal; Finance/AP' AS affected_departments,
                CONCAT(vendor, ' / ', adjuster) AS primary_entity,
                CAST(COUNT(*) AS VARCHAR) || ' flagged claim records; total reserve $' || CAST(ROUND(SUM(TRY_CAST(reserve AS DOUBLE)), 2) AS VARCHAR) AS supporting_evidence,
                'Review claim assignment pattern, vendor selection basis, reserve movement, and supporting claim documentation.' AS recommended_action
            FROM "{tables['claims']}"
            WHERE LOWER(COALESCE(risk_flag, '')) IN ('y', 'yes', 'true')
            GROUP BY vendor, adjuster

            UNION ALL
            SELECT
                86 AS enterprise_rank_score,
                'Contract governance' AS risk_area,
                'Legal; Procurement; Finance/AP' AS affected_departments,
                col1 AS primary_entity,
                CONCAT('Contract ', col0, ' has status ', col4, ', end date ', col3, ', hourly rate ', col5) AS supporting_evidence,
                'Confirm active authorization, renewal status, billing rate approval, and whether related invoices are properly supported.' AS recommended_action
            FROM "{tables['contracts']}"
            WHERE col0 <> 'contract_id' AND LOWER(COALESCE(col4, '')) = 'expired'

            UNION ALL
            SELECT
                83 AS enterprise_rank_score,
                'Vendor insurance / operational readiness' AS risk_area,
                'Engineering; Procurement; Legal' AS affected_departments,
                col0 AS primary_entity,
                CONCAT('Vendor status ', col1, '; insurance expiration ', col2) AS supporting_evidence,
                'Validate insurance certificate status, exception approval, and whether vendor work should continue under current controls.' AS recommended_action
            FROM "{tables['engineering']}"
            WHERE col0 <> 'vendor'
              AND (
                LOWER(COALESCE(col1, '')) = 'exception'
                OR TRY_CAST(col2 AS DATE) < CURRENT_DATE
              )

            UNION ALL
            SELECT
                80 AS enterprise_rank_score,
                'Marketing spend control' AS risk_area,
                'Marketing; Finance; Procurement' AS affected_departments,
                vendor AS primary_entity,
                CONCAT('Campaign ', campaign, ' variance $', CAST(variance AS VARCHAR), ' on budget $', CAST(budget AS VARCHAR), ' and actual $', CAST(actual AS VARCHAR)) AS supporting_evidence,
                'Review budget approval, change authorization, vendor scope, and invoice support for campaign overrun.' AS recommended_action
            FROM "{tables['marketing']}"
            WHERE TRY_CAST(variance AS DOUBLE) > 0

            UNION ALL
            SELECT
                CASE
                    WHEN LOWER(col2) = 'duplicate_identity' THEN 89
                    WHEN LOWER(col2) = 'reports_concern_about' THEN 87
                    WHEN LOWER(col2) IN ('involves_vendor', 'pays_vendor') THEN 84
                    ELSE 76
                END AS enterprise_rank_score,
                'Cross-department relationship signal' AS risk_area,
                col0 AS affected_departments,
                col3 AS primary_entity,
                CONCAT(col1, ' ', col2, ' ', col3) AS supporting_evidence,
                'Trace the relationship across source records and assign an owner to validate whether the connection needs management review.' AS recommended_action
            FROM "{tables['relationships']}"
            WHERE col0 <> 'source_group'
        )
        SELECT
            ROW_NUMBER() OVER (ORDER BY enterprise_rank_score DESC, primary_entity, supporting_evidence) AS item_rank,
            risk_area,
            affected_departments,
            primary_entity,
            enterprise_rank_score,
            supporting_evidence,
            recommended_action
        FROM findings
        ORDER BY enterprise_rank_score DESC, primary_entity, supporting_evidence
        LIMIT 10
    """
    try:
        rows = _athena_rows(review_sql)
    except Exception as e:
        return f"(query error: {e})"

    return "\n".join([
        "Legal_Department enterprise review",
        "",
        "This deterministic review uses only the selected Legal_Department structured tables. It ranks review items by enterprise impact signals across invoices, contracts, claims, vendor readiness, budget variance, and cross-department relationships. These are review priorities, not conclusions.",
        "",
        "Top enterprise review items",
        "",
        _format_rows_as_markdown(rows, "No Legal_Department enterprise review items found."),
        "",
        "How to read this",
        "- `enterprise_rank_score` is a deterministic priority score from the source-table signal type.",
        "- `supporting_evidence` names the table-derived fact that put the item into the review set.",
        "- `recommended_action` is a practical next step for management review and evidence validation.",
        "",
        "Suggested follow-up prompts",
        "- For this Legal_Department group, review AP invoice items marked for review. Include invoice_id, vendor, amount, invoice_date, related_group, and recommended next action.",
        "- For this Legal_Department group, summarize flagged claims by vendor and adjuster. Include claim count, reserve total, loss types, and affected departments.",
        "- For this Legal_Department group, review expired contracts and related vendor activity. Include contract_id, vendor, end_date, status, hourly_rate, and likely business impact.",
        "- For this Legal_Department group, review cross-department relationship signals. Include source group, source object, relationship, target, affected departments, and follow-up question.",
        "- For this Legal_Department group, compare marketing budget variances by campaign and vendor. Include budget, actual, variance, affected department, and recommended next action.",
    ])


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
            or "claim packet" in text
        )
        and any(term in text for term in ("pattern", "investigative", "evidence", "combine", "summarize", "look for", "review", "packet"))
    )


def _looks_like_nightingale_claim_packet_request(prompt: str, context: dict[str, Any] | None) -> bool:
    text = _normalize_lookup_text(prompt)
    context_text = _normalize_lookup_text(
        " ".join(str((context or {}).get(key) or "") for key in ("projectId", "projectName", "groupName"))
    )
    scope = f"{text} {context_text}"
    return (
        "nightingale" in scope
        and "claim packet" in text
        and ("provider" in text or "attorney" in text)
    )


def _nightingale_tables_for_context(context: dict[str, Any] | None, prompt: str) -> dict[str, str | None]:
    return {
        "bills": _table_for_fragments(("medical_bills", "provider_bills"), context, prompt),
        "treatments": _table_for_fragment("treatment_sessions", context, prompt),
        "duplicates": _table_for_fragment("duplicate_bill_candidates", context, prompt),
        "siu": _table_for_fragment("siu_referrals", context, prompt),
        "claims": _table_for_fragment("claims_master", context, prompt),
        "accidents": _table_for_fragment("accident_reports", context, prompt),
    }


def _nightingale_billing_sql_parts(tables: dict[str, str | None]) -> tuple[str, str, str]:
    bill_columns = _glue_columns_for_table(tables.get("bills"))
    bills_provider_expr = "provider_id" if "provider_id" in bill_columns else "provider"
    bills_cpt_expr = "cpt_code" if "cpt_code" in bill_columns else "procedure_code"
    bills_desc_expr = "description" if "description" in bill_columns else bills_cpt_expr
    bills_units_expr = "units" if "units" in bill_columns else "1"
    bills_amount_expr = "TRY_CAST(billed_amount AS DOUBLE)"
    bills_cte = f"""
        bills AS (
            SELECT
                claim_id,
                CAST({bills_provider_expr} AS VARCHAR) AS provider_id,
                CAST({bills_cpt_expr} AS VARCHAR) AS cpt_code,
                CAST({bills_desc_expr} AS VARCHAR) AS description,
                TRY_CAST({bills_units_expr} AS DOUBLE) AS units,
                {bills_amount_expr} AS billed_amount,
                CAST(service_date AS VARCHAR) AS service_date
            FROM "{tables['bills']}"
            WHERE claim_id <> 'claim_id'
        )
    """
    claims_cte = f"""
        claims AS (
            SELECT
                col0 AS claim_id,
                col3 AS loss_date,
                col5 AS attorney_id,
                col6 AS claim_provider,
                col7 AS severity_hint,
                col8 AS claim_status
            FROM "{tables['claims']}"
            WHERE col0 <> 'claim_id'
        )
    """
    duplicates_cte = ""
    if tables.get("duplicates") and "provider_id" in _glue_columns_for_table(tables.get("duplicates")):
        duplicates_cte = f""",
        duplicates AS (
            SELECT bill_id, claim_id, provider_id, duplicate_reason, confidence
            FROM "{tables['duplicates']}"
            WHERE bill_id <> 'bill_id'
        )
        """
    return bills_cte, claims_cte, duplicates_cte


def _handle_nightingale_claim_packet_request(prompt: str, context: dict[str, Any] | None) -> str | None:
    if not _looks_like_nightingale_claim_packet_request(prompt, context):
        return None

    tables = _nightingale_tables_for_context(context, prompt)
    missing = [name for name in ("bills", "claims") if not tables.get(name)]
    if missing:
        return (
            "I could not resolve all Nightingale tables needed for claim packet review. "
            f"Missing: {', '.join(missing)}."
        )

    bills_cte, claims_cte, duplicates_cte = _nightingale_billing_sql_parts(tables)
    duplicate_join = ""
    duplicate_select = "0 AS duplicate_candidates, '' AS duplicate_reasons"
    duplicate_reason_case = "''"
    duplicate_order_expr = "duplicate_candidates"
    if duplicates_cte:
        duplicate_join = """
        LEFT JOIN (
            SELECT
                claim_id,
                provider_id,
                COUNT(*) AS duplicate_candidates,
                array_join(array_sort(array_distinct(array_agg(duplicate_reason))), ', ') AS duplicate_reasons
            FROM duplicates
            GROUP BY claim_id, provider_id
        ) d ON d.claim_id = cb.claim_id AND d.provider_id = cb.provider_id
        """
        duplicate_select = "COALESCE(d.duplicate_candidates, 0) AS duplicate_candidates, COALESCE(d.duplicate_reasons, '') AS duplicate_reasons"
        duplicate_reason_case = "CASE WHEN COALESCE(d.duplicate_candidates, 0) > 0 THEN 'duplicate bill candidate; ' ELSE '' END"
        duplicate_order_expr = "duplicate_candidates"

    claim_packet_sql = f"""
        WITH
        {claims_cte},
        {bills_cte}
        {duplicates_cte},
        provider_top AS (
            SELECT
                provider_id,
                ROUND(SUM(COALESCE(billed_amount, 0)), 2) AS provider_billed_amount
            FROM bills
            GROUP BY provider_id
            ORDER BY provider_billed_amount DESC
            LIMIT 8
        ),
        attorney_top AS (
            SELECT
                c.attorney_id,
                ROUND(SUM(COALESCE(b.billed_amount, 0)), 2) AS attorney_billed_amount
            FROM claims c
            JOIN bills b ON b.claim_id = c.claim_id
            GROUP BY c.attorney_id
            ORDER BY attorney_billed_amount DESC
            LIMIT 8
        ),
        claim_bills AS (
            SELECT
                claim_id,
                provider_id,
                ROUND(SUM(COALESCE(billed_amount, 0)), 2) AS billed_amount,
                COUNT(*) AS bill_lines,
                SUM(CASE WHEN LOWER(description) LIKE '%mri%' OR LOWER(cpt_code) LIKE '%mri%' THEN 1 ELSE 0 END) AS mri_lines,
                SUM(CASE WHEN COALESCE(units, 0) >= 4 THEN 1 ELSE 0 END) AS high_unit_lines
            FROM bills
            GROUP BY claim_id, provider_id
        )
        SELECT
            c.claim_id,
            cb.provider_id,
            c.attorney_id,
            c.loss_date,
            c.severity_hint,
            c.claim_status,
            cb.billed_amount,
            cb.bill_lines,
            cb.mri_lines,
            cb.high_unit_lines,
            {duplicate_select},
            CONCAT(
                CASE WHEN pt.provider_id IS NOT NULL THEN 'top provider; ' ELSE '' END,
                CASE WHEN at.attorney_id IS NOT NULL THEN 'top attorney; ' ELSE '' END,
                CASE WHEN cb.mri_lines > 0 THEN 'MRI billing; ' ELSE '' END,
                CASE WHEN cb.high_unit_lines > 0 THEN 'high-unit billing; ' ELSE '' END,
                {duplicate_reason_case},
                CASE WHEN LOWER(c.severity_hint) IN ('low', 'minor') THEN 'low severity; ' ELSE '' END
            ) AS review_reason
        FROM claim_bills cb
        JOIN claims c ON c.claim_id = cb.claim_id
        LEFT JOIN provider_top pt ON pt.provider_id = cb.provider_id
        LEFT JOIN attorney_top at ON at.attorney_id = c.attorney_id
        {duplicate_join}
        WHERE pt.provider_id IS NOT NULL OR at.attorney_id IS NOT NULL
        ORDER BY
            {duplicate_order_expr} DESC,
            cb.mri_lines DESC,
            cb.billed_amount DESC,
            c.claim_id
        LIMIT 15
    """
    try:
        rows = _athena_rows(claim_packet_sql)
    except Exception as e:
        return f"(query error: {e})"

    group_name = (context or {}).get("groupName") or "selected Nightingale group"
    return "\n".join([
        "Nightingale claim packet review",
        "",
        f"Scope: `{group_name}`. This review narrows the prior provider/attorney billing signals into claim-level packets for follow-up.",
        "",
        "Claim packet candidates",
        "",
        _format_rows_as_markdown(rows, "No claim packet candidates found for the top provider/attorney signals."),
        "",
        "How to use this",
        "- Start with claims where `review_reason` includes both `top provider` and `top attorney`.",
        "- Use `mri_lines`, `high_unit_lines`, and `duplicate_candidates` to prioritize packet review order.",
        "- Review the source bill images, payment records, treatment notes, and call/document tables before drawing conclusions.",
    ])


def _handle_nightingale_pattern_request(prompt: str, context: dict[str, Any] | None) -> str | None:
    if not _looks_like_nightingale_pattern_request(prompt, context):
        return None

    tables = {
        "bills": _table_for_fragments(("medical_bills", "provider_bills"), context, prompt),
        "treatments": _table_for_fragment("treatment_sessions", context, prompt),
        "duplicates": _table_for_fragment("duplicate_bill_candidates", context, prompt),
        "siu": _table_for_fragment("siu_referrals", context, prompt),
        "claims": _table_for_fragment("claims_master", context, prompt),
        "accidents": _table_for_fragment("accident_reports", context, prompt),
    }
    missing = [name for name in ("bills", "claims") if not tables.get(name)]
    if missing:
        return (
            "I could not resolve all Nightingale tables needed for the deterministic pattern report. "
            f"Missing: {', '.join(missing)}."
        )

    bill_columns = _glue_columns_for_table(tables["bills"])
    bills_provider_expr = "provider_id" if "provider_id" in bill_columns else "provider"
    bills_cpt_expr = "cpt_code" if "cpt_code" in bill_columns else "procedure_code"
    bills_desc_expr = "description" if "description" in bill_columns else bills_cpt_expr
    bills_units_expr = "units" if "units" in bill_columns else "1"
    bills_amount_expr = "TRY_CAST(billed_amount AS DOUBLE)"
    bills_cte = f"""
        bills AS (
            SELECT
                claim_id,
                CAST({bills_provider_expr} AS VARCHAR) AS provider_id,
                CAST({bills_cpt_expr} AS VARCHAR) AS cpt_code,
                CAST({bills_desc_expr} AS VARCHAR) AS description,
                TRY_CAST({bills_units_expr} AS DOUBLE) AS units,
                {bills_amount_expr} AS billed_amount,
                CAST(service_date AS VARCHAR) AS service_date
            FROM "{tables['bills']}"
            WHERE claim_id <> 'claim_id'
        )
    """
    claims_cte = f"""
        claims AS (
            SELECT
                col0 AS claim_id,
                col3 AS loss_date,
                col5 AS attorney_id,
                col6 AS claim_provider,
                col7 AS severity_hint,
                col8 AS claim_status
            FROM "{tables['claims']}"
            WHERE col0 <> 'claim_id'
        )
    """
    duplicate_join = ""
    duplicate_select = "0 AS duplicate_candidates"
    if tables.get("duplicates") and "provider_id" in _glue_columns_for_table(tables["duplicates"]):
        duplicate_join = """
        LEFT JOIN (
            SELECT provider_id, COUNT(*) AS duplicate_candidates
            FROM duplicates
            GROUP BY provider_id
        ) d ON d.provider_id = b.provider_id
        """
        duplicate_select = "COALESCE(MAX(d.duplicate_candidates), 0) AS duplicate_candidates"

    provider_sql = f"""
        WITH
        {bills_cte}
        {", duplicates AS (SELECT * FROM \"" + tables["duplicates"] + "\" WHERE bill_id <> 'bill_id')" if tables.get("duplicates") else ""}
        SELECT
            b.provider_id,
            COUNT(*) AS bill_lines,
            COUNT(DISTINCT b.claim_id) AS claims,
            ROUND(SUM(COALESCE(b.billed_amount, 0)), 2) AS billed_amount,
            SUM(CASE WHEN LOWER(b.description) LIKE '%mri%' OR LOWER(b.cpt_code) LIKE '%mri%' THEN 1 ELSE 0 END) AS mri_lines,
            SUM(CASE WHEN COALESCE(b.units, 0) >= 4 THEN 1 ELSE 0 END) AS high_unit_lines,
            {duplicate_select}
        FROM bills b
        {duplicate_join}
        GROUP BY b.provider_id
        ORDER BY duplicate_candidates DESC, billed_amount DESC
        LIMIT 8
    """
    attorney_sql = f"""
        WITH
        {claims_cte},
        {bills_cte}
        SELECT
            c.attorney_id,
            COUNT(DISTINCT c.claim_id) AS claims,
            ROUND(SUM(COALESCE(b.billed_amount, 0)), 2) AS billed_amount,
            SUM(CASE WHEN LOWER(b.description) LIKE '%mri%' OR LOWER(b.cpt_code) LIKE '%mri%' THEN 1 ELSE 0 END) AS mri_lines
        FROM claims c
        JOIN bills b ON b.claim_id = c.claim_id
        GROUP BY c.attorney_id
        ORDER BY billed_amount DESC
        LIMIT 8
    """
    delayed_sql = f"""
        WITH
        {claims_cte},
        {bills_cte},
        first_treatment AS (
            SELECT claim_id, MIN(CAST(treatment_date AS DATE)) AS first_treatment_date
            FROM "{tables['treatments']}"
            GROUP BY claim_id
        ),
        claim_bills AS (
            SELECT
                claim_id,
                ROUND(SUM(CAST(billed_amount AS DOUBLE)), 2) AS billed_amount,
                SUM(CASE WHEN LOWER(description) LIKE '%mri%' OR LOWER(cpt_code) LIKE '%mri%' THEN 1 ELSE 0 END) AS mri_lines
            FROM bills
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
        FROM claims c
        JOIN first_treatment ft ON ft.claim_id = c.claim_id
        JOIN claim_bills cb ON cb.claim_id = c.claim_id
        WHERE date_diff('day', CAST(c.loss_date AS DATE), ft.first_treatment_date) BETWEEN 10 AND 15
        ORDER BY cb.billed_amount DESC
        LIMIT 8
    """ if tables.get("treatments") else None
    if tables.get("accidents"):
        low_severity_sql = f"""
        WITH
        {bills_cte},
        claim_bills AS (
            SELECT claim_id, ROUND(SUM(COALESCE(billed_amount, 0)), 2) AS billed_amount
            FROM bills
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
    else:
        low_severity_sql = f"""
        WITH
        {claims_cte},
        {bills_cte},
        claim_bills AS (
            SELECT claim_id, ROUND(SUM(COALESCE(billed_amount, 0)), 2) AS billed_amount
            FROM bills
            GROUP BY claim_id
        )
        SELECT
            c.claim_id,
            c.severity_hint AS damage_severity,
            cb.billed_amount
        FROM claims c
        JOIN claim_bills cb ON cb.claim_id = c.claim_id
        WHERE LOWER(c.severity_hint) IN ('low', 'minor')
        ORDER BY cb.billed_amount DESC
        LIMIT 8
        """
    sunday_sql = f"""
        WITH
        {bills_cte}
        SELECT
            b.provider_id,
            COUNT(*) AS sunday_bill_lines,
            COUNT(DISTINCT b.claim_id) AS claims,
            ROUND(SUM(COALESCE(b.billed_amount, 0)), 2) AS billed_amount
        FROM bills b
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
    """ if tables.get("siu") else None

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
        if not sql:
            sections.append((title, "", []))
            continue
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
        "Suggested follow-up prompts",
        "- Pull the top provider IDs and attorney IDs above into a claim packet review for this Nightingale group. Include claim_id, provider_id or provider name, attorney_id or attorney name, billed amount, MRI indicators, duplicate candidate count, and why each claim should be reviewed.",
        "- Review duplicate bill candidates in this Nightingale group against payment records. Include bill_id, claim_id, provider_id, duplicate_reason, confidence, paid amount if available, and a short recommendation.",
        "- Validate Sunday service dates in this Nightingale group against provider operating records and treatment/session logs. Include provider_id, claim_id, service_date, billed amount, and whether the provider appears open.",
        "- Compare delayed-treatment claims in this Nightingale group against call-center intake logs and witness or document narrative tables. Include days from loss to first treatment and the evidence that supports review.",
        "- Prioritize low-severity and high-billing Nightingale claims for SIU review before payment escalation. Include claim_id, severity, provider, attorney, billed amount, duplicate indicator, and SIU reason if available.",
    ])
    return "\n".join(lines)


def _looks_like_nightingale_benchmark_request(prompt: str, context: dict[str, Any] | None) -> bool:
    text = _normalize_lookup_text(prompt)
    context_text = _normalize_lookup_text(
        " ".join(str((context or {}).get(key) or "") for key in ("projectId", "projectName", "groupName"))
    )
    available_tables = _normalize_lookup_text(
        " ".join(str(table) for table in ((context or {}).get("tableHints") or []))
    )
    scope = f"{text} {context_text} {available_tables}"
    return (
        "nightingale" in scope
        and ("provider" in text or "claim" in text or "billing" in text or "billed" in text)
        and (
            "benchmark" in text
            or "medical benchmark rates" in text
            or "benchmark rates" in available_tables
        )
        and (
            "duplicate" in text
            or "high" in text
            or "suspicious" in text
            or "unusually" in text
            or "units" in text
            or "billed amounts" in text
        )
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
        "This deterministic billing-quality review joins provider bills to medical benchmark rates by `cpt_code`, then overlays duplicate bill candidates and provider review hints. A row is included when billed amount exceeds the benchmark, units exceed the utilization norm, a duplicate candidate exists, or the provider is marked with a review hint in the project data. These are audit candidates, not conclusions of wrongdoing.",
        "",
        "Top bill-line review candidates",
        "",
        _format_rows_as_markdown(line_rows, "No matching bill lines found."),
        "",
        "Top providers by benchmark/duplicate review signals",
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
    request_prompt = _user_request_from_scoped_prompt(prompt)
    stripped_prompt = request_prompt.strip().rstrip(";").lstrip("(").strip()
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
            rows = _athena_rows(request_prompt)
            return {"result": json.dumps(rows[:ATHENA_MAX_ROWS], indent=2)}
        except Exception as e:
            return {"result": f"(query error: {e})"}
    actor_id   = (payload.get("actor_id")   or "anonymous")[:128]
    persona    = (payload.get("persona")    or "employee")[:16]
    session_id = (payload.get("session_id") or "adhoc")[:128]
    chat_type  = (payload.get("chat_type")  or "analyst")[:16]
    user_email = (payload.get("user_email") or "")[:200]
    explicit_context = (
        _context_from_ui_selector_prompt(prompt)
        or _resolve_single_group_context(prompt)
        or _resolve_group_context_from_glue(prompt)
    )
    if explicit_context and session_id != "adhoc":
        SESSION_GROUP_CONTEXTS[session_id] = explicit_context
    resolved_context = _context_with_glue_table_hints(explicit_context or SESSION_GROUP_CONTEXTS.get(session_id))
    if _looks_like_group_inventory_request(request_prompt):
        if resolved_context:
            return {"result": _format_group_inventory(resolved_context)}
        return {
            "result": (
                "I need a selected Data Group before listing files and tables for `this group`.\n\n"
                f"{_list_projects_payload()}"
            )
        }
    row_count_result = _handle_row_count_request(request_prompt, resolved_context)
    if row_count_result:
        return {"result": row_count_result}
    daily_sales_multi_zone_result = _handle_daily_sales_multi_zone_request(request_prompt, resolved_context)
    if daily_sales_multi_zone_result:
        return {"result": daily_sales_multi_zone_result}
    storm_glass_claim_review_result = _handle_storm_glass_claim_review_request(request_prompt, resolved_context)
    if storm_glass_claim_review_result:
        return {"result": storm_glass_claim_review_result}
    legal_department_review_result = _handle_legal_department_enterprise_review(request_prompt, resolved_context)
    if legal_department_review_result:
        return {"result": legal_department_review_result}
    nightingale_claim_packet_result = _handle_nightingale_claim_packet_request(request_prompt, resolved_context)
    if nightingale_claim_packet_result:
        return {"result": nightingale_claim_packet_result}
    nightingale_pattern_result = _handle_nightingale_pattern_request(request_prompt, resolved_context)
    if nightingale_pattern_result:
        return {"result": nightingale_pattern_result}
    nightingale_benchmark_result = _handle_nightingale_benchmark_request(request_prompt, resolved_context)
    if nightingale_benchmark_result:
        return {"result": nightingale_benchmark_result}
    helios_project_risk_result = _handle_helios_project_risk_request(request_prompt, resolved_context)
    if helios_project_risk_result:
        return {"result": helios_project_risk_result}
    claims_year_result = _handle_claims_loss_year_request(request_prompt, resolved_context)
    if claims_year_result:
        return {"result": claims_year_result}
    first_records_result = _handle_first_records_request(request_prompt, resolved_context)
    if first_records_result:
        return {"result": first_records_result}
    agent_prompt = _prepend_resolved_group_context(request_prompt, resolved_context) if resolved_context else request_prompt
    if resolved_context:
        log.info(
            "Structured specialist resolved group: project=%s group=%s",
            resolved_context.get("projectId"),
            resolved_context.get("groupName"),
        )
    log.info("Structured specialist: persona=%s session=%s prompt=%s", persona, session_id, request_prompt[:200])
    agent = build_agent()
    try:
        agent_result = agent(agent_prompt)
    except MaxTokensReachedException:
        log.exception("Structured specialist hit max token loop for prompt=%s", request_prompt[:500])
        if resolved_context:
            group_name = resolved_context.get("groupName") or "the selected group"
            return {
                "result": (
                    "The selected Data Group is already the analysis boundary, but the "
                    "request still expanded too far for one pass. Start with this group "
                    "inventory, then ask about one file or table from the list.\n\n"
                    f"{_format_group_inventory(resolved_context)}\n\n"
                    f"Selected group: `{group_name}`."
                )
            }
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
