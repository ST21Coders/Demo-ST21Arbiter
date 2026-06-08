#!/usr/bin/env python3
"""Post-deploy UI publisher for ST21-ARBITER.

Runs after `10-ui-hosting` is CREATE_COMPLETE/UPDATE_COMPLETE. It:
  1. Reads CloudFront domain + bucket + distribution id from stack outputs.
  2. Adds the CloudFront callback/logout URLs to the Cognito app client
     (in addition to the local-dev http://localhost:5173/ entries).
  3. Writes ui/.env.production from the live API/Chat/Cognito values.
  4. Runs `npm run build` so the production bundle picks up that env.
  5. Syncs ui/dist → s3://<UIBucketName>.
  6. Issues a CloudFront /* invalidation.

Idempotent — re-runs are safe (Cognito client patch is replace-only, the
S3 sync and invalidation are too).

Env vars (all optional, read from ENVIRONMENT/AWS_REGION/PROJECT or the
defaults below):
  ENVIRONMENT (default: dev)
  AWS_REGION  (default: us-east-1)
  PROJECT     (default: read from params/dev.json's ProjectName)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import boto3

# ──────────────────────────── config ────────────────────────────
HERE = Path(__file__).resolve().parent
REPO = HERE.parent
PARAMS_FILE = HERE / "params" / "dev.json"

ENV = os.environ.get("ENVIRONMENT", "dev")
REGION = os.environ.get("AWS_REGION", "us-east-1")


def _params() -> dict:
    """Parse params/<env>.json into a ParameterKey→ParameterValue dict."""
    data = json.loads(PARAMS_FILE.read_text())
    return {p["ParameterKey"]: p.get("ParameterValue", "")
            for p in data if "ParameterKey" in p}


PARAMS = _params()


def _project_from_params() -> str:
    """Mirror deploy.sh: ProjectName comes from params/<env>.json."""
    name = PARAMS.get("ProjectName")
    if not name:
        raise SystemExit(f"ProjectName not found in {PARAMS_FILE}")
    return name


PROJECT = os.environ.get("PROJECT") or _project_from_params()
PREFIX = f"{ENV}-{PROJECT}"

# Local dev URLs that must stay on the Cognito client so engineers can keep
# using `npm run dev`. Listed alongside the new CloudFront URL.
LOCAL_CALLBACK = "http://localhost:5173/callback"
LOCAL_LOGOUT = "http://localhost:5173/"

cfn = boto3.client("cloudformation", region_name=REGION)
cognito = boto3.client("cognito-idp", region_name=REGION)
cf = boto3.client("cloudfront", region_name=REGION)


# ──────────────────────────── helpers ───────────────────────────
def _export(name: str) -> str:
    """Return the value of a CFN export, or raise."""
    paginator = cfn.get_paginator("list_exports")
    for page in paginator.paginate():
        for e in page.get("Exports", []):
            if e["Name"] == name:
                return e["Value"]
    raise SystemExit(f"Missing CFN export: {name} (did 10-ui-hosting deploy yet?)")


def _run(cmd: list[str], *, cwd: Path | None = None) -> None:
    """Run a subprocess, stream output, fail loudly."""
    print(f"  $ {' '.join(cmd)}")
    subprocess.run(cmd, cwd=cwd, check=True)


# ──────────────────────────── steps ─────────────────────────────
def patch_cognito_client(cf_domain: str) -> None:
    """Add CloudFront callback/logout URLs to the Cognito SPA client.

    update-user-pool-client is replace-only — we re-send every field so the
    localhost + cloudfront entries co-exist.
    """
    user_pool_id = _export(f"{PREFIX}-UserPoolId")
    client_id = _export(f"{PREFIX}-UserPoolClientId")

    cf_callback = f"https://{cf_domain}/callback"
    cf_logout = f"https://{cf_domain}/"

    print(f"  → Cognito client {client_id}: adding {cf_callback}")
    cognito.update_user_pool_client(
        UserPoolId=user_pool_id,
        ClientId=client_id,
        CallbackURLs=[LOCAL_CALLBACK, cf_callback],
        LogoutURLs=[LOCAL_LOGOUT, cf_logout],
        SupportedIdentityProviders=["COGNITO"],
        AllowedOAuthFlows=["code"],
        AllowedOAuthScopes=["openid", "email", "profile"],
        AllowedOAuthFlowsUserPoolClient=True,
        ExplicitAuthFlows=[
            "ALLOW_REFRESH_TOKEN_AUTH",
            "ALLOW_USER_PASSWORD_AUTH",
            "ALLOW_USER_SRP_AUTH",
        ],
    )


def write_env_production(cf_domain: str) -> Path:
    """Write ui/.env.production from CFN exports + the new CF domain."""
    api_url = _export(f"{PREFIX}-ApiEndpoint")
    chat_url = _export(f"{PREFIX}-ChatFunctionUrl")
    user_pool_id = _export(f"{PREFIX}-UserPoolId")
    client_id = _export(f"{PREFIX}-UserPoolClientId")
    domain_prefix = "poc-st21arbiter"  # mirrors params/dev.json::CognitoDomainPrefix

    # LLM Control panel config — mirrored from params/dev.json so the page's
    # guardrail header + Agent Registry match what deploy_agents.py provisions.
    default_model = PARAMS.get("DefaultModelId", "us.amazon.nova-2-lite-v1:0")

    def _model(key: str) -> str:
        return PARAMS.get(key, "") or default_model

    guardrail_name = f"{PREFIX}-guardrail"
    guardrail_id = PARAMS.get("GuardrailId", "")
    guardrail_version = PARAMS.get("GuardrailVersion", "") or "DRAFT"
    guardrail_versions = PARAMS.get("GuardrailVersions", "") or guardrail_version

    env_path = REPO / "ui" / ".env.production"
    env_path.write_text(
        f"VITE_API_URL={api_url}\n"
        f"VITE_CHAT_URL={chat_url}\n"
        f"\n"
        f"VITE_COGNITO_REGION={REGION}\n"
        f"VITE_COGNITO_USER_POOL_ID={user_pool_id}\n"
        f"VITE_COGNITO_CLIENT_ID={client_id}\n"
        f"VITE_COGNITO_DOMAIN={domain_prefix}.auth.{REGION}.amazoncognito.com\n"
        f"VITE_COGNITO_REDIRECT_URI=https://{cf_domain}/callback\n"
        f"VITE_COGNITO_LOGOUT_URI=https://{cf_domain}/\n"
        f"\n"
        f"VITE_GUARDRAIL_NAME={guardrail_name}\n"
        f"VITE_GUARDRAIL_ID={guardrail_id}\n"
        f"VITE_GUARDRAIL_VERSION={guardrail_version}\n"
        f"VITE_GUARDRAIL_VERSIONS={guardrail_versions}\n"
        f"VITE_MASTER_MODEL_ID={_model('MasterModelId')}\n"
        f"VITE_SHAREPOINT_MODEL_ID={_model('SharepointModelId')}\n"
        f"VITE_AWSCONFIG_MODEL_ID={_model('AwsConfigModelId')}\n"
        f"VITE_ZSCALER_MODEL_ID={_model('ZscalerModelId')}\n"
    )
    print(f"  → wrote {env_path}")
    return env_path


def build_ui() -> Path:
    """Run `npm run build` inside ui/. Assumes node_modules already installed."""
    ui_dir = REPO / "ui"
    if not (ui_dir / "node_modules").exists():
        _run(["npm", "install"], cwd=ui_dir)
    _run(["npm", "run", "build"], cwd=ui_dir)
    dist = ui_dir / "dist"
    if not dist.exists():
        raise SystemExit(f"Build did not produce {dist}")
    return dist


def sync_to_s3(dist: Path, bucket: str) -> None:
    """aws s3 sync ui/dist s3://<bucket> --delete."""
    _run([
        "aws", "s3", "sync", str(dist), f"s3://{bucket}",
        "--delete",
        "--region", REGION,
    ])


