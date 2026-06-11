"""Pytest fixtures for the logging / audit layer (Block G).

Layer scope
-----------
Covers checklist items #67 (insufficient logging of security events),
#68 (sensitive data in logs), and #71 (log-injection downstream verification
of Block A's log_injection corpus).

Why "logging_audit" not "logging"
---------------------------------
The directory name avoids a collision with the stdlib ``logging`` module —
pytest's collection imports the package by directory name, and any module
inside that tries ``import logging`` would otherwise resolve to ``./logging``
instead of the stdlib package. ``logging_audit`` sidesteps the problem
entirely and is more descriptive: the layer's surface is the audit trail.

IAM permissions required
------------------------
The layer makes read-only AWS calls against the deployed dev account
(669810405473 / us-east-1). Required permissions on the principal running
the harness:

  * ``logs:FilterLogEvents`` on
    ``arn:aws:logs:us-east-1:669810405473:log-group:/aws/lambda/dev-st21arbiter-poc-api-handler:*``
  * ``dynamodb:Scan`` on
    ``arn:aws:dynamodb:us-east-1:669810405473:table/dev-st21arbiter-poc-audit-log``
    (we use Scan + FilterExpression because the table has no GSI on
    timestamp alone — see ``04-storage.yaml`` lines 128-155).

If either permission is missing the layer is module-level skipped; this is
itself a signal (the harness shouldn't pretend coverage we can't verify).

Why a dedicated conftest
------------------------
The logging_audit layer's surface differs from every other layer:

  * It does NOT make HTTP probes itself — it triggers events via the API
    (using `requests`), then reads CloudWatch + DDB to verify the
    side-effects landed. Two different transports per test.
  * It runs as CISO + SOC (matches logic/ layer) — CISO for the legitimate
    "approve action" probe (which should fire an audit-log row), SOC for
    the cross-persona attempt (which should also be audited).
  * AWS clients need a region pinned and creds resolved at session start;
    the layer is module-skipped if either is missing.
  * Hard timeout per layer is 600 s (CloudWatch FilterLogEvents queries can
    legitimately take 10+ seconds each, and we run several per test).

Bedrock cost
------------
Zero. None of the default-on probes call `/chat` in a way that invokes the
master orchestrator's Bedrock model — the JWT canary test uses a real
IdToken (already obtained at preflight) and routes via `/health` / API
Gateway probes that return without touching Bedrock. The cost stash at
session end records 0.0 to keep the per-layer attribution clean.
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

# Make `src.identity.cognito_auth` and `logging_audit.classifiers` importable
# when pytest is invoked from the `logging_audit/` subdirectory.
_HARNESS_ROOT = Path(__file__).resolve().parent.parent
if str(_HARNESS_ROOT) not in sys.path:
    sys.path.insert(0, str(_HARNESS_ROOT))


# ─────────────────────────────── manifest ────────────────────────────────────


_MANIFEST_PATH = _HARNESS_ROOT / "src" / "coverage" / "manifest.json"


@lru_cache(maxsize=1)
def manifest() -> dict:
    """Read and cache the coverage manifest."""
    return json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))


# ────────────────────────── deployed-resource names ──────────────────────────


# Audit-log DynamoDB table. Matches `04-storage.yaml::AuditLogTable`.
AUDIT_LOG_TABLE_NAME = "dev-st21arbiter-poc-audit-log"

# api_handler Lambda log group. Matches `06-api.yaml::ApiHandlerLogGroup`.
API_HANDLER_LOG_GROUP = "/aws/lambda/dev-st21arbiter-poc-api-handler"

# AWS region — every resource is in us-east-1 per CLAUDE.local.md.
AWS_REGION = "us-east-1"


# ────────────────────────────── path helpers ─────────────────────────────────


def logging_audit_results_path(run_dir: str | Path | None) -> Path:
    """Where the logging_audit layer writes its `results.json`.

    Mirrors auth/fuzz/headers/dos/logic convention: when `RUN_DIR` is set
    (orchestrator runs), write under `${RUN_DIR}/logging_audit/results.json`;
    otherwise fall back to `test-reports/_local/logging_audit/results.json`
    so a standalone `pytest logging_audit/` run still produces an artifact.
    """
    if run_dir:
        return Path(run_dir) / "logging_audit" / "results.json"
    return _HARNESS_ROOT / "test-reports" / "_local" / "logging_audit" / "results.json"


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
    Gateway. The log-redaction body-field test uses /chat to inject a
    canary; it skips when this env var is unset.
    """
    url = os.environ.get("CHAT_FUNCTION_URL", "").strip()
    return url.rstrip("/") if url else None


