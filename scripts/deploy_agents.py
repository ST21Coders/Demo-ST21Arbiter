#!/usr/bin/env python3
"""Build, push, and deploy ARBITER agents to Bedrock AgentCore Runtime.

For each agent:
  1. docker build (arm64) the image from agents/<name>/Dockerfile
  2. docker push to its ECR repo
  3. create-or-update the AgentCore Runtime, attached to the project VPC's
     private subnets via the AgentCoreSG security group
  4. Wire the master orchestrator's environment with the specialist
     runtime ARNs once they're ready

Prerequisites:
  - Infra deployed (00-bootstrap, 01-network, 02-security, 04-storage, 09-agentcore)
  - Bedrock KB created via scripts/setup_bedrock_kb.py — its ID is passed in
    as KB_ID env var when invoking this script
  - Docker (or finch / podman aliased to docker) running locally
  - AWS CLI authenticated

Usage:
  KB_ID=ABCDEFGHIJ GUARDRAIL_ID=e0axl6y90il0 python scripts/deploy_agents.py
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError

# Project root = parent of the directory containing this script. All agent
# paths below are resolved relative to this, so the script can be invoked
# from anywhere (cwd-independent).
PROJECT_ROOT = Path(__file__).resolve().parent.parent

ENV = os.environ.get("ENVIRONMENT", "dev")
PROJECT = os.environ.get("PROJECT", "st21arbiter-poc")
REGION = os.environ.get("AWS_REGION", "us-east-1")
PREFIX = f"{ENV}-{PROJECT}"

# params/dev.json is the single source of truth for per-agent foundation
# models and the guardrail id/version (mirrored into the UI by
# Infra/post_deploy_ui.py). Env vars below still win when set, so existing
# command-line overrides keep working.
PARAMS_FILE = PROJECT_ROOT / "Infra" / "params" / f"{ENV}.json"


def _params() -> dict[str, str]:
    """Parse params/<env>.json into a ParameterKey→ParameterValue dict."""
    try:
        data = json.loads(PARAMS_FILE.read_text())
    except (OSError, ValueError):
        return {}
    return {p["ParameterKey"]: p.get("ParameterValue", "")
            for p in data if "ParameterKey" in p}


PARAMS = _params()
DEFAULT_MODEL_ID = PARAMS.get("DefaultModelId") or "us.amazon.nova-2-lite-v1:0"

KB_ID = os.environ.get("KB_ID", "")
# Guardrail: env var wins; otherwise fall back to params/dev.json.
GUARDRAIL_ID = os.environ.get("GUARDRAIL_ID", "") or PARAMS.get("GuardrailId", "")
GUARDRAIL_VERSION = (os.environ.get("GUARDRAIL_VERSION", "")
                     or PARAMS.get("GuardrailVersion", "") or "DRAFT")
# Only the master orchestrator uses memory in the current design.
# Set via env var when invoking the script; leave empty to disable memory.
MASTER_MEMORY_ID = os.environ.get("MASTER_MEMORY_ID", "")


def resolve_model_id(agent: dict[str, Any]) -> str:
    """Per-agent foundation model, by precedence:
    env override (e.g. MASTER_MODEL_ID) → params/dev.json model key →
    params/dev.json DefaultModelId → hardcoded Nova 2 Lite default.
    """
    return (os.environ.get(agent["env_model_var"], "")
            or PARAMS.get(agent["model_param"], "")
            or DEFAULT_MODEL_ID)

# Order matters: specialists must exist before the master can be wired to them.
AGENTS = [
    {
        "name": "sharepoint-specialist",
        "src": "agents/sharepoint_specialist",
        "repo_export": f"{PREFIX}-SharepointSpecialistRepoUri",
        "model_param": "SharepointModelId",
        "env_model_var": "SHAREPOINT_MODEL_ID",
        "env_overrides": {},
    },
    {
        "name": "awsconfig-specialist",
        "src": "agents/awsconfig_specialist",
        "repo_export": f"{PREFIX}-AwsConfigSpecialistRepoUri",
        "model_param": "AwsConfigModelId",
        "env_model_var": "AWSCONFIG_MODEL_ID",
        "env_overrides": {},
    },
    {
        "name": "zscaler-specialist",
        "src": "agents/zscaler_specialist",
        "repo_export": f"{PREFIX}-ZscalerSpecialistRepoUri",  # from 05-compute
        "model_param": "ZscalerModelId",
        "env_model_var": "ZSCALER_MODEL_ID",
        # ZSCALER_API_BASE + ZSCALER_SECRET_ID intentionally unset for the
        # demo — specialist falls back to KB-only mode. Set via env vars when
        # real Zscaler API creds are provisioned.
        "env_overrides": {},
    },
    {
        "name": "master-orchestrator",
        "src": "agents/master_orchestrator",
        "repo_export": f"{PREFIX}-MasterOrchestratorRepoUri",  # from 05-compute
        "model_param": "MasterModelId",
        "env_model_var": "MASTER_MODEL_ID",
        "env_overrides": {},  # filled in after specialists are deployed
    },
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("deploy_agents")

session = boto3.Session(region_name=REGION)
cfn = session.client("cloudformation")
ecr = session.client("ecr")
agentcore_control = session.client("bedrock-agentcore-control")
codebuild = session.client("codebuild")
iam = session.client("iam")
s3 = session.client("s3")
sts = session.client("sts")
ACCOUNT_ID = sts.get_caller_identity()["Account"]

CODEBUILD_PROJECT_NAME = f"{PREFIX}-agent-builder"
CODEBUILD_ROLE_NAME = f"{PREFIX}-codebuild-agent-role"
_TEMPLATE_BUCKET = ""  # populated by main() from CFN export before build_and_push runs


# ──────────────────────────── helpers ───────────────────────────
def cf_export(name: str) -> str:
    paginator = cfn.get_paginator("list_exports")
    for page in paginator.paginate():
        for exp in page["Exports"]:
            if exp["Name"] == name:
                return exp["Value"]
    raise SystemExit(f"CFN export not found: {name}")


# ──────────────────────────── CodeBuild (arm64 Graviton) ─────────


CODEBUILD_BUILDSPEC = """\
version: 0.2
phases:
  pre_build:
    commands:
      - aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin $REPO_URI
  build:
    commands:
      - cd $CODEBUILD_SRC_DIR
      - docker build --platform linux/arm64 -t $REPO_URI:$IMAGE_TAG .
  post_build:
    commands:
      - docker push $REPO_URI:$IMAGE_TAG
