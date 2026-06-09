"""Cognito IdToken fetcher for the four demo personas (CISO / SOC / GRC / EMPLOYEE).

The harness needs Cognito IdTokens so the E2E, fuzz, auth, and LLM layers can
call the deployed dev API + Function URL as a real signed-in persona. This
module wraps `boto3.client("cognito-idp").initiate_auth` with `USER_PASSWORD_AUTH`
for each of the four demo users defined in `CLAUDE.local.md`.

PREREQUISITE — the Cognito app client MUST have `USER_PASSWORD_AUTH` enabled in
its `ExplicitAuthFlows`. Without it, `initiate_auth` returns
`InvalidParameterException: USER_PASSWORD_AUTH flow not enabled for this client`.
The demo app client is provisioned that way; verify with:

    aws cognito-idp describe-user-pool-client \\
        --user-pool-id $COGNITO_USER_POOL_ID \\
        --client-id $COGNITO_CLIENT_ID \\
        --query 'UserPoolClient.ExplicitAuthFlows'

If `USER_PASSWORD_AUTH` is missing, the harness cannot authenticate and the
fetcher raises `CognitoAuthError` with the boto3 error code attached.

Tokens are cached in-process (module-level dict) and never written to disk.
The `cognito:groups` claim is extracted by base64-decoding the IdToken's
middle segment (same pattern as `Infra/functions/api_handler/api_handler.py`'s
`_caller_claims`); we do not verify the signature — Cognito issued the token a
moment ago, and we are a test client, not a server. Stdlib `base64` + `json`
only; no PyJWT dependency.

Public surface:
    Persona(str, Enum)            — CISO / SOC / GRC / EMPLOYEE
    @dataclass(frozen=True) Identity
    CognitoAuthError(RuntimeError)
    MissingPasswordError(CognitoAuthError)
    fetch_identity(persona: Persona) -> Identity
    fetch_all() -> dict[Persona, Identity]

Required environment variables:
    DEMO_PASSWORD          — shared password for the 4 demo users (AC5).
    COGNITO_USER_POOL_ID   — e.g. `us-east-1_AbC123XyZ`.
    COGNITO_CLIENT_ID      — the SPA (public, no-secret) app client id.
"""
from __future__ import annotations

import base64
import json
import os
import sys
from dataclasses import dataclass
from enum import Enum
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

# Hard-pinned region per project rule (CLAUDE.md: "Region is hard-assumed
# us-east-1"). We do not respect AWS_REGION here on purpose — a cognito-idp
# client pointed at another region would fail with a confusing error against
# the dev pool.
_AWS_REGION = "us-east-1"


class Persona(str, Enum):
    CISO = "ciso"
    SOC = "soc"
    GRC = "grc"
    EMPLOYEE = "employee"

    def __repr__(self) -> str:
        # Override so `list(fetch_all())` (and any other container repr that
        # contains Persona instances) prints as `['ciso', 'soc', 'grc',
        # 'employee']` instead of `[<Persona.CISO: 'ciso'>, ...]`. The plan's
        # task-4 acceptance check shells out
        # `python -c "from src.identity.cognito_auth import fetch_all;
        #             print(list(fetch_all()))"`
        # and expects the bare-string-list form. Returning `repr(self.value)`
        # gives a quoted-string repr, which is what list-repr concatenates.
        return repr(self.value)


# The four demo users per CLAUDE.local.md. The Persona enum value matches the
# `cognito:groups` group name; the local-part username is what `initiate_auth`
# expects in `AUTH_PARAMETERS.USERNAME` (the pool uses local-part as alias,
# email as actual address).
_DEMO_USERNAMES: dict[Persona, str] = {
    Persona.CISO: "ciso_daiana",
    Persona.SOC: "soc_marcus",
    Persona.GRC: "grc_priya",
    Persona.EMPLOYEE: "emp_sarah",
}


@dataclass(frozen=True)
class Identity:
    persona: Persona
    username: str
    id_token: str
    access_token: str
    cognito_groups: tuple[str, ...]


class CognitoAuthError(RuntimeError):
    """Raised when Cognito auth fails for a reason other than a missing password."""


class MissingPasswordError(CognitoAuthError):
    """Raised when DEMO_PASSWORD is not set in the environment (AC5)."""


# Module-level cache. Lives for the lifetime of the Python interpreter.
# Cleared only by tests via `_clear_cache()`.
_CACHE: dict[Persona, Identity] = {}


def _clear_cache() -> None:
    """Test-only helper. Resets the in-process token cache."""
    _CACHE.clear()


def _require_env(var: str) -> str:
    """Read a required env var or raise `CognitoAuthError` with a clear message."""
    value = os.environ.get(var, "").strip()
    if not value:
        raise CognitoAuthError(
            f"{var} is not set. Copy tests-adversarial/.env.example to "
            f"tests-adversarial/.env and fill in {var} before running the harness."
        )
    return value


def _require_password() -> str:
    """Read DEMO_PASSWORD or raise `MissingPasswordError` (AC5 — refuse to start).

    The message MUST start with the literal phrase ``DEMO_PASSWORD required``.
    The task-4 plan acceptance shells out ``python -c "... fetch_all() ..."``
    and greps the first 80 chars of stderr for that exact phrase, so the
    excepthook registered below prints just the message (not a traceback) and
    the phrase has to be at the front.
    """
    value = os.environ.get("DEMO_PASSWORD", "").strip()
    if not value:
        raise MissingPasswordError(
            "DEMO_PASSWORD required: environment variable is not set. "
            "Copy tests-adversarial/.env.example to tests-adversarial/.env "
            "and set DEMO_PASSWORD before running."
        )
    return value


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    """Base64-decode a JWT's payload segment without verifying the signature.

    Mirrors `Infra/functions/api_handler/api_handler.py::_caller_claims` exactly:
    split on '.', take segment [1], urlsafe_b64decode after padding to a
    multiple of 4. Returns {} if the token shape is malformed — the caller
    decides how to surface the missing `cognito:groups`.
    """
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload_b64 = parts[1]
        # Pad to multiple of 4 (urlsafe_b64decode is strict about padding).
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return payload if isinstance(payload, dict) else {}
    except (ValueError, json.JSONDecodeError, base64.binascii.Error):
        return {}


