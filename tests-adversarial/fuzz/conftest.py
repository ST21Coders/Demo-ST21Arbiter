"""Pytest fixtures + CLI flags for the fuzz layer (task 12).

Why a conftest, not a plain test file
-------------------------------------
The fuzz layer needs three things shared across many test functions:
  1. A common identity source — the 4 demo Cognito personas fetched once
     per session (calling Cognito 4× per pytest module would burn rate
     limits).
  2. A `requests.Session` with sane defaults (10s timeout, retry off,
     leak-free).
  3. A `results_writer` that accumulates one row per test and dumps to
     `${RUN_DIR}/fuzz/results.json` at session end — matching the shape
     `src/coverage/builder.py::load_results` expects.

The `--include-destructive` flag is defined here so the orchestrator (task
25) can pass it through. Without it, POST/DELETE/PATCH/PUT routes are
collected but skipped — the dev DDB tables are shared with the demo
front-end and we don't want stray rows. Pure GET fuzz still runs.

Test isolation rules
--------------------
- Default: destructive routes skipped (orchestrator must opt in).
- Per-request 10s timeout (no infinite hangs on a deployed throttle).
- Throttle: at most 5 requests/second across the layer (a `time.sleep`
  spaced before each yield in the http_session fixture).

Fixtures provided
-----------------
target_base_url    — base URL of the deployed app (CloudFront).
api_base_url       — base URL of the API Gateway (defaults to TARGET_API_URL
                     env, falls back to `${TARGET_BASE_URL}/api`).
chat_function_url  — Function URL for /chat (CHAT_FUNCTION_URL env). May be
                     None — fuzz tests against /chat skip when unset.
identities         — dict[Persona, Identity] from cognito_auth.fetch_all().
                     Session-scoped. Fails fast if DEMO_PASSWORD unset.
auth_header        — parameterized fixture yielding one persona at a time.
                     Yields `{"Authorization": f"Bearer {id_token}"}`.
http_session       — requests.Session() with the 10s timeout default and
                     a built-in throttle.
corpus             — dict[family_id, dict] from `load_corpus(corpus_dir)`.
results_writer     — accumulates TestResult rows; writes results.json at
                     session end via `pytest_sessionfinish`.
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Generator
from pathlib import Path

import pytest
import requests

# Make `src.identity.cognito_auth` importable when pytest is invoked from the
# `fuzz/` subdirectory without first installing the harness package. The
# harness root is `tests-adversarial/`.
_HARNESS_ROOT = Path(__file__).resolve().parent.parent
if str(_HARNESS_ROOT) not in sys.path:
    sys.path.insert(0, str(_HARNESS_ROOT))

from fuzz._payloads import fuzz_results_path, load_corpus  # noqa: E402


# ────────────────────────────── CLI flags ────────────────────────────────────


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add the `--include-destructive` flag.

    Without the flag, POST / DELETE / PATCH / PUT routes are collected but
    skipped (so the test inventory remains complete in the report). The
    orchestrator passes the flag when it wants the full fuzz pass.
    """
    parser.addoption(
        "--include-destructive",
        action="store_true",
        default=False,
        help=(
            "Include destructive (POST/PUT/PATCH/DELETE) routes in the fuzz "
            "pass. Default off — those routes are skipped to avoid polluting "
            "dev DDB with harness traffic."
        ),
    )
    # Task-13 knob. Default count keeps a single layer run at ~40s wall-clock
    # under the 5 RPS throttle (8 examples × 25 routes × 0.2s). Honored by
    # `fuzz/test_hypothesis_strategies.py`.
    #
    # Note: `--hypothesis-seed` is intentionally NOT registered here — the
    # Hypothesis pytest plugin already owns that flag. Our test reads the
    # plugin's `--hypothesis-seed` value if set and falls back to the harness
    # default (HYPOTHESIS_DEFAULT_SEED, 0xA4B17E4) otherwise. This keeps the
    # generated input set identical across days unless the operator overrides
    # — required by the spec §6.4 stable-diff guarantee.
    parser.addoption(
        "--hypothesis-examples",
        action="store",
        type=int,
        default=8,
        help=(
            "Per-route examples for the Hypothesis-driven fuzz layer (default 8, "
            "max 32). Values above 32 are silently capped."
        ),
    )


# ─────────────────────────── base-url fixtures ───────────────────────────────


@pytest.fixture(scope="session")
def target_base_url() -> str:
    """Public CloudFront URL of the deployed app. Strips trailing slash."""
    url = os.environ.get("TARGET_BASE_URL", "https://d5u0vv1zl3eqd.cloudfront.net").strip()
    return url.rstrip("/")


