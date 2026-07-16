"""ARBITER JIRA Specialist — runs on Bedrock AgentCore Runtime.

Unlike the other specialists (which call boto3 / the Bedrock KB directly), this
agent reaches Jira through the Model Context Protocol. It spawns the
open-source `mcp-atlassian` server as a stdio subprocess and exposes that
server's tools to a Strands `Agent` via `MCPClient`.

Per the Strands docs, an MCP client's tools are only valid inside the client's
`with` block, so the model invocation happens there (see invoke()).

Environment variables:
  MODEL_ID           Bedrock model (default: Nova 2 Lite cross-region inference profile)
  GUARDRAIL_ID       Optional guardrail
  GUARDRAIL_VERSION  Guardrail version (default: DRAFT)
  JIRA_SECRET_ID     Secrets Manager id holding {"url","email","api_token"} and
                     optionally "confluence_url" (the .../wiki base) to enable the
                     Confluence tools (username/token are reused from email/api_token).
                     Empty/unreadable = agent runs in "(JIRA not configured)" mode.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import timedelta
from typing import Any

import boto3
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from mcp import StdioServerParameters, stdio_client
from strands import Agent
from strands.models.bedrock import BedrockModel
from strands.tools.mcp import MCPClient
from strands.types.exceptions import MCPClientInitializationError

from _shared.token_usage import record_from_agent_result

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("jira_specialist")

REGION = os.environ.get("AWS_REGION", "us-east-1")
MODEL_ID = os.environ.get("MODEL_ID", "us.amazon.nova-2-lite-v1:0")
JIRA_SECRET_ID = os.environ.get("JIRA_SECRET_ID", "")
GUARDRAIL_ID = os.environ.get("GUARDRAIL_ID")
GUARDRAIL_VERSION = os.environ.get("GUARDRAIL_VERSION", "DRAFT")

# Bound the mcp-atlassian stdio subprocess. The runtime is VPC-attached (PrivateSubnet2
# → NAT → *.atlassian.net); if that egress is blocked or the MCP handshake stalls, an
# unbounded start()/list_tools would hang until the AgentCore runtime's own timeout,
# which looks like "not responding". startup_timeout caps the subprocess spawn + MCP
# init; MCP_TOOL_TIMEOUT caps a single deterministic Jira/Confluence tool call. Both are
# env-overridable so ops can tune without a code change (strands MCPClient default is 30s).
MCP_STARTUP_TIMEOUT = int(os.environ.get("MCP_STARTUP_TIMEOUT", "25"))
MCP_TOOL_TIMEOUT = timedelta(seconds=int(os.environ.get("MCP_TOOL_TIMEOUT_SECONDS", "45")))

# Tier-0 least-privilege scoping for the mcp-atlassian server (set via runtime
# env in deploy_agents.py so they're declarative). Passed straight through to
# the mcp-atlassian subprocess:
#   ENABLED_TOOLS        — allowlist of MCP tool names the server exposes: Jira
#                          read + create + L1-resolution (transition/comment) plus
#                          Confluence search/read/create/update. The Confluence
#                          tools only function when CONFLUENCE_URL is in the secret.
#   JIRA_PROJECTS_FILTER — optional: restrict Jira ops to these project keys.
#                          OFF by default (empty) so reads aren't silently scoped
#                          out — set it via runtime env only if you want the limit.
# Env-var names match mcp-atlassian >= 0.11.x.
JIRA_ENABLED_TOOLS = os.environ.get(
    "ENABLED_TOOLS",
    # Tier-0 read + create, plus L1-resolution write tools (transition/comment).
    # get_transitions is needed so a transition can be resolved by name → id
    # defensively (workflow transition ids differ per project).
    "jira_search,jira_get_issue,jira_get_all_projects,jira_create_issue,"
    "jira_get_transitions,jira_transition_issue,jira_add_comment,"
    # Confluence read + page create/update (enabled only when CONFLUENCE_URL set).
    "confluence_search,confluence_get_page,confluence_create_page,confluence_update_page",
)
JIRA_PROJECTS_FILTER = os.environ.get("JIRA_PROJECTS_FILTER", "")
# Default Confluence space KEY to use when the user gives a space by display name
# or omits it. mcp-atlassian's confluence_create_page needs the key, not the name.
# Set via deploy_agents.py env override.
CONFLUENCE_DEFAULT_SPACE_KEY = os.environ.get("CONFLUENCE_DEFAULT_SPACE_KEY", "Arbiterpoc")

SYSTEM_PROMPT = f"""You are the Atlassian specialist for ARBITER. You work with
Jira (issues, tickets, projects, sprints) and Confluence (spaces and pages). You
read live data and, when explicitly asked, create/update Jira issues and
create/update Confluence pages.

