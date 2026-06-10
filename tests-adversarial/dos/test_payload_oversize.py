"""Oversized body degradation (Block E, closes checklist item #64).

For each of three JSON-accepting routes, send a baseline request, a 1 MB
JSON body, and a 10 MB JSON body — measuring latency at each size. The
classifier in `dos.classifiers.classify_oversize_payload` decides PASS /
FAIL MEDIUM / FAIL HIGH based on the latency ratio and the response code.

Route choice
------------
We hit three routes that legitimately accept JSON bodies (so the test
shape isn't artificial):

  1. ``post-chat``         — the largest legitimate-body surface in ARBITER.
  2. ``post-jira-tickets`` — accepts ticket payload (summary + description).
  3. ``post-actions``      — accepts the change-request action payload.

Each route × size combination emits one TestResult row:
``dos.payload-oversize.<route-id>.<size>`` where ``<size>`` is one of
``1mb`` / ``10mb``. The baseline call is not its own row — it's the
denominator for the ratio in the 1 MB and 10 MB rows.
"""

from __future__ import annotations

import time

import pytest
import requests

from dos.classifiers import TIMEOUT_SECONDS_FLOOR, classify_oversize_payload
from dos.conftest import evidence_path_for

# ───────────────────────────── probe targets ─────────────────────────────────

# (route_id, http_method, path, kwargs_factory). The kwargs_factory takes
# a "size_bytes" int and returns a dict to splat into the request.
_SIZE_1MB = 1 * 1024 * 1024
_SIZE_10MB = 10 * 1024 * 1024
# Baseline body is short but plausible — large enough to defeat trivial
# "empty body" rejection paths but small enough to be the unambiguous
# denominator.
_BASELINE_BODY_BYTES = 32


def _chat_body(size_bytes: int) -> dict:
    """`{"prompt": "X" * N, "session_id": ...}`. The `session_id` field keeps
    the body shape valid so a body-shape check doesn't short-circuit before
    the size check runs.
    """
    return {
        "prompt": "X" * size_bytes,
        "session_id": "dos-payload-oversize",
    }


def _jira_body(size_bytes: int) -> dict:
    """`{"summary": ..., "description": "X" * N}`."""
    return {
        "summary": "dos-payload-oversize",
        "description": "X" * size_bytes,
    }


def _actions_body(size_bytes: int) -> dict:
    """`{"action_type": "note", "payload": "X" * N}`."""
    return {
        "action_type": "note",
        "payload": "X" * size_bytes,
    }


_PROBE_ROUTES: list[tuple[str, str, str, "callable"]] = [  # type: ignore[valid-type]
    ("post-chat", "POST", "/chat", _chat_body),
    ("post-jira-tickets", "POST", "/jira/tickets", _jira_body),
    ("post-actions", "POST", "/actions", _actions_body),
]


_SIZES: list[tuple[str, int]] = [
    ("1mb", _SIZE_1MB),
    ("10mb", _SIZE_10MB),
]


@pytest.fixture(scope="module")
def _baselines() -> dict[str, tuple[int, float]]:
    """Per-route baseline (status, latency_ms). Populated once per module
    so the per-size tests share the denominator. Filled lazily by
    `test_payload_oversize` via the per-call helper below — we don't fetch
    at fixture-setup time because that would require already-resolved auth.
    """
    return {}


def _fetch_baseline(
    route_id: str,
    method: str,
    url: str,
    body_factory,
    http_session: requests.Session,
    ciso_auth_header: dict,
) -> tuple[int, float]:
    """Send one short-body request and return ``(status, latency_ms)``.

    A transport error returns ``(0, elapsed_ms)`` — the classifier handles
    a zero-baseline by falling back to an absolute threshold.
    """
    body = body_factory(_BASELINE_BODY_BYTES)
    started = time.monotonic()
    try:
        resp = http_session.request(
            method,
            url,
            headers=dict(ciso_auth_header),
            json=body,
        )
        elapsed_ms = (time.monotonic() - started) * 1000.0
        return resp.status_code, elapsed_ms
    except requests.RequestException:
        elapsed_ms = (time.monotonic() - started) * 1000.0
        return 0, elapsed_ms


