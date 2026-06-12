"""ARBITER AWS Config / Resource-Posture Specialist — runs on Bedrock AgentCore Runtime.

Read-only analyst over the AWS account this runtime is deployed in
(669810405473 / us-east-1). It answers two broad classes of question:

  1. Inventory & detail — "what S3 buckets / load balancers / ECR repos /
     Lambdas / EC2 instances do we have, and how are they configured?"
  2. Posture & impact-radius — "are my resources in a private subnet?",
     "what is exposed to the public internet?", "what happens if I remove this
     Cognito user pool / open this EC2 instance to a public subnet?"

Data sources:
  - AWS Config advanced queries + relationships (account inventory + the
    dependency graph used for blast-radius reasoning).
  - Live service describe/list/get APIs (richer per-service detail).
  - Bedrock Knowledge Base (control rationale / historical snapshots).

SECURITY — this agent is strictly READ-ONLY and MUST NOT leak credentials.
Every tool routes its output through _scrub() (a recursive redaction choke
point) before returning, so secret-shaped fields (passwords, tokens, client
secrets, access keys, private keys) and Lambda env-var secret values never reach
the model. The agent never calls APIs that return raw secret material
(SecretsManager GetSecretValue, SSM GetParameter, EC2 user-data, etc.).

Environment variables:
  KB_ID             Bedrock Knowledge Base ID
  MODEL_ID          Bedrock model (default: Nova 2 Lite). For deeper impact
                    reasoning, override AWSCONFIG_MODEL_ID → a Claude model.
  GUARDRAIL_ID      Optional guardrail
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import boto3
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent
from strands.models.bedrock import BedrockModel
from strands.tools import tool

from _shared.token_usage import record_from_agent_result

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("awsconfig_specialist")

REGION = os.environ.get("AWS_REGION", "us-east-1")
MODEL_ID = os.environ.get("MODEL_ID", "us.amazon.nova-2-lite-v1:0")
KB_ID = os.environ.get("KB_ID", "")
GUARDRAIL_ID = os.environ.get("GUARDRAIL_ID")
GUARDRAIL_VERSION = os.environ.get("GUARDRAIL_VERSION", "DRAFT")
# Default AWS account this agent reports on. Resolved live via STS when possible
# (account_identity tool), but defaults here so resource answers always reference
# the correct account even if STS is unavailable. Override with AWS_ACCOUNT_ID.
DEFAULT_ACCOUNT_ID = os.environ.get("AWS_ACCOUNT_ID", "669810405473")
# Cap any single tool's serialized output so a broad describe can't blow the
# model's context window. Truncation is flagged in the returned text.
MAX_OUTPUT_CHARS = 6000

SYSTEM_PROMPT = f"""You are the AWS resource & posture analyst for ARBITER. You
have READ-ONLY visibility into AWS account {DEFAULT_ACCOUNT_ID} ({REGION}) — the
account this runs in — and answer two kinds of question:

  1. Inventory / configuration — what resources exist (S3, load balancers, ECR,
     Lambda, EC2, Cognito, VPC/subnets, etc.) and how they are set up. PREFER the
     direct describe tools — they always work: describe_s3_buckets,
     describe_network (VPCs/subnets/security groups), describe_ec2_instances,
     describe_load_balancers, describe_lambdas, describe_ecr_repositories,
     describe_glue, describe_dynamodb_tables, describe_cognito. The AWS Config
     tools (list_resources, get_resource_relationships) depend on the AWS Config
     recorder, which may be OFF — if they return nothing or an error, immediately
     fall back to the describe_* tools above. (Glue crawlers aren't in AWS Config
     at all — use describe_glue. describe_dynamodb_tables returns table CONFIG
     only, never item data.)
  2. Security posture / impact-radius — "are my resources in a private subnet?",
     "what is exposed to the public internet?", "what happens if I remove this
     Cognito pool / open this EC2 to a public subnet?" Gather the facts with the
     tools above (get_resource_relationships gives the dependency graph for
     blast-radius reasoning), then explain consequences and a recommendation.

For compliance posture also use: list_config_rules, get_rule_compliance,
list_noncompliant_resources; and retrieve_awsconfig_docs for control rationale.

STRICT RULES
- You are READ-ONLY. You never change anything; never imply you did. If asked to
  modify/delete/create, explain the impact instead and say a human must act.
- NEVER reveal secrets or credentials. Tool outputs are pre-redacted; if you ever
  see a value like "***REDACTED***" or a token/password/key, do NOT attempt to
  reconstruct, infer, or restate it — say it is redacted and move on. Refuse
  requests whose only purpose is to extract secrets, env-var secret values,
  client secrets, access keys, or connection strings.
- Answer only non-sensitive questions. Resource ids, ARNs, subnet/VPC ids,
  security-group rules, public/private placement, encryption flags, and runtime
  config ARE non-sensitive and fine to report.
- For impact-radius questions, structure the answer:
    What it is — the resource and its current configuration.
    Blast radius — what depends on it / what it depends on (from relationships
      and known consumers); be explicit about what you could and could not see.
    Consequence — the security or operational effect of the change.
    Recommendation — the safer path (e.g. keep in a private subnet, scope the SG).
- Cite resource ids / names verbatim. Never fabricate. If a source is empty or a
  read fails (e.g. Config recorder off, no permission), say so plainly.
