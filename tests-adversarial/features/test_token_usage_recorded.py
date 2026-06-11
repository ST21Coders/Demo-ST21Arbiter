"""Token-usage recording smoke (features layer).

What this proves
----------------
After a /chat turn:
  1. Capture a pre-chat timestamp.
  2. Send a chat as CISO.
  3. Wait 3 s for the async DDB write the agent performs out-of-band.
  4. GET /token-usage (CISO-only) and assert at least one record with
     timestamp > the pre-chat marker exists.

PASS    — at least one new record landed.
FAIL HIGH   — GET /token-usage returned 5xx / transport drop / 403.
FAIL MEDIUM — endpoint OK but no new records appeared within the window.

Test id: `features.token-usage.recorded-after-chat` (1 test).

Cleanup
-------
The created chat session is DELETEd in teardown.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

import pytest
import requests

from features.classifiers import classify_token_usage_recorded
from features.conftest import evidence_path_for
from src.identity.cognito_auth import Persona


def _records_newer_than(payload: object, marker_iso: str) -> int:
    """Count `records` whose `timestamp` is strictly greater than `marker_iso`.

    api_handler.py emits `{"records": [...], "count": N, "filters": ...}`.
    Timestamps are ISO 8601 strings; lexicographic compare is correct for
    fixed-offset ISO timestamps (Z or +00:00). Rows missing a timestamp are
    skipped silently.
    """
    if not isinstance(payload, dict):
        return 0
    records = payload.get("records")
    if not isinstance(records, list):
        return 0
    count = 0
    for r in records:
        if not isinstance(r, dict):
            continue
        ts = r.get("timestamp")
        if not isinstance(ts, str) or not ts:
            continue
        if ts > marker_iso:
            count += 1
    return count


def test_token_usage_recorded_after_chat(
    identities: dict,
    chat_function_url: str | None,
    api_base_url: str,
    http_session: requests.Session,
    results_writer,
    cost_tracker_dict: dict,
) -> None:
    """Send a chat as CISO, then verify a new token-usage row appears."""
    test_id = "features.token-usage.recorded-after-chat"

    if not chat_function_url:
        results_writer.record(
            {
                "test_id": test_id,
                "status": "skipped",
                "layer": "features",
                "target_kind": "api_route",
                "target_id": "get-token-usage",
                "skipped_reason": "CHAT_FUNCTION_URL unset; cannot trigger chat to record",
            }
        )
        pytest.skip("CHAT_FUNCTION_URL unset")

    ciso = identities[Persona.CISO]
    headers = {"Authorization": f"Bearer {ciso.id_token}"}

    # Marker captured BEFORE the chat. The agent writes its row at the end of
    # the turn so any record with timestamp > marker_iso must be new.
    marker_iso = datetime.now(timezone.utc).isoformat()

    session_id = f"features-token-{uuid.uuid4().hex[:12]}"
    body = {"prompt": "[harness] token usage smoke", "session_id": session_id}

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

    if chat_status != 200:
        results_writer.record(
            {
                "test_id": test_id,
                "status": "fail",
                "layer": "features",
                "target_kind": "api_route",
                "target_id": "get-token-usage",
                "duration_seconds": time.monotonic() - started,
                "severity": "high",
                "evidence_path": evidence_path_for(test_id),
            }
        )
        pytest.fail(
            f"{test_id}: chat returned HTTP {chat_status} — cannot verify token usage"
        )

    # Async write window. The agent's record_usage call returns inside the
    # /chat invocation, so 3 s is conservative; we still poll up to 5 s.
    time.sleep(3.0)
    deadline = time.monotonic() + 5.0
    list_status = 0
    new_count = 0
    while time.monotonic() < deadline:
        try:
            list_resp = http_session.get(
                f"{api_base_url}/token-usage",
                headers=headers,
                params={"range": "today"},
            )
            list_status = list_resp.status_code
            if list_status == 200:
                try:
                    payload = list_resp.json()
                except ValueError:
                    payload = {}
                new_count = _records_newer_than(payload, marker_iso)
                if new_count > 0:
                    break
        except requests.RequestException:
            list_status = 0
        time.sleep(0.5)

    latency = time.monotonic() - started
    status, severity, reason = classify_token_usage_recorded(list_status, new_count)

    row: dict = {
        "test_id": test_id,
        "status": status,
        "layer": "features",
        "target_kind": "api_route",
        "target_id": "get-token-usage",
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
