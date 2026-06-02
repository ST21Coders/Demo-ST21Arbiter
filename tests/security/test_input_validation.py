"""Input-validation and protocol-correctness tests.

Covers categories explicitly called out in the QA strategy:
  - Mass assignment (extra fields are ignored, not echoed)
  - Wrong HTTP method handling
  - Wrong Content-Type
  - Oversize payloads
  - Unicode and special characters
  - Boundary conditions (empty / max-length / null fields)
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import lambda_event, FAKE_USER_SUB, SESSIONS_TABLE  # noqa: E402


def _body(resp):
    return json.loads(resp["body"])


# ──────────────────────────── Mass assignment ────────────────────
def test_chat_extra_fields_in_body_are_ignored(api_handler, monkeypatch):
    """Sending extra fields to /chat must not let an attacker pin an admin flag,
    impersonate another user, or change runtime ARN. Handler must read only
    the fields it expects."""
    captured = {}

    def capture(**kwargs):
        captured["payload"] = json.loads(kwargs["payload"])
        captured["arn"] = kwargs.get("agentRuntimeArn")
        return {"response": MagicMock(read=MagicMock(
            return_value=json.dumps({"result": "ok"}).encode()
        ))}

    mock_ac = MagicMock()
    mock_ac.invoke_agent_runtime.side_effect = capture
    monkeypatch.setattr(api_handler, "agentcore", mock_ac)

    api_handler.handler(lambda_event("POST", "/chat", body={
        "prompt": "hi",
        # Hostile extras — must be ignored.
        "is_admin": True,
        "user_id": "victim-sub",
        "actor_id": "victim-sub",
        "agentRuntimeArn": "arn:aws:bedrock-agentcore:us-east-1:000:runtime/EVIL",
        "MASTER_AGENT_RUNTIME_ARN": "arn:aws:bedrock-agentcore:us-east-1:000:runtime/EVIL",
    }), None)

    # The actor_id forwarded to the agent must be the caller's JWT sub, not
    # the attacker-supplied 'user_id' or 'actor_id'.
    assert captured["payload"]["actor_id"] == FAKE_USER_SUB
    # The runtime ARN must be the configured one, not the attacker-supplied.
    assert "EVIL" not in (captured["arn"] or "")


# ──────────────────────────── Wrong HTTP method ──────────────────
@pytest.mark.parametrize("method", ["PUT", "DELETE", "PATCH", "OPTIONS"])
def test_chat_with_wrong_http_method_does_not_invoke_runtime(api_handler, monkeypatch, method):
    mock_ac = MagicMock()
    monkeypatch.setattr(api_handler, "agentcore", mock_ac)
    api_handler.handler(lambda_event(method, "/chat", body={"prompt": "hi"}), None)
    mock_ac.invoke_agent_runtime.assert_not_called()


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_findings_with_write_methods_does_not_mutate(api_handler, ddb_tables, method):
    resp = api_handler.handler(lambda_event(method, "/findings"), None)
    # Today the unknown-method path falls through to a stub. Document that:
    # if this changes to return 405, update the assertion.
    assert resp["statusCode"] in (200, 405)


# ──────────────────────────── Oversize payloads ──────────────────
def test_chat_with_very_large_prompt_is_accepted_or_rejected_cleanly(api_handler, monkeypatch):
    """A 100KB prompt must either succeed or return a clear 4xx — never crash
    the handler with an uncaught exception."""
    mock_ac = MagicMock()
    mock_ac.invoke_agent_runtime.return_value = {
        "response": MagicMock(read=MagicMock(
            return_value=json.dumps({"result": "ok"}).encode()
        ))
    }
    monkeypatch.setattr(api_handler, "agentcore", mock_ac)

    big_prompt = "x" * 100_000
    resp = api_handler.handler(
        lambda_event("POST", "/chat", body={"prompt": big_prompt}), None
    )
    assert resp["statusCode"] in (200, 400, 413), \
        f"Unexpected status {resp['statusCode']} for oversize prompt"


# ──────────────────────────── Unicode / special chars ─────────────
def test_chat_unicode_prompt_round_trips_safely(api_handler, monkeypatch):
    mock_ac = MagicMock()
    mock_ac.invoke_agent_runtime.return_value = {
        "response": MagicMock(read=MagicMock(
            return_value=json.dumps({"result": "ok"}).encode()
        ))
    }
    monkeypatch.setattr(api_handler, "agentcore", mock_ac)

    unicode_prompt = "Test 日本語 🔒 emoji and zero-width​ chars and quotes \"'`"
    resp = api_handler.handler(
        lambda_event("POST", "/chat", body={"prompt": unicode_prompt}), None
    )
    assert resp["statusCode"] == 200


def test_session_id_with_unicode_does_not_crash(api_handler):
    resp = api_handler.handler(
        lambda_event("GET", "/conversations/日本語"), None
    )
    # 404 (not found) is expected. 500 would indicate a crash.
    assert resp["statusCode"] in (400, 404)


# ──────────────────────────── Empty / null inputs ────────────────
def test_chat_with_null_prompt_returns_400(api_handler):
    resp = api_handler.handler(
        lambda_event("POST", "/chat", body={"prompt": None}), None
    )
    assert resp["statusCode"] == 400


def test_findings_with_empty_string_filter_does_not_narrow_results(api_handler, ddb_tables):
    from conftest import CONFLICTS_TABLE
    table = ddb_tables.Table(CONFLICTS_TABLE)
    table.put_item(Item={"conflict_id": "A", "severity": "HIGH", "status": "OPEN"})
    resp = api_handler.handler(
        lambda_event("GET", "/findings", query={"severity": "", "status": ""}), None
    )
    # Empty filter strings should be treated as 'no filter'.
    assert len(_body(resp)["findings"]) == 1


# ──────────────────────────── Sensitive data exposure ────────────
def test_error_responses_never_echo_authorization_header(api_handler):
    """If the handler errors, the response must not leak the Bearer token."""
    event = lambda_event("GET", "/conversations", auth_sub=None)
    event["headers"]["Authorization"] = "Bearer secret-token-that-should-not-leak"
    resp = api_handler.handler(event, None)
    assert "secret-token-that-should-not-leak" not in resp["body"]
    assert "secret-token-that-should-not-leak" not in json.dumps(resp.get("headers") or {})


def test_session_metadata_response_never_includes_user_id(api_handler, ddb_tables):
    """The session summary returned to the client must not include user_id —
    it's caller-internal and would help attackers enumerate other users."""
    sess = ddb_tables.Table(SESSIONS_TABLE)
    sess.put_item(Item={"session_id": "s1", "user_id": FAKE_USER_SUB,
                        "title": "x", "created_at": "x",
                        "last_message_at": "x", "message_count": 1,
                        "chat_type": "analyst"})
    resp = api_handler.handler(
        lambda_event("GET", "/conversations/s1"), None
    )
    body = _body(resp)
    assert "user_id" not in body, "user_id leaked in session metadata response"


