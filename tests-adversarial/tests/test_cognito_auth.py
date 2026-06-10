"""Smoke tests for src/identity/cognito_auth.py.

Covers:
  - DEMO_PASSWORD unset -> MissingPasswordError (AC5 alignment).
  - COGNITO_USER_POOL_ID unset (with DEMO_PASSWORD set) -> CognitoAuthError.
  - COGNITO_CLIENT_ID unset (with the others set) -> CognitoAuthError.
  - Happy path with a stubbed boto3 client: Identity has the expected fields
    and `cognito:groups` is parsed from a hand-built fake JWT.
  - In-process cache: a second fetch_identity does not re-invoke boto3.
  - fetch_all returns all four personas.
  - Module source has no token-writing code (no open(...,'w') or write_text).

These tests do NOT hit the deployed Cognito pool. `boto3.client` is monkey-
patched to a stub before any module-under-test code path that constructs a
client.
"""

from __future__ import annotations

import base64
import inspect
import json
import re
from pathlib import Path

import pytest

from src.identity import cognito_auth
from src.identity.cognito_auth import (
    CognitoAuthError,
    Identity,
    MissingPasswordError,
    Persona,
    fetch_all,
    fetch_identity,
)


# ──────────────────────────── fixtures / helpers ────────────────────────────


def _make_fake_jwt(groups: list[str] | str) -> str:
    """Build a 3-segment JWT-shaped token whose payload has cognito:groups.

    The signature segment is the literal string 'fake' — the module's decoder
    never validates it, so any non-empty placeholder works. The header segment
    is irrelevant for our test (decoder takes parts[1]).
    """
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload_dict = {
        "sub": "00000000-0000-0000-0000-000000000000",
        "cognito:username": "test_user",
        "cognito:groups": groups,
    }
    payload_b64 = (
        base64.urlsafe_b64encode(json.dumps(payload_dict).encode())
        .rstrip(b"=")
        .decode()
    )
    return f"{header}.{payload_b64}.fake"


class _StubCognitoClient:
    """Stand-in for boto3.client('cognito-idp'). Records calls; returns canned tokens."""

    def __init__(self, id_token: str, access_token: str = "access-token-xyz"):
        self._id_token = id_token
        self._access_token = access_token
        self.calls: list[dict] = []

    def initiate_auth(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "AuthenticationResult": {
                "IdToken": self._id_token,
                "AccessToken": self._access_token,
                "RefreshToken": "refresh-token-xyz",
                "ExpiresIn": 3600,
                "TokenType": "Bearer",
            }
        }


@pytest.fixture(autouse=True)
def _clear_cognito_cache():
    """Reset the module-level cache before every test."""
    cognito_auth._clear_cache()
    yield
    cognito_auth._clear_cache()


@pytest.fixture
def _full_env(monkeypatch):
    """Set DEMO_PASSWORD + COGNITO_USER_POOL_ID + COGNITO_CLIENT_ID."""
    monkeypatch.setenv("DEMO_PASSWORD", "Sup3rSecret!")
    monkeypatch.setenv("COGNITO_USER_POOL_ID", "us-east-1_AbC123XyZ")
    monkeypatch.setenv("COGNITO_CLIENT_ID", "1example2client3id4")


# ──────────────────────────── tests ────────────────────────────


def test_fetch_identity_without_demo_password_raises_missing_password(monkeypatch):
    """AC5: refuse to start when DEMO_PASSWORD is unset.

    The message MUST start with the literal phrase ``DEMO_PASSWORD required``
    so the task-4 plan acceptance grep (first 80 chars of stderr) finds it.
    """
    monkeypatch.delenv("DEMO_PASSWORD", raising=False)
    monkeypatch.setenv("COGNITO_USER_POOL_ID", "us-east-1_AbC123XyZ")
    monkeypatch.setenv("COGNITO_CLIENT_ID", "1example2client3id4")

    with pytest.raises(MissingPasswordError) as exc_info:
        fetch_identity(Persona.CISO)

    msg = str(exc_info.value)
    assert msg.startswith("DEMO_PASSWORD required"), (
        "error message must start with the literal phrase 'DEMO_PASSWORD required' "
        "so the plan-acceptance grep on first 80 chars of stderr finds it"
    )
    assert "DEMO_PASSWORD" in msg, "error message must name the env var"
    assert ".env.example" in msg, "error message must point at the template"


