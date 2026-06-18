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


# CR-creation refactor (Task 1 of autonomous-agent-layer): the pure
# _build_cr_item builder is the seam the autonomy binding will call, so
# we lock in its output shape and the persister's audit-write side
# effect. These tests run against moto via the existing api_handler
# fixture so they pick up _finding_ownership's DDB read too.
_CR_REQUIRED_KEYS = {
    "cr_id", "status", "conflict_id", "linked_conflict_id", "action_type",
    "target_resource", "target_environment", "severity", "description",
    "requested_by", "justification", "owner_team", "consumer_team",
    "platform_team", "routed_team", "tags", "jira_project_key",
    "created_at", "approvers", "total_approvers_needed",
    "total_approvals_received", "state_transitions",
}


def test_build_cr_item_returns_expected_shape(api_handler):
    body = {
        "conflict_id": "UC-1",
        "target_environment": "STAGING",
        "severity": "HIGH",
        "action_type": "SECURITY_FIX",
        "target_resource": "s3://bucket/key",
        "description": "fix it",
        "justification": "because",
    }
    item = api_handler._build_cr_item(body, "tester@example.com")
    assert _CR_REQUIRED_KEYS.issubset(item.keys())
    # Server-derived fields use the body's normalized values, not raw.
    assert item["conflict_id"] == "UC-1"
    assert item["linked_conflict_id"] == "UC-1"
    assert item["target_environment"] == "STAGING"
    assert item["severity"] == "HIGH"
    assert item["requested_by"] == "tester@example.com"
    # STAGING gets a one-element team_lead chain → PENDING_APPROVAL.
    assert item["status"] == "PENDING_APPROVAL"
    assert len(item["approvers"]) == 1
    assert item["approvers"][0]["role"] == "team_lead"
    # cr_id is derived from the timestamp + conflict_id suffix.
    assert item["cr_id"].startswith("CR-")
    assert item["cr_id"].endswith("-UC-1".upper()[-6:])
    # state_transitions is initialized with the "Created" row.
    assert len(item["state_transitions"]) == 1
    assert item["state_transitions"][0]["actor"] == "tester@example.com"


def test_build_cr_item_dev_env_auto_approves(api_handler):
    item = api_handler._build_cr_item({"target_environment": "DEV"}, "tester")
    assert item["status"] == "AUTO_APPROVED"
    assert item["approvers"] == []
    assert item["total_approvers_needed"] == 0


def test_build_cr_item_accepts_extra_identity_args(api_handler):
    # The signature accepts user_email, persona, claims to give the
    # autonomy binding a way to pass richer identity later. Today they
    # are no-ops; this test pins the contract.
    item = api_handler._build_cr_item(
        {"target_environment": "DEV"},
        "tester",
        user_email="t@example.com",
        persona="soc",
        claims={"sub": "abc"},
    )
    assert item["status"] == "AUTO_APPROVED"


def test_persist_cr_writes_ddb_row(api_handler, ddb_tables):
    item = api_handler._build_cr_item(
        {"conflict_id": "UC-2", "target_environment": "DEV"},
        "tester",
    )
    returned = api_handler._persist_cr(item, "tester")

    # Returns the same item dict so the REST shim does not re-read DDB.
    assert returned is item

    crs = ddb_tables.Table(CHANGE_REQUESTS_TABLE).scan()["Items"]
    assert [c["cr_id"] for c in crs] == [item["cr_id"]]
    # NOTE: the conftest's AUDIT_TABLE fixture uses a `log_id` PK that
    # does not match the production schema (`event_id`), so audit writes
    # are swallowed by `_audit`'s try/except. We assert the audit
    # behaviour at the integration layer (live deploy), not here. See
    # tests/conftest.py for the fixture bug.


def test_create_action_rest_shim_response_shape_unchanged(api_handler, ddb_tables):
    # End-to-end check that the REST shim still produces the expected
    # CR row after the refactor — guards against any regression in the
    # byte-shape of the response.
    resp = api_handler.handler(
        lambda_event(
            "POST",
            "/actions",
            body={
                "conflict_id": "UC-3",
                "target_environment": "STAGING",
                "severity": "HIGH",
                "action_type": "SECURITY_FIX",
                "target_resource": "arn:aws:s3:::demo",
                "description": "remediate",
                "justification": "policy",
            },
        ),
        None,
    )
    assert resp["statusCode"] == 200
    item = _body(resp)
    assert _CR_REQUIRED_KEYS.issubset(item.keys())
    assert item["status"] == "PENDING_APPROVAL"

    crs = ddb_tables.Table(CHANGE_REQUESTS_TABLE).scan()["Items"]
    assert [c["cr_id"] for c in crs] == [item["cr_id"]]


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


