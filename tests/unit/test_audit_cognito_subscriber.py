"""Unit tests for the Cognito audit subscriber Lambda.

The handler module reads ``AUDIT_LOG_TABLE_NAME`` at import time and constructs
a module-level boto3 DynamoDB resource, so each test re-imports the module
under a fresh env + monkeypatched ``boto3.resource`` to avoid hitting real AWS.

Three scenarios are covered:

1. Happy path — every field present, one PutItem with the expected shape.
2. Missing fields — CloudTrail redacted username and additionalEventData,
   PutItem still goes out with ``actor_id="unknown"`` and other defaults.
3. DDB raises — handler returns 200 and logs a warning (best-effort semantics
   so EventBridge does not retry on transient throttles).
"""

from __future__ import annotations

import importlib
import json
import sys
import time
from pathlib import Path
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError

# Make the subscriber package importable. The tests/ conftest only wires up the
# api_handler path; this module lives under a different functions/ directory.
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "Infra" / "functions" / "audit_cognito_subscriber"))


_TABLE_NAME = "test-arbiter-audit-log"
_TTL_SECONDS = 7_776_000


class _FakeTable:
    """Captures put_item calls. Optionally raises a configured exception so the
    test can drive the best-effort error path."""

    def __init__(self, raise_exc: Exception | None = None) -> None:
        self.items: list[dict] = []
        self._raise = raise_exc

    def put_item(self, *, Item: dict) -> dict:  # noqa: N803 — boto3 kwarg
        if self._raise is not None:
            raise self._raise
        self.items.append(Item)
        return {}


def _import_handler(monkeypatch, *, fake_table: _FakeTable):
    """Re-import the subscriber module with AUDIT_LOG_TABLE_NAME set and the
    ``boto3.resource("dynamodb").Table(...)`` chain stubbed to return our
    list-capturing fake table."""
    monkeypatch.setenv("AUDIT_LOG_TABLE_NAME", _TABLE_NAME)

    class _FakeResource:
        def Table(self, name: str):  # noqa: N802 — boto3 method name
            assert name == _TABLE_NAME
            return fake_table

    real_resource = boto3.resource

    def _resource(service: str, **kw):
        if service == "dynamodb":
            return _FakeResource()
        return real_resource(service, **kw)

    monkeypatch.setattr("boto3.resource", _resource)

    if "handler" in sys.modules:
        del sys.modules["handler"]
    return importlib.import_module("handler")


def _event(detail: dict[str, Any]) -> dict[str, Any]:
    """Wrap a CloudTrail-shaped detail in the surrounding EventBridge envelope."""
    return {
        "version": "0",
        "id": "ev-abc",
        "detail-type": "AWS API Call via CloudTrail",
        "source": "aws.cognito-idp",
        "account": "669810405473",
        "time": "2026-06-12T12:34:56Z",
        "region": "us-east-1",
        "detail": detail,
    }


# ──────────────────────── 1. Happy path ────────────────────────────────────
def test_happy_path_writes_auth_failed_row(monkeypatch):
    fake = _FakeTable()
    handler = _import_handler(monkeypatch, fake_table=fake)

    detail = {
        "eventTime": "2026-06-12T12:34:56Z",
        "eventSource": "cognito-idp.amazonaws.com",
        "eventName": "AdminInitiateAuth",
        "errorCode": "NotAuthorizedException",
        "errorMessage": "Incorrect username or password.",
        "sourceIPAddress": "203.0.113.42",
        "userAgent": "aws-cli/2.x",
        "requestParameters": {
            "userPoolId": "us-east-1_xxxxxxxxx",
            "clientId": "abc123",
            "authFlow": "USER_PASSWORD_AUTH",
            "username": "soc_marcus@example.com",
        },
        "responseElements": None,
        "additionalEventData": {"userIdentifier": "soc_marcus@example.com"},
        "eventID": "abc-def-123",
        "eventType": "AwsApiCall",
    }

    t_before = int(time.time())
    resp = handler.handler(_event(detail), None)
    t_after = int(time.time())

    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["written"] is True
    assert body["action_type"] == "AUTH_FAILED"

    assert len(fake.items) == 1
    item = fake.items[0]
    assert item["action_type"] == "AUTH_FAILED"
    assert item["status"] == "NotAuthorizedException"
    assert item["resource"] == "soc_marcus@example.com"
    assert item["user"] == "soc_marcus@example.com"
    assert item["timestamp"] == "2026-06-12T12:34:56Z"
    assert item["event_id"].startswith("AUTH_FAILED-abc-def-123-")

    details = json.loads(item["details"])
    assert details["source_ip"] == "203.0.113.42"
    assert details["error_code"] == "NotAuthorizedException"
    assert details["user_agent"] == "aws-cli/2.x"
    assert details["event_id_cloudtrail"] == "abc-def-123"
    assert details["event_time"] == "2026-06-12T12:34:56Z"
    assert details["event_name"] == "AdminInitiateAuth"

    # 90-day TTL with a 5-second slop for test runtime.
    assert t_before + _TTL_SECONDS <= item["ttl"] <= t_after + _TTL_SECONDS + 5


