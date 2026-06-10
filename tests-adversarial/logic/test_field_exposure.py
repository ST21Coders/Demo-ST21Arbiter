"""Excessive-data-exposure probes (#50).

For each persona × each manifest GET route the persona can reach, fire
the request and walk the response JSON for sensitive-field patterns.

Routes that take path params (e.g. `/findings/{conflict_id}`) are skipped
— the harness has no way to mint a valid id without coupling to the data
layer. Their parent collection route (`/findings`) is in the rotation
and produces the same item shape, so a leak in the singular endpoint
would still be visible there.

`/health` is also skipped: unauthenticated, well-known fixed shape with
no sensitive fields.

Test ids: `logic.field-exposure.<persona>.<route-id>`.
"""

from __future__ import annotations

import json

import pytest
import requests

from logic.classifiers import (
    classify_field_exposure,
    walk_json_for_leaks,  # noqa: F401 — re-exported for unit-test discoverability
)
from logic.conftest import api_routes, evidence_path_for

# Routes the field-exposure walker skips. The criteria:
#   * `/health` — no sensitive data possible, no auth required.
#   * any route with a path param — we can't safely synthesize an id.
# The walker still covers the parent collection so the row shape is sampled.
_SKIP_ROUTE_IDS: frozenset[str] = frozenset(
    {
        "get-health",
        "get-finding-by-id",
        "get-scan-run-by-id",
        "get-conversation-by-id",
        "get-conversation-messages",
    }
)


def _eligible_get_routes() -> list[dict]:
    """All manifest routes that are GET, not in the skip set, and have no
    `{path-param}` in the path.

    Path-param detection is a substring check for `{` and `}` so a route
    like `/conversations/{session_id}` is filtered out even if it's not in
    the explicit skip set (defense in depth — new path-param routes get
    skipped automatically).
    """
    out: list[dict] = []
    for route in api_routes():
        if not isinstance(route, dict):
            continue
        if route.get("method") != "GET":
            continue
        route_id = route.get("id") or ""
        if route_id in _SKIP_ROUTE_IDS:
            continue
        path = route.get("path") or ""
        if "{" in path or "}" in path:
            continue
        out.append(route)
    return out


def _persona_can_reach(route: dict, persona_id: str) -> bool:
    """Read the manifest's `accessible_to` list. Defaults to all four
    personas (which matches the manifest's convention)."""
    accessible = route.get("accessible_to") or ["ciso", "soc", "grc", "employee"]
    return persona_id in accessible


def _do_get(
    *,
    api_base_url: str,
    path: str,
    auth_header: dict,
    session: requests.Session,
) -> tuple[int, object | None]:
    """`GET` and return `(status, parsed_body)`. parsed_body is None on
    non-JSON / parse error / transport error.
    """
    try:
        resp = session.get(f"{api_base_url}{path}", headers=auth_header)
    except requests.RequestException:
        return 0, None
    if resp.status_code != 200:
        return resp.status_code, None
    try:
        return resp.status_code, resp.json()
    except (ValueError, json.JSONDecodeError):
        return resp.status_code, None


# ───────────────────────── parametrize cases ─────────────────────────────────


def _persona_route_cases() -> list[tuple[str, dict]]:
    """`(persona_id, route_dict)` for every (persona, route) the persona
    can reach.

    Stable order: personas in their canonical order, routes in manifest
    order. Used to drive `@pytest.mark.parametrize` so each case is its
    own pytest item.
    """
    cases: list[tuple[str, dict]] = []
    for persona_id in ("ciso", "soc", "grc", "employee"):
        for route in _eligible_get_routes():
            if _persona_can_reach(route, persona_id):
                cases.append((persona_id, route))
    return cases


_PERSONA_ROUTE_CASES = _persona_route_cases()


@pytest.mark.parametrize(
    ("persona_id", "route"),
    _PERSONA_ROUTE_CASES,
    ids=[f"{p}-{r.get('id', '?')}" for p, r in _PERSONA_ROUTE_CASES],
)
def test_field_exposure(
    persona_id: str,
    route: dict,
    api_base_url: str,
    persona_auth_headers: dict[str, dict],
    persona_emails: dict[str, str],
    http_session: requests.Session,
    results_writer,
) -> None:
    """One probe per (persona, route).

    Behavior:
      * Fetch the route as the persona.
      * If status is not 200 (e.g. 403 for a persona-restricted route the
        manifest declares reachable but the live API rejects), record SKIP
        with the reason.
      * Walk the response with `classify_field_exposure`, passing the
        caller's groups + email so cross-persona / cross-user leaks are
        detectable.
      * Record one TestResult row.
    """
    route_id = route.get("id") or ""
    test_id = f"logic.field-exposure.{persona_id}.{route_id}"
    target_id = route_id

    auth_header = persona_auth_headers[persona_id]
    caller_email = persona_emails.get(persona_id, "")
    caller_groups = (persona_id,)

    path = route.get("path") or ""
    status, body = _do_get(
        api_base_url=api_base_url,
        path=path,
        auth_header=auth_header,
        session=http_session,
    )

    if status != 200:
        # The route refused the persona (403 / 404 / transport drop) — not
        # a field-exposure signal. Record SKIP with the reason so the row
        # still lands in the coverage matrix.
        reason = f"non-200 response (HTTP {status})" if status else "transport error"
        results_writer.record(
            {
                "test_id": test_id,
                "status": "skipped",
                "layer": "logic",
                "target_kind": "api_route",
                "target_id": target_id,
                "skipped_reason": reason,
            }
        )
        pytest.skip(reason)

    if body is None:
        # 200 with non-JSON body (e.g. CSV export). No fields to walk.
        results_writer.record(
            {
                "test_id": test_id,
                "status": "skipped",
                "layer": "logic",
                "target_kind": "api_route",
                "target_id": target_id,
                "skipped_reason": "200 response was not JSON-parseable",
            }
        )
        pytest.skip("non-JSON response")

    verdict, severity, reason = classify_field_exposure(
        body,
        caller_groups=caller_groups,
        caller_email=caller_email,
    )

    row: dict = {
        "test_id": test_id,
        "status": verdict,
        "layer": "logic",
        "target_kind": "api_route",
        "target_id": target_id,
    }
    if severity:
        row["severity"] = severity
    if verdict == "fail":
        row["evidence_path"] = evidence_path_for(test_id)
    results_writer.record(row)

    if verdict == "fail":
        pytest.fail(f"{test_id}: {reason}")
