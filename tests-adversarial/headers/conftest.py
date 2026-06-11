"""Pytest fixtures for the headers / transport-security layer (Block B).

Why a separate conftest
-----------------------
The headers layer is structurally similar to `auth/` and `fuzz/`: it needs
base URLs, a `requests.Session` with a sane timeout + RPS throttle, and a
results writer that drains to `${RUN_DIR}/headers/results.json` at session end.
Each layer keeps its own conftest so a standalone `pytest headers/` run still
works.

Layer scope
-----------
The headers layer covers checklist items #23 (plaintext transmission), #24
(weak crypto algorithms), #31 (security headers), #35 (CORS), #55 (CSRF),
and #56 (clickjacking). All probes are read-only or rejected-by-design, so
there's no `--include-destructive` toggle — every test runs by default.

Bedrock cost
------------
The headers layer makes zero Bedrock calls. The `cost.json` stash at session
end is all-zero by design — it exists so the orchestrator (`scripts/run_all.py`)
can aggregate every layer's cost dict uniformly.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Generator
from functools import lru_cache
from pathlib import Path

import pytest
import requests

# Make `src.identity.cognito_auth` and harness `scripts.*` importable when
# pytest is invoked from the `headers/` subdirectory.
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


def api_routes() -> list[dict]:
    """All api_routes from the manifest, in declaration order."""
    return list(manifest()["api_routes"])


# ────────────────────────────── path helpers ─────────────────────────────────


def headers_results_path(run_dir: str | Path | None) -> Path:
    """Where the headers layer writes its `results.json`.

    Mirrors the auth/fuzz convention: when `RUN_DIR` is set (orchestrator
    runs), write under `${RUN_DIR}/headers/results.json`; otherwise fall back
    to `test-reports/_local/headers/results.json` so a standalone
    `pytest headers/` run still produces an artifact.
    """
    if run_dir:
        return Path(run_dir) / "headers" / "results.json"
    return _HARNESS_ROOT / "test-reports" / "_local" / "headers" / "results.json"


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
    """API Gateway invoke URL (the host that actually serves /conversations,
    /findings, /token-usage, etc.).

    Read order:
      1. `API_BASE_URL` env (preferred, operator sets it explicitly).
      2. `TARGET_API_URL` env (legacy name).

    No fallback to `${TARGET_BASE_URL}/api`. CloudFront does NOT proxy
    /api/* to API Gateway in this deployment, so a fallback URL returns
    the SPA's index.html for every API request and the classifiers
    silently misread that as "API accepts X". Source of truth: CFN
    export dev-st21arbiter-poc-ApiEndpoint. Module-skips if neither
    var is set.
    """
    explicit = os.environ.get("API_BASE_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    legacy = os.environ.get("TARGET_API_URL", "").strip()
    if legacy:
        return legacy.rstrip("/")
    pytest.skip(
        "layer requires API_BASE_URL or TARGET_API_URL to be set "
        "(CFN export dev-st21arbiter-poc-ApiEndpoint). CloudFront does "
        "not proxy /api/* in this deployment.",
        allow_module_level=True,
    )


@pytest.fixture(scope="session")
def chat_function_url() -> str | None:
    """Lambda Function URL for `/chat`. May be None.

    `/chat` is served by a Function URL (AuthType=NONE) rather than API
    Gateway to dodge the 29s integration timeout. Headers tests that hit
    `/chat` skip when this env var is unset.
    """
    url = os.environ.get("CHAT_FUNCTION_URL", "").strip()
    return url.rstrip("/") if url else None


# ───────────────────────────── identities ────────────────────────────────────


@pytest.fixture(scope="session")
def identities() -> dict:
    """The four demo Cognito identities, session-scoped.

    Skipped at module level when DEMO_PASSWORD is unset — without a real
    Authorization header the CSRF probe (which needs to compare cookie-only
    vs token-only) can't draw a real distinction. Tests that don't need a
    live identity can call `_throttle()` directly and skip this fixture.
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
            f"DEMO_PASSWORD not set — headers layer cannot acquire IdTokens: {exc}",
            allow_module_level=True,
        )
    except CognitoAuthError as exc:
        pytest.skip(
            f"Cognito auth failed — headers layer cannot acquire IdTokens: {exc}",
            allow_module_level=True,
        )


# ─────────────────────────── http session + throttle ─────────────────────────


# Module-level throttle state. Shared across all tests in the run. Independent
# of the other layers — each pytest invocation has its own `_LAST_REQUEST_TS`.
_LAST_REQUEST_TS: list[float] = [0.0]
_THROTTLE_INTERVAL_SECONDS = 0.2  # 5 RPS cap — matches auth/fuzz convention.


def _throttle() -> None:
    """Sleep enough to keep total RPS at or below 1/_THROTTLE_INTERVAL_SECONDS."""
    now = time.monotonic()
    delta = now - _LAST_REQUEST_TS[0]
    if delta < _THROTTLE_INTERVAL_SECONDS:
        time.sleep(_THROTTLE_INTERVAL_SECONDS - delta)
    _LAST_REQUEST_TS[0] = time.monotonic()


@pytest.fixture(scope="session")
def http_session() -> Generator[requests.Session, None, None]:
    """A `requests.Session` configured for the headers layer.

    Default timeout is enforced via a wrapper around `request()` rather than
    a `Session` attribute, because `requests` does not honor a Session-level
    `timeout` (the kwarg has to be on each call).

    Headers tests need to inspect redirects (e.g. http → https) — so we
    default `allow_redirects=False`. Tests that want to follow a chain can
    override per-call.
    """
    sess = requests.Session()
    original_request = sess.request

    def _wrapped_request(method, url, **kwargs):
        _throttle()
        kwargs.setdefault("timeout", 10)
        kwargs.setdefault("allow_redirects", False)
        return original_request(method, url, **kwargs)

    sess.request = _wrapped_request  # type: ignore[assignment]
    yield sess
    sess.close()


# ─────────────────────────── results writer ──────────────────────────────────


class HeadersResultsWriter:
    """Accumulates headers `TestResult`-shaped rows and writes them at session
    end. Shape matches `src/coverage/builder.py::TestResult`.
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
        same set of tests.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = sorted(self._rows, key=lambda r: r.get("test_id", ""))
        path.write_text(
            json.dumps(rows, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


@pytest.fixture(scope="session")
def results_writer() -> Generator[HeadersResultsWriter, None, None]:
    """Session-scoped writer. Drained to disk by `pytest_sessionfinish`."""
    writer = HeadersResultsWriter()
    yield writer
    # The actual write happens in pytest_sessionfinish below so a session
    # interrupt (KeyboardInterrupt, --maxfail) still flushes what we have.


_LIVE_WRITER: dict[str, HeadersResultsWriter] = {}


@pytest.fixture(scope="session", autouse=True)
def _capture_writer(results_writer: HeadersResultsWriter) -> HeadersResultsWriter:
    """Side-channel to give `pytest_sessionfinish` the writer instance."""
    _LIVE_WRITER["writer"] = results_writer
    return results_writer


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Drain the headers results writer to disk at session end.

    Also writes a zero-shape `cost.json` because the layer makes no Bedrock
    calls. The orchestrator (`scripts/run_all.py::_aggregate_cost`) sums
    every layer's cost.json regardless of whether the layer ran, so the
    presence of the file is what proves the layer's pass.
    """
    writer = _LIVE_WRITER.get("writer")
    if writer is None:
        return
    out_path = headers_results_path(os.environ.get("RUN_DIR"))
    try:
        writer.write(out_path)
    except OSError as exc:
        print(f"warning: headers results write failed: {exc}", file=sys.stderr)

    cost_path = out_path.parent / "cost.json"
    try:
        cost_path.write_text(
            json.dumps(
                {
                    "total_usd": 0.0,
                    "per_layer_usd": {"headers": 0.0},
                    "probe_counts": {},
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"warning: headers cost stash failed: {exc}", file=sys.stderr)


# ─────────────────── shared evidence-path helper ────────────────────────────


def evidence_path_for(test_id: str) -> str:
    """Stable evidence pointer per layer's convention.

    Mirrors auth/fuzz: every FAIL row points back at its own row in the
    layer's results.json. The renderer dereferences this for the report
    drill-down. AC20 requires every FAIL to have an `evidence_path`.
    """
    return f"headers/results.json#{test_id}"


__all__ = [
    "HeadersResultsWriter",
    "api_routes",
    "evidence_path_for",
    "headers_results_path",
    "manifest",
]