@pytest.mark.parametrize(
    ("route_id", "method", "path", "body_factory"),
    _PROBE_ROUTES,
    ids=[t[0] for t in _PROBE_ROUTES],
)
@pytest.mark.parametrize(
    ("size_label", "size_bytes"),
    _SIZES,
    ids=[s[0] for s in _SIZES],
)
def test_payload_oversize(
    route_id: str,
    method: str,
    path: str,
    body_factory,
    size_label: str,
    size_bytes: int,
    api_base_url: str,
    chat_function_url: str | None,
    http_session: requests.Session,
    ciso_auth_header: dict,
    include_destructive: bool,
    results_writer,
    _baselines,
) -> None:
    """One (route × size) row. PASS if the route refused the body or absorbed
    it without latency blow-up.

    All three probe routes are destructive — the oversize-body test always
    requires `--include-destructive`. We skip with a recorded row so the
    coverage matrix still shows the missing cell.
    """
    test_id = f"dos.payload-oversize.{route_id}.{size_label}"

    if not include_destructive:
        results_writer.record(
            {
                "test_id": test_id,
                "status": "skipped",
                "layer": "dos",
                "target_kind": "api_route",
                "target_id": route_id,
                "skipped_reason": "destructive route; pass --include-destructive to enable",
            }
        )
        pytest.skip("destructive; --include-destructive not passed")

    if route_id == "post-chat":
        if not chat_function_url:
            results_writer.record(
                {
                    "test_id": test_id,
                    "status": "skipped",
                    "layer": "dos",
                    "target_kind": "api_route",
                    "target_id": route_id,
                    "skipped_reason": "CHAT_FUNCTION_URL unset; /chat lives behind the Function URL",
                }
            )
            pytest.skip("CHAT_FUNCTION_URL unset")
        url = f"{chat_function_url}/chat"
    else:
        url = f"{api_base_url}{path}"

    # Lazy per-route baseline. Fetch once and cache; subsequent (size) probes
    # for the same route reuse it.
    if route_id not in _baselines:
        _baselines[route_id] = _fetch_baseline(
            route_id,
            method,
            url,
            body_factory,
            http_session,
            ciso_auth_header,
        )
    baseline_status, baseline_ms = _baselines[route_id]

    # The oversize call itself. We pre-build the body once and treat a read
    # timeout as the FAIL-HIGH case via `timed_out=True`. The session's 30 s
    # timeout doubles as the TIMEOUT_SECONDS_FLOOR in the classifier.
    body = body_factory(size_bytes)
    timed_out = False
    started = time.monotonic()
    oversize_status = 0
    try:
        resp = http_session.request(
            method,
            url,
            headers=dict(ciso_auth_header),
            json=body,
        )
        oversize_status = resp.status_code
    except requests.Timeout:
        timed_out = True
    except requests.RequestException:
        # Other transport errors (connection reset, SSL) — classified the
        # same as timeout for our purposes (HIGH).
        timed_out = True
    oversize_ms = (time.monotonic() - started) * 1000.0
    if oversize_ms >= TIMEOUT_SECONDS_FLOOR * 1000.0 and not timed_out:
        # Defence in depth: if the wall-clock crossed the floor but we
        # didn't catch a timeout exception, still treat it as a timeout.
        timed_out = True

    status, severity, reason = classify_oversize_payload(
        baseline_status,
        baseline_ms,
        oversize_status,
        oversize_ms,
        timed_out=timed_out,
    )

    row: dict = {
        "test_id": test_id,
        "status": status,
        "layer": "dos",
        "target_kind": "api_route",
        "target_id": route_id,
        "duration_seconds": oversize_ms / 1000.0,
    }
    if severity:
        row["severity"] = severity
    if status == "fail":
        row["evidence_path"] = evidence_path_for(test_id)
    results_writer.record(row)

    if status == "fail":
        pytest.fail(f"{test_id}: {reason}")
