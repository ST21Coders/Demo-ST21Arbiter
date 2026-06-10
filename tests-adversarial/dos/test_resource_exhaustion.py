"""Concurrent requests on a single resource (Block E, upgrades item #65).

Fan out 10 concurrent requests against one expensive route using a
`concurrent.futures.ThreadPoolExecutor`. The classifier in
`dos.classifiers.classify_concurrent_burst` decides PASS / FAIL MEDIUM /
FAIL HIGH based on the status code distribution and the count of malformed
responses.

Route choice
------------
Default target is ``POST /scan`` — a scan kick-off triggers an SQS message
to the processing pipeline (no Bedrock invocation per request) and is
representative of the most expensive non-LLM resource in the system. If
``--include-bedrock-dos`` is passed, the target switches to ``POST /chat``
(which DOES cost real Bedrock money) so the operator can deliberately
exercise the LLM-burst case.

Each route × 10-parallel emits one TestResult row:
``dos.concurrent.<route-id>.10-parallel``.
"""

from __future__ import annotations

import concurrent.futures
import json
import time

import pytest
import requests

from dos.classifiers import classify_concurrent_burst
from dos.conftest import evidence_path_for

# Number of parallel requests in the fan-out. Held as a module constant so
# the unit tests can pin it; not exposed as a CLI flag because the value
# is part of the test's identity (`.10-parallel`).
_CONCURRENCY = 10


def _scan_body() -> dict:
    """Minimal /scan kickoff payload."""
    return {"scope": "demo", "source": "dos-concurrent"}


def _chat_body() -> dict:
    """Minimal /chat payload — kept tiny so per-request Bedrock cost is
    bounded if the operator explicitly opted in with `--include-bedrock-dos`.
    """
    return {"prompt": "ping", "session_id": "dos-concurrent"}


def _fire_one(
    method: str,
    url: str,
    headers: dict,
    body: dict,
    timeout_seconds: float,
) -> tuple[int, bool]:
    """Issue one request. Return ``(status, body_malformed)``.

    A body counts as malformed if the response advertises JSON but
    `resp.json()` raises, or if the content-type header is missing on a
    2xx. A transport error returns ``(0, False)`` — the classifier picks
    that up as FAIL HIGH on its own.

    Uses a fresh `requests` Session per call because Python's `requests`
    Session is documented as thread-safe-for-read but not thread-safe for
    writes, and we want the fan-out to actually parallelize.
    """
    try:
        with requests.Session() as sess:
            resp = sess.request(
                method,
                url,
                headers=headers,
                json=body,
                timeout=timeout_seconds,
            )
            status = resp.status_code
            content_type = resp.headers.get("Content-Type", "")
            body_malformed = False
            if "json" in content_type.lower():
                try:
                    resp.json()
                except (ValueError, json.JSONDecodeError):
                    body_malformed = True
            elif 200 <= status < 300:
                # 2xx with no JSON content-type on a JSON-shaped route is
                # malformed by the convention of every other ARBITER route.
                body_malformed = not content_type
            return status, body_malformed
    except requests.RequestException:
        return 0, False


def test_concurrent_burst(
    api_base_url: str,
    chat_function_url: str | None,
    ciso_auth_header: dict,
    include_bedrock_dos: bool,
    include_destructive: bool,
    results_writer,
) -> None:
    """One row per default target. PASS if every concurrent request returns
    a clean 200 / 429 / 503.
    """
    if include_bedrock_dos:
        route_id = "post-chat"
        method = "POST"
        body = _chat_body()
        if not chat_function_url:
            test_id = f"dos.concurrent.{route_id}.10-parallel"
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
        route_id = "post-scan"
        method = "POST"
        body = _scan_body()
        url = f"{api_base_url}/scan"

    test_id = f"dos.concurrent.{route_id}.10-parallel"

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

    headers = dict(ciso_auth_header)

    # Per-request timeout matches the conftest's 30 s session default —
    # we want concurrent requests to fail loudly if the system can't
    # respond, not silently after the orchestrator's overall budget runs
    # out.
    per_request_timeout = 30.0

    started = time.monotonic()
    status_codes: list[int] = []
    malformed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=_CONCURRENCY) as pool:
        futures = [
            pool.submit(_fire_one, method, url, headers, body, per_request_timeout)
            for _ in range(_CONCURRENCY)
        ]
        for fut in concurrent.futures.as_completed(
            futures, timeout=per_request_timeout + 5
        ):
            status, body_malformed = fut.result()
            status_codes.append(status)
            if body_malformed:
                malformed += 1
    elapsed_s = time.monotonic() - started

    status, severity, reason = classify_concurrent_burst(status_codes, malformed)

    row: dict = {
        "test_id": test_id,
        "status": status,
        "layer": "dos",
        "target_kind": "api_route",
        "target_id": route_id,
        "duration_seconds": elapsed_s,
    }
    if severity:
        row["severity"] = severity
    if status == "fail":
        row["evidence_path"] = evidence_path_for(test_id)
    results_writer.record(row)

    if status == "fail":
        pytest.fail(f"{test_id}: {reason} (status_codes={status_codes})")