Use the available tools for live data. Cite the artifacts you reference — Jira
issue keys (e.g. MIG-123) and Confluence page titles/URLs.

When creating a Confluence page, pass the space KEY to confluence_create_page —
never the space display name. If the user gives a space by display name (e.g.
"Arbiter-poc-confluence") or does not name a space, use the default space key
"{CONFLUENCE_DEFAULT_SPACE_KEY}". Render the supplied content as the page body and
return the new page's title and URL.

Do not fabricate issue keys, page ids, statuses, or assignees — if a tool returns
nothing, say so. Keep answers concise and factual.
"""

app = BedrockAgentCoreApp()
secrets_client = boto3.client("secretsmanager", region_name=REGION)


def _load_jira_credentials() -> dict[str, str] | None:
    """Read {url, email, api_token} from Secrets Manager. None if unavailable."""
    if not JIRA_SECRET_ID:
        return None
    try:
        resp = secrets_client.get_secret_value(SecretId=JIRA_SECRET_ID)
        return json.loads(resp["SecretString"])
    except Exception as e:
        log.warning("Could not load JIRA secret: %s", e)
        return None


def _build_mcp_client(creds: dict[str, str]) -> MCPClient:
    """Spawn the mcp-atlassian server over stdio, scoped to Jira Cloud.

    mcp-atlassian reads JIRA_URL / JIRA_USERNAME / JIRA_API_TOKEN from its own
    process env, so we pass the credentials through StdioServerParameters.env.
    """
    # Merge onto the parent env (not replace) so the subprocess keeps PATH etc.
    # needed to locate/run the mcp-atlassian executable.
    subprocess_env = {
        **os.environ,
        "JIRA_URL": creds.get("url", ""),
        "JIRA_USERNAME": creds.get("email", ""),
        "JIRA_API_TOKEN": creds.get("api_token", ""),
    }
    # Confluence (same Atlassian site + token). Only set when confluence_url is
    # present in the secret, so a Jira-only secret can't half-configure Confluence.
    # mcp-atlassian reads CONFLUENCE_URL/USERNAME/API_TOKEN from its process env.
    confluence_url = creds.get("confluence_url", "")
    if confluence_url:
        subprocess_env["CONFLUENCE_URL"] = confluence_url
        subprocess_env["CONFLUENCE_USERNAME"] = creds.get("email", "")
        subprocess_env["CONFLUENCE_API_TOKEN"] = creds.get("api_token", "")
    # Tier-0 scoping — only set when configured so an empty value can't
    # accidentally widen the surface.
    if JIRA_PROJECTS_FILTER:
        subprocess_env["JIRA_PROJECTS_FILTER"] = JIRA_PROJECTS_FILTER
    if JIRA_ENABLED_TOOLS:
        subprocess_env["ENABLED_TOOLS"] = JIRA_ENABLED_TOOLS
    return MCPClient(lambda: stdio_client(StdioServerParameters(
        command="mcp-atlassian",
        args=["--transport", "stdio"],
        env=subprocess_env,
    )), startup_timeout=MCP_STARTUP_TIMEOUT)


def _model_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {"model_id": MODEL_ID, "region_name": REGION}
    if GUARDRAIL_ID:
        kwargs["guardrail_id"] = GUARDRAIL_ID
        kwargs["guardrail_version"] = GUARDRAIL_VERSION
    return kwargs


def _tool_result_text(result: dict[str, Any]) -> str:
    """Flatten an MCPClient.call_tool_sync result's content blocks to text."""
    parts: list[str] = []
    for block in result.get("content") or []:
        if not isinstance(block, dict):
            continue
        if "text" in block:
            parts.append(str(block["text"]))
        elif "json" in block:
            parts.append(json.dumps(block["json"]))
    return "\n".join(parts)