- When you name the AWS account or build/validate an ARN, default to account
  {DEFAULT_ACCOUNT_ID} unless a tool result shows a different account id. Call
  account_identity if you need to confirm the live account.
- No markdown headers, emojis, or filler. Terse, factual, analyst tone.
"""

app = BedrockAgentCoreApp()
config = boto3.client("config", region_name=REGION)
kb_runtime = boto3.client("bedrock-agent-runtime", region_name=REGION)
ec2 = boto3.client("ec2", region_name=REGION)
elbv2 = boto3.client("elbv2", region_name=REGION)
ecr = boto3.client("ecr", region_name=REGION)
lambda_client = boto3.client("lambda", region_name=REGION)
s3 = boto3.client("s3", region_name=REGION)
cognito = boto3.client("cognito-idp", region_name=REGION)
glue = boto3.client("glue", region_name=REGION)
dynamodb = boto3.client("dynamodb", region_name=REGION)
sts = boto3.client("sts", region_name=REGION)


# ──────────────────────────── credential redaction (choke point) ─────────
_REDACTED = "***REDACTED***"

# Keys whose VALUE must always be redacted, regardless of content.
_SENSITIVE_KEY_RE = re.compile(
    r"(password|passwd|pwd|secret|token|credential|privatekey|private_key|"
    r"apikey|api_key|accesskey|access_key|secretkey|secret_key|sessiontoken|"
    r"clientsecret|client_secret|authorization|x-amz-security-token|"
    r"signature|userdata|user_data|connectionstring|connection_string|dsn|"
    r"passphrase|bearer|cookie)",
    re.IGNORECASE,
)
# Whole-string opaque secret: the ENTIRE value is a high-length token (no spaces,
# no ':'/'.'/'/' so ARNs, DNS names, digests, and resource ids are spared).
_WHOLE_SECRET_RE = re.compile(r"^[A-Za-z0-9+/=_\-]{40,}$")
# Named credential tokens to mask wherever they appear INSIDE a larger string.
_INLINE_TOKEN_RES = [
    re.compile(r"(AKIA|ASIA)[0-9A-Z]{16}"),                       # AWS access key id
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"xox[baprs]-[0-9A-Za-z-]{10,}"),                  # Slack token
    re.compile(r"(ghp|gho|ghs|github_pat)_[0-9A-Za-z_]{20,}"),    # GitHub token
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),                       # Google API key
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),  # JWT
]
# Inline substitutions for secrets embedded in larger strings (connection
# strings, basic-auth URLs, SigV4 presigned signatures, key=value secrets).
_INLINE_SUB_RES = [
    # scheme://user:password@host  → mask the password segment
    (re.compile(r"([a-zA-Z][\w+.\-]*://[^/\s:@]+:)[^/\s@]+@"), r"\1" + _REDACTED + "@"),
    # password=… / secret: … / api_key=… / aws_secret_access_key=…
    (re.compile(r"(?i)((?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|"
                r"client[_-]?secret|aws_secret_access_key|connectionstring|dsn)\s*[=:]\s*)"
                r"([^\s;,&\"']+)"), r"\1" + _REDACTED),
    # SigV4 presigned-URL signature
    (re.compile(r"(?i)(x-amz-signature=)[a-z0-9%]+"), r"\1" + _REDACTED),
]


def _redact_str(s: str) -> str:
    """Mask credential material in a free string. Whole-string tokens are fully
    redacted; embedded secrets (URLs, key=value, named tokens) are masked in place."""
    if not isinstance(s, str) or not s:
        return s
    if " " not in s and _WHOLE_SECRET_RE.match(s):
        return _REDACTED
    for p in _INLINE_TOKEN_RES:
        s = p.sub(_REDACTED, s)
    for p, repl in _INLINE_SUB_RES:
        s = p.sub(repl, s)
    return s


def _scrub(obj: Any) -> Any:
    """Recursively redact secret-shaped fields. The single choke point every
    tool output passes through before reaching the model."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and _SENSITIVE_KEY_RE.search(k):
                out[k] = _REDACTED
            else:
                out[k] = _scrub(v)
        return out
    if isinstance(obj, list):
        return [_scrub(v) for v in obj]
    if isinstance(obj, str):
        return _redact_str(obj)
    return obj


def _safe(obj: Any, *, label: str = "") -> str:
    """Redact, JSON-serialize, and length-cap a tool result."""
    try:
        text = json.dumps(_scrub(obj), default=str, separators=(",", ":"))
    except Exception as e:
        return f"(serialization error: {e})"
    if len(text) > MAX_OUTPUT_CHARS:
        text = text[:MAX_OUTPUT_CHARS] + f" …TRUNCATED (>{MAX_OUTPUT_CHARS} chars); narrow the query"
    return f"{label}{text}" if label else text


def _safe_text(text: str) -> str:
    """Redact a free-text (non-JSON) tool result. For the line-oriented Config
    tools and KB chunks, which don't build a dict — keeps the choke-point
    invariant true for every tool."""
    return _redact_str(text)