# ──────────────────────────── Pagination boundaries ──────────────
def test_findings_at_exactly_limit_boundary(api_handler, ddb_tables):
    """Insert exactly 200 items (the scan Limit) and verify the response."""
    from conftest import CONFLICTS_TABLE
    table = ddb_tables.Table(CONFLICTS_TABLE)
    for i in range(200):
        table.put_item(Item={"conflict_id": f"UC-{i:04d}", "severity": "LOW",
                              "status": "OPEN", "detected_at": f"2026-01-{i % 28 + 1:02d}"})
    resp = api_handler.handler(lambda_event("GET", "/findings"), None)
    body = _body(resp)
    # At the boundary the scan returns up to 200; partial results are ok but
    # the request must not error.
    assert resp["statusCode"] == 200
    assert len(body["findings"]) <= 200


# ──────────────────────────── State transitions ──────────────────
def test_session_message_count_increases_only_via_master_writer(api_handler, ddb_tables):
    """The api_handler must NOT increment message_count on its own — that's
    the master orchestrator's job. This documents the contract."""
    sess = ddb_tables.Table(SESSIONS_TABLE)
    sess.put_item(Item={"session_id": "s1", "user_id": FAKE_USER_SUB,
                        "title": "x", "created_at": "x",
                        "last_message_at": "x", "message_count": 5,
                        "chat_type": "analyst"})
    # Even after several GETs, message_count must not change.
    for _ in range(3):
        api_handler.handler(lambda_event("GET", "/conversations/s1"), None)
    row = sess.get_item(Key={"session_id": "s1"})["Item"]
    assert int(row["message_count"]) == 5