# ─────────────────────────── identities (CISO + SOC) ─────────────────────────


@pytest.fixture(scope="session")
def identities() -> dict:
    """All four demo Cognito identities. Layer needs CISO + SOC.

    Skipped at module level when DEMO_PASSWORD is unset — without a real
    Authorization header we cannot trigger an authenticated event, so the
    audit-log probes would never produce a row.
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
            f"DEMO_PASSWORD not set — logging_audit cannot acquire IdTokens: {exc}",
            allow_module_level=True,
        )
    except CognitoAuthError as exc:
        pytest.skip(
            f"Cognito auth failed — logging_audit cannot acquire IdTokens: {exc}",
            allow_module_level=True,
        )


def _auth_header(identities: dict, persona_id: str) -> dict:
    """`{Authorization: "Bearer <persona IdToken>"}` for the given persona."""
    from src.identity.cognito_auth import Persona

    persona = Persona(persona_id)
    return {"Authorization": f"Bearer {identities[persona].id_token}"}


@pytest.fixture(scope="session")
def ciso_auth_header(identities: dict) -> dict:
    """CISO bearer header. Used for the legitimate-approve audit probe."""
    return _auth_header(identities, "ciso")


@pytest.fixture(scope="session")
def soc_auth_header(identities: dict) -> dict:
    """SOC bearer header. Used for the cross-persona audit probe."""
    return _auth_header(identities, "soc")


@pytest.fixture(scope="session")
def ciso_id_token(identities: dict) -> str:
    """Raw CISO IdToken (for the JWT-not-logged canary probe)."""
    from src.identity.cognito_auth import Persona

    return identities[Persona.CISO].id_token


# ───────────────────────────── AWS clients ──────────────────────────────────


@pytest.fixture(scope="session")
def aws_clients() -> dict:
    """boto3 clients for the AWS read-side of the layer.

    Returns a dict with:
      * ``cloudwatch_logs`` — for FilterLogEvents on the api_handler log group.
      * ``dynamodb`` — DynamoDB resource for the audit-log table.

    Skipped at module level if:
      * boto3 import fails (the harness env doesn't have it installed).
      * NoCredentialsError is raised on a probe call (no creds available).
      * AccessDeniedException is raised on a probe call (creds present but
        the principal lacks logs:FilterLogEvents / dynamodb:Scan).
    """
    try:
        import boto3
        from botocore.exceptions import (
            ClientError,
            NoCredentialsError,
            NoRegionError,
        )
    except ImportError as exc:
        pytest.skip(f"boto3 unavailable: {exc}", allow_module_level=True)

    try:
        logs_client = boto3.client("logs", region_name=AWS_REGION)
        ddb_resource = boto3.resource("dynamodb", region_name=AWS_REGION)
    except NoRegionError as exc:
        pytest.skip(f"AWS region not resolvable: {exc}", allow_module_level=True)

    # Probe the credentials shape: a 1-event FilterLogEvents on the api_handler
    # log group. If the operator has no creds OR the principal can't read
    # CloudWatch, we'd get NoCredentialsError or AccessDeniedException.
    try:
        logs_client.filter_log_events(
            logGroupName=API_HANDLER_LOG_GROUP,
            limit=1,
        )
    except NoCredentialsError as exc:
        pytest.skip(
            f"AWS credentials unavailable for logs:FilterLogEvents: {exc}",
            allow_module_level=True,
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("AccessDeniedException", "UnauthorizedOperation"):
            pytest.skip(
                f"principal lacks logs:FilterLogEvents on {API_HANDLER_LOG_GROUP}: {exc}",
                allow_module_level=True,
            )
        if code == "ResourceNotFoundException":
            pytest.skip(
                f"log group {API_HANDLER_LOG_GROUP} not provisioned: {exc}",
                allow_module_level=True,
            )
        # Anything else (throttle, transient) — let the tests handle it.

    return {
        "cloudwatch_logs": logs_client,
        "dynamodb": ddb_resource,
    }


@pytest.fixture(scope="session")
def audit_log_table(aws_clients: dict):
    """DynamoDB Table resource pointing at the audit-log table.

    Skipped at module level if the table is missing or unreadable. We probe
    the table's existence with a 1-row Scan because Scan requires the same
    IAM action (dynamodb:Scan) we use in the tests, so a permissions gap
    surfaces here rather than mid-test.
    """
    try:
        from botocore.exceptions import ClientError
    except ImportError as exc:
        pytest.skip(f"botocore unavailable: {exc}", allow_module_level=True)

    table = aws_clients["dynamodb"].Table(AUDIT_LOG_TABLE_NAME)
    try:
        # 1-row scan to verify both existence and scan permission.
        table.scan(Limit=1)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "ResourceNotFoundException":
            pytest.skip(
                f"audit-log table '{AUDIT_LOG_TABLE_NAME}' not provisioned",
                allow_module_level=True,
            )
        if code in ("AccessDeniedException", "UnauthorizedOperation"):
            pytest.skip(
                f"principal lacks dynamodb:Scan on '{AUDIT_LOG_TABLE_NAME}': {exc}",
                allow_module_level=True,
            )
        # Other errors are surfaced to the test for context.
    return table


# ─────────────────────────── http session ────────────────────────────────────


@pytest.fixture(scope="session")
def http_session() -> Generator[requests.Session, None, None]:
    """A `requests.Session` configured for the logging_audit layer.

    10 s default timeout — the layer's HTTP probes are short single requests
    (the slow part is CloudWatch query latency, which happens via boto3).
    """
    sess = requests.Session()
    original_request = sess.request

    def _wrapped_request(method, url, **kwargs):
        kwargs.setdefault("timeout", 10)
        return original_request(method, url, **kwargs)

    sess.request = _wrapped_request  # type: ignore[assignment]
    yield sess
    sess.close()


# ─────────────────────────── results writer ──────────────────────────────────


class LoggingAuditResultsWriter:
    """Accumulates logging_audit `TestResult`-shaped rows and writes them at
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
def results_writer() -> Generator[LoggingAuditResultsWriter, None, None]:
    """Session-scoped writer. Drained to disk by `pytest_sessionfinish`."""
    writer = LoggingAuditResultsWriter()
    yield writer


_LIVE_WRITER: dict[str, LoggingAuditResultsWriter] = {}


@pytest.fixture(scope="session", autouse=True)
def _capture_writer(
    results_writer: LoggingAuditResultsWriter,
) -> LoggingAuditResultsWriter:
    """Side-channel to give `pytest_sessionfinish` the writer instance."""
    _LIVE_WRITER["writer"] = results_writer
    return results_writer


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Drain the logging_audit results writer to disk at session end.

    Also writes a zero-shape `cost.json`: the layer makes no Bedrock calls
    — every probe is HTTP / DDB / CloudWatch only.
    """
    writer = _LIVE_WRITER.get("writer")
    if writer is None:
        return
    out_path = logging_audit_results_path(os.environ.get("RUN_DIR"))
    try:
        writer.write(out_path)
    except OSError as exc:
        print(f"warning: logging_audit results write failed: {exc}", file=sys.stderr)

    cost_path = out_path.parent / "cost.json"
    try:
        cost_path.write_text(
            json.dumps(
                {
                    "total_usd": 0.0,
                    "per_layer_usd": {"logging_audit": 0.0},
                    "probe_counts": {},
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"warning: logging_audit cost stash failed: {exc}", file=sys.stderr)


# ─────────────────── shared evidence-path helper ─────────────────────────────


def evidence_path_for(test_id: str) -> str:
    """Stable evidence pointer per layer's convention.

    Mirrors auth/fuzz/headers/dos/logic: every FAIL row points back at its
    own row in the layer's results.json. AC20 requires every FAIL to have
    an `evidence_path`.
    """
    return f"logging_audit/results.json#{test_id}"


__all__ = [
    "API_HANDLER_LOG_GROUP",
    "AUDIT_LOG_TABLE_NAME",
    "AWS_REGION",
    "LoggingAuditResultsWriter",
    "evidence_path_for",
    "logging_audit_results_path",
    "manifest",
]