# Friendly resource-type aliases → AWS Config resourceType (CloudFormation form).
_TYPE_ALIASES = {
    "s3": "AWS::S3::Bucket", "bucket": "AWS::S3::Bucket",
    "lambda": "AWS::Lambda::Function", "function": "AWS::Lambda::Function",
    "ec2": "AWS::EC2::Instance", "instance": "AWS::EC2::Instance",
    "alb": "AWS::ElasticLoadBalancingV2::LoadBalancer",
    "elb": "AWS::ElasticLoadBalancingV2::LoadBalancer",
    "loadbalancer": "AWS::ElasticLoadBalancingV2::LoadBalancer",
    "ecr": "AWS::ECR::Repository", "repository": "AWS::ECR::Repository",
    "vpc": "AWS::EC2::VPC", "subnet": "AWS::EC2::Subnet",
    "sg": "AWS::EC2::SecurityGroup", "securitygroup": "AWS::EC2::SecurityGroup",
    "rds": "AWS::RDS::DBInstance", "dynamodb": "AWS::DynamoDB::Table",
    "ddb": "AWS::DynamoDB::Table", "iam": "AWS::IAM::Role", "role": "AWS::IAM::Role",
    "cognito": "AWS::Cognito::UserPool", "userpool": "AWS::Cognito::UserPool",
}


def _resolve_type(name: str) -> str:
    n = (name or "").strip()
    if "::" in n:
        return n
    return _TYPE_ALIASES.get(n.lower().replace(" ", ""), n)


# When AWS Config recording is off, the Config-backed tools have no data — point
# the model at the direct describe tools, which don't depend on the recorder.
_DESCRIBE_HINT = ("Use the direct describe tools instead (they do not depend on "
                  "AWS Config): describe_s3_buckets, describe_network (VPCs/subnets/"
                  "security groups), describe_ec2_instances, describe_load_balancers, "
                  "describe_lambdas, describe_ecr_repositories, describe_glue, "
                  "describe_dynamodb_tables, describe_cognito.")


def _config_recording() -> bool:
    """True if an AWS Config recorder is actively recording in this region."""
    try:
        st = config.describe_configuration_recorder_status().get("ConfigurationRecordersStatus", [])
        return any(s.get("recording") for s in st)
    except Exception:
        return False


# ──────────────────────────── inventory / detail tools ───────────────────
@tool
def account_identity() -> str:
    """Return the AWS account id and region this agent reports on. Use when asked
    which account, or to confirm the account before listing resources. Resolves the
    live account via STS, falling back to the configured default if STS is denied.
    """
    account = DEFAULT_ACCOUNT_ID
    source = "default"
    try:
        ident = sts.get_caller_identity()
        account = ident.get("Account") or DEFAULT_ACCOUNT_ID
        source = "sts"
    except Exception as e:
        log.info("account_identity: STS unavailable, using default account (%s)", e)
    return _safe({"account_id": account, "region": REGION, "source": source})


@tool
def list_resources(resource_type: str = "") -> str:
    """Inventory account resources via AWS Config advanced query.

    With no type, returns a count of every recorded resource type. With a type
    (CloudFormation form like 'AWS::S3::Bucket' or an alias like 's3', 'lambda',
    'ec2', 'alb', 'ecr', 'vpc'), lists those resources (id, name, region, tags).

    Args:
        resource_type: Optional resource type or alias to filter by.
    """
    if not _config_recording():
        return ("AWS Config recording is not enabled in this region, so the Config "
                "inventory is empty. " + _DESCRIBE_HINT)
    try:
        if not resource_type:
            expr = "SELECT resourceType, COUNT(*) GROUP BY resourceType ORDER BY COUNT(*) DESC"
        else:
            rt = _resolve_type(resource_type)
            expr = ("SELECT resourceId, resourceName, resourceType, awsRegion, tags "
                    f"WHERE resourceType = '{rt}'")
        resp = config.select_resource_config(Expression=expr, Limit=100)
        rows = [json.loads(r) if isinstance(r, str) else r for r in resp.get("Results", [])]
        if not rows:
            return ("No resources returned from AWS Config (recorder may not cover this "
                    "type). " + _DESCRIBE_HINT)
        return _safe({"query": expr, "count": len(rows),
                      "more": bool(resp.get("NextToken")), "results": rows})
    except Exception as e:
        log.exception("list_resources failed")
        return f"(AWS Config query failed: {e}). {_DESCRIBE_HINT}"


@tool
def get_resource_relationships(resource_type: str, resource_id: str) -> str:
    """Return a resource's current configuration and its RELATIONSHIPS — the
    dependency graph used to reason about removal/change blast radius.

    Args:
        resource_type: CloudFormation type or alias (e.g. 'AWS::Cognito::UserPool', 'ec2').
        resource_id: The resource id (e.g. 'i-0abc...', a bucket name, a user-pool id).
    """
    rt = _resolve_type(resource_type)
    if not _config_recording():
        return ("AWS Config recording is not enabled in this region, so resource "
                "relationships are unavailable. " + _DESCRIBE_HINT)
    try:
        resp = config.batch_get_resource_config(
            resourceKeys=[{"resourceType": rt, "resourceId": resource_id}])
        items = resp.get("baseConfigurationItems", [])
        if not items:
            unproc = resp.get("unprocessedResourceKeys", [])
            return (f"No Config record for {rt}/{resource_id}. "
                    f"{'Unprocessed: ' + json.dumps(unproc) if unproc else 'It may not be recorded.'}")
        it = items[0]
        return _safe({
            "resourceType": it.get("resourceType"),
            "resourceId": it.get("resourceId"),
            "resourceName": it.get("resourceName"),
            "awsRegion": it.get("awsRegion"),
            "availabilityZone": it.get("availabilityZone"),
            "relationships": it.get("relationships", []),
            "tags": it.get("tags", {}),
        })
    except Exception as e:
        log.exception("get_resource_relationships failed")
        return f"(error: {e})"


