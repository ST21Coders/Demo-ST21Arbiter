"""Shared fixtures for ARBITER backend tests.

The api_handler module reads env vars and creates boto3 resources at import
time, so we must (a) set env vars and (b) start moto's mock context *before*
importing it. Each unit test gets a fresh DynamoDB mock + a fresh import of
the handler module so state never bleeds between tests.
"""
from __future__ import annotations

import base64
import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws

# Make the api_handler package importable.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "Infra" / "functions" / "api_handler"))


# Constants the unit tests share. Names match what's in conftest's table creator.
CONFLICTS_TABLE = "test-arbiter-conflicts"
CHANGE_REQUESTS_TABLE = "test-arbiter-change-requests"
AUDIT_TABLE = "test-arbiter-audit-log"
SESSIONS_TABLE = "test-arbiter-sessions"
FAKE_USER_SUB = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def aws_env(monkeypatch):
    """Set the env vars api_handler expects, plus dummy AWS creds for moto."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("CONFLICTS_TABLE", CONFLICTS_TABLE)
    monkeypatch.setenv("CHANGE_REQUESTS_TABLE", CHANGE_REQUESTS_TABLE)
    monkeypatch.setenv("AUDIT_TABLE", AUDIT_TABLE)
    monkeypatch.setenv("SESSIONS_TABLE", SESSIONS_TABLE)
    monkeypatch.setenv("MASTER_AGENT_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-east-1:000000000000:runtime/test-master")
    monkeypatch.setenv("MEMORY_ID", "test-memory-id")


@pytest.fixture
def ddb_tables(aws_env):
    """Spin up moto, create the 4 DDB tables, and yield the resource."""
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName=CONFLICTS_TABLE,
            KeySchema=[{"AttributeName": "conflict_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "conflict_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        ddb.create_table(
            TableName=CHANGE_REQUESTS_TABLE,
            KeySchema=[{"AttributeName": "cr_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "cr_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        ddb.create_table(
            TableName=AUDIT_TABLE,
            KeySchema=[{"AttributeName": "log_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "log_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        ddb.create_table(
            TableName=SESSIONS_TABLE,
            KeySchema=[{"AttributeName": "session_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "session_id", "AttributeType": "S"},
                {"AttributeName": "user_id", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[{
                "IndexName": "user-sessions-index",
                "KeySchema": [{"AttributeName": "user_id", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            }],
            BillingMode="PAY_PER_REQUEST",
        )
        yield ddb


@pytest.fixture
def api_handler(ddb_tables):
    """Re-import api_handler under the moto + env context so its module-level
    boto3 clients hit the mock backend, not real AWS."""
    if "api_handler" in sys.modules:
        del sys.modules["api_handler"]
    return importlib.import_module("api_handler")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def make_jwt(sub: str = FAKE_USER_SUB, groups: list[str] | None = None) -> str:
    """Build a Cognito-shaped JWT *without* a real signature.

    api_handler._caller_user_id decodes the payload only — it does NOT verify
    the signature (production path relies on API Gateway's authorizer for
    that). So a hand-crafted token is enough to exercise the code path.
    """
    header = {"alg": "RS256", "typ": "JWT", "kid": "test"}
    payload = {"sub": sub, "cognito:username": sub, "token_use": "id"}
    if groups:
        payload["cognito:groups"] = groups
    return ".".join([
        _b64url(json.dumps(header).encode()),
        _b64url(json.dumps(payload).encode()),
        _b64url(b"fake-signature"),
    ])


def lambda_event(
    method: str,
    path: str,
    body: Any = None,
    query: dict[str, str] | None = None,
    auth_sub: str | None = FAKE_USER_SUB,
    use_authorizer_claims: bool = False,
) -> dict[str, Any]:
    """Build a minimal Lambda invocation event. Defaults to the Function URL
    shape with an Authorization header; set use_authorizer_claims=True to
    simulate API Gateway with a Cognito JWT authorizer instead."""
    event: dict[str, Any] = {
        "path": path,
        "httpMethod": method,
        "queryStringParameters": query,
        "headers": {},
    }
    if body is not None:
        event["body"] = json.dumps(body) if not isinstance(body, str) else body
    if auth_sub:
        if use_authorizer_claims:
            event["requestContext"] = {
                "authorizer": {"claims": {"sub": auth_sub, "cognito:username": auth_sub}}
            }
        else:
            event["headers"]["Authorization"] = f"Bearer {make_jwt(auth_sub)}"
    return event
