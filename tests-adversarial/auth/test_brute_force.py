"""Credential-stuffing / brute-force throttle probes (Block C — #17).

Verifies that Cognito's built-in throttling kicks in within a small number
of consecutive failed sign-in attempts. We do NOT need this to actually
authenticate; we only need to confirm that Cognito starts rate-limiting
before an attacker can iterate through a wordlist.

Method
------
1. Pick a username that is GUARANTEED not to exist in the pool — a UUID-
   suffixed email under the demo domain. Using a non-existent user means
   Cognito returns ``NotAuthorizedException`` (or ``UserNotFoundException``
   on some pool configs) on every attempt, so we get a consistent baseline
   without locking out a real demo user.
2. Send 10 consecutive InitiateAuth calls with a wrong password. The
   harness's 5 RPS throttle is bypassed for this test because the whole
   point is to fire as fast as Cognito will let us — we're characterising
   Cognito's throttling, not the harness's.
3. Record the transition point K — the call index at which Cognito
   stops returning ``NotAuthorizedException`` and starts returning
   ``LimitExceededException`` / ``TooManyRequestsException``.

Outcomes
--------
  * Limit hit within 10 attempts (K in [1, 10]) → PASS. Two test rows
    are emitted:
      * ``auth.brute-force.throttle-kicks-in`` — PASS.
      * ``auth.brute-force.lockout-after-K``    — PASS, with K recorded
        on the row as ``transition_attempt``.
  * No limit observed across 10 attempts → FAIL severity HIGH. Both rows
    are emitted as FAIL — the throttle row signals the missing control
    and the lockout row records K=null.

Skip behaviour
--------------
The module imports cleanly without DEMO_PASSWORD; we don't need the demo
user passwords because we never authenticate. We DO need
``COGNITO_CLIENT_ID`` and AWS credentials so boto3 can hit
``cognito-idp.initiate_auth``; without the client id we skip both rows.

Why not against a real user
---------------------------
Cognito's account-lockout policy will lock a real user after K consecutive
failures. Locking the demo CISO out mid-day is hostile to the team. The
synthetic UUID username is rejected by Cognito at the lookup stage but
still increments the per-IP throttle counter, which is the signal we want.
"""

from __future__ import annotations

import os
import time
import uuid

import boto3
import pytest
from botocore.exceptions import BotoCoreError, ClientError

_AWS_REGION = "us-east-1"
_TEST_ID_PREFIX = "auth.brute-force"

# How many bad-password attempts to fire. Picked at 10 so a working
# Cognito throttle (typical K=5-6) is comfortably within the window, and
# a missing throttle is clear by attempt 10.
_MAX_ATTEMPTS = 10

# Error codes that mean "Cognito throttled us" — the regex captures the
# documented variants. If Cognito ever surfaces a new throttle code, add
# it here.
_THROTTLE_CODES: set[str] = {
    "LimitExceededException",
    "TooManyRequestsException",
    "ThrottlingException",
}

# Error codes that mean "credential was rejected but throttle isn't active
# yet" — the normal pre-throttle state.
_REJECTED_CODES: set[str] = {
    "NotAuthorizedException",
    "UserNotFoundException",
}

SEVERITY_BRUTE_FORCE_HIGH = "high"
SEVERITY_BRUTE_FORCE_MEDIUM = "medium"


def _synthetic_username() -> str:
    """Build a UUID-suffixed email that is guaranteed not to exist."""
    return f"nonexistent_bf_{uuid.uuid4()}@meridianinsurance.com"


def classify_brute_force_attempts(
    error_codes: list[str | None],
) -> tuple[str, str | None, int | None]:
    """Map a list of per-attempt error codes to (status, severity, K).

    K is the 1-based index of the first attempt where Cognito switched
    from a "rejected" code to a "throttle" code. If no throttle was hit,
    K is None.

    Rules:
      * any throttle code in the list → PASS (Cognito did its job).
      * authentication succeeded (None in the list) → FAIL HIGH — the
        non-existent user should never authenticate.
      * neither throttle nor success across all attempts → FAIL HIGH
        (no rate-limit observed).
      * unexpected error codes → FAIL MEDIUM (operator should investigate).
    """
    if any(c is None for c in error_codes):
        return "fail", SEVERITY_BRUTE_FORCE_HIGH, None
    for idx, code in enumerate(error_codes, start=1):
        if code in _THROTTLE_CODES:
            return "pass", None, idx
    # No throttle hit. Were the errors all "rejected" codes? If yes, the
    # throttle is missing; if no, something else is going on.
    if all(c in _REJECTED_CODES for c in error_codes):
        return "fail", SEVERITY_BRUTE_FORCE_HIGH, None
    return "fail", SEVERITY_BRUTE_FORCE_MEDIUM, None