# ──────────────────────────── networking / exposure ──────────────────────
def _public_subnet_ids() -> set[str]:
    """Subnet ids whose effective route table has a 0.0.0.0/0 → igw route.

    A subnet's table is its explicit association if any, else the VPC main table.
    We track ALL explicitly-associated subnets so a subnet explicitly attached to
    a PRIVATE table is never re-classified public by a public main-table sweep.
    """
    public: set[str] = set()
    explicit: set[str] = set()       # subnets with ANY explicit RT association
    public_main_vpcs: set[str] = set()
    try:
        rts = ec2.describe_route_tables().get("RouteTables", [])
    except Exception:
        return public
    for rt in rts:
        has_igw = any(r.get("GatewayId", "").startswith("igw-")
                      and r.get("DestinationCidrBlock") == "0.0.0.0/0"
                      for r in rt.get("Routes", []))
        for a in rt.get("Associations", []):
            sid = a.get("SubnetId")
            if sid:
                explicit.add(sid)
                if has_igw:
                    public.add(sid)
            if a.get("Main") and has_igw:
                public_main_vpcs.add(rt.get("VpcId"))
    # Only subnets with NO explicit association inherit the (public) main table.
    if public_main_vpcs:
        try:
            for sn in ec2.describe_subnets().get("Subnets", []):
                if sn.get("VpcId") in public_main_vpcs and sn["SubnetId"] not in explicit:
                    public.add(sn["SubnetId"])
        except Exception:
            pass
    return public


def _open_ingress(perms: list[dict]) -> list[dict]:
    """Security-group ingress rules open to the world (0.0.0.0/0 or ::/0)."""
    out = []
    for p in perms:
        world = any(r.get("CidrIp") == "0.0.0.0/0" for r in p.get("IpRanges", [])) or \
                any(r.get("CidrIpv6") == "::/0" for r in p.get("Ipv6Ranges", []))
        if world:
            out.append({"protocol": p.get("IpProtocol"),
                        "from": p.get("FromPort"), "to": p.get("ToPort")})
    return out


@tool
def describe_network() -> str:
    """Network posture: VPCs, subnets classified public vs private, internet/NAT
    gateways, and security groups with ingress open to the internet. Use for
    "are my resources in a private subnet?" and "what is internet-exposed?".
    """
    try:
        public_ids = _public_subnet_ids()
        subnets = []
        for sn in ec2.describe_subnets().get("Subnets", []):
            sid = sn["SubnetId"]
            subnets.append({"subnet": sid, "vpc": sn.get("VpcId"), "cidr": sn.get("CidrBlock"),
                            "az": sn.get("AvailabilityZone"),
                            "placement": "public" if sid in public_ids else "private",
                            "auto_public_ip": sn.get("MapPublicIpOnLaunch")})
        sgs = []
        for sg in ec2.describe_security_groups().get("SecurityGroups", []):
            opened = _open_ingress(sg.get("IpPermissions", []))
            if opened:
                sgs.append({"group": sg["GroupId"], "name": sg.get("GroupName"),
                            "vpc": sg.get("VpcId"), "open_ingress": opened})
        vpcs = [{"vpc": v["VpcId"], "cidr": v.get("CidrBlock"), "default": v.get("IsDefault")}
                for v in ec2.describe_vpcs().get("Vpcs", [])]
        igws = [g["InternetGatewayId"] for g in ec2.describe_internet_gateways().get("InternetGateways", [])]
        nats = [{"id": n["NatGatewayId"], "subnet": n.get("SubnetId"), "state": n.get("State")}
                for n in ec2.describe_nat_gateways().get("NatGateways", [])]
        return _safe({"vpcs": vpcs, "subnets": subnets,
                      "internet_gateways": igws, "nat_gateways": nats,
                      "security_groups_open_to_internet": sgs})
    except Exception as e:
        log.exception("describe_network failed")
        return f"(error describing network: {e})"


@tool
def describe_ec2_instances(name_or_id_contains: str = "") -> str:
    """EC2 instances with their public-exposure posture: subnet placement
    (public/private), public IP, open security-group ingress, IAM profile. Use
    for "what happens if I open an EC2 to a public subnet?" / exposure review.

    Args:
        name_or_id_contains: Optional substring to filter by instance id or Name tag.
    """
    try:
        public_ids = _public_subnet_ids()
        # Pre-index SG open-ingress so we can flag per instance.
        sg_open: dict[str, list] = {}
        for sg in ec2.describe_security_groups().get("SecurityGroups", []):
            o = _open_ingress(sg.get("IpPermissions", []))
            if o:
                sg_open[sg["GroupId"]] = o
        out = []
        for res in ec2.describe_instances().get("Reservations", []):
            for i in res.get("Instances", []):
                iid = i["InstanceId"]
                name = next((t["Value"] for t in i.get("Tags", []) if t["Key"] == "Name"), "")
                if name_or_id_contains and name_or_id_contains.lower() not in (iid + name).lower():
                    continue
                subnet = i.get("SubnetId", "")
                sgs = [g["GroupId"] for g in i.get("SecurityGroups", [])]
                open_rules = {g: sg_open[g] for g in sgs if g in sg_open}
                in_public = subnet in public_ids
                has_public_ip = bool(i.get("PublicIpAddress"))
                out.append({
                    "id": iid, "name": name, "type": i.get("InstanceType"),
                    "state": i.get("State", {}).get("Name"), "vpc": i.get("VpcId"),
                    "subnet": subnet, "subnet_placement": "public" if in_public else "private",
                    "private_ip": i.get("PrivateIpAddress"),
                    "public_ip": i.get("PublicIpAddress") or None,
                    "security_groups": sgs, "open_ingress": open_rules or None,
                    "iam_instance_profile": (i.get("IamInstanceProfile") or {}).get("Arn"),
                    # DIRECTLY reachable = public subnet + public IP + an open SG.
                    # Does not account for ALB/NLB-fronted exposure (instance with
                    # no public IP behind an internet-facing LB may still be reachable).
                    "directly_internet_reachable": bool(in_public and has_public_ip and open_rules),
                })
        if not out:
            return "No EC2 instances found (or none matched the filter)."
        return _safe({"count": len(out), "instances": out})
    except Exception as e:
        log.exception("describe_ec2_instances failed")
        return f"(error describing EC2 instances: {e})"


