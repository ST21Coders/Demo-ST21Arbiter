"""Cognito ForgotPassword probes (Block C — #21).

Two probes against the ``cognito-idp:forgot_password`` API:

1. ``auth.password-reset.enumeration`` — Send one ForgotPassword for a
   known username (``ciso_diana@meridianinsurance.com``) and one for an
   unknown username (UUID-suffixed). If Cognito returns DIFFERENT errors
   for the two calls (e.g. ``UserNotFoundException`` vs.
   ``CodeDeliveryDetails``), an attacker can use the API to enumerate
   valid usernames.

   * Same outcome (both succeed or both rejected with the same code) →
     PASS — no enumeration possible.
   * Different outcomes → FAIL severity MEDIUM.

2. ``auth.password-reset.rate-limit`` — Send 5 rapid ForgotPassword calls
   against a UUID-suffixed (non-existent) username. Cognito should
   rate-limit after a small K. We probe a NON-EXISTENT user specifically
   so we don't repeatedly trigger SMS / email delivery to demo users.

   * Limit hit within 5 attempts → PASS.
   * No limit observed → FAIL severity MEDIUM (account-targeted abuse
     vector against any valid user).

Why MEDIUM, not HIGH
--------------------
Enumeration and rate-limit gaps are real exposures but they don't
directly disclose data or escalate privilege — they enable downstream
attacks. The CVSS-equivalent is MEDIUM in both cases.

Skip behaviour
--------------
Both probes need ``COGNITO_CLIENT_ID``. Without it the module skips.
``DEMO_PASSWORD`` is not required — we don't authenticate, only call
ForgotPassword.

Safety
------
The enumeration probe sends a SINGLE ForgotPassword for the known user
(CISO). Cognito will deliver one reset code to the demo CISO's email
address. That's an acceptable cost for the test (one email per run);
the operator is expected to either ignore it or use a dev-only email.
"""

from __future__ import annotations

import os
import time
import uuid

import boto3
import pytest
from botocore.exceptions import BotoCoreError, ClientError

_AWS_REGION = "us-east-1"
_TEST_ID_PREFIX = "auth.password-reset"

ENUMERATION_TEST_ID = f"{_TEST_ID_PREFIX}.enumeration"
RATE_LIMIT_TEST_ID = f"{_TEST_ID_PREFIX}.rate-limit"

SEVERITY_PASSWORD_RESET_MEDIUM = "medium"

# Known username (demo CISO). Must match the cognito_auth module.
_KNOWN_USERNAME = "ciso_diana@meridianinsurance.com"

# How many rapid ForgotPassword calls to fire for the rate-limit probe.
_MAX_RATE_LIMIT_ATTEMPTS = 5

_THROTTLE_CODES: set[str] = {
    "LimitExceededException",
    "TooManyRequestsException",
    "ThrottlingException",
}


def _module_skip_if_client_id_missing() -> None:
    if not os.environ.get("COGNITO_CLIENT_ID", "").strip():
        pytest.skip(
            "COGNITO_CLIENT_ID not set — password-reset probes need the app "
            "client id to call ForgotPassword.",
            allow_module_level=True,
        )


_module_skip_if_client_id_missing()


def _synthetic_username() -> str:
    return f"nonexistent_pr_{uuid.uuid4()}@meridianinsurance.com"


def classify_enumeration_responses(
    known_code: str | None, unknown_code: str | None
) -> tuple[str, str | None]:
    """Map (known_outcome, unknown_outcome) to (CellStatus, severity).

    Both ``None`` and both same code → PASS (no enumeration distinguish).
    Different outcomes → FAIL severity MEDIUM.

    Codes considered "same outcome from an enumeration PoV":
      * both None (both succeeded — Cognito sends to both; no leak).
      * both equal error code.
    """
    if known_code == unknown_code:
        return "pass", None
    return "fail", SEVERITY_PASSWORD_RESET_MEDIUM


def classify_rate_limit_attempts(
    codes: list[str | None],
) -> tuple[str, str | None, int | None]:
    """Map a list of per-attempt error codes to (status, severity, K).

    K is the 1-based index of the first throttle hit; None if none.
    """
    for idx, code in enumerate(codes, start=1):
        if code in _THROTTLE_CODES:
            return "pass", None, idx
    return "fail", SEVERITY_PASSWORD_RESET_MEDIUM, None


