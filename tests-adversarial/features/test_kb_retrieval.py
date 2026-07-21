"""Knowledge Base retrieval smoke (features layer).

What this proves
----------------
Send a compliance-themed prompt that should trigger the master orchestrator
to use the SharePoint specialist (which queries the seeded Bedrock KB
`SQCLG3W09Y`). Assert the reply contains at least one KB-grounding marker
(citation, "source:", "according to", domain phrase like "gdpr" / "retention").

PASS    — reply contains a KB-grounding marker.
FAIL HIGH   — chat returned 5xx / empty.
FAIL MEDIUM — reply present but no grounding signal — the answer was generic.

Test id: `features.kb-retrieval.compliance-query` (1 test).

Cleanup
-------
DELETEs the created session on the way out.
"""

from __future__ import annotations

import time
import uuid

import pytest
import requests

from features.classifiers import classify_kb_retrieval
from features.conftest import evidence_path_for
from src.identity.cognito_auth import Persona


def test_kb_retrieval_compliance_query(
    identities: dict,
    chat_function_url: str | None,
    api_base_url: str,
    http_session: requests.Session,
    results_writer,
    cost_tracker_dict: dict,
) -> None:
    """One KB-themed prompt. PASS when the reply shows grounding."""
    test_id = "features.kb-retrieval.compliance-query"

    if not chat_function_url:
        results_writer.record(
            {
                "test_id": test_id,
                "status": "skipped",
                "layer": "features",
                "target_kind": "agent_tool",
                "target_id": "master.sharepoint_lookup",
                "skipped_reason": "CHAT_FUNCTION_URL unset",
            }
        )
        pytest.skip("CHAT_FUNCTION_URL unset")

    ciso = identities[Persona.CISO]
    headers = {"Authorization": f"Bearer {ciso.id_token}"}
    session_id = f"features-kb-{uuid.uuid4().hex[:12]}"
    body = {
        "prompt": (
            "[harness] What are the GDPR data retention requirements "
            "according to our compliance documentation?"
        ),
        "session_id": session_id,
    }

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

    status, severity, reason = classify_kb_retrieval(status_code, reply_text)

    # Targeted at the sharepoint specialist tool since that's the documented
    # path to the KB. The coverage matrix will surface this as a "tool
    # exercised in a positive feature smoke" row.
    row: dict = {
        "test_id": test_id,
        "status": status,
        "layer": "features",
        "target_kind": "agent_tool",
        "target_id": "master.sharepoint_lookup",
        "duration_seconds": latency,
    }
    if severity:
        row["severity"] = severity
    if status == "fail":
        row["evidence_path"] = evidence_path_for(test_id)
    results_writer.record(row)

    if status_code == 200:
        cost_tracker_dict["rows"].append(
            {"layer": "features", "test_id": test_id, "usd": 0.001}
        )

    try:
        http_session.delete(
            f"{api_base_url}/conversations/{session_id}",
            headers=headers,
        )
    except requests.RequestException:
        pass

    if status == "fail":
        pytest.fail(f"{test_id}: {reason}")