def test_persona_repr_matches_value():
    """`repr(Persona.CISO)` is `"'ciso'"` and `list(Persona)` formats as a string list.

    The plan acceptance for task 4 expects
    `print(list(fetch_all()))` to produce `['ciso', 'soc', 'grc', 'employee']`.
    `list(fetch_all())` iterates the dict keys (Persona instances) and Python's
    list-repr concatenates `repr(item)` for each item. Overriding
    `Persona.__repr__` to return `repr(self.value)` makes that work.
    """
    assert repr(Persona.CISO) == "'ciso'"
    assert repr(Persona.SOC) == "'soc'"
    assert repr(Persona.GRC) == "'grc'"
    assert repr(Persona.EMPLOYEE) == "'employee'"
    # The full container repr in canonical persona order.
    assert repr(list(Persona)) == "['ciso', 'soc', 'grc', 'employee']"


def test_excepthook_writes_message_to_stderr(capsys):
    """`_short_stderr_excepthook` prints just the message for our exception family.

    This is what makes the plan acceptance work end-to-end: under the
    `python -c "... fetch_all() ..."` shell-out, an unhandled
    `MissingPasswordError` is routed through this hook, which writes the
    human-readable message to stderr WITHOUT a traceback header. The phrase
    `DEMO_PASSWORD required` lands within the first 80 chars.
    """
    err = MissingPasswordError(
        "DEMO_PASSWORD required: environment variable is not set."
    )
    cognito_auth._short_stderr_excepthook(MissingPasswordError, err, None)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "DEMO_PASSWORD required" in captured.err[:80], (
        "the phrase must land within the first 80 chars of stderr "
        "to satisfy the plan-acceptance grep"
    )
    # No "Traceback" header — that was the whole point of the hook.
    assert "Traceback" not in captured.err


def test_excepthook_delegates_for_unrelated_exception(monkeypatch, capsys):
    """For exceptions outside `CognitoAuthError`, the hook delegates to the default.

    We patch `sys.__excepthook__` with a sentinel to verify delegation occurred,
    without actually invoking the real default hook (which writes a traceback
    to the real stderr — capsys captures it but we'd rather assert on the call).
    """
    import sys as _sys

    sentinel_calls = []

    def _fake_default(exc_type, exc_value, exc_tb):
        sentinel_calls.append((exc_type, exc_value))

    monkeypatch.setattr(_sys, "__excepthook__", _fake_default)

    err = ValueError("unrelated")
    cognito_auth._short_stderr_excepthook(ValueError, err, None)

    assert sentinel_calls == [(ValueError, err)], (
        "non-CognitoAuthError exceptions must go through sys.__excepthook__"
    )
    captured = capsys.readouterr()
    assert captured.err == "", (
        "hook must not write anything itself for unrelated exceptions"
    )


def test_fetch_identity_without_user_pool_id_raises_cognito_auth_error(monkeypatch):
    """Password set, pool id missing -> CognitoAuthError naming the missing var."""
    monkeypatch.setenv("DEMO_PASSWORD", "Sup3rSecret!")
    monkeypatch.delenv("COGNITO_USER_POOL_ID", raising=False)
    monkeypatch.setenv("COGNITO_CLIENT_ID", "1example2client3id4")

    with pytest.raises(CognitoAuthError) as exc_info:
        fetch_identity(Persona.CISO)

    assert "COGNITO_USER_POOL_ID" in str(exc_info.value)
    # Must NOT be the MissingPasswordError subclass — that one is reserved for
    # DEMO_PASSWORD specifically.
    assert not isinstance(exc_info.value, MissingPasswordError)


