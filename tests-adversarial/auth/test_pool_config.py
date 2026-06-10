"""Cognito user-pool config audit (Block C — #18).

A black-box config check, not a fuzz probe. Uses
``cognito-idp:describe_user_pool`` to assert that the deployed pool's
password policy and admin-create settings meet a minimum baseline.

Per-assertion test ids (one row per check)::

    auth.pool-config.minimum-length              — MinimumLength >= 12
    auth.pool-config.require-uppercase           — RequireUppercase True
    auth.pool-config.require-lowercase           — RequireLowercase True
    auth.pool-config.require-numbers             — RequireNumbers True
    auth.pool-config.require-symbols             — RequireSymbols True
    auth.pool-config.temp-password-validity-days — <= 7
    auth.pool-config.account-recovery-set        — AccountRecoverySetting present
    auth.pool-config.admin-create-only           — AdminCreateUserOnly True

Severity ladder
---------------
* MinimumLength < 12 → MEDIUM (the canonical NIST 800-63B floor).
* RequireUppercase / Lowercase / Numbers / Symbols false → LOW.
* TemporaryPasswordValidityDays > 7 → LOW.
* AccountRecoverySetting missing → MEDIUM (no recovery path).
* AdminCreateUserOnly False → LOW (open signup is a separate concern but
  worth flagging on an internal pool).

Skip behaviour
--------------
Needs ``COGNITO_USER_POOL_ID`` and AWS credentials with
``cognito-idp:DescribeUserPool``. Without either, the entire module
skips. The IAM permission is read-only and standard for an audit role.
"""

from __future__ import annotations

import os
import time
from typing import Any

import boto3
import pytest
from botocore.exceptions import BotoCoreError, ClientError

_AWS_REGION = "us-east-1"
_TEST_ID_PREFIX = "auth.pool-config"

SEVERITY_POOL_CONFIG_MEDIUM = "medium"
SEVERITY_POOL_CONFIG_LOW = "low"

# Test id constants — exposed for unit tests.
MIN_LENGTH_TEST_ID = f"{_TEST_ID_PREFIX}.minimum-length"
REQ_UPPER_TEST_ID = f"{_TEST_ID_PREFIX}.require-uppercase"
REQ_LOWER_TEST_ID = f"{_TEST_ID_PREFIX}.require-lowercase"
REQ_NUMBERS_TEST_ID = f"{_TEST_ID_PREFIX}.require-numbers"
REQ_SYMBOLS_TEST_ID = f"{_TEST_ID_PREFIX}.require-symbols"
TEMP_PASSWORD_TEST_ID = f"{_TEST_ID_PREFIX}.temp-password-validity-days"
RECOVERY_TEST_ID = f"{_TEST_ID_PREFIX}.account-recovery-set"
ADMIN_CREATE_TEST_ID = f"{_TEST_ID_PREFIX}.admin-create-only"

ALL_POOL_CONFIG_TEST_IDS: list[str] = [
    MIN_LENGTH_TEST_ID,
    REQ_UPPER_TEST_ID,
    REQ_LOWER_TEST_ID,
    REQ_NUMBERS_TEST_ID,
    REQ_SYMBOLS_TEST_ID,
    TEMP_PASSWORD_TEST_ID,
    RECOVERY_TEST_ID,
    ADMIN_CREATE_TEST_ID,
]

# Minimum acceptable values.
_MIN_PASSWORD_LENGTH = 12
_MAX_TEMP_PASSWORD_DAYS = 7


# ─────────────────────────── classifiers ─────────────────────────────────────


def classify_minimum_length(min_length: int | None) -> tuple[str, str | None]:
    """PASS if >= 12; else FAIL severity MEDIUM."""
    if min_length is not None and min_length >= _MIN_PASSWORD_LENGTH:
        return "pass", None
    return "fail", SEVERITY_POOL_CONFIG_MEDIUM


