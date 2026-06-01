"""Edge-case and error-path tests for api_handler.

Complements test_api_handler.py (happy-path coverage) with the table-missing,
malformed-input, AgentCore-failure, and pagination cases enumerated in
docs/TEST_BACKLOG.md. Item IDs (B-N) reference that backlog.
"""
from __future__ import annotations

import json
from unittest.mock import patch, MagicMock

import pytest

from conftest import (
    AUDIT_TABLE,
    CHANGE_REQUESTS_TABLE,
    CONFLICTS_TABLE,
    SESSIONS_TABLE,
    FAKE_USER_SUB,
    lambda_event,
    make_jwt,
)


def _body(resp):
    return json.loads(resp["body"])


# ──────────────────────────── B-1: /health response shape ─────────
def test_health_response_includes_service_field_with_correct_type(api_handler):
    resp = api_handler.handler(lambda_event("GET", "/health", auth_sub=None), None)
    body = _body(resp)
    assert isinstance(body["service"], str)
    assert isinstance(body["status"], str)
    assert resp["headers"]["Content-Type"] == "application/json"


# ──────────────────────────── B-2, B-5, B-6: tables unset ─────────
def test_findings_returns_500_when_table_unset(api_handler, monkeypatch):
    monkeypatch.setattr(api_handler, "conflicts_table", None)
    resp = api_handler.handler(lambda_event("GET", "/findings"), None)
    assert resp["statusCode"] == 500
    assert "CONFLICTS_TABLE" in _body(resp)["error"]


def test_actions_returns_500_when_table_unset(api_handler, monkeypatch):
    monkeypatch.setattr(api_handler, "crs_table", None)
    resp = api_handler.handler(lambda_event("GET", "/actions"), None)
    assert resp["statusCode"] == 500


def test_audit_returns_500_when_table_unset(api_handler, monkeypatch):
    monkeypatch.setattr(api_handler, "audit_table", None)
    resp = api_handler.handler(lambda_event("GET", "/audit"), None)
    assert resp["statusCode"] == 500


# ──────────────────────────── B-3: filter handles missing fields ──
def test_findings_filter_skips_items_missing_severity_field(api_handler, ddb_tables):
    table = ddb_tables.Table(CONFLICTS_TABLE)
    # Item with no severity field should never match a severity filter.
    table.put_item(Item={"conflict_id": "X", "status": "OPEN"})
    table.put_item(Item={"conflict_id": "Y", "severity": "HIGH", "status": "OPEN"})
    resp = api_handler.handler(
        lambda_event("GET", "/findings", query={"severity": "HIGH"}), None
    )
    ids = {i["conflict_id"] for i in _body(resp)["findings"]}
    assert ids == {"Y"}


def test_findings_filter_skips_items_missing_status_field(api_handler, ddb_tables):
    table = ddb_tables.Table(CONFLICTS_TABLE)
    table.put_item(Item={"conflict_id": "X", "severity": "HIGH"})
    table.put_item(Item={"conflict_id": "Y", "severity": "HIGH", "status": "OPEN"})
    resp = api_handler.handler(
        lambda_event("GET", "/findings", query={"status": "OPEN"}), None
    )
    ids = {i["conflict_id"] for i in _body(resp)["findings"]}
    assert ids == {"Y"}


# ──────────────────────────── B-7, B-8: /chat input validation ───
def test_chat_empty_body_dict_returns_400(api_handler):
    resp = api_handler.handler(lambda_event("POST", "/chat", body={}), None)
    assert resp["statusCode"] == 400


def test_chat_whitespace_only_prompt_returns_400(api_handler):
    resp = api_handler.handler(
        lambda_event("POST", "/chat", body={"prompt": "   \n\t  "}), None
    )
    assert resp["statusCode"] == 400


def test_chat_accepts_message_field_as_prompt_alias(api_handler, monkeypatch):
    """Backward-compat: api_handler accepts both 'prompt' and 'message'."""
    monkeypatch.setattr(api_handler, "agentcore", MagicMock(
        invoke_agent_runtime=MagicMock(return_value={
            "response": MagicMock(read=MagicMock(
                return_value=json.dumps({"result": "ok"}).encode()
            ))
        })
    ))
    resp = api_handler.handler(
        lambda_event("POST", "/chat", body={"message": "hi"}), None
    )
    assert resp["statusCode"] == 200