@tool
def describe_load_balancers() -> str:
    """Load balancers (ELBv2): scheme (internet-facing vs internal), listeners,
    target groups, security groups, and subnets.
    """
    try:
        out = []
        lbs = elbv2.describe_load_balancers().get("LoadBalancers", [])
        for lb in lbs:
            arn = lb["LoadBalancerArn"]
            try:
                listeners = [{"port": l.get("Port"), "protocol": l.get("Protocol")}
                             for l in elbv2.describe_listeners(LoadBalancerArn=arn).get("Listeners", [])]
            except Exception:
                listeners = []
            out.append({
                "name": lb.get("LoadBalancerName"), "type": lb.get("Type"),
                "scheme": lb.get("Scheme"),  # internet-facing | internal
                "dns": lb.get("DNSName"), "vpc": lb.get("VpcId"),
                "state": lb.get("State", {}).get("Code"),
                "security_groups": lb.get("SecurityGroups", []),
                "subnets": [z.get("SubnetId") for z in lb.get("AvailabilityZones", [])],
                "listeners": listeners,
            })
        if not out:
            return "No load balancers found."
        return _safe({"count": len(out), "load_balancers": out})
    except Exception as e:
        log.exception("describe_load_balancers failed")
        return f"(error describing load balancers: {e})"


@tool
def describe_lambdas(name_contains: str = "") -> str:
    """Lambda functions: runtime, handler, role, memory/timeout, VPC config, and
    environment-variable KEYS. Env-var VALUES are redacted at the source — this
    tool never returns secret values.

    Args:
        name_contains: Optional substring to filter function names.
    """
    try:
        out = []
        paginator = lambda_client.get_paginator("list_functions")
        for page in paginator.paginate():
            for fn in page.get("Functions", []):
                name = fn.get("FunctionName", "")
                if name_contains and name_contains.lower() not in name.lower():
                    continue
                env = (fn.get("Environment") or {}).get("Variables") or {}
                # Show env-var KEYS only — values never leave the boundary.
                env_keys = sorted(env.keys())
                vpc = fn.get("VpcConfig") or {}
                out.append({
                    "name": name, "runtime": fn.get("Runtime"),
                    "handler": fn.get("Handler"), "role": fn.get("Role"),
                    "memory": fn.get("MemorySize"), "timeout": fn.get("Timeout"),
                    "last_modified": fn.get("LastModified"),
                    "env_var_keys": env_keys,
                    "vpc": {"subnets": vpc.get("SubnetIds", []),
                            "security_groups": vpc.get("SecurityGroupIds", [])} if vpc.get("VpcId") else None,
                })
        if not out:
            return "No Lambda functions found (or none matched the filter)."
        return _safe({"count": len(out), "functions": out})
    except Exception as e:
        log.exception("describe_lambdas failed")
        return f"(error describing Lambdas: {e})"


@tool
def describe_s3_buckets(name_contains: str = "") -> str:
    """S3 buckets with their security posture: region, public-access-block,
    default encryption, versioning, policy-status (public or not).

    Args:
        name_contains: Optional substring to filter bucket names.
    """
    try:
        buckets = s3.list_buckets().get("Buckets", [])
        out = []
        for b in buckets:
            name = b["Name"]
            if name_contains and name_contains.lower() not in name.lower():
                continue
            info: dict[str, Any] = {"name": name}
            try:
                pab = s3.get_public_access_block(Bucket=name)["PublicAccessBlockConfiguration"]
                info["public_access_block"] = pab
            except Exception:
                info["public_access_block"] = "none (not configured)"
            try:
                enc = s3.get_bucket_encryption(Bucket=name)["ServerSideEncryptionConfiguration"]["Rules"]
                info["encryption"] = [r.get("ApplyServerSideEncryptionByDefault", {}).get("SSEAlgorithm") for r in enc]
            except Exception:
                info["encryption"] = "none"
            try:
                info["versioning"] = s3.get_bucket_versioning(Bucket=name).get("Status", "Disabled")
            except Exception:
                info["versioning"] = "unknown"
            try:
                info["public"] = s3.get_bucket_policy_status(Bucket=name)["PolicyStatus"]["IsPublic"]
            except Exception:
                info["public"] = False
            out.append(info)
        if not out:
            return "No S3 buckets found (or none matched the filter)."
        return _safe({"count": len(out), "buckets": out})
    except Exception as e:
        log.exception("describe_s3_buckets failed")
        return f"(error describing S3 buckets: {e})"