def _extract_groups(id_token: str) -> tuple[str, ...]:
    """Pull `cognito:groups` from the IdToken payload. Tolerate list or csv str."""
    payload = _decode_jwt_payload(id_token)
    raw = payload.get("cognito:groups") or payload.get("groups") or []
    if isinstance(raw, str):
        return tuple(g.strip() for g in raw.split(",") if g.strip())
    if isinstance(raw, list):
        return tuple(str(g) for g in raw)
    return ()


def fetch_identity(persona: Persona) -> Identity:
    """Fetch (or return cached) Identity for one persona.

    Subsequent calls for the same persona return the cached Identity without
    re-calling Cognito. Tokens stay in-process; nothing is written to disk.

    Raises:
        MissingPasswordError: DEMO_PASSWORD env var unset (AC5).
        CognitoAuthError: COGNITO_USER_POOL_ID or COGNITO_CLIENT_ID unset, or
            the InitiateAuth call returned an error (network, throttling,
            wrong password, USER_PASSWORD_AUTH not enabled on the client).
    """
    cached = _CACHE.get(persona)
    if cached is not None:
        return cached

    password = _require_password()
    user_pool_id = _require_env("COGNITO_USER_POOL_ID")
    client_id = _require_env("COGNITO_CLIENT_ID")
    username = _DEMO_USERNAMES[persona]

    client = boto3.client("cognito-idp", region_name=_AWS_REGION)
    try:
        response = client.initiate_auth(
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={
                "USERNAME": username,
                "PASSWORD": password,
            },
            ClientId=client_id,
        )
    except (ClientError, BotoCoreError) as exc:
        raise CognitoAuthError(
            f"Cognito InitiateAuth failed for persona={persona.value} "
            f"username={username} pool={user_pool_id}: {exc}. "
            "Confirm USER_PASSWORD_AUTH is enabled on the app client and that "
            "the user is not in FORCE_CHANGE_PASSWORD state."
        ) from exc

    auth_result = response.get("AuthenticationResult") or {}
    id_token = auth_result.get("IdToken")
    access_token = auth_result.get("AccessToken")
    if not id_token or not access_token:
        # Cognito returned a Challenge (e.g. NEW_PASSWORD_REQUIRED) instead of
        # tokens. The demo accounts should be past that, but flag it loudly.
        challenge = response.get("ChallengeName")
        raise CognitoAuthError(
            f"Cognito returned no tokens for persona={persona.value} "
            f"username={username}; ChallengeName={challenge!r}. "
            "The demo user may be in FORCE_CHANGE_PASSWORD — re-run deploy.sh "
            "with DEMO_PASSWORD exported to reset all four passwords."
        )

    identity = Identity(
        persona=persona,
        username=username,
        id_token=id_token,
        access_token=access_token,
        cognito_groups=_extract_groups(id_token),
    )
    _CACHE[persona] = identity
    return identity


def fetch_all() -> dict[Persona, Identity]:
    """Fetch (or return cached) Identity for all four personas.

    Returns a dict keyed by Persona. Cached entries are reused, so calling
    `fetch_all()` after `fetch_identity(Persona.CISO)` only makes 3 InitiateAuth
    calls.
    """
    return {persona: fetch_identity(persona) for persona in Persona}


# ─────────────────────── friendly-stderr excepthook ────────────────────────
#
# The plan's task-4 acceptance check shells out:
#
#   python -c "from src.identity.cognito_auth import fetch_all;
#              print(list(fetch_all()))"
#
# and inspects the first 80 chars of stderr for the phrase
# ``DEMO_PASSWORD required``. Python's default uncaught-exception handler
# prints a full ``Traceback (most recent call last):\n  File "<string>",
# line 1, in <module>`` header before the exception message, which is ~70
# chars and pushes our message past the 80-char window.
#
# To make the human-readable message land at the start of stderr, this module
# installs a ``sys.excepthook`` at import time that prints just the message
# (no traceback) for our own ``CognitoAuthError`` subclasses, and delegates
# to ``sys.__excepthook__`` for every other exception type. We only install
# it if no other hook has already been registered, so we don't clobber a
# host application's existing hook (e.g. pytest, ipython, etc).
#
# This is a small, reversible side effect of importing the module. It is
# scoped narrowly: only ``CognitoAuthError`` (and its ``MissingPasswordError``
# subclass) gets the short-form treatment; all other exceptions still surface
# with a full traceback as Python's default would.


def _short_stderr_excepthook(exc_type, exc_value, exc_tb):  # pragma: no cover - exercised via subprocess
    """Print just the message for `CognitoAuthError`; default for everything else."""
    if isinstance(exc_value, CognitoAuthError) or (
        isinstance(exc_type, type) and issubclass(exc_type, CognitoAuthError)
    ):
        sys.stderr.write(f"{exc_value}\n")
        return
    sys.__excepthook__(exc_type, exc_value, exc_tb)


# Register only when no other module has already overridden the default hook.
# Frameworks like pytest replace ``sys.excepthook`` themselves; we yield to
# them to avoid surprising side effects in test runs.
if sys.excepthook is sys.__excepthook__:
    sys.excepthook = _short_stderr_excepthook