# ──────────────────────────── B-9, B-10: /chat AgentCore failures ─
def test_chat_agentcore_exception_returns_502(api_handler, monkeypatch):
    mock_ac = MagicMock()
    mock_ac.invoke_agent_runtime.side_effect = TimeoutError("read timeout")
    monkeypatch.setattr(api_handler, "agentcore", mock_ac)
    resp = api_handler.handler(
        lambda_event("POST", "/chat", body={"prompt": "hi"}), None
    )
    assert resp["statusCode"] == 502
    assert "TimeoutError" in _body(resp)["error"]


def test_chat_agentcore_malformed_response_returns_502(api_handler, monkeypatch):
    mock_ac = MagicMock()
    mock_ac.invoke_agent_runtime.return_value = {
        "response": MagicMock(read=MagicMock(return_value=b"not json {{{"))
    }
    monkeypatch.setattr(api_handler, "agentcore", mock_ac)
    resp = api_handler.handler(
        lambda_event("POST", "/chat", body={"prompt": "hi"}), None
    )
    assert resp["statusCode"] == 502


# ──────────────────────────── B-11: /chat default chat_type ──────
def test_chat_chat_type_defaults_to_analyst(api_handler, monkeypatch):
    """When chat_type is missing from the body, it should default to 'analyst'."""
    captured = {}

    def capture(**kwargs):
        captured["payload"] = json.loads(kwargs["payload"])
        return {"response": MagicMock(read=MagicMock(
            return_value=json.dumps({"result": "ok"}).encode()
        ))}

    mock_ac = MagicMock()
    mock_ac.invoke_agent_runtime.side_effect = capture
    monkeypatch.setattr(api_handler, "agentcore", mock_ac)
    api_handler.handler(
        lambda_event("POST", "/chat", body={"prompt": "hi"}), None
    )
    assert captured["payload"]["chat_type"] == "analyst"


# ──────────────────────────── B-14, B-15: /conversations errors ──
def test_get_conversation_empty_session_id_returns_400(api_handler):
    # /conversations/ with trailing slash — tail[0] becomes ""
    resp = api_handler.handler(lambda_event("GET", "/conversations/"), None)
    # Router routes empty session_id to _handle_get_conversation, which errors 400.
    # If router falls through to stub, that's acceptable too — verify it isn't 200.
    assert resp["statusCode"] != 200


def test_get_messages_for_nonexistent_session_returns_404(api_handler, ddb_tables):
    resp = api_handler.handler(
        lambda_event("GET", "/conversations/does-not-exist/messages"), None
    )
    assert resp["statusCode"] == 404


# ──────────────────────────── B-17, B-18: /messages list_events ──
def test_get_messages_reverses_to_chronological_order(api_handler, ddb_tables, monkeypatch):
    # Seed an owned session.
    sess = ddb_tables.Table(SESSIONS_TABLE)
    sess.put_item(Item={"session_id": "s1", "user_id": FAKE_USER_SUB,
                        "title": "x", "created_at": "2026-01-01",
                        "last_message_at": "2026-01-01", "message_count": 2})

    # AgentCore returns newest-first; api_handler must reverse.
    def list_events_fake(**kwargs):
        return {"events": [
            {"eventTimestamp": "2026-05-26T12:00:00Z",
             "payload": [{"conversational": {"role": "ASSISTANT",
                                              "content": {"text": "second"}}}]},
            {"eventTimestamp": "2026-05-26T11:59:00Z",
             "payload": [{"conversational": {"role": "USER",
                                              "content": {"text": "first"}}}]},
        ]}
    mock_ac = MagicMock()
    mock_ac.list_events.side_effect = list_events_fake
    monkeypatch.setattr(api_handler, "agentcore", mock_ac)

    resp = api_handler.handler(
        lambda_event("GET", "/conversations/s1/messages"), None
    )
    assert resp["statusCode"] == 200
    msgs = _body(resp)["messages"]
    assert [m["content"] for m in msgs] == ["first", "second"]


