"""Smoke tests for scripts/emit_storage_states.py.

Covers the structural verification listed in the task-8 prompt:

  - With mocked identities, `emit()` writes exactly 4 valid JSON files named
    `<persona>.json` under the requested output dir.
  - Each emitted file contains the SPA-shaped `arbiter.tokens` blob in
    `sessionStorage` under the correct origin (no leading/trailing slash,
    no path), and has empty `cookies` + empty `localStorage` arrays.
  - The `arbiter.tokens` blob has all four fields useAuth.js::load() reads
    (`id_token`, `access_token`, `refresh_token`, `expires_at`).
  - `expires_at` is in epoch-MILLISECONDS (Date.now() shape), not seconds.
  - The `exp` claim in the JWT is converted to ms; missing/malformed exp
    falls back to "now + 1h" without raising.
  - The output dir is created if missing.
  - `main()` defaults to `TARGET_BASE_URL` from env, falls back to the
    deployed dev CloudFront, and exits 0 on success.
  - Origin extraction rejects malformed base URLs early.

These tests do NOT hit Cognito. The production `fetch_all()` path is exercised
only by the live E2E run (which requires DEMO_PASSWORD + a real user pool).
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import pytest

from scripts import emit_storage_states
from src.identity.cognito_auth import Identity, Persona


# ──────────────────────────── fixtures / helpers ────────────────────────────


def _make_fake_jwt(exp_seconds: int | None = None) -> str:
    """Build a minimally-valid 3-segment JWT with the given `exp` claim.

    Mirrors the helper in tests/test_cognito_auth.py — kept local so this
    file remains self-contained.
    """
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload: dict = {"sub": "fake-user", "cognito:groups": ["ciso"]}
    if exp_seconds is not None:
        payload["exp"] = exp_seconds
    payload_b64 = (
        base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    )
    signature = "deadbeef"
    return f"{header}.{payload_b64}.{signature}"


def _make_identity(persona: Persona, exp_seconds: int | None = None) -> Identity:
    return Identity(
        persona=persona,
        username=f"fake_{persona.value}",
        id_token=_make_fake_jwt(exp_seconds=exp_seconds),
        access_token=f"access-{persona.value}",
        cognito_groups=(persona.value,),
    )


def _all_fake_identities(
    exp_seconds: int | None = None,
) -> list[tuple[Persona, Identity]]:
    return [(p, _make_identity(p, exp_seconds=exp_seconds)) for p in Persona]


# ──────────────────────────── emit() — files on disk ────────────────────────


def test_emit_writes_four_files(tmp_path: Path) -> None:
    out = tmp_path / "storage-states"
    written = emit_storage_states.emit(
        out,
        base_url="https://example.test/",
        identities=_all_fake_identities(),
    )
    assert len(written) == 4
    names = sorted(p.name for p in written)
    assert names == ["ciso.json", "employee.json", "grc.json", "soc.json"]
    for path in written:
        assert path.exists() and path.is_file()


def test_emit_creates_missing_output_dir(tmp_path: Path) -> None:
    out = tmp_path / "nested" / "does" / "not" / "exist"
    written = emit_storage_states.emit(
        out,
        base_url="https://example.test/",
        identities=_all_fake_identities(),
    )
    assert out.is_dir()
    assert len(written) == 4


def test_emit_returns_iteration_order_matches_persona_enum(tmp_path: Path) -> None:
    """Persona order (CISO, SOC, GRC, EMPLOYEE) is preserved in the output."""
    out = tmp_path / "storage-states"
    written = emit_storage_states.emit(
        out,
        base_url="https://example.test/",
        identities=_all_fake_identities(),
    )
    expected_order = [f"{p.value}.json" for p in Persona]
    assert [p.name for p in written] == expected_order


# ──────────────────────────── storageState JSON shape ───────────────────────


def _load_state(path: Path) -> dict:
    with path.open() as fh:
        return json.load(fh)


def test_emit_file_has_playwright_storagestate_shape(tmp_path: Path) -> None:
    out = tmp_path / "storage-states"
    emit_storage_states.emit(
        out,
        base_url="https://d5u0vv1zl3eqd.cloudfront.net/",
        identities=[(Persona.CISO, _make_identity(Persona.CISO))],
    )
    state = _load_state(out / "ciso.json")

    # Top-level keys per Playwright's storageState contract.
    assert set(state.keys()) == {"cookies", "origins"}
    assert state["cookies"] == []
    assert len(state["origins"]) == 1

    origin_entry = state["origins"][0]
    # Origin string has no trailing slash and no path.
    assert origin_entry["origin"] == "https://d5u0vv1zl3eqd.cloudfront.net"
    assert origin_entry["localStorage"] == []
    assert len(origin_entry["sessionStorage"]) == 1


def test_emit_file_carries_arbiter_tokens_blob(tmp_path: Path) -> None:
    """The single sessionStorage entry matches the key + shape useAuth.js reads."""
    out = tmp_path / "storage-states"
    identity = _make_identity(Persona.CISO, exp_seconds=int(time.time()) + 3600)
    emit_storage_states.emit(
        out,
        base_url="https://example.test/",
        identities=[(Persona.CISO, identity)],
    )
    state = _load_state(out / "ciso.json")

    entry = state["origins"][0]["sessionStorage"][0]
    assert entry["name"] == "arbiter.tokens"

    blob = json.loads(entry["value"])
    # All four keys useAuth.js::load() reads (lines 150-156).
    assert set(blob.keys()) == {
        "id_token",
        "access_token",
        "refresh_token",
        "expires_at",
    }
    assert blob["id_token"] == identity.id_token
    assert blob["access_token"] == identity.access_token
    # refresh_token intentionally empty — see emit_storage_states module docstring.
    assert blob["refresh_token"] == ""


# ──────────────────────────── expires_at conversion ─────────────────────────


def test_expires_at_is_milliseconds(tmp_path: Path) -> None:
    """useAuth.js::isAuthenticated compares against Date.now() (ms). Confirm the
    JWT `exp` claim (seconds) is multiplied by 1000.
    """
    exp_seconds = int(time.time()) + 3600
    out = tmp_path / "storage-states"
    emit_storage_states.emit(
        out,
        base_url="https://example.test/",
        identities=[
            (Persona.CISO, _make_identity(Persona.CISO, exp_seconds=exp_seconds))
        ],
    )
    state = _load_state(out / "ciso.json")
    blob = json.loads(state["origins"][0]["sessionStorage"][0]["value"])
    assert blob["expires_at"] == exp_seconds * 1000


def test_expires_at_falls_back_when_jwt_has_no_exp(tmp_path: Path) -> None:
    """A JWT without an `exp` claim must not raise; expires_at defaults to
    now + 1h (in ms). This keeps a malformed token from breaking the run.
    """
    out = tmp_path / "storage-states"
    identity = _make_identity(Persona.CISO, exp_seconds=None)
    before_ms = int(time.time() * 1000)
    emit_storage_states.emit(
        out,
        base_url="https://example.test/",
        identities=[(Persona.CISO, identity)],
    )
    after_ms = int(time.time() * 1000)
    state = _load_state(out / "ciso.json")
    blob = json.loads(state["origins"][0]["sessionStorage"][0]["value"])
    # Fallback is now + 1h; allow generous slop for test runtime jitter.
    assert before_ms + 3500_000 <= blob["expires_at"] <= after_ms + 3700_000


def test_expires_at_falls_back_when_jwt_is_malformed(tmp_path: Path) -> None:
    """A non-JWT id_token must not raise; the fallback path takes over."""
    out = tmp_path / "storage-states"
    identity = Identity(
        persona=Persona.CISO,
        username="fake_ciso",
        id_token="not.a.jwt",  # base64 decode will choke
        access_token="access",
        cognito_groups=("ciso",),
    )
    emit_storage_states.emit(
        out,
        base_url="https://example.test/",
        identities=[(Persona.CISO, identity)],
    )
    state = _load_state(out / "ciso.json")
    blob = json.loads(state["origins"][0]["sessionStorage"][0]["value"])
    # Falls back to a future timestamp; just sanity-check it's a positive int.
    assert isinstance(blob["expires_at"], int)
    assert blob["expires_at"] > 0


# ──────────────────────────── origin extraction ─────────────────────────────


@pytest.mark.parametrize(
    "url, expected_origin",
    [
        ("https://example.com/", "https://example.com"),
        ("https://example.com", "https://example.com"),
        ("https://example.com/some/path", "https://example.com"),
        ("https://example.com:8443/", "https://example.com:8443"),
        ("http://localhost:5173/", "http://localhost:5173"),
    ],
)
def test_origin_from_url(url: str, expected_origin: str) -> None:
    assert emit_storage_states._origin_from_url(url) == expected_origin


@pytest.mark.parametrize("bad_url", ["", "not-a-url", "/no-scheme", "https://"])
def test_origin_from_url_rejects_malformed(bad_url: str) -> None:
    with pytest.raises(ValueError, match="TARGET_BASE_URL"):
        emit_storage_states._origin_from_url(bad_url)


# ──────────────────────────── main() CLI ────────────────────────────────────


def test_main_uses_default_base_url_when_env_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """With no TARGET_BASE_URL set, main() uses the spec §8 default."""
    monkeypatch.delenv("TARGET_BASE_URL", raising=False)
    monkeypatch.setattr(
        emit_storage_states,
        "fetch_all",
        lambda: {p: _make_identity(p) for p in Persona},
    )

    out = tmp_path / "storage-states"
    rc = emit_storage_states.main([str(out)])
    assert rc == 0

    # Verify the default URL flowed through to the origin in each file.
    state = _load_state(out / "ciso.json")
    assert state["origins"][0]["origin"] == "https://d5u0vv1zl3eqd.cloudfront.net"

    # And stdout listed all 4 written files.
    captured = capsys.readouterr()
    for persona in Persona:
        assert f"{persona.value}.json" in captured.out


def test_main_respects_target_base_url_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TARGET_BASE_URL", "https://staging.example.test/app/")
    monkeypatch.setattr(
        emit_storage_states,
        "fetch_all",
        lambda: {p: _make_identity(p) for p in Persona},
    )

    out = tmp_path / "storage-states"
    rc = emit_storage_states.main([str(out)])
    assert rc == 0

    state = _load_state(out / "soc.json")
    assert state["origins"][0]["origin"] == "https://staging.example.test"


def test_main_passes_through_fetch_all_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If fetch_all() raises (e.g. MissingPasswordError), main() does not swallow."""

    def boom() -> dict:
        from src.identity.cognito_auth import MissingPasswordError

        raise MissingPasswordError("DEMO_PASSWORD required: test injection")

    monkeypatch.setattr(emit_storage_states, "fetch_all", boom)

    with pytest.raises(Exception, match="DEMO_PASSWORD required"):
        emit_storage_states.main([str(tmp_path / "storage-states")])


# ──────────────────────────── content sanity ────────────────────────────────


def test_emitted_files_are_valid_json(tmp_path: Path) -> None:
    out = tmp_path / "storage-states"
    written = emit_storage_states.emit(
        out,
        base_url="https://example.test/",
        identities=_all_fake_identities(),
    )
    # Re-read every file as JSON; a TypeError or json.JSONDecodeError would fail.
    for path in written:
        with path.open() as fh:
            json.load(fh)


def test_emitted_files_end_with_newline(tmp_path: Path) -> None:
    """Cosmetic but consistent with the project's text-file convention."""
    out = tmp_path / "storage-states"
    emit_storage_states.emit(
        out,
        base_url="https://example.test/",
        identities=[(Persona.CISO, _make_identity(Persona.CISO))],
    )
    text = (out / "ciso.json").read_text()
    assert text.endswith("\n")