def _create_issue(payload: dict[str, Any], creds: dict[str, str]) -> dict[str, Any]:
    """Deterministically create a Jira issue via the MCP create tool.

    No LLM in this path — the api_handler hands us exact summary/description and
    expects a clean {key, url} back, so we call the mcp-atlassian create tool
    directly and parse the issue key out of its response. The browse URL is
    built from the Jira base URL in the secret.
    """
    project_key = (payload.get("project_key") or "DEVARBITER").strip()
    summary     = (payload.get("summary") or payload.get("title") or "ARBITER ticket").strip()
    description = (payload.get("description") or "").strip()
    issue_type  = (payload.get("issue_type") or "Task").strip()

    try:
        jira_mcp = _build_mcp_client(creds)
        with jira_mcp:
            # Resolve the create tool name from the live tool list (defensive
            # against mcp-atlassian renaming it across versions).
            tools = jira_mcp.list_tools_sync()
            create_tool = next(
                (t.tool_name for t in tools
                 if "create" in t.tool_name.lower() and "issue" in t.tool_name.lower()),
                "jira_create_issue",
            )
            result = jira_mcp.call_tool_sync(
                tool_use_id="arbiter-create-issue",
                name=create_tool,
                arguments={
                    "project_key": project_key,
                    "summary": summary,
                    "issue_type": issue_type,
                    "description": description,
                },
                read_timeout_seconds=MCP_TOOL_TIMEOUT,
            )
    except Exception as e:
        log.exception("JIRA create_issue failed")
        return {"error": f"{type(e).__name__}: {e}"}

    raw = _tool_result_text(result)
    if result.get("status") == "error":
        return {"error": raw or "JIRA create tool returned an error"}

    # Pull the created issue key (e.g. DEVARBITER-123) out of the tool response.
    m = (re.search(r'"key"\s*:\s*"([A-Z][A-Z0-9]+-\d+)"', raw)
         or re.search(r'\b([A-Z][A-Z0-9]+-\d+)\b', raw))
    if not m:
        return {"error": f"Could not parse issue key from JIRA response: {raw[:300]}"}
    key = m.group(1)
    url = f"{creds.get('url', '').rstrip('/')}/browse/{key}"
    log.info("Created JIRA issue %s in %s", key, project_key)
    return {"key": key, "url": url, "summary": summary, "project_key": project_key}


def _resolve_tool(tools, *needles, default=None):
    """Pick the first MCP tool whose name contains ALL needles (case-insensitive).

    Defensive against mcp-atlassian renaming tools across versions.
    """
    low = [n.lower() for n in needles]
    return next(
        (t.tool_name for t in tools
         if all(n in t.tool_name.lower() for n in low)),
        default,
    )


