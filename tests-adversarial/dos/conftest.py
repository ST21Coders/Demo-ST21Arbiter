"""Pytest fixtures + CLI flags for the DoS / rate-limit layer (Block E).

Why a dedicated conftest
------------------------
The DoS layer's surface differs from the rest of the harness:

  * Tests run as **CISO only** — a sustained burst from four personas in
    parallel would multiply the load on the dev environment by 4× for no
    extra signal (DoS is a per-route property, not a per-persona property).
  * The HTTP session uses a **30 s timeout** instead of the 10 s common to
    `auth/`/`fuzz/`/`headers/` — a legitimate slow response under load is
    still useful evidence, and the rate-limit burst itself caps at 30 s
    via `--dos-duration-seconds`.
  * The layer carries a **`--dos-rps`** / **`--dos-duration-seconds`** pair
    with hard ceilings of 100 RPS and 30 s. Without those caps a misread
    flag could DDoS the dev account.

Layer scope
-----------
Covers checklist items #51 (lack of rate limiting), #64 (application-layer
DoS via oversized bodies), and #65 (resource exhaustion via concurrent
requests).

Bedrock cost
------------
The default-on probes make **zero** Bedrock calls — `/chat` is included in
the rate-limit burst but each individual request is short-circuited by the
Lambda before it reaches Bedrock (the burst is HTTP-shaped and the
classifier only inspects status / latency). The concurrent-burst test
defaults to `POST /scan`, which also does not invoke Bedrock. Opt-in to
hitting `/chat` for the concurrent test via `--include-bedrock-dos`.
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

# Make `src.identity.cognito_auth` and `dos.classifiers` importable when
# pytest is invoked from the `dos/` subdirectory.
_HARNESS_ROOT = Path(__file__).resolve().parent.parent
if str(_HARNESS_ROOT) not in sys.path:
    sys.path.insert(0, str(_HARNESS_ROOT))

from dos.classifiers import (  # noqa: E402
    DOS_DURATION_HARD_CEILING_SECONDS,
    DOS_RPS_HARD_CEILING,
    clamp_duration_seconds,
    clamp_rps,
)


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


def dos_results_path(run_dir: str | Path | None) -> Path:
    """Where the dos layer writes its `results.json`.

    Mirrors auth/fuzz/headers convention: when `RUN_DIR` is set (orchestrator
    runs), write under `${RUN_DIR}/dos/results.json`; otherwise fall back
    to `test-reports/_local/dos/results.json` so a standalone `pytest dos/`
    run still produces an artifact.
    """
    if run_dir:
        return Path(run_dir) / "dos" / "results.json"
    return _HARNESS_ROOT / "test-reports" / "_local" / "dos" / "results.json"


# ────────────────────────────── CLI flags ────────────────────────────────────


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add the DoS layer's three CLI flags.

    `--dos-rps` and `--dos-duration-seconds` are silently clamped to their
    hard ceilings in `classifiers.clamp_*` — see the safety note at the top
    of this file. `--include-bedrock-dos` is required to make the concurrent
    test target `POST /chat` (which does cost real money).
    """
    parser.addoption(
        "--dos-rps",
        action="store",
        type=int,
        default=20,
        help=(
            f"Burst rate (requests/sec) for the rate-limit layer "
            f"(default 20, hard ceiling {DOS_RPS_HARD_CEILING}). "
            f"Values above the ceiling are silently capped."
        ),
    )
    parser.addoption(
        "--dos-duration-seconds",
        action="store",
        type=int,
        default=5,
        help=(
            f"Sustained-burst length in seconds for the rate-limit layer "
            f"(default 5, hard ceiling {DOS_DURATION_HARD_CEILING_SECONDS}). "
            f"Values above the ceiling are silently capped."
        ),
    )
    parser.addoption(
        "--include-bedrock-dos",
        action="store_true",
        default=False,
        help=(
            "Allow the concurrent-burst test to target POST /chat instead "
            "of POST /scan. /chat will incur real Bedrock cost. Default off."
        ),
    )
    # The fuzz layer already registers --include-destructive; we re-register
    # it (try/except idempotent) so a standalone `pytest dos/` run sees it.
    # Pytest raises ValueError on a duplicate option name; that's fine — we
    # only get here when running the dos layer alone.
    try:
        parser.addoption(
            "--include-destructive",
            action="store_true",
            default=False,
            help=(
                "Include destructive (POST/PUT/PATCH/DELETE) routes in the "
                "rate-limit burst. Default off — those routes are skipped to "
                "avoid polluting dev DDB with harness traffic."
            ),
        )
    except ValueError:
        pass


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
      2. `TARGET_API_URL` env (legacy name).
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
    Gateway. Tests that hit `/chat` skip when this env var is unset.
    """
    url = os.environ.get("CHAT_FUNCTION_URL", "").strip()
    return url.rstrip("/") if url else None


# ─────────────────────────── identities (CISO only) ──────────────────────────


@pytest.fixture(scope="session")
def identities() -> dict:
    """All four demo Cognito identities. The DoS layer only needs CISO, but
    we fetch all four so the fixture shape matches every other layer and
    cross-imports keep working.

    Skipped at module level when DEMO_PASSWORD is unset — without a real
    Authorization header the API-Gateway tests would all return 401, which
    isn't a meaningful DoS signal.
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
            f"DEMO_PASSWORD not set — DoS layer cannot acquire IdTokens: {exc}",
            allow_module_level=True,
        )
    except CognitoAuthError as exc:
        pytest.skip(
            f"Cognito auth failed — DoS layer cannot acquire IdTokens: {exc}",
            allow_module_level=True,
        )


