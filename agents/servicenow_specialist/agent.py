"""ARBITER ServiceNow Specialist — runs on Bedrock AgentCore Runtime.

Answers IT-asset configuration-change IMPACT-ANALYSIS questions by reading the
ServiceNow CMDB and Change Management modules: given a changed AWS resource, it
resolves the matching CI, walks cmdb_rel_ci for the blast radius, finds the
owning team, and (on request) drafts a change_request with the affected CIs
attached.

Connection is direct REST (Table API + Change API). All ServiceNow reach is
behind a thin ServiceNowClient so an MCP backend can replace it later without
touching the @tool functions — mirroring how jira_specialist is structured.

Two invocation paths, like jira_specialist:
  - Chat path (payload.prompt): a Strands Agent runs with the @tool functions.
  - Deterministic action paths (payload.action), no LLM, used by api_handler:
      impact_analysis   — full resolve→traverse→owner→(optional draft CHG)
      create_change     — POST change_request
      add_affected_cis  — POST task_ci rows for a change

Environment variables:
  MODEL_ID             Bedrock model (default: Nova 2 Lite cross-region profile)
  GUARDRAIL_ID         Optional guardrail
  GUARDRAIL_VERSION    Guardrail version (default: DRAFT)
  KB_ID                Bedrock Knowledge Base ID (optional; not required here)
  SERVICENOW_API_BASE  Instance base URL, e.g. https://dev12345.service-now.com
  SERVICENOW_SECRET_ID Secrets Manager id holding either
                       {instance_url, username, password} (basic auth) or
                       {instance_url, client_id, client_secret} (OAuth2).
                       Empty/unreadable = agent runs in "(ServiceNow not
                       configured)" mode (mock CHG ids, like jira).
  SERVICENOW_MAX_DEPTH cmdb_rel_ci BFS depth cap (default 3).
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
HTTP_TIMEOUT = 20

SYSTEM_PROMPT = """You are the ServiceNow specialist for ARBITER. You answer IT
asset configuration-change impact questions from the ServiceNow CMDB and Change
Management modules.

Use the tools to read live data:
  - query_ci: resolve an AWS resource id/ARN or name to a CMDB CI.
  - get_affected_cis: walk CI relationships (cmdb_rel_ci) to find the blast
    radius of a change — what depends on, and what is depended on by, a CI.
  - get_ci_owner: the support/assignment group that owns a CI (who does the work).
  - query_change: look up an existing change_request by number.

When asked "what is the impact of changing X / who does the work / who approves",
resolve the CI, list the affected CIs, name the owning team, and state whether
the change needs CAB approval (PROD or high-risk changes do). Cite CI names and
change numbers verbatim. Never fabricate sys_ids, CI names, or change numbers —
if a tool returns nothing, say so. Keep answers concise and factual.
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
        if creds.get("client_id") and creds.get("client_secret"):
            self._bearer = self._fetch_oauth_token()
            self._session.headers["Authorization"] = f"Bearer {self._bearer}"
        else:
            self._session.auth = (creds.get("username", ""), creds.get("password", ""))

    def _fetch_oauth_token(self) -> str:
        """OAuth2 client-credentials grant against /oauth_token.do."""
        resp = requests.post(
            f"{self.base}/oauth_token.do",
            data={
                "grant_type": "client_credentials",
                "client_id": self._creds["client_id"],
                "client_secret": self._creds["client_secret"],
            },
            timeout=HTTP_TIMEOUT,
        )
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
        tools=[query_ci, get_affected_cis, get_ci_owner, query_change],
    )


# ──────────────────────────── AgentCore entrypoint ───────────────
@app.entrypoint
def invoke(payload: dict[str, Any]) -> dict[str, Any]:
    # Deterministic action paths (api_handler → /servicenow/*). No LLM.
    action = (payload.get("action") or "").strip()
    if action == "impact_analysis":
        return _impact_analysis(payload)
    if action == "create_change":
        return _create_change(payload)
    if action == "add_affected_cis":
        return _add_affected_cis(payload)

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
