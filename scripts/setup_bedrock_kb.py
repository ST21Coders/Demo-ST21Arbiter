#!/usr/bin/env python3
"""Create the ARBITER Bedrock Knowledge Base end-to-end.

Steps performed:
  1. Create the OpenSearch Serverless vector index `policy-vectors`
  2. Grant the KB service role access to that index (data access policy update)
  3. Create the Bedrock Knowledge Base
  4. Attach the S3 data source (s3://dev-st21arbiter-poc-processed/policies/)
  5. Create the Bedrock Guardrail (or, if it already exists, UPDATE it in place
     so the live DRAFT matches this script — pass --publish-version to also cut
     a new immutable version)
  6. Print KB / Guardrail IDs + version to stdout so agent scripts can pick them up

This script is idempotent — re-running it skips/creates resources that already
exist; the guardrail is kept in sync (updated) rather than skipped.

Prerequisites (deployed by deploy.sh):
  - dev-st21arbiter-poc-02-security (KMS keys)
  - dev-st21arbiter-poc-04-storage (OpenSearch collection, processed bucket)

Usage:
  pip install boto3 opensearch-py requests-aws4auth
  AWS_PROFILE=... python scripts/setup_bedrock_kb.py
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any

import boto3
from botocore.exceptions import ClientError
from opensearchpy import OpenSearch, RequestsHttpConnection
from requests_aws4auth import AWS4Auth

ENV = os.environ.get("ENVIRONMENT", "dev")
PROJECT = os.environ.get("PROJECT", "st21arbiter-poc")
REGION = os.environ.get("AWS_REGION", "us-east-1")
PREFIX = f"{ENV}-{PROJECT}"

INDEX_NAME = "policy-vectors"
EMBEDDING_MODEL_ARN = (
    f"arn:aws:bedrock:{REGION}::foundation-model/amazon.titan-embed-text-v2:0"
)
EMBEDDING_DIMS = 1024  # titan-embed-text-v2 default

# Comma-separated S3 key prefixes to limit ingestion. Empty = entire bucket.
# Example: S3_INCLUSION_PREFIXES="policies/,sharepoint/,zscaler/"
S3_INCLUSION_PREFIXES = [
    p.strip() for p in os.environ.get("S3_INCLUSION_PREFIXES", "").split(",") if p.strip()
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("setup_kb")

session = boto3.Session(region_name=REGION)
cfn = session.client("cloudformation")
aoss = session.client("opensearchserverless")
bedrock_agent = session.client("bedrock-agent")
bedrock = session.client("bedrock")
iam = session.client("iam")
sts = session.client("sts")
ACCOUNT_ID = sts.get_caller_identity()["Account"]


# ──────────────────────────── helpers ────────────────────────────
def cf_export(name: str) -> str:
    """Resolve a CloudFormation export by name."""
    paginator = cfn.get_paginator("list_exports")
    for page in paginator.paginate():
        for exp in page["Exports"]:
            if exp["Name"] == name:
                return exp["Value"]
    raise SystemExit(f"CloudFormation export not found: {name}")


def allow_public_network_access() -> None:
    """Flip the OpenSearch Serverless network policy to AllowFromPublic=true.

    DEV ONLY — production should restrict access via VPC endpoints. This is
    required so that scripts running locally (outside the VPC) can hit the
    collection's data plane endpoint to create indices, etc.
    """
    policy_name = f"{PREFIX}-net"
    try:
        current = aoss.get_security_policy(name=policy_name, type="network")
    except ClientError as e:
        log.warning("Could not read network policy %s: %s", policy_name, e)
        return

    raw = current["securityPolicyDetail"]["policy"]
    policy_json = json.loads(raw) if isinstance(raw, str) else raw

    changed = False
    for rule_block in policy_json:
        if not rule_block.get("AllowFromPublic"):
            rule_block["AllowFromPublic"] = True
            changed = True
        # When AllowFromPublic=true, SourceVPCEs / SourceServices must be absent
        for key in ("SourceVPCEs", "SourceServices"):
            if key in rule_block:
                del rule_block[key]
                changed = True

    if not changed:
        log.info("Network policy %s already allows public access", policy_name)
        return

    aoss.update_security_policy(
        name=policy_name,
        type="network",
        policyVersion=current["securityPolicyDetail"]["policyVersion"],
        policy=json.dumps(policy_json),
    )
    log.info("Network policy %s updated: AllowFromPublic=true (dev convenience)", policy_name)
    time.sleep(15)  # network policy propagation


def wait_for_index(client: OpenSearch, index: str, timeout: int = 120) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if client.indices.exists(index=index):
            log.info("Index %s is available", index)
            return
        time.sleep(3)
    raise SystemExit(f"Timed out waiting for index {index}")


# ──────────────────────────── 1. Create OpenSearch index ─────────
def create_opensearch_index(collection_endpoint: str) -> None:
    host = collection_endpoint.replace("https://", "")
    credentials = session.get_credentials()
    auth = AWS4Auth(
        credentials.access_key,
        credentials.secret_key,
        REGION,
        "aoss",
        session_token=credentials.token,
    )
    client = OpenSearch(
        hosts=[{"host": host, "port": 443}],
        http_auth=auth,
        use_ssl=True,
        verify_certs=True,
        connection_class=RequestsHttpConnection,
        timeout=30,
    )

    if client.indices.exists(index=INDEX_NAME):
        log.info("OpenSearch index %s already exists — skipping", INDEX_NAME)
        return

    body = {
        "settings": {"index": {"knn": True}},
        "mappings": {
            "properties": {
                "embedding": {
                    "type": "knn_vector",
                    "dimension": EMBEDDING_DIMS,
                    "method": {
                        "engine": "faiss",
                        "name": "hnsw",
                        "space_type": "l2",
                    },
                },
                "text": {"type": "text"},
                "metadata": {"type": "text"},
            }
        },
    }
    client.indices.create(index=INDEX_NAME, body=body)
    log.info("Created OpenSearch index %s", INDEX_NAME)
    wait_for_index(client, INDEX_NAME)


# ──────────────────────────── 2. KB IAM role ─────────────────────
def ensure_kb_role(
    processed_bucket_arn: str,
    collection_arn: str,
    s3_kms_arn: str,
) -> str:
    role_name = f"{PREFIX}-kb-role"
    try:
        existing = iam.get_role(RoleName=role_name)
        log.info("KB role %s already exists", role_name)
        return existing["Role"]["Arn"]
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise

    trust = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "bedrock.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {"StringEquals": {"aws:SourceAccount": ACCOUNT_ID}},
            }
        ],
    }
    role = iam.create_role(
        RoleName=role_name,
        AssumeRolePolicyDocument=json.dumps(trust),
        Description="ARBITER Bedrock Knowledge Base service role",
    )
    log.info("Created KB role %s", role_name)

    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["bedrock:InvokeModel"],
                "Resource": EMBEDDING_MODEL_ARN,
            },
            {
                "Effect": "Allow",
                "Action": ["aoss:APIAccessAll"],
                "Resource": collection_arn,
            },
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:ListBucket"],
                "Resource": [processed_bucket_arn, f"{processed_bucket_arn}/*"],
            },
            {
                "Effect": "Allow",
                "Action": ["kms:Decrypt", "kms:DescribeKey"],
                "Resource": s3_kms_arn,
            },
        ],
    }
    iam.put_role_policy(
        RoleName=role_name,
        PolicyName="KBPolicy",
        PolicyDocument=json.dumps(policy),
    )
    log.info("Attached inline policy to %s", role_name)
    time.sleep(8)  # IAM eventual consistency before KB tries to assume
    return role["Role"]["Arn"]


# ──────────────────────────── 3. Grant role access to index ──────
def get_caller_arn() -> str:
    """Return the ARN of the current AWS caller (root, user, or assumed-role)."""
    arn = sts.get_caller_identity()["Arn"]
    # If the caller is an assumed role (sts:AssumeRole session), normalize to
    # the underlying role ARN since OpenSearch policies match role ARNs.
    if ":assumed-role/" in arn:
        # arn:aws:sts::ACCT:assumed-role/ROLE/SESSION → arn:aws:iam::ACCT:role/ROLE
        parts = arn.split(":")
        role_name = parts[5].split("/")[1]
        arn = f"arn:aws:iam::{parts[4]}:role/{role_name}"
    return arn


def add_principals_to_data_policy(principal_arns: list[str]) -> None:
    """Ensure the given ARNs are listed in the OpenSearch data access policy."""
    policy_name = f"{PREFIX}-data"
    try:
        current = aoss.get_access_policy(name=policy_name, type="data")
    except ClientError as e:
        log.warning("Could not read data access policy %s: %s", policy_name, e)
        return

    raw_policy = current["accessPolicyDetail"]["policy"]
    policy_json = json.loads(raw_policy) if isinstance(raw_policy, str) else raw_policy
    added: list[str] = []
    for rule_block in policy_json:
        principals = rule_block.setdefault("Principal", [])
        for arn in principal_arns:
            if arn not in principals:
                principals.append(arn)
                if arn not in added:
                    added.append(arn)

    if not added:
        log.info("Data access policy %s already grants all required principals", policy_name)
        return

    aoss.update_access_policy(
        name=policy_name,
        type="data",
        policyVersion=current["accessPolicyDetail"]["policyVersion"],
        policy=json.dumps(policy_json),
    )
    log.info("Granted index access to: %s", added)
    time.sleep(8)  # policy propagation


# ──────────────────────────── 4. Create KB + Data Source ─────────
def create_kb(
    kb_role_arn: str,
    collection_arn: str,
    processed_bucket_arn: str,
) -> dict[str, Any]:
    kb_name = f"{PREFIX}-policy-kb"
    for kb in bedrock_agent.list_knowledge_bases()["knowledgeBaseSummaries"]:
        if kb["name"] == kb_name:
            log.info("Knowledge base %s already exists (%s)", kb_name, kb["knowledgeBaseId"])
            return {"knowledgeBaseId": kb["knowledgeBaseId"]}

    resp = bedrock_agent.create_knowledge_base(
        name=kb_name,
        description="ARBITER policy document knowledge base",
        roleArn=kb_role_arn,
        knowledgeBaseConfiguration={
            "type": "VECTOR",
            "vectorKnowledgeBaseConfiguration": {
                "embeddingModelArn": EMBEDDING_MODEL_ARN,
            },
        },
        storageConfiguration={
            "type": "OPENSEARCH_SERVERLESS",
            "opensearchServerlessConfiguration": {
                "collectionArn": collection_arn,
                "vectorIndexName": INDEX_NAME,
                "fieldMapping": {
                    "vectorField": "embedding",
                    "textField": "text",
                    "metadataField": "metadata",
                },
            },
        },
    )
    kb_id = resp["knowledgeBase"]["knowledgeBaseId"]
    log.info("Created knowledge base %s (id=%s)", kb_name, kb_id)

    s3_config: dict[str, Any] = {"bucketArn": processed_bucket_arn}
    if S3_INCLUSION_PREFIXES:
        s3_config["inclusionPrefixes"] = S3_INCLUSION_PREFIXES
        log.info("Ingestion limited to prefixes: %s", S3_INCLUSION_PREFIXES)
    else:
        log.info("Ingesting entire bucket (no inclusion prefix filter)")

    ds = bedrock_agent.create_data_source(
        knowledgeBaseId=kb_id,
        name=f"{PREFIX}-processed-docs",
        description="Processed policy documents from S3",
        dataSourceConfiguration={
            "type": "S3",
            "s3Configuration": s3_config,
        },
        vectorIngestionConfiguration={
            "chunkingConfiguration": {
                "chunkingStrategy": "FIXED_SIZE",
                "fixedSizeChunkingConfiguration": {
                    "maxTokens": 512,
                    "overlapPercentage": 20,
                },
            }
        },
    )
    log.info("Attached data source %s", ds["dataSource"]["dataSourceId"])
    return {"knowledgeBaseId": kb_id, "dataSourceId": ds["dataSource"]["dataSourceId"]}


# ──────────────────────────── 5. Guardrail ───────────────────────
# Single source of truth for the guardrail policy. Both create_guardrail and
# update_guardrail take these same kwargs, so editing here keeps create + sync
# in lockstep. To add/remove a denied topic, PII entity, or content filter,
# change it here and re-run this script.
GUARDRAIL_NAME = f"{PREFIX}-guardrail"
GUARDRAIL_DESCRIPTION = "ARBITER content safety, PII, denied topics, grounding"
GUARDRAIL_POLICY_KWARGS: dict[str, Any] = {
    "blockedInputMessaging": "Your request was blocked by content safety policies.",
    "blockedOutputsMessaging": "The response was blocked by content safety policies.",
    "contentPolicyConfig": {
        "filtersConfig": [
            {"type": t, "inputStrength": "HIGH", "outputStrength": "HIGH"}
            for t in ("SEXUAL", "VIOLENCE", "HATE", "INSULTS", "MISCONDUCT")
        ]
        + [{"type": "PROMPT_ATTACK", "inputStrength": "HIGH", "outputStrength": "NONE"}],
    },
    "sensitiveInformationPolicyConfig": {
        "piiEntitiesConfig": [
            {"type": "US_SOCIAL_SECURITY_NUMBER", "action": "ANONYMIZE"},
            {"type": "CREDIT_DEBIT_CARD_NUMBER", "action": "ANONYMIZE"},
            {"type": "AWS_ACCESS_KEY", "action": "BLOCK"},
            {"type": "AWS_SECRET_KEY", "action": "BLOCK"},
        ],
    },
    "topicPolicyConfig": {
        "topicsConfig": [
            {
                "name": "CredentialDisclosure",
                "definition": "Requests asking the agent to reveal stored credentials, API keys, or secrets",
                "type": "DENY",
            },
            {
                "name": "InfrastructureDestruction",
                "definition": "Requests to delete VPCs, subnets, production databases, or critical infrastructure",
                "type": "DENY",
            },
            {
                "name": "Politics",
                "definition": "Requests for political opinions, endorsements, or partisan commentary",
                "type": "DENY",
            },
        ]
    },
}


def _wait_guardrail_ready(guardrail_id: str, timeout: int = 180) -> None:
    """Block until the guardrail's DRAFT settles to READY (versions can only be
    cut from a READY guardrail)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = bedrock.get_guardrail(guardrailIdentifier=guardrail_id).get("status")
        if status == "READY":
            return
        if status == "FAILED":
            raise SystemExit(f"Guardrail {guardrail_id} entered FAILED status")
        log.info("  guardrail status: %s", status)
        time.sleep(5)
    raise SystemExit(f"Timeout waiting for guardrail {guardrail_id} to be READY")


