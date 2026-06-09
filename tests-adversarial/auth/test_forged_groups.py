"""Forged `cognito:groups` claim probes (task 16).

Threat model
------------
The lambda's ``_caller_claims`` (``Infra/functions/api_handler/api_handler.py``)
decodes the middle segment of the IdToken — header + payload + signature in
``base64url(header).base64url(payload).base64url(signature)`` form — and reads
the ``cognito:groups`` list from the JSON payload. It does **not** verify the
RSA signature against the Cognito JWKS. This is the documented-unsafe trust
model called out in ``CLAUDE.local.md`` and in spec AC11.

A direct consequence: an attacker who already holds a valid lower-privilege
IdToken (say, a SOC user) can take that token, change the ``cognito:groups``
field of the payload from ``["soc"]`` to ``["ciso"]``, re-encode the payload as
base64url, glue the original header and signature back on, and present the new
token to the API. Because the signature is not checked, the API trusts the
forged claim, and a CISO-only gate like ``_require_ciso`` admits the caller.

This is a HIGH-severity probe: if it succeeds, the auth model is broken in a
privilege-escalation sense — any authenticated user can become any persona,
including CISO. The test does not assert what `_require_ciso` *should* do
(that's the spec's job); it asserts the deployed behaviour and emits a row
classified as:

  * 2xx → ``fail`` ``severity:high`` (privilege escalation succeeded).
  * 401 / 403 → ``pass`` (the API correctly rejected the forged token, most
    likely because some other claim re-derivation kicked in or because
    signature verification was retro-fitted in this deploy).
  * 5xx → ``fail`` ``severity:medium`` (API crashed on a forged token — bug,
    but not an escalation).

Enumeration
-----------
For each of the three CISO-only API routes in the manifest::

    get-token-usage          GET  /token-usage
    get-token-usage-summary  GET  /token-usage/summary
    post-action-approve      POST /actions/{cr_id}/approve

we forge upward from every non-CISO persona (soc, grc, employee). That's
3 routes × 3 personas = 9 upward-escalation tests.

We also include one lateral-escalation sanity test:
``auth.token-usage.forged-employee-add-soc-claim`` — employee token with
``cognito:groups`` augmented to include ``soc`` (but NOT ``ciso``) hitting the
CISO-only ``/token-usage`` route. The earlier wiring pointed this test at
``/findings``, which is **universally accessible** per the manifest (all four
personas, including employee, are in ``accessible_to``) — a 2xx response from
``/findings`` is the expected, correct contract behaviour, so the old test
reported a HIGH-severity false-positive on every run. The new wiring picks a
route the API actually gates (``_require_ciso`` in ``api_handler.py``) so a
2xx is a real escalation signal.

Why this shape is load-bearing
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The upward tests forge ``cognito:groups: ["ciso"]`` outright, which probes
"does the API verify the signature at all?". The lateral test forges
``["employee", "soc"]`` (no ``ciso``), which probes a different question:
"does the API check for ``ciso`` specifically, or does it accept any
non-default group as privileged?". A pass on every upward test plus a fail
on the lateral test would mean ``_require_ciso`` was rewritten to
``if not _caller_groups(event): return 403`` — a real, subtle regression
distinct from the upward signature-verification bypass.

Total: **10 tests** (9 upward + 1 lateral).

Why we don't re-sign
--------------------
Real production code would forge a token by ALSO re-signing with a stolen or
guessed RSA private key. We do not, because:

  1. The deployed lambda doesn't verify the signature anyway (documented-unsafe
     per AC11), so re-signing is pointless against this target.
  2. Re-signing would require a key we don't have. Importing a fake RSA key
     just to satisfy a check the server never performs would add noise without
     adding signal.
  3. The harness's job is to characterise the deployed surface as-is. If the
     platform later starts verifying signatures, this test will start
     returning 401 — and the result row will flip from ``fail`` to ``pass``,
     which is exactly the regression direction we want recorded.

Skip behaviour
--------------
Tests skip cleanly when ``DEMO_PASSWORD`` is unset (no real IdToken to forge
from) — the ``identities`` fixture takes care of that at module-scope.
"""
from __future__ import annotations

import base64
import json
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

# ─────────────────────────── severity constants ──────────────────────────────

# Severity for "the forged-groups token reached a 2xx" — direct privilege
# escalation, the worst outcome.
SEVERITY_FORGED_PRIV_ESC_HIGH = "high"
# Severity for "the API crashed on a forged token" — should reject cleanly,
# not panic. Still a bug, but not an escalation.
SEVERITY_FORGED_API_CRASH_MEDIUM = "medium"

# Test-id prefix per spec §7.3.
_TEST_ID_PREFIX = "auth"


# ───────────────────────────── JWT forgery ───────────────────────────────────


