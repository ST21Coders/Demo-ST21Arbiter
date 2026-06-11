"""Pytest fixtures for the logic / state layer (Block F).

Layer scope
-----------
Covers checklist items #46 (race conditions / TOCTOU), #50 (excessive data
exposure), and #61 (workflow bypass on the action lifecycle).

Why a dedicated conftest
------------------------
The logic layer is sequential by nature:

  * Workflow probes consume + reset state on the same action_id (approve →
    expect rejection on the next approve → reset by rejecting the action).
  * The race probe owns its target for the duration of the fan-out.
  * The field-exposure walker iterates per-persona per-route.

So we run as multiple personas (CISO + SOC needed for some workflow tests
and for field-exposure cross-checks). The HTTP session uses a 10 s timeout
matching auth/fuzz/headers — the logic layer doesn't generate sustained
load.

A 5 RPS in-session throttle (configurable via `--logic-rps`) keeps probe
pacing below anything that could trip a rate-limit response from the API
and contaminate the verdict.

Bedrock cost
------------
The default-on probes make **zero** Bedrock calls — no probe routes through
`/chat`. The race-condition test does create a conversation via `POST /chat`,
which costs one Bedrock invocation per run; this is bounded and accounted
for in the LayerBudget shape in `scripts/run_all.py`.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections.abc import Generator
from functools import lru_cache
from pathlib import Path

import pytest
import requests

# Make `src.identity.cognito_auth` and `logic.classifiers` importable when
# pytest is invoked from the `logic/` subdirectory.
_HARNESS_ROOT = Path(__file__).resolve().parent.parent
if str(_HARNESS_ROOT) not in sys.path:
    sys.path.insert(0, str(_HARNESS_ROOT))

from logic.classifiers import LOGIC_RPS  # noqa: E402


# ─────────────────────────────── manifest ────────────────────────────────────


_MANIFEST_PATH = _HARNESS_ROOT / "src" / "coverage" / "manifest.json"


@lru_cache(maxsize=1)
def manifest() -> dict:
    """Read and cache the coverage manifest."""
    return json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))


def api_routes() -> list[dict]:
    """All api_routes from the manifest, in declaration order."""
    return list(manifest()["api_routes"])


# ────────────────────────────── path helpers ─────────────────────────────────


def logic_results_path(run_dir: str | Path | None) -> Path:
    """Where the logic layer writes its `results.json`.

    Mirrors the dos / auth / fuzz / headers convention: when `RUN_DIR` is set
    (orchestrator runs), write under `${RUN_DIR}/logic/results.json`;
    otherwise fall back to `test-reports/_local/logic/results.json` so a
    standalone `pytest logic/` run still produces an artifact.
    """
    if run_dir:
        return Path(run_dir) / "logic" / "results.json"
    return _HARNESS_ROOT / "test-reports" / "_local" / "logic" / "results.json"


# ────────────────────────────── CLI flags ────────────────────────────────────


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add the logic layer's CLI flags.

    `--logic-rps` lets an operator turn the throttle down further on a
    flaky environment. The default is the layer's documented 5 RPS.
    """
    parser.addoption(
        "--logic-rps",
        action="store",
        type=int,
        default=LOGIC_RPS,
        help=(
            f"Per-session request rate for the logic layer (default {LOGIC_RPS}). "
            f"Lower values pace probes further apart; values < 1 are clamped to 1."
        ),
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
    Gateway. Tests that hit `/chat` skip when this env var is unset.
    """
    url = os.environ.get("CHAT_FUNCTION_URL", "").strip()
    return url.rstrip("/") if url else None


# ─────────────────────────── identities (CISO + SOC) ─────────────────────────


@pytest.fixture(scope="session")
def identities() -> dict:
    """All four demo Cognito identities.

    The logic layer needs CISO for action-workflow probes (CISO is the
    only persona that can override the approval chain per the handler at
    line 1606) and SOC for field-exposure cross-checks (a SOC response
    should not contain CISO's groups). We fetch all four so the fixture
    shape matches every other layer and cross-imports keep working.

    Skipped at module level when DEMO_PASSWORD is unset — without a real
    Authorization header the API-Gateway tests would all return 401.
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
            f"DEMO_PASSWORD not set — logic layer cannot acquire IdTokens: {exc}",
            allow_module_level=True,
        )
    except CognitoAuthError as exc:
        pytest.skip(
            f"Cognito auth failed — logic layer cannot acquire IdTokens: {exc}",
            allow_module_level=True,
        )


def _auth_header(identities: dict, persona_id: str) -> dict:
    """`{Authorization: "Bearer <persona IdToken>"}` for the given persona."""
    from src.identity.cognito_auth import Persona

    persona = Persona(persona_id)
    return {"Authorization": f"Bearer {identities[persona].id_token}"}


@pytest.fixture(scope="session")
def ciso_auth_header(identities: dict) -> dict:
    """CISO bearer header. Workflow probes run as CISO so the chain-override
    branch in the API handler is exercised when needed.
    """
    return _auth_header(identities, "ciso")


@pytest.fixture(scope="session")
def soc_auth_header(identities: dict) -> dict:
    """SOC bearer header. Field-exposure cross-checks compare SOC responses
    against the CISO identity so a leaked persona group is visible.
    """
    return _auth_header(identities, "soc")


@pytest.fixture(scope="session")
def persona_auth_headers(identities: dict) -> dict[str, dict]:
    """`{persona_id: {Authorization: ...}}` for each of the four personas.

    The field-exposure walker iterates this map so every persona's view of
    every GET endpoint is sampled.
    """
    return {
        "ciso": _auth_header(identities, "ciso"),
        "soc": _auth_header(identities, "soc"),
        "grc": _auth_header(identities, "grc"),
        "employee": _auth_header(identities, "employee"),
    }


@pytest.fixture(scope="session")
def persona_emails(identities: dict) -> dict[str, str]:
    """`{persona_id: email_claim_from_idtoken}` for each persona.

    The field-exposure classifier needs the caller's own email to decide
    whether an `email` field in a response is a cross-user leak vs the
    caller's own. We resolve it from the Cognito identity once at session
    start so the per-probe loop doesn't decode the JWT each time.
    """
    # The Identity dataclass exposes `username`, which the deployed user
    # pool's `UsernameAttributes = ["email"]` configuration guarantees is the
    # caller's email (cognito_auth.py module docstring lines 81-84). No JWT
    # decode needed here.
    out: dict[str, str] = {}
    for persona_id, identity in identities.items():
        pid = persona_id.value if hasattr(persona_id, "value") else str(persona_id)
        out[pid] = getattr(identity, "username", "") or ""
    return out


# ─────────────────────────── http session ────────────────────────────────────


class _ThrottledSession(requests.Session):
    """A `requests.Session` that paces outbound requests to at most `rps`
    requests per second.

    Implementation: a lock + a "next earliest" timestamp. Each request
    blocks until the timestamp is reached, then advances it by `1/rps`.
    Thread-safe so the race-condition tests' `ThreadPoolExecutor` doesn't
    accidentally bypass the throttle from worker threads.

    Used in place of `requests.Session()` so the logic layer's pacing is
    enforced at the transport boundary rather than scattered through each
    test module.
    """

    def __init__(self, rps: int) -> None:
        super().__init__()
        self._min_interval_s = 1.0 / max(1, rps)
        self._next_at = 0.0
        self._lock = threading.Lock()

    def request(self, method: str, url: str, **kwargs):  # type: ignore[override]
        kwargs.setdefault("timeout", 10)
        with self._lock:
            now = time.monotonic()
            wait = self._next_at - now
            if wait > 0:
                time.sleep(wait)
            self._next_at = max(now, self._next_at) + self._min_interval_s
        return super().request(method, url, **kwargs)


@pytest.fixture(scope="session")
def http_session(
    request: pytest.FixtureRequest,
) -> Generator[requests.Session, None, None]:
    """A throttled `requests.Session` for the logic layer.

    10 s default timeout — same as auth / fuzz / headers. Throttle is
    enforced via the `_ThrottledSession` subclass so race-condition fan-outs
    can't bypass it from worker threads.
    """
    raw_rps = int(request.config.getoption("--logic-rps"))
    sess = _ThrottledSession(rps=max(1, raw_rps))
    yield sess
    sess.close()


@pytest.fixture(scope="session")
def logic_rps(request: pytest.FixtureRequest) -> int:
    """The effective `--logic-rps` value (post-clamp)."""
    return max(1, int(request.config.getoption("--logic-rps")))


# ─────────────────────────── results writer ──────────────────────────────────


class LogicResultsWriter:
    """Accumulates logic `TestResult`-shaped rows and writes them at session
    end. Shape matches `src/coverage/builder.py::TestResult`.
    """

    def __init__(self) -> None:
        self._rows: list[dict] = []
        self._lock = threading.Lock()

    def record(self, row: dict) -> None:
        """Stash one row. Caller is responsible for the shape.

        Thread-safe so the race-condition test can record from worker
        threads without losing rows.
        """
        with self._lock:
            self._rows.append(dict(row))

    def rows(self) -> list[dict]:
        """Return a copy of the accumulated rows (for inspection in tests)."""
        with self._lock:
            return list(self._rows)

    def write(self, path: Path) -> None:
        """Dump the accumulated rows to `path` as a JSON list.

        Sort by test_id so the file is byte-stable across runs that hit the
        same set of tests.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            rows = sorted(self._rows, key=lambda r: r.get("test_id", ""))
        path.write_text(
            json.dumps(rows, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


@pytest.fixture(scope="session")
def results_writer() -> Generator[LogicResultsWriter, None, None]:
    """Session-scoped writer. Drained to disk by `pytest_sessionfinish`."""
    writer = LogicResultsWriter()
    yield writer


_LIVE_WRITER: dict[str, LogicResultsWriter] = {}


@pytest.fixture(scope="session", autouse=True)
def _capture_writer(results_writer: LogicResultsWriter) -> LogicResultsWriter:
    """Side-channel to give `pytest_sessionfinish` the writer instance."""
    _LIVE_WRITER["writer"] = results_writer
    return results_writer


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Drain the logic results writer to disk at session end.

    Also writes a zero-shape `cost.json`: the layer's default-on probes
    make no Bedrock calls (workflow probes hit `/actions/*` endpoints
    which never invoke a model; race probes hit `/conversations/{id}`
    delete which is DDB-only; the one /chat call in the race-on-delete
    test produces a single short Bedrock invocation that's bounded and
    documented in the LayerBudget).
    """
    writer = _LIVE_WRITER.get("writer")
    if writer is None:
        return
    out_path = logic_results_path(os.environ.get("RUN_DIR"))
    try:
        writer.write(out_path)
    except OSError as exc:
        print(f"warning: logic results write failed: {exc}", file=sys.stderr)

    cost_path = out_path.parent / "cost.json"
    try:
        cost_path.write_text(
            json.dumps(
                {
                    "total_usd": 0.0,
                    "per_layer_usd": {"logic": 0.0},
                    "probe_counts": {},
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"warning: logic cost stash failed: {exc}", file=sys.stderr)


# ─────────────────── shared evidence-path helper ─────────────────────────────


def evidence_path_for(test_id: str) -> str:
    """Stable evidence pointer per layer's convention.

    Mirrors auth/fuzz/headers/dos: every FAIL row points back at its own row
    in the layer's results.json. AC20 requires every FAIL to have an
    `evidence_path`.
    """
    return f"logic/results.json#{test_id}"


__all__ = [
    "LogicResultsWriter",
    "api_routes",
    "evidence_path_for",
    "logic_results_path",
    "manifest",
]