# ─────────────────────────── helpers ─────────────────────────────────────────


def _forgot_password_once(client, client_id: str, username: str) -> str | None:
    """Call forgot_password once; return None on success or the boto3 code."""
    try:
        client.forgot_password(ClientId=client_id, Username=username)
        return None
    except ClientError as exc:
        return exc.response.get("Error", {}).get("Code") or "UnknownClientError"


# ─────────────────────────── tests ───────────────────────────────────────────


def test_forgot_password_enumeration(results_writer) -> None:
    """One ForgotPassword for a known user + one for an unknown user."""
    client_id = os.environ["COGNITO_CLIENT_ID"].strip()
    client = boto3.client("cognito-idp", region_name=_AWS_REGION)
    started = time.monotonic()
    try:
        known_code = _forgot_password_once(client, client_id, _KNOWN_USERNAME)
        unknown_code = _forgot_password_once(client, client_id, _synthetic_username())
    except BotoCoreError as exc:
        duration = time.monotonic() - started
        results_writer.record(
            {
                "test_id": ENUMERATION_TEST_ID,
                "status": "skipped",
                "layer": "auth",
                "target_kind": "api_route",
                "target_id": "cognito-forgot-password",
                "skipped_reason": f"boto3 transport error: {type(exc).__name__}",
                "duration_seconds": duration,
            }
        )
        pytest.skip(f"boto3 transport error: {exc}")

    duration = time.monotonic() - started
    status, severity = classify_enumeration_responses(known_code, unknown_code)

    row: dict = {
        "test_id": ENUMERATION_TEST_ID,
        "status": status,
        "layer": "auth",
        "target_kind": "api_route",
        "target_id": "cognito-forgot-password",
        "duration_seconds": duration,
    }
    if severity is not None:
        row["severity"] = severity
    if status == "fail":
        row["evidence_path"] = f"auth/results.json#{ENUMERATION_TEST_ID}"
    results_writer.record(row)

    if status == "fail":
        pytest.fail(
            f"{ENUMERATION_TEST_ID}: ForgotPassword returned different outcomes "
            f"for known ({known_code!r}) vs unknown ({unknown_code!r}) usernames "
            f"— attacker can enumerate (severity={severity})"
        )


def test_forgot_password_rate_limit(results_writer) -> None:
    """5 rapid ForgotPassword calls; assert Cognito throttles within window."""
    client_id = os.environ["COGNITO_CLIENT_ID"].strip()
    username = _synthetic_username()
    client = boto3.client("cognito-idp", region_name=_AWS_REGION)
    started = time.monotonic()
    codes: list[str | None] = []
    try:
        for _ in range(_MAX_RATE_LIMIT_ATTEMPTS):
            codes.append(_forgot_password_once(client, client_id, username))
    except BotoCoreError as exc:
        duration = time.monotonic() - started
        results_writer.record(
            {
                "test_id": RATE_LIMIT_TEST_ID,
                "status": "skipped",
                "layer": "auth",
                "target_kind": "api_route",
                "target_id": "cognito-forgot-password",
                "skipped_reason": f"boto3 transport error: {type(exc).__name__}",
                "duration_seconds": duration,
            }
        )
        pytest.skip(f"boto3 transport error: {exc}")

    duration = time.monotonic() - started
    status, severity, transition_k = classify_rate_limit_attempts(codes)

    row: dict = {
        "test_id": RATE_LIMIT_TEST_ID,
        "status": status,
        "layer": "auth",
        "target_kind": "api_route",
        "target_id": "cognito-forgot-password",
        "duration_seconds": duration,
    }
    if severity is not None:
        row["severity"] = severity
    if transition_k is not None:
        row["transition_attempt"] = transition_k
    if status == "fail":
        row["evidence_path"] = f"auth/results.json#{RATE_LIMIT_TEST_ID}"
    results_writer.record(row)

    if status == "fail":
        pytest.fail(
            f"{RATE_LIMIT_TEST_ID}: Cognito did not throttle ForgotPassword "
            f"within {_MAX_RATE_LIMIT_ATTEMPTS} calls (severity={severity}, "
            f"codes={codes!r})"
        )


__all__ = [
    "ENUMERATION_TEST_ID",
    "RATE_LIMIT_TEST_ID",
    "SEVERITY_PASSWORD_RESET_MEDIUM",
    "classify_enumeration_responses",
    "classify_rate_limit_attempts",
]
