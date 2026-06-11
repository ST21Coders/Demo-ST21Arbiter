"""Fail-closed probes — DOCUMENTED-UNSAFE per AC11.

Verify the API's behaviour when auth metadata is partially malformed.
CLAUDE.md / CLAUDE.local.md document the demo's trust model: the lambda
decodes the JWT payload without verifying the signature, the persona
gates are illustrative, and a malformed / missing Authorization header
resolves to a default identity rather than refusing. All five scenarios
in this module hit that same documented-unsafe surface.

Per AC11 we reclassify the "fail-open" outcome (2xx on a malformed auth
probe) as ``documented_unsafe`` / ``info`` so the row stays in the
inventory and acts as a regression detector, but does NOT crowd the daily
report's failure count. A 401/403 today is the regression direction:
legitimate demo callers (which today rely on the loose decode) would
break.

Scenarios
---------

  1. ``corrupted-jwt-middle-byte``       — real CISO IdToken with one byte
                                            of the payload segment swapped.
  2. ``invalid-json-payload``            — valid base64 segments but the
                                            payload base64-decodes to
                                            "invalid JSON".
  3. ``no-authorization-header``         — protected route without any
                                            Authorization header.
  4. ``empty-authorization-value``       — ``Authorization:`` with empty
                                            value.
  5. ``non-bearer-scheme``               — ``Authorization: Basic …``.

Each probe sends a deliberately-corrupt request to a CISO-only route
(``GET /token-usage``). Classification (AC11 framing):
  * 2xx → documented_unsafe / info — current demo behaviour.
  * 401/403/400 → FAIL severity MEDIUM — regression direction.
  * 5xx → FAIL severity MEDIUM (crash) or transport drop.

Test IDs follow the harness convention: ``fault.fail-closed.<scenario>``.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest
import requests

_LAYER_DIR = Path(__file__).resolve().parent
if str(_LAYER_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_LAYER_DIR.parent))

from fault.classifiers import classify_fail_closed  # noqa: E402
from fault.conftest import evidence_path_for  # noqa: E402

# The route we send the probes at. /token-usage is CISO-only (see
# api_handler.py ::_require_ciso). Choosing a CISO-only route means a
# probe that incorrectly authenticates as anonymous / non-CISO would
# surface as a 200 (the API treated the malformed auth as a valid CISO
# token), which is the fail-open we're hunting for.
_TARGET_ROUTE_PATH = "/token-usage"
_TARGET_ROUTE_ID = "get-token-usage"


def _corrupt_jwt_byte_at(token: str, *, position: int, replacement: str = "X") -> str:
    """Return `token` with the byte at `position` (0-indexed) replaced.

    The IdToken is `<header>.<payload>.<signature>`. We deliberately keep
    the dot positions intact so the API's first-stage split still works —
    we want the corruption to land in the JWT payload bytes, not in the
    wire format. Position 50 lands inside the payload segment for every
    Cognito IdToken (the header is < 50 bytes long).
    """
    if not token:
        return token
    if position >= len(token):
        return token + replacement
    return token[:position] + replacement + token[position + 1 :]


def _replace_payload_segment(token: str, new_payload_b64: str) -> str:
    """Swap the payload segment of a JWT for a new base64-url value.

    Useful for the "valid base64 but invalid JSON inside" scenario — the
    handler needs to reach the JSON decode step before failing, which
    proves it isn't fail-opening on the earlier base64-decode step.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return token
    return f"{parts[0]}.{new_payload_b64}.{parts[2]}"


def _b64url(data: bytes) -> str:
    """Base64-url-safe-encode `data` and strip the trailing padding so the
    result matches Cognito's IdToken segment shape.
    """
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _probe(
    *,
    api_base_url: str,
    auth_header: dict,
    session: requests.Session,
) -> int:
    """Fire a single GET against the target route. Returns the HTTP status
    (0 on transport error)."""
    try:
        resp = session.get(
            f"{api_base_url}{_TARGET_ROUTE_PATH}",
            headers=auth_header,
        )
    except requests.RequestException:
        return 0
    return resp.status_code


def _record_and_assert(
    *,
    test_id: str,
    scenario: str,
    response_status: int,
    results_writer,
) -> None:
    """Classify, record the row, raise on FAIL."""
    verdict, severity, reason = classify_fail_closed(response_status, scenario=scenario)
    row: dict = {
        "test_id": test_id,
        "status": verdict,
        "layer": "fault",
        "target_kind": "api_route",
        "target_id": _TARGET_ROUTE_ID,
    }
    if severity:
        row["severity"] = severity
    if verdict == "fail":
        row["evidence_path"] = evidence_path_for(test_id)
    results_writer.record(row)
    if verdict == "fail":
        pytest.fail(f"{test_id}: {reason} (http_status={response_status})")


