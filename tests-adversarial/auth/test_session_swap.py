"""Session fixation / stale-session probes (Block C — #19).

Two scenarios:

1. ``auth.session-swap.cross-persona-fixation`` — CISO sends ``POST /chat``
   creating session X (a server-side row keyed on ``session_id``). SOC then
   sends ``POST /chat`` with the same ``session_id`` in the body. The
   expected behaviour is for the API to either (a) reject the SOC request
   (403) because SOC isn't the owner, or (b) create a NEW row keyed on
   ``(soc_user_id, session_id)`` so the two persona's transcripts stay
   isolated. A FAIL signal here is "SOC's prompt landed in CISO's
   conversation history" — that's session fixation by definition.

2. ``auth.session-swap.stale-token-still-works`` — re-use a CISO IdToken
   AFTER simulating a logout. We can't actually invoke Cognito's
   GlobalSignOut from the harness without complicating the identity cache
   for every other test in the layer, so the simulation is "clear the
   harness's in-process token cache, request a fresh token, then re-send
   ``POST /chat`` with the OLD token". A 200 with the OLD token confirms
   the documented-unsafe AC11 behaviour (the JWT is still valid until its
   ``exp`` even after a notional logout). This is recorded as
   ``documented_unsafe`` (severity info) — it matches AC11's framing and
   does NOT fail the run.

Why both scenarios live here
----------------------------
The compliance checklist item #19 conflates two distinct attack patterns:
session fixation (attacker forces a known session id on the victim) and
session hijacking (attacker keeps using a captured token after the victim
"logs out"). The harness covers both as a sanity check on the deployed
contract.

Skip behaviour
--------------
Both scenarios require ``CHAT_FUNCTION_URL`` because ``/chat`` lives behind
the Function URL. If unset, both rows record ``skipped`` with a clear
reason and the tests bail.

Cleanup
-------
Both scenarios delete any session_id they create at module teardown.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Generator

import pytest
import requests

SEVERITY_SESSION_FIXATION_HIGH = "high"
SEVERITY_SESSION_API_CRASH_MEDIUM = "medium"

_TEST_ID_PREFIX = "auth.session-swap"

# Canonical test ids.
FIXATION_TEST_ID = f"{_TEST_ID_PREFIX}.cross-persona-fixation"
STALE_TOKEN_TEST_ID = f"{_TEST_ID_PREFIX}.stale-token-still-works"


def _harness_prompt(label: str) -> str:
    return f"[harness] session-swap probe — {label}"


def classify_session_fixation_response(
    soc_status_code: int,
    soc_session_id_in_response: str | None,
    requested_session_id: str,
) -> tuple[str, str | None]:
    """Map (SOC POST /chat status + echoed session_id) to (status, severity).

    Rules:
      * 401 / 403 → PASS — SOC was rejected from CISO's session.
      * 2xx with a DIFFERENT echoed session_id → PASS — the API issued a
        new session for SOC instead of grafting them onto CISO's.
      * 2xx with the SAME echoed session_id → FAIL severity HIGH
        (session fixation: SOC's message landed in CISO's conversation).
      * 5xx → FAIL severity MEDIUM (API crash on cross-persona session
        reuse).
      * other → FAIL severity MEDIUM.
    """
    if soc_status_code in (401, 403):
        return "pass", None
    if 200 <= soc_status_code < 300:
        if (
            soc_session_id_in_response
            and soc_session_id_in_response == requested_session_id
        ):
            return "fail", SEVERITY_SESSION_FIXATION_HIGH
        return "pass", None
    if 500 <= soc_status_code < 600:
        return "fail", SEVERITY_SESSION_API_CRASH_MEDIUM
    return "fail", SEVERITY_SESSION_API_CRASH_MEDIUM


def classify_stale_token_response(status_code: int) -> tuple[str, str | None]:
    """Map an HTTP status to (status, severity) for the stale-token probe.

    Per AC11 the documented-unsafe behaviour is "the JWT is valid until
    its ``exp`` regardless of logout". A 200 here matches AC11 and is
    recorded as ``documented_unsafe`` (info). A 401/403 means the
    platform now invalidates tokens on logout — flag it as the regression
    direction so the team notices.

    Rules:
      * 2xx → DOCUMENTED_UNSAFE severity info — matches AC11.
      * 401 / 403 → FAIL severity MEDIUM — regression direction.
      * 5xx → FAIL severity MEDIUM — API crashed.
      * other → FAIL severity MEDIUM.
    """
    if 200 <= status_code < 300:
        return "documented_unsafe", "info"
    if status_code in (401, 403):
        return "fail", SEVERITY_SESSION_API_CRASH_MEDIUM
    if 500 <= status_code < 600:
        return "fail", SEVERITY_SESSION_API_CRASH_MEDIUM
    return "fail", SEVERITY_SESSION_API_CRASH_MEDIUM


# ─────────────────────────── helpers ─────────────────────────────────────────


def _post_chat(
    http_session: requests.Session,
    chat_function_url: str,
    id_token: str,
    session_id: str,
    prompt: str,
    timeout: int = 60,
) -> requests.Response:
    """Send one ``POST /chat`` and return the response (no .raise_for_status)."""
    return http_session.request(
        "POST",
        f"{chat_function_url.rstrip('/')}/chat",
        headers={"Authorization": f"Bearer {id_token}"},
        json={"prompt": prompt, "session_id": session_id},
        timeout=timeout,
    )


def _extract_echoed_session_id(response: requests.Response) -> str | None:
    """Pull `session_id` from the response body if it's present + JSON."""
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    value = body.get("session_id")
    return str(value) if value else None


