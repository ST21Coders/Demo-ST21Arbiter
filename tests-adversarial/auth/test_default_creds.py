"""Default-credential probes (Block A — compliance checklist item 29).

Tries a small, curated wordlist of common factory / placeholder credentials
against the deployed Cognito user pool. The expected outcome for every pair
is ``NotAuthorizedException`` — Cognito has no factory account, and the
demo users have unique emails + a DEMO_PASSWORD that never matches any
member of the wordlist.

PASS = Cognito returned ``NotAuthorizedException`` (the credential is
rejected). FAIL severity HIGH = the credential authenticated unexpectedly,
which would mean a stray test account got provisioned with a guessable
password.

Why this test exists
--------------------
The compliance checklist (OWASP "Configuration & Infrastructure" #29) asks
for explicit probing of default / common credentials. Cognito's built-in
throttling rate-limits brute force in general, but a one-shot probe per
common pair is well under the throttle and is the cheapest signal we have
for "did somebody create a `admin/admin` test account and forget".

Scope
-----
- Five pairs only. The wordlist is intentionally tiny so the test wall-clock
  stays under 5s and the auth-layer's 5 RPS throttle absorbs the cost.
- Bypasses the ``cognito_auth.fetch_identity`` cache by constructing a fresh
  boto3 client per pair — the cache stores successful Identity dicts, not
  per-attempt outcomes, so it isn't useful here.
- Skips the entire module if ``DEMO_PASSWORD`` is unset (consistent with
  the rest of the auth layer).

Probe set
---------
``admin/admin``, ``admin/password``, ``test/test``, ``arbiter/arbiter``,
``demo/demo123``. Picked from the SANS top-default-creds list pruned to
the patterns most likely to land in a "spun up to try something"
test account.
"""

from __future__ import annotations

import os
import time

import boto3
import pytest
from botocore.exceptions import BotoCoreError, ClientError

_AWS_REGION = "us-east-1"

# Canonical id prefix matches the auth layer's convention.
_TEST_ID_PREFIX = "auth.default-creds"

# (username, password) pairs. Username is sent as-is to InitiateAuth so the
# user pool's UsernameAttributes config decides whether it's looked up as an
# email or a sub. The deployed pool requires email, so these usernames will
# all miss — Cognito returns NotAuthorizedException in both the
# "username not found" and "wrong password" cases, so the result is
# indistinguishable to the harness. That is exactly what we want: PASS
# means "rejected, for any reason".
DEFAULT_CRED_PAIRS: list[tuple[str, str]] = [
    ("admin", "admin"),
    ("admin", "password"),
    ("test", "test"),
    ("arbiter", "arbiter"),
    ("demo", "demo123"),
]


def _test_id_for(username: str, password: str) -> str:
    """Stable, slug-shaped test id per pair.

    Includes both the username and a short hash-derived suffix of the
    password so distinct pairs sharing a username (e.g. ``admin/admin`` and
    ``admin/password``) get distinct ids. We don't put the literal password
    in the id — even ``password`` would look like the test is documenting
    a secret. The 4-char hex prefix of MD5(password) is just an opaque
    distinguisher.
    """
    import hashlib

    slug = username.lower().replace(" ", "-")
    pwhash = hashlib.md5(password.encode("utf-8")).hexdigest()[:4]
    return f"{_TEST_ID_PREFIX}.{slug}-{pwhash}"


_DEFAULT_CRED_TEST_IDS: list[str] = [_test_id_for(u, p) for u, p in DEFAULT_CRED_PAIRS]


def _module_skip_if_password_missing() -> None:
    """Skip the whole module if DEMO_PASSWORD is unset.

    Matches the rest of the auth layer's pattern — without a way to set up
    a real Identity, the test infra is incomplete. We pin on DEMO_PASSWORD
    (not COGNITO_CLIENT_ID) because operators who haven't configured the
    demo password almost certainly haven't configured Cognito either.
    """
    if not os.environ.get("DEMO_PASSWORD", "").strip():
        pytest.skip(
            "DEMO_PASSWORD not set — default-creds module skipped "
            "(harness needs deployed-env env vars to call Cognito)",
            allow_module_level=True,
        )


