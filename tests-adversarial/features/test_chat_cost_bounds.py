"""Chat cost bounds (features layer).

What this proves
----------------
After a /chat turn, query the CISO-only `/token-usage` endpoint for the row
the agent just wrote, compute the cost from the row's tokens × the project's
MODEL_PRICING, and assert it stays inside the expected cliff:

  PASS    — cost < $0.01 per chat turn (Nova 2 Lite typical range).
  FAIL MEDIUM — $0.01 <= cost < $0.10 (more than expected but not catastrophic).
  FAIL HIGH   — cost >= $0.10 (broken model selection or runaway loop).

Test id: `features.chat-cost.under-one-cent` (1 test).

Cleanup
-------
DELETEs the created chat session.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
import requests

from features.classifiers import classify_chat_cost, compute_chat_cost_usd
from features.conftest import evidence_path_for
from src.identity.cognito_auth import Persona


# Default Nova 2 Lite pricing — the master is wired to either Nova or Sonnet.
# We resolve actual pricing via `src.cost.pricing.load_pricing` so any drift
# between agents/_shared/token_usage.py and ui/src/mockData.js fails preflight,
# not this test. If the reconciled table is unavailable, fall back to Nova
# defaults so the test can still produce a verdict.
_FALLBACK_PRICING = {"input": 0.06, "output": 0.24}


def _pricing_for(model_id: str) -> dict[str, float]:
    """Resolve per-million pricing for the given model id.

    Tries `src.cost.pricing.load_pricing` first; falls back to the Nova
    defaults if that import path or the model id is unavailable. Never raises
    — pricing drift is a preflight concern, not a feature-test failure mode.
    """
    try:
        from src.cost import pricing as _pricing  # noqa: PLC0415

        table = _pricing.load_pricing()
    except Exception:
        return dict(_FALLBACK_PRICING)
    return table.get(model_id, _FALLBACK_PRICING)


def _latest_record(payload: object, marker_iso: str) -> dict | None:
    """Pick the most recent record with timestamp > marker_iso, or None."""
    if not isinstance(payload, dict):
        return None
    records = payload.get("records")
    if not isinstance(records, list):
        return None
    candidates: list[dict] = []
    for r in records:
        if not isinstance(r, dict):
            continue
        ts = r.get("timestamp")
        if isinstance(ts, str) and ts > marker_iso:
            candidates.append(r)
    if not candidates:
        return None
    candidates.sort(key=lambda r: r.get("timestamp") or "", reverse=True)
    return candidates[0]


def _to_int(v: object) -> int:
    """Coerce a DDB-shaped numeric (int / Decimal / str) to int, defaulting 0."""
    if v is None:
        return 0
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v
    if isinstance(v, Decimal):
        return int(v)
    try:
        return int(v)  # str / float
    except (TypeError, ValueError):
        return 0


def test_chat_cost_under_one_cent(
    identities: dict,
    chat_function_url: str | None,
    api_base_url: str,
    http_session: requests.Session,
    results_writer,
    cost_tracker_dict: dict,
) -> None:
    """Send a chat, fetch the resulting token-usage row, classify the cost."""
    test_id = "features.chat-cost.under-one-cent"

    if not chat_function_url:
        results_writer.record(
            {
                "test_id": test_id,
                "status": "skipped",
                "layer": "features",
                "target_kind": "api_route",
                "target_id": "post-chat",
                "skipped_reason": "CHAT_FUNCTION_URL unset",
            }
        )
        pytest.skip("CHAT_FUNCTION_URL unset")

    ciso = identities[Persona.CISO]
    headers = {"Authorization": f"Bearer {ciso.id_token}"}
    marker_iso = datetime.now(timezone.utc).isoformat()
    session_id = f"features-cost-{uuid.uuid4().hex[:12]}"
    body = {
        "prompt": "[harness] What's a one-sentence summary of our security posture?",
        "session_id": session_id,
    }

    started = time.monotonic()
    chat_status = 0
    try:
        chat_resp = http_session.post(
            f"{chat_function_url}/chat",
            json=body,
            headers=headers,
        )
        chat_status = chat_resp.status_code
    except requests.RequestException:
        chat_status = 0

    if chat_status != 200:
        results_writer.record(
            {
                "test_id": test_id,
                "status": "fail",
                "layer": "features",
                "target_kind": "api_route",
                "target_id": "post-chat",
                "duration_seconds": time.monotonic() - started,
                "severity": "high",
                "evidence_path": evidence_path_for(test_id),
            }
        )
        pytest.fail(
            f"{test_id}: chat returned HTTP {chat_status} — cannot evaluate cost"
        )

    # Wait for the async record_usage write, then poll up to 5 s for the row.
    time.sleep(3.0)
    deadline = time.monotonic() + 5.0
    latest_record: dict | None = None
    while time.monotonic() < deadline:
        try:
            list_resp = http_session.get(
                f"{api_base_url}/token-usage",
                headers=headers,
                params={"range": "today"},
            )
            if list_resp.status_code == 200:
                try:
                    payload = list_resp.json()
                except ValueError:
                    payload = {}
                latest_record = _latest_record(payload, marker_iso)
                if latest_record:
                    break
        except requests.RequestException:
            pass
        time.sleep(0.5)

    latency = time.monotonic() - started

    if latest_record is None:
        # No row to read = we can't compute cost. Distinct from a cost
        # overrun; flag as MEDIUM so the operator sees the missing telemetry.
        results_writer.record(
            {
                "test_id": test_id,
                "status": "fail",
                "layer": "features",
                "target_kind": "api_route",
                "target_id": "post-chat",
                "duration_seconds": latency,
                "severity": "medium",
                "evidence_path": evidence_path_for(test_id),
            }
        )
        # Best-effort cleanup before failing.
        try:
            http_session.delete(
                f"{api_base_url}/conversations/{session_id}",
                headers=headers,
            )
        except requests.RequestException:
            pass
        pytest.fail(f"{test_id}: no token-usage row appeared — cannot evaluate cost")

    # Prefer the agent-reported estimated_cost; if absent or invalid, recompute
    # from the row's tokens × pricing for the row's model_id.
    cost_usd: float
    reported = latest_record.get("estimated_cost")
    try:
        cost_usd = float(reported) if reported is not None else float("nan")
    except (TypeError, ValueError):
        cost_usd = float("nan")
    if cost_usd != cost_usd:  # NaN
        model_id = str(latest_record.get("model_id") or "us.amazon.nova-2-lite-v1:0")
        cost_usd = compute_chat_cost_usd(
            _to_int(latest_record.get("input_tokens")),
            _to_int(latest_record.get("output_tokens")),
            _pricing_for(model_id),
        )

    status, severity, reason = classify_chat_cost(cost_usd)

    row: dict = {
        "test_id": test_id,
        "status": status,
        "layer": "features",
        "target_kind": "api_route",
        "target_id": "post-chat",
        "duration_seconds": latency,
    }
    if severity:
        row["severity"] = severity
    if status == "fail":
        row["evidence_path"] = evidence_path_for(test_id)
    results_writer.record(row)

    cost_tracker_dict["rows"].append(
        {"layer": "features", "test_id": test_id, "usd": max(0.0, cost_usd)}
    )

    try:
        http_session.delete(
            f"{api_base_url}/conversations/{session_id}",
            headers=headers,
        )
    except requests.RequestException:
        pass

    if status == "fail":
        pytest.fail(f"{test_id}: {reason}")
