"""Token-replay probes (task 14).

Three test families per auth-required route, all using the CISO IdToken as
the base identity:

1. `auth.<route-id>.token-from-another-route`
   Does the API accept a token correctly issued for a different OAuth scope?
   Cognito issues single-scope IdTokens per user pool client — replay-by-
   scope-mismatch isn't actually exercisable. Recorded as a `documented_unsafe`
   row with an explanatory `skipped_reason` so the inventory stays complete
   and the renderer can show that this surface was considered, not just
   silently absent.

2. `auth.<route-id>.token-replay-after-logout`
   Cognito IdTokens are stateless JWTs. There is no server-side revocation —
   the token is valid until `exp`. This is a documented unsafe behaviour
   (the same trust model `/chat` relies on per AC11). Recorded as
   `documented_unsafe` so the report carries the row but it doesn't fail the
   run unless the platform's behaviour changes (which would be a regression
   in legitimate callers).

3. `auth.<route-id>.access-token-instead-of-id-token`
   The API decodes the bearer token's middle segment as JSON and reads
   ``cognito:groups`` / ``sub`` from there — both IdTokens and AccessTokens
   are Cognito-signed, share the same ``sub``, and decode to the same
   group claim on this demo's pool. Per AC11 / CLAUDE.md ("Demo-only, not
   production"), accepting an AccessToken in the IdToken slot is the demo's
   documented current behaviour. The row is recorded so the inventory
   tracks the surface and so a future deploy that distinguishes token_use
   flips it from ``documented_unsafe`` to ``fail`` (regression: legitimate
   demo callers that happened to forward the AccessToken would break).

The first two families are deterministic / documentation-only — they don't
hit the network. The third actually sends a request.

Enumeration covers every route with `auth_required: true` in the manifest.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
import requests

from auth.conftest import api_routes
from auth.test_cross_persona import (
    _request_body_for,
    _request_url,
    _strip_route_prefix,
)

_TEST_ID_PREFIX = "auth"


def _auth_required_routes() -> list[dict]:
    """Routes that require an Authorization header per the manifest."""
    return [r for r in api_routes() if r.get("auth_required")]


_REPLAY_ROUTES: list[dict] = _auth_required_routes()
_REPLAY_ROUTE_IDS: list[str] = [
    f"{_TEST_ID_PREFIX}.{_strip_route_prefix(r['id'])}" for r in _REPLAY_ROUTES
]


# ────────────── Family 1: scope-mismatch token (documented N/A) ──────────────


@pytest.mark.parametrize("route", _REPLAY_ROUTES, ids=_REPLAY_ROUTE_IDS)
def test_token_from_another_route(route: dict, results_writer) -> None:
    """Cognito IdTokens are single-scope. Document as not-applicable.

    We don't make an HTTP request because there is no second-scope token to
    send — Cognito issues one IdToken per InitiateAuth response with a
    fixed audience (the SPA's app client id). The row is recorded as
    `documented_unsafe` so the matrix still tracks the surface.
    """
    test_id = (
        f"{_TEST_ID_PREFIX}.{_strip_route_prefix(route['id'])}.token-from-another-route"
    )
    results_writer.record(
        {
            "test_id": test_id,
            "status": "documented_unsafe",
            "layer": "auth",
            "target_kind": "api_route",
            "target_id": route["id"],
            "skipped_reason": (
                "Cognito issues single-scope IdTokens; replay-by-scope-"
                "mismatch is not applicable. Row preserved so the coverage "
                "matrix shows the surface was considered."
            ),
        }
    )
    pytest.skip("Cognito issues single-scope tokens; replay-by-scope-mismatch is N/A")


# ────────── Family 2: post-logout replay (documented unsafe per AC11) ────────


@pytest.mark.parametrize("route", _REPLAY_ROUTES, ids=_REPLAY_ROUTE_IDS)
def test_token_replay_after_logout(route: dict, results_writer) -> None:
    """Cognito IdTokens are stateless JWTs — logout does not revoke them.

    The token stays valid for the full `exp` window regardless of any UI
    sign-out action. This is documented behaviour: changing it would break
    every legitimate caller mid-session, so we record it as
    `documented_unsafe` and do NOT fail the run.

    The harness does not actually call `GlobalSignOut` (would affect the
    demo user's session for other tests in the run, violating idempotency
    per AC21). The row exists to demonstrate the surface was considered.
    """
    test_id = (
        f"{_TEST_ID_PREFIX}.{_strip_route_prefix(route['id'])}."
        f"token-replay-after-logout"
    )
    results_writer.record(
        {
            "test_id": test_id,
            "status": "documented_unsafe",
            "layer": "auth",
            "target_kind": "api_route",
            "target_id": route["id"],
            "skipped_reason": (
                "Cognito IdTokens are stateless; logout does not revoke them. "
                "Documented-unsafe per AC11 / CLAUDE.local.md trust model."
            ),
        }
    )


# ──────── Family 3: AccessToken-instead-of-IdToken (actually probes) ─────────


@pytest.mark.parametrize("route", _REPLAY_ROUTES, ids=_REPLAY_ROUTE_IDS)
def test_access_token_instead_of_id_token(
    route: dict,
    api_base_url: str,
    chat_function_url: str | None,
    http_session: requests.Session,
    identities: dict,
    results_writer,
) -> None:
    """Send the CISO AccessToken in `Authorization` instead of the IdToken.

    AC11 / CLAUDE.md documented-unsafe framing — the demo intentionally
    does NOT distinguish IdToken from AccessToken (both decode to the same
    group claim on this Cognito pool). A 200 is the documented current
    behaviour; a 401/403 means the platform tightened (regression — a
    legitimate demo caller forwarding the AccessToken would now break).

    Classification:
      - 200 on a CISO-only route: documented_unsafe / info — the demo's
        current behaviour. Recorded for regression detection; not a finding.
      - 200 on a non-CISO route: pass (sub is sub; legitimate).
      - 401 / 403: fail severity MEDIUM — regression direction.
      - 5xx: fail severity MEDIUM (API crashed).
    """
    from src.identity.cognito_auth import Persona

    test_id = (
        f"{_TEST_ID_PREFIX}.{_strip_route_prefix(route['id'])}."
        f"access-token-instead-of-id-token"
    )

    ciso = identities[Persona.CISO]
    headers = {"Authorization": f"Bearer {ciso.access_token}"}

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
    status_code = response.status_code
    ciso_only = route.get("accessible_to") == ["ciso"]
    row: dict = {
        "test_id": test_id,
        "status": "pass",
        "layer": "auth",
        "target_kind": "api_route",
        "target_id": route["id"],
        "duration_seconds": duration,
    }
    if status_code in (401, 403):
        # Regression direction: legitimate demo callers forwarding the
        # AccessToken would now break.
        row["status"] = "fail"
        row["severity"] = "medium"
        row["evidence_path"] = f"auth/results.json#{test_id}"
    elif 200 <= status_code < 300:
        if ciso_only:
            # AC11 documented-unsafe: the demo intentionally accepts an
            # AccessToken in the IdToken slot (signature/group claim model
            # collapses them). Record the row so regressions surface, but
            # don't count it as a finding.
            row["status"] = "documented_unsafe"
            row["severity"] = "info"
            row["evidence_path"] = f"auth/results.json#{test_id}"
        # else: non-CISO route + 200 is fine (sub-based lookup may legit-
        # imately use the AccessToken).
    elif 500 <= status_code < 600:
        row["status"] = "fail"
        row["severity"] = "medium"
        row["evidence_path"] = f"auth/results.json#{test_id}"
    else:
        # Unexpected status — surface as a low-severity fail.
        row["status"] = "fail"
        row["severity"] = "low"
        row["evidence_path"] = f"auth/results.json#{test_id}"

    results_writer.record(row)
    if row["status"] == "fail":
        pytest.fail(
            f"{test_id}: HTTP {status_code} for AccessToken (ciso_only="
            f"{ciso_only}, severity={row.get('severity')})"
        )