def forge_cognito_groups(original_token: str, new_groups: list[str]) -> str:
    """Modify ``cognito:groups`` in the payload; keep header + signature intact.

    Splits the JWT on ``.`` into three segments, base64url-decodes the middle
    segment (with the canonical right-pad), replaces the ``cognito:groups``
    list in the JSON payload with ``new_groups``, re-encodes the payload as
    base64url with the trailing ``=`` padding stripped (the JWT convention),
    and rejoins ``header.<new_payload>.<original_signature>``.

    Real production code would also need to re-sign; we don't, because the API
    doesn't verify signatures (documented unsafe per AC11). Re-attaching the
    original signature unchanged is intentional — it's what an attacker who
    captured a real token off the wire would actually do.

    Args:
        original_token: A 3-segment JWT in canonical ``a.b.c`` form. The
            payload must parse as a JSON object.
        new_groups: The replacement value for ``cognito:groups`` in the
            payload. Use ``["ciso"]`` to escalate to CISO; pass any list to
            test other directions.

    Returns:
        A new 3-segment JWT with the payload's ``cognito:groups`` replaced.
        The header and signature segments are byte-identical to the input.

    Raises:
        ValueError: if ``original_token`` is not a 3-segment dot-separated
            string, or if the payload segment does not decode to a JSON
            object.
    """
    parts = original_token.split(".")
    if len(parts) != 3:
        raise ValueError(
            "forge_cognito_groups expects a 3-segment JWT "
            f"(header.payload.signature); got {len(parts)} segment(s)"
        )
    header_b64, payload_b64, signature_b64 = parts

    # base64url decode payload (padded). The pad is required because Python's
    # base64 module is strict about length-%-4 == 0 even on the urlsafe variant.
    padding = "=" * (-len(payload_b64) % 4)
    payload_bytes = base64.urlsafe_b64decode(payload_b64 + padding)
    payload = json.loads(payload_bytes)
    if not isinstance(payload, dict):
        raise ValueError(
            "forge_cognito_groups expects the payload to decode to a JSON "
            f"object; got {type(payload).__name__}"
        )

    # Forge the claim. Use a copy so we don't mutate any aliased dict.
    payload = dict(payload)
    payload["cognito:groups"] = list(new_groups)

    # Re-encode (strip padding to match JWT convention).
    forged_payload_bytes = json.dumps(payload, separators=(",", ":")).encode()
    forged_payload_b64 = (
        base64.urlsafe_b64encode(forged_payload_bytes).rstrip(b"=").decode("ascii")
    )

    return f"{header_b64}.{forged_payload_b64}.{signature_b64}"


# ─────────────────────────── response classifier ─────────────────────────────


def classify_forged_groups_response(
    status_code: int,
) -> tuple[str, str | None]:
    """Map an HTTP status to a (CellStatus, severity) tuple for forged groups.

    Rules (per the task-16 prompt):
      * 401 / 403: PASS — the API correctly rejected the forged claim.
      * 2xx:        FAIL severity HIGH — privilege escalation succeeded.
      * 5xx:        FAIL severity MEDIUM — API crash on forged token.
      * other:      FAIL severity MEDIUM — unexpected, treat as a crash.
    """
    if status_code in (401, 403):
        return "pass", None
    if 200 <= status_code < 300:
        return "fail", SEVERITY_FORGED_PRIV_ESC_HIGH
    if 500 <= status_code < 600:
        return "fail", SEVERITY_FORGED_API_CRASH_MEDIUM
    return "fail", SEVERITY_FORGED_API_CRASH_MEDIUM


# ─────────────────────────── pair enumeration ────────────────────────────────


# CISO-only API routes per the manifest. Hard-coded by id so the test list
# stays stable across runs and surface drift is caught loudly at collection
# time (rather than silently dropping a test). The drift detector
# (`scripts/check_manifest_drift.py`) is the long-term guard; this guard is
# the short-term one inside the test module itself.
_CISO_ONLY_ROUTE_IDS: list[str] = [
    "get-token-usage",
    "get-token-usage-summary",
    "post-action-approve",
]

# Non-CISO personas the attacker may already have a token for. Order matches
# the manifest's persona declaration order so test ids are deterministic.
_NON_CISO_PERSONAS: list[str] = ["soc", "grc", "employee"]

# The route used by the lateral-escalation sanity test. We target a CISO-only
# route (the only kind the API actually gates by persona today — every other
# route is universally accessible to all 4 personas, so no real "lateral"
# target exists at the API surface). The forge adds ``soc`` to an employee
# token WITHOUT also adding ``ciso`` — so a 2xx means ``_require_ciso`` accepts
# any non-default group as privileged, which is a different bug from the
# upward "signature not verified" path. Reusing ``get-token-usage`` (rather
# than ``get-token-usage-summary`` or ``post-action-approve``) keeps the lateral
# probe non-destructive (GET, no body) and avoids any handler that might
# mutate state before the auth check fires.
_LATERAL_ROUTE_ID = "get-token-usage"


