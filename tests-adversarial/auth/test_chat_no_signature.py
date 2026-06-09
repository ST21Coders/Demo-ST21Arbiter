"""auth.chat.no-signature — AC11 documented-unsafe regression detector.

The `/chat` Lambda Function URL endpoint is wired with ``AuthType=NONE`` and
the lambda decodes JWT claims **without verifying the signature** (see
``Infra/functions/api_handler/api_handler.py::_caller_claims``). This is the
documented unsafe trust model called out in ``CLAUDE.local.md`` and in the
spec's AC11 — it lets the lambda dodge API Gateway's 29s integration timeout
while still pinning a caller identity.

This test sends a single ``/chat`` request whose Authorization header carries
a JWT with the signature segment stripped (``header.payload.``). Per AC11:

  * **200 OK** → the platform still ignores the signature (current behaviour).
    Recorded as ``status: documented_unsafe`` with ``severity: info``. Does NOT
    fail the run — the builder's summary tally counts ``documented_unsafe``
    rows on their own line per AC11.
  * **401 / 403** → the platform has tightened. Recorded as ``status: fail``
    with ``severity: medium``. This is the regression direction the spec names
    explicitly: legitimate callers (which today send signature-stripped or
    locally-decoded tokens through the same path) would break if the platform
    started rejecting unsigned tokens, so we want the report to surface this
    loudly.
  * **5xx** → the API crashed on a malformed JWT. Recorded as ``status: fail``
    with ``severity: medium``. Different bug class but still worth flagging.
  * **Timeout / network error** → ``status: skipped`` with the error in
    ``skipped_reason``.

The test runs **only as CISO** (one prompt per run — minimal cost). The
``post-chat`` route is universally accessible per the manifest, so we don't
need to enumerate over personas; one persona's signature-stripped token is
enough to exercise the documented contract.

This test is **not parametrised** — there is exactly one test id
``auth.chat.no-signature`` (the canonical AC11 literal).

Skip behaviour
--------------
The Function URL is the AC11 target specifically; if ``CHAT_FUNCTION_URL``
is unset the test records a ``skipped`` row and bails. The test never falls
back to API Gateway (which would not be exercising the documented-unsafe
surface AC11 names).
"""
from __future__ import annotations

import time
import uuid
from typing import Any

import pytest
import requests

# Canonical test id mandated by AC11. Kept as a module-level constant so the
# harness-of-the-harness unit tests can assert against the literal without
# importing the test function.
TEST_ID = "auth.chat.no-signature"

# Manifest target id for the /chat route. Matches the manifest entry at
# `src/coverage/manifest.json::api_routes[id="post-chat"]`.
TARGET_ID = "post-chat"

# Prompt prefix matches CLAUDE.local.md / spec §11 guidance: every harness-
# originated chat prompt is tagged with `[harness]` so the dev team can
# distinguish them in the audit-log table.
PROMPT_TEXT = "[harness] documented-unsafe regression check"


