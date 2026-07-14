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
# Env var wins; otherwise fall back to params/dev.json. Empty disables memory.
MASTER_MEMORY_ID = os.environ.get("MASTER_MEMORY_ID", "") or PARAMS.get("MasterMemoryId", "")


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
        # Dedicated Tier-0 role: broad READ-ONLY resource inventory + posture,
        # but NO secret access (no secretsmanager:GetSecretValue / Secrets CMK /
        # S3 write). Falls back to the shared role if the export is missing
        # (09-agentcore not yet redeployed) — but then it inherits the shared
        # role's secret perms, so redeploy 09-agentcore for the no-leak guarantee.
        "role_export": f"{PREFIX}-AwsConfigAgentRuntimeRoleArn",  # from 09-agentcore
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
        "name": "paloalto-specialist",
        "src": "agents/paloalto_specialist",
        "repo_export": f"{PREFIX}-PaloaltoSpecialistRepoUri",  # from 09-agentcore
        "model_param": "PaloaltoModelId",
        "env_model_var": "PALOALTO_MODEL_ID",
        # PALOALTO_API_BASE + PALOALTO_SECRET_ID intentionally unset for the
        # demo — specialist falls back to KB-only mode. Set via env vars when
        # real PAN-OS / Panorama API creds are provisioned.
        "env_overrides": {},
    },
    {
        "name": "structured-specialist",
        "src": "agents/structured_specialist",
        "repo_export": f"{PREFIX}-StructuredSpecialistRepoUri",  # from 09-agentcore
        "model_param": "StructuredModelId",
        "env_model_var": "STRUCTURED_MODEL_ID",
        # Deterministic names (match 04-storage): Glue db underscores the dashed
        # prefix; workgroup keeps dashes; Athena results go under the processed bucket.
        "env_overrides": {
            "GLUE_DATABASE": f"{ENV}_{PROJECT}_structured".replace("-", "_"),
            "ATHENA_WORKGROUP": f"{PREFIX}-wg",
            "ATHENA_OUTPUT": f"s3://{PREFIX}-processed/athena-results/",
        },
    },
    {
        "name": "sales-specialist",
        "src": "agents/sales_specialist",
        "repo_export": f"{PREFIX}-SalesSpecialistRepoUri",  # from 09-agentcore
        "model_param": "SalesModelId",
        "env_model_var": "SALES_MODEL_ID",
        # Ships the reusable arbiter_rag RAG library into the image (single source of
        # truth stays under rag_src/). The Dockerfile COPYs the injected arbiter_rag/ dir.
        "extra_pkgs": [("rag_src/arbiter_rag", "arbiter_rag")],
        # The agent builds an arbiter_rag Settings from these env vars (it never reads
        # settings.toml). Semantic path → S3 Vectors sales-facts index. SQL path → the
        # existing structured Glue DB + read-only workgroup (already IAM-granted); the
        # Hawaii sales table is `hawaii_sales`.
        "env_overrides": {
            "SALES_VECTOR_BUCKET": f"{PREFIX}-sales-vectors",
            "SALES_VECTOR_INDEX": "sales-facts",
            "EMBEDDING_MODEL_ID": "amazon.titan-embed-text-v2:0",
            "EMBEDDING_DIM": "1024",
            "RERANK_ENABLED": "false",
            "GLUE_DATABASE": f"{ENV}_{PROJECT}_structured".replace("-", "_"),
            "GLUE_TABLE": "hawaii_sales",
            "ATHENA_WORKGROUP": f"{PREFIX}-wg",
            "ATHENA_OUTPUT": f"s3://{PREFIX}-processed/athena-results/",
        },
    },
    {
        "name": "hr-specialist",
        "src": "agents/hr_specialist",
        "repo_export": f"{PREFIX}-HrSpecialistRepoUri",  # from 09-agentcore
        "model_param": "HrModelId",
        "env_model_var": "HR_MODEL_ID",
        # Ships the reusable arbiter_rag RAG library into the image (single source of
        # truth stays under rag_src/). The Dockerfile COPYs the injected arbiter_rag/ dir.
        "extra_pkgs": [("rag_src/arbiter_rag", "arbiter_rag")],
        # The agent builds an arbiter_rag Settings from these env vars (never settings.toml).
        # Semantic-only path → the S3 Vectors hr-policies index (its own vector bucket).
        "env_overrides": {
            "HR_VECTOR_BUCKET": f"{PREFIX}-hr-vectors",
            "HR_VECTOR_INDEX": "hr-policies",
            "EMBEDDING_MODEL_ID": "amazon.titan-embed-text-v2:0",
            "EMBEDDING_DIM": "1024",
            "RETRIEVAL_TOP_K": "4",
            "RERANK_ENABLED": "false",
        },
    },
    {
        "name": "jira-specialist",
        "src": "agents/jira_specialist",
        "repo_export": f"{PREFIX}-JiraSpecialistRepoUri",  # from 09-agentcore
        "model_param": "JiraModelId",
        "env_model_var": "JIRA_MODEL_ID",
        # Tier-0: the JIRA agent calls an EXTERNAL SaaS, so it runs under its own
        # least-privilege role (not the shared AgentCoreRuntimeRole). Falls back
        # to the shared role if this export is missing.
        "role_export": f"{PREFIX}-JiraAgentRuntimeRoleArn",  # from 09-agentcore
        # Jira URL/email/API token live in this Secrets Manager secret (JSON
        # {url,email,api_token}). Create it before deploy — see DEPLOYMENT.md.
        # Empty secret = agent runs in "(JIRA not configured)" mode.
        # Tier-0 scoping: minimal tool allowlist (Jira read + create + L1
        # resolution, plus Confluence search/read/create/update). The Confluence
        # tools only function when the secret carries "confluence_url" (the .../wiki
        # base) — see agents/jira_specialist/agent.py::_build_mcp_client.
        # JIRA_PROJECTS_FILTER intentionally NOT set — it silently scoped reads
        # out; least-privilege here comes from the service account's permissions
        # + the tool allowlist instead.
        "env_overrides": {
            "JIRA_SECRET_ID": f"{ENV}/{PROJECT}/jira",
            # Default Confluence space KEY (mcp-atlassian needs the key, not the
            # display name "Arbiter-poc-confluence"). Used when the user omits the
            # space or names it by display name.
            "CONFLUENCE_DEFAULT_SPACE_KEY": "Arbiterpoc",
            # Jira read + create + L1-resolution writes (transition/comment;
            # get_transitions resolves a transition by name → id defensively),
            # plus Confluence search/read/create/update.
            "ENABLED_TOOLS": (
                "jira_search,jira_get_issue,jira_get_all_projects,jira_create_issue,"
                "jira_get_transitions,jira_transition_issue,jira_add_comment,"
                "confluence_search,confluence_get_page,confluence_create_page,confluence_update_page"
            ),
        },
    },
    {
        "name": "servicenow-specialist",
        "src": "agents/servicenow_specialist",
        "repo_export": f"{PREFIX}-ServicenowSpecialistRepoUri",  # from 09-agentcore
        "model_param": "ServicenowModelId",
        "env_model_var": "SERVICENOW_MODEL_ID",
        # Tier-0: like JIRA, the ServiceNow agent calls an EXTERNAL SaaS, so it
        # runs under its own least-privilege role (not the shared role). Falls
        # back to the shared role if the export is missing (09-agentcore not yet
        # redeployed).
        "role_export": f"{PREFIX}-ServicenowAgentRuntimeRoleArn",  # from 09-agentcore
        # Instance URL + creds live in this Secrets Manager secret (JSON, either
        # {instance_url,username,password} or {instance_url,client_id,client_secret}).
        # Create it before deploy — see DEPLOYMENT.md. Empty/unreadable secret =
        # agent runs in "(ServiceNow not configured)" mode (mock CHG ids).
        # SERVICENOW_API_BASE is read from the secret's instance_url; set it here
        # only to override.
        "env_overrides": {
            "SERVICENOW_SECRET_ID": f"{ENV}/{PROJECT}/servicenow",
        },
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
    """Idempotently create the IAM role CodeBuild assumes, and ALWAYS refresh
    its inline policy.

    The ECR push statement is scoped to `repo_arns`. When a new agent (and its
    ECR repo) is added, an already-existing role would otherwise keep the stale
    repo list and CodeBuild's `docker push` fails with `ecr:InitiateLayerUpload
    ... no identity-based policy allows`. So we re-put the policy on every run,
    not just at creation.
    """
    trust = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "codebuild.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }],
    }
    created = False
    try:
        existing = iam.get_role(RoleName=CODEBUILD_ROLE_NAME)
        role_arn = existing["Role"]["Arn"]
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise
        role = iam.create_role(
            RoleName=CODEBUILD_ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(trust),
            Description="ARBITER agent image builder",
        )
        role_arn = role["Role"]["Arn"]
        created = True

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
    # Always (re)apply — picks up newly-added ECR repos on existing roles.
    iam.put_role_policy(
        RoleName=CODEBUILD_ROLE_NAME,
        PolicyName="AgentBuilderPolicy",
        PolicyDocument=json.dumps(policy),
    )
    log.info("%s CodeBuild role %s (repos: %d)",
             "Created" if created else "Refreshed policy on", CODEBUILD_ROLE_NAME, len(repo_arns))
    time.sleep(8)  # IAM eventual consistency before CodeBuild assumes/uses it
    return role_arn


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

    # Build the zip in a merged context so the agent image can `from _shared.token_usage
    # import ...` — the per-agent Dockerfile copies _shared/ into /app at build time.
    shared_src = PROJECT_ROOT / "agents" / "_shared"
    with tempfile.TemporaryDirectory() as tmpdir:
        build_ctx = Path(tmpdir) / "ctx"
        shutil.copytree(agent_src, build_ctx)
        if shared_src.exists():
            shutil.copytree(shared_src, build_ctx / "_shared")
        # Optional: vendor extra source packages that live OUTSIDE agents/ (e.g. the
        # shared arbiter_rag RAG library under rag_src/) into the build context so
        # the image can import them WITHOUT forking the canonical copy. Keyed per-agent
        # so only agents that opt in pay the image-size cost; the agent Dockerfile must
        # COPY the destination dir explicitly.
        for src_rel, dest_name in agent.get("extra_pkgs", []):
            pkg_src = PROJECT_ROOT / src_rel
            if not pkg_src.exists():
                raise SystemExit(f"extra_pkgs source not found for {agent['name']}: {pkg_src}")
            shutil.copytree(
                pkg_src, build_ctx / dest_name,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"),
            )
        zip_base = Path(tmpdir) / agent["name"]
        zip_path = shutil.make_archive(str(zip_base), "zip", root_dir=str(build_ctx))
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
def _patch_api_handler_lambda(runtime_arns: dict[str, str]) -> None:
    """Set the master + specialist runtime ARNs (and MEMORY_ID) on the api_handler.

    The MCP page chats directly to a specialist by sending a "target" to
    POST /chat; the Lambda resolves the target → one of these ARNs. The Analyst
    page sends no target and routes to the master. Also drives GET /agent-status.

    Preserves any other env vars already set. Triggers a cold start on the
    next invocation so the new values are picked up. ARNs for agents not built
    in this run are backfilled from the live runtimes (via find_runtime) so a
    partial run (e.g. --agents paloalto-specialist master-orchestrator) still
    sets the COMPLETE set — important because the 06-api SAM template resets
    every *_RUNTIME_ARN env var to "" on deploy, and the standard workflow runs
    this patch last to repair it.
    """
    lambda_client = session.client("lambda")
    func_name = f"{PREFIX}-api-handler"
    try:
        cur = lambda_client.get_function_configuration(FunctionName=func_name)
    except ClientError as e:
        log.warning("api_handler Lambda %s not found, skipping env patch: %s", func_name, e)
        return

    env = (cur.get("Environment") or {}).get("Variables") or {}
    # name (in AGENTS) → api_handler env var.
    arn_env_map = {
        "master-orchestrator":   "MASTER_AGENT_RUNTIME_ARN",
        "sharepoint-specialist": "SHAREPOINT_RUNTIME_ARN",
        "awsconfig-specialist":  "AWSCONFIG_RUNTIME_ARN",
        "zscaler-specialist":    "ZSCALER_RUNTIME_ARN",
        "paloalto-specialist":   "PALOALTO_RUNTIME_ARN",
        "structured-specialist": "STRUCTURED_RUNTIME_ARN",
        "sales-specialist":      "SALES_RUNTIME_ARN",
        "hr-specialist":         "HR_RUNTIME_ARN",
        "jira-specialist":       "JIRA_RUNTIME_ARN",
        "servicenow-specialist": "SERVICENOW_RUNTIME_ARN",
    }
    desired = {}
    for name, env_key in arn_env_map.items():
        arn = runtime_arns.get(name)
        if not arn:
            # Backfill from the live runtime so a --agents-scoped run (or a
            # fresh SAM deploy that blanked the env) doesn't drop the others.
            runtime_name = f"{PREFIX}-{name}".replace("-", "_")[:63]
            existing = find_runtime(runtime_name)
            if existing:
                arn = existing.get("agentRuntimeArn", "")
        if arn:
            desired[env_key] = arn
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


