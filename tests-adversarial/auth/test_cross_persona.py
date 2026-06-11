"""Cross-persona authorization probes — DOCUMENTED-UNSAFE per AC11.

CLAUDE.md frames ARBITER as "Demo-only, not production — single AZ, demo
passwords, MFA off, WAF off." The team intentionally does NOT enforce
per-persona authorization gates at the API boundary; the persona surface
in the SPA is illustrative, not enforced. A SOC IdToken successfully
hitting a CISO-only route is the documented current behaviour.

We still enumerate every (route × blocked-persona) pair so the row exists
in the inventory and acts as a regression detector — but the 2xx outcome
(the demo's current behaviour) is classified as ``documented_unsafe`` /
``info`` per AC11. The regression direction (the platform tightening to
401/403) is the new failure case: legitimate cross-persona traffic in the
demo would start breaking and we want that loud in the daily report.

The canonical id mandated by AC9 is

    auth.token-usage.soc-forbidden

— SOC IdToken hitting `GET /token-usage`. Today it records
``documented_unsafe`` (current behaviour); if the platform ever starts
returning 401/403 on that call, the row flips to ``fail`` / ``medium`` and
the operator sees a real regression signal.

Why enumeration is done at module-import time
---------------------------------------------
pytest's `parametrize()` decorator runs at collection time. We pull the
manifest in `conftest.py::manifest()` (cached) and walk the api_routes in
this module's top-level scope so `pytest --collect-only` shows the full
inventory. This is the same pattern as `pages-per-persona.spec.js` in the
E2E layer and `test_api_routes.py` in the fuzz layer.

Path-param materialisation
--------------------------
Routes like `/findings/{conflict_id}` are sent with a stable placeholder
value (`harness-probe`) substituted for the brace segment. We use a
placeholder rather than a real id because:
  - the test asserts on the auth response, not on resource existence;
  - a 401/403 from the auth guard must fire BEFORE any DDB lookup, so the
    id never needs to be valid;
  - using the same placeholder across runs keeps test ids deterministic
    and the diff-from-last-green block stable.

Destructive-route safety (W2 / W3)
----------------------------------
For mutating methods (POST / PUT / PATCH / DELETE) against placeholder ids,
we rely on the API handler resolving the id BEFORE applying the mutation.
A direct read of ``Infra/functions/api_handler/api_handler.py`` (HEAD as
of this writing) confirms both of the destructive code paths the auth layer
exercises are resolve-then-mutate:

  * ``_handle_action_transition`` (line ~1336) — first calls
    ``crs_table.get_item(Key={"cr_id": cr_id})``; if the row is missing
    it returns ``_err(404, ...)`` before any approval state mutation. So a
    POST to ``/actions/harness-probe/approve`` 404s without side effects.
  * ``_handle_delete_conversation`` (line ~773) — first calls
    ``sessions_table.get_item(Key={"session_id": session_id})``; if the row
    is missing OR does not belong to the caller it returns ``_err(404, ...)``
    before any ``delete_item``. So a DELETE to
    ``/conversations/harness-probe`` 404s without side effects.

If either handler is ever rewritten to mutate-first, the auth layer probes
on placeholder ids would risk real state writes. The single source of
truth is the handler order; if you change that order, also re-evaluate this
docstring and the routes listed in ``_REPRESENTATIVE_ROUTE_IDS`` /
``_CISO_ONLY_ROUTE_IDS``.

The placeholder string ``harness-probe`` is itself a safety signal —
operators triaging incoming traffic on the deployed lambda can grep
CloudWatch for ``harness-probe`` and identify any auth-layer probe that
somehow leaked through (e.g. a real session id collision). Keep the
prefix stable.
"""

from __future__ import annotations

import re
import time
from typing import Any

import pytest
import requests

from auth.conftest import (
    api_routes,
    classify_cross_persona_response,
    persona_ids,
)

# Placeholder used to materialise path-param routes. Distinctive enough that
# `harness-probe` shows up in evidence transcripts.
_PATH_PARAM_PLACEHOLDER = "harness-probe"

