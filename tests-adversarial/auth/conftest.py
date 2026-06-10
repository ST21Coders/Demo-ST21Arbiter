"""Pytest fixtures + shared helpers for the auth-abuse layer (task 14).

Why a conftest, not a plain test file
-------------------------------------
The auth layer is structurally similar to the fuzz layer (task 12) — it
needs Cognito identities, an HTTP session with a sane timeout + throttle,
and a results writer that drains to `${RUN_DIR}/auth/results.json` at session
end. We mirror the fuzz conftest's fixture shapes but keep the two layers
independent (no cross-import) so each can be invoked standalone via
`pytest auth/` or `pytest fuzz/` without dragging the other in.

The auth layer specifically does NOT need:
- the parameterised `auth_header` fixture (auth tests build their own
  Authorization header — half the point is to send the wrong one),
- the `corpus` fixture (auth probes are enumerated from the manifest, not
  loaded from JSON files in `auth/corpus/`),
- the `--include-destructive` flag (auth tests use HTTP methods exactly as
  declared in the manifest; cross-persona tests against POST routes are
  intentional — a SOC token posting to a CISO-only POST is the whole point).

Manifest-driven test enumeration
--------------------------------
Each test module pulls its parametrisation from `coverage/manifest.json`:

  - `test_cross_persona.py`  → (route × blocked-persona) pairs.
  - `test_token_replay.py`   → routes with `auth_required: true`.
  - `test_expired_token.py`  → one representative route per HTTP method.

Manifest reads happen at module import time (so pytest can show all
parametrised test ids in `--collect-only`). The `manifest()` helper below
is the single read path — modules import it directly.

Fixtures provided
-----------------
target_base_url   — base URL of the deployed app (CloudFront).
api_base_url      — base URL of the API Gateway (TARGET_API_URL env, falls
                    back to `${TARGET_BASE_URL}/api`).
chat_function_url — Function URL for /chat (CHAT_FUNCTION_URL env). May be
                    None — auth tests against /chat skip when unset.
identities        — dict[Persona, Identity] from cognito_auth.fetch_all().
                    Session-scoped. Module skips if MissingPasswordError.
http_session      — requests.Session() with 10s default timeout and a 5 RPS
                    throttle (matches fuzz layer; same shared budget).
results_writer    — accumulates AuthResultsWriter rows; writes results.json
                    at session end via `pytest_sessionfinish`.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
from collections.abc import Generator
from functools import lru_cache
from pathlib import Path

import pytest
import requests

# Make `src.identity.cognito_auth` importable when pytest is invoked from the
# `auth/` subdirectory without first installing the harness package. The
# harness root is `tests-adversarial/`.
_HARNESS_ROOT = Path(__file__).resolve().parent.parent
if str(_HARNESS_ROOT) not in sys.path:
    sys.path.insert(0, str(_HARNESS_ROOT))


# ───────────────────────────── manifest loader ───────────────────────────────


_MANIFEST_PATH = _HARNESS_ROOT / "src" / "coverage" / "manifest.json"


@lru_cache(maxsize=1)
def manifest() -> dict:
    """Read and cache the coverage manifest. Module-scoped reads — pytest
    walks parametrise() at collection time so this fires before any fixture.
    """
    return json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))


def persona_ids() -> list[str]:
    """Ordered persona ids from the manifest."""
    return [p["id"] for p in manifest()["personas"]]


def api_routes() -> list[dict]:
    """All api_routes from the manifest, in declaration order."""
    return list(manifest()["api_routes"])


# ────────────────────────────── path helpers ─────────────────────────────────


def auth_results_path(run_dir: str | Path | None) -> Path:
    """Where the auth layer writes its `results.json`.

    Mirrors the fuzz layer's convention: when `RUN_DIR` is set (orchestrator
    runs), write under `${RUN_DIR}/auth/results.json`; otherwise fall back to
    `test-reports/_local/auth/results.json` so a standalone `pytest auth/`
    run still produces an artifact.
    """
    if run_dir:
        return Path(run_dir) / "auth" / "results.json"
    return _HARNESS_ROOT / "test-reports" / "_local" / "auth" / "results.json"


# ──────────────────────── JWT forging helper ─────────────────────────────────


def _b64url(data: bytes) -> str:
    """URL-safe base64 with padding stripped — the JWT canonical form."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def make_jwt(
    sub: str = "harness-test-sub",
    groups: list[str] | None = None,
    exp: int | None = None,
    extra: dict | None = None,
) -> str:
    """Build a Cognito-shaped JWT WITHOUT a real signature.

    Mirrors `tests/conftest.py::make_jwt` so the auth layer probes the
    deployed API the same way the project's existing security tests probe
    the lambda locally. `api_handler._caller_claims` decodes the middle
    segment only — it does NOT verify the signature (documented unsafe per
    AC11 / CLAUDE.local.md). The fake signature is fine for any test that
    exercises the deployed contract.

    Args:
        sub: Cognito `sub` claim (user id). Free-form for tests.
        groups: optional `cognito:groups` list (e.g. ["ciso"]).
        exp: optional `exp` claim (unix seconds). When set, the
            expired-token test uses a past value. When None, no `exp` is
            included — the API does not require it to decode the payload.
        extra: optional extra payload fields (token_use, iss, etc.).

    Returns:
        A `header.payload.signature` JWT string. The signature segment is
        the literal text `fake-signature` base64-encoded — so the token
        parses but does not verify against any Cognito public key.
    """
    header = {"alg": "RS256", "typ": "JWT", "kid": "harness-test"}
    payload: dict = {"sub": sub, "cognito:username": sub, "token_use": "id"}
    if groups is not None:
        payload["cognito:groups"] = list(groups)
    if exp is not None:
        payload["exp"] = int(exp)
    if extra:
        payload.update(extra)
    return ".".join(
        [
            _b64url(json.dumps(header, separators=(",", ":")).encode()),
            _b64url(json.dumps(payload, separators=(",", ":")).encode()),
            _b64url(b"fake-signature"),
        ]
    )