"""


def ensure_codebuild_role(repo_arns: list[str], source_bucket_arn: str) -> str:
    """Idempotently create the IAM role CodeBuild will assume."""
    try:
        existing = iam.get_role(RoleName=CODEBUILD_ROLE_NAME)
        return existing["Role"]["Arn"]
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise

    trust = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "codebuild.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }],
    }
    role = iam.create_role(
        RoleName=CODEBUILD_ROLE_NAME,
        AssumeRolePolicyDocument=json.dumps(trust),
        Description="ARBITER agent image builder",
    )
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["ecr:GetAuthorizationToken"],
                "Resource": "*",
            },
            {
                "Effect": "Allow",
                "Action": [
                    "ecr:BatchCheckLayerAvailability",
                    "ecr:CompleteLayerUpload",
                    "ecr:InitiateLayerUpload",
                    "ecr:PutImage",
                    "ecr:UploadLayerPart",
                    "ecr:BatchGetImage",
                    "ecr:GetDownloadUrlForLayer",
                ],
                "Resource": repo_arns,
            },
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:GetObjectVersion"],
                "Resource": [f"{source_bucket_arn}/*"],
            },
            {
                "Effect": "Allow",
                "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
                "Resource": f"arn:aws:logs:{REGION}:{ACCOUNT_ID}:log-group:/aws/codebuild/{CODEBUILD_PROJECT_NAME}*",
            },
        ],
    }
    iam.put_role_policy(
        RoleName=CODEBUILD_ROLE_NAME,
        PolicyName="AgentBuilderPolicy",
        PolicyDocument=json.dumps(policy),
    )
    log.info("Created CodeBuild role %s", CODEBUILD_ROLE_NAME)
    time.sleep(8)  # IAM eventual consistency
    return role["Role"]["Arn"]


def ensure_codebuild_project(role_arn: str) -> None:
    """Idempotently create the shared CodeBuild project for agent builds."""
    existing = codebuild.batch_get_projects(names=[CODEBUILD_PROJECT_NAME])
    if existing.get("projects"):
        log.info("CodeBuild project %s already exists", CODEBUILD_PROJECT_NAME)
        return

    codebuild.create_project(
        name=CODEBUILD_PROJECT_NAME,
        description="Builds ARBITER agent container images on Graviton",
        # NO_SOURCE here; per-build we override sourceTypeOverride=S3
        source={"type": "NO_SOURCE", "buildspec": CODEBUILD_BUILDSPEC},
        artifacts={"type": "NO_ARTIFACTS"},
        environment={
            "type": "ARM_CONTAINER",
            "image": "aws/codebuild/amazonlinux2-aarch64-standard:3.0",
            "computeType": "BUILD_GENERAL1_SMALL",
            "privilegedMode": True,  # required for docker build
        },
        serviceRole=role_arn,
        timeoutInMinutes=20,
    )
    log.info("Created CodeBuild project %s", CODEBUILD_PROJECT_NAME)


def build_and_push(agent: dict[str, Any], repo_uri: str) -> str:
    """Build and push an arm64 image for the agent via CodeBuild on Graviton.

    Zips agents/<name>/ → uploads to the bootstrap template bucket → starts
    a CodeBuild build that docker-builds and pushes to ECR. Returns the
    fully-qualified image URI.
    """
    import shutil
    import tempfile

    image_tag = f"{int(time.time())}"
    agent_src = PROJECT_ROOT / agent["src"]

    # Zip the agent source
    with tempfile.TemporaryDirectory() as tmpdir:
        zip_base = Path(tmpdir) / agent["name"]
        zip_path = shutil.make_archive(str(zip_base), "zip", root_dir=str(agent_src))
        source_key = f"agent-builds/{agent['name']}-{image_tag}.zip"
        s3.upload_file(zip_path, _TEMPLATE_BUCKET, source_key)

    log.info("Starting CodeBuild for %s (tag=%s)", agent["name"], image_tag)
    resp = codebuild.start_build(
        projectName=CODEBUILD_PROJECT_NAME,
        sourceTypeOverride="S3",
        sourceLocationOverride=f"{_TEMPLATE_BUCKET}/{source_key}",
        environmentVariablesOverride=[
            {"name": "REPO_URI", "value": repo_uri, "type": "PLAINTEXT"},
            {"name": "IMAGE_TAG", "value": image_tag, "type": "PLAINTEXT"},
            {"name": "AWS_REGION", "value": REGION, "type": "PLAINTEXT"},
        ],
    )
    build_id = resp["build"]["id"]
    log.info("  build id: %s", build_id)

    # Poll
    deadline = time.time() + 1500  # 25 min
    while time.time() < deadline:
        info = codebuild.batch_get_builds(ids=[build_id])["builds"][0]
        status = info.get("buildStatus")
        phase = info.get("currentPhase", "?")
        log.info("  %s: %s (phase=%s)", agent["name"], status, phase)
        if status == "SUCCEEDED":
            return f"{repo_uri}:{image_tag}"
        if status in ("FAILED", "FAULT", "TIMED_OUT", "STOPPED"):
            raise SystemExit(
                f"CodeBuild build for {agent['name']} ended with status={status}. "
                f"Inspect logs: aws logs tail /aws/codebuild/{CODEBUILD_PROJECT_NAME} --since 30m"
            )
        time.sleep(15)
    raise SystemExit(f"CodeBuild timeout for {agent['name']}")


# ──────────────────────────── AgentCore Runtime ─────────────────
def find_runtime(runtime_name: str) -> dict[str, Any] | None:
    try:
        paginator = agentcore_control.get_paginator("list_agent_runtimes")
        for page in paginator.paginate():
            for r in page.get("agentRuntimes", []):
                if r.get("agentRuntimeName") == runtime_name:
                    return r
    except ClientError:
        pass
    return None


def deploy_runtime(
    agent: dict[str, Any],
    image_uri: str,
    role_arn: str,
    subnet_ids: list[str],
    sg_id: str,
    env_vars: dict[str, str],
) -> str:
    runtime_name = f"{PREFIX}-{agent['name']}".replace("-", "_")[:63]
    existing = find_runtime(runtime_name)

    container_config = {
        "containerUri": image_uri,
    }
    network_config = {
        "networkMode": "VPC",
        "networkModeConfig": {
            "subnets": subnet_ids,
            "securityGroups": [sg_id],
        },
    }

    if existing:
        log.info("Updating existing runtime %s", runtime_name)
        resp = agentcore_control.update_agent_runtime(
            agentRuntimeId=existing["agentRuntimeId"],
            agentRuntimeArtifact={"containerConfiguration": container_config},
            roleArn=role_arn,
            networkConfiguration=network_config,
            environmentVariables=env_vars,
        )
        runtime_arn = existing["agentRuntimeArn"]
    else:
        log.info("Creating runtime %s", runtime_name)
        resp = agentcore_control.create_agent_runtime(
            agentRuntimeName=runtime_name,
            description=f"ARBITER {agent['name']}",
            agentRuntimeArtifact={"containerConfiguration": container_config},
            roleArn=role_arn,
            networkConfiguration=network_config,
            environmentVariables=env_vars,
        )
        runtime_arn = resp["agentRuntimeArn"]

    # Wait for READY
    runtime_id = runtime_arn.split("/")[-1]
    log.info("Waiting for runtime %s to be READY...", runtime_name)
    deadline = time.time() + 600
    while time.time() < deadline:
        info = agentcore_control.get_agent_runtime(agentRuntimeId=runtime_id)
        status = info.get("status")
        log.info("  %s status: %s", runtime_name, status)
        if status == "READY":
            return runtime_arn
        if status in ("CREATE_FAILED", "UPDATE_FAILED"):
            raise SystemExit(f"Runtime {runtime_name} failed: {info.get('failureReason')}")
        time.sleep(10)
    raise SystemExit(f"Timeout waiting for {runtime_name}")


# ──────────────────────────── api_handler env-var patch ─────────
def _patch_api_handler_lambda(master_runtime_arn: str) -> None:
    """Set MASTER_AGENT_RUNTIME_ARN (and MEMORY_ID if available) on the api_handler.

    Preserves any other env vars already set. Triggers a cold start on the
    next invocation so the new values are picked up.
    """
    lambda_client = session.client("lambda")
    func_name = f"{PREFIX}-api-handler"
    try:
        cur = lambda_client.get_function_configuration(FunctionName=func_name)
    except ClientError as e:
        log.warning("api_handler Lambda %s not found, skipping env patch: %s", func_name, e)
        return

    env = (cur.get("Environment") or {}).get("Variables") or {}
    desired = {"MASTER_AGENT_RUNTIME_ARN": master_runtime_arn}
    if MASTER_MEMORY_ID:
        desired["MEMORY_ID"] = MASTER_MEMORY_ID
    if all(env.get(k) == v for k, v in desired.items()):
        log.info("api_handler Lambda env already up to date")
        return

    env.update(desired)
    lambda_client.update_function_configuration(
        FunctionName=func_name,
        Environment={"Variables": env},
    )
    log.info("✓ Patched %s env: %s", func_name, list(desired.keys()))


# ──────────────────────────── main ──────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agents", nargs="*", help="Only deploy specific agents by name")
    parser.add_argument("--skip-build", action="store_true", help="Skip docker build/push (reuse :latest)")
    args = parser.parse_args()

    if not KB_ID:
        log.warning("KB_ID env var is empty — agents will deploy but KB-backed tools will be no-ops")

    log.info("Resolving CFN exports...")
    role_arn = cf_export(f"{PREFIX}-AgentCoreRuntimeRoleArn")
    sg_id = cf_export(f"{PREFIX}-AgentCoreSGId")
    # AgentCore Runtime requires its subnet to be in a supported physical AZ
    # (use1-az1/az2/az4 in us-east-1). PrivateSubnet2 is provisioned in 01-network.yaml
    # specifically for this — PrivateSubnet1 is kept on its original AZ for the
    # OSS VPC endpoint + Lambda VpcConfigs that don't have the same constraint.
    subnet_ids = [cf_export(f"{PREFIX}-PrivateSubnet2Id")]
    template_bucket = cf_export(f"{PREFIX}-TemplateBucketName")
    log.info("  role:    %s", role_arn)
    log.info("  sg:      %s", sg_id)
    log.info("  subnets: %s", subnet_ids)
    log.info("  build src bucket: %s", template_bucket)

    # Provision the shared CodeBuild project + service role (idempotent).
    # Scope ECR permissions to the four agent repos by ARN.
    global _TEMPLATE_BUCKET
    _TEMPLATE_BUCKET = template_bucket
    if not args.skip_build:
        repo_arns = [
            f"arn:aws:ecr:{REGION}:{ACCOUNT_ID}:repository/{cf_export(a['repo_export']).split('/', 1)[1]}"
            for a in AGENTS
        ]
        source_bucket_arn = f"arn:aws:s3:::{template_bucket}"
        cb_role_arn = ensure_codebuild_role(repo_arns, source_bucket_arn)
        ensure_codebuild_project(cb_role_arn)

    runtime_arns: dict[str, str] = {}
    for agent in AGENTS:
        if args.agents and agent["name"] not in args.agents:
            continue

        repo_uri = cf_export(agent["repo_export"])

        if args.skip_build:
            image_uri = f"{repo_uri}:latest"
        else:
            image_uri = build_and_push(agent, repo_uri)

        env_vars: dict[str, str] = {
            "AWS_REGION": REGION,
            "KB_ID": KB_ID,
            # Per-agent foundation model from params/dev.json (env override wins).
            "MODEL_ID": resolve_model_id(agent),
        }
        if GUARDRAIL_ID:
            env_vars["GUARDRAIL_ID"] = GUARDRAIL_ID
            env_vars["GUARDRAIL_VERSION"] = GUARDRAIL_VERSION

        # Specialist-specific env
        env_vars.update(agent["env_overrides"])

        # The master orchestrator needs the specialist ARNs. If a specialist
        # wasn't deployed in this run (e.g. --agents master-orchestrator), look
        # up its existing runtime ARN so we don't clobber master's env with "".
        if agent["name"] == "master-orchestrator":
            for spec_name, env_key in [
                ("sharepoint-specialist", "SHAREPOINT_RUNTIME_ARN"),
                ("awsconfig-specialist", "AWSCONFIG_RUNTIME_ARN"),
                ("zscaler-specialist", "ZSCALER_RUNTIME_ARN"),
            ]:
                arn = runtime_arns.get(spec_name)
                if not arn:
                    spec_runtime_name = f"{PREFIX}-{spec_name}".replace("-", "_")[:63]
                    existing = find_runtime(spec_runtime_name)
                    if existing:
                        arn = existing.get("agentRuntimeArn", "")
                env_vars[env_key] = arn or ""
            # Long-term memory (master-only). Empty MEMORY_ID disables it.
            if MASTER_MEMORY_ID:
                env_vars["MEMORY_ID"] = MASTER_MEMORY_ID
            # Conversation index lives in the same sessions table the api_handler reads.
            # The master writes a new row on the first turn of a session, then
            # bumps last_message_at + message_count after each turn.
            env_vars["SESSIONS_TABLE"] = f"{PREFIX}-sessions"

        runtime_arn = deploy_runtime(agent, image_uri, role_arn, subnet_ids, sg_id, env_vars)
        runtime_arns[agent["name"]] = runtime_arn
        log.info("✓ %s → %s", agent["name"], runtime_arn)

    # Wire the api_handler Lambda to the master runtime by patching its
    # MASTER_AGENT_RUNTIME_ARN env var. This is what bridges API Gateway → AgentCore.
    if "master-orchestrator" in runtime_arns:
        _patch_api_handler_lambda(runtime_arns["master-orchestrator"])

    print()
    print("════════════════════════════════════════════════════════")
    print("  ARBITER agents deployed")
    print("════════════════════════════════════════════════════════")
    print(json.dumps(runtime_arns, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log.exception("Deploy failed: %s", e)
        sys.exit(1)