@pytest.fixture(scope="session")
def ciso_auth_header(identities: dict) -> dict:
    """`{Authorization: "Bearer <CISO IdToken>"}`.

    The DoS layer pins to CISO so the burst rate isn't accidentally
    multiplied by persona fan-out. Other personas would change the surface
    only at the IAM-policy layer, which `auth/` already covers.
    """
    from src.identity.cognito_auth import Persona

    ciso = identities[Persona.CISO]
    return {"Authorization": f"Bearer {ciso.id_token}"}


# ─────────────────────────── http session ────────────────────────────────────


@pytest.fixture(scope="session")
def http_session() -> Generator[requests.Session, None, None]:
    """A `requests.Session` configured for the DoS layer.

    30 s default timeout — DoS tests legitimately take longer than the
    10 s common to the other layers. The session does NOT throttle:
    that's the whole point of the rate-limit burst. Individual tests
    enforce their own pacing via `--dos-rps`.
    """
    sess = requests.Session()
    original_request = sess.request

    def _wrapped_request(method, url, **kwargs):
        kwargs.setdefault("timeout", 30)
        return original_request(method, url, **kwargs)

    sess.request = _wrapped_request  # type: ignore[assignment]
    yield sess
    sess.close()


# ──────────────────────── CLI flag accessors ─────────────────────────────────


@pytest.fixture(scope="session")
def dos_rps(request: pytest.FixtureRequest) -> int:
    """`--dos-rps` value, clamped to the hard ceiling."""
    raw = request.config.getoption("--dos-rps")
    return clamp_rps(int(raw))


@pytest.fixture(scope="session")
def dos_duration_seconds(request: pytest.FixtureRequest) -> int:
    """`--dos-duration-seconds` value, clamped to the hard ceiling."""
    raw = request.config.getoption("--dos-duration-seconds")
    return clamp_duration_seconds(int(raw))


@pytest.fixture(scope="session")
def include_destructive(request: pytest.FixtureRequest) -> bool:
    """Whether destructive (POST/PUT/PATCH/DELETE) routes should run."""
    return bool(request.config.getoption("--include-destructive"))


@pytest.fixture(scope="session")
def include_bedrock_dos(request: pytest.FixtureRequest) -> bool:
    """Whether the concurrent-burst test may target `POST /chat`."""
    return bool(request.config.getoption("--include-bedrock-dos"))


# ─────────────────────────── results writer ──────────────────────────────────


class DosResultsWriter:
    """Accumulates DoS `TestResult`-shaped rows and writes them at session
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
        same set of tests (lets diff-from-last-green stay clean).
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = sorted(self._rows, key=lambda r: r.get("test_id", ""))
        path.write_text(
            json.dumps(rows, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


@pytest.fixture(scope="session")
def results_writer() -> Generator[DosResultsWriter, None, None]:
    """Session-scoped writer. Drained to disk by `pytest_sessionfinish`."""
    writer = DosResultsWriter()
    yield writer
    # The actual write happens in pytest_sessionfinish below so a session
    # interrupt (KeyboardInterrupt, --maxfail) still flushes what we have.


_LIVE_WRITER: dict[str, DosResultsWriter] = {}


@pytest.fixture(scope="session", autouse=True)
def _capture_writer(results_writer: DosResultsWriter) -> DosResultsWriter:
    """Side-channel to give `pytest_sessionfinish` the writer instance."""
    _LIVE_WRITER["writer"] = results_writer
    return results_writer


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Drain the DoS results writer to disk at session end.

    Also writes a zero-shape `cost.json`: the layer's default-on probes make
    no Bedrock calls. (If `--include-bedrock-dos` was passed and the
    concurrent test fired against `/chat`, the real cost is captured
    upstream by the LLM layer's pricing path — we deliberately don't
    re-account for it here to keep the per-layer attribution clean.)
    """
    writer = _LIVE_WRITER.get("writer")
    if writer is None:
        return
    out_path = dos_results_path(os.environ.get("RUN_DIR"))
    try:
        writer.write(out_path)
    except OSError as exc:
        print(f"warning: dos results write failed: {exc}", file=sys.stderr)

    cost_path = out_path.parent / "cost.json"
    try:
        cost_path.write_text(
            json.dumps(
                {
                    "total_usd": 0.0,
                    "per_layer_usd": {"dos": 0.0},
                    "probe_counts": {},
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"warning: dos cost stash failed: {exc}", file=sys.stderr)


# ─────────────────── shared evidence-path helper ─────────────────────────────


def evidence_path_for(test_id: str) -> str:
    """Stable evidence pointer per layer's convention.

    Mirrors auth/fuzz/headers: every FAIL row points back at its own row in
    the layer's results.json. AC20 requires every FAIL to have an
    `evidence_path`.
    """
    return f"dos/results.json#{test_id}"


# ─────────────────────────── small timing helper ─────────────────────────────


def measure_ms(fn) -> tuple[float, "any"]:  # type: ignore[valid-type]
    """Return ``(elapsed_ms, fn_return)``. Helper used by the test modules
    so per-request latency is captured consistently. Lives in the conftest
    so unit tests can monkey-patch it cleanly when needed.
    """
    started = time.monotonic()
    result = fn()
    return (time.monotonic() - started) * 1000.0, result


__all__ = [
    "DosResultsWriter",
    "api_routes",
    "dos_results_path",
    "evidence_path_for",
    "manifest",
    "measure_ms",
]
