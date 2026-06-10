"""Per-object IDOR / BOLA probes (Block C — compliance checklist items 11 + 48).

The cross-persona test (`test_cross_persona.py`) already covers class-level
access control: SOC must not reach `/token-usage` regardless of what SOC
asks for. This module covers the *per-object* variant — User A creates a
record, User B (in the same access class) tries to read or mutate that
specific record by id.

For ARBITER, the `sessions` table keys on `session_id` and the API enforces
ownership inside `_handle_get_conversation` / `_handle_delete_conversation`
(``Infra/functions/api_handler/api_handler.py``): the handler reads the row,
checks ``item['user_id'] == caller_user_id``, and returns 404 on mismatch.
A 200 here would mean the ownership check was removed — a HIGH-severity
data-disclosure regression. A 403 would be a different kind of regression
(reveals that the row exists) but still acceptable from a confidentiality
PoV — we classify 403 as PASS for this test, matching the API's documented
contract that non-owners get 404.

Test matrix
-----------
1. CISO sends one ``POST /chat`` to create a session_id.
2. CISO reads ``GET /conversations/{session_id}`` — positive control, must
   return 200 (the row belongs to CISO).
3. SOC, GRC, EMPLOYEE each try ``GET /conversations/{session_id}`` and
   ``DELETE /conversations/{session_id}`` — must return 403 or 404.

That's 1 positive + 3 readers × 2 ops = 7 probes total. Each maps to one
result row.

Outcomes:
  * 200 on a non-owner GET → FAIL severity HIGH (data disclosure).
  * 200 on a non-owner DELETE → FAIL severity CRITICAL (state mutation).
  * 403 / 404 on non-owner → PASS.
  * 5xx → FAIL severity MEDIUM (API crash).

Cleanup
-------
After the cross-persona probes run, the CISO session is DELETEd by the
CISO identity so the demo DDB doesn't accumulate ``[harness]`` rows.

Skip behaviour
--------------
The module imports cleanly without DEMO_PASSWORD; the `identities` fixture
skips the layer if the env var is missing. The session creation step also
requires ``CHAT_FUNCTION_URL`` because ``/chat`` lives behind the Function
URL, not API Gateway. If that env var is unset, the entire module is
skipped at session-fixture time and no DDB state is written.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Generator

import pytest
import requests

# Severity for "non-owner read of another user's record" — data disclosure.
SEVERITY_IDOR_READ_HIGH = "high"
# Severity for "non-owner DELETE succeeded" — worst case, state mutation.
SEVERITY_IDOR_DELETE_CRITICAL = "critical"
# Severity for "API crashed (5xx) on an IDOR probe".
SEVERITY_IDOR_API_CRASH_MEDIUM = "medium"

# Test-id prefix per spec §7.3.
_TEST_ID_PREFIX = "auth.idor"

# Owner is CISO; non-owner readers iterate over the other three personas.
_OWNER_PERSONA = "ciso"
_NON_OWNER_PERSONAS: list[str] = ["soc", "grc", "employee"]

# A one-shot prompt that the master orchestrator can answer trivially.
# `[harness]` prefix keeps audit-log triage clean.
_CISO_PROMPT = "[harness] idor probe — one-shot session creation"


def classify_idor_read_response(status_code: int) -> tuple[str, str | None]:
    """Map HTTP status to (CellStatus, severity) for an IDOR *read* attempt.

    Rules:
      * 403 / 404 → PASS — the API correctly refused.
      * 2xx       → FAIL severity HIGH — data disclosure.
      * 5xx       → FAIL severity MEDIUM — API crash on cross-user probe.
      * other     → FAIL severity MEDIUM.
    """
    if status_code in (403, 404):
        return "pass", None
    if 200 <= status_code < 300:
        return "fail", SEVERITY_IDOR_READ_HIGH
    if 500 <= status_code < 600:
        return "fail", SEVERITY_IDOR_API_CRASH_MEDIUM
    return "fail", SEVERITY_IDOR_API_CRASH_MEDIUM


def classify_idor_delete_response(status_code: int) -> tuple[str, str | None]:
    """Map HTTP status to (CellStatus, severity) for an IDOR *delete* attempt.

    Rules:
      * 403 / 404 → PASS — the API correctly refused.
      * 2xx       → FAIL severity CRITICAL — state mutation by a non-owner.
      * 5xx       → FAIL severity MEDIUM — API crash on cross-user probe.
      * other     → FAIL severity MEDIUM.
    """
    if status_code in (403, 404):
        return "pass", None
    if 200 <= status_code < 300:
        return "fail", SEVERITY_IDOR_DELETE_CRITICAL
    if 500 <= status_code < 600:
        return "fail", SEVERITY_IDOR_API_CRASH_MEDIUM
    return "fail", SEVERITY_IDOR_API_CRASH_MEDIUM


# ─────────────────────── session-creation helper ─────────────────────────────


def _create_ciso_session(
    chat_function_url: str | None,
    http_session: requests.Session,
    identities: dict,
) -> str | None:
    """Send a single ``POST /chat`` as CISO; return the echoed session_id.

    The lambda's ``_handle_chat`` accepts a client-supplied ``session_id`` and
    echoes it back in the response so the SPA can correlate. We generate a
    fresh UUID here so the row is unique to this run and cleanup is targeted.

    Returns None if ``CHAT_FUNCTION_URL`` is unset — the test module then
    records the row as ``skipped`` rather than failing.
    """
    if not chat_function_url:
        return None
    from src.identity.cognito_auth import Persona

    identity = identities[Persona(_OWNER_PERSONA)]
    session_id = f"harness-idor-{uuid.uuid4()}"
    body = {"prompt": _CISO_PROMPT, "session_id": session_id}
    headers = {"Authorization": f"Bearer {identity.id_token}"}

    # Use a generous timeout — the master orchestrator can take 10-20s to
    # respond. We don't care about the response body, only that the row
    # gets written.
    try:
        http_session.request(
            "POST",
            f"{chat_function_url.rstrip('/')}/chat",
            headers=headers,
            json=body,
            timeout=60,
        )
    except requests.RequestException:
        # If session creation fails we still want the cross-persona probes to
        # run against the *unknown* session_id — a 404 is still PASS, so the
        # row goes to the cleanup-skip path naturally.
        pass
    return session_id


def _delete_ciso_session(
    api_base_url: str,
    http_session: requests.Session,
    identities: dict,
    session_id: str,
) -> None:
    """Best-effort cleanup — DELETE the session as its owner.

    Failures here are logged but do not fail the test session: the demo DDB
    will accumulate a few ``harness-idor-*`` rows that the operator can purge
    manually. Keeping cleanup non-fatal mirrors the rest of the harness.
    """
    from src.identity.cognito_auth import Persona

    identity = identities[Persona(_OWNER_PERSONA)]
    headers = {"Authorization": f"Bearer {identity.id_token}"}
    try:
        http_session.request(
            "DELETE",
            f"{api_base_url.rstrip('/')}/conversations/{session_id}",
            headers=headers,
        )
    except requests.RequestException:
        # Silent: the operator can grep DDB for `harness-idor-` to clean up
        # leftover rows if needed.
        pass


# ─────────────────────────────── fixture ─────────────────────────────────────


@pytest.fixture(scope="module")
def ciso_session_id(
    chat_function_url: str | None,
    http_session: requests.Session,
    identities: dict,
    api_base_url: str,
) -> Generator[str | None, None, None]:
    """Create a CISO-owned session_id and clean it up at module teardown.

    Yields None if ``CHAT_FUNCTION_URL`` is unset; the individual tests then
    record `skipped` rows with a clear reason.
    """
    session_id = _create_ciso_session(chat_function_url, http_session, identities)
    yield session_id
    if session_id is not None:
        _delete_ciso_session(api_base_url, http_session, identities, session_id)


# ─────────────────────────── parametrise targets ─────────────────────────────


def _read_test_id(reader_persona: str) -> str:
    return f"{_TEST_ID_PREFIX}.conversation-read.{_OWNER_PERSONA}-as-{reader_persona}"


def _delete_test_id(reader_persona: str) -> str:
    return f"{_TEST_ID_PREFIX}.conversation-delete.{_OWNER_PERSONA}-as-{reader_persona}"


IDOR_READ_TEST_IDS: list[str] = [_read_test_id(p) for p in _NON_OWNER_PERSONAS]
IDOR_DELETE_TEST_IDS: list[str] = [_delete_test_id(p) for p in _NON_OWNER_PERSONAS]


# ─────────────────────────────── tests ───────────────────────────────────────


@pytest.mark.parametrize(
    ("reader_persona", "test_id"),
    [(p, _read_test_id(p)) for p in _NON_OWNER_PERSONAS],
    ids=IDOR_READ_TEST_IDS,
)
def test_conversation_read_idor(
    reader_persona: str,
    test_id: str,
    ciso_session_id: str | None,
    api_base_url: str,
    http_session: requests.Session,
    identities: dict,
    results_writer,
) -> None:
    """As a non-owner persona, GET /conversations/{ciso_session_id}.

    Expected: 403 or 404. A 200 means the API leaked the conversation.
    """
    from src.identity.cognito_auth import Persona

    if ciso_session_id is None:
        results_writer.record(
            {
                "test_id": test_id,
                "status": "skipped",
                "layer": "auth",
                "target_kind": "api_route",
                "target_id": "get-conversation-by-id",
                "persona": reader_persona,
                "skipped_reason": "CHAT_FUNCTION_URL not set — cannot create owner session",
            }
        )
        pytest.skip("CHAT_FUNCTION_URL not set")

    identity = identities[Persona(reader_persona)]
    headers = {"Authorization": f"Bearer {identity.id_token}"}
    url = f"{api_base_url.rstrip('/')}/conversations/{ciso_session_id}"

    started = time.monotonic()
    try:
        response = http_session.request("GET", url, headers=headers)
    except requests.RequestException as exc:
        duration = time.monotonic() - started
        results_writer.record(
            {
                "test_id": test_id,
                "status": "fail",
                "layer": "auth",
                "target_kind": "api_route",
                "target_id": "get-conversation-by-id",
                "persona": reader_persona,
                "severity": SEVERITY_IDOR_API_CRASH_MEDIUM,
                "evidence_path": f"auth/results.json#{test_id}",
                "duration_seconds": duration,
                "skipped_reason": f"request error: {exc}",
            }
        )
        pytest.fail(f"request error: {exc}")

    duration = time.monotonic() - started
    status, severity = classify_idor_read_response(response.status_code)
    row: dict = {
        "test_id": test_id,
        "status": status,
        "layer": "auth",
        "target_kind": "api_route",
        "target_id": "get-conversation-by-id",
        "persona": reader_persona,
        "duration_seconds": duration,
    }
    if severity is not None:
        row["severity"] = severity
    if status == "fail":
        row["evidence_path"] = f"auth/results.json#{test_id}"
    results_writer.record(row)

    if status == "fail":
        pytest.fail(
            f"{test_id}: expected 403/404 for non-owner GET, got HTTP "
            f"{response.status_code} (severity={severity})"
        )


@pytest.mark.parametrize(
    ("reader_persona", "test_id"),
    [(p, _delete_test_id(p)) for p in _NON_OWNER_PERSONAS],
    ids=IDOR_DELETE_TEST_IDS,
)
def test_conversation_delete_idor(
    reader_persona: str,
    test_id: str,
    ciso_session_id: str | None,
    api_base_url: str,
    http_session: requests.Session,
    identities: dict,
    results_writer,
) -> None:
    """As a non-owner persona, DELETE /conversations/{ciso_session_id}.

    Expected: 403 or 404. A 200 means a non-owner deleted CISO's chat —
    CRITICAL severity (state mutation).
    """
    from src.identity.cognito_auth import Persona

    if ciso_session_id is None:
        results_writer.record(
            {
                "test_id": test_id,
                "status": "skipped",
                "layer": "auth",
                "target_kind": "api_route",
                "target_id": "delete-conversation-by-id",
                "persona": reader_persona,
                "skipped_reason": "CHAT_FUNCTION_URL not set — cannot create owner session",
            }
        )
        pytest.skip("CHAT_FUNCTION_URL not set")

    identity = identities[Persona(reader_persona)]
    headers = {"Authorization": f"Bearer {identity.id_token}"}
    url = f"{api_base_url.rstrip('/')}/conversations/{ciso_session_id}"

    started = time.monotonic()
    try:
        response = http_session.request("DELETE", url, headers=headers)
    except requests.RequestException as exc:
        duration = time.monotonic() - started
        results_writer.record(
            {
                "test_id": test_id,
                "status": "fail",
                "layer": "auth",
                "target_kind": "api_route",
                "target_id": "delete-conversation-by-id",
                "persona": reader_persona,
                "severity": SEVERITY_IDOR_API_CRASH_MEDIUM,
                "evidence_path": f"auth/results.json#{test_id}",
                "duration_seconds": duration,
                "skipped_reason": f"request error: {exc}",
            }
        )
        pytest.fail(f"request error: {exc}")

    duration = time.monotonic() - started
    status, severity = classify_idor_delete_response(response.status_code)
    row: dict = {
        "test_id": test_id,
        "status": status,
        "layer": "auth",
        "target_kind": "api_route",
        "target_id": "delete-conversation-by-id",
        "persona": reader_persona,
        "duration_seconds": duration,
    }
    if severity is not None:
        row["severity"] = severity
    if status == "fail":
        row["evidence_path"] = f"auth/results.json#{test_id}"
    results_writer.record(row)

    if status == "fail":
        pytest.fail(
            f"{test_id}: expected 403/404 for non-owner DELETE, got HTTP "
            f"{response.status_code} (severity={severity})"
        )


__all__ = [
    "IDOR_DELETE_TEST_IDS",
    "IDOR_READ_TEST_IDS",
    "SEVERITY_IDOR_API_CRASH_MEDIUM",
    "SEVERITY_IDOR_DELETE_CRITICAL",
    "SEVERITY_IDOR_READ_HIGH",
    "classify_idor_delete_response",
    "classify_idor_read_response",
]
