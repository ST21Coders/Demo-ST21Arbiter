"""Pytest fixtures for the features (positive end-to-end) layer.

Why a dedicated conftest
------------------------
The features layer differs from the adversarial layers above:

  * Default expected outcome is **PASS**. A FAIL row is a real feature
    regression, not a security finding. Severity tags are still applied
    (`high` = broken feature, `medium` = degraded, `low` = cosmetic).
  * The HTTP session uses a **30 s timeout** because /chat legitimately takes
    8-15 s under Bedrock. The other layers cap at 10 s.
  * Tests run as **each persona** (4 of them) for the per-persona modules
    (chat roundtrip, conversation persistence). Routing / KB / cost / token
    tests pin to CISO since the question is "does the feature work" not
    "does it work per persona".

Bedrock cost
------------
Each /chat probe touches Bedrock. Typical Nova 2 Lite spend per turn is well
under $0.001, so the worst-case (~12 chat calls × $0.001) sits inside the
1-cent layer budget the orchestrator allocates. The session-end cost.json
sums the per-call costs recorded by `cost_tracker_dict`.
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

# Make `src.identity.cognito_auth`, `features.classifiers`, and harness
# `scripts.*` importable when pytest is invoked from the `features/` subdir.
_HARNESS_ROOT = Path(__file__).resolve().parent.parent
if str(_HARNESS_ROOT) not in sys.path:
    sys.path.insert(0, str(_HARNESS_ROOT))


# ──────────────────────────── manifest helpers ───────────────────────────────


_MANIFEST_PATH = _HARNESS_ROOT / "src" / "coverage" / "manifest.json"


@lru_cache(maxsize=1)
def manifest() -> dict:
    """Read and cache the coverage manifest."""
    return json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))


def api_routes() -> list[dict]:
    """All api_routes from the manifest, in declaration order."""
    return list(manifest()["api_routes"])


# ────────────────────────────── path helpers ─────────────────────────────────


def features_results_path(run_dir: str | Path | None) -> Path:
    """Where the features layer writes its `results.json`.

    Mirrors the other layers' convention: write under
    `${RUN_DIR}/features/results.json` when RUN_DIR is set, otherwise fall
    back to `test-reports/_local/features/results.json` so standalone runs
    still produce an artifact.
    """
    if run_dir:
        return Path(run_dir) / "features" / "results.json"
    return _HARNESS_ROOT / "test-reports" / "_local" / "features" / "results.json"


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

    No fallback to `${TARGET_BASE_URL}/api`. CloudFront does NOT proxy /api/*
    to API Gateway in this deployment, so a fallback URL would return the
    SPA's index.html for every API request and the harness would falsely
    report conversation persistence and token usage as broken (regression
    incident 2026-06-11). Module-skips if neither var is set.
    """
    explicit = os.environ.get("API_BASE_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    legacy = os.environ.get("TARGET_API_URL", "").strip()
    if legacy:
        return legacy.rstrip("/")
    pytest.skip(
        "features layer requires API_BASE_URL (the API Gateway invoke URL) or "
        "TARGET_API_URL to be set. CloudFront does not proxy /api/* to the "
        "API in this deployment. Source: CFN export "
        "dev-st21arbiter-poc-ApiEndpoint.",
        allow_module_level=True,
    )


@pytest.fixture(scope="session")
def chat_function_url() -> str | None:
    """Lambda Function URL for `/chat`. May be None.

    `/chat` is served by a Function URL (AuthType=NONE) rather than API
    Gateway to dodge the 29 s integration timeout. Tests that hit `/chat`
    skip when this env var is unset.
    """
    url = os.environ.get("CHAT_FUNCTION_URL", "").strip()
    return url.rstrip("/") if url else None


# ─────────────────────────────── identities ──────────────────────────────────


@pytest.fixture(scope="session")
def identities() -> dict:
    """The four demo Cognito identities, session-scoped.

    Skipped at module level when DEMO_PASSWORD is unset — without a real
    Authorization header none of the features probes can complete. A FAIL
    here would be misleading (the harness failed to log in, not the app).
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
            f"DEMO_PASSWORD not set — features layer cannot acquire IdTokens: {exc}",
            allow_module_level=True,
        )
    except CognitoAuthError as exc:
        pytest.skip(
            f"Cognito auth failed — features layer cannot acquire IdTokens: {exc}",
            allow_module_level=True,
        )


# ─────────────────────────── http session + throttle ─────────────────────────


# Module-level throttle state. 5 RPS cap matches auth/fuzz/headers convention.
_LAST_REQUEST_TS: list[float] = [0.0]
_THROTTLE_INTERVAL_SECONDS = 0.2  # 5 RPS


def _throttle() -> None:
    """Sleep enough to keep total RPS at or below 1/_THROTTLE_INTERVAL_SECONDS."""
    now = time.monotonic()
    delta = now - _LAST_REQUEST_TS[0]
    if delta < _THROTTLE_INTERVAL_SECONDS:
        time.sleep(_THROTTLE_INTERVAL_SECONDS - delta)
    _LAST_REQUEST_TS[0] = time.monotonic()


@pytest.fixture(scope="session")
def http_session() -> Generator[requests.Session, None, None]:
    """A `requests.Session` with a 30 s default timeout for /chat.

    The 30 s default matches the chat-roundtrip budget — anything beyond is
    a feature regression and the classifier flags it MEDIUM.
    """
    sess = requests.Session()
    original_request = sess.request

    def _wrapped_request(method, url, **kwargs):
        _throttle()
        kwargs.setdefault("timeout", 30)
        return original_request(method, url, **kwargs)

    sess.request = _wrapped_request  # type: ignore[assignment]
    yield sess
    sess.close()


# ─────────────────────────── results writer ──────────────────────────────────


class FeaturesResultsWriter:
    """Accumulates features `TestResult`-shaped rows and writes them at
    session end. Shape matches `src/coverage/builder.py::TestResult`.
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
def results_writer() -> Generator[FeaturesResultsWriter, None, None]:
    """Session-scoped writer. Drained to disk by `pytest_sessionfinish`."""
    writer = FeaturesResultsWriter()
    yield writer
    # Actual write happens in pytest_sessionfinish below so a session
    # interrupt (KeyboardInterrupt, --maxfail) still flushes what we have.


_LIVE_WRITER: dict[str, FeaturesResultsWriter] = {}
_LIVE_COST: dict[str, dict] = {}


@pytest.fixture(scope="session", autouse=True)
def _capture_writer(results_writer: FeaturesResultsWriter) -> FeaturesResultsWriter:
    """Side-channel to give `pytest_sessionfinish` the writer instance."""
    _LIVE_WRITER["writer"] = results_writer
    return results_writer


# ─────────────────────────── cost tracker dict ───────────────────────────────


@pytest.fixture(scope="session")
def cost_tracker_dict() -> dict:
    """Session-scoped dict each chat-creating test appends a cost row to.

    Shape per row: {"layer": "features", "test_id": ..., "usd": float}.

    The session-finish hook sums these into the layer's `cost.json` so the
    orchestrator's `_aggregate_cost` can attribute the spend cleanly.
    """
    bucket = {"rows": []}
    _LIVE_COST["bucket"] = bucket
    return bucket


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Drain the features results writer + cost stash to disk at session end."""
    writer = _LIVE_WRITER.get("writer")
    if writer is None:
        return
    out_path = features_results_path(os.environ.get("RUN_DIR"))
    try:
        writer.write(out_path)
    except OSError as exc:
        print(f"warning: features results write failed: {exc}", file=sys.stderr)

    # Cost stash. Sum every recorded row into a single per-layer total so the
    # orchestrator's aggregate_cost reads it uniformly.
    cost_path = out_path.parent / "cost.json"
    bucket = _LIVE_COST.get("bucket", {"rows": []})
    total_usd = sum(float(r.get("usd", 0.0) or 0.0) for r in bucket["rows"])
    try:
        cost_path.write_text(
            json.dumps(
                {
                    "total_usd": round(total_usd, 6),
                    "per_layer_usd": {"features": round(total_usd, 6)},
                    "probe_counts": {"features": len(bucket["rows"])},
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"warning: features cost stash failed: {exc}", file=sys.stderr)


# ─────────────────── shared evidence-path helper ────────────────────────────


def evidence_path_for(test_id: str) -> str:
    """Stable evidence pointer per layer's convention.

    Mirrors auth/fuzz/headers/dos: every FAIL row points back at its own row
    in the layer's results.json. AC20 requires every FAIL to have an
    `evidence_path`.
    """
    return f"features/results.json#{test_id}"


__all__ = [
    "FeaturesResultsWriter",
    "api_routes",
    "evidence_path_for",
    "features_results_path",
    "manifest",
]