def ensure_guardrail(publish_version: bool = False) -> dict[str, str]:
    """Create the guardrail if missing, otherwise UPDATE it in place so the live
    DRAFT matches this script's policy. With publish_version=True, also cut a new
    immutable version. Returns {"id", "version"} (version is "DRAFT" unless published)."""
    existing_id = next(
        (g["id"] for g in bedrock.list_guardrails().get("guardrails", [])
         if g["name"] == GUARDRAIL_NAME),
        None,
    )

    if existing_id:
        bedrock.update_guardrail(
            guardrailIdentifier=existing_id,
            name=GUARDRAIL_NAME,
            description=GUARDRAIL_DESCRIPTION,
            **GUARDRAIL_POLICY_KWARGS,
        )
        guardrail_id = existing_id
        log.info("Updated guardrail %s (id=%s) to match script policy", GUARDRAIL_NAME, guardrail_id)
    else:
        resp = bedrock.create_guardrail(
            name=GUARDRAIL_NAME,
            description=GUARDRAIL_DESCRIPTION,
            **GUARDRAIL_POLICY_KWARGS,
        )
        guardrail_id = resp["guardrailId"]
        log.info("Created guardrail %s (id=%s)", GUARDRAIL_NAME, guardrail_id)

    version = "DRAFT"
    if publish_version:
        _wait_guardrail_ready(guardrail_id)
        vresp = bedrock.create_guardrail_version(
            guardrailIdentifier=guardrail_id,
            description="Published by setup_bedrock_kb.py",
        )
        version = vresp["version"]
        log.info("Published guardrail %s version %s", GUARDRAIL_NAME, version)

    return {"id": guardrail_id, "version": version}