def invalidate(distribution_id: str) -> None:
    """Force CloudFront to drop cached /index.html and all assets."""
    import time
    resp = cf.create_invalidation(
        DistributionId=distribution_id,
        InvalidationBatch={
            "Paths": {"Quantity": 1, "Items": ["/*"]},
            "CallerReference": f"deploy-{int(time.time())}",
        },
    )
    print(f"  → invalidation {resp['Invalidation']['Id']} created")


# ──────────────────────────── main ──────────────────────────────
def main() -> int:
    print(f"[post_deploy_ui] env={ENV} region={REGION} project={PROJECT}")
    cf_domain = _export(f"{PREFIX}-CloudFrontDomain")
    bucket = _export(f"{PREFIX}-UIBucketName")
    distribution_id = _export(f"{PREFIX}-UIDistributionId")

    print(f"  CloudFront domain : {cf_domain}")
    print(f"  S3 bucket         : {bucket}")
    print(f"  Distribution id   : {distribution_id}")

    print("[1/5] Patching Cognito callback/logout URLs…")
    patch_cognito_client(cf_domain)

    print("[2/5] Writing ui/.env.production…")
    write_env_production(cf_domain)

    print("[3/5] Building UI (npm run build)…")
    dist = build_ui()

    print("[4/5] Syncing dist → S3…")
    sync_to_s3(dist, bucket)

    print("[5/5] Invalidating CloudFront /*…")
    invalidate(distribution_id)

    print()
    print(f"  ✓ UI live at https://{cf_domain}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