_module_skip_if_password_missing()


def classify_default_creds_response(error_code: str | None) -> tuple[str, str | None]:
    """Map a Cognito error code to (status, severity).

    Rules:
      * ``NotAuthorizedException``  → ``pass`` (the credential was rejected).
      * ``UserNotFoundException``   → ``pass`` (some pools surface this
        instead of NotAuthorizedException; same outcome from our PoV).
      * ``None`` (no exception)     → ``fail`` severity ``high`` (the
        credential authenticated — a stray test account exists with a
        guessable password).
      * Any other code              → ``fail`` severity ``medium`` (the
        pool returned an unexpected error; not a finding on its own but
        worth flagging so the operator can investigate).
    """
    if error_code in ("NotAuthorizedException", "UserNotFoundException"):
        return "pass", None
    if error_code is None:
        return "fail", "high"
    return "fail", "medium"


@pytest.mark.parametrize(
    ("username", "password", "test_id"),
    [(u, p, _test_id_for(u, p)) for (u, p) in DEFAULT_CRED_PAIRS],
    ids=_DEFAULT_CRED_TEST_IDS,
)
def test_default_credentials_rejected(
    username: str,
    password: str,
    test_id: str,
    results_writer,
) -> None:
    """Try one default-credential pair against the deployed Cognito pool.

    PASS = Cognito returns ``NotAuthorizedException`` (or ``UserNotFoundException``).
    FAIL HIGH = the pair authenticated.
    """
    client_id = os.environ.get("COGNITO_CLIENT_ID", "").strip()
    if not client_id:
        results_writer.record(
            {
                "test_id": test_id,
                "status": "skipped",
                "layer": "auth",
                "target_kind": "api_route",
                "target_id": "cognito-initiate-auth",
                "skipped_reason": "COGNITO_CLIENT_ID not set",
            }
        )
        pytest.skip("COGNITO_CLIENT_ID not set")

    # Fresh client per pair — bypasses the cognito_auth module-level Identity
    # cache (which stores successes keyed by Persona, not raw username/password
    # pairs).
    client = boto3.client("cognito-idp", region_name=_AWS_REGION)
    started = time.monotonic()
    error_code: str | None = None
    try:
        client.initiate_auth(
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": username, "PASSWORD": password},
            ClientId=client_id,
        )
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code") or "UnknownClientError"
    except BotoCoreError as exc:
        # Network / DNS / endpoint resolution problem — not a security
        # finding; record as skipped with the underlying reason.
        duration = time.monotonic() - started
        results_writer.record(
            {
                "test_id": test_id,
                "status": "skipped",
                "layer": "auth",
                "target_kind": "api_route",
                "target_id": "cognito-initiate-auth",
                "skipped_reason": f"boto3 transport error: {type(exc).__name__}",
                "duration_seconds": duration,
            }
        )
        pytest.skip(f"boto3 transport error: {exc}")

    duration = time.monotonic() - started
    status, severity = classify_default_creds_response(error_code)
    row: dict = {
        "test_id": test_id,
        "status": status,
        "layer": "auth",
        "target_kind": "api_route",
        "target_id": "cognito-initiate-auth",
        "duration_seconds": duration,
    }
    if severity is not None:
        row["severity"] = severity
    if status == "fail":
        row["evidence_path"] = f"auth/results.json#{test_id}"
    results_writer.record(row)

    if status == "fail":
        # HIGH severity (authenticated) AND medium (unexpected error) both
        # surface as a hard pytest.fail so the operator can't miss them.
        pytest.fail(
            f"{test_id}: Cognito returned error_code={error_code!r} for "
            f"username={username!r} — expected NotAuthorizedException "
            f"(severity={severity})"
        )
