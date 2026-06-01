"""Live smoke tests against the deployed ARBITER stack.

Skipped unless TEST_MODE=live. Hits real AWS via the dev stack:
  - GET /health on the API GW (unauthenticated)
  - Cognito InitiateAuth to mint a real IdToken
  - GET /findings on the Function URL (authenticated)
  - POST /chat with a benign prompt (structural assertion only — LLM output
    is non-deterministic so we never equality-check the reply text)

Required env vars when TEST_MODE=live:
  LIVE_API_BASE_URL   — API Gateway invoke URL (https://...execute-api...)
  LIVE_CHAT_URL       — Lambda Function URL for /chat (https://...lambda-url...)
  COGNITO_REGION
  COGNITO_CLIENT_ID
  TEST_USER_USERNAME
  TEST_USER_PASSWORD
"""
from __future__ import annotations

import os

import boto3
import pytest
import requests

pytestmark = pytest.mark.skipif(
    os.environ.get("TEST_MODE") != "live",
    reason="Live smoke tests skipped outside TEST_MODE=live",
)


def _require(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        pytest.skip(f"Missing required env var: {name}")
    return v


@pytest.fixture(scope="module")
def id_token() -> str:
    client = boto3.client("cognito-idp", region_name=_require("COGNITO_REGION"))
    resp = client.initiate_auth(
        AuthFlow="USER_PASSWORD_AUTH",
        ClientId=_require("COGNITO_CLIENT_ID"),
        AuthParameters={
            "USERNAME": _require("TEST_USER_USERNAME"),
            "PASSWORD": _require("TEST_USER_PASSWORD"),
        },
    )
    result = resp.get("AuthenticationResult") or {}
    token = result.get("IdToken")
    if not token:
        pytest.fail(
            f"Cognito InitiateAuth returned no IdToken "
            f"(challenge={resp.get('ChallengeName')}). "
            "If NEW_PASSWORD_REQUIRED, reset the test user's password."
        )
    return token


def test_live_health_endpoint_returns_200():
    base = _require("LIVE_API_BASE_URL").rstrip("/")
    r = requests.get(f"{base}/health", timeout=15)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("status") == "healthy"


def test_live_findings_round_trip(id_token: str):
    base = _require("LIVE_API_BASE_URL").rstrip("/")
    r = requests.get(
        f"{base}/findings",
        headers={"Authorization": f"Bearer {id_token}"},
        timeout=30,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "findings" in body
    assert isinstance(body["findings"], list)


def test_live_chat_round_trip_returns_structured_reply(id_token: str):
    """Asserts shape only — LLM output varies turn-to-turn."""
    chat_url = _require("LIVE_CHAT_URL").rstrip("/")
    r = requests.post(
        chat_url + "/chat",
        headers={"Authorization": f"Bearer {id_token}", "Content-Type": "application/json"},
        json={"prompt": "ping", "session_id": "adhoc"},
        timeout=90,  # master orchestrator can take 30-60s on a cold runtime
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "reply" in body
    assert isinstance(body["reply"], str)
    assert len(body["reply"]) > 0
