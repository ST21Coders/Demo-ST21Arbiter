"""Race-condition / TOCTOU probes (#46).

Two probes:

  * `test_concurrent_approve` — fan out 5 concurrent
    `POST /actions/{id}/approve` requests at the same action. The handler
    at `_handle_action_transition` reads the action row, mutates it in
    Python, and PutItems it back without a conditional expression — a
    classic TOCTOU window. The probe asserts exactly one 2xx wins.

  * `test_concurrent_delete_conversation` — CISO creates a session via
    `POST /chat`, then 3 concurrent `DELETE /conversations/{id}` requests
    are fired at the same session_id. Expected: one 200 (the winner),
    two 404 (the row is already gone). Multiple 200s reveal a race in
    the ownership-check → delete path.

Both probes use `concurrent.futures.ThreadPoolExecutor`. The session's
throttle still applies (the lock in `_ThrottledSession` serializes the
wire-time of requests issued from worker threads), but the workers spawn
in parallel and each waits its turn — so the actual server arrival
ordering is closely spaced, which is what we want to expose a race
window.

Test ids:
  * logic.race.concurrent-approve
  * logic.race.concurrent-delete-conversation
"""

from __future__ import annotations

import concurrent.futures
import json
import time
import uuid

import pytest
import requests

from logic.classifiers import classify_concurrent_writes
from logic.conftest import evidence_path_for

# How many concurrent callers to fire at the same resource. 5 is enough to
# expose any obvious race window without overwhelming the dev API.
_APPROVE_FANOUT = 5
_DELETE_FANOUT = 3


def _fetch_one_action_id(
    api_base_url: str, auth_header: dict, session: requests.Session
) -> str | None:
    """Pick the first action's cr_id from `GET /actions`. None on empty / error."""
    try:
        resp = session.get(f"{api_base_url}/actions", headers=auth_header)
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    try:
        body = resp.json()
    except (ValueError, json.JSONDecodeError):
        return None
    for action in body.get("change_requests") or []:
        if not isinstance(action, dict):
            continue
        cr_id = action.get("cr_id") or action.get("id")
        if cr_id:
            return str(cr_id)
    return None


def _fire_approve(
    *,
    url: str,
    auth_header: dict,
) -> int:
    """One concurrent approve. Returns HTTP status, 0 on transport drop.

    Uses a fresh `requests.Session()` per call because:
      (a) the conftest's throttled session would serialize via its lock,
          defeating the purpose of the concurrency test;
      (b) `requests.Session` is documented as not thread-safe for writes,
          so sharing one across worker threads is unsafe regardless.
    """
    try:
        with requests.Session() as sess:
            resp = sess.post(
                url,
                headers={**auth_header, "Content-Type": "application/json"},
                json={
                    "approver_email": "ciso@harness",
                    "approver_role": "ciso",
                    "comment": "logic.race.concurrent-approve",
                },
                timeout=10,
            )
            return resp.status_code
    except requests.RequestException:
        return 0


def _fire_delete(*, url: str, auth_header: dict) -> int:
    """One concurrent DELETE. Returns HTTP status, 0 on transport drop."""
    try:
        with requests.Session() as sess:
            resp = sess.delete(url, headers=auth_header, timeout=10)
            return resp.status_code
    except requests.RequestException:
        return 0


def _post_chat_create_session(
    *,
    chat_function_url: str,
    auth_header: dict,
    session: requests.Session,
) -> str | None:
    """Create a conversation by sending a single `/chat` message.

    Returns the `session_id` the API echoed back. None on error. We mint
    the session_id client-side (matches the SPA's behavior — see
    `_handle_chat` in api_handler.py) so the conversation is guaranteed
    to be ours: any echoed value other than what we sent flags an
    upstream rewrite.
    """
    session_id = f"logic-race-{uuid.uuid4()}"
    try:
        resp = session.post(
            f"{chat_function_url}/chat",
            headers={**auth_header, "Content-Type": "application/json"},
            json={
                "prompt": "ping",
                "session_id": session_id,
                "chat_type": "analyst",
            },
            timeout=30,
        )
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    try:
        echoed = resp.json().get("session_id") or session_id
    except (ValueError, json.JSONDecodeError):
        echoed = session_id
    return str(echoed)


# ────────────────────────── concurrent-approve probe ─────────────────────────


