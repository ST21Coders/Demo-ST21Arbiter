"""Unit tests for the CROSS_PERSONA audit row written inside _require_ciso.

The real audit-log table uses (event_id HASH + timestamp RANGE); the test
conftest's moto table uses log_id HASH only, which would fail validation for
real _audit(...) Items. We sidestep that by replacing audit_table.put_item with
a list-capturing spy, which is also closer to the spec's intent ("Mock the DDB
client / assert PutItem was called once").

Three scenarios are covered:

1. SOC token against a CISO-only path — CROSS_PERSONA row written, 403 returned.
2. Forged cognito:groups (canary value) — same CROSS_PERSONA action_type,
   canary value lands in details.caller_groups (no separate FORGED_TOKEN type).
3. DDB put_item raises — the response is still 403 and a warning is logged.
"""
from __future__ import annotations

import json
import time

from conftest import lambda_event, make_jwt


# ──────────────────────────── helpers ──────────────────────────────────────
def _build_event_with_groups(groups: list[str], path: str = "/token-usage") -> dict:
    """Lambda Function URL event with a hand-built JWT carrying cognito:groups."""
    event = lambda_event("GET", path, auth_sub="ciso-real-sub")
    # Replace the default token with one whose cognito:groups is what we want.
    event["headers"]["Authorization"] = (
        f"Bearer {make_jwt(sub='ciso-real-sub', groups=groups)}"
    )
    return event


def _install_put_spy(api_handler, monkeypatch) -> list[dict]:
    """Replace audit_table.put_item with a list-capturing spy. Returns the list
    that will collect every Item the production code tries to write."""
    captured: list[dict] = []

    def _spy(Item: dict):  # noqa: N803 — boto3 kwarg name
        captured.append(Item)
        return {}

    monkeypatch.setattr(api_handler.audit_table, "put_item", _spy)
    return captured


# ──────────────────────── 1. cross-persona DENIED ──────────────────────────
def test_soc_token_against_token_usage_writes_cross_persona_row(
    api_handler, monkeypatch
):
    captured = _install_put_spy(api_handler, monkeypatch)

    event = _build_event_with_groups(["soc"])
    t_before = int(time.time())
    resp = api_handler.handler(event, None)
    t_after = int(time.time())

    assert resp["statusCode"] == 403
    assert len(captured) == 1
    row = captured[0]
    assert row["action_type"] == "CROSS_PERSONA"
    assert row["status"] == "DENIED"
    assert row["resource"] == "/token-usage"

    details = json.loads(row["details"])
    assert details["required_group"] == "ciso"
    assert "soc" in details["caller_groups"]

    # 90-day TTL with a 5-second slop for test runtime.
    ninety_days = 90 * 24 * 60 * 60
    assert t_before + ninety_days <= row["ttl"] <= t_after + ninety_days + 5


# ──────────────────────── 2. forged cognito:groups ─────────────────────────
def test_forged_cognito_groups_writes_cross_persona_not_forged_token(
    api_handler, monkeypatch
):
    captured = _install_put_spy(api_handler, monkeypatch)

    canary = "harness-deadbeef"
    resp = api_handler.handler(_build_event_with_groups([canary]), None)

    assert resp["statusCode"] == 403
    assert len(captured) == 1
    row = captured[0]
    # Folded into CROSS_PERSONA — no separate FORGED_TOKEN type.
    assert row["action_type"] == "CROSS_PERSONA"
    assert row["action_type"] != "FORGED_TOKEN"

    details = json.loads(row["details"])
    assert canary in details["caller_groups"]


# ──────────────────────── 3. DDB error doesn't break ───────────────────────
def test_audit_write_failure_does_not_break_403(api_handler, monkeypatch, caplog):
    def _boom(Item):  # noqa: N803
        raise RuntimeError("simulated DDB throttle")

    monkeypatch.setattr(api_handler.audit_table, "put_item", _boom)

    with caplog.at_level("WARNING"):
        resp = api_handler.handler(_build_event_with_groups(["soc"]), None)

    # 403 is still returned despite the audit write blowing up.
    assert resp["statusCode"] == 403

    # At least one warning/error message hit the log. The existing _audit
    # writer logs at exception level (logger.exception → ERROR); the outer
    # try/except in _require_ciso logs at WARNING. Either is acceptable signal.
    assert any(
        "audit" in rec.message.lower() or "audit" in (rec.name or "").lower()
        for rec in caplog.records
    ), f"expected an audit-related log record, got: {[r.message for r in caplog.records]}"
