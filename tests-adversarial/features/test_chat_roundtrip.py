"""Per-persona /chat round-trip smoke (features layer).

What this proves
----------------
For each of the four demo personas, send a simple prompt to /chat via the
Lambda Function URL and assert the master orchestrator returns a non-empty
reply within the wall-clock budget.

  Body: {"prompt": "[harness] What pages am I allowed to see?",
         "session_id": "<uuid>"}

PASS    — HTTP 200, `reply` field present with >= 20 chars, latency < 30 s.
FAIL HIGH   — 5xx / transport drop / missing reply.
FAIL MEDIUM — reply too short OR latency above budget.

Cleanup
-------
Every chat creates a conversation row in DDB. The test teardown deletes the
session via DELETE /conversations/{session_id} so successive runs don't
accumulate sessions in dev. Cleanup failures are logged but do not change
the test verdict — the feature itself worked, the harness's house-keeping
is a separate concern.

Test ids: `features.chat-roundtrip.<persona>` (4 tests).
"""

from __future__ import annotations

import time
import uuid

import pytest
import requests

from features.classifiers import classify_chat_roundtrip
from features.conftest import evidence_path_for
from src.identity.cognito_auth import Persona


# Order matches the manifest's persona list. Parametrising on the enum keeps
# the test ids stable across renames.
_PERSONA_IDS = [p.value for p in Persona]


@pytest.mark.parametrize("persona_id", _PERSONA_IDS, ids=_PERSONA_IDS)
def test_chat_roundtrip(
    persona_id: str,
    identities: dict,
    chat_function_url: str | None,
    api_base_url: str,
    http_session: requests.Session,
    results_writer,
    cost_tracker_dict: dict,
) -> None:
    """One chat round-trip per persona. PASS when the master replies cleanly."""
    test_id = f"features.chat-roundtrip.{persona_id}"

    if not chat_function_url:
        results_writer.record(
            {
                "test_id": test_id,
                "status": "skipped",
                "layer": "features",
                "target_kind": "api_route",
                "target_id": "post-chat",
                "skipped_reason": (
                    "CHAT_FUNCTION_URL unset; /chat lives behind the Function URL"
                ),
            }
        )
        pytest.skip("CHAT_FUNCTION_URL unset")

    identity = identities[Persona(persona_id)]
    headers = {"Authorization": f"Bearer {identity.id_token}"}
    session_id = f"features-chat-{persona_id}-{uuid.uuid4().hex[:12]}"
    body = {
        "prompt": "[harness] What pages am I allowed to see?",
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
            # api_handler.py emits `reply` at the top level — see _handle_chat.
            reply_value = payload.get("reply") if isinstance(payload, dict) else None
            reply_text = reply_value if isinstance(reply_value, str) else None
    except requests.RequestException:
        status_code = 0
    latency = time.monotonic() - started

    status, severity, reason = classify_chat_roundtrip(status_code, reply_text, latency)

    row: dict = {
        "test_id": test_id,
        "status": status,
        "layer": "features",
        "target_kind": "api_route",
        "target_id": "post-chat",
        "persona": persona_id,
        "duration_seconds": latency,
    }
    if severity:
        row["severity"] = severity
    if status == "fail":
        row["evidence_path"] = evidence_path_for(test_id)
    results_writer.record(row)

    # Cost: account a small placeholder per successful turn. The orchestrator
    # treats this layer's per-call spend as informational; we don't have
    # response telemetry here so a conservative $0.0005 keeps the budget
    # honest without coupling to the live token count.
    if status_code == 200:
        cost_tracker_dict["rows"].append(
            {"layer": "features", "test_id": test_id, "usd": 0.0005}
        )

    # Cleanup: best-effort DELETE on the API Gateway endpoint. The /chat
    # turn created a DDB row keyed on session_id; remove it so we don't leak
    # state across runs. Failures are intentionally swallowed.
    try:
        http_session.delete(
            f"{api_base_url}/conversations/{session_id}",
            headers=headers,
        )
    except requests.RequestException:
        pass

    if status == "fail":
        pytest.fail(f"{test_id}: {reason}")
