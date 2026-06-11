"""Pytest fixtures for the fault-injection layer (Block H).

Layer scope
-----------
Covers checklist items #43 (fail-open logic), #45 (swallowed errors),
#47 (inconsistent state after partial failure), #53 (unsafe consumption
of third-party APIs), and #74 (LLM insecure output handling).

Why a dedicated conftest
------------------------
The fault layer's surface differs from the rest of the harness:

  * True fault injection (killing a Lambda mid-request, swapping a
    downstream response in flight) requires AWS Fault Injection
    Simulator or Lambda extensions — out of scope for a black-box
    harness. We probe the client-observable side instead: send a
    deliberately-malformed / partial / crafted request and assert the
    API's response shape is safe.
  * Tests run as **CISO only** — the layer's probes target either
    auth-failure boundaries (no persona needed beyond a baseline real
    token) or destructive state machines that are only fully reachable
    via the CISO override path.
  * The HTTP session uses a **30 s timeout** because the
    third-party / specialist-latency probes legitimately wait for a hang
    detection.

Bedrock cost
------------
The default-on probes make **bounded** Bedrock calls — the LLM-output
probes POST to ``/chat`` (3 probes × 1 short turn each ≈ 3 short Bedrock
invocations per run). The layer's LayerBudget in ``scripts/run_all.py``
keeps token-counts at zero so the cost-cap preflight doesn't false-fire;
actual Bedrock cost attribution is handled upstream by the LLM layer's
pricing path.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Generator
from functools import lru_cache
from pathlib import Path

import pytest
import requests

# Make `src.identity.cognito_auth` and `fault.classifiers` importable when
# pytest is invoked from the `fault/` subdirectory.
_HARNESS_ROOT = Path(__file__).resolve().parent.parent
if str(_HARNESS_ROOT) not in sys.path:
    sys.path.insert(0, str(_HARNESS_ROOT))


# ─────────────────────────────── manifest ────────────────────────────────────


_MANIFEST_PATH = _HARNESS_ROOT / "src" / "coverage" / "manifest.json"


@lru_cache(maxsize=1)
def manifest() -> dict:
    """Read and cache the coverage manifest."""
    return json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))


def api_routes() -> list[dict]:
    """All api_routes from the manifest, in declaration order."""
    return list(manifest()["api_routes"])


# ────────────────────────── deployed-resource names ──────────────────────────


# api_handler Lambda log group. Same as logging_audit layer's constant.
API_HANDLER_LOG_GROUP = "/aws/lambda/dev-st21arbiter-poc-api-handler"

# AWS region — every resource is in us-east-1 per CLAUDE.local.md.
AWS_REGION = "us-east-1"


# ────────────────────────────── path helpers ─────────────────────────────────


def fault_results_path(run_dir: str | Path | None) -> Path:
    """Where the fault layer writes its `results.json`.

    Mirrors auth/fuzz/headers/dos/logic/logging_audit convention: when
    `RUN_DIR` is set (orchestrator runs), write under
    `${RUN_DIR}/fault/results.json`; otherwise fall back to
    `test-reports/_local/fault/results.json` so a standalone
    `pytest fault/` run still produces an artifact.
    """
    if run_dir:
        return Path(run_dir) / "fault" / "results.json"
    return _HARNESS_ROOT / "test-reports" / "_local" / "fault" / "results.json"


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
    Gateway. The LLM-output / specialist probes use it; tests skip when
    this env var is unset.
    """
    url = os.environ.get("CHAT_FUNCTION_URL", "").strip()
    return url.rstrip("/") if url else None


# ─────────────────────────── identities (CISO only) ──────────────────────────