def _cleanup_session(
    http_session: requests.Session,
    api_base_url: str,
    id_token: str,
    session_id: str,
) -> None:
    """Best-effort DELETE of a session_id. Silent on error."""
    try:
        http_session.request(
            "DELETE",
            f"{api_base_url.rstrip('/')}/conversations/{session_id}",
            headers={"Authorization": f"Bearer {id_token}"},
        )
    except requests.RequestException:
        pass


# ─────────────────────────── fixtures ────────────────────────────────────────


@pytest.fixture(scope="module")
def created_session_ids() -> Generator[list[str], None, None]:
    """Track session ids created during the module for teardown cleanup."""
    ids: list[str] = []
    yield ids
    # Cleanup happens inside each test via _cleanup_session; this just acts
    # as a sentinel for tests that want to register an id for later GC.


# ─────────────────────────── tests ───────────────────────────────────────────


def test_cross_persona_session_fixation(
    chat_function_url: str | None,
    api_base_url: str,
    http_session: requests.Session,
    identities: dict,
    results_writer,
) -> None:
    """CISO creates session X; SOC POSTs to /chat with the same session_id."""
    from src.identity.cognito_auth import Persona

    if not chat_function_url:
        results_writer.record(
            {
                "test_id": FIXATION_TEST_ID,
                "status": "skipped",
                "layer": "auth",
                "target_kind": "api_route",
                "target_id": "post-chat",
                "persona": "soc",
                "skipped_reason": "CHAT_FUNCTION_URL not set",
            }
        )
        pytest.skip("CHAT_FUNCTION_URL not set")

    ciso = identities[Persona.CISO]
    soc = identities[Persona.SOC]
    session_id = f"harness-fixation-{uuid.uuid4()}"

    started = time.monotonic()
    try:
        # Step 1 — CISO creates the session.
        _post_chat(
            http_session,
            chat_function_url,
            ciso.id_token,
            session_id,
            _harness_prompt("ciso seed"),
        )
        # Step 2 — SOC tries to POST into the SAME session_id.
        soc_response = _post_chat(
            http_session,
            chat_function_url,
            soc.id_token,
            session_id,
            _harness_prompt("soc fixation attempt"),
        )
    except requests.RequestException as exc:
        duration = time.monotonic() - started
        results_writer.record(
            {
                "test_id": FIXATION_TEST_ID,
                "status": "fail",
                "layer": "auth",
                "target_kind": "api_route",
                "target_id": "post-chat",
                "persona": "soc",
                "severity": SEVERITY_SESSION_API_CRASH_MEDIUM,
                "evidence_path": f"auth/results.json#{FIXATION_TEST_ID}",
                "duration_seconds": duration,
                "skipped_reason": f"request error: {exc}",
            }
        )
        # Cleanup the CISO row even on transport failure.
        _cleanup_session(http_session, api_base_url, ciso.id_token, session_id)
        pytest.fail(f"request error: {exc}")

    duration = time.monotonic() - started
    echoed = _extract_echoed_session_id(soc_response)
    status, severity = classify_session_fixation_response(
        soc_response.status_code, echoed, session_id
    )

    row: dict = {
        "test_id": FIXATION_TEST_ID,
        "status": status,
        "layer": "auth",
        "target_kind": "api_route",
        "target_id": "post-chat",
        "persona": "soc",
        "duration_seconds": duration,
    }
    if severity is not None:
        row["severity"] = severity
    if status == "fail":
        row["evidence_path"] = f"auth/results.json#{FIXATION_TEST_ID}"
    results_writer.record(row)

    # Cleanup — delete the CISO session row regardless of outcome.
    _cleanup_session(http_session, api_base_url, ciso.id_token, session_id)
    # If SOC ended up with a different session id, clean that too.
    if echoed and echoed != session_id:
        _cleanup_session(http_session, api_base_url, soc.id_token, echoed)

    if status == "fail":
        pytest.fail(
            f"{FIXATION_TEST_ID}: SOC's POST /chat with CISO's session_id "
            f"returned HTTP {soc_response.status_code} echoed_session_id="
            f"{echoed!r} (severity={severity})"
        )