def _module_skip_if_client_id_missing() -> None:
    """Skip the module when COGNITO_CLIENT_ID is unset."""
    if not os.environ.get("COGNITO_CLIENT_ID", "").strip():
        pytest.skip(
            "COGNITO_CLIENT_ID not set — brute-force probe needs the app "
            "client id to call InitiateAuth.",
            allow_module_level=True,
        )


_module_skip_if_client_id_missing()


def _run_attempts() -> list[str | None]:
    """Fire `_MAX_ATTEMPTS` InitiateAuth calls; collect per-attempt error codes.

    Each entry is either ``None`` (Cognito returned tokens — should never
    happen for the synthetic username) or the boto3 error code string.
    """
    client_id = os.environ["COGNITO_CLIENT_ID"].strip()
    username = _synthetic_username()
    bad_password = f"wrong-password-{uuid.uuid4()}"
    client = boto3.client("cognito-idp", region_name=_AWS_REGION)

    codes: list[str | None] = []
    for _ in range(_MAX_ATTEMPTS):
        try:
            client.initiate_auth(
                AuthFlow="USER_PASSWORD_AUTH",
                AuthParameters={"USERNAME": username, "PASSWORD": bad_password},
                ClientId=client_id,
            )
            codes.append(None)
        except ClientError as exc:
            codes.append(
                exc.response.get("Error", {}).get("Code") or "UnknownClientError"
            )
        except BotoCoreError as exc:
            # Transport error — record as a synthetic transport code so the
            # classifier flags it as MEDIUM rather than confusing PASS.
            codes.append(f"TransportError:{type(exc).__name__}")
    return codes


def test_brute_force_throttle_kicks_in(results_writer) -> None:
    """Fire 10 bad-password attempts; assert Cognito throttles within the window.

    Emits TWO result rows:
      * ``auth.brute-force.throttle-kicks-in`` — pass/fail signal.
      * ``auth.brute-force.lockout-after-K``    — same signal plus K.
    """
    started = time.monotonic()
    try:
        codes = _run_attempts()
    except (BotoCoreError, ClientError) as exc:
        duration = time.monotonic() - started
        for suffix in ("throttle-kicks-in", "lockout-after-K"):
            results_writer.record(
                {
                    "test_id": f"{_TEST_ID_PREFIX}.{suffix}",
                    "status": "skipped",
                    "layer": "auth",
                    "target_kind": "api_route",
                    "target_id": "cognito-initiate-auth",
                    "skipped_reason": f"boto3 error: {type(exc).__name__}",
                    "duration_seconds": duration,
                }
            )
        pytest.skip(f"boto3 transport error: {exc}")

    duration = time.monotonic() - started
    status, severity, transition_k = classify_brute_force_attempts(codes)

    base_test_id = f"{_TEST_ID_PREFIX}.throttle-kicks-in"
    base_row: dict = {
        "test_id": base_test_id,
        "status": status,
        "layer": "auth",
        "target_kind": "api_route",
        "target_id": "cognito-initiate-auth",
        "duration_seconds": duration,
    }
    if severity is not None:
        base_row["severity"] = severity
    if status == "fail":
        base_row["evidence_path"] = f"auth/results.json#{base_test_id}"
    results_writer.record(base_row)

    k_test_id = f"{_TEST_ID_PREFIX}.lockout-after-K"
    k_row: dict = {
        "test_id": k_test_id,
        "status": status,
        "layer": "auth",
        "target_kind": "api_route",
        "target_id": "cognito-initiate-auth",
        "duration_seconds": duration,
    }
    if severity is not None:
        k_row["severity"] = severity
    if transition_k is not None:
        k_row["transition_attempt"] = transition_k
    if status == "fail":
        k_row["evidence_path"] = f"auth/results.json#{k_test_id}"
    results_writer.record(k_row)

    if status == "fail":
        pytest.fail(
            f"{base_test_id}: Cognito did not throttle within {_MAX_ATTEMPTS} "
            f"attempts (severity={severity}, codes={codes!r})"
        )


__all__ = [
    "SEVERITY_BRUTE_FORCE_HIGH",
    "SEVERITY_BRUTE_FORCE_MEDIUM",
    "classify_brute_force_attempts",
]
