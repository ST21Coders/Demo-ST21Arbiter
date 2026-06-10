"""Security-event audit-log probes (#67).

Trigger known security events against the deployed API, then read the
``dev-st21arbiter-poc-audit-log`` DynamoDB table and verify a matching
entry appeared. PASS means the event was logged; FAIL HIGH means the
event went unrecorded.

Scenarios
---------

  * ``logging.security-event.forged-token`` — send a forged-groups token
    against ``GET /token-usage``. Whether the request is accepted or
    rejected, an audit entry should fire (suspicious activity).
  * ``logging.security-event.cross-persona`` — SOC token sent to a
    CISO-only route. Should produce an audit entry.
  * ``logging.security-event.legitimate-approve`` — legitimate CISO
    ``POST /actions/{id}/approve``. Should produce a normal audit entry.
  * ``logging.security-event.brute-force`` — 6 rapid failed sign-ins via
    Cognito ``InitiateAuth``. At least one audit-log entry should
    reference the username or source IP.

Each test:
  1. Captures the start epoch.
  2. Triggers the event.
  3. Sleeps 3 s for log propagation.
  4. Scans the audit-log table with a FilterExpression on a 60-second
     window and on a string that should appear (persona username or
     event_type substring).
  5. Classifies with ``classify_security_event_logged``.

If the table has no matching shape (no event_type / actor field at all,
or no rows added in the window), the test FAILs HIGH — silence is the
finding.

Test IDs follow the harness convention: dot-separated lowercase.
"""

from __future__ import annotations

import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import requests

# Local imports (the layer's conftest puts the harness root on sys.path).
_LAYER_DIR = Path(__file__).resolve().parent
if str(_LAYER_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_LAYER_DIR.parent))

from logging_audit.classifiers import classify_security_event_logged  # noqa: E402
from logging_audit.conftest import evidence_path_for  # noqa: E402

# How long we wait between the probe firing and the audit-log scan. The api
# handler writes audit rows synchronously, but DDB propagation + clock skew
# argues for 3 seconds of slack. The brute-force scenario waits longer (5 s)
# because Cognito's throttle response is sometimes deferred.
_PROPAGATION_SLEEP_SECONDS = 3.0
_BRUTE_FORCE_SLEEP_SECONDS = 5.0


# ─────────────────────── shared audit-log helpers ────────────────────────────


def _scan_recent_audit_rows(
    table: Any,
    *,
    start_iso: str,
    contains_any: list[str],
    max_pages: int = 4,
) -> list[dict]:
    """Scan audit-log for rows added after ``start_iso`` containing any of the
    needles in ``contains_any``.

    The audit-log table's primary key is ``event_id (HASH) + timestamp
    (RANGE)`` — no GSI on timestamp alone — so we use Scan with a
    FilterExpression. We cap at ``max_pages`` to keep the probe bounded
    (default 4 ≈ 4 MB scanned, plenty for a recently-active dev table).

    The FilterExpression matches any row whose ``timestamp`` attribute is
    >= start_iso AND whose stringified attribute set contains at least one
    needle. We can't use OR-over-arbitrary-attribute-names in DDB, so we
    do the needle check client-side (one Scan, server-side timestamp
    filter, client-side contains).

    Returns the matching row dicts.
    """
    try:
        from boto3.dynamodb.conditions import Attr
    except ImportError:
        return []

    matches: list[dict] = []
    needles_lower = [n.lower() for n in contains_any if n]
    last_evaluated_key: dict | None = None
    pages = 0
    while pages < max_pages:
        kwargs: dict[str, Any] = {
            "FilterExpression": Attr("timestamp").gte(start_iso),
        }
        if last_evaluated_key:
            kwargs["ExclusiveStartKey"] = last_evaluated_key
        try:
            resp = table.scan(**kwargs)
        except Exception:  # noqa: BLE001 - any AWS error means we have no data
            break
        items = resp.get("Items") or []
        for item in items:
            blob = str(item).lower()
            if not needles_lower or any(n in blob for n in needles_lower):
                matches.append(item)
        last_evaluated_key = resp.get("LastEvaluatedKey")
        if not last_evaluated_key:
            break
        pages += 1
    return matches


