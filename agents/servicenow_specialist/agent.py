"""ARBITER ServiceNow Specialist — runs on Bedrock AgentCore Runtime.

Full ITSM/ITAM reach over the ServiceNow REST Table + Change APIs:
  - CMDB:    read/write Configuration Items and relationships (cmdb_ci, cmdb_rel_ci).
  - Incident/Problem/Change: read, create, update, and comment (work_notes/comments).
  - Asset Management (ITAM): read/write assets (alm_asset, alm_hardware) and the
    asset↔CI link.
  - Change-impact analysis: resolve a changed AWS resource to its CI, walk
    cmdb_rel_ci for the blast radius, find the owning team, and (on request) draft
    a change_request with the affected CIs attached.
  - AI-Scan drift: dump a CMDB + asset snapshot the master orchestrator correlates
    against live AWS reality to surface CMDB/Asset drift.

All ServiceNow reach is behind a thin ServiceNowClient so an MCP backend can
replace it later without touching the @tool functions — mirroring jira_specialist.

Two invocation paths, like jira_specialist:
  - Chat path (payload.prompt): a Strands Agent runs with the @tool functions
    (CMDB / incident / problem / asset reads + comments + light updates + drift).
  - Deterministic action paths (payload.action), no LLM, used by api_handler and
    the master orchestrator:
      impact_analysis   — full resolve→traverse→owner→(optional draft CHG)
      create_change / update_change / comment_change
      create_incident / update_incident / comment_incident
      create_problem  / update_problem  / comment_problem
      create_ci       / update_ci
      create_asset    / update_asset
      add_affected_cis  — POST task_ci rows for a change
      cmdb_snapshot     — CIs + assets dump for drift correlation (master)

Environment variables:
  MODEL_ID             Bedrock model (default: Nova 2 Lite cross-region profile)
  GUARDRAIL_ID         Optional guardrail
  GUARDRAIL_VERSION    Guardrail version (default: DRAFT)
  KB_ID                Bedrock Knowledge Base ID (optional; not required here)
  SERVICENOW_API_BASE  Instance base URL, e.g. https://dev12345.service-now.com
  SERVICENOW_SECRET_ID Secrets Manager id holding ONE of (auth auto-detected):
                       {instance_url, username, password}              (basic — primary)
                       {instance_url, api_key}                         (Inbound REST API Key)
                       {instance_url, client_id, client_secret}        (OAuth2 client-creds)
                       {instance_url, client_id, client_secret, jwt_assertion} (OAuth2 JWT)
                       Empty/unreadable = agent runs in "(ServiceNow not
                       configured)" mode (mock CHG ids, like jira).
  SERVICENOW_MAX_DEPTH cmdb_rel_ci BFS depth cap (default 3).
  SERVICENOW_SNAPSHOT_LIMIT  Per-table row cap for cmdb_snapshot (default 200).
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any
from urllib.parse import quote

import boto3
import requests
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent
from strands.models.bedrock import BedrockModel
from strands.tools import tool

from _shared.token_usage import record_from_agent_result

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("servicenow_specialist")

REGION = os.environ.get("AWS_REGION", "us-east-1")
MODEL_ID = os.environ.get("MODEL_ID", "us.amazon.nova-2-lite-v1:0")
KB_ID = os.environ.get("KB_ID", "")
GUARDRAIL_ID = os.environ.get("GUARDRAIL_ID")
GUARDRAIL_VERSION = os.environ.get("GUARDRAIL_VERSION", "DRAFT")
SERVICENOW_API_BASE = os.environ.get("SERVICENOW_API_BASE", "").strip().rstrip("/")
SERVICENOW_SECRET_ID = os.environ.get("SERVICENOW_SECRET_ID", "").strip()
# Bounded blast-radius traversal — cap depth so a richly-related CMDB can't
# explode the query count. Per-node fan-out is capped in the client.
MAX_DEPTH = max(1, int(os.environ.get("SERVICENOW_MAX_DEPTH", "3") or "3"))
# Per-table row cap for the drift snapshot — keep the CMDB/asset dump bounded.
SNAPSHOT_LIMIT = max(1, int(os.environ.get("SERVICENOW_SNAPSHOT_LIMIT", "200") or "200"))
HTTP_TIMEOUT = 20

SYSTEM_PROMPT = """You are the ServiceNow specialist for ARBITER — an expert ITSM/ITAM
operator working the live ServiceNow instance over its REST API.

You can READ and WRITE across CMDB, Incident, Problem, Change, and Asset Management:
  - query_ci / get_ci_details: resolve an AWS resource id/ARN or name to a CMDB CI.
  - get_affected_cis: walk cmdb_rel_ci for the blast radius of a change — what
    depends on, and what is depended on by, a CI.
  - get_ci_owner: the support/assignment group that owns a CI (who does the work).
  - query_change / query_incident / query_problem: look records up by number or query.
  - query_asset: look up hardware/software assets by tag, serial, model, or state.
  - update_incident / comment_incident / comment_problem: change state/assignment or
    append a work note (internal) or comment (customer-visible) to a record.
  - detect_drift: report CMDB/asset hygiene gaps (missing correlation_id/owner, unlinked
    assets); point users to the Drift Scan dashboard for full CMDB-vs-AWS drift.

