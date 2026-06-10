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
  JIRA_SECRET_ID     Secrets Manager id holding {"url","email","api_token"}.
                     Empty/unreadable = agent runs in "(JIRA not configured)" mode.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import boto3
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from mcp import StdioServerParameters, stdio_client
from strands import Agent
from strands.models.bedrock import BedrockModel
from strands.tools.mcp import MCPClient

from _shared.token_usage import record_from_agent_result

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("jira_specialist")

REGION = os.environ.get("AWS_REGION", "us-east-1")
MODEL_ID = os.environ.get("MODEL_ID", "us.amazon.nova-2-lite-v1:0")
JIRA_SECRET_ID = os.environ.get("JIRA_SECRET_ID", "")
GUARDRAIL_ID = os.environ.get("GUARDRAIL_ID")
GUARDRAIL_VERSION = os.environ.get("GUARDRAIL_VERSION", "DRAFT")

# Tier-0 least-privilege scoping for the mcp-atlassian server (set via runtime
# env in deploy_agents.py so they're declarative). Passed straight through to
# the mcp-atlassian subprocess:
#   ENABLED_TOOLS        — allowlist of MCP tool names the server exposes; drops
#                          unused write/delete/transition + all Confluence tools.
#   JIRA_PROJECTS_FILTER — optional: restrict Jira ops to these project keys.
#                          OFF by default (empty) so reads aren't silently scoped
#                          out — set it via runtime env only if you want the limit.
# Env-var names match mcp-atlassian >= 0.11.x.
JIRA_ENABLED_TOOLS = os.environ.get(
    "ENABLED_TOOLS",
    "jira_search,jira_get_issue,jira_get_all_projects,jira_create_issue",
)
JIRA_PROJECTS_FILTER = os.environ.get("JIRA_PROJECTS_FILTER", "")

SYSTEM_PROMPT = """You are the JIRA specialist for ARBITER. You answer
questions about Jira issues, tickets, projects, and sprints, and you can create
or update issues when explicitly asked.

Use the available Jira tools to read live data. Cite the issue keys (e.g.
MIG-123) you reference. Do not fabricate issue keys, statuses, or assignees —
if a tool returns nothing, say so. Keep answers concise and factual, suitable
for a security analyst's ticket notes.
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
    )))


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


@app.entrypoint
def invoke(payload: dict[str, Any]) -> dict[str, Any]:
    creds = _load_jira_credentials()
    if not creds:
        return {"result": "(JIRA not configured — set JIRA_SECRET_ID to a Secrets "
                          "Manager secret holding url/email/api_token)",
                "error": "not_configured"}

    # Structured create path (api_handler → /jira/tickets). Deterministic,
    # no LLM — returns {key, url}.
    if (payload.get("action") or "").strip() == "create_issue":
        return _create_issue(payload, creds)

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