def _resolve_ciso_only_routes() -> list[dict]:
    """Resolve the hard-coded CISO-only route ids to manifest entries.

    Errors loudly on drift — if the manifest no longer carries one of these
    ids (or if its `accessible_to` no longer equals `["ciso"]`), the import
    fails so the operator sees the mismatch immediately.
    """
    by_id = {r["id"]: r for r in api_routes()}
    out: list[dict] = []
    for route_id in _CISO_ONLY_ROUTE_IDS:
        if route_id not in by_id:
            raise RuntimeError(
                f"forged-groups test wired to route id {route_id!r} but the "
                f"manifest no longer has it. Update _CISO_ONLY_ROUTE_IDS or "
                f"the manifest."
            )
        route = by_id[route_id]
        accessible = list(route.get("accessible_to", []))
        if accessible != ["ciso"]:
            raise RuntimeError(
                f"forged-groups test wired to route id {route_id!r} as CISO-only "
                f"but manifest now says accessible_to={accessible!r}. Update "
                f"_CISO_ONLY_ROUTE_IDS or the manifest."
            )
        out.append(route)
    return out


def _resolve_lateral_route() -> dict:
    """Resolve the lateral-escalation sanity route id to its manifest entry.

    Errors loudly on drift — if the manifest no longer carries the lateral
    route id (or if ``accessible_to`` no longer excludes ``employee``), the
    import fails so the operator sees the mismatch immediately. We require
    employee to be gated out because the whole point of the lateral test is
    that a 2xx response is a real escalation finding — pointing this at a
    universally-accessible route is the bug the reviewer caught in C1.
    """
    by_id = {r["id"]: r for r in api_routes()}
    if _LATERAL_ROUTE_ID not in by_id:
        raise RuntimeError(
            f"forged-groups lateral test wired to route id "
            f"{_LATERAL_ROUTE_ID!r} but the manifest no longer has it. "
            f"Update _LATERAL_ROUTE_ID or the manifest."
        )
    route = by_id[_LATERAL_ROUTE_ID]
    accessible = list(route.get("accessible_to", []))
    if "employee" in accessible:
        raise RuntimeError(
            f"forged-groups lateral test wired to route id "
            f"{_LATERAL_ROUTE_ID!r} but the manifest says employee is in "
            f"accessible_to={accessible!r} — a 2xx from a route the employee "
            f"can already access is not an escalation. Pick a route that "
            f"actually gates the employee persona."
        )
    return route


def _build_upward_pairs() -> list[tuple[dict, str, list[str], str, str]]:
    """Build (route, original_persona, forged_groups, forged_persona, test_id).

    Each tuple drives one parametrised test:
      * ``route`` — manifest route entry.
      * ``original_persona`` — whose real IdToken we forge from.
      * ``forged_groups`` — value to write into ``cognito:groups``.
      * ``forged_persona`` — the persona the attacker is trying to impersonate
        (recorded on the row so the report attributes the escalation to the
        impersonated identity, not the attacker's own).
      * ``test_id`` — the canonical test id (spec §7.3).
    """
    pairs: list[tuple[dict, str, list[str], str, str]] = []
    routes = _resolve_ciso_only_routes()
    for route in routes:
        for original_persona in _NON_CISO_PERSONAS:
            test_id = (
                f"{_TEST_ID_PREFIX}.{_strip_route_prefix(route['id'])}."
                f"forged-{original_persona}-to-ciso"
            )
            pairs.append((route, original_persona, ["ciso"], "ciso", test_id))
    return pairs


def _build_lateral_pair() -> tuple[dict, str, list[str], str, str]:
    """Build the single lateral-escalation tuple.

    Employee token, ``cognito:groups`` augmented to include ``soc`` but
    deliberately NOT ``ciso`` (keeping ``employee`` is more realistic — a
    real forger would add a claim, not replace the whole list). The route
    is ``/token-usage``, which the API gates with ``_require_ciso`` —
    a 2xx therefore means the gate accepts ``soc`` as privileged, not just
    ``ciso``. That's a different bug than the upward-escalation tests
    detect, so the lateral pair is genuinely load-bearing.

    The recorded persona is ``soc`` (the impersonated identity), matching
    the upward-escalation pattern.

    Test id follows the strip-prefix convention used by the upward escalation
    ids — the route id ``get-token-usage`` becomes ``token-usage``, so the
    full id is ``auth.token-usage.forged-employee-add-soc-claim``. If you
    change the lateral route, update the matching unit test in
    ``tests/test_auth_abuse_infrastructure.py``
    (``test_forged_groups_lateral_pair_*``) at the same time.
    """
    route = _resolve_lateral_route()
    # Employee + soc but NOT ciso. The forged groups deliberately exclude
    # ``ciso`` so the test probes a different gate-behaviour than the upward
    # escalation ids (which forge straight to ``["ciso"]``).
    forged_groups = ["employee", "soc"]
    test_id = (
        f"{_TEST_ID_PREFIX}.{_strip_route_prefix(route['id'])}."
        f"forged-employee-add-soc-claim"
    )
    return (route, "employee", forged_groups, "soc", test_id)