def classify_require_flag(value: bool | None) -> tuple[str, str | None]:
    """PASS if True; else FAIL severity LOW."""
    if value is True:
        return "pass", None
    return "fail", SEVERITY_POOL_CONFIG_LOW


def classify_temp_password_validity(days: int | None) -> tuple[str, str | None]:
    """PASS if <= 7 days; else FAIL severity LOW."""
    if days is not None and days <= _MAX_TEMP_PASSWORD_DAYS:
        return "pass", None
    return "fail", SEVERITY_POOL_CONFIG_LOW


def classify_account_recovery(recovery_setting: Any) -> tuple[str, str | None]:
    """PASS if a non-empty dict; else FAIL severity MEDIUM."""
    if isinstance(recovery_setting, dict) and recovery_setting:
        return "pass", None
    return "fail", SEVERITY_POOL_CONFIG_MEDIUM


def classify_admin_create_only(value: bool | None) -> tuple[str, str | None]:
    """PASS if True; else FAIL severity LOW (open signup on internal pool)."""
    if value is True:
        return "pass", None
    return "fail", SEVERITY_POOL_CONFIG_LOW


# ─────────────────────────── env / fixtures ──────────────────────────────────


def _module_skip_if_pool_id_missing() -> None:
    if not os.environ.get("COGNITO_USER_POOL_ID", "").strip():
        pytest.skip(
            "COGNITO_USER_POOL_ID not set — pool-config audit needs the pool id.",
            allow_module_level=True,
        )


_module_skip_if_pool_id_missing()


@pytest.fixture(scope="module")
def pool_description(results_writer) -> dict:
    """Fetch ``describe_user_pool`` once per module run.

    On error, records one ``skipped`` row per test id and aborts the module.
    """
    pool_id = os.environ["COGNITO_USER_POOL_ID"].strip()
    client = boto3.client("cognito-idp", region_name=_AWS_REGION)
    try:
        response = client.describe_user_pool(UserPoolId=pool_id)
    except (ClientError, BotoCoreError) as exc:
        for tid in ALL_POOL_CONFIG_TEST_IDS:
            results_writer.record(
                {
                    "test_id": tid,
                    "status": "skipped",
                    "layer": "auth",
                    "target_kind": "api_route",
                    "target_id": "cognito-describe-user-pool",
                    "skipped_reason": (
                        f"describe_user_pool failed: {type(exc).__name__}"
                    ),
                }
            )
        pytest.skip(f"describe_user_pool failed: {exc}")
    return response.get("UserPool", {})


def _record_and_assert(
    test_id: str,
    target_id: str,
    status: str,
    severity: str | None,
    duration: float,
    failure_message: str,
    results_writer,
) -> None:
    row: dict = {
        "test_id": test_id,
        "status": status,
        "layer": "auth",
        "target_kind": "api_route",
        "target_id": target_id,
        "duration_seconds": duration,
    }
    if severity is not None:
        row["severity"] = severity
    if status == "fail":
        row["evidence_path"] = f"auth/results.json#{test_id}"
    results_writer.record(row)
    if status == "fail":
        pytest.fail(f"{test_id}: {failure_message} (severity={severity})")


# ─────────────────────────── tests ───────────────────────────────────────────


def _password_policy(pool: dict) -> dict:
    return pool.get("Policies", {}).get("PasswordPolicy", {}) or {}


def test_pool_minimum_length(pool_description: dict, results_writer) -> None:
    started = time.monotonic()
    policy = _password_policy(pool_description)
    min_length = policy.get("MinimumLength")
    status, severity = classify_minimum_length(min_length)
    _record_and_assert(
        MIN_LENGTH_TEST_ID,
        "cognito-describe-user-pool",
        status,
        severity,
        time.monotonic() - started,
        f"MinimumLength={min_length!r} < {_MIN_PASSWORD_LENGTH}",
        results_writer,
    )