# ─────────────────────── corrupted-jwt-middle-byte ───────────────────────────


def test_corrupted_jwt_middle_byte(
    api_base_url: str,
    ciso_id_token: str,
    http_session: requests.Session,
    results_writer,
) -> None:
    """A real CISO IdToken with one byte of the payload swapped to 'X'.

    A correctly-implemented handler attempts to base64-decode the payload
    segment, hits an invalid base64 char (or, if the swap lands in a
    legitimate base64 alphabet position, gets garbled JSON on decode), and
    refuses the request. FAIL HIGH if the API returns 2xx (fail-open).
    """
    test_id = "fault.fail-closed.corrupted-jwt-middle-byte"
    corrupted = _corrupt_jwt_byte_at(ciso_id_token, position=50, replacement="X")
    status = _probe(
        api_base_url=api_base_url,
        auth_header={"Authorization": f"Bearer {corrupted}"},
        session=http_session,
    )
    _record_and_assert(
        test_id=test_id,
        scenario="corrupted-jwt-middle-byte",
        response_status=status,
        results_writer=results_writer,
    )


# ────────────────────────── invalid-json-payload ─────────────────────────────


def test_invalid_json_payload(
    api_base_url: str,
    ciso_id_token: str,
    http_session: requests.Session,
    results_writer,
) -> None:
    """A JWT whose payload segment base64-decodes to "invalid JSON".

    The base64 step succeeds; the JSON-parse step fails. A correct handler
    rejects with 401. FAIL HIGH if the API accepts (it ignored the
    parse error and proceeded).
    """
    test_id = "fault.fail-closed.invalid-json-payload"
    bad_payload = _b64url(b"invalid JSON")
    corrupted = _replace_payload_segment(ciso_id_token, bad_payload)
    status = _probe(
        api_base_url=api_base_url,
        auth_header={"Authorization": f"Bearer {corrupted}"},
        session=http_session,
    )
    _record_and_assert(
        test_id=test_id,
        scenario="invalid-json-payload",
        response_status=status,
        results_writer=results_writer,
    )


# ────────────────────── no-authorization-header ──────────────────────────────


def test_no_authorization_header(
    api_base_url: str,
    http_session: requests.Session,
    results_writer,
) -> None:
    """GET /token-usage without any Authorization header.

    Expected: 401. FAIL HIGH if 2xx — fail-open: missing auth = allow.
    """
    test_id = "fault.fail-closed.no-authorization-header"
    status = _probe(
        api_base_url=api_base_url,
        auth_header={},  # no Authorization header at all
        session=http_session,
    )
    _record_and_assert(
        test_id=test_id,
        scenario="no-authorization-header",
        response_status=status,
        results_writer=results_writer,
    )


# ───────────────────── empty-authorization-value ─────────────────────────────


def test_empty_authorization_value(
    api_base_url: str,
    http_session: requests.Session,
    results_writer,
) -> None:
    """GET /token-usage with an empty Authorization header value.

    Expected: 401. FAIL HIGH if 2xx. This catches the bug where the
    handler checks `header is not None` instead of `header has a Bearer
    prefix`.
    """
    test_id = "fault.fail-closed.empty-authorization-value"
    status = _probe(
        api_base_url=api_base_url,
        auth_header={"Authorization": ""},
        session=http_session,
    )
    _record_and_assert(
        test_id=test_id,
        scenario="empty-authorization-value",
        response_status=status,
        results_writer=results_writer,
    )


# ───────────────────────── non-bearer-scheme ─────────────────────────────────


def test_non_bearer_scheme(
    api_base_url: str,
    http_session: requests.Session,
    results_writer,
) -> None:
    """GET /token-usage with `Authorization: Basic <base64>`.

    Expected: 401. FAIL HIGH if 2xx. This catches the bug where the
    handler reads any Authorization header as proof-of-auth without
    checking the scheme.
    """
    test_id = "fault.fail-closed.non-bearer-scheme"
    fake_basic = _b64url(b"admin:admin")
    status = _probe(
        api_base_url=api_base_url,
        auth_header={"Authorization": f"Basic {fake_basic}"},
        session=http_session,
    )
    _record_and_assert(
        test_id=test_id,
        scenario="non-bearer-scheme",
        response_status=status,
        results_writer=results_writer,
    )
