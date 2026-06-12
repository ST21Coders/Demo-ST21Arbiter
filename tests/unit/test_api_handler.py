"""Unit tests for Infra/functions/api_handler/api_handler.py.

Hits moto-mocked DynamoDB — no real AWS calls. Covers every route the
handler exposes plus the auth resolution paths.
"""
from __future__ import annotations

import json

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


# ──────────────────────────── /health ──────────────────────────
def test_health_returns_200_with_no_auth(api_handler):
    resp = api_handler.handler(lambda_event("GET", "/health", auth_sub=None), None)
    assert resp["statusCode"] == 200
    body = _body(resp)
    assert body["status"] == "healthy"
    assert body["service"] == "arbiter-api"


# ──────────────────────────── /findings ────────────────────────
def test_findings_returns_empty_list_when_table_empty(api_handler):
    resp = api_handler.handler(lambda_event("GET", "/findings"), None)
    assert resp["statusCode"] == 200
    assert _body(resp) == {"findings": []}


def test_findings_returns_seeded_items_newest_first(api_handler, ddb_tables):
    table = ddb_tables.Table(CONFLICTS_TABLE)
    table.put_item(Item={"conflict_id": "UC-OLD", "severity": "HIGH", "status": "OPEN",
                         "detected_at": "2026-01-01T00:00:00Z"})
    table.put_item(Item={"conflict_id": "UC-NEW", "severity": "HIGH", "status": "OPEN",
                         "detected_at": "2026-05-26T00:00:00Z"})
    resp = api_handler.handler(lambda_event("GET", "/findings"), None)
    items = _body(resp)["findings"]
    assert [i["conflict_id"] for i in items] == ["UC-NEW", "UC-OLD"]


def test_findings_severity_filter_narrows_results(api_handler, ddb_tables):
    table = ddb_tables.Table(CONFLICTS_TABLE)
    table.put_item(Item={"conflict_id": "A", "severity": "HIGH", "status": "OPEN",
                         "detected_at": "2026-05-01"})
    table.put_item(Item={"conflict_id": "B", "severity": "LOW", "status": "OPEN",
                         "detected_at": "2026-05-02"})
    resp = api_handler.handler(
        lambda_event("GET", "/findings", query={"severity": "HIGH"}), None
    )
    items = _body(resp)["findings"]
    assert {i["conflict_id"] for i in items} == {"A"}


def test_findings_status_filter_is_case_insensitive(api_handler, ddb_tables):
    table = ddb_tables.Table(CONFLICTS_TABLE)
    table.put_item(Item={"conflict_id": "A", "severity": "HIGH", "status": "OPEN"})
    table.put_item(Item={"conflict_id": "B", "severity": "HIGH", "status": "CLOSED"})
    resp = api_handler.handler(
        lambda_event("GET", "/findings", query={"status": "open"}), None
    )
    assert {i["conflict_id"] for i in _body(resp)["findings"]} == {"A"}


# ──────────────────────────── /actions ─────────────────────────
def test_actions_returns_empty_list_when_table_empty(api_handler):
    resp = api_handler.handler(lambda_event("GET", "/actions"), None)
    assert resp["statusCode"] == 200
    assert _body(resp) == {"change_requests": []}


def test_actions_sorted_newest_first(api_handler, ddb_tables):
    table = ddb_tables.Table(CHANGE_REQUESTS_TABLE)
    table.put_item(Item={"cr_id": "CR-OLD", "created_at": "2026-01-01"})
    table.put_item(Item={"cr_id": "CR-NEW", "created_at": "2026-05-26"})
    resp = api_handler.handler(lambda_event("GET", "/actions"), None)
    items = _body(resp)["change_requests"]
    assert [i["cr_id"] for i in items] == ["CR-NEW", "CR-OLD"]


# ──────────────────────────── /audit ───────────────────────────
def test_audit_sorted_newest_first(api_handler, ddb_tables):
    table = ddb_tables.Table(AUDIT_TABLE)
    table.put_item(Item={"log_id": "L1", "timestamp": "2026-01-01"})
    table.put_item(Item={"log_id": "L2", "timestamp": "2026-05-26"})
    resp = api_handler.handler(lambda_event("GET", "/audit"), None)
    assert [i["log_id"] for i in _body(resp)["logs"]] == ["L2", "L1"]


# ──────────────────────────── /chat ────────────────────────────
def test_chat_missing_prompt_returns_400(api_handler):
    resp = api_handler.handler(
        lambda_event("POST", "/chat", body={"session_id": "s1"}), None
    )
    assert resp["statusCode"] == 400
    assert "prompt" in _body(resp)["error"].lower()


def test_chat_invalid_json_body_returns_400(api_handler):
    resp = api_handler.handler(
        lambda_event("POST", "/chat", body="{not-json"), None
    )
    assert resp["statusCode"] == 400


def test_chat_no_runtime_arn_returns_503(api_handler, monkeypatch):
    monkeypatch.setattr(api_handler, "MASTER_AGENT_RUNTIME_ARN", "")
    resp = api_handler.handler(
        lambda_event("POST", "/chat", body={"prompt": "hi"}), None
    )
    assert resp["statusCode"] == 503