# Test-id prefix per spec §7.3. Cross-persona uses the route id (not the
# method+path) so the id stays short and human-readable in summary.md.
_TEST_ID_PREFIX = "auth"


# ─────────────────────────────── enumeration ─────────────────────────────────


def _strip_route_prefix(route_id: str) -> str:
    """Normalise a route id for the test-id segment.

    The manifest uses ids like `get-token-usage` and `post-action-approve`.
    Spec AC9 specifies the canonical id as `auth.token-usage.soc-forbidden`
    — so the leading HTTP verb (`get-` / `post-` / `delete-`) is stripped
    from the route id for the test id.
    """
    prefixes = ("get-", "post-", "put-", "patch-", "delete-")
    for prefix in prefixes:
        if route_id.startswith(prefix):
            return route_id[len(prefix) :]
    return route_id


def _build_cross_persona_pairs() -> list[tuple[dict, str, str]]:
    """Build (route, blocked_persona_id, test_id) triples from the manifest.

    For each route where `accessible_to` is a strict subset of the 4 personas,
    emit one triple per persona in (all_personas - accessible_to).
    """
    pairs: list[tuple[dict, str, str]] = []
    all_personas = set(persona_ids())
    for route in api_routes():
        accessible = set(route.get("accessible_to", []))
        if not accessible or accessible == all_personas:
            # Universally accessible — no blocked persona to enumerate.
            continue
        blocked = all_personas - accessible
        # Iterate in canonical persona order (manifest order) so test ids are
        # stable across runs.
        for persona in persona_ids():
            if persona not in blocked:
                continue
            test_id = (
                f"{_TEST_ID_PREFIX}.{_strip_route_prefix(route['id'])}."
                f"{persona}-forbidden"
            )
            pairs.append((route, persona, test_id))
    return pairs


# Frozen at import time. The test id list is used by both the parametrize()
# below and by the harness-of-the-harness unit tests in
# `tests/test_auth_abuse_infrastructure.py`.
CROSS_PERSONA_PAIRS: list[tuple[dict, str, str]] = _build_cross_persona_pairs()
CROSS_PERSONA_TEST_IDS: list[str] = [pair[2] for pair in CROSS_PERSONA_PAIRS]


# ───────────────────────────── path helpers ──────────────────────────────────


_PATH_PARAM_RE = re.compile(r"\{[^}]+\}")


def materialise_path(path: str, placeholder: str = _PATH_PARAM_PLACEHOLDER) -> str:
    """Substitute every `{...}` segment in a route path with the placeholder.

    `/findings/{conflict_id}` -> `/findings/harness-probe`
    `/conversations/{session_id}/messages` -> `/conversations/harness-probe/messages`
    """
    return _PATH_PARAM_RE.sub(placeholder, path)


def _request_url(
    route: dict, api_base_url: str, chat_function_url: str | None
) -> str | None:
    """Pick the right base URL for a route.

    `/chat` lives behind the Function URL (AuthType=NONE) in production —
    everything else routes through API Gateway. Return None when the route
    is `/chat` and `CHAT_FUNCTION_URL` is unset so the test can skip.
    """
    path = materialise_path(route["path"])
    if route["path"] == "/chat":
        if not chat_function_url:
            return None
        return f"{chat_function_url.rstrip('/')}{path}"
    return f"{api_base_url.rstrip('/')}{path}"