@pytest.fixture(scope="session")
def api_base_url() -> str:
    """API Gateway base URL.

    Read order:
      1. `API_BASE_URL` env (preferred — operator sets it explicitly).
      2. `TARGET_API_URL` env (legacy name in the spec).
      3. Fallback: `${TARGET_BASE_URL}/api` — works when CloudFront fronts
         the API behind a `/api/*` route mapping.
    """
    explicit = os.environ.get("API_BASE_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    legacy = os.environ.get("TARGET_API_URL", "").strip()
    if legacy:
        return legacy.rstrip("/")
    base = os.environ.get("TARGET_BASE_URL", "https://d5u0vv1zl3eqd.cloudfront.net").strip()
    return f"{base.rstrip('/')}/api"


@pytest.fixture(scope="session")
def chat_function_url() -> str | None:
    """Lambda Function URL for `/chat`. May be None.

    `/chat` is served by a Function URL (AuthType=NONE) rather than API
    Gateway to dodge the 29s integration timeout. Tests that hit `/chat`
    skip when this env var is unset — they're documented as optional.
    """
    url = os.environ.get("CHAT_FUNCTION_URL", "").strip()
    return url.rstrip("/") if url else None


# ───────────────────────── identity & auth header ────────────────────────────


@pytest.fixture(scope="session")
def identities() -> dict:
    """The four demo Cognito identities, session-scoped.

    Calls `fetch_all()` exactly once per pytest session. If `DEMO_PASSWORD`
    is unset, the call raises `MissingPasswordError`, which we re-raise as a
    pytest skip-collection failure so every fuzz test is reported as
    "skipped: identity unavailable" instead of erroring out at the first
    request.

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
            f"DEMO_PASSWORD not set — fuzz layer cannot acquire IdTokens: {exc}",
            allow_module_level=True,
        )
    except CognitoAuthError as exc:
        pytest.skip(
            f"Cognito auth failed — fuzz layer cannot acquire IdTokens: {exc}",
            allow_module_level=True,
        )


# Parameterized auth header — pytest fans this out across all 4 personas.
# A fuzz test that takes `auth_header` runs once per persona; tests that only
# need a single token typically take a positional persona fixture instead.
@pytest.fixture(params=["ciso", "soc", "grc", "employee"], ids=lambda p: f"persona={p}")
def auth_header(request: pytest.FixtureRequest, identities: dict) -> dict:
    """`{Authorization: "Bearer <IdToken>"}` for one of the 4 personas.

    Parameterized so a single test definition runs 4×. Set `indirect=False`
    (the default) so the parameter value is the persona id string.
    """
    from src.identity.cognito_auth import Persona

    persona_id = request.param
    persona = Persona(persona_id)
    identity = identities[persona]
    return {"Authorization": f"Bearer {identity.id_token}"}


# ─────────────────────────── http session + throttle ─────────────────────────


# Module-level throttle state. Shared across all tests in the run.
_LAST_REQUEST_TS: list[float] = [0.0]  # mutable container so a fixture can update
_THROTTLE_INTERVAL_SECONDS = 0.2  # 5 RPS cap per task-12 prompt


def _throttle() -> None:
    """Sleep enough to keep total RPS at or below 1/_THROTTLE_INTERVAL_SECONDS."""
    now = time.monotonic()
    delta = now - _LAST_REQUEST_TS[0]
    if delta < _THROTTLE_INTERVAL_SECONDS:
        time.sleep(_THROTTLE_INTERVAL_SECONDS - delta)
    _LAST_REQUEST_TS[0] = time.monotonic()


@pytest.fixture(scope="session")
def http_session() -> Generator[requests.Session, None, None]:
    """A `requests.Session` configured for the fuzz layer.

    Default timeout is enforced via a wrapper around `request()` rather than
    a `Session` attribute, because `requests` does not honor a Session-level
    `timeout` (the kwarg has to be on each call).
    """
    sess = requests.Session()

    original_request = sess.request

    def _wrapped_request(method, url, **kwargs):
        _throttle()
        kwargs.setdefault("timeout", 10)  # 10s hard cap per task-12 prompt
        return original_request(method, url, **kwargs)

    sess.request = _wrapped_request  # type: ignore[assignment]
    yield sess
    sess.close()


# ─────────────────────────────── corpus ──────────────────────────────────────


@pytest.fixture(scope="session")
def corpus() -> dict:
    """Load the curated corpus from `fuzz/corpus/*.json`.

    Session-scoped — every test in the layer uses the same dict.
    """
    return load_corpus(Path(__file__).resolve().parent / "corpus")


# ─────────────────────────── results writer ──────────────────────────────────


class FuzzResultsWriter:
    """Accumulates fuzz `TestResult`-shaped rows and writes them at session end.

    Shape matches `src/coverage/builder.py::TestResult` (the layer-builder
    contract): one dict per test with keys `test_id`, `status`, `layer`,
    `target_kind`, `target_id`, optional `persona`, `severity`, `evidence_path`,
    `duration_seconds`. The builder's `load_results` will read this back when
    the orchestrator renders the report.
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
        """Dump the accumulated rows to `path` as a JSON list."""
        path.parent.mkdir(parents=True, exist_ok=True)
        # Sort by test_id so the file is byte-stable across runs that hit the
        # same set of tests (lets diff-from-last-green stay clean).
        rows = sorted(self._rows, key=lambda r: r.get("test_id", ""))
        path.write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


@pytest.fixture(scope="session")
def results_writer() -> Generator[FuzzResultsWriter, None, None]:
    """Session-scoped writer. Drained to disk by `pytest_sessionfinish`."""
    writer = FuzzResultsWriter()
    yield writer
    # The actual write happens in pytest_sessionfinish below so a session
    # interrupt (KeyboardInterrupt, --maxfail) still flushes what we have.


# Module-level handle so `pytest_sessionfinish` can find the writer that the
# `results_writer` fixture handed out. We can't access fixtures from
# `pytest_sessionfinish` directly because the fixture scope tear-down may
# already have run.
_LIVE_WRITER: dict[str, FuzzResultsWriter] = {}


@pytest.fixture(scope="session", autouse=True)
def _capture_writer(results_writer: FuzzResultsWriter) -> FuzzResultsWriter:
    """Side-channel to give `pytest_sessionfinish` the writer instance."""
    _LIVE_WRITER["writer"] = results_writer
    return results_writer


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Drain the fuzz results writer to disk at session end.

    Also writes the zero-shape `cost.json` so the orchestrator (task 25)
    can aggregate every layer's spend uniformly — fuzz never touches
    Bedrock so the dict is all-zero, but the file's presence proves the
    layer ran.
    """
    writer = _LIVE_WRITER.get("writer")
    if writer is None:
        return
    out_path = fuzz_results_path(os.environ.get("RUN_DIR"))
    try:
        writer.write(out_path)
    except OSError as exc:
        # Don't fail the session on a write error — surface it so the operator
        # sees the partial state.
        print(f"warning: fuzz results write failed: {exc}", file=sys.stderr)

    # Cost stash — zero shape because fuzz makes no Bedrock calls.
    cost_path = out_path.parent / "cost.json"
    try:
        cost_path.write_text(
            json.dumps(
                {"total_usd": 0.0, "per_layer_usd": {"fuzz": 0.0}, "probe_counts": {}},
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"warning: fuzz cost stash failed: {exc}", file=sys.stderr)


# ───────────────────────── destructive-test gate ─────────────────────────────


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Two-phase post-collection hook:

    Phase 1: walk every fuzz test item and tag it with `@pytest.mark.destructive`
             if its `route` parametrize value has a destructive HTTP method
             (POST / PUT / PATCH / DELETE). We can't attach the marker via
             `@pytest.mark.parametrize` because the destructive-ness depends
             on the parameter value at collection time.

    Phase 2: if `--include-destructive` is OFF, skip every item that now
             carries the destructive marker. The skipped items still appear
             in the test inventory so the coverage report shows them.

    Both phases live in conftest (not the test module) so they run before
    pytest evaluates the skip markers for execution.
    """
    from fuzz._payloads import is_destructive

    # Phase 1: attach destructive marker to destructive routes.
    for item in items:
        callspec = getattr(item, "callspec", None)
        if callspec is None:
            continue
        route = callspec.params.get("route")
        if route and is_destructive(route.get("method") or "GET"):
            item.add_marker(pytest.mark.destructive)

    # Phase 2: skip destructive items unless explicitly enabled.
    if config.getoption("--include-destructive"):
        return
    skip_marker = pytest.mark.skip(
        reason=(
            "destructive route (POST/DELETE/PATCH/PUT); "
            "pass --include-destructive to enable"
        )
    )
    for item in items:
        if "destructive" in item.keywords:
            item.add_marker(skip_marker)