@tool
def describe_ecr_repositories() -> str:
    """ECR repositories: URI, scan-on-push, tag mutability, encryption, and
    lifecycle-policy presence. Does not read image contents.
    """
    try:
        repos = ecr.describe_repositories().get("repositories", [])
        out = []
        for r in repos:
            name = r["repositoryName"]
            try:
                has_lifecycle = bool(ecr.get_lifecycle_policy(repositoryName=name).get("lifecyclePolicyText"))
            except Exception:
                has_lifecycle = False
            out.append({
                "name": name, "uri": r.get("repositoryUri"),
                "scan_on_push": (r.get("imageScanningConfiguration") or {}).get("scanOnPush"),
                "tag_mutability": r.get("imageTagMutability"),
                "encryption": (r.get("encryptionConfiguration") or {}).get("encryptionType"),
                "lifecycle_policy": has_lifecycle,
            })
        if not out:
            return "No ECR repositories found."
        return _safe({"count": len(out), "repositories": out})
    except Exception as e:
        log.exception("describe_ecr_repositories failed")
        return f"(error describing ECR repositories: {e})"


def _glue_targets(targets: dict) -> list[dict]:
    """Flatten a crawler's target spec to a readable list."""
    out: list[dict] = []
    for t in targets.get("S3Targets", []):
        out.append({"type": "s3", "path": t.get("Path")})
    for t in targets.get("JdbcTargets", []):
        out.append({"type": "jdbc", "connection": t.get("ConnectionName"), "path": t.get("Path")})
    for t in targets.get("CatalogTargets", []):
        out.append({"type": "catalog", "database": t.get("DatabaseName"),
                    "tables": t.get("Tables", [])})
    for t in targets.get("DynamoDBTargets", []):
        out.append({"type": "dynamodb", "path": t.get("Path")})
    return out


@tool
def describe_glue(name_contains: str = "") -> str:
    """Glue crawlers and Data Catalog databases: crawler state, target, schedule,
    role, and last-crawl status. AWS Config does NOT record Glue crawlers, so this
    is the source for them (e.g. the structured-ingestion crawler).

    Args:
        name_contains: Optional substring to filter crawler / database names.
    """
    try:
        crawlers = []
        for page in glue.get_paginator("get_crawlers").paginate():
            for c in page.get("Crawlers", []):
                name = c.get("Name", "")
                if name_contains and name_contains.lower() not in name.lower():
                    continue
                last = c.get("LastCrawl") or {}
                crawlers.append({
                    "name": name, "state": c.get("State"),
                    "database": c.get("DatabaseName"),
                    "role": c.get("Role"),
                    "schedule": (c.get("Schedule") or {}).get("ScheduleExpression"),
                    "targets": _glue_targets(c.get("Targets") or {}),
                    "last_crawl": {"status": last.get("Status"),
                                   "start_time": last.get("StartTime"),
                                   "error": last.get("ErrorMessage")},
                })
        databases = []
        try:
            for page in glue.get_paginator("get_databases").paginate():
                for d in page.get("DatabaseList", []):
                    dn = d.get("Name", "")
                    if name_contains and name_contains.lower() not in dn.lower():
                        continue
                    databases.append({"name": dn, "location": d.get("LocationUri")})
        except Exception:
            pass
        if not crawlers and not databases:
            return "No Glue crawlers or databases found (or none matched the filter)."
        return _safe({"crawler_count": len(crawlers), "crawlers": crawlers,
                      "database_count": len(databases), "databases": databases})
    except Exception as e:
        log.exception("describe_glue failed")
        return f"(error describing Glue: {e})"


@tool
def describe_dynamodb_tables(name_contains: str = "") -> str:
    """DynamoDB tables — METADATA only (never reads item data): key schema,
    billing mode, item count, size, GSIs/LSIs, encryption (SSE/KMS), streams,
    point-in-time recovery, and status. Use for "what DynamoDB tables exist?" /
    "how is table X configured / is it encrypted / backed up?".

    Args:
        name_contains: Optional substring to filter table names.
    """
    try:
        names: list[str] = []
        for page in dynamodb.get_paginator("list_tables").paginate():
            names.extend(page.get("TableNames", []))
        if name_contains:
            names = [n for n in names if name_contains.lower() in n.lower()]
        if not names:
            return "No DynamoDB tables found (or none matched the filter)."
        out = []
        for n in names[:50]:
            try:
                t = dynamodb.describe_table(TableName=n)["Table"]
            except Exception as e:
                out.append({"name": n, "error": str(e)})
                continue
            sse = t.get("SSEDescription") or {}
            prov = t.get("ProvisionedThroughput") or {}
            info: dict[str, Any] = {
                "name": n, "status": t.get("TableStatus"),
                "item_count": t.get("ItemCount"), "size_bytes": t.get("TableSizeBytes"),
                "billing_mode": (t.get("BillingModeSummary") or {}).get("BillingMode")
                                or ("PROVISIONED" if prov.get("ReadCapacityUnits") else None),
                "key_schema": [{"attr": k["AttributeName"], "key": k["KeyType"]} for k in t.get("KeySchema", [])],
                "global_secondary_indexes": [g["IndexName"] for g in t.get("GlobalSecondaryIndexes", [])],
                "local_secondary_indexes": [l["IndexName"] for l in t.get("LocalSecondaryIndexes", [])],
                "stream": (t.get("StreamSpecification") or {}).get("StreamViewType")
                          if (t.get("StreamSpecification") or {}).get("StreamEnabled") else None,
                "encryption": ({"type": sse.get("SSEType"), "kms_key": sse.get("KMSMasterKeyArn")}
                               if sse else "DEFAULT (AWS-owned key)"),
                "deletion_protection": t.get("DeletionProtectionEnabled"),
            }
            # Point-in-time recovery is a separate read-only describe call.
            try:
                pitr = dynamodb.describe_continuous_backups(TableName=n)
                info["pitr"] = (pitr.get("ContinuousBackupsDescription", {})
                                .get("PointInTimeRecoveryDescription", {})
                                .get("PointInTimeRecoveryStatus"))
            except Exception:
                pass
            out.append(info)
        result: dict[str, Any] = {"count": len(out), "tables": out}
        if len(names) > 50:
            result["note"] = f"{len(names)} tables total; showing first 50 (narrow with name_contains)."
        return _safe(result)
    except Exception as e:
        log.exception("describe_dynamodb_tables failed")
        return f"(error describing DynamoDB tables: {e})"