def test_fetch_identity_without_client_id_raises_cognito_auth_error(monkeypatch):
    """Pool id set, client id missing -> CognitoAuthError naming the missing var."""
    monkeypatch.setenv("DEMO_PASSWORD", "Sup3rSecret!")
    monkeypatch.setenv("COGNITO_USER_POOL_ID", "us-east-1_AbC123XyZ")
    monkeypatch.delenv("COGNITO_CLIENT_ID", raising=False)

    with pytest.raises(CognitoAuthError) as exc_info:
        fetch_identity(Persona.CISO)

    assert "COGNITO_CLIENT_ID" in str(exc_info.value)


def test_fetch_identity_happy_path_returns_expected_identity(monkeypatch, _full_env):
    """Stubbed boto3 returns canned tokens; Identity carries the right fields."""
    fake_id_token = _make_fake_jwt(groups=["ciso"])
    stub = _StubCognitoClient(id_token=fake_id_token)
    monkeypatch.setattr(
        cognito_auth, "boto3", type("M", (), {"client": lambda *a, **kw: stub})
    )

    identity = fetch_identity(Persona.CISO)

    assert isinstance(identity, Identity)
    assert identity.persona == Persona.CISO
    assert identity.username == "ciso_diana@meridianinsurance.com"
    assert identity.id_token == fake_id_token
    assert identity.access_token == "access-token-xyz"
    assert identity.cognito_groups == ("ciso",)

    # boto3 InitiateAuth was called exactly once with the expected shape.
    assert len(stub.calls) == 1
    call = stub.calls[0]
    assert call["AuthFlow"] == "USER_PASSWORD_AUTH"
    assert call["ClientId"] == "1example2client3id4"
    assert call["AuthParameters"]["USERNAME"] == "ciso_diana@meridianinsurance.com"
    assert call["AuthParameters"]["PASSWORD"] == "Sup3rSecret!"


def test_fetch_identity_parses_csv_groups_from_jwt(monkeypatch, _full_env):
    """`cognito:groups` may be a CSV string; the decoder must tolerate both."""
    fake_id_token = _make_fake_jwt(groups="ciso,grc")
    stub = _StubCognitoClient(id_token=fake_id_token)
    monkeypatch.setattr(
        cognito_auth, "boto3", type("M", (), {"client": lambda *a, **kw: stub})
    )

    identity = fetch_identity(Persona.CISO)

    assert identity.cognito_groups == ("ciso", "grc")


def test_fetch_identity_caches_across_calls(monkeypatch, _full_env):
    """Second call for the same persona must not re-invoke boto3."""
    fake_id_token = _make_fake_jwt(groups=["soc"])
    stub = _StubCognitoClient(id_token=fake_id_token)
    monkeypatch.setattr(
        cognito_auth, "boto3", type("M", (), {"client": lambda *a, **kw: stub})
    )

    first = fetch_identity(Persona.SOC)
    second = fetch_identity(Persona.SOC)

    assert first is second, "cache must return the same Identity object"
    assert len(stub.calls) == 1, "boto3.initiate_auth must only be called once"


def test_fetch_all_returns_all_four_personas(monkeypatch, _full_env):
    """fetch_all() returns one Identity per persona; usernames match the manifest."""
    fake_id_token = _make_fake_jwt(groups=["any"])
    stub = _StubCognitoClient(id_token=fake_id_token)
    monkeypatch.setattr(
        cognito_auth, "boto3", type("M", (), {"client": lambda *a, **kw: stub})
    )

    identities = fetch_all()

    assert set(identities.keys()) == set(Persona)
    assert identities[Persona.CISO].username == "ciso_diana@meridianinsurance.com"
    assert identities[Persona.SOC].username == "soc_marcus@meridianinsurance.com"
    assert identities[Persona.GRC].username == "grc_priya@meridianinsurance.com"
    assert identities[Persona.EMPLOYEE].username == "emp_sarah@meridianinsurance.com"
    # Each persona invokes initiate_auth exactly once.
    assert len(stub.calls) == 4