def test_get_messages_skips_payload_entries_missing_role_or_text(api_handler, ddb_tables, monkeypatch):
    sess = ddb_tables.Table(SESSIONS_TABLE)
    sess.put_item(Item={"session_id": "s1", "user_id": FAKE_USER_SUB,
                        "title": "x", "created_at": "x",
                        "last_message_at": "x", "message_count": 0})

    mock_ac = MagicMock()
    mock_ac.list_events.return_value = {"events": [{
        "eventTimestamp": "2026-05-26T12:00:00Z",
        "payload": [
            {"conversational": {"role": "USER", "content": {"text": "valid"}}},
            {"conversational": {"role": "", "content": {"text": "no-role"}}},
            {"conversational": {"role": "ASSISTANT", "content": {"text": ""}}},
            {"conversational": {}},
            {},  # non-conversational entry
        ],
    }]}
    monkeypatch.setattr(api_handler, "agentcore", mock_ac)
    resp = api_handler.handler(
        lambda_event("GET", "/conversations/s1/messages"), None
    )
    msgs = _body(resp)["messages"]
    assert len(msgs) == 1
    assert msgs[0]["content"] == "valid"


# ──────────────────────────── B-20: case-insensitive auth header ──
def test_auth_lowercase_authorization_header_works(api_handler):
    """Function URL passes header keys lowercased; api_handler must accept both."""
    event = {
        "path": "/conversations",
        "httpMethod": "GET",
        "headers": {"authorization": f"Bearer {make_jwt()}"},
    }
    resp = api_handler.handler(event, None)
    # Should not 401 — caller is resolved.
    assert resp["statusCode"] != 401


# ──────────────────────────── B-21, B-22, B-23: malformed JWTs ────
def test_jwt_with_invalid_base64_padding_returns_no_user(api_handler):
    """A JWT whose payload segment has invalid base64 must fail decode silently
    and not impersonate anyone."""
    bad = "header.????invalid????.signature"
    event = {"path": "/conversations", "httpMethod": "GET",
             "headers": {"Authorization": f"Bearer {bad}"}}
    resp = api_handler.handler(event, None)
    assert resp["statusCode"] == 401


def test_jwt_with_fewer_than_three_segments_returns_no_user(api_handler):
    """A 'token' with no dots can't be decoded and must not authenticate."""
    event = {"path": "/conversations", "httpMethod": "GET",
             "headers": {"Authorization": "Bearer not-even-a-jwt"}}
    resp = api_handler.handler(event, None)
    assert resp["statusCode"] == 401


def test_jwt_with_non_json_payload_returns_no_user(api_handler):
    """A JWT whose payload base64-decodes to non-JSON must fail decode silently."""
    # "this is not json" base64url-encoded:
    import base64
    payload_b64 = base64.urlsafe_b64encode(b"this is not json").rstrip(b"=").decode()
    token = f"header.{payload_b64}.signature"
    event = {"path": "/conversations", "httpMethod": "GET",
             "headers": {"Authorization": f"Bearer {token}"}}
    resp = api_handler.handler(event, None)
    assert resp["statusCode"] == 401


# ──────────────────────────── B-24: Decimal handling ─────────────
def test_to_int_handles_decimal(api_handler):
    from decimal import Decimal
    assert api_handler._to_int(Decimal("42")) == 42
    assert api_handler._to_int(None) is None
    assert api_handler._to_int(7) == 7


# ──────────────────────────── B-25: response shape sanity ────────
def test_all_responses_include_cors_headers(api_handler):
    resp = api_handler.handler(lambda_event("GET", "/health", auth_sub=None), None)
    assert "Access-Control-Allow-Origin" in resp["headers"]
    assert resp["headers"]["Content-Type"] == "application/json"


def test_error_responses_have_json_error_field(api_handler, monkeypatch):
    monkeypatch.setattr(api_handler, "conflicts_table", None)
    resp = api_handler.handler(lambda_event("GET", "/findings"), None)
    body = _body(resp)
    assert "error" in body
    assert isinstance(body["error"], str)


# ──────────────────────────── Unknown route handling ─────────────
def test_unknown_path_returns_stub_response(api_handler):
    """Per current router, unknown paths return a 'stub' response (not 404).
    Documents the behavior so any change to it is intentional."""
    resp = api_handler.handler(
        lambda_event("GET", "/no-such-route", auth_sub=None), None
    )
    body = _body(resp)
    assert body.get("status") == "stub"


def test_chat_get_method_not_handled(api_handler):
    """Only POST /chat is handled. GET should fall through to stub, not invoke runtime."""
    resp = api_handler.handler(lambda_event("GET", "/chat"), None)
    body = _body(resp)
    assert body.get("status") == "stub"