def test_concurrent_approve(
    api_base_url: str,
    ciso_auth_header: dict,
    http_session: requests.Session,
    results_writer,
) -> None:
    """5 concurrent approves on the same action_id.

    Expected: exactly one 2xx + four 4xx (typically 409 from the handler's
    `Caller not an approver` branch once the chain is exhausted, or 403).
    FAIL HIGH if more than one 2xx — the action was approved multiple
    times in a row.
    """
    test_id = "logic.race.concurrent-approve"
    cr_id = _fetch_one_action_id(api_base_url, ciso_auth_header, http_session)
    if not cr_id:
        results_writer.record(
            {
                "test_id": test_id,
                "status": "skipped",
                "layer": "logic",
                "target_kind": "api_route",
                "target_id": "post-action-approve",
                "skipped_reason": "no actions available to race",
            }
        )
        pytest.skip("no actions to race against")

    url = f"{api_base_url}/actions/{cr_id}/approve"

    started = time.monotonic()
    statuses: list[int] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=_APPROVE_FANOUT) as pool:
        futures = [
            pool.submit(_fire_approve, url=url, auth_header=ciso_auth_header)
            for _ in range(_APPROVE_FANOUT)
        ]
        for fut in concurrent.futures.as_completed(futures, timeout=30):
            statuses.append(fut.result())
    elapsed = time.monotonic() - started

    verdict, severity, reason = classify_concurrent_writes(
        statuses, expected_successes=1
    )

    row: dict = {
        "test_id": test_id,
        "status": verdict,
        "layer": "logic",
        "target_kind": "api_route",
        "target_id": "post-action-approve",
        "duration_seconds": elapsed,
    }
    if severity:
        row["severity"] = severity
    if verdict == "fail":
        row["evidence_path"] = evidence_path_for(test_id)
    results_writer.record(row)

    # Best-effort: reject the action so the next run sees clean state.
    try:
        http_session.post(
            f"{api_base_url}/actions/{cr_id}/reject",
            headers={**ciso_auth_header, "Content-Type": "application/json"},
            json={"reason": "harness reset after concurrent-approve probe"},
        )
    except requests.RequestException:
        pass

    if verdict == "fail":
        pytest.fail(f"{test_id}: {reason} (cr_id={cr_id}, statuses={statuses})")


# ─────────────── concurrent-delete-conversation probe ────────────────────────


def test_concurrent_delete_conversation(
    api_base_url: str,
    chat_function_url: str | None,
    ciso_auth_header: dict,
    http_session: requests.Session,
    results_writer,
) -> None:
    """3 concurrent DELETEs on a freshly-created conversation.

    Expected: exactly one 2xx + two 404 (the row is gone after the first
    winner). FAIL HIGH if multiple 2xx are reported — the delete handler's
    ownership check (`_handle_delete_conversation` lines 856-868) doesn't
    use a conditional delete, so the race window is real even if the
    expected verdict is normally clean.
    """
    test_id = "logic.race.concurrent-delete-conversation"

    if not chat_function_url:
        results_writer.record(
            {
                "test_id": test_id,
                "status": "skipped",
                "layer": "logic",
                "target_kind": "api_route",
                "target_id": "delete-conversation-by-id",
                "skipped_reason": "CHAT_FUNCTION_URL unset; cannot mint a conversation",
            }
        )
        pytest.skip("CHAT_FUNCTION_URL unset")

    session_id = _post_chat_create_session(
        chat_function_url=chat_function_url,
        auth_header=ciso_auth_header,
        session=http_session,
    )
    if not session_id:
        results_writer.record(
            {
                "test_id": test_id,
                "status": "skipped",
                "layer": "logic",
                "target_kind": "api_route",
                "target_id": "delete-conversation-by-id",
                "skipped_reason": "could not create conversation via /chat",
            }
        )
        pytest.skip("could not create conversation")

    url = f"{api_base_url}/conversations/{session_id}"

    started = time.monotonic()
    statuses: list[int] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=_DELETE_FANOUT) as pool:
        futures = [
            pool.submit(_fire_delete, url=url, auth_header=ciso_auth_header)
            for _ in range(_DELETE_FANOUT)
        ]
        for fut in concurrent.futures.as_completed(futures, timeout=30):
            statuses.append(fut.result())
    elapsed = time.monotonic() - started

    verdict, severity, reason = classify_concurrent_writes(
        statuses, expected_successes=1
    )

    row: dict = {
        "test_id": test_id,
        "status": verdict,
        "layer": "logic",
        "target_kind": "api_route",
        "target_id": "delete-conversation-by-id",
        "duration_seconds": elapsed,
    }
    if severity:
        row["severity"] = severity
    if verdict == "fail":
        row["evidence_path"] = evidence_path_for(test_id)
    results_writer.record(row)

    if verdict == "fail":
        pytest.fail(
            f"{test_id}: {reason} (session_id={session_id}, statuses={statuses})"
        )