# ─────────────────────────── base-url fixtures ───────────────────────────────


@pytest.fixture(scope="session")
def target_base_url() -> str:
    """Public CloudFront URL of the deployed app. Strips trailing slash."""
    url = os.environ.get(
        "TARGET_BASE_URL", "https://d5u0vv1zl3eqd.cloudfront.net"
    ).strip()
    return url.rstrip("/")


@pytest.fixture(scope="session")
def api_base_url() -> str:
    """API Gateway base URL.

    Read order:
      1. `API_BASE_URL` env (preferred — operator sets it explicitly).
      2. `TARGET_API_URL` env (legacy name in the spec).
      3. Fallback: `${TARGET_BASE_URL}/api`.
    """
    explicit = os.environ.get("API_BASE_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    legacy = os.environ.get("TARGET_API_URL", "").strip()
    if legacy:
        return legacy.rstrip("/")
    base = os.environ.get(
        "TARGET_BASE_URL", "https://d5u0vv1zl3eqd.cloudfront.net"
    ).strip()
    return f"{base.rstrip('/')}/api"


@pytest.fixture(scope="session")
def chat_function_url() -> str | None:
    """Lambda Function URL for `/chat`. May be None.

    `/chat` is served by a Function URL (AuthType=NONE) rather than API
    Gateway to dodge the 29s integration timeout. Auth tests that hit `/chat`
    skip when this env var is unset.
    """
    url = os.environ.get("CHAT_FUNCTION_URL", "").strip()
    return url.rstrip("/") if url else None


# ───────────────────────────── identities ────────────────────────────────────


@pytest.fixture(scope="session")
def identities() -> dict:
    """The four demo Cognito identities, session-scoped.

    If `DEMO_PASSWORD` is unset, the call raises `MissingPasswordError`. We
    re-raise it as `pytest.skip(... allow_module_level=True)` so every auth
    test is reported as "skipped: identity unavailable" instead of erroring
    on the first request.

    Returns:
        dict[Persona, Identity] keyed by Persona enum.
    """
    from src.identity.cognito_auth import (
        CognitoAuthError,
        MissingPasswordError,
        fetch_all,
    )

    try:
        return fetch_all()
    except MissingPasswordError as exc:
        pytest.skip(
            f"DEMO_PASSWORD not set — auth layer cannot acquire IdTokens: {exc}",
            allow_module_level=True,
        )
    except CognitoAuthError as exc:
        pytest.skip(
            f"Cognito auth failed — auth layer cannot acquire IdTokens: {exc}",
            allow_module_level=True,
        )


# ─────────────────────────── http session + throttle ─────────────────────────


# Module-level throttle state. Shared across all tests in the run. Independent
# of the fuzz layer's throttle — each pytest invocation has its own
# `_LAST_REQUEST_TS` reference.
_LAST_REQUEST_TS: list[float] = [0.0]
_THROTTLE_INTERVAL_SECONDS = 0.2  # 5 RPS cap, matches fuzz layer.


def _throttle() -> None:
    now = time.monotonic()
    delta = now - _LAST_REQUEST_TS[0]
    if delta < _THROTTLE_INTERVAL_SECONDS:
        time.sleep(_THROTTLE_INTERVAL_SECONDS - delta)
    _LAST_REQUEST_TS[0] = time.monotonic()


@pytest.fixture(scope="session")
def http_session() -> Generator[requests.Session, None, None]:
    """A `requests.Session` configured for the auth layer.

    Default timeout is enforced via a wrapper around `request()` rather than
    a `Session` attribute, because `requests` does not honor a Session-level
    `timeout` (the kwarg has to be on each call).
    """
    sess = requests.Session()
    original_request = sess.request

    def _wrapped_request(method, url, **kwargs):
        _throttle()
        kwargs.setdefault("timeout", 10)
        # Auth tests are intentionally probing forbidden territory — do NOT
        # follow redirects automatically. A 302 to /signin is itself a result.
        kwargs.setdefault("allow_redirects", False)
        return original_request(method, url, **kwargs)

    sess.request = _wrapped_request  # type: ignore[assignment]
    yield sess
    sess.close()


# ─────────────────────────── results writer ──────────────────────────────────


class AuthResultsWriter:
    """Accumulates auth `TestResult`-shaped rows and writes them at session end.

    Shape matches `src/coverage/builder.py::TestResult`: one dict per test
    with keys `test_id`, `status`, `layer`, `target_kind`, `target_id`,
    optional `persona`, `severity`, `evidence_path`, `skipped_reason`,
    `duration_seconds`. The builder's `load_results` reads this back when
    the orchestrator (task 25) renders the report.
    """

    def __init__(self) -> None:
        self._rows: list[dict] = []

    def record(self, row: dict) -> None:
        """Stash one row. Caller is responsible for the shape."""
        self._rows.append(dict(row))

    def rows(self) -> list[dict]:
        """Return a copy of the accumulated rows (for inspection in tests)."""
        return list(self._rows)

    def write(self, path: Path) -> None:
        """Dump the accumulated rows to `path` as a JSON list.

        Sort by test_id so the file is byte-stable across runs that hit the
        same set of tests (lets diff-from-last-green stay clean).
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = sorted(self._rows, key=lambda r: r.get("test_id", ""))
        path.write_text(
            json.dumps(rows, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


@pytest.fixture(scope="session")
def results_writer() -> Generator[AuthResultsWriter, None, None]:
    """Session-scoped writer. Drained to disk by `pytest_sessionfinish`."""
    writer = AuthResultsWriter()
    yield writer
    # The actual write happens in pytest_sessionfinish below so a session
    # interrupt (KeyboardInterrupt, --maxfail) still flushes what we have.


_LIVE_WRITER: dict[str, AuthResultsWriter] = {}


@pytest.fixture(scope="session", autouse=True)
def _capture_writer(results_writer: AuthResultsWriter) -> AuthResultsWriter:
    """Side-channel to give `pytest_sessionfinish` the writer instance."""
    _LIVE_WRITER["writer"] = results_writer
    return results_writer


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Drain the auth results writer to disk at session end.

    Also writes the zero-shape `cost.json` so the orchestrator (task 25)
    can aggregate every layer's spend uniformly. The auth layer's only
    Bedrock spend is the optional forged-token /chat probe (capped at
    0-2 calls per spec §5.3) — we record zero by default and let the
    individual tests bump the dict if they actually call Bedrock.
    """
    writer = _LIVE_WRITER.get("writer")
    if writer is None:
        return
    out_path = auth_results_path(os.environ.get("RUN_DIR"))
    try:
        writer.write(out_path)
    except OSError as exc:
        # Don't fail the session on a write error — surface it so the
        # operator sees the partial state.
        print(f"warning: auth results write failed: {exc}", file=sys.stderr)

    cost_path = out_path.parent / "cost.json"
    try:
        cost_path.write_text(
            json.dumps(
                {"total_usd": 0.0, "per_layer_usd": {"auth": 0.0}, "probe_counts": {}},
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"warning: auth cost stash failed: {exc}", file=sys.stderr)


# ─────────────────── shared classification + emission helpers ────────────────


# Severity for "blocked persona reached a 2xx" — direct privilege escalation.
SEVERITY_PRIV_ESC_HIGH = "high"
# Severity for "API crashed (5xx) on an unauthorized probe" — should reject,
# not panic.
SEVERITY_API_CRASH_MEDIUM = "medium"
# Severity for "API returned 200 for an expired token" — silent acceptance.
SEVERITY_EXPIRED_ACCEPTED_HIGH = "high"


def classify_cross_persona_response(status_code: int) -> tuple[str, str | None]:
    """Map an HTTP status to a (CellStatus, severity) tuple for cross-persona.

    Rules (mirrored from the task-14 prompt):
      - 401 / 403: PASS (RBAC fired). No severity.
      - 2xx:        FAIL severity HIGH (privilege escalation).
      - 5xx:        FAIL severity MEDIUM (API crashed on unauthorized probe).
      - other:      FAIL severity MEDIUM (unexpected, not what the contract
                     promises).
    """
    if status_code in (401, 403):
        return "pass", None
    if 200 <= status_code < 300:
        return "fail", SEVERITY_PRIV_ESC_HIGH
    if 500 <= status_code < 600:
        return "fail", SEVERITY_API_CRASH_MEDIUM
    return "fail", SEVERITY_API_CRASH_MEDIUM


def classify_expired_token_response(status_code: int) -> tuple[str, str | None]:
    """Map an HTTP status to a (CellStatus, severity) tuple for expired-token.

    Rules (per the task-14 prompt §4):
      - 401 / 403: PASS — the API checks `exp` (good).
      - 2xx:        FAIL severity HIGH — the API silently accepts an
                     expired token (documented-unsafe but worth flagging
                     loudly).
      - 5xx:        FAIL severity MEDIUM — API crashed on an expired token.
      - other:      FAIL severity MEDIUM.
    """
    if status_code in (401, 403):
        return "pass", None
    if 200 <= status_code < 300:
        return "fail", SEVERITY_EXPIRED_ACCEPTED_HIGH
    if 500 <= status_code < 600:
        return "fail", SEVERITY_API_CRASH_MEDIUM
    return "fail", SEVERITY_API_CRASH_MEDIUM


__all__ = [
    "AuthResultsWriter",
    "SEVERITY_API_CRASH_MEDIUM",
    "SEVERITY_EXPIRED_ACCEPTED_HIGH",
    "SEVERITY_PRIV_ESC_HIGH",
    "api_routes",
    "auth_results_path",
    "classify_cross_persona_response",
    "classify_expired_token_response",
    "make_jwt",
    "manifest",
    "persona_ids",
]
