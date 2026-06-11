"""Conversation persistence smoke (features layer).

What this proves
----------------
For each persona: send a chat that creates a brand-new session_id, then poll
`GET /conversations` (up to 5 s) and assert the session_id appears in the
list. This verifies the master orchestrator's first-turn `PutItem` on the
sessions table is actually landing — a regression here means the history
sidebar in the SPA would silently miss the latest chat.

Test ids: `features.conversation-persistence.<persona>` (4 tests).

Cleanup
-------
The created session is DELETEd in teardown regardless of verdict.
"""

from __future__ import annotations

import time
import uuid

import pytest
import requests

from features.classifiers import classify_conversation_persistence
from features.conftest import evidence_path_for
from src.identity.cognito_auth import Persona


_PERSONA_IDS = [p.value for p in Persona]


def _extract_session_ids(payload: object) -> list[str]:
    """Pull session_ids out of a GET /conversations response.

    api_handler.py returns `{"sessions": [{"session_id": ..., ...}, ...]}`.
    We tolerate either `session_id` or `sessionId` keys for forward
    compatibility, and silently skip rows that don't have one.
    """
    if not isinstance(payload, dict):
        return []
    sessions = payload.get("sessions")
    if not isinstance(sessions, list):
        return []
    out: list[str] = []
    for row in sessions:
        if not isinstance(row, dict):
            continue
        sid = row.get("session_id") or row.get("sessionId")
        if isinstance(sid, str) and sid:
            out.append(sid)
    return out


@pytest.mark.parametrize("persona_id", _PERSONA_IDS, ids=_PERSONA_IDS)
def test_conversation_persistence(
    persona_id: str,
    identities: dict,
    chat_function_url: str | None,
    api_base_url: str,
    http_session: requests.Session,
    results_writer,
    cost_tracker_dict: dict,
) -> None:
    """One chat + one list per persona. PASS when the new session_id is found."""
    test_id = f"features.conversation-persistence.{persona_id}"

    if not chat_function_url:
        results_writer.record(
            {
                "test_id": test_id,
                "status": "skipped",
                "layer": "features",
                "target_kind": "api_route",
                "target_id": "get-conversations",
                "skipped_reason": (
                    "CHAT_FUNCTION_URL unset; cannot create a session to verify"
                ),
            }
        )
        pytest.skip("CHAT_FUNCTION_URL unset")

    identity = identities[Persona(persona_id)]
    headers = {"Authorization": f"Bearer {identity.id_token}"}
    session_id = f"features-persist-{persona_id}-{uuid.uuid4().hex[:12]}"
    body = {
        "prompt": "[harness] persistence smoke",
        "session_id": session_id,
    }

    # Step 1: chat creates the DDB row.
    started = time.monotonic()
    chat_status = 0
    try:
        chat_resp = http_session.post(
            f"{chat_function_url}/chat",
            json=body,
            headers=headers,
        )
        chat_status = chat_resp.status_code
    except requests.RequestException:
        chat_status = 0

    # Step 2: poll up to 5 s. The orchestrator's PutItem is synchronous so
    # the row should appear immediately, but we tolerate a small replication
    # window for read-after-write consistency on the GSI.
    deadline = time.monotonic() + 5.0
    list_status = 0
    found_ids: list[str] = []
    while time.monotonic() < deadline:
        try:
            list_resp = http_session.get(
                f"{api_base_url}/conversations",
                headers=headers,
            )
            list_status = list_resp.status_code
            if list_status == 200:
                try:
                    payload = list_resp.json()
                except ValueError:
                    payload = {}
                found_ids = _extract_session_ids(payload)
                if session_id in found_ids:
                    break
        except requests.RequestException:
            list_status = 0
        time.sleep(0.5)

    latency = time.monotonic() - started

    # If the chat itself failed we surface that as the failure instead of a
    # bogus "missing from list" — there was no row to find.
    if chat_status != 200:
        results_writer.record(
            {
                "test_id": test_id,
                "status": "fail",
                "layer": "features",
                "target_kind": "api_route",
                "target_id": "get-conversations",
                "persona": persona_id,
                "duration_seconds": latency,
                "severity": "high",
                "evidence_path": evidence_path_for(test_id),
            }
        )
        pytest.fail(
            f"{test_id}: chat returned HTTP {chat_status} — cannot verify persistence"
        )

    status, severity, reason = classify_conversation_persistence(
        list_status, found_ids, session_id
    )

    row: dict = {
        "test_id": test_id,
        "status": status,
        "layer": "features",
        "target_kind": "api_route",
        "target_id": "get-conversations",
        "persona": persona_id,
        "duration_seconds": latency,
    }
    if severity:
        row["severity"] = severity
    if status == "fail":
        row["evidence_path"] = evidence_path_for(test_id)
    results_writer.record(row)

    cost_tracker_dict["rows"].append(
        {"layer": "features", "test_id": test_id, "usd": 0.0005}
    )

    # Cleanup.
    try:
        http_session.delete(
            f"{api_base_url}/conversations/{session_id}",
            headers=headers,
        )
    except requests.RequestException:
        pass

    if status == "fail":
        pytest.fail(f"{test_id}: {reason}")