# ──────────────────────────── input validation ──────────────────
# Catches the failure mode that took down all 4 runtimes once: someone
# pastes the docstring example verbatim (KB_ID=ABCDEFGHIJ, MASTER_MEMORY_ID=<id>,
# GUARDRAIL_VERSION="Version 1") and ends up overwriting good env vars with
# placeholder/malformed values. By the time Bedrock rejects the InvokeModel
# call the runtimes are already broken; pre-flight validation here stops the
# script before any update_agent_runtime call.

_DOCSTRING_PLACEHOLDERS = {
    "ABCDEFGHIJ", "abcdefghij",  # KB_ID example from this file's docstring
    "<id>", "<ID>",               # angle-bracket placeholders, common copy-paste artifact
    "xxxxx", "XXXXX",
}


def _validate_inputs() -> None:
    """Pre-flight validation of env-var inputs. Exits non-zero on bad values."""
    fatal: list[str] = []

    if KB_ID in _DOCSTRING_PLACEHOLDERS:
        fatal.append(
            f"KB_ID={KB_ID!r} is a placeholder. The real KB_ID lives in "
            f"Infra/params/dev.json; extract it with: "
            f"jq -r '.[] | select(.ParameterKey==\"KbId\") | .ParameterValue' Infra/params/dev.json"
        )
    elif not KB_ID:
        log.warning("KB_ID env var is empty — agents will deploy but KB-backed tools will be no-ops")

    # GUARDRAIL_VERSION must be 'DRAFT' or a positive integer string. Bedrock
    # InvokeModel rejects anything else with HTTP 500, which the runtime
    # surfaces as 500 → api_handler 502 → user-facing chat failure. The
    # common typo is "Version 1" (display-string copied from the console).
    if GUARDRAIL_ID and GUARDRAIL_VERSION not in ("DRAFT",) and not GUARDRAIL_VERSION.isdigit():
        fatal.append(
            f"GUARDRAIL_VERSION={GUARDRAIL_VERSION!r} is malformed. Must be exactly "
            f"'DRAFT' or a positive integer string like '1'. 'Version 1' is the "
            f"console display string, NOT a valid API value."
        )

    if GUARDRAIL_ID in _DOCSTRING_PLACEHOLDERS:
        fatal.append(
            f"GUARDRAIL_ID={GUARDRAIL_ID!r} is a placeholder. Discover the real "
            f"value with: aws bedrock list-guardrails --region us-east-1"
        )

    if MASTER_MEMORY_ID in _DOCSTRING_PLACEHOLDERS:
        fatal.append(
            f"MASTER_MEMORY_ID={MASTER_MEMORY_ID!r} is a placeholder. Discover the real "
            f"value with: aws bedrock-agentcore-control list-memories --region us-east-1"
        )

    if fatal:
        log.error("Input validation failed — refusing to deploy:")
        for msg in fatal:
            log.error("  • %s", msg)
        log.error("Fix the env vars and re-run. (If you actually want to deploy with these "
                  "values, edit _DOCSTRING_PLACEHOLDERS in this file — but you almost certainly don't.)")
        raise SystemExit(2)