@pytest.fixture(scope="session")
def identities() -> dict:
    """All four demo Cognito identities. The fault layer only needs CISO.

    Skipped at module level when DEMO_PASSWORD is unset — without a real
    Authorization header the fail-closed probes degenerate (every probe
    would return 401 regardless of corruption).
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
            f"DEMO_PASSWORD not set — fault layer cannot acquire IdTokens: {exc}",
            allow_module_level=True,
        )
    except CognitoAuthError as exc:
        pytest.skip(
            f"Cognito auth failed — fault layer cannot acquire IdTokens: {exc}",
            allow_module_level=True,
        )


@pytest.fixture(scope="session")
def ciso_auth_header(identities: dict) -> dict:
    """`{Authorization: "Bearer <CISO IdToken>"}`."""
    from src.identity.cognito_auth import Persona

    ciso = identities[Persona.CISO]
    return {"Authorization": f"Bearer {ciso.id_token}"}


@pytest.fixture(scope="session")
def ciso_id_token(identities: dict) -> str:
    """Raw CISO IdToken — needed by fail-closed scenarios that corrupt
    a real, otherwise-valid token (e.g. flip a byte in the payload).
    """
    from src.identity.cognito_auth import Persona

    return identities[Persona.CISO].id_token


# ───────────────────────────── AWS clients ───────────────────────────────────


@pytest.fixture(scope="session")
def aws_logs_client():
    """boto3 CloudWatch Logs client for the CloudWatch-verification side of
    the error-propagation probes.

    Returns None (NOT a skip) when:
      * boto3 is unavailable.
      * AWS credentials cannot be resolved.
      * The principal lacks ``logs:FilterLogEvents`` on the api_handler log
        group.

    The error-propagation tests handle a None client by skipping just the
    CloudWatch sub-check (the main HTTP probe still runs and contributes a
    row). Compared to the logging_audit layer which module-skips on the
    same conditions, the fault layer is more permissive: most of its
    probes are HTTP-only.
    """
    try:
        import boto3
        from botocore.exceptions import (
            ClientError,
            NoCredentialsError,
            NoRegionError,
        )
    except ImportError:
        return None

    try:
        client = boto3.client("logs", region_name=AWS_REGION)
    except NoRegionError:
        return None

    # Probe credentials with a 1-event FilterLogEvents.
    try:
        client.filter_log_events(
            logGroupName=API_HANDLER_LOG_GROUP,
            limit=1,
        )
    except NoCredentialsError:
        return None
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in (
            "AccessDeniedException",
            "UnauthorizedOperation",
            "ResourceNotFoundException",
        ):
            return None
        # Anything else (throttle, transient) — return the client and let
        # tests handle it.
    return client


# ─────────────────────────── http session ────────────────────────────────────


@pytest.fixture(scope="session")
def http_session() -> Generator[requests.Session, None, None]:
    """A `requests.Session` configured for the fault layer.

    30 s default timeout — fault paths may legitimately be slow (the
    specialist-latency probe needs to wait long enough to distinguish a
    legitimate slow response from a hang).
    """
    sess = requests.Session()
    original_request = sess.request

    def _wrapped_request(method, url, **kwargs):
        kwargs.setdefault("timeout", 30)
        return original_request(method, url, **kwargs)

    sess.request = _wrapped_request  # type: ignore[assignment]
    yield sess
    sess.close()


# ─────────────────────────── results writer ──────────────────────────────────


class FaultResultsWriter:
    """Accumulates fault `TestResult`-shaped rows and writes them at session
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
        """Dump the accumulated rows to `path` as a JSON list, sorted by
        test_id so the file is byte-stable across runs.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = sorted(self._rows, key=lambda r: r.get("test_id", ""))
        path.write_text(
            json.dumps(rows, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


@pytest.fixture(scope="session")
def results_writer() -> Generator[FaultResultsWriter, None, None]:
    """Session-scoped writer. Drained to disk by `pytest_sessionfinish`."""
    writer = FaultResultsWriter()
    yield writer


_LIVE_WRITER: dict[str, FaultResultsWriter] = {}


@pytest.fixture(scope="session", autouse=True)
def _capture_writer(results_writer: FaultResultsWriter) -> FaultResultsWriter:
    """Side-channel to give `pytest_sessionfinish` the writer instance."""
    _LIVE_WRITER["writer"] = results_writer
    return results_writer


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Drain the fault results writer to disk at session end.

    Also writes a zero-shape `cost.json`. The LLM-output probes do POST to
    /chat (3 short Bedrock calls), but cost attribution is handled by the
    LLM layer's pricing path; we keep this layer's stash at zero so the
    aggregator doesn't double-count.
    """
    writer = _LIVE_WRITER.get("writer")
    if writer is None:
        return
    out_path = fault_results_path(os.environ.get("RUN_DIR"))
    try:
        writer.write(out_path)
    except OSError as exc:
        print(f"warning: fault results write failed: {exc}", file=sys.stderr)

    cost_path = out_path.parent / "cost.json"
    try:
        cost_path.write_text(
            json.dumps(
                {
                    "total_usd": 0.0,
                    "per_layer_usd": {"fault": 0.0},
                    "probe_counts": {},
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"warning: fault cost stash failed: {exc}", file=sys.stderr)


# ─────────────────── shared evidence-path helper ─────────────────────────────


def evidence_path_for(test_id: str) -> str:
    """Stable evidence pointer per layer's convention.

    Mirrors auth/fuzz/headers/dos/logic/logging_audit: every FAIL row
    points back at its own row in the layer's results.json. AC20 requires
    every FAIL to have an `evidence_path`.
    """
    return f"fault/results.json#{test_id}"


__all__ = [
    "API_HANDLER_LOG_GROUP",
    "AWS_REGION",
    "FaultResultsWriter",
    "api_routes",
    "evidence_path_for",
    "fault_results_path",
    "manifest",
]