def _get_transitions(jira_mcp, tools, issue_key: str) -> list[dict[str, str]]:
    """Return [{id, name}] of available workflow transitions for an issue."""
    get_tool = _resolve_tool(tools, "transition", "get") or _resolve_tool(tools, "transition", "list")
    if not get_tool:
        return []
    try:
        result = jira_mcp.call_tool_sync(
            tool_use_id="arbiter-get-transitions", name=get_tool,
            arguments={"issue_key": issue_key},
            read_timeout_seconds=MCP_TOOL_TIMEOUT,
        )
    except Exception:
        log.exception("get_transitions call failed for %s", issue_key)
        return []
    try:
        data = json.loads(_tool_result_text(result))
    except Exception:
        return []
    items = data.get("transitions") if isinstance(data, dict) else data
    out: list[dict[str, str]] = []
    for it in (items or []):
        if isinstance(it, dict):
            tid = str(it.get("id") or it.get("transition_id") or "")
            tname = str(it.get("name") or "")
            if tid:
                out.append({"id": tid, "name": tname})
    return out


def _post_comment(jira_mcp, tools, issue_key: str, body: str) -> tuple[bool, str]:
    """Add a comment via the MCP comment tool. Returns (ok, raw_text).

    mcp-atlassian's jira_add_comment takes the comment text as `body` (not
    `comment`), so we pass it under that key.
    """
    comment_tool = _resolve_tool(tools, "comment", "add", default="jira_add_comment")
    result = jira_mcp.call_tool_sync(
        tool_use_id="arbiter-comment", name=comment_tool,
        arguments={"issue_key": issue_key, "body": body},
        read_timeout_seconds=MCP_TOOL_TIMEOUT)
    if result.get("status") == "error":
        return False, _tool_result_text(result) or "JIRA comment tool returned an error"
    return True, _tool_result_text(result)


def _transition_issue(payload: dict[str, Any], creds: dict[str, str]) -> dict[str, Any]:
    """Deterministically transition a Jira issue (L1 resolution).

    Resolves the requested transition by id or name (exact then fuzzy-contains)
    against the issue's live transition list, so callers can ask for "Done"
    without knowing the project's transition ids. Optionally adds a comment in
    the same call. No LLM in this path.
    """
    issue_key = (payload.get("issue_key") or payload.get("jira_key") or "").strip()
    want = (payload.get("transition") or payload.get("transition_name") or "Done").strip()
    comment = (payload.get("comment") or "").strip()
    if not issue_key:
        return {"error": "Missing issue_key"}
    try:
        jira_mcp = _build_mcp_client(creds)
        with jira_mcp:
            tools = jira_mcp.list_tools_sync()
            transitions = _get_transitions(jira_mcp, tools, issue_key)
            chosen = None
            for tr in transitions:                      # exact id or name
                if tr["id"] == want or tr["name"].lower() == want.lower():
                    chosen = tr
                    break
            if chosen is None:                          # fuzzy contains
                for tr in transitions:
                    if want.lower() in tr["name"].lower():
                        chosen = tr
                        break
            if chosen is None:
                return {"error": f"No transition matching '{want}' on {issue_key}",
                        "available_transitions": [t["name"] for t in transitions]}
            trans_tool = _resolve_tool(tools, "transition", "issue", default="jira_transition_issue")
            result = jira_mcp.call_tool_sync(
                tool_use_id="arbiter-transition", name=trans_tool,
                arguments={"issue_key": issue_key, "transition_id": chosen["id"]},
                read_timeout_seconds=MCP_TOOL_TIMEOUT)
            if result.get("status") == "error":
                return {"error": _tool_result_text(result) or "JIRA transition tool returned an error"}
            # Comment is posted as a SEPARATE call (the transition and comment
            # tools take different arg shapes) so a comment-arg quirk can never
            # fail the transition itself.
            comment_ok = True
            if comment:
                comment_ok, _ = _post_comment(jira_mcp, tools, issue_key, comment)
    except Exception as e:
        log.exception("JIRA transition failed")
        return {"error": f"{type(e).__name__}: {e}"}
    url = f"{creds.get('url', '').rstrip('/')}/browse/{issue_key}"
    log.info("Transitioned JIRA issue %s → %s (commented=%s)", issue_key, chosen["name"], bool(comment) and comment_ok)
    return {"key": issue_key, "url": url, "transitioned_to": chosen["name"],
            "transition_id": chosen["id"], "commented": bool(comment) and comment_ok}