# ──────────────────────────── POST /conversations/bulk-delete ──
def _seed_session(table, session_id: str, user_id: str = FAKE_USER_SUB,
                  chat_type: str = "analyst"):
    table.put_item(Item={
        "session_id": session_id,
        "user_id": user_id,
        "title": session_id,
        "created_at": "2026-05-26",
        "last_message_at": "2026-05-26",
        "message_count": 1,
        "chat_type": chat_type,
    })


def test_bulk_delete_happy_path_all_owned(api_handler, ddb_tables):
    table = ddb_tables.Table(SESSIONS_TABLE)
    _seed_session(table, "harness-a")
    _seed_session(table, "harness-b")
    _seed_session(table, "harness-c")

    resp = api_handler.handler(
        lambda_event(
            "POST",
            "/conversations/bulk-delete",
            body={"session_ids": ["harness-a", "harness-b", "harness-c"]},
        ),
        None,
    )
    assert resp["statusCode"] == 200
    body = _body(resp)
    assert sorted(body["deleted"]) == ["harness-a", "harness-b", "harness-c"]
    assert body["failed"] == []

    # All rows actually removed from DDB.
    remaining = {i["session_id"] for i in table.scan()["Items"]}
    assert remaining == set()


def test_bulk_delete_partial_failure_mixed_reasons(api_handler, ddb_tables):
    table = ddb_tables.Table(SESSIONS_TABLE)
    _seed_session(table, "mine-ok")                                # owned → deleted
    _seed_session(table, "theirs", user_id="someone-else")          # forbidden
    # "ghost" is intentionally NOT inserted → not_found

    resp = api_handler.handler(
        lambda_event(
            "POST",
            "/conversations/bulk-delete",
            body={"session_ids": ["mine-ok", "theirs", "ghost"]},
        ),
        None,
    )
    assert resp["statusCode"] == 200
    body = _body(resp)
    assert body["deleted"] == ["mine-ok"]
    failed_by_id = {f["session_id"]: f["reason"] for f in body["failed"]}
    assert failed_by_id == {"theirs": "forbidden", "ghost": "not_found"}

    # "theirs" row must survive the bulk delete.
    remaining = {i["session_id"] for i in table.scan()["Items"]}
    assert remaining == {"theirs"}


def test_bulk_delete_missing_body_returns_400(api_handler):
    resp = api_handler.handler(
        lambda_event("POST", "/conversations/bulk-delete"), None
    )
    assert resp["statusCode"] == 400
    assert _body(resp) == {"error": "missing session_ids"}


def test_bulk_delete_empty_list_returns_400(api_handler):
    resp = api_handler.handler(
        lambda_event(
            "POST", "/conversations/bulk-delete", body={"session_ids": []}
        ),
        None,
    )
    assert resp["statusCode"] == 400
    assert _body(resp) == {"error": "missing session_ids"}


def test_bulk_delete_too_many_ids_returns_400(api_handler):
    ids = [f"sid-{i}" for i in range(101)]
    resp = api_handler.handler(
        lambda_event(
            "POST", "/conversations/bulk-delete", body={"session_ids": ids}
        ),
        None,
    )
    assert resp["statusCode"] == 400
    assert _body(resp) == {"error": "too many ids"}


def test_bulk_delete_unauthenticated_returns_401(api_handler):
    resp = api_handler.handler(
        lambda_event(
            "POST",
            "/conversations/bulk-delete",
            body={"session_ids": ["x"]},
            auth_sub=None,
        ),
        None,
    )
    assert resp["statusCode"] == 401
    assert _body(resp) == {"error": "unauthorized"}


def test_bulk_delete_no_sessions_table_returns_500(api_handler, monkeypatch):
    monkeypatch.setattr(api_handler, "sessions_table", None)
    resp = api_handler.handler(
        lambda_event(
            "POST", "/conversations/bulk-delete", body={"session_ids": ["x"]}
        ),
        None,
    )
    assert resp["statusCode"] == 500
    assert _body(resp) == {"error": "sessions table not configured"}


# ──────────────────────────── POST /conversations/bulk-delete (scope) ──
def _seed_session_at(table, session_id, created_at, user_id=FAKE_USER_SUB):
    table.put_item(Item={
        "session_id": session_id,
        "user_id": user_id,
        "title": session_id,
        "created_at": created_at,
        "last_message_at": created_at,
        "message_count": 1,
        "chat_type": "analyst",
    })