def _request_body_for(method: str) -> Any:
    """Body for methods that require one.

    Empty JSON object — the auth check must fire BEFORE the handler tries to
    parse the body, so the content doesn't matter for the test signal.

    Safety guarantee (W2 / W3)
    --------------------------
    Even though this returns a body that would be VALID for the destructive
    POST/DELETE routes (e.g. ``/actions/{cr_id}/approve``,
    ``/conversations/{session_id}``), no real mutation can land because:

      * the ``cr_id`` / ``session_id`` segment is materialised with the
        ``harness-probe`` placeholder by ``materialise_path``;
      * the api_handler's destructive routes resolve the id (DDB ``get_item``)
        BEFORE applying any mutation, and ``harness-probe`` is not a real id
        so the lookup returns no item and the handler short-circuits with a
        404 — confirmed by direct reading of
        ``Infra/functions/api_handler/api_handler.py``
        (``_handle_action_transition`` and ``_handle_delete_conversation``).

    The module-level docstring expands on the resolve-then-mutate guarantee
    and what to re-check if the handler order ever changes.

    If the handler is ever rewritten to mutate-first, the harness must
    refuse to enumerate auth-abuse tests against routes with ``{cr_id}`` or
    ``{session_id}`` placeholders — see the placeholder-route guard in
    each test module's ``_REPRESENTATIVE_ROUTE_IDS`` / ``_CISO_ONLY_ROUTE_IDS``.
    """
    if method in ("POST", "PUT", "PATCH"):
        return {}
    return None


# ─────────────────────────────── the test ────────────────────────────────────


@pytest.mark.parametrize(
    ("route", "blocked_persona", "test_id"),
    CROSS_PERSONA_PAIRS,
    ids=CROSS_PERSONA_TEST_IDS,
)
def test_cross_persona_forbidden(
    route: dict,
    blocked_persona: str,
    test_id: str,
    api_base_url: str,
    chat_function_url: str | None,
    http_session: requests.Session,
    identities: dict,
    results_writer,
) -> None:
    """Send the route's method + path with a blocked persona's IdToken.

    Classification (AC11 documented-unsafe framing):
      - 2xx:        documented_unsafe / info — the demo's current behaviour;
                    not a finding.
      - 401 / 403: FAIL severity MEDIUM — the platform tightened; legitimate
                    cross-persona traffic in the demo would start breaking.
      - 5xx:        FAIL severity MEDIUM (API crashed on unauthorised probe).
    """
    from src.identity.cognito_auth import Persona

    persona = Persona(blocked_persona)
    identity = identities[persona]
    headers = {"Authorization": f"Bearer {identity.id_token}"}

    url = _request_url(route, api_base_url, chat_function_url)
    if url is None:
        # /chat without CHAT_FUNCTION_URL.
        results_writer.record(
            {
                "test_id": test_id,
                "status": "skipped",
                "layer": "auth",
                "target_kind": "api_route",
                "target_id": route["id"],
                "persona": blocked_persona,
                "skipped_reason": "CHAT_FUNCTION_URL not set",
            }
        )
        pytest.skip("CHAT_FUNCTION_URL not set")

    method = route["method"].upper()
    body = _request_body_for(method)
    started = time.monotonic()
    try:
        if body is not None:
            response = http_session.request(method, url, headers=headers, json=body)
        else:
            response = http_session.request(method, url, headers=headers)
    except requests.RequestException as exc:
        # Network error is neither a pass nor a privilege escalation.
        # Record as fail (medium) so the operator sees it; the harness
        # report ranks alongside crashes.
        duration = time.monotonic() - started
        results_writer.record(
            {
                "test_id": test_id,
                "status": "fail",
                "layer": "auth",
                "target_kind": "api_route",
                "target_id": route["id"],
                "persona": blocked_persona,
                "severity": "medium",
                "evidence_path": f"auth/results.json#{test_id}",
                "duration_seconds": duration,
                "skipped_reason": f"request error: {exc}",
            }
        )
        pytest.fail(f"request error: {exc}")

    duration = time.monotonic() - started
    status, severity = classify_cross_persona_response(response.status_code)
    row: dict = {
        "test_id": test_id,
        "status": status,
        "layer": "auth",
        "target_kind": "api_route",
        "target_id": route["id"],
        "persona": blocked_persona,
        "duration_seconds": duration,
    }
    if severity is not None:
        row["severity"] = severity
    if status == "fail":
        row["evidence_path"] = f"auth/results.json#{test_id}"
    results_writer.record(row)

    if status == "fail":
        pytest.fail(
            f"{test_id}: expected 401/403 for blocked persona, got "
            f"HTTP {response.status_code} (severity={severity})"
        )