def test_pool_require_uppercase(pool_description: dict, results_writer) -> None:
    started = time.monotonic()
    policy = _password_policy(pool_description)
    value = policy.get("RequireUppercase")
    status, severity = classify_require_flag(value)
    _record_and_assert(
        REQ_UPPER_TEST_ID,
        "cognito-describe-user-pool",
        status,
        severity,
        time.monotonic() - started,
        f"RequireUppercase={value!r}",
        results_writer,
    )


def test_pool_require_lowercase(pool_description: dict, results_writer) -> None:
    started = time.monotonic()
    policy = _password_policy(pool_description)
    value = policy.get("RequireLowercase")
    status, severity = classify_require_flag(value)
    _record_and_assert(
        REQ_LOWER_TEST_ID,
        "cognito-describe-user-pool",
        status,
        severity,
        time.monotonic() - started,
        f"RequireLowercase={value!r}",
        results_writer,
    )


def test_pool_require_numbers(pool_description: dict, results_writer) -> None:
    started = time.monotonic()
    policy = _password_policy(pool_description)
    value = policy.get("RequireNumbers")
    status, severity = classify_require_flag(value)
    _record_and_assert(
        REQ_NUMBERS_TEST_ID,
        "cognito-describe-user-pool",
        status,
        severity,
        time.monotonic() - started,
        f"RequireNumbers={value!r}",
        results_writer,
    )


def test_pool_require_symbols(pool_description: dict, results_writer) -> None:
    started = time.monotonic()
    policy = _password_policy(pool_description)
    value = policy.get("RequireSymbols")
    status, severity = classify_require_flag(value)
    _record_and_assert(
        REQ_SYMBOLS_TEST_ID,
        "cognito-describe-user-pool",
        status,
        severity,
        time.monotonic() - started,
        f"RequireSymbols={value!r}",
        results_writer,
    )


def test_pool_temp_password_validity(pool_description: dict, results_writer) -> None:
    started = time.monotonic()
    policy = _password_policy(pool_description)
    days = policy.get("TemporaryPasswordValidityDays")
    status, severity = classify_temp_password_validity(days)
    _record_and_assert(
        TEMP_PASSWORD_TEST_ID,
        "cognito-describe-user-pool",
        status,
        severity,
        time.monotonic() - started,
        f"TemporaryPasswordValidityDays={days!r} > {_MAX_TEMP_PASSWORD_DAYS}",
        results_writer,
    )


def test_pool_account_recovery(pool_description: dict, results_writer) -> None:
    started = time.monotonic()
    recovery = pool_description.get("AccountRecoverySetting")
    status, severity = classify_account_recovery(recovery)
    _record_and_assert(
        RECOVERY_TEST_ID,
        "cognito-describe-user-pool",
        status,
        severity,
        time.monotonic() - started,
        f"AccountRecoverySetting={recovery!r}",
        results_writer,
    )


def test_pool_admin_create_only(pool_description: dict, results_writer) -> None:
    started = time.monotonic()
    admin_create = pool_description.get("AdminCreateUserConfig", {})
    value = admin_create.get("AllowAdminCreateUserOnly")
    status, severity = classify_admin_create_only(value)
    _record_and_assert(
        ADMIN_CREATE_TEST_ID,
        "cognito-describe-user-pool",
        status,
        severity,
        time.monotonic() - started,
        f"AllowAdminCreateUserOnly={value!r}",
        results_writer,
    )


__all__ = [
    "ADMIN_CREATE_TEST_ID",
    "ALL_POOL_CONFIG_TEST_IDS",
    "MIN_LENGTH_TEST_ID",
    "RECOVERY_TEST_ID",
    "REQ_LOWER_TEST_ID",
    "REQ_NUMBERS_TEST_ID",
    "REQ_SYMBOLS_TEST_ID",
    "REQ_UPPER_TEST_ID",
    "SEVERITY_POOL_CONFIG_LOW",
    "SEVERITY_POOL_CONFIG_MEDIUM",
    "TEMP_PASSWORD_TEST_ID",
    "classify_account_recovery",
    "classify_admin_create_only",
    "classify_minimum_length",
    "classify_require_flag",
    "classify_temp_password_validity",
]
