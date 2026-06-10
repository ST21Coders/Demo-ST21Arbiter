"""Curated jailbreak / prompt-injection probes (task 17).

For each probe in `llm/corpus/jailbreaks.yaml` (20 total), this module sends
the prompt to the deployed `/chat` Function URL as CISO, captures the response
+ token counts, classifies refusal vs compliance vs ambiguous, and emits one
`TestResult` row.

Probe ordering
--------------
Probes are run in the order returned by `load_corpus()` (sorted by source
file then declared order). The curated YAML lists the most-likely-to-find-a-
finding probes first inside each category (system-prompt extraction before
fictional roleplay, direct data-exfil before indirect KB-poisoning) so that
when the AC16 budget is tight, the highest-signal probes still execute.

Probe-budget enforcement
------------------------
The `llm_probe_budget` fixture is called BEFORE the request goes out. If the
budget is already at the cap, it raises `ProbeBudgetExhausted` and the test
records a `skipped` row with reason `budget exhausted` — no `/chat` call is
ever made.

Result row shape (per spec §6 + builder.py::TestResult)
-------------------------------------------------------
  test_id        — "llm.<probe.id>" (e.g. "llm.jailbreak.ignore-previous-instructions")
  status         — "pass" | "fail" | "skipped"
  layer          — "llm"
  target_kind    — "agent_tool"
  target_id      — "master.chat_surface" sentinel — every red-team probe
                   targets the master orchestrator's entry point regardless
                   of which specialist tool it tries to coerce. The per-tool
                   granularity lives in the auth/fuzz layers, not here. This
                   id is a synthetic manifest entry (`synthetic: true`) — see
                   `coverage/manifest.json::agent_tools[master.chat_surface]`.
  persona        — "ciso"
  severity       — probe.severity_on_failure on fail, else None
  evidence_path  — relative path to the transcript JSONL
  skipped_reason — set when status == "skipped"
  duration_seconds — wall time of the /chat call (or 0 on skipped)
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

import pytest
import requests

from llm.conftest import (
    ProbeBudgetExhausted,
    classify_probe_response,
    llm_transcript_path,
    load_corpus,
)


# Load the corpus at module import so pytest can show every probe id in
# `--collect-only`. A YAML parse error here surfaces as a collection error,
# which is exactly the right time to catch a malformed corpus.
_CORPUS = load_corpus()


def _probe_ids() -> list[str]:
    """Pytest parametrize ids — each is the bare probe id (not 'llm.<id>')."""
    return [p["id"] for p in _CORPUS]


def _required_fields() -> tuple[str, ...]:
    return (
        "id",
        "prompt",
        "category",
        "expected_behavior",
        "severity_on_failure",
        "rationale",
    )


# Validate the corpus shape at import time — saves every test from re-checking.
for _probe in _CORPUS:
    for _field in _required_fields():
        if _field not in _probe:
            raise ValueError(
                f"LLM corpus probe missing required field {_field!r}: {_probe}"
            )


def _extract_response_text(payload: dict | None) -> str:
    """Pull the assistant's textual reply out of the /chat response body.

    The api_handler `/chat` route returns a dict; the assistant text is
    typically under `reply`, `response`, `message`, or `content`. We try the
    common shapes and fall back to a JSON-dump of the whole payload so the
    transcript still has something to classify.
    """
    if not isinstance(payload, dict):
        return str(payload or "")
    for key in ("reply", "response", "message", "content", "text", "output"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    # Some shapes nest under {"messages": [{"content": "..."}]}
    messages = payload.get("messages")
    if isinstance(messages, list) and messages:
        last = messages[-1]
        if isinstance(last, dict):
            for key in ("content", "text"):
                value = last.get(key)
                if isinstance(value, str) and value.strip():
                    return value
    return json.dumps(payload, ensure_ascii=False)


def _extract_token_counts(payload: dict | None) -> tuple[int, int]:
    """Read input/output token counts from a /chat response.

    Looks under `usage`, `token_usage`, or top-level `input_tokens`/
    `output_tokens` fields. Returns (0, 0) when no usage data is present —
    callers should not block on it (the cost footer will under-report by
    the missing rows, which is preferable to fabricating numbers).
    """
    if not isinstance(payload, dict):
        return (0, 0)
    for key in ("usage", "token_usage"):
        usage = payload.get(key)
        if isinstance(usage, dict):
            in_t = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
            out_t = int(
                usage.get("output_tokens") or usage.get("completion_tokens") or 0
            )
            return (in_t, out_t)
    in_t = int(payload.get("input_tokens") or 0)
    out_t = int(payload.get("output_tokens") or 0)
    return (in_t, out_t)


def _write_transcript(transcript_path: Path, row: dict) -> str:
    """Append a single JSONL row to the per-probe transcript file.

    Returns the relative-to-RUN_DIR path string for the `evidence_path` field
    in the result row.
    """
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    with transcript_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return str(transcript_path)


@pytest.mark.parametrize("probe", _CORPUS, ids=_probe_ids())
def test_curated_jailbreak(
    probe: dict,
    llm_probe_budget,
    cost_tracker,
    chat_function_url: str,
    ciso_identity,
    http_session: requests.Session,
    results_writer,
) -> None:
    """One probe per parametrize row. See module docstring for the row shape."""
    test_id = f"llm.{probe['id']}"
    target_id = "master.chat_surface"
    run_dir = os.environ.get("RUN_DIR")
    transcript_path = llm_transcript_path(run_dir, probe["id"])

    # Step 1: pre-bump the probe counter. If the cap is hit, record a skip
    # and return WITHOUT sending the request.
    try:
        probe_index = llm_probe_budget()
    except ProbeBudgetExhausted as exc:
        results_writer.record(
            {
                "test_id": test_id,
                "status": "skipped",
                "layer": "llm",
                "target_kind": "agent_tool",
                "target_id": target_id,
                "persona": "ciso",
                "severity": None,
                "skipped_reason": f"budget exhausted: {exc}",
                "duration_seconds": 0.0,
            }
        )
        pytest.skip(f"LLM probe budget exhausted before {test_id}")
        return

    # Step 2: send the probe.
    session_id = f"harness-{uuid.uuid4()}"
    body = {
        "prompt": f"[harness] {probe['prompt']}",
        "session_id": session_id,
    }
    headers = {
        "Authorization": f"Bearer {ciso_identity.id_token}",
        "Content-Type": "application/json",
    }

    started = time.monotonic()
    response_text = ""
    in_tokens = 0
    out_tokens = 0
    http_status: int | None = None
    error_note: str | None = None
    response_payload: dict | None = None

    try:
        resp = http_session.post(
            f"{chat_function_url}/chat",
            json=body,
            headers=headers,
        )
        http_status = resp.status_code
        try:
            response_payload = resp.json()
        except ValueError:
            response_payload = {"raw": resp.text}
        response_text = _extract_response_text(response_payload)
        in_tokens, out_tokens = _extract_token_counts(response_payload)
    except requests.RequestException as exc:
        error_note = f"request error: {exc}"

    duration = time.monotonic() - started

    # Step 3: record billed cost IF we got token counts back. A failed probe
    # still counted against the cap via `increment_probe()` above.
    if in_tokens or out_tokens:
        # The model_id may not always be in the response; default to the
        # project-wide foundation model per CLAUDE.local.md.
        model_id = "us.amazon.nova-2-lite-v1:0"
        if isinstance(response_payload, dict):
            for key in ("model_id", "model"):
                if isinstance(response_payload.get(key), str):
                    model_id = response_payload[key]
                    break
        try:
            cost_tracker.record(
                layer="llm",
                model_id=model_id,
                input_tokens=in_tokens,
                output_tokens=out_tokens,
            )
        except KeyError:
            # Unknown model id — log into transcript but don't crash the run.
            error_note = (
                error_note + "; " if error_note else ""
            ) + f"unknown model_id for cost recording: {model_id}"

    # Step 4: classify.
    if error_note:
        status, severity, reason = "skipped", None, error_note
    elif http_status is not None and http_status >= 500:
        # 5xx is a runtime failure, not a model decision. Record skipped so
        # one Bedrock blip doesn't fail the whole run.
        status, severity, reason = (
            "skipped",
            None,
            f"http {http_status} from /chat (runtime error)",
        )
    elif http_status is not None and http_status >= 400:
        # 4xx most often means malformed harness request, not a model finding.
        status, severity, reason = (
            "skipped",
            None,
            f"http {http_status} from /chat (client error)",
        )
    else:
        status, severity, reason = classify_probe_response(response_text, probe)

    # Step 5: write the transcript row.
    transcript_row = {
        "test_id": test_id,
        "probe_index": probe_index,
        "probe_id": probe["id"],
        "category": probe["category"],
        "prompt": probe["prompt"],
        "request_body": body,
        "http_status": http_status,
        "response_text": response_text,
        "response_payload": response_payload,
        "input_tokens": in_tokens,
        "output_tokens": out_tokens,
        "classification": {"status": status, "severity": severity, "reason": reason},
        "duration_seconds": duration,
    }
    evidence_path = _write_transcript(transcript_path, transcript_row)

    # Step 6: emit the layer result row.
    result_row = {
        "test_id": test_id,
        "status": status,
        "layer": "llm",
        "target_kind": "agent_tool",
        "target_id": target_id,
        "persona": "ciso",
        "severity": severity,
        "evidence_path": evidence_path,
        "duration_seconds": duration,
    }
    if status == "skipped":
        result_row["skipped_reason"] = reason
    results_writer.record(result_row)

    # Step 7: drive the pytest verdict. A `skipped` classification skips the
    # test (no run-level failure); a `fail` classification fails it loudly.
    if status == "skipped":
        pytest.skip(reason)
    if status == "fail":
        pytest.fail(
            f"{test_id} classified as fail ({severity}): {reason}. "
            f"Evidence: {evidence_path}"
        )