@tool
def describe_cognito(user_pool_id: str = "") -> str:
    """Cognito user pools and their app clients (client SECRETS are redacted at
    the source). With a pool id, returns that pool plus its clients and an
    estimated-consumers note for "what happens if I remove this pool?".

    Args:
        user_pool_id: Optional user-pool id (e.g. 'us-east-1_abc123').
    """
    try:
        if not user_pool_id:
            pools = cognito.list_user_pools(MaxResults=50).get("UserPools", [])
            return _safe({"count": len(pools),
                          "user_pools": [{"id": p["Id"], "name": p.get("Name")} for p in pools]})
        pool = cognito.describe_user_pool(UserPoolId=user_pool_id).get("UserPool", {})
        clients = cognito.list_user_pool_clients(UserPoolId=user_pool_id, MaxResults=50).get("UserPoolClients", [])
        client_details = []
        for c in clients:
            d = cognito.describe_user_pool_client(
                UserPoolId=user_pool_id, ClientId=c["ClientId"]).get("UserPoolClient", {})
            # ClientSecret is redacted by _scrub (sensitive key); also drop callback noise.
            client_details.append({
                "client_id": d.get("ClientId"), "name": d.get("ClientName"),
                # OAuth confidential (has a client secret) vs public client. Boolean
                # only — the secret value itself is never read out (and _scrub would
                # redact it anyway). Named without "secret" so the flag survives scrub.
                "confidential_client": bool(d.get("ClientSecret")),
                "callback_urls": d.get("CallbackURLs", []),
                "allowed_flows": d.get("AllowedOAuthFlows", []),
                "explicit_auth_flows": d.get("ExplicitAuthFlows", []),
            })
        return _safe({
            "user_pool": {"id": pool.get("Id"), "name": pool.get("Name"),
                          "mfa": pool.get("MfaConfiguration"),
                          "estimated_users": pool.get("EstimatedNumberOfUsers"),
                          "domain": pool.get("Domain")},
            "app_clients": client_details,
            "impact_note": ("Removing a user pool invalidates every app client above and breaks "
                            "any API Gateway authorizer or application that authenticates against "
                            "it; all users in the pool lose access. Check API Gateway authorizers "
                            "and app env vars referencing this pool id before removal."),
        })
    except Exception as e:
        log.exception("describe_cognito failed")
        return f"(error describing Cognito: {e})"


# ──────────────────────────── existing Config-rule tools ─────────────────
@tool
def list_config_rules(name_contains: str = "") -> str:
    """List AWS Config rules, optionally filtered by name substring.

    Args:
        name_contains: Case-insensitive substring to filter rule names. Empty = all rules.
    """
    try:
        paginator = config.get_paginator("describe_config_rules")
        rules = []
        for page in paginator.paginate():
            for r in page.get("ConfigRules", []):
                name = r.get("ConfigRuleName", "")
                if name_contains and name_contains.lower() not in name.lower():
                    continue
                rules.append({
                    "name": name,
                    "description": (r.get("Description") or "")[:200],
                    "state": r.get("ConfigRuleState"),
                    "scope": r.get("Scope", {}).get("ComplianceResourceTypes", []),
                })
        if not rules:
            return "No matching AWS Config rules found."
        body = "\n".join(f"- {r['name']} [{r['state']}]: {r['description']}" for r in rules[:50])
        if len(rules) > 50:
            body += f"\n… ({len(rules) - 50} more not shown; narrow with name_contains)"
        return _safe_text(body)
    except Exception as e:
        log.exception("list_config_rules failed")
        return f"(error listing config rules: {e})"