For impact questions ("what is the impact of changing X / who does the work / who
approves"), resolve the CI, list affected CIs, name the owning team, and state whether
the change needs CAB approval (PROD or high-risk changes do).

Etiquette: confirm the target record before writing; prefer work notes over public
comments unless asked; cite CI names, incident/problem/change/asset numbers verbatim;
never fabricate sys_ids, numbers, names, or states — if a tool returns nothing, say so.
Keep answers concise and factual.
"""

app = BedrockAgentCoreApp()
secrets_client = boto3.client("secretsmanager", region_name=REGION)


# ──────────────────────────── ServiceNow REST client ─────────────
class ServiceNowClient:
    """Thin REST client over the ServiceNow Table + Change APIs.

    Auth precedence: OAuth2 client-credentials when {client_id, client_secret}
    are present in the secret, else HTTP basic with {username, password}.
    All reach goes through this class so an MCP backend can swap in later.
    """

    def __init__(self, base_url: str, creds: dict[str, str]):
        self.base = base_url.rstrip("/")
        self._creds = creds
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})
        self._bearer: str | None = None
        # Auth precedence — auto-detected from the secret's keys:
        #   1. api_key                               → Inbound REST API Key header
        #   2. client_id+client_secret(+jwt_assertion) → OAuth2 (JWT-bearer / client-creds)
        #   3. username+password                     → HTTP basic (primary for this deploy)
        if creds.get("api_key"):
            self._session.headers["x-sn-apikey"] = creds["api_key"]
        elif creds.get("client_id") and creds.get("client_secret"):
            self._bearer = self._fetch_oauth_token()
            self._session.headers["Authorization"] = f"Bearer {self._bearer}"
        else:
            self._session.auth = (creds.get("username", ""), creds.get("password", ""))

    def _fetch_oauth_token(self) -> str:
        """Exchange OAuth2 credentials at /oauth_token.do for an access token.

        Uses the JWT-bearer grant when a signed assertion is present in the secret
        (jwt_assertion), else the client-credentials grant.
        """
        if self._creds.get("jwt_assertion"):
            data = {
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "client_id": self._creds["client_id"],
                "client_secret": self._creds["client_secret"],
                "assertion": self._creds["jwt_assertion"],
            }
        else:
            data = {
                "grant_type": "client_credentials",
                "client_id": self._creds["client_id"],
                "client_secret": self._creds["client_secret"],
            }
        resp = requests.post(f"{self.base}/oauth_token.do", data=data, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        return resp.json()["access_token"]

    def get_table(self, table: str, *, query: str = "", fields: str = "",
                  limit: int = 50, display_value: str = "false") -> list[dict[str, Any]]:
        """GET /api/now/table/{table} with sysparm_query/fields/limit."""
        params = {"sysparm_limit": str(limit), "sysparm_display_value": display_value}
        if query:
            params["sysparm_query"] = query
        if fields:
            params["sysparm_fields"] = fields
        resp = self._session.get(
            f"{self.base}/api/now/table/{table}", params=params, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        return resp.json().get("result", [])

    def post_table(self, table: str, body: dict[str, Any], *,
                   input_display_value: bool = True) -> dict[str, Any]:
        """POST /api/now/table/{table}; returns the created record.

        sysparm_input_display_value=true lets us pass reference fields
        (assignment_group) by display name instead of sys_id.
        """
        params = {"sysparm_input_display_value": "true" if input_display_value else "false"}
        resp = self._session.post(
            f"{self.base}/api/now/table/{table}", params=params,
            json=body, timeout=HTTP_TIMEOUT,
            headers={"Content-Type": "application/json"})
        resp.raise_for_status()
        return resp.json().get("result", {})

    def patch_table(self, table: str, sys_id: str, body: dict[str, Any], *,
                    input_display_value: bool = True) -> dict[str, Any]:
        """PATCH /api/now/table/{table}/{sys_id}; returns the updated record.

        Journal fields (work_notes, comments) are append-on-write in ServiceNow,
        so a PATCH carrying them adds a journal entry rather than overwriting.
        """
        params = {"sysparm_input_display_value": "true" if input_display_value else "false"}
        resp = self._session.patch(
            f"{self.base}/api/now/table/{table}/{quote(sys_id)}", params=params,
            json=body, timeout=HTTP_TIMEOUT,
            headers={"Content-Type": "application/json"})
        resp.raise_for_status()
        return resp.json().get("result", {})

    def get_one(self, table: str, sys_id: str, *, fields: str = "",
                display_value: str = "true") -> dict[str, Any]:
        """GET a single record by sys_id."""
        params = {"sysparm_display_value": display_value}
        if fields:
            params["sysparm_fields"] = fields
        resp = self._session.get(
            f"{self.base}/api/now/table/{table}/{quote(sys_id)}",
            params=params, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        return resp.json().get("result", {})

    def record_url(self, table: str, sys_id: str) -> str:
        return f"{self.base}/nav_to.do?uri={table}.do?sys_id={quote(sys_id)}"


# Module-level client cache. Each container handles one invocation at a time, so
# a lazily-built singleton is safe and avoids re-auth per tool call.
_CLIENT: ServiceNowClient | None = None
_CLIENT_TRIED = False


def _load_credentials() -> dict[str, str] | None:
    if not SERVICENOW_SECRET_ID:
        return None
    try:
        resp = secrets_client.get_secret_value(SecretId=SERVICENOW_SECRET_ID)
        return json.loads(resp["SecretString"])
    except Exception as e:
        log.warning("Could not load ServiceNow secret: %s", e)
        return None


def _get_client() -> ServiceNowClient | None:
    """Build (once) and return the REST client, or None if not configured."""
    global _CLIENT, _CLIENT_TRIED
    if _CLIENT_TRIED:
        return _CLIENT
    _CLIENT_TRIED = True
    creds = _load_credentials() or {}
    base = SERVICENOW_API_BASE or (creds.get("instance_url") or "").strip().rstrip("/")
    if not base or not creds:
        log.warning("ServiceNow not configured (SERVICENOW_API_BASE / secret missing) — degraded mode")
        return None
    try:
        _CLIENT = ServiceNowClient(base, creds)
    except Exception as e:
        log.exception("ServiceNow client init failed: %s", e)
        _CLIENT = None
    return _CLIENT


# ──────────────────────────── CMDB helpers ───────────────────────
def _ref(rec: dict[str, Any], base: str) -> tuple[str, str]:
    """Extract (sys_id, display) for a dot-walked reference field.

    Table API returns dot-walked reference fields either as flat keys
    ("parent.sys_id") or as nested {value, link, display_value} objects
    depending on instance config — handle both.
    """
    sid = rec.get(f"{base}.sys_id") or rec.get(f"{base}.value") or ""
    name = rec.get(f"{base}.name") or rec.get(f"{base}.display_value") or ""
    val = rec.get(base)
    if isinstance(val, dict):
        sid = sid or val.get("value") or ""
        name = name or val.get("display_value") or ""
    return sid, name


def _resolve_ci(client: ServiceNowClient, resource: str) -> dict[str, Any] | None:
    """Resolve an AWS resource id/ARN (or name) to a cmdb_ci record.

    Tries correlation_id first (where Service Graph Connector / seed stores the
    ARN), then exact name, then a LIKE-name fallback.
    """
    fields = "sys_id,name,sys_class_name,correlation_id,support_group,managed_by"
    for q in (f"correlation_id={resource}", f"name={resource}", f"nameLIKE{resource}"):
        try:
            rows = client.get_table("cmdb_ci", query=q, fields=fields, limit=1)
        except Exception as e:
            log.warning("cmdb_ci query failed (%s): %s", q, e)
            continue
        if rows:
            return rows[0]
    return None


def _traverse_relationships(client: ServiceNowClient, root_sys_id: str,
                            max_depth: int) -> list[dict[str, Any]]:
    """Bounded BFS over cmdb_rel_ci in both directions from root_sys_id.

    Returns affected CIs as [{sys_id, name, class, depth, via, direction}].
    direction: 'downstream' = CIs that depend on the root (impacted by a change);
               'upstream'   = CIs the root depends on.
    Per-node fan-out and total depth are capped to keep query count bounded.
    """
    fields = ("parent.sys_id,parent.name,parent.sys_class_name,"
              "child.sys_id,child.name,child.sys_class_name,type.name")
    visited: set[str] = {root_sys_id}
    affected: list[dict[str, Any]] = []
    frontier = [root_sys_id]
    for depth in range(1, max_depth + 1):
        next_frontier: list[str] = []
        for sid in frontier:
            # child.X depends on parent → parent is upstream, child is downstream.
            for direction, sel in (("downstream", f"parent.sys_id={sid}"),
                                    ("upstream", f"child.sys_id={sid}")):
                try:
                    rels = client.get_table("cmdb_rel_ci", query=sel, fields=fields, limit=50)
                except Exception as e:
                    log.warning("cmdb_rel_ci query failed (%s): %s", sel, e)
                    continue
                other = "child" if direction == "downstream" else "parent"
                for r in rels:
                    o_sid, o_name = _ref(r, other)
                    o_class = (r.get(f"{other}.sys_class_name") or "")
                    # relationship type display name (flat key or nested object)
                    rel_type = r.get("type.name") or ""
                    if not rel_type and isinstance(r.get("type"), dict):
                        rel_type = r["type"].get("display_value", "")
                    if not o_sid or o_sid in visited:
                        continue
                    visited.add(o_sid)
                    affected.append({"sys_id": o_sid, "name": o_name, "class": o_class,
                                     "depth": depth, "via": rel_type, "direction": direction})
                    next_frontier.append(o_sid)
        frontier = next_frontier
        if not frontier:
            break
    return affected


def _ci_owner(client: ServiceNowClient, ci: dict[str, Any]) -> dict[str, str]:
    """Owning team (support_group) + assignee (managed_by) for a CI."""
    group_sid, group_name = _ref(ci, "support_group")
    user_sid, user_name = _ref(ci, "managed_by")
    return {"group_sys_id": group_sid, "owner_team": group_name,
            "managed_by_sys_id": user_sid, "managed_by": user_name}


# ──────────────────────────── ITSM record helpers ────────────────
def _find_by_number(client: ServiceNowClient, table: str, number: str, *,
                    fields: str = "", display_value: str = "true") -> dict[str, Any] | None:
    """Resolve an incident/problem/change_request by its display number."""
    try:
        rows = client.get_table(table, query=f"number={number}", fields=fields,
                                limit=1, display_value=display_value)
    except Exception as e:
        log.warning("%s lookup failed for %s: %s", table, number, e)
        return None
    return rows[0] if rows else None


def _resolve_ci_sys_id(client: ServiceNowClient, ci_ref: str) -> str:
    """Best-effort resolve a CI reference (name/ARN/sys_id) to a cmdb_ci sys_id.

    A 32-hex string is treated as an already-resolved sys_id; otherwise the same
    correlation_id→name→LIKE resolution used everywhere else is applied.
    """
    ci_ref = (ci_ref or "").strip()
    if not ci_ref:
        return ""
    if len(ci_ref) == 32 and all(c in "0123456789abcdef" for c in ci_ref.lower()):
        return ci_ref
    ci = _resolve_ci(client, ci_ref)
    return ci.get("sys_id", "") if ci else ""


# Default field projections per process table (kept compact for the small model).
_INCIDENT_FIELDS = ("number,short_description,state,priority,assignment_group.name,"
                    "caller_id.name,cmdb_ci.name,sys_id")
_PROBLEM_FIELDS = ("number,short_description,state,priority,assignment_group.name,"
                   "cmdb_ci.name,sys_id")


def _summarize(rec: dict[str, Any], *, kind: str) -> str:
    """One-line human summary of an incident/problem/change record."""
    g = rec.get("assignment_group.name") or rec.get("assignment_group") or "unassigned"
    return (f"{rec.get('number')}: {rec.get('short_description')} "
            f"[{kind} state={rec.get('state')}, group={g}].")


# ──────────────────────────── @tool functions ────────────────────
@tool
def query_ci(resource: str) -> str:
    """Resolve an AWS resource id/ARN (or CI name) to a ServiceNow CMDB CI.

    Args:
        resource: The AWS resource id, ARN, or CI name, e.g.
            "alb-mig-prod-claims-api-001".
    """
    client = _get_client()
    if not client:
        return "(ServiceNow not configured)"
    ci = _resolve_ci(client, resource)
    if not ci:
        return f"No CMDB CI found for '{resource}'."
    owner = _ci_owner(client, ci)
    return (f"CI: {ci.get('name')} (class={ci.get('sys_class_name')}, "
            f"sys_id={ci.get('sys_id')}, correlation_id={ci.get('correlation_id')}). "
            f"Owning team: {owner['owner_team'] or 'unassigned'}.")


@tool
def get_affected_cis(resource: str, max_depth: int = 0) -> str:
    """List the blast radius of a change to a CI (cmdb_rel_ci traversal).

    Args:
        resource: AWS resource id/ARN or CI name to start from.
        max_depth: Relationship hops to traverse (1-5). 0 = server default.
    """
    client = _get_client()
    if not client:
        return "(ServiceNow not configured)"
    ci = _resolve_ci(client, resource)
    if not ci:
        return f"No CMDB CI found for '{resource}'."
    depth = MAX_DEPTH if not max_depth else max(1, min(5, max_depth))
    affected = _traverse_relationships(client, ci["sys_id"], depth)
    if not affected:
        return f"{ci.get('name')} has no related CIs in the CMDB (depth {depth})."
    lines = [f"Blast radius for {ci.get('name')} (depth {depth}): {len(affected)} CI(s)"]
    for a in affected[:50]:
        lines.append(f"- {a['name']} [{a['class']}] {a['direction']} via {a['via']} (hop {a['depth']})")
    return "\n".join(lines)


@tool
def get_ci_owner(resource: str) -> str:
    """Return the owning/support team for a CI (who does the work).

    Args:
        resource: AWS resource id/ARN or CI name.
    """
    client = _get_client()
    if not client:
        return "(ServiceNow not configured)"
    ci = _resolve_ci(client, resource)
    if not ci:
        return f"No CMDB CI found for '{resource}'."
    owner = _ci_owner(client, ci)
    return (f"{ci.get('name')}: owning team = {owner['owner_team'] or 'unassigned'}, "
            f"assignee = {owner['managed_by'] or 'unassigned'}.")


@tool
def query_change(number: str) -> str:
    """Look up an existing ServiceNow change_request by number.

    Args:
        number: The change number, e.g. "CHG0030001".
    """
    client = _get_client()
    if not client:
        return "(ServiceNow not configured)"
    try:
        rows = client.get_table(
            "change_request", query=f"number={number}",
            fields="number,short_description,state,assignment_group.name,cab_required,sys_id",
            limit=1, display_value="true")
    except Exception as e:
        return f"(error querying change {number}: {e})"
    if not rows:
        return f"No change_request found with number '{number}'."
    r = rows[0]
    return (f"{r.get('number')}: {r.get('short_description')} [state={r.get('state')}, "
            f"group={r.get('assignment_group.name')}, cab_required={r.get('cab_required')}].")


@tool
def get_ci_details(resource: str) -> str:
    """Return the full attribute set for a CMDB CI (class, status, owner, correlation_id).

    Args:
        resource: AWS resource id/ARN or CI name.
    """
    client = _get_client()
    if not client:
        return "(ServiceNow not configured)"
    ci = _resolve_ci(client, resource)
    if not ci:
        return f"No CMDB CI found for '{resource}'."
    full = client.get_one(ci.get("sys_class_name") or "cmdb_ci", ci["sys_id"]) or ci
    owner = _ci_owner(client, ci)
    keys = ("name", "sys_class_name", "operational_status", "correlation_id",
            "environment", "location", "short_description")
    parts = [f"{k}={full.get(k)}" for k in keys if full.get(k)]
    parts.append(f"owner={owner['owner_team'] or 'unassigned'}")
    return f"CI {ci.get('name')}: " + ", ".join(parts)


@tool
def query_incident(query: str) -> str:
    """Look up incidents by number or encoded query.

    Args:
        query: An incident number (INC0010001) or a ServiceNow encoded query
            (e.g. 'active=true^priority=1', 'cmdb_ci.name=Claims API').
    """
    client = _get_client()
    if not client:
        return "(ServiceNow not configured)"
    q = query.strip()
    enc = f"number={q}" if q.upper().startswith("INC") else q
    try:
        rows = client.get_table("incident", query=enc, fields=_INCIDENT_FIELDS,
                                limit=10, display_value="true")
    except Exception as e:
        return f"(error querying incidents: {e})"
    if not rows:
        return f"No incidents found for '{query}'."
    return "\n".join(_summarize(r, kind="incident") for r in rows)


@tool
def update_incident(number: str, state: str = "", assignment_group: str = "",
                    priority: str = "", note: str = "") -> str:
    """Update an incident's state / assignment / priority and optionally add a work note.

    Args:
        number: The incident number, e.g. INC0010001.
        state: New state label (New, In Progress, On Hold, Resolved, Closed) — optional.
        assignment_group: Reassign to this group display name — optional.
        priority: New priority 1-5 — optional.
        note: Work note to append — optional.
    """
    fields = {k: v for k, v in (("state", state), ("assignment_group", assignment_group),
                                ("priority", priority)) if v}
    return _fmt_write(_do_update("incident", number, fields, work_note=note or None),
                      f"incident {number} updated")


@tool
def comment_incident(number: str, note: str, work_note: bool = True) -> str:
    """Add a comment or work note to an incident.

    Args:
        number: The incident number, e.g. INC0010001.
        note: The text to add.
        work_note: True = internal work note (default); False = customer-visible comment.
    """
    return _fmt_write(_do_comment("incident", number, note, work_note),
                      f"{'work note' if work_note else 'comment'} added to {number}")


@tool
def query_problem(query: str) -> str:
    """Look up problems by number or encoded query.

    Args:
        query: A problem number (PRB0010001) or a ServiceNow encoded query.
    """
    client = _get_client()
    if not client:
        return "(ServiceNow not configured)"
    q = query.strip()
    enc = f"number={q}" if q.upper().startswith("PRB") else q
    try:
        rows = client.get_table("problem", query=enc, fields=_PROBLEM_FIELDS,
                                limit=10, display_value="true")
    except Exception as e:
        return f"(error querying problems: {e})"
    if not rows:
        return f"No problems found for '{query}'."
    return "\n".join(_summarize(r, kind="problem") for r in rows)


@tool
def comment_problem(number: str, note: str, work_note: bool = True) -> str:
    """Add a comment or work note to a problem.

    Args:
        number: The problem number, e.g. PRB0010001.
        note: The text to add.
        work_note: True = internal work note (default); False = customer-visible comment.
    """
    return _fmt_write(_do_comment("problem", number, note, work_note),
                      f"{'work note' if work_note else 'comment'} added to {number}")


@tool
def query_asset(query: str) -> str:
    """Look up hardware/software assets by tag, serial, model, or state.

    Args:
        query: An asset tag (e.g. P1000123) or an encoded query
            (e.g. 'serial_number=ABC', 'install_status=1', 'model_category.name=Server').
    """
    client = _get_client()
    if not client:
        return "(ServiceNow not configured)"
    q = query.strip()
    enc = q if ("=" in q or "^" in q) else f"asset_tag={q}"
    try:
        rows = client.get_table(
            "alm_asset", query=enc,
            fields="asset_tag,display_name,serial_number,install_status,"
                   "assigned_to.name,ci.name,model_category.name,sys_id",
            limit=10, display_value="true")
    except Exception as e:
        return f"(error querying assets: {e})"
    if not rows:
        return f"No assets found for '{query}'."
    return "\n".join(
        f"{r.get('asset_tag') or r.get('display_name')}: {r.get('model_category.name') or ''} "
        f"[status={r.get('install_status')}, assigned={r.get('assigned_to.name') or 'none'}, "
        f"ci={r.get('ci.name') or 'unlinked'}]." for r in rows)


@tool
def detect_drift(scope: str = "all") -> str:
    """Report CMDB/asset hygiene drift visible from ServiceNow alone: CIs missing a
    correlation_id (untraceable to AWS) or an owning group, and assets not linked to
    a CI. For full CMDB-vs-AWS drift, run the Drift Scan dashboard.

    Args:
        scope: 'cmdb', 'asset', or 'all' (default).
    """
    client = _get_client()
    if not client:
        return "(ServiceNow not configured)"
    issues = _hygiene_drift(client, scope)
    if not issues:
        return "No CMDB/asset hygiene drift detected from ServiceNow data."
    return "CMDB/asset hygiene drift:\n" + "\n".join(f"- {i}" for i in issues)


# ──────────────────────────── deterministic action paths ─────────
def _cab_required(target_env: str, severity: str) -> bool:
    """PROD-scoped or high-risk changes need CAB approval."""
    return (target_env or "").upper() == "PROD" or (severity or "").upper() in ("CRITICAL", "HIGH")


def _create_change(payload: dict[str, Any]) -> dict[str, Any]:
    """Create a change_request. No LLM. Returns {number, sys_id, url}."""
    client = _get_client()
    short_desc = (payload.get("short_description") or payload.get("summary")
                  or "ARBITER change request").strip()
    description = (payload.get("description") or "").strip()
    assignment_group = (payload.get("assignment_group") or "").strip()
    cab = bool(payload.get("cab_required"))
    if not client:
        # Mock so the demo flow still renders (mirrors jira's degradation).
        import hashlib
        suffix = int(hashlib.sha256(short_desc.encode()).hexdigest()[:6], 16) % 100000
        return {"configured": False, "number": f"CHG-MOCK-{suffix:05d}",
                "note": "ServiceNow not configured — run scripts/deploy_agents.py with a populated secret."}
    body: dict[str, Any] = {"short_description": short_desc[:160], "description": description,
                            "cab_required": "true" if cab else "false"}
    if assignment_group:
        body["assignment_group"] = assignment_group
    try:
        rec = client.post_table("change_request", body)
    except Exception as e:
        log.exception("change_request create failed")
        return {"error": f"{type(e).__name__}: {e}"}
    sys_id = rec.get("sys_id", "")
    return {"configured": True, "number": rec.get("number", ""), "sys_id": sys_id,
            "url": client.record_url("change_request", sys_id) if sys_id else ""}


def _add_affected_cis(payload: dict[str, Any]) -> dict[str, Any]:
    """Attach affected CIs to a change via task_ci. Requires change_sys_id."""
    client = _get_client()
    if not client:
        return {"configured": False, "attached": 0}
    change_sys_id = (payload.get("change_sys_id") or "").strip()
    ci_sys_ids = payload.get("ci_sys_ids") or []
    if not change_sys_id or not isinstance(ci_sys_ids, list):
        return {"error": "Missing change_sys_id or ci_sys_ids[]"}
    attached = 0
    for ci_sid in ci_sys_ids:
        try:
            # task_ci.ci_item needs the CI sys_id (not name); task = the change.
            client.post_table("task_ci", {"task": change_sys_id, "ci_item": ci_sid},
                              input_display_value=False)
            attached += 1
        except Exception as e:
            log.warning("task_ci attach failed for %s: %s", ci_sid, e)
    return {"configured": True, "attached": attached}


# ── shared write helpers (used by both @tool functions and action paths) ──
def _fmt_write(res: dict[str, Any], ok_msg: str) -> str:
    """Render a write-result dict as a concise chat string."""
    if not res.get("configured", True):
        return "(ServiceNow not configured)"
    if res.get("error"):
        return f"(error: {res['error']})"
    extra = f" ({res['number']})" if res.get("number") else ""
    return f"Done — {ok_msg}{extra}."


def _do_update(table: str, number: str, fields: dict[str, Any], *,
               work_note: str | None = None, comment: str | None = None) -> dict[str, Any]:
    """Resolve {table} by number, PATCH the given fields + optional journal entry."""
    client = _get_client()
    if not client:
        return {"configured": False}
    if not number:
        return {"error": "Missing 'number'"}
    rec = _find_by_number(client, table, number, fields="sys_id,number")
    if not rec:
        return {"error": f"No {table} found with number '{number}'"}
    body = dict(fields or {})
    if work_note:
        body["work_notes"] = work_note
    if comment:
        body["comments"] = comment
    if not body:
        return {"error": "No fields to update"}
    try:
        updated = client.patch_table(table, rec["sys_id"], body)
    except Exception as e:
        log.exception("%s update failed", table)
        return {"error": f"{type(e).__name__}: {e}"}
    return {"configured": True, "number": rec.get("number", number), "sys_id": rec["sys_id"],
            "state": updated.get("state"), "url": client.record_url(table, rec["sys_id"])}


def _do_comment(table: str, number: str, note: str, work_note: bool) -> dict[str, Any]:
    """Append a work note (internal) or comment (customer-visible) to a record."""
    if not (note or "").strip():
        return {"error": "Empty note"}
    if work_note:
        return _do_update(table, number, {}, work_note=note)
    return _do_update(table, number, {}, comment=note)


def _do_create_process(payload: dict[str, Any], *, table: str, default_desc: str,
                       extra_fields: tuple[str, ...]) -> dict[str, Any]:
    """Create an incident/problem record. Links a CI by name when supplied."""
    client = _get_client()
    short_desc = (payload.get("short_description") or payload.get("summary") or default_desc).strip()
    if not client:
        return {"configured": False, "note": "ServiceNow not configured."}
    body: dict[str, Any] = {"short_description": short_desc[:160],
                            "description": (payload.get("description") or "").strip()}
    for k in extra_fields:
        if payload.get(k):
            body[k] = payload[k]
    ci_ref = payload.get("ci") or payload.get("cmdb_ci")
    if ci_ref:
        ci = _resolve_ci(client, ci_ref)
        if ci and ci.get("name"):
            body["cmdb_ci"] = ci["name"]  # input_display_value=true resolves by name
    try:
        rec = client.post_table(table, body)
    except Exception as e:
        log.exception("%s create failed", table)
        return {"error": f"{type(e).__name__}: {e}"}
    sys_id = rec.get("sys_id", "")
    return {"configured": True, "number": rec.get("number", ""), "sys_id": sys_id,
            "url": client.record_url(table, sys_id) if sys_id else ""}


def _do_create_ci(payload: dict[str, Any]) -> dict[str, Any]:
    """Create a CMDB CI in its class table (default cmdb_ci)."""
    client = _get_client()
    name = (payload.get("name") or "").strip()
    ci_class = (payload.get("ci_class") or payload.get("sys_class_name") or "cmdb_ci").strip()
    if not name:
        return {"error": "Missing 'name'"}
    if not client:
        return {"configured": False, "note": "ServiceNow not configured."}
    body: dict[str, Any] = {"name": name}
    for k in ("correlation_id", "operational_status", "support_group",
              "environment", "short_description"):
        if payload.get(k):
            body[k] = payload[k]
    try:
        rec = client.post_table(ci_class, body)
    except Exception as e:
        log.exception("CI create failed")
        return {"error": f"{type(e).__name__}: {e}"}
    sys_id = rec.get("sys_id", "")
    return {"configured": True, "name": name, "ci_class": ci_class, "sys_id": sys_id,
            "url": client.record_url(ci_class, sys_id) if sys_id else ""}


def _do_update_ci(payload: dict[str, Any]) -> dict[str, Any]:
    """Update CMDB CI attributes (operational_status, support_group, environment, …)."""
    client = _get_client()
    resource = (payload.get("resource") or payload.get("ci") or "").strip()
    fields = dict(payload.get("fields") or {})
    for k in ("operational_status", "support_group", "environment", "short_description", "name"):
        if payload.get(k) is not None:
            fields[k] = payload[k]
    if not resource:
        return {"error": "Missing 'resource'"}
    if not fields:
        return {"error": "No fields to update"}
    if not client:
        return {"configured": False}
    ci = _resolve_ci(client, resource)
    if not ci:
        return {"error": f"No CMDB CI found for '{resource}'"}
    ci_class = ci.get("sys_class_name") or "cmdb_ci"
    try:
        client.patch_table(ci_class, ci["sys_id"], fields)
    except Exception as e:
        log.exception("CI update failed")
        return {"error": f"{type(e).__name__}: {e}"}
    return {"configured": True, "name": ci.get("name"), "sys_id": ci["sys_id"],
            "url": client.record_url(ci_class, ci["sys_id"])}


def _do_create_asset(payload: dict[str, Any]) -> dict[str, Any]:
    """Create an asset (default alm_hardware); optionally link it to a CI (2-step)."""
    client = _get_client()
    asset_class = (payload.get("asset_class") or "alm_hardware").strip()
    if not client:
        return {"configured": False, "note": "ServiceNow not configured."}
    body: dict[str, Any] = {}
    display_name = (payload.get("display_name") or payload.get("name") or "").strip()
    if display_name:
        body["display_name"] = display_name
    for k in ("asset_tag", "serial_number", "model_category", "model",
              "install_status", "assigned_to", "cost_center", "location"):
        if payload.get(k):
            body[k] = payload[k]
    try:
        rec = client.post_table(asset_class, body)  # names resolved (display_value=true)
    except Exception as e:
        log.exception("asset create failed")
        return {"error": f"{type(e).__name__}: {e}"}
    sys_id = rec.get("sys_id", "")
    # Link to CI as a separate sys_id write to avoid mixing display/value modes.
    ci_ref = payload.get("ci")
    if ci_ref and sys_id:
        sid = _resolve_ci_sys_id(client, ci_ref)
        if sid:
            try:
                client.patch_table(asset_class, sys_id, {"ci": sid}, input_display_value=False)
            except Exception as e:
                log.warning("asset ci-link failed: %s", e)
    return {"configured": True, "asset_tag": rec.get("asset_tag", ""), "sys_id": sys_id,
            "url": client.record_url(asset_class, sys_id) if sys_id else ""}


def _do_update_asset(payload: dict[str, Any]) -> dict[str, Any]:
    """Update an asset's lifecycle/assignment and optionally (re)link it to a CI."""
    client = _get_client()
    if not client:
        return {"configured": False}
    ref = (payload.get("asset_tag") or payload.get("sys_id") or "").strip()
    if not ref:
        return {"error": "Missing 'asset_tag' or 'sys_id'"}
    fields = dict(payload.get("fields") or {})
    for k in ("install_status", "substatus", "assigned_to", "location", "cost_center", "display_name"):
        if payload.get(k) is not None:
            fields[k] = payload[k]
    rows = client.get_table("alm_asset", query=f"asset_tag={ref}",
                            fields="sys_id,sys_class_name", limit=1)
    if rows:
        sys_id, cls = rows[0]["sys_id"], rows[0].get("sys_class_name") or "alm_asset"
    elif len(ref) == 32:
        sys_id, cls = ref, "alm_asset"
    else:
        return {"error": f"No asset found for '{ref}'"}
    try:
        if fields:
            client.patch_table(cls, sys_id, fields)
        ci_ref = payload.get("ci")
        if ci_ref:
            sid = _resolve_ci_sys_id(client, ci_ref)
            if sid:
                client.patch_table(cls, sys_id, {"ci": sid}, input_display_value=False)
    except Exception as e:
        log.exception("asset update failed")
        return {"error": f"{type(e).__name__}: {e}"}
    return {"configured": True, "asset_tag": ref, "sys_id": sys_id,
            "url": client.record_url(cls, sys_id)}


def _cmdb_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    """Dump bounded CMDB CI + asset lists for the master's drift correlation."""
    client = _get_client()
    if not client:
        return {"configured": False, "cis": [], "assets": []}
    cis: list[dict[str, Any]] = []
    try:
        for r in client.get_table(
                "cmdb_ci", limit=SNAPSHOT_LIMIT, display_value="true",
                fields="sys_id,name,sys_class_name,correlation_id,operational_status,"
                       "support_group.name,sys_updated_on"):
            cis.append({"sys_id": r.get("sys_id"), "name": r.get("name"),
                        "class": r.get("sys_class_name"), "correlation_id": r.get("correlation_id"),
                        "operational_status": r.get("operational_status"),
                        "owner_team": r.get("support_group.name"), "updated": r.get("sys_updated_on")})
    except Exception as e:
        log.warning("cmdb_ci snapshot failed: %s", e)
    assets: list[dict[str, Any]] = []
    try:
        for r in client.get_table(
                "alm_asset", limit=SNAPSHOT_LIMIT, display_value="true",
                fields="sys_id,asset_tag,display_name,serial_number,install_status,"
                       "assigned_to.name,ci.name,ci.correlation_id,model_category.name"):
            assets.append({"sys_id": r.get("sys_id"), "asset_tag": r.get("asset_tag"),
                           "display_name": r.get("display_name"), "serial": r.get("serial_number"),
                           "install_status": r.get("install_status"),
                           "assigned_to": r.get("assigned_to.name"), "ci_name": r.get("ci.name"),
                           "ci_correlation_id": r.get("ci.correlation_id"),
                           "category": r.get("model_category.name")})
    except Exception as e:
        log.warning("alm_asset snapshot failed: %s", e)
    return {"configured": True, "cis": cis, "assets": assets,
            "counts": {"cis": len(cis), "assets": len(assets)}}


def _hygiene_drift(client: ServiceNowClient, scope: str) -> list[str]:
    """ServiceNow-only hygiene drift (no AWS data needed): missing correlation_id /
    owner on CIs, and assets unlinked from a CI."""
    issues: list[str] = []
    if scope in ("cmdb", "all"):
        try:
            for r in client.get_table(
                    "cmdb_ci", limit=SNAPSHOT_LIMIT, display_value="true",
                    fields="name,correlation_id,support_group.name"):
                if not (r.get("correlation_id") or "").strip():
                    issues.append(f"CI '{r.get('name')}' has no correlation_id (untraceable to AWS).")
                if not (r.get("support_group.name") or "").strip():
                    issues.append(f"CI '{r.get('name')}' has no owning support group.")
        except Exception as e:
            log.warning("hygiene cmdb scan failed: %s", e)
    if scope in ("asset", "all"):
        try:
            for r in client.get_table(
                    "alm_asset", query="ciISEMPTY", limit=SNAPSHOT_LIMIT, display_value="true",
                    fields="asset_tag,display_name"):
                issues.append(f"Asset '{r.get('asset_tag') or r.get('display_name')}' is not linked to a CI.")
        except Exception as e:
            log.warning("hygiene asset scan failed: %s", e)
    return issues[:50]


def _impact_analysis(payload: dict[str, Any]) -> dict[str, Any]:
    """Full impact workflow: resolve CI → blast radius → owner → optional CHG.

    Returns structured JSON consumed by api_handler's /servicenow/impact-analysis
    route, which grafts the approver chain (via _build_approver_chain) before
    returning to the UI. When draft_change is true, also creates the CHG and
    attaches the affected CIs (create-then-attach ordering).
    """
    resource = (payload.get("resource") or payload.get("target_resource") or "").strip()
    if not resource:
        return {"error": "Missing 'resource'"}
    target_env = (payload.get("target_environment") or "PROD").strip().upper()
    severity = (payload.get("severity") or "HIGH").strip().upper()
    draft_change = bool(payload.get("draft_change"))
    cab = _cab_required(target_env, severity)

    client = _get_client()
    if not client:
        out: dict[str, Any] = {
            "configured": False, "changed_resource": {"input": resource},
            "affected_cis": [], "owner_team": "", "cab_required": cab,
            "note": "ServiceNow not configured — showing structure only.",
        }
        if draft_change:
            out["change"] = _create_change({
                "short_description": f"Change impact: {resource}",
                "description": f"Impact analysis for {resource} ({target_env}, {severity}).",
                "cab_required": cab})
        return out

    ci = _resolve_ci(client, resource)
    if not ci:
        return {"configured": True, "changed_resource": {"input": resource},
                "affected_cis": [], "owner_team": "", "cab_required": cab,
                "note": f"No CMDB CI found for '{resource}'."}

    owner = _ci_owner(client, ci)
    affected = _traverse_relationships(client, ci["sys_id"], MAX_DEPTH)
    out = {
        "configured": True,
        "changed_resource": {
            "input": resource, "sys_id": ci.get("sys_id"), "name": ci.get("name"),
            "class": ci.get("sys_class_name"), "correlation_id": ci.get("correlation_id"),
        },
        "affected_cis": affected,
        "owner_team": owner["owner_team"],
        "owner_group_sys_id": owner["group_sys_id"],
        "managed_by": owner["managed_by"],
        "cab_required": cab,
        "target_environment": target_env,
        "severity": severity,
    }

    if draft_change:
        desc_lines = [f"ARBITER change impact analysis for {ci.get('name')} ({resource}).",
                      f"Environment: {target_env}; Severity: {severity}.",
                      f"Owning team: {owner['owner_team'] or 'unassigned'}.",
                      f"Affected CIs ({len(affected)}):"]
        desc_lines += [f"- {a['name']} [{a['class']}] {a['direction']}" for a in affected[:30]]
        change = _create_change({
            "short_description": f"Change impact: {ci.get('name')}",
            "description": "\n".join(desc_lines),
            "assignment_group": owner["owner_team"],
            "cab_required": cab})
        out["change"] = change
        # Create-then-attach: the change must exist before task_ci rows.
        change_sid = change.get("sys_id")
        if change_sid and affected:
            out["affected_attached"] = _add_affected_cis({
                "change_sys_id": change_sid,
                "ci_sys_ids": [a["sys_id"] for a in affected]}).get("attached", 0)
    return out


# ──────────────────────────── agent factory ──────────────────────
def _model_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {"model_id": MODEL_ID, "region_name": REGION}
    if GUARDRAIL_ID:
        kwargs["guardrail_id"] = GUARDRAIL_ID
        kwargs["guardrail_version"] = GUARDRAIL_VERSION
    return kwargs


def build_agent() -> Agent:
    return Agent(
        model=BedrockModel(**_model_kwargs()),
        system_prompt=SYSTEM_PROMPT,
        tools=[query_ci, get_affected_cis, get_ci_owner, get_ci_details, query_change,
               query_incident, update_incident, comment_incident,
               query_problem, comment_problem, query_asset, detect_drift],
    )


# ──────────────────────────── AgentCore entrypoint ───────────────
@app.entrypoint
def invoke(payload: dict[str, Any]) -> dict[str, Any]:
    # Deterministic action paths (api_handler / master → /servicenow/*). No LLM.
    action = (payload.get("action") or "").strip()
    if action == "impact_analysis":
        return _impact_analysis(payload)
    if action == "create_change":
        return _create_change(payload)
    if action == "update_change":
        return _do_update("change_request", payload.get("number", ""), payload.get("fields") or {},
                          work_note=payload.get("work_note"), comment=payload.get("comment"))
    if action == "comment_change":
        return _do_comment("change_request", payload.get("number", ""),
                           payload.get("note", ""), bool(payload.get("work_note", True)))
    if action == "add_affected_cis":
        return _add_affected_cis(payload)
    if action == "create_incident":
        return _do_create_process(payload, table="incident", default_desc="ARBITER incident",
                                  extra_fields=("urgency", "impact", "priority",
                                                "assignment_group", "caller_id"))
    if action == "update_incident":
        return _do_update("incident", payload.get("number", ""), payload.get("fields") or {},
                          work_note=payload.get("work_note"), comment=payload.get("comment"))
    if action == "comment_incident":
        return _do_comment("incident", payload.get("number", ""),
                           payload.get("note", ""), bool(payload.get("work_note", True)))
    if action == "create_problem":
        return _do_create_process(payload, table="problem", default_desc="ARBITER problem",
                                  extra_fields=("priority", "assignment_group"))
    if action == "update_problem":
        return _do_update("problem", payload.get("number", ""), payload.get("fields") or {},
                          work_note=payload.get("work_note"), comment=payload.get("comment"))
    if action == "comment_problem":
        return _do_comment("problem", payload.get("number", ""),
                           payload.get("note", ""), bool(payload.get("work_note", True)))
    if action == "create_ci":
        return _do_create_ci(payload)
    if action == "update_ci":
        return _do_update_ci(payload)
    if action == "create_asset":
        return _do_create_asset(payload)
    if action == "update_asset":
        return _do_update_asset(payload)
    if action == "cmdb_snapshot":
        return _cmdb_snapshot(payload)

    prompt = payload.get("prompt") or payload.get("input") or ""
    if not prompt:
        return {"error": "Missing 'prompt'"}
    actor_id   = (payload.get("actor_id")   or "anonymous")[:128]
    persona    = (payload.get("persona")    or "employee")[:16]
    session_id = (payload.get("session_id") or "adhoc")[:128]
    chat_type  = (payload.get("chat_type")  or "analyst")[:16]
    user_email = (payload.get("user_email") or "")[:200]
    log.info("ServiceNow specialist: persona=%s session=%s prompt=%s",
             persona, session_id, prompt[:200])

    try:
        agent = build_agent()
        agent_result = agent(prompt)
    except Exception as e:
        log.exception("ServiceNow invocation failed")
        return {"result": f"(ServiceNow error: {type(e).__name__}: {e})"}

    record_from_agent_result(
        agent_result, agent="servicenow", persona=persona, actor_id=actor_id,
        session_id=session_id, chat_type=chat_type, model_id=MODEL_ID,
        user_email=user_email,
    )
    return {"result": str(agent_result)}


if __name__ == "__main__":
    app.run()