def _add_comment(payload: dict[str, Any], creds: dict[str, str]) -> dict[str, Any]:
    """Deterministically add a comment to a Jira issue. No LLM in this path."""
    issue_key = (payload.get("issue_key") or payload.get("jira_key") or "").strip()
    comment = (payload.get("comment") or "").strip()
    if not issue_key or not comment:
        return {"error": "Missing issue_key or comment"}
    try:
        jira_mcp = _build_mcp_client(creds)
        with jira_mcp:
            tools = jira_mcp.list_tools_sync()
            ok, raw = _post_comment(jira_mcp, tools, issue_key, comment)
    except Exception as e:
        log.exception("JIRA add_comment failed")
        return {"error": f"{type(e).__name__}: {e}"}
    if not ok:
        return {"error": raw}
    url = f"{creds.get('url', '').rstrip('/')}/browse/{issue_key}"
    log.info("Commented on JIRA issue %s", issue_key)
    return {"key": issue_key, "url": url, "commented": True}


@app.entrypoint
def invoke(payload: dict[str, Any]) -> dict[str, Any]:
    creds = _load_jira_credentials()
    if not creds:
        return {"result": "(JIRA not configured — set JIRA_SECRET_ID to a Secrets "
                          "Manager secret holding url/email/api_token)",
                "error": "not_configured"}

    # Structured action paths (api_handler → /jira/*). Deterministic, no LLM.
    action = (payload.get("action") or "").strip()
    if action == "create_issue":
        return _create_issue(payload, creds)
    if action == "transition_issue":
        return _transition_issue(payload, creds)
    if action == "add_comment":
        return _add_comment(payload, creds)

    prompt = payload.get("prompt") or payload.get("input") or ""
    if not prompt:
        return {"error": "Missing 'prompt'"}
    actor_id   = (payload.get("actor_id")   or "anonymous")[:128]
    persona    = (payload.get("persona")    or "employee")[:16]
    session_id = (payload.get("session_id") or "adhoc")[:128]
    chat_type  = (payload.get("chat_type")  or "analyst")[:16]
    log.info("JIRA specialist: persona=%s session=%s prompt=%s",
             persona, session_id, prompt[:200])

    # MCP tools are only valid inside the client's context manager, so the
    # model invocation lives here too (Strands requirement).
    try:
        jira_mcp = _build_mcp_client(creds)
        with jira_mcp:
            tools = jira_mcp.list_tools_sync()
            agent = Agent(
                model=BedrockModel(**_model_kwargs()),
                system_prompt=SYSTEM_PROMPT,
                tools=tools,
            )
            agent_result = agent(prompt)
    except MCPClientInitializationError as e:
        # Startup/handshake exceeded MCP_STARTUP_TIMEOUT — almost always VPC egress to
        # *.atlassian.net being blocked, or the mcp-atlassian entrypoint stalling. Surface
        # a fast, actionable message instead of letting the runtime time out silently.
        log.exception("JIRA MCP init/timeout")
        return {"result": f"(JIRA timeout/connectivity: could not initialize the Atlassian "
                          f"MCP server within {MCP_STARTUP_TIMEOUT}s — check VPC egress to "
                          f"*.atlassian.net and the API token. {e})",
                "error": "mcp_init_timeout"}
    except Exception as e:
        log.exception("JIRA MCP invocation failed")
        return {"result": f"(JIRA error: {type(e).__name__}: {e})"}

    record_from_agent_result(
        agent_result, agent="jira", persona=persona, actor_id=actor_id,
        session_id=session_id, chat_type=chat_type, model_id=MODEL_ID,
    )
    return {"result": str(agent_result)}


if __name__ == "__main__":
    app.run()