# ──────────────────────── 2. Missing fields ────────────────────────────────
def test_missing_fields_fall_back_to_unknown(monkeypatch):
    fake = _FakeTable()
    handler = _import_handler(monkeypatch, fake_table=fake)

    detail = {
        # No eventName, no errorCode, no sourceIPAddress, no userAgent.
        "eventTime": "2026-06-12T01:02:03Z",
        "eventSource": "cognito-idp.amazonaws.com",
        "additionalEventData": None,
        # requestParameters present but no username.
        "requestParameters": {
            "userPoolId": "us-east-1_xxxxxxxxx",
            "clientId": "abc123",
        },
        "eventID": "evt-redacted",
    }

    resp = handler.handler(_event(detail), None)

    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["written"] is True

    assert len(fake.items) == 1
    item = fake.items[0]
    assert item["resource"] == "unknown"
    assert item["user"] == "unknown"
    assert item["status"] == "unknown"
    details = json.loads(item["details"])
    assert details["source_ip"] == "unknown"
    assert details["user_agent"] == "unknown"
    assert details["error_code"] == "unknown"
    assert details["event_name"] == ""
    assert details["event_id_cloudtrail"] == "evt-redacted"


# ─────────────── 2b. CloudTrail-redacted authParameters (real shape) ──────
# Regression for the 2026-06-15 outage: the original _extract_actor called
# .get("USERNAME") on authParameters, which Cognito redacts to the literal
# string "HIDDEN_DUE_TO_SECURITY_REASONS" on failed InitiateAuth. Strings have
# no .get(), so the Lambda crashed on every real event and EventBridge retried
# fruitlessly. This test pins the real CloudTrail shape so a regression brings
# the test red before it brings prod red.
def test_redacted_authparameters_does_not_crash(monkeypatch):
    fake = _FakeTable()
    handler = _import_handler(monkeypatch, fake_table=fake)

    event = {
        "id": "real-shape-1",
        "detail-type": "AWS API Call via CloudTrail",
        "source": "aws.cognito-idp",
        "detail": {
            "eventTime": "2026-06-15T18:48:00Z",
            "eventName": "InitiateAuth",
            "errorCode": "NotAuthorizedException",
            "sourceIPAddress": "203.0.113.99",
            "userAgent": "Boto3/1.43.12",
            "requestParameters": {
                "authFlow": "USER_PASSWORD_AUTH",
                # The real shape: a string, not a dict.
                "authParameters": "HIDDEN_DUE_TO_SECURITY_REASONS",
                "clientId": "7jj2t2ng0nta408g0h302f39ks",
            },
            "eventID": "ct-12345",
        },
    }

    result = handler.handler(event, None)

    # Must not crash. Must still write the row. Username falls back to unknown
    # because no extractable field was available.
    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body["written"] is True
    assert len(fake.items) == 1
    item = fake.items[0]
    assert item["user"] == "unknown"
    assert item["action_type"] == "AUTH_FAILED"
    assert item["status"] == "NotAuthorizedException"
    # Source IP and other top-level fields survive.
    details = json.loads(item["details"])
    assert details["source_ip"] == "203.0.113.99"


# ──────────────────────── 3. DDB raises ────────────────────────────────────
def test_ddb_failure_does_not_raise(monkeypatch, caplog):
    err = ClientError(
        {
            "Error": {
                "Code": "ProvisionedThroughputExceededException",
                "Message": "Rate exceeded",
            }
        },
        "PutItem",
    )
    fake = _FakeTable(raise_exc=err)
    handler = _import_handler(monkeypatch, fake_table=fake)

    detail = {
        "eventTime": "2026-06-12T12:34:56Z",
        "eventName": "InitiateAuth",
        "errorCode": "NotAuthorizedException",
        "sourceIPAddress": "203.0.113.42",
        "userAgent": "ua",
        "requestParameters": {"username": "fake@example.com"},
        "eventID": "evt-throttled",
    }

    with caplog.at_level("WARNING"):
        resp = handler.handler(_event(detail), None)

    # Best-effort: handler still reports 200 to EventBridge.
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["written"] is False
    assert body["action_type"] == "AUTH_FAILED"

    # And it logged a warning mentioning the AUTH_FAILED action_type so an
    # operator searching logs by event type finds it.
    assert any("AUTH_FAILED" in rec.message for rec in caplog.records), (
        f"expected an AUTH_FAILED warning, got: {[r.message for r in caplog.records]}"
    )


# ──────────────────────── 4. Missing env var ───────────────────────────────
def test_module_refuses_to_import_without_env(monkeypatch):
    """Sanity check: handler.py treats a missing AUDIT_LOG_TABLE_NAME as a
    deploy-time misconfig. The module-level guard must raise."""
    monkeypatch.delenv("AUDIT_LOG_TABLE_NAME", raising=False)
    if "handler" in sys.modules:
        del sys.modules["handler"]
    with pytest.raises(RuntimeError, match="AUDIT_LOG_TABLE_NAME"):
        importlib.import_module("handler")