# ──────────────────────────── /jira/tickets ────────────────────
def test_jira_ticket_query_invokes_deterministic_read_action(api_handler, monkeypatch):
    calls = []

    def fake_invoke(action, args):
        calls.append((action, args))
        return {
            "status": "ok",
            "jql": 'project = "DEVARBITER"',
            "issues": [{"key": "DEVARBITER-12", "summary": "Review policy"}],
            "total": 1,
        }

    monkeypatch.setattr(api_handler, "_invoke_jira_action", fake_invoke)
    resp = api_handler.handler(
        lambda_event("GET", "/jira/tickets", query={"project_key": "DEVARBITER", "limit": "10"}),
        None,
    )
    assert resp["statusCode"] == 200
    assert _body(resp)["issues"][0]["key"] == "DEVARBITER-12"
    assert calls == [("query_issues", {
        "jql": "",
        "project_key": "DEVARBITER",
        "status": "",
        "text": "",
        "assignee": "",
        "limit": "10",
    })]


def test_jira_ticket_get_fetches_single_issue(api_handler, monkeypatch):
    def fake_invoke(action, args):
        assert action == "query_issues"
        assert args == {"issue_key": "DEVARBITER-12", "limit": 1}
        return {"status": "ok", "issue": {"key": "DEVARBITER-12"}, "issues": [{"key": "DEVARBITER-12"}]}

    monkeypatch.setattr(api_handler, "_invoke_jira_action", fake_invoke)
    resp = api_handler.handler(lambda_event("GET", "/jira/tickets/DEVARBITER-12"), None)
    assert resp["statusCode"] == 200
    assert _body(resp)["issue"]["key"] == "DEVARBITER-12"


def test_jira_ticket_query_runtime_error_returns_502(api_handler, monkeypatch):
    monkeypatch.setattr(api_handler, "_invoke_jira_action", lambda action, args: {"error": "not configured"})
    resp = api_handler.handler(lambda_event("GET", "/jira/tickets"), None)
    assert resp["statusCode"] == 502
    assert "jira query failed" in _body(resp)["error"].lower()


# ──────────────────────────── /conversations ───────────────────
def test_conversations_unauthenticated_returns_401(api_handler):
    resp = api_handler.handler(
        lambda_event("GET", "/conversations", auth_sub=None), None
    )
    assert resp["statusCode"] == 401


def test_conversations_returns_only_calling_users_sessions(api_handler, ddb_tables):
    table = ddb_tables.Table(SESSIONS_TABLE)
    table.put_item(Item={"session_id": "mine", "user_id": FAKE_USER_SUB,
                         "title": "Mine", "created_at": "2026-05-26",
                         "last_message_at": "2026-05-26", "message_count": 2,
                         "chat_type": "analyst"})
    table.put_item(Item={"session_id": "theirs", "user_id": "someone-else",
                         "title": "Theirs", "created_at": "2026-05-26",
                         "last_message_at": "2026-05-26", "message_count": 2,
                         "chat_type": "analyst"})
    resp = api_handler.handler(lambda_event("GET", "/conversations"), None)
    assert resp["statusCode"] == 200
    sessions = _body(resp)["sessions"]
    assert [s["session_id"] for s in sessions] == ["mine"]


def test_conversations_type_filter(api_handler, ddb_tables):
    table = ddb_tables.Table(SESSIONS_TABLE)
    for sid, ctype in [("a", "analyst"), ("m", "mcp"), ("legacy", None)]:
        item = {"session_id": sid, "user_id": FAKE_USER_SUB,
                "title": sid, "created_at": "2026-05-26",
                "last_message_at": "2026-05-26", "message_count": 1}
        if ctype:
            item["chat_type"] = ctype
        table.put_item(Item=item)
    resp = api_handler.handler(
        lambda_event("GET", "/conversations", query={"type": "mcp"}), None
    )
    sids = {s["session_id"] for s in _body(resp)["sessions"]}
    assert sids == {"m"}


# ──────────────────────────── /conversations/{id} ──────────────
def test_get_conversation_other_users_returns_404(api_handler, ddb_tables):
    table = ddb_tables.Table(SESSIONS_TABLE)
    table.put_item(Item={"session_id": "theirs", "user_id": "someone-else",
                         "title": "Not yours", "created_at": "2026-05-26",
                         "last_message_at": "2026-05-26", "message_count": 1})
    resp = api_handler.handler(lambda_event("GET", "/conversations/theirs"), None)
    assert resp["statusCode"] == 404


# ──────────────────────────── auth resolution ──────────────────
def test_caller_user_id_prefers_authorizer_claims_over_header(api_handler):
    event = lambda_event("GET", "/conversations", use_authorizer_claims=True)
    event["headers"]["Authorization"] = f"Bearer {make_jwt(sub='other-sub')}"
    assert api_handler._caller_user_id(event) == FAKE_USER_SUB


def test_caller_user_id_decodes_function_url_bearer(api_handler):
    event = lambda_event("GET", "/conversations")
    assert api_handler._caller_user_id(event) == FAKE_USER_SUB


def test_caller_user_id_returns_none_when_no_auth(api_handler):
    event = lambda_event("GET", "/conversations", auth_sub=None)
    assert api_handler._caller_user_id(event) is None