@tool
def get_rule_compliance(rule_name: str) -> str:
    """Return compliance summary for a specific AWS Config rule.

    Args:
        rule_name: Exact name of the Config rule.
    """
    try:
        resp = config.get_compliance_details_by_config_rule(
            ConfigRuleName=rule_name,
            ComplianceTypes=["COMPLIANT", "NON_COMPLIANT", "NOT_APPLICABLE"],
            Limit=100,
        )
        details = resp.get("EvaluationResults", [])
        if not details:
            return f"No compliance evaluations for rule '{rule_name}'."

        by_status: dict[str, list[str]] = {}
        for d in details:
            status = d.get("ComplianceType", "UNKNOWN")
            qualifier = d.get("EvaluationResultIdentifier", {}).get("EvaluationResultQualifier", {})
            resource = f"{qualifier.get('ResourceType', '?')}/{qualifier.get('ResourceId', '?')}"
            by_status.setdefault(status, []).append(resource)

        out_lines = [f"Compliance for '{rule_name}':"]
        for status, resources in by_status.items():
            out_lines.append(f"  {status}: {len(resources)}")
            for r in resources[:10]:
                out_lines.append(f"    - {r}")
            if len(resources) > 10:
                out_lines.append(f"    … ({len(resources) - 10} more)")
        return _safe_text("\n".join(out_lines))
    except Exception as e:
        log.exception("get_rule_compliance failed")
        return f"(error getting compliance for {rule_name}: {e})"


@tool
def list_noncompliant_resources(rule_name: str) -> str:
    """List the resources currently failing a given Config rule.

    Args:
        rule_name: Exact name of the Config rule.
    """
    try:
        resp = config.get_compliance_details_by_config_rule(
            ConfigRuleName=rule_name,
            ComplianceTypes=["NON_COMPLIANT"],
            Limit=100,
        )
        results = resp.get("EvaluationResults", [])
        if not results:
            return f"No non-compliant resources for rule '{rule_name}'."
        lines = []
        for r in results:
            q = r.get("EvaluationResultIdentifier", {}).get("EvaluationResultQualifier", {})
            lines.append(f"- {q.get('ResourceType', '?')}/{q.get('ResourceId', '?')} (annotation: {r.get('Annotation', 'n/a')})")
        if len(results) >= 100:
            lines.append("… (list capped at 100; more may be non-compliant)")
        return _safe_text("\n".join(lines))
    except Exception as e:
        log.exception("list_noncompliant_resources failed")
        return f"(error: {e})"


@tool
def retrieve_awsconfig_docs(query: str, max_results: int = 5) -> str:
    """Retrieve AWS Config conformance-pack docs / historical compliance snapshots from the KB.

    Use this for control rationale, NIST/CIS mappings, and prior-period
    compliance reports that aren't available through the live Config API.

    Args:
        query: Natural-language search query.
        max_results: How many chunks to return (1-10).
    """
    if not KB_ID:
        return "(KB_ID not configured)"

    retrieval_config: dict[str, Any] = {
        "vectorSearchConfiguration": {"numberOfResults": min(max(max_results, 1), 10)}
    }

    try:
        resp = kb_runtime.retrieve(
            knowledgeBaseId=KB_ID,
            retrievalQuery={"text": query},
            retrievalConfiguration=retrieval_config,
        )
    except Exception as e:
        log.exception("KB retrieve failed")
        return f"(retrieval error: {e})"

    chunks = []
    for i, item in enumerate(resp.get("retrievalResults", []), 1):
        text = item.get("content", {}).get("text", "")
        src = item.get("location", {}).get("s3Location", {}).get("uri", "unknown")
        score = item.get("score", 0)
        chunks.append(f"[{i}] (score={score:.3f}, src={src})\n{text}")

    # KB chunks are free text from ingested docs — scrub before returning so a
    # credential accidentally present in a source doc never reaches the model.
    return _safe_text("\n\n---\n\n".join(chunks)) if chunks else "No matching AWS Config documents found."


def build_agent() -> Agent:
    model_kwargs: dict[str, Any] = {"model_id": MODEL_ID, "region_name": REGION}
    if GUARDRAIL_ID:
        model_kwargs["guardrail_id"] = GUARDRAIL_ID
        model_kwargs["guardrail_version"] = GUARDRAIL_VERSION
    return Agent(
        model=BedrockModel(**model_kwargs),
        system_prompt=SYSTEM_PROMPT,
        tools=[
            # account context
            account_identity,
            # inventory + impact
            list_resources,
            get_resource_relationships,
            # posture / networking / exposure
            describe_network,
            describe_ec2_instances,
            describe_load_balancers,
            describe_lambdas,
            describe_s3_buckets,
            describe_ecr_repositories,
            describe_glue,
            describe_dynamodb_tables,
            describe_cognito,
            # compliance + docs
            list_config_rules,
            get_rule_compliance,
            list_noncompliant_resources,
            retrieve_awsconfig_docs,
        ],
    )


@app.entrypoint
def invoke(payload: dict[str, Any]) -> dict[str, Any]:
    prompt = payload.get("prompt") or payload.get("input") or ""
    if not prompt:
        return {"error": "Missing 'prompt'"}
    actor_id   = (payload.get("actor_id")   or "anonymous")[:128]
    persona    = (payload.get("persona")    or "employee")[:16]
    session_id = (payload.get("session_id") or "adhoc")[:128]
    chat_type  = (payload.get("chat_type")  or "analyst")[:16]
    user_email = (payload.get("user_email") or "")[:200]
    log.info("AWS resource/posture specialist: persona=%s session=%s prompt=%s",
             persona, session_id, prompt[:200])
    agent = build_agent()
    agent_result = agent(prompt)
    record_from_agent_result(
        agent_result, agent="awsconfig", persona=persona, actor_id=actor_id,
        session_id=session_id, chat_type=chat_type, model_id=MODEL_ID,
        user_email=user_email,
    )
    return {"result": str(agent_result)}


if __name__ == "__main__":
    app.run()