# ──────────────────────────── main ───────────────────────────────
def main(publish_version: bool = False) -> None:
    log.info("Resolving CloudFormation exports...")
    collection_arn = cf_export(f"{PREFIX}-OpenSearchCollectionArn")
    collection_endpoint = cf_export(f"{PREFIX}-OpenSearchEndpoint")
    processed_bucket_arn = cf_export(f"{PREFIX}-ProcessedBucketArn")
    s3_kms_arn = cf_export(f"{PREFIX}-S3KeyArn")

    log.info("OpenSearch endpoint: %s", collection_endpoint)
    log.info("Processed bucket:    %s", processed_bucket_arn)

    caller_arn = get_caller_arn()
    log.info("Caller identity:     %s", caller_arn)

    log.info("Step 1a: granting current caller access to OpenSearch index...")
    add_principals_to_data_policy([caller_arn])

    log.info("Step 1b: enabling public network access on OpenSearch collection (dev)...")
    allow_public_network_access()

    log.info("Step 2: creating vector index...")
    create_opensearch_index(collection_endpoint)

    log.info("Step 3: ensuring KB IAM role...")
    kb_role_arn = ensure_kb_role(processed_bucket_arn, collection_arn, s3_kms_arn)

    log.info("Step 4: granting KB role access to OpenSearch index...")
    add_principals_to_data_policy([kb_role_arn])

    log.info("Step 5: creating knowledge base + data source...")
    kb_info = create_kb(kb_role_arn, collection_arn, processed_bucket_arn)

    log.info("Step 6: ensuring guardrail (create or sync)...")
    guardrail = ensure_guardrail(publish_version=publish_version)

    print()
    print("════════════════════════════════════════════════════════")
    print("  Bedrock KB setup complete")
    print("════════════════════════════════════════════════════════")
    print(json.dumps(
        {**kb_info, "guardrailId": guardrail["id"], "guardrailVersion": guardrail["version"]},
        indent=2,
    ))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Provision/sync the ARBITER Bedrock KB + Guardrail")
    parser.add_argument(
        "--publish-version",
        action="store_true",
        help="After syncing the guardrail DRAFT, publish a new immutable version "
             "(copy its number into params/dev.json::GuardrailVersion).",
    )
    cli = parser.parse_args()

    try:
        main(publish_version=cli.publish_version)
    except Exception as e:
        log.error("Setup failed: %s", e)
        sys.exit(1)