def test_stale_token_still_works(
    chat_function_url: str | None,
    api_base_url: str,
    http_session: requests.Session,
    identities: dict,
    results_writer,
) -> None:
    """Re-use the CISO IdToken after simulating a logout (cache clear)."""
    from src.identity.cognito_auth import Persona, _clear_cache, fetch_identity

    if not chat_function_url:
        results_writer.record(
            {
                "test_id": STALE_TOKEN_TEST_ID,
                "status": "skipped",
                "layer": "auth",
                "target_kind": "api_route",
                "target_id": "post-chat",
                "persona": "ciso",
                "skipped_reason": "CHAT_FUNCTION_URL not set",
            }
        )
        pytest.skip("CHAT_FUNCTION_URL not set")

    ciso = identities[Persona.CISO]
    old_id_token = ciso.id_token
    session_id = f"harness-stale-{uuid.uuid4()}"

    # Simulate a logout: clear the in-process cache and force a re-fetch
    # of a NEW token. The old token still exists in our local variable and
    # is what we send below.
    _clear_cache()
    try:
        fetch_identity(Persona.CISO)
    except Exception:  # noqa: BLE001 — failure here is recoverable
        # Refetch failed — record as skipped because we can't characterise
        # the contract without the simulation.
        results_writer.record(
            {
                "test_id": STALE_TOKEN_TEST_ID,
                "status": "skipped",
                "layer": "auth",
                "target_kind": "api_route",
                "target_id": "post-chat",
                "persona": "ciso",
                "skipped_reason": "refetch of CISO identity failed (simulation incomplete)",
            }
        )
        pytest.skip("identity refetch failed")

    started = time.monotonic()
    try:
        response = _post_chat(
            http_session,
            chat_function_url,
            old_id_token,
            session_id,
            _harness_prompt("stale token reuse"),
        )
    except requests.RequestException as exc:
        duration = time.monotonic() - started
        results_writer.record(
            {
                "test_id": STALE_TOKEN_TEST_ID,
                "status": "fail",
                "layer": "auth",
                "target_kind": "api_route",
                "target_id": "post-chat",
                "persona": "ciso",
                "severity": SEVERITY_SESSION_API_CRASH_MEDIUM,
                "evidence_path": f"auth/results.json#{STALE_TOKEN_TEST_ID}",
                "duration_seconds": duration,
                "skipped_reason": f"request error: {exc}",
            }
        )
        pytest.fail(f"request error: {exc}")

    duration = time.monotonic() - started
    status, severity = classify_stale_token_response(response.status_code)

    row: dict = {
        "test_id": STALE_TOKEN_TEST_ID,
        "status": status,
        "layer": "auth",
        "target_kind": "api_route",
        "target_id": "post-chat",
        "persona": "ciso",
        "duration_seconds": duration,
    }
    if severity is not None:
        row["severity"] = severity
    if status in ("fail", "documented_unsafe"):
        row["evidence_path"] = f"auth/results.json#{STALE_TOKEN_TEST_ID}"
    results_writer.record(row)

    # Cleanup — re-resolve the current CISO identity (cache was cleared) and
    # delete the session row with whichever token works now.
    try:
        current = fetch_identity(Persona.CISO)
        _cleanup_session(http_session, api_base_url, current.id_token, session_id)
    except Exception:  # noqa: BLE001 — cleanup is best-effort
        pass

    # documented_unsafe is NOT a failure per AC11. Only the regression-
    # direction outcomes (401/403/5xx) raise pytest.fail.
    if status == "fail":
        pytest.fail(
            f"{STALE_TOKEN_TEST_ID}: re-use of pre-logout token returned "
            f"HTTP {response.status_code} (severity={severity})"
        )


__all__ = [
    "FIXATION_TEST_ID",
    "SEVERITY_SESSION_API_CRASH_MEDIUM",
    "SEVERITY_SESSION_FIXATION_HIGH",
    "STALE_TOKEN_TEST_ID",
    "classify_session_fixation_response",
    "classify_stale_token_response",
]