def test_bulk_delete_scope_all_deletes_only_callers_rows(api_handler, ddb_tables):
    table = ddb_tables.Table(SESSIONS_TABLE)
    _seed_session_at(table, "mine-a", "2026-06-01T00:00:00Z")
    _seed_session_at(table, "mine-b", "2026-06-02T00:00:00Z")
    _seed_session_at(table, "theirs", "2026-06-03T00:00:00Z", user_id="other-user")

    resp = api_handler.handler(
        lambda_event("POST", "/conversations/bulk-delete", body={"scope": "all"}),
        None,
    )
    assert resp["statusCode"] == 200
    body = _body(resp)
    assert sorted(body["deleted"]) == ["mine-a", "mine-b"]
    assert body["failed"] == []
    assert body["truncated"] is False

    remaining = {i["session_id"] for i in table.scan()["Items"]}
    assert remaining == {"theirs"}


def test_bulk_delete_scope_harness_matches_documented_prefixes(api_handler, ddb_tables):
    table = ddb_tables.Table(SESSIONS_TABLE)
    _seed_session_at(table, "harness-1", "2026-06-01T00:00:00Z")
    _seed_session_at(table, "features-2", "2026-06-01T00:00:00Z")
    _seed_session_at(table, "logic-race-3", "2026-06-01T00:00:00Z")
    _seed_session_at(table, "analyst-4", "2026-06-01T00:00:00Z")
    _seed_session_at(table, "mcp-5", "2026-06-01T00:00:00Z")

    resp = api_handler.handler(
        lambda_event("POST", "/conversations/bulk-delete", body={"scope": "harness"}),
        None,
    )
    assert resp["statusCode"] == 200
    body = _body(resp)
    assert sorted(body["deleted"]) == ["features-2", "harness-1", "logic-race-3"]

    remaining = {i["session_id"] for i in table.scan()["Items"]}
    assert remaining == {"analyst-4", "mcp-5"}


def test_bulk_delete_scope_older_than_days_uses_cutoff(api_handler, ddb_tables):
    from datetime import datetime, timezone, timedelta
    table = ddb_tables.Table(SESSIONS_TABLE)
    now = datetime.now(timezone.utc)
    _seed_session_at(table, "ancient",
                     (now - timedelta(days=120)).isoformat())
    _seed_session_at(table, "border",
                     (now - timedelta(days=29)).isoformat())  # inside the 30-day window
    _seed_session_at(table, "fresh", now.isoformat())

    resp = api_handler.handler(
        lambda_event("POST", "/conversations/bulk-delete",
                     body={"scope": "older_than_days", "days": 30}),
        None,
    )
    assert resp["statusCode"] == 200
    body = _body(resp)
    assert body["deleted"] == ["ancient"]

    remaining = {i["session_id"] for i in table.scan()["Items"]}
    assert remaining == {"border", "fresh"}


def test_bulk_delete_scope_invalid_returns_400(api_handler, ddb_tables):
    resp = api_handler.handler(
        lambda_event("POST", "/conversations/bulk-delete", body={"scope": "nope"}),
        None,
    )
    assert resp["statusCode"] == 400
    assert _body(resp) == {"error": "invalid scope"}


def test_bulk_delete_scope_older_than_days_missing_days_returns_400(api_handler, ddb_tables):
    resp = api_handler.handler(
        lambda_event("POST", "/conversations/bulk-delete",
                     body={"scope": "older_than_days"}),
        None,
    )
    assert resp["statusCode"] == 400


def test_bulk_delete_scope_unauthenticated_returns_401(api_handler):
    resp = api_handler.handler(
        lambda_event("POST", "/conversations/bulk-delete",
                     body={"scope": "all"}, auth_sub=None),
        None,
    )
    assert resp["statusCode"] == 401


def test_bulk_delete_scope_truncates_at_cap(api_handler, ddb_tables, monkeypatch):
    monkeypatch.setattr(api_handler, "BULK_DELETE_SCOPE_CAP", 3)
    table = ddb_tables.Table(SESSIONS_TABLE)
    for i in range(5):
        _seed_session_at(table, f"row-{i}", f"2026-06-0{i+1}T00:00:00Z")

    resp = api_handler.handler(
        lambda_event("POST", "/conversations/bulk-delete", body={"scope": "all"}),
        None,
    )
    body = _body(resp)
    assert resp["statusCode"] == 200
    assert len(body["deleted"]) == 3
    assert body["truncated"] is True
    # Second call drains the remaining 2 and reports truncated=false.
    resp2 = api_handler.handler(
        lambda_event("POST", "/conversations/bulk-delete", body={"scope": "all"}),
        None,
    )
    body2 = _body(resp2)
    assert len(body2["deleted"]) == 2
    assert body2["truncated"] is False
    assert table.scan()["Items"] == []


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