def test_fetch_all_reuses_cached_identity(monkeypatch, _full_env):
    """After fetch_identity(CISO), fetch_all() only makes 3 more InitiateAuth calls."""
    fake_id_token = _make_fake_jwt(groups=["x"])
    stub = _StubCognitoClient(id_token=fake_id_token)
    monkeypatch.setattr(
        cognito_auth, "boto3", type("M", (), {"client": lambda *a, **kw: stub})
    )

    fetch_identity(Persona.CISO)
    assert len(stub.calls) == 1

    fetch_all()
    assert len(stub.calls) == 4, "CISO should be cached; only 3 new calls"


def test_module_source_has_no_token_writing_code():
    """Static check: the module never opens a file for write and never calls write_text.

    Tokens MUST stay in-process. If a future change introduces persistence,
    this test fails loudly.
    """
    source_path = Path(inspect.getsourcefile(cognito_auth))
    source = source_path.read_text()

    # Match `open(` calls with a write-ish mode (w, wb, a, x).
    write_open_pattern = re.compile(r"""open\([^)]*['"][wax]b?['"]""", re.MULTILINE)
    assert not write_open_pattern.search(source), (
        "cognito_auth.py contains an open(...,'w'|'wb'|'a'|'x') call; "
        "tokens must never be persisted to disk"
    )

    # Match write_text / write_bytes calls on any Path-like.
    write_method_pattern = re.compile(r"\.write_(text|bytes)\s*\(")
    assert not write_method_pattern.search(source), (
        "cognito_auth.py contains a .write_text/.write_bytes call; "
        "tokens must never be persisted to disk"
    )


def test_fetch_identity_no_disk_writes_under_run_dir(monkeypatch, _full_env, tmp_path):
    """Behavioral check: calling fetch_identity creates no files under tmp_path.

    tmp_path is the test harness's per-test scratch directory. If cognito_auth
    ever writes a token-cache file to disk, it would land somewhere — and even
    if not under tmp_path, listing files before/after is a cheap second guard.
    """
    fake_id_token = _make_fake_jwt(groups=["ciso"])
    stub = _StubCognitoClient(id_token=fake_id_token)
    monkeypatch.setattr(
        cognito_auth, "boto3", type("M", (), {"client": lambda *a, **kw: stub})
    )
    monkeypatch.chdir(tmp_path)

    before = set(tmp_path.rglob("*"))
    fetch_identity(Persona.CISO)
    after = set(tmp_path.rglob("*"))

    assert before == after, "fetch_identity must not write any file"


def test_initiate_auth_client_error_wraps_to_cognito_auth_error(monkeypatch, _full_env):
    """A boto3 ClientError surfaces as CognitoAuthError, not a raw boto exception."""
    from botocore.exceptions import ClientError

    class _BadClient:
        def initiate_auth(self, **kwargs):
            raise ClientError(
                {
                    "Error": {
                        "Code": "NotAuthorizedException",
                        "Message": "Incorrect username or password.",
                    }
                },
                "InitiateAuth",
            )

    monkeypatch.setattr(
        cognito_auth, "boto3", type("M", (), {"client": lambda *a, **kw: _BadClient()})
    )

    with pytest.raises(CognitoAuthError) as exc_info:
        fetch_identity(Persona.CISO)

    assert "InitiateAuth failed" in str(exc_info.value)
    assert "ciso" in str(exc_info.value).lower()


def test_initiate_auth_challenge_response_raises(monkeypatch, _full_env):
    """If Cognito returns a Challenge (e.g. NEW_PASSWORD_REQUIRED), surface it loudly."""

    class _ChallengeClient:
        def initiate_auth(self, **kwargs):
            return {"ChallengeName": "NEW_PASSWORD_REQUIRED", "Session": "sess-xyz"}

    monkeypatch.setattr(
        cognito_auth,
        "boto3",
        type("M", (), {"client": lambda *a, **kw: _ChallengeClient()}),
    )

    with pytest.raises(CognitoAuthError) as exc_info:
        fetch_identity(Persona.CISO)

    assert "NEW_PASSWORD_REQUIRED" in str(exc_info.value)
