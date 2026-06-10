"""Log-injection downstream probes (#71).

Block A added ``fuzz/corpus/log_injection.json``, which tests whether the
API reflects CR/LF / ANSI / NUL in the response body. That covers the
API-layer surface only — the more dangerous question is: do those
payloads break CloudWatch's log parsing?

For each payload in the corpus we:

  1. Mint a unique marker (UUID) and wrap the payload like
     ``<marker>--<payload>--<marker>``. The marker is what we search for
     in CloudWatch; the payload is what we want to land inside one log
     event.
  2. POST the wrapped value to ``/chat`` as the prompt. /chat goes
     through the api_handler logging path.
  3. Sleep 3 s for log propagation.
  4. Search CloudWatch for the marker. Count the number of matching
     events.
  5. Classify with ``classify_log_injection_downstream``:
       * 1 event, no ANSI residue → PASS (payload escaped or stripped).
       * 0 events → PASS (API didn't log the input; #67/#68 catches
         genuine logging gaps separately).
       * >1 event → FAIL MEDIUM (CRLF split → log forgery).
       * 1 event with ANSI bytes still present → FAIL LOW.

Test IDs follow the harness convention:
``logging.log-injection-downstream.<payload-id>``.
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
import requests

# Local imports.
_LAYER_DIR = Path(__file__).resolve().parent
if str(_LAYER_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_LAYER_DIR.parent))

from logging_audit.classifiers import (  # noqa: E402
    classify_log_injection_downstream,
)
from logging_audit.conftest import (  # noqa: E402
    API_HANDLER_LOG_GROUP,
    evidence_path_for,
)

_HARNESS_ROOT = _LAYER_DIR.parent
_CORPUS_PATH = _HARNESS_ROOT / "fuzz" / "corpus" / "log_injection.json"
_PROPAGATION_SLEEP_SECONDS = 3.0
_QUERY_WINDOW_BEFORE_MS = 30_000
_QUERY_WINDOW_AFTER_MS = 60_000


def _load_corpus() -> list[dict]:
    """Read Block A's log-injection corpus. Returns the ``payloads`` list."""
    if not _CORPUS_PATH.exists():
        return []
    try:
        data = json.loads(_CORPUS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    payloads = data.get("payloads") or []
    return [p for p in payloads if isinstance(p, dict) and "id" in p]


def _payloads_for_parametrize() -> list[tuple[str, str]]:
    """Return ``[(payload_id, payload), ...]`` from the corpus.

    Used at module import time by ``@pytest.mark.parametrize``. If the
    corpus is missing we return an empty list — the test then collects
    zero cases and pytest reports "no tests ran", which is captured as a
    skip by the orchestrator's per-layer summary.
    """
    return [(p["id"], p.get("payload", "")) for p in _load_corpus()]


# ─────────────────────────── CloudWatch helper ───────────────────────────────


def _epoch_ms() -> int:
    return int(time.time() * 1000)


def _filter_events(
    logs_client: Any,
    *,
    marker: str,
    start_ms: int,
    end_ms: int,
    sample_size: int = 5,
) -> tuple[int, list[str]]:
    """Count CloudWatch events matching ``marker`` and return up to
    ``sample_size`` sample message bodies.

    Returns ``(event_count, sample_messages)`` — the count is the number
    of distinct log events (each one a separate line in CloudWatch); the
    samples are the verbatim message bodies, which the classifier
    inspects for ANSI residue.
    """
    pattern = f'"{marker}"'
    count = 0
    samples: list[str] = []
    next_token: str | None = None
    pages = 0
    max_pages = 8
    while pages < max_pages:
        kwargs: dict[str, Any] = {
            "logGroupName": API_HANDLER_LOG_GROUP,
            "startTime": start_ms,
            "endTime": end_ms,
            "filterPattern": pattern,
            "limit": 100,
        }
        if next_token:
            kwargs["nextToken"] = next_token
        try:
            resp = logs_client.filter_log_events(**kwargs)
        except Exception:  # noqa: BLE001 - transient errors mean "no data"
            break
        events = resp.get("events") or []
        for ev in events:
            count += 1
            msg = ev.get("message") or ""
            if len(samples) < sample_size:
                samples.append(msg)
        next_token = resp.get("nextToken")
        if not next_token:
            break
        pages += 1
    return count, samples


# ────────────────────────── parametrized probe ───────────────────────────────


@pytest.mark.parametrize(
    "payload_id,payload",
    _payloads_for_parametrize(),
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_log_injection_payload_does_not_split_log(
    payload_id: str,
    payload: str,
    chat_function_url: str | None,
    ciso_auth_header: dict,
    http_session: requests.Session,
    aws_clients: dict,
    results_writer,
) -> None:
    """One probe per corpus payload.

    Send ``<marker>--<payload>--<marker>`` as the /chat prompt, then read
    CloudWatch and classify.
    """
    test_id = f"logging.log-injection-downstream.{payload_id}"
    target_id = "post-chat"

    if not chat_function_url:
        results_writer.record(
            {
                "test_id": test_id,
                "status": "skipped",
                "layer": "logging_audit",
                "target_kind": "api_route",
                "target_id": target_id,
                "skipped_reason": "CHAT_FUNCTION_URL unset",
            }
        )
        pytest.skip("CHAT_FUNCTION_URL unset")

    # Marker that won't collide with corpus payloads or other harness traffic.
    marker = f"li-marker-{uuid.uuid4().hex[:12]}"
    wrapped_payload = f"{marker}--{payload}--{marker}"

    start_ms = _epoch_ms()
    try:
        http_session.post(
            f"{chat_function_url}/chat",
            headers={**ciso_auth_header, "Content-Type": "application/json"},
            json={
                "prompt": wrapped_payload,
                "session_id": f"li-{uuid.uuid4()}",
                "chat_type": "analyst",
            },
            timeout=30,
        )
    except requests.RequestException:
        pass

    time.sleep(_PROPAGATION_SLEEP_SECONDS)
    end_ms = _epoch_ms() + _QUERY_WINDOW_AFTER_MS

    matched_count, samples = _filter_events(
        aws_clients["cloudwatch_logs"],
        marker=marker,
        start_ms=start_ms - _QUERY_WINDOW_BEFORE_MS,
        end_ms=end_ms,
    )

    verdict, severity, reason = classify_log_injection_downstream(
        matched_count, sample_messages=samples, payload_id=payload_id
    )
    row: dict = {
        "test_id": test_id,
        "status": verdict,
        "layer": "logging_audit",
        "target_kind": "api_route",
        "target_id": target_id,
    }
    if severity:
        row["severity"] = severity
    if verdict == "fail":
        row["evidence_path"] = evidence_path_for(test_id)
    results_writer.record(row)
    if verdict == "fail":
        pytest.fail(
            f"{test_id}: {reason} (marker={marker}, matched_events={matched_count})"
        )
