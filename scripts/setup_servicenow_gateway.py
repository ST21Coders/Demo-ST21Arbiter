#!/usr/bin/env python3
"""Provision the AgentCore Gateway that fronts the ServiceNow Table API.

Creates (idempotently, create-or-update):
  1. An AgentCore Identity API-key credential provider holding the ServiceNow
     Inbound REST API key (read from the existing Secrets Manager secret's
     "api_key" field) — the gateway injects it as the x-sn-apikey header at
     the edge, so agent code never touches the ServiceNow credential.
  2. The gateway itself (MCP protocol, CUSTOM_JWT inbound authorizer bound to
     the Cognito user pool + gateway M2M app client from 03-identity).
  3. An OpenAPI target exposing a READ-ONLY subset of the Table API
     (scripts/gateway/servicenow_table_openapi.json with the instance URL
     injected). Writes deliberately stay on the specialist's direct REST path.

Prerequisites:
  - 03-identity + 09-agentcore stacks deployed with the gateway additions
    (GatewayM2MClientId + ServicenowGatewayRoleArn exports).
  - Secret ${ENV}/${PROJECT}/servicenow contains "instance_url" AND "api_key"
    (a ServiceNow Inbound REST API key). Basic-auth-only secrets are NOT
    enough for the gateway path — the script fails loudly with instructions;
    the specialist keeps working via direct REST until then.

Usage (from scripts/, venv active):
  AWS_REGION=us-east-1 ENVIRONMENT=dev PROJECT=st21arbiter-poc \
      python3 setup_servicenow_gateway.py

Afterwards re-run deploy_agents.py (at least --agents servicenow-specialist):
it discovers the READY gateway and wires SERVICENOW_GW* env onto the runtime.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

ENV = os.environ.get("ENVIRONMENT", "dev")
PROJECT = os.environ.get("PROJECT", "st21arbiter-poc")
REGION = os.environ.get("AWS_REGION", "us-east-1")
PREFIX = f"{ENV}-{PROJECT}"

SECRET_ID = f"{ENV}/{PROJECT}/servicenow"
PROVIDER_NAME = f"{PREFIX}-servicenow-apikey"
GATEWAY_NAME = f"{PREFIX}-servicenow-gw"
TARGET_NAME = "servicenow-table"
SPEC_PATH = Path(__file__).resolve().parent / "gateway" / "servicenow_table_openapi.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("setup_servicenow_gateway")

session = boto3.Session(region_name=REGION)
cfn = session.client("cloudformation")
secretsmanager = session.client("secretsmanager")
control = session.client("bedrock-agentcore-control")


def cf_export(name: str) -> str:
    paginator = cfn.get_paginator("list_exports")
    for page in paginator.paginate():
        for exp in page["Exports"]:
            if exp["Name"] == name:
                return exp["Value"]
    raise SystemExit(
        f"CFN export not found: {name} — deploy the updated 03-identity/09-agentcore "
        f"stacks first (Infra/deploy.sh)."
    )


def load_servicenow_secret() -> dict:
    try:
        raw = secretsmanager.get_secret_value(SecretId=SECRET_ID)["SecretString"]
        secret = json.loads(raw)
    except (ClientError, ValueError) as e:
        raise SystemExit(f"Cannot read secret {SECRET_ID}: {e}")

    instance_url = (secret.get("instance_url") or "").rstrip("/")
    api_key = secret.get("api_key") or ""
    if not instance_url:
        raise SystemExit(f"Secret {SECRET_ID} has no instance_url — add it first.")
    if not api_key:
        raise SystemExit(
            f"Secret {SECRET_ID} has no api_key. The gateway's outbound auth needs a "
            f"ServiceNow Inbound REST API key (System Web Services → API Access Policies "
            f"→ REST API Key). Create one in the instance, then:\n"
            f"  aws secretsmanager get-secret-value --secret-id {SECRET_ID} "
            f"--query SecretString --output text\n"
            f"  # merge {{\"api_key\": \"<key>\"}} into the JSON and put-secret-value.\n"
            f"Until then the servicenow_specialist keeps working over direct REST."
        )
    return {"instance_url": instance_url, "api_key": api_key}


def ensure_credential_provider(api_key: str) -> str:
    """Create-or-update the API-key credential provider; return its ARN."""
    existing = None
    token = None
    while True:
        kwargs = {"nextToken": token} if token else {}
        page = control.list_api_key_credential_providers(**kwargs)
        for item in page.get("credentialProviders", []):
            if item.get("name") == PROVIDER_NAME:
                existing = item
        token = page.get("nextToken")
        if not token:
            break

    if existing:
        control.update_api_key_credential_provider(name=PROVIDER_NAME, apiKey=api_key)
        arn = existing["credentialProviderArn"]
        log.info("✓ credential provider updated: %s", PROVIDER_NAME)
    else:
        resp = control.create_api_key_credential_provider(name=PROVIDER_NAME, apiKey=api_key)
        arn = resp["credentialProviderArn"]
        log.info("✓ credential provider created: %s", PROVIDER_NAME)
    return arn


def _wait(describe, ready_states=("READY",), failed_states=("FAILED",), timeout=300) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        detail = describe()
        status = detail.get("status")
        if status in ready_states:
            return detail
        if status in failed_states:
            raise SystemExit(f"Provisioning failed: status={status} detail="
                             f"{detail.get('statusReasons') or detail.get('failureReasons')}")
        time.sleep(5)
    raise SystemExit("Timed out waiting for READY")


def ensure_gateway(role_arn: str, user_pool_id: str, m2m_client_id: str) -> dict:
    discovery_url = (f"https://cognito-idp.{REGION}.amazonaws.com/{user_pool_id}"
                     f"/.well-known/openid-configuration")
    authorizer = {
        "customJWTAuthorizer": {
            "discoveryUrl": discovery_url,
            "allowedClients": [m2m_client_id],
        }
    }

    gateway = None
    token = None
    while True:
        kwargs = {"nextToken": token} if token else {}
        page = control.list_gateways(**kwargs)
        for item in page.get("items", []):
            if item.get("name") == GATEWAY_NAME:
                gateway = item
        token = page.get("nextToken")
        if not token:
            break

    if gateway:
        gateway_id = gateway["gatewayId"]
        control.update_gateway(
            gatewayIdentifier=gateway_id,
            name=GATEWAY_NAME,
            roleArn=role_arn,
            protocolType="MCP",
            authorizerType="CUSTOM_JWT",
            authorizerConfiguration=authorizer,
        )
        log.info("✓ gateway updated: %s", GATEWAY_NAME)
    else:
        resp = control.create_gateway(
            name=GATEWAY_NAME,
            description="Read-only ServiceNow Table API gateway for the servicenow_specialist",
            roleArn=role_arn,
            protocolType="MCP",
            authorizerType="CUSTOM_JWT",
            authorizerConfiguration=authorizer,
        )
        gateway_id = resp["gatewayId"]
        log.info("✓ gateway created: %s (%s)", GATEWAY_NAME, gateway_id)

    detail = _wait(lambda: control.get_gateway(gatewayIdentifier=gateway_id))
    log.info("  gateway READY: %s", detail.get("gatewayUrl"))
    return detail


def ensure_target(gateway_id: str, instance_url: str, provider_arn: str) -> None:
    spec = json.loads(SPEC_PATH.read_text())
    spec["servers"] = [{"url": instance_url}]
    target_config = {"mcp": {"openApiSchema": {"inlinePayload": json.dumps(spec)}}}
    cred_config = [{
        "credentialProviderType": "API_KEY",
        "credentialProvider": {
            "apiKeyCredentialProvider": {
                "providerArn": provider_arn,
                "credentialParameterName": "x-sn-apikey",
                "credentialLocation": "HEADER",
            }
        },
    }]

    existing = None
    token = None
    while True:
        kwargs = {"gatewayIdentifier": gateway_id}
        if token:
            kwargs["nextToken"] = token
        page = control.list_gateway_targets(**kwargs)
        for item in page.get("items", []):
            if item.get("name") == TARGET_NAME:
                existing = item
        token = page.get("nextToken")
        if not token:
            break

    if existing:
        target_id = existing["targetId"]
        control.update_gateway_target(
            gatewayIdentifier=gateway_id,
            targetId=target_id,
            name=TARGET_NAME,
            targetConfiguration=target_config,
            credentialProviderConfigurations=cred_config,
        )
        log.info("✓ target updated: %s", TARGET_NAME)
    else:
        resp = control.create_gateway_target(
            gatewayIdentifier=gateway_id,
            name=TARGET_NAME,
            description="ServiceNow Table API (read-only subset)",
            targetConfiguration=target_config,
            credentialProviderConfigurations=cred_config,
        )
        target_id = resp["targetId"]
        log.info("✓ target created: %s (%s)", TARGET_NAME, target_id)

    _wait(lambda: control.get_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id))
    log.info("  target READY (tools: %s___getTableRecords, %s___getRecordById)",
             TARGET_NAME, TARGET_NAME)


def main() -> None:
    secret = load_servicenow_secret()
    role_arn = cf_export(f"{PREFIX}-ServicenowGatewayRoleArn")
    user_pool_id = cf_export(f"{PREFIX}-UserPoolId")
    m2m_client_id = cf_export(f"{PREFIX}-GatewayM2MClientId")

    provider_arn = ensure_credential_provider(secret["api_key"])
    gateway = ensure_gateway(role_arn, user_pool_id, m2m_client_id)
    ensure_target(gateway["gatewayId"], secret["instance_url"], provider_arn)

    print()
    print("════════════════════════════════════════════════════════")
    print("  ServiceNow gateway ready")
    print("════════════════════════════════════════════════════════")
    print(f"  gatewayUrl: {gateway.get('gatewayUrl')}")
    print()
    print("Next: re-run deploy_agents.py (it auto-wires SERVICENOW_GW* onto the")
    print("servicenow-specialist runtime):")
    print("  python3 deploy_agents.py --agents servicenow-specialist")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        log.exception("Gateway setup failed: %s", e)
        sys.exit(1)
