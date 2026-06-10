"""CSRF-resistance probe (checklist item #55).

ARBITER's API expects credentials via a Bearer JWT in the `Authorization`
header — there's no cookie-based auth path documented. This probe confirms
the contract: for each POST / DELETE route, fire a request with NO
Authorization header but WITH a fake `Cookie: arbiter.tokens=...` payload,
and expect a 401 / 403.

Failure mode (FAIL HIGH): the API returns 2xx, meaning a cookie-based
fallback exists. Any attacker-controlled cross-site form post could ride
that cookie — classical CSRF.

We don't probe the actual /chat Function URL here because that endpoint is
intentionally `AuthType=NONE` (the Lambda decodes the JWT manually) — sending
a cookie does nothing, but sending neither cookie nor Bearer is the canonical
no-auth case, which is covered by `auth/test_token_replay.py`.
"""

from __future__ import annotations

import re
import time

import pytest
import requests

from headers.classifiers import classify_csrf_cookie_only
from headers.conftest import api_routes, evidence_path_for

# Only fire against routes that actually accept state-changing methods.
_DESTRUCTIVE_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Fake cookie payload — looks like the SPA's token cookie name (cosmetic) but
# carries no real credential. The API should reject it.
_FAKE_COOKIE = {"arbiter.tokens": "fake-token-value-not-a-real-jwt"}


def _route_url(api_base_url: str, path_template: str) -> str:
    """Replace path params with the literal ``probe`` so the request is concrete."""
    return api_base_url + re.sub(r"\{[^}]+\}", "probe", path_template)


@pytest.mark.parametrize(
    "route",
    [r for r in api_routes() if r["method"] in _DESTRUCTIVE_METHODS],
    ids=lambda r: r["id"],
)
def test_destructive_route_rejects_cookie_only(
    route: dict,
    api_base_url: str,
    http_session: requests.Session,
    results_writer,
) -> None:
    """For each state-changing route, fire with a cookie + no Authorization.
    Expected: 401 / 403.
    """
    test_id = f"headers.csrf.cookie-only.{route['id']}"
    url = _route_url(api_base_url, route["path"])
    started = time.monotonic()
    try:
        resp = http_session.request(
            route["method"],
            url,
            cookies=_FAKE_COOKIE,
            json={},
        )
    except requests.RequestException as exc:
        results_writer.record(
            {
                "test_id": test_id,
                "status": "skipped",
                "layer": "headers",
                "target_kind": "api_route",
                "target_id": route["id"],
                "skipped_reason": f"transport error: {type(exc).__name__}",
                "duration_seconds": time.monotonic() - started,
            }
        )
        pytest.skip(f"{test_id}: {exc}")

    duration = time.monotonic() - started
    status, severity, reason = classify_csrf_cookie_only(resp.status_code)

    row: dict = {
        "test_id": test_id,
        "status": status,
        "layer": "headers",
        "target_kind": "api_route",
        "target_id": route["id"],
        "duration_seconds": duration,
    }
    if severity:
        row["severity"] = severity
    if status == "fail":
        row["evidence_path"] = evidence_path_for(test_id)
    results_writer.record(row)

    if status == "fail":
        pytest.fail(f"{test_id}: {reason}")
