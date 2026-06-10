"""Expired-token replay probes (task 14 §4).

Build a synthetic JWT whose `exp` is in the past and send it as the bearer
token. The api_handler's `_caller_claims` decodes the payload but does NOT
verify the signature (documented unsafe per AC11). It also does NOT check
`exp` in the current implementation — so this test is a regression detector
for both:

  - the platform tightening (`exp` enforced -> 401/403 -> PASS); and
  - the API silently accepting an expired token forever (200 -> FAIL HIGH).

We pick one representative route per HTTP method (GET, POST, DELETE) from
the manifest. The choice is hard-coded by route id so the test ids stay
stable across runs. If the manifest is reshaped and one of these route ids
disappears, the test errors loudly at collection time rather than silently
skipping.

The expired JWT is built with stdlib base64 + json — same approach as
`cognito_auth.py::_decode_jwt_payload`. The signature segment is the literal
text `fake-signature` base64-encoded — sufficient because the API doesn't
verify it.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
import requests

from auth.conftest import (
    api_routes,
    classify_expired_token_response,
    make_jwt,
)
from auth.test_cross_persona import (
    _request_body_for,
    _request_url,
    _strip_route_prefix,
)

_TEST_ID_PREFIX = "auth"


# ─────────────────────────── representative routes ───────────────────────────


# One representative route per HTTP method (GET, POST, DELETE). Chosen for:
#   - high cardinality of expected callers (every authenticated UI session
#     hits /findings on page load),
#   - non-destructive shape where possible (the POST is a safe re-trigger
#     and the DELETE targets a placeholder id so no real session is hit),
#   - presence in the manifest with auth_required: true.
#
# W3 destructive-route safety: the DELETE on a placeholder session id is
# safe because the handler resolves-first. Direct reading of
# ``Infra/functions/api_handler/api_handler.py::_handle_delete_conversation``
# (HEAD as of this writing) confirms:
#
#   1. ``sessions_table.get_item(Key={"session_id": session_id})`` runs
#      first;
#   2. ``if not item or item.get("user_id") != user_id: return _err(404, ...)``
#      short-circuits before any ``delete_item`` is called.
#
# So a DELETE on ``/conversations/harness-probe`` with an expired token
# either returns 401/403 (the API enforces ``exp``, the regression-direction
# we want flagged) or 404 (the placeholder lookup misses). It cannot delete
# a real session — the session_id never matches a real row. If the handler
# is ever rewritten to mutate-first, remove ``delete-conversation-by-id``
# from this list until the handler reverts. The two-phase destructive-route
# gate in ``fuzz/conftest.py::pytest_collection_modifyitems`` provides a
# second-line defence: by default the DELETE row is skipped with
# ``destructive route (POST/DELETE/PATCH/PUT); pass --include-destructive
# to enable``; opt-in is explicit.
_REPRESENTATIVE_ROUTE_IDS: list[str] = [
    "get-findings",
    "post-scan",
    "delete-conversation-by-id",
]


def _resolve_representatives() -> list[dict]:
    """Resolve route ids to manifest entries. Errors loudly on drift."""
    by_id = {r["id"]: r for r in api_routes()}
    out: list[dict] = []
    for route_id in _REPRESENTATIVE_ROUTE_IDS:
        if route_id not in by_id:
            raise RuntimeError(
                f"expired-token test wired to route id {route_id!r} but the "
                f"manifest no longer has it. Update _REPRESENTATIVE_ROUTE_IDS "
                f"or the manifest."
            )
        out.append(by_id[route_id])
    return out


_EXPIRED_ROUTES: list[dict] = _resolve_representatives()
_EXPIRED_TEST_IDS: list[str] = [
    f"{_TEST_ID_PREFIX}.{_strip_route_prefix(r['id'])}.expired-token"
    for r in _EXPIRED_ROUTES
]


# ─────────────────────────────── helpers ─────────────────────────────────────


def build_expired_jwt(sub: str = "expired-token-probe") -> str:
    """Build a JWT with `exp` 1 hour in the past.

    Public helper so the harness-of-the-harness tests can assert the shape
    without re-implementing the logic. `sub` is parametrisable so a future
    test can pin it to a specific demo user if needed.
    """
    one_hour_ago = int(time.time()) - 3600
    return make_jwt(
        sub=sub,
        groups=["ciso"],  # claim CISO so the API can't reject on groups alone
        exp=one_hour_ago,
        extra={"iss": "harness-synthetic", "aud": "harness-test-client"},
    )


# ─────────────────────────────── the test ────────────────────────────────────


@pytest.mark.parametrize(
    ("route", "test_id"),
    list(zip(_EXPIRED_ROUTES, _EXPIRED_TEST_IDS)),
    ids=_EXPIRED_TEST_IDS,
)
def test_expired_token_replay(
    route: dict,
    test_id: str,
    api_base_url: str,
    chat_function_url: str | None,
    http_session: requests.Session,
    results_writer,
) -> None:
    """Replay a synthetic JWT with `exp` in the past against `route`.

    PASS criteria:
      - 401 / 403: pass — API checks `exp` (good).
      - 200:        fail severity HIGH — API silently accepts expired token.
      - 5xx:        fail severity MEDIUM — API crashed on expired token.
    """
    expired_jwt = build_expired_jwt()
    headers = {"Authorization": f"Bearer {expired_jwt}"}

    url = _request_url(route, api_base_url, chat_function_url)
    if url is None:
        results_writer.record(
            {
                "test_id": test_id,
                "status": "skipped",
                "layer": "auth",
                "target_kind": "api_route",
                "target_id": route["id"],
                "skipped_reason": "CHAT_FUNCTION_URL not set",
            }
        )
        pytest.skip("CHAT_FUNCTION_URL not set")

    method = route["method"].upper()
    body: Any = _request_body_for(method)
    started = time.monotonic()
    try:
        if body is not None:
            response = http_session.request(method, url, headers=headers, json=body)
        else:
            response = http_session.request(method, url, headers=headers)
    except requests.RequestException as exc:
        duration = time.monotonic() - started
        results_writer.record(
            {
                "test_id": test_id,
                "status": "fail",
                "layer": "auth",
                "target_kind": "api_route",
                "target_id": route["id"],
                "severity": "medium",
                "evidence_path": f"auth/results.json#{test_id}",
                "duration_seconds": duration,
                "skipped_reason": f"request error: {exc}",
            }
        )
        pytest.fail(f"request error: {exc}")

    duration = time.monotonic() - started
    status, severity = classify_expired_token_response(response.status_code)
    row: dict = {
        "test_id": test_id,
        "status": status,
        "layer": "auth",
        "target_kind": "api_route",
        "target_id": route["id"],
        "duration_seconds": duration,
    }
    if severity is not None:
        row["severity"] = severity
    if status == "fail":
        row["evidence_path"] = f"auth/results.json#{test_id}"
    results_writer.record(row)

    # NB: per the task-14 prompt, the test should NOT hard-fail the run if
    # the API doesn't check `exp`. The behaviour is documented-unsafe and
    # the report ranks the row by severity, NOT by hard-fail. We still call
    # pytest.fail() for 5xx (API crash) so the operator sees it loudly —
    # but a clean 200 (silent acceptance) emits the row and returns
    # without failing the run.
    if status == "fail" and severity == "medium":
        pytest.fail(
            f"{test_id}: expected 401/403 for expired token, got HTTP "
            f"{response.status_code} (severity={severity})"
        )