# ──────────────────────────── main ──────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agents", nargs="*", help="Only deploy specific agents by name")
    parser.add_argument("--skip-build", action="store_true", help="Skip docker build/push (reuse :latest)")
    args = parser.parse_args()

    _validate_inputs()

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
    # Scope ECR permissions to every agent repo (one per AGENTS entry) by ARN.
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
            # Token Tracking — all 4 agents write best-effort usage records here
            # via _shared/token_usage.py. Empty value disables the write path
            # cleanly (the helper short-circuits when the table name is unset).
            "TOKEN_USAGE_TABLE": f"{PREFIX}-token-usage",
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
                ("paloalto-specialist", "PALOALTO_RUNTIME_ARN"),
                ("structured-specialist", "STRUCTURED_RUNTIME_ARN"),
                ("sales-specialist", "SALES_RUNTIME_ARN"),
                ("hr-specialist", "HR_RUNTIME_ARN"),
                ("jira-specialist", "JIRA_RUNTIME_ARN"),
                ("servicenow-specialist", "SERVICENOW_RUNTIME_ARN"),
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

        # Per-agent execution role: agents with a "role_export" get their own
        # least-privilege role (Tier-0 isolation for the external-SaaS JIRA
        # agent); everyone else uses the shared AgentCoreRuntimeRole. Fall back
        # to the shared role if the dedicated export isn't published yet.
        agent_role_arn = role_arn
        if agent.get("role_export"):
            # cf_export raises SystemExit when the export is missing (e.g. the
            # updated 09-agentcore stack hasn't been deployed yet). Catch it so a
            # missing dedicated role degrades to the shared role with a warning
            # rather than aborting the whole agent deploy.
            try:
                agent_role_arn = cf_export(agent["role_export"])
            except (SystemExit, Exception):
                log.warning("Role export %s not found — falling back to shared role for %s "
                            "(deploy 09-agentcore to get the least-privilege JIRA role).",
                            agent["role_export"], agent["name"])
        log.info("  %s role: %s", agent["name"], agent_role_arn)

        runtime_arn = deploy_runtime(agent, image_uri, agent_role_arn, subnet_ids, sg_id, env_vars)
        runtime_arns[agent["name"]] = runtime_arn
        log.info("✓ %s → %s", agent["name"], runtime_arn)

    # Wire the api_handler Lambda to the runtimes by patching the master +
    # specialist ARN env vars. This bridges API Gateway / Function URL →
    # AgentCore, and powers per-agent routing on the MCP page + /agent-status.
    if runtime_arns:
        _patch_api_handler_lambda(runtime_arns)

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