def strip_jwt_signature(token: str) -> str:
    """Remove the signature segment from a JWT.

    A JWT is ``header.payload.signature`` (three base64url segments separated
    by dots). The Lambda's ``_caller_claims`` decodes ``payload`` only and
    never verifies the signature. AC11 specifies the regression probe shape
    as "stripped signature" — concretely, ``header.payload.`` (the trailing
    dot is preserved so the token still has 3 segments and parses).

    Example::

        in:  ``eyJhbGc...header.eyJzdWI...payload.abc123signature``
        out: ``eyJhbGc...header.eyJzdWI...payload.``

    Raises:
        ValueError: when ``token`` does not have three dot-separated segments.
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError(
            f"expected a 3-segment JWT, got {len(parts)} segments"
        )
    # Replace segment 2 (the signature) with the empty string. We rejoin with
    # "." so the result is "header.payload." — the same 3-segment shape the
    # lambda's decode pipeline expects.
    parts[2] = ""
    return ".".join(parts)


def classify_chat_no_signature_response(status_code: int) -> tuple[str, str | None]:
    """Map the /chat response status to (CellStatus.value, severity).

    Rules (AC11):
      * 2xx          → ``documented_unsafe`` / ``info``  (current behaviour).
      * 401 / 403    → ``fail`` / ``medium``             (regression).
      * 5xx          → ``fail`` / ``medium``             (API crash).
      * other 4xx    → ``fail`` / ``medium``             (unexpected).
    """
    if 200 <= status_code < 300:
        return "documented_unsafe", "info"
    if 500 <= status_code < 600:
        return "fail", "medium"
    if status_code in (401, 403):
        return "fail", "medium"
    return "fail", "medium"


def test_chat_no_signature(
    chat_function_url: str | None,
    http_session: requests.Session,
    identities: dict,
    results_writer,
) -> None:
    """Send /chat with a signature-stripped CISO IdToken (AC11)."""
    # Late import so the unit tests can import this module without dragging
    # in the cognito_auth side effects at import time.
    from src.identity.cognito_auth import Persona

    if chat_function_url is None:
        results_writer.record(
            {
                "test_id": TEST_ID,
                "status": "skipped",
                "layer": "auth",
                "target_kind": "api_route",
                "target_id": TARGET_ID,
                "persona": "ciso",
                "skipped_reason": "CHAT_FUNCTION_URL not set",
            }
        )
        pytest.skip("CHAT_FUNCTION_URL not set — AC11 targets the Function URL")

    ciso = identities[Persona.CISO]
    stripped = strip_jwt_signature(ciso.id_token)
    headers = {"Authorization": f"Bearer {stripped}"}
    body: dict[str, Any] = {
        "prompt": PROMPT_TEXT,
        # A random session id so the lambda's per-session DDB write doesn't
        # collide across re-runs. UUID4 keeps the request deterministic in
        # *shape* (length, charset) while not polluting any pre-existing
        # demo session.
        "session_id": str(uuid.uuid4()),
    }

    url = f"{chat_function_url.rstrip('/')}/chat"
    started = time.monotonic()
    try:
        response = http_session.request("POST", url, headers=headers, json=body)
    except requests.RequestException as exc:
        # Timeout / connection error: skipped, not a finding. AC11 cares about
        # the behavior change direction, not about every transient network
        # blip.
        duration = time.monotonic() - started
        results_writer.record(
            {
                "test_id": TEST_ID,
                "status": "skipped",
                "layer": "auth",
                "target_kind": "api_route",
                "target_id": TARGET_ID,
                "persona": "ciso",
                "skipped_reason": f"request error: {exc}",
                "duration_seconds": duration,
            }
        )
        pytest.skip(f"network error reaching /chat: {exc}")

    duration = time.monotonic() - started
    status, severity = classify_chat_no_signature_response(response.status_code)
    row: dict = {
        "test_id": TEST_ID,
        "status": status,
        "layer": "auth",
        "target_kind": "api_route",
        "target_id": TARGET_ID,
        "persona": "ciso",
        "duration_seconds": duration,
    }
    if severity is not None:
        row["severity"] = severity
    # Every FAIL row needs an evidence pointer per AC20. The
    # documented_unsafe row also gets one so a future reader can correlate
    # the row with its results.json entry.
    row["evidence_path"] = f"auth/results.json#{TEST_ID}"
    results_writer.record(row)

    # The whole point of AC11: do NOT hard-fail the run on the documented-
    # unsafe path. Only the regression-direction outcomes (401/403/5xx) flip
    # the test to pytest.fail() so the operator sees a loud signal.
    if status == "fail":
        pytest.fail(
            f"{TEST_ID}: /chat returned HTTP {response.status_code} for a "
            f"signature-stripped token — this is a regression from the "
            f"documented-unsafe contract (severity={severity})."
        )