def _now_iso() -> str:
    """Current UTC ISO timestamp, second precision. Matches the audit-log
    table's documented ``timestamp`` attribute shape.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _record_and_assert(
    *,
    test_id: str,
    target_id: str,
    matches: list[dict],
    scenario_id: str,
    results_writer,
    extra_context: str,
) -> None:
    """Drop the verdict into the results writer and pytest.fail on FAIL.

    Shared helper because every scenario does the same dance.
    """
    verdict, severity, reason = classify_security_event_logged(
        len(matches), scenario_id=scenario_id
    )
    row: dict = {
        "test_id": test_id,
        "status": verdict,
        "layer": "logging_audit",
        "target_kind": "api_route",
        "target_id": target_id,
    }
    if severity:
        row["severity"] = severity
    if verdict == "fail":
        row["evidence_path"] = evidence_path_for(test_id)
    results_writer.record(row)
    if verdict == "fail":
        pytest.fail(f"{test_id}: {reason} ({extra_context})")


# ────────────────────────── scenario 1: forged token ─────────────────────────


def test_forged_token_attempt_is_audited(
    api_base_url: str,
    ciso_id_token: str,
    soc_auth_header: dict,
    http_session: requests.Session,
    audit_log_table,
    results_writer,
) -> None:
    """Forge a CISO claim on top of a SOC IdToken and hit /token-usage.

    Whether the API accepts (which is itself a high finding handled by
    the auth layer) or rejects, an audit-log entry should fire because
    the JWT signature does not match the claims — that's an explicit
    "suspicious activity" signal.

    The probe forges via the same `forge_cognito_groups` helper Block C
    uses, importing it locally to avoid a top-level import that would
    couple the two layers.
    """
    from auth.test_forged_groups import forge_cognito_groups

    test_id = "logging.security-event.forged-token"
    scenario_id = "forged-token"

    # Build a forged token: take CISO's real IdToken (so the signature is
    # at least syntactically valid) and tamper with the groups claim to
    # something unusual ("admin") that the API definitely doesn't recognise.
    canary_group = f"harness-{uuid.uuid4().hex[:8]}"
    forged = forge_cognito_groups(ciso_id_token, [canary_group])

    start_iso = _now_iso()
    try:
        http_session.get(
            f"{api_base_url}/token-usage",
            headers={"Authorization": f"Bearer {forged}"},
        )
    except requests.RequestException:
        # Network blip is fine — we still check whether an audit entry
        # was attempted for the inbound request.
        pass

    time.sleep(_PROPAGATION_SLEEP_SECONDS)

    matches = _scan_recent_audit_rows(
        audit_log_table,
        start_iso=start_iso,
        # We can't predict the exact event_type the audit handler uses,
        # so we look for any row whose attribute set mentions either the
        # forged group, the path, or one of the conventional security
        # event labels.
        contains_any=[
            canary_group,
            "/token-usage",
            "token-usage",
            "forged",
            "invalid_token",
            "auth_failure",
        ],
    )
    _record_and_assert(
        test_id=test_id,
        target_id="get-token-usage",
        matches=matches,
        scenario_id=scenario_id,
        results_writer=results_writer,
        extra_context=f"start_iso={start_iso}, canary_group={canary_group}",
    )


# ─────────────────────── scenario 2: cross-persona attempt ───────────────────


def test_cross_persona_attempt_is_audited(
    api_base_url: str,
    soc_auth_header: dict,
    identities: dict,
    http_session: requests.Session,
    audit_log_table,
    results_writer,
) -> None:
    """SOC IdToken sent to a CISO-only route should be audited.

    ``GET /token-usage`` is a CISO-only endpoint per
    ``Documents/token_tracking_spec.md``. Sending it a legitimate SOC
    token is the canonical cross-persona attempt — the request should be
    denied (403) AND audited.
    """
    from src.identity.cognito_auth import Persona

    test_id = "logging.security-event.cross-persona"
    scenario_id = "cross-persona"
    soc_username = identities[Persona.SOC].username

    start_iso = _now_iso()
    try:
        http_session.get(
            f"{api_base_url}/token-usage",
            headers=soc_auth_header,
        )
    except requests.RequestException:
        pass

    time.sleep(_PROPAGATION_SLEEP_SECONDS)

    matches = _scan_recent_audit_rows(
        audit_log_table,
        start_iso=start_iso,
        contains_any=[
            soc_username,
            "/token-usage",
            "token-usage",
            "forbidden",
            "access_denied",
            "cross_persona",
        ],
    )
    _record_and_assert(
        test_id=test_id,
        target_id="get-token-usage",
        matches=matches,
        scenario_id=scenario_id,
        results_writer=results_writer,
        extra_context=f"start_iso={start_iso}, soc_username={soc_username}",
    )


# ───────────────────── scenario 3: legitimate CISO approve ───────────────────


def test_legitimate_ciso_approve_is_audited(
    api_base_url: str,
    ciso_auth_header: dict,
    identities: dict,
    http_session: requests.Session,
    audit_log_table,
    results_writer,
) -> None:
    """A legitimate CISO approval is the happy-path audit event.

    The handler at ``_handle_action_transition`` records an audit row on
    every approve / reject; we trigger one and verify the row shows up.
    If the action list is empty we skip — there's nothing to approve.
    """
    from src.identity.cognito_auth import Persona

    test_id = "logging.security-event.legitimate-approve"
    scenario_id = "legitimate-approve"
    ciso_username = identities[Persona.CISO].username

    # Pick the first action we can find. The state-machine layer's probe
    # uses the same helper; we duplicate the body here to keep the layers
    # independent.
    try:
        resp = http_session.get(f"{api_base_url}/actions", headers=ciso_auth_header)
    except requests.RequestException:
        resp = None
    cr_id: str | None = None
    if resp is not None and resp.status_code == 200:
        try:
            for action in resp.json().get("change_requests") or []:
                if isinstance(action, dict):
                    cid = action.get("cr_id") or action.get("id")
                    if cid:
                        cr_id = str(cid)
                        break
        except (ValueError, KeyError):
            cr_id = None
    if not cr_id:
        results_writer.record(
            {
                "test_id": test_id,
                "status": "skipped",
                "layer": "logging_audit",
                "target_kind": "api_route",
                "target_id": "post-action-approve",
                "skipped_reason": "no actions available to approve",
            }
        )
        pytest.skip("no actions to approve")

    start_iso = _now_iso()
    try:
        http_session.post(
            f"{api_base_url}/actions/{cr_id}/approve",
            headers={**ciso_auth_header, "Content-Type": "application/json"},
            json={
                "approver_email": "ciso@harness",
                "approver_role": "ciso",
                "comment": test_id,
            },
        )
    except requests.RequestException:
        pass

    time.sleep(_PROPAGATION_SLEEP_SECONDS)

    matches = _scan_recent_audit_rows(
        audit_log_table,
        start_iso=start_iso,
        contains_any=[
            cr_id,
            ciso_username,
            "approve",
        ],
    )
    # Best-effort reset so the next run sees clean state.
    try:
        http_session.post(
            f"{api_base_url}/actions/{cr_id}/reject",
            headers={**ciso_auth_header, "Content-Type": "application/json"},
            json={"reason": f"reset after {test_id}"},
        )
    except requests.RequestException:
        pass

    _record_and_assert(
        test_id=test_id,
        target_id="post-action-approve",
        matches=matches,
        scenario_id=scenario_id,
        results_writer=results_writer,
        extra_context=f"start_iso={start_iso}, cr_id={cr_id}",
    )


# ──────────────────────── scenario 4: brute-force audit ──────────────────────


def test_brute_force_attempts_are_audited(
    audit_log_table,
    results_writer,
) -> None:
    """6 failed Cognito InitiateAuth calls should leave at least one audit
    trail referencing the synthetic username.

    Why Cognito and not the API: brute-force is detected at the auth-pool
    layer, not the application layer. The api_handler never sees a failed
    sign-in — Cognito's hosted UI does. If the harness has a sidecar that
    relays Cognito audit events into the audit-log table, this probe
    catches it.

    Skips cleanly if COGNITO_USER_POOL_ID / COGNITO_CLIENT_ID env vars
    aren't resolvable — the dependency on `src.identity.cognito_auth`
    surfaces the same skip as the rest of the layer.
    """
    from src.identity.cognito_auth import _require_env

    test_id = "logging.security-event.brute-force"
    scenario_id = "brute-force"

    try:
        import boto3

        pool_id = _require_env("COGNITO_USER_POOL_ID")
        client_id = _require_env("COGNITO_CLIENT_ID")
        client = boto3.client("cognito-idp", region_name="us-east-1")
    except Exception as exc:  # noqa: BLE001
        results_writer.record(
            {
                "test_id": test_id,
                "status": "skipped",
                "layer": "logging_audit",
                "target_kind": "api_route",
                "target_id": "post-cognito-initiate-auth",
                "skipped_reason": f"cognito client unavailable: {exc}",
            }
        )
        pytest.skip(f"cognito client unavailable: {exc}")

    synthetic_username = f"harness-bf-{uuid.uuid4().hex[:8]}@harness.invalid"
    start_iso = _now_iso()

    # Fire 6 failed sign-ins. We expect Cognito to throttle after a few;
    # we don't actually care here — we just want the failed attempts to
    # show up in the audit trail.
    for _ in range(6):
        try:
            client.initiate_auth(
                AuthFlow="USER_PASSWORD_AUTH",
                AuthParameters={
                    "USERNAME": synthetic_username,
                    "PASSWORD": "definitely-not-the-password",
                },
                ClientId=client_id,
            )
        except Exception:  # noqa: BLE001 - failure IS the test
            pass

    time.sleep(_BRUTE_FORCE_SLEEP_SECONDS)

    matches = _scan_recent_audit_rows(
        audit_log_table,
        start_iso=start_iso,
        contains_any=[
            synthetic_username,
            "harness-bf",
            "failed_login",
            "throttle",
            "limit_exceeded",
            "auth_failure",
            "brute_force",
        ],
    )
    _record_and_assert(
        test_id=test_id,
        target_id="post-cognito-initiate-auth",
        matches=matches,
        scenario_id=scenario_id,
        results_writer=results_writer,
        extra_context=(
            f"start_iso={start_iso}, synthetic_username={synthetic_username}, "
            f"pool_id={pool_id}"
        ),
    )