# Frozen at import time. Used by the parametrize() below and by the harness-
# of-the-harness unit tests in `tests/test_auth_abuse_infrastructure.py`.
FORGED_GROUPS_UPWARD_PAIRS: list[tuple[dict, str, list[str], str, str]] = (
    _build_upward_pairs()
)
FORGED_GROUPS_LATERAL_PAIR: tuple[dict, str, list[str], str, str] = _build_lateral_pair()
FORGED_GROUPS_PAIRS: list[tuple[dict, str, list[str], str, str]] = (
    FORGED_GROUPS_UPWARD_PAIRS + [FORGED_GROUPS_LATERAL_PAIR]
)
FORGED_GROUPS_TEST_IDS: list[str] = [pair[4] for pair in FORGED_GROUPS_PAIRS]


# ─────────────────────────────── the test ────────────────────────────────────


@pytest.mark.parametrize(
    ("route", "original_persona", "forged_groups", "forged_persona", "test_id"),
    FORGED_GROUPS_PAIRS,
    ids=FORGED_GROUPS_TEST_IDS,
)
def test_forged_cognito_groups_claim(
    route: dict,
    original_persona: str,
    forged_groups: list[str],
    forged_persona: str,
    test_id: str,
    api_base_url: str,
    chat_function_url: str | None,
    http_session: requests.Session,
    identities: dict,
    results_writer,
) -> None:
    """Forge ``cognito:groups`` on a real IdToken; hit a gated route.

    PASS criteria (verbatim from the task-16 prompt):
      * 2xx → FAIL severity HIGH — privilege escalation succeeded.
      * 401 / 403 → PASS — the API correctly rejected the forged claim.
      * 5xx → FAIL severity MEDIUM — API crash on forged token.

    The ``persona`` recorded on the row is the FORGED persona (the identity
    the attacker is trying to impersonate), not the original persona whose
    token was the source material. This matches the spec's framing: the
    report attributes the escalation to the impersonated identity so the
    severity ranking reflects what was gained, not who started.
    """
    from src.identity.cognito_auth import Persona

    persona_enum = Persona(original_persona)
    identity = identities[persona_enum]

    forged_token = forge_cognito_groups(identity.id_token, forged_groups)
    headers = {"Authorization": f"Bearer {forged_token}"}

    url = _request_url(route, api_base_url, chat_function_url)
    if url is None:
        # None of the CISO-only routes use /chat today, but the lateral
        # /findings route doesn't either, so this branch is defensive. If
        # CHAT_FUNCTION_URL ever becomes required for a forged-groups route,
        # this skip keeps the run clean.
        results_writer.record(
            {
                "test_id": test_id,
                "status": "skipped",
                "layer": "auth",
                "target_kind": "api_route",
                "target_id": route["id"],
                "persona": forged_persona,
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
                "persona": forged_persona,
                "severity": SEVERITY_FORGED_API_CRASH_MEDIUM,
                "evidence_path": f"auth/results.json#{test_id}",
                "duration_seconds": duration,
                "skipped_reason": f"request error: {exc}",
            }
        )
        pytest.fail(f"request error: {exc}")

    duration = time.monotonic() - started
    status, severity = classify_forged_groups_response(response.status_code)
    row: dict = {
        "test_id": test_id,
        "status": status,
        "layer": "auth",
        "target_kind": "api_route",
        "target_id": route["id"],
        "persona": forged_persona,
        "duration_seconds": duration,
    }
    if severity is not None:
        row["severity"] = severity
    if status == "fail":
        row["evidence_path"] = f"auth/results.json#{test_id}"
    results_writer.record(row)

    if status == "fail":
        pytest.fail(
            f"{test_id}: forged cognito:groups={forged_groups!r} from "
            f"{original_persona!r} expected 401/403, got HTTP "
            f"{response.status_code} (severity={severity})"
        )


__all__ = [
    "FORGED_GROUPS_LATERAL_PAIR",
    "FORGED_GROUPS_PAIRS",
    "FORGED_GROUPS_TEST_IDS",
    "FORGED_GROUPS_UPWARD_PAIRS",
    "SEVERITY_FORGED_API_CRASH_MEDIUM",
    "SEVERITY_FORGED_PRIV_ESC_HIGH",
    "classify_forged_groups_response",
    "forge_cognito_groups",
]
