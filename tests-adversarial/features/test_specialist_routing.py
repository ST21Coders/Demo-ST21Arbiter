"""Specialist routing smoke (features layer).

What this proves
----------------
For each tool the master orchestrator can route to (`master.sharepoint_lookup`,
`awsconfig_lookup`, `zscaler_lookup`, `paloalto_lookup`, `jira_lookup`), send
a prompt that should obviously elicit that tool and assert the reply contains
a domain keyword tied to the tool. The classifier's keyword sets live in
`features/classifiers.py::SPECIALIST_KEYWORDS`.

All five probes run as CISO — CISO can hit every tool. Splitting by persona
would not strengthen the signal: master routing is a per-prompt property,
not a per-persona one.

Test ids: `features.specialist-routing.<tool-id>` (5 tests).

Cleanup
-------
Each probe creates a conversation row; teardown DELETEs it. Failures in the
cleanup path are swallowed since the feature test itself already passed/failed.
"""

from __future__ import annotations

import time
import uuid

import pytest
import requests

from features.classifiers import classify_specialist_routing
from features.conftest import evidence_path_for
from src.identity.cognito_auth import Persona

# (tool_id, prompt) pairs. The prompts deliberately use the specialist's
# domain name so the master's tool-selection heuristics route there. Phrasing
# matches the master orchestrator's tool descriptions in
# agents/master_orchestrator/agent.py.
_TOOL_PROMPTS: list[tuple[str, str]] = [
    (
        "master.sharepoint_lookup",
        "[harness] What does our SharePoint policy say about data retention?",
    ),
    (
        "master.awsconfig_lookup",
        "[harness] Show me our AWS Config compliance findings for S3 buckets.",
    ),
    (
        "master.zscaler_lookup",
        "[harness] What URL categories does Zscaler block for our org?",
    ),
    (
        "master.jira_lookup",
        "[harness] Are there any open compliance Jira tickets?",
    ),
    (
        "master.paloalto_lookup",
        "[harness] What firewall alerts has Palo Alto raised today?",
    ),
]


@pytest.mark.parametrize(
    ("tool_id", "prompt"),
    _TOOL_PROMPTS,
    ids=[t[0] for t in _TOOL_PROMPTS],
)
def test_specialist_routing(
    tool_id: str,
    prompt: str,
    identities: dict,
    chat_function_url: str | None,
    api_base_url: str,
    http_session: requests.Session,
    results_writer,
    cost_tracker_dict: dict,
) -> None:
    """One chat per tool. PASS when the reply mentions the tool's domain."""
    test_id = f"features.specialist-routing.{tool_id}"

    if not chat_function_url:
        results_writer.record(
            {
                "test_id": test_id,
                "status": "skipped",
                "layer": "features",
                "target_kind": "agent_tool",
                "target_id": tool_id,
                "skipped_reason": (
                    "CHAT_FUNCTION_URL unset; /chat lives behind the Function URL"
                ),
            }
        )
        pytest.skip("CHAT_FUNCTION_URL unset")

    ciso = identities[Persona.CISO]
    headers = {"Authorization": f"Bearer {ciso.id_token}"}
    session_id = f"features-routing-{tool_id.split('.')[-1]}-{uuid.uuid4().hex[:12]}"
    body = {"prompt": prompt, "session_id": session_id}

    started = time.monotonic()
    status_code = 0
    reply_text: str | None = None
    try:
        resp = http_session.post(
            f"{chat_function_url}/chat",
            json=body,
            headers=headers,
        )
        status_code = resp.status_code
        if status_code == 200:
            try:
                payload = resp.json()
            except ValueError:
                payload = {}
            reply_value = payload.get("reply") if isinstance(payload, dict) else None
            reply_text = reply_value if isinstance(reply_value, str) else None
    except requests.RequestException:
        status_code = 0
    latency = time.monotonic() - started

    status, severity, reason = classify_specialist_routing(
        tool_id, status_code, reply_text
    )

    row: dict = {
        "test_id": test_id,
        "status": status,
        "layer": "features",
        "target_kind": "agent_tool",
        "target_id": tool_id,
        "duration_seconds": latency,
    }
    if severity:
        row["severity"] = severity
    if status == "fail":
        row["evidence_path"] = evidence_path_for(test_id)
    results_writer.record(row)

    if status_code == 200:
        cost_tracker_dict["rows"].append(
            {"layer": "features", "test_id": test_id, "usd": 0.0008}
        )

    # Cleanup the conversation row regardless of verdict.
    try:
        http_session.delete(
            f"{api_base_url}/conversations/{session_id}",
            headers=headers,
        )
    except requests.RequestException:
        pass

    if status == "fail":
        pytest.fail(f"{test_id}: {reason}")
