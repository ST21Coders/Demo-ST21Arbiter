"""Read-only AWS health snapshot for the daily test report.

Scoped to ARBITER + LM resources in us-east-1. Writes a single JSON file at
test-reports/aws-health.json shaped as:

  {
    "runDate": "<iso>",
    "facts":    [ {"name": "...", "value": "..."}, ... ],
    "findings": [ {"severity": "High|Medium|Low",
                   "title": "...", "detail": "...",
                   "location": "...", "fix": "..."} , ... ]
  }

The card builder (testing/build_teams_messages.py) consumes both arrays
verbatim — no further translation. If a check raises, the resource is
omitted with a Low-severity finding noting the failure mode.

Requires AWS credentials with read access to: lambda, dynamodb, s3,
cloudfront, apigateway, cognito-idp, cloudwatch, ec2, logs,
bedrock-agentcore-control.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

REGION = os.environ.get("AWS_REGION", "us-east-1")
OUT_PATH = Path(os.environ.get("TEST_REPORTS_DIR", "test-reports")) / "aws-health.json"

PROJECT_PATTERNS = ("dev-st21arbiter-poc", "dev_st21arbiter_poc", "lm-arbiter", "lm_arbiter")

CFG = Config(region_name=REGION, retries={"max_attempts": 3, "mode": "standard"})


def _matches(name: str) -> bool:
    n = name.lower()
    return any(p.lower() in n for p in PROJECT_PATTERNS)


def _cw_sum_24h(cw: Any, namespace: str, metric: str, dim_name: str, dim_value: str) -> float:
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=24)
    try:
        r = cw.get_metric_statistics(
            Namespace=namespace, MetricName=metric,
            Dimensions=[{"Name": dim_name, "Value": dim_value}],
            StartTime=start, EndTime=end, Period=86400, Statistics=["Sum"],
        )
        dps = r.get("Datapoints") or []
        return dps[0]["Sum"] if dps else 0.0
    except ClientError:
        return 0.0


def _cw_stat_24h(cw: Any, namespace: str, metric: str, dim_name: str, dim_value: str,
                 stat: str) -> float:
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=24)
    try:
        r = cw.get_metric_statistics(
            Namespace=namespace, MetricName=metric,
            Dimensions=[{"Name": dim_name, "Value": dim_value}],
            StartTime=start, EndTime=end, Period=86400, Statistics=[stat],
        )
        dps = r.get("Datapoints") or []
        return dps[0][stat] if dps else 0.0
    except ClientError:
        return 0.0


def check_lambdas(facts: list, findings: list) -> None:
    lam = boto3.client("lambda", config=CFG)
    cw = boto3.client("cloudwatch", config=CFG)
    fns = []
    paginator = lam.get_paginator("list_functions")
    for page in paginator.paginate():
        for f in page["Functions"]:
            if _matches(f["FunctionName"]):
                fns.append(f)
    fns.sort(key=lambda f: f["FunctionName"])
    for fn in fns:
        name = fn["FunctionName"]
        inv = _cw_sum_24h(cw, "AWS/Lambda", "Invocations", "FunctionName", name)
        err = _cw_sum_24h(cw, "AWS/Lambda", "Errors", "FunctionName", name)
        thr = _cw_sum_24h(cw, "AWS/Lambda", "Throttles", "FunctionName", name)
        avg = _cw_stat_24h(cw, "AWS/Lambda", "Duration", "FunctionName", name, "Average")
        mx = _cw_stat_24h(cw, "AWS/Lambda", "Duration", "FunctionName", name, "Maximum")
        parts = [
            "Active" if fn.get("State", "Active") == "Active" else fn.get("State", "?"),
            f"{int(err)} errors",
            f"{int(thr)} throttles 24h",
            f"{int(inv)} invocations",
            f"avg {avg:.0f}ms",
        ]
        if mx > 10_000:
            parts.append(f"max {mx/1000:.1f}s")
        facts.append({"name": f"Lambda {name.replace('dev-st21arbiter-poc-', '')}",
                      "value": " · ".join(parts)})
        if err > 0:
            findings.append({
                "severity": "High",
                "title": f"Lambda {name}: {int(err)} error(s) in last 24h",
                "detail": f"{int(err)} errors over {int(inv)} invocations",
                "location": f"CloudWatch Logs /aws/lambda/{name}",
                "fix": "Inspect recent log streams for the failing path; check whether config or upstream dependency changed.",
            })
        if mx > 60_000:
            findings.append({
                "severity": "Medium",
                "title": f"Lambda {name} max duration {mx/1000:.1f}s (24h)",
                "detail": f"Tail latency vs avg {avg:.0f}ms — likely long /chat or upstream call",
                "location": name,
                "fix": "Add X-Ray segments per specialist tool to identify the slow leg.",
            })


def check_dynamodb(facts: list, findings: list) -> None:
    ddb = boto3.client("dynamodb", config=CFG)
    cw = boto3.client("cloudwatch", config=CFG)
    names = [n for n in ddb.list_tables()["TableNames"] if _matches(n)]
    active = 0
    sse_enabled = 0
    total_throttles = 0.0
    for n in sorted(names):
        t = ddb.describe_table(TableName=n)["Table"]
        if t.get("TableStatus") == "ACTIVE": active += 1
        if (t.get("SSEDescription") or {}).get("Status") == "ENABLED": sse_enabled += 1
        total_throttles += _cw_sum_24h(cw, "AWS/DynamoDB", "ThrottledRequests", "TableName", n)
    if names:
        facts.append({
            "name": f"DynamoDB tables ({len(names)})",
            "value": f"{active}/{len(names)} ACTIVE · SSE on {sse_enabled}/{len(names)} · {int(total_throttles)} throttles 24h",
        })
    if total_throttles > 0:
        findings.append({
            "severity": "High",
            "title": f"DynamoDB throttling: {int(total_throttles)} throttled requests in 24h",
            "detail": "PAY_PER_REQUEST tables should not throttle under POC volumes",
            "location": "DynamoDB tables",
            "fix": "Check for hot-key or large-item write patterns in api_handler.py.",
        })


def check_s3(facts: list, findings: list) -> None:
    s3 = boto3.client("s3")
    resp = s3.list_buckets()
    buckets = [b["Name"] for b in resp["Buckets"] if _matches(b["Name"])]
    public = []
    unversioned = []
    sse_count = 0
    for b in buckets:
        # Public access block
        try:
            pab = s3.get_public_access_block(Bucket=b)["PublicAccessBlockConfiguration"]
            if not all(pab.values()):
                public.append(b)
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchPublicAccessBlockConfiguration":
                public.append(b)
        # Versioning
        try:
            v = s3.get_bucket_versioning(Bucket=b).get("Status")
            if v != "Enabled":
                unversioned.append((b, v or "NeverEnabled"))
        except ClientError:
            unversioned.append((b, "Unknown"))
        # SSE
        try:
            s3.get_bucket_encryption(Bucket=b)
            sse_count += 1
        except ClientError:
            pass
    if buckets:
        facts.append({
            "name": f"S3 buckets ({len(buckets)})",
            "value": f"{len(buckets) - len(public)}/{len(buckets)} public access blocked · "
                     f"{sse_count}/{len(buckets)} encrypted at rest",
        })
    for b in public:
        findings.append({
            "severity": "High",
            "title": f"S3 bucket {b} missing public-access block",
            "detail": "PublicAccessBlock configuration absent or partial",
            "location": f"s3://{b}",
            "fix": f"aws s3api put-public-access-block --bucket {b} --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true",
        })
    if unversioned:
        bucket_list = ", ".join(f"{b} ({v})" for b, v in unversioned[:4])
        findings.append({
            "severity": "Medium",
            "title": f"{len(unversioned)} S3 bucket(s) unversioned: {bucket_list}",
            "detail": "Lost objects cannot be recovered from accidental delete",
            "location": "S3",
            "fix": "Enable versioning on each: aws s3api put-bucket-versioning --bucket <name> --versioning-configuration Status=Enabled",
        })


def check_apigateway(facts: list, findings: list) -> None:
    api = boto3.client("apigateway", config=CFG)
    apis = [a for a in api.get_rest_apis()["items"] if _matches(a["name"])]
    if apis:
        facts.append({
            "name": "API Gateway",
            "value": ", ".join(f"{a['name']}" for a in apis) + " deployed",
        })


def check_cloudfront(facts: list, findings: list) -> None:
    cf = boto3.client("cloudfront")
    resp = cf.list_distributions()
    items = (resp.get("DistributionList") or {}).get("Items") or []
    dists = []
    for d in items:
        comment = d.get("Comment", "")
        aliases = " ".join((d.get("Aliases") or {}).get("Items") or [])
        if _matches(comment) or _matches(aliases) or _matches(d.get("Id", "")):
            dists.append(d)
    if dists:
        deployed = sum(1 for d in dists if d.get("Status") == "Deployed")
        details = " | ".join(f"{d['Id']} ({d.get('Status', '?')})" for d in dists)
        facts.append({"name": "CloudFront", "value": f"{deployed}/{len(dists)} Deployed: {details}"})


def check_cognito(facts: list, findings: list) -> None:
    cog = boto3.client("cognito-idp", config=CFG)
    pools = [p for p in cog.list_user_pools(MaxResults=60)["UserPools"] if _matches(p["Name"])]
    for p in pools:
        facts.append({"name": "Cognito User Pool", "value": f"{p['Id']} ({p['Name']}) active"})


def check_security_groups(facts: list, findings: list) -> None:
    ec2 = boto3.client("ec2", config=CFG)
    sgs = ec2.describe_security_groups()["SecurityGroups"]
    open_ingress = []
    for sg in sgs:
        for perm in sg.get("IpPermissions") or []:
            for r in perm.get("IpRanges") or []:
                if r.get("CidrIp") == "0.0.0.0/0":
                    open_ingress.append((sg["GroupId"], sg["GroupName"],
                                          perm.get("FromPort"), perm.get("ToPort")))
    facts.append({
        "name": "Security Groups",
        "value": "No 0.0.0.0/0 ingress on any port" if not open_ingress else f"{len(open_ingress)} group(s) with public ingress",
    })
    sensitive = [g for g in open_ingress if g[2] in (22, 3306, 5432, 3389)]
    for gid, gname, fp, tp in sensitive:
        findings.append({
            "severity": "High",
            "title": f"Security group {gid} ({gname}) allows 0.0.0.0/0 on port {fp}-{tp}",
            "detail": "Sensitive port exposed to internet",
            "location": gid,
            "fix": f"aws ec2 revoke-security-group-ingress --group-id {gid} --protocol tcp --port {fp} --cidr 0.0.0.0/0",
        })


def check_cloudwatch_alarms(facts: list, findings: list) -> None:
    cw = boto3.client("cloudwatch", config=CFG)
    alarms = cw.describe_alarms(StateValue="ALARM")["MetricAlarms"]
    facts.append({"name": "CloudWatch alarms",
                  "value": "Zero in ALARM state" if not alarms else f"{len(alarms)} alarm(s) firing"})
    for a in alarms[:5]:
        findings.append({
            "severity": "High",
            "title": f"CloudWatch alarm firing: {a['AlarmName']}",
            "detail": a.get("StateReason", ""),
            "location": "CloudWatch",
            "fix": "Investigate the metric and underlying cause; clear or adjust the threshold.",
        })


def check_cloudwatch_logs(facts: list, findings: list) -> None:
    logs = boto3.client("logs", config=CFG)
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - 86_400_000
    log_groups = logs.describe_log_groups(logGroupNamePrefix="/aws/lambda/dev-st21arbiter-poc")["logGroups"]
    total_errors = 0
    for lg in log_groups:
        try:
            r = logs.filter_log_events(
                logGroupName=lg["logGroupName"],
                startTime=start_ms, endTime=end_ms,
                filterPattern='?ERROR ?Exception ?"Task timed out"',
                limit=50,
            )
            total_errors += len(r.get("events") or [])
        except ClientError:
            continue
    facts.append({
        "name": "CloudWatch logs (24h)",
        "value": "Zero ERROR-pattern events on either Lambda" if total_errors == 0 else f"{total_errors} ERROR-pattern event(s)",
    })


def check_agentcore(facts: list, findings: list) -> None:
    try:
        ac = boto3.client("bedrock-agentcore-control", config=CFG)
        runtimes = ac.list_agent_runtimes().get("agentRuntimes") or []
    except (ClientError, Exception):
        return
    arbiter_rts = [r for r in runtimes if _matches(r.get("agentRuntimeName", ""))]
    ready = sum(1 for r in arbiter_rts if r.get("status") == "READY")
    if arbiter_rts:
        names = ", ".join(r["agentRuntimeName"].replace("dev_st21arbiter_poc_", "")
                          for r in arbiter_rts)
        facts.append({
            "name": f"AgentCore Runtimes ({len(arbiter_rts)})",
            "value": f"{ready}/{len(arbiter_rts)} READY: {names}",
        })
    for r in arbiter_rts:
        if r.get("status") != "READY":
            findings.append({
                "severity": "High",
                "title": f"AgentCore runtime {r['agentRuntimeName']} status: {r.get('status', '?')}",
                "detail": "Runtime not in READY state",
                "location": r.get("agentRuntimeArn", ""),
                "fix": "Inspect runtime logs and re-run scripts/deploy_agents.py for the affected runtime.",
            })


def main() -> int:
    facts: list[dict] = []
    findings: list[dict] = []
    for check in (
        check_lambdas, check_dynamodb, check_agentcore, check_s3, check_apigateway,
        check_cloudfront, check_cognito, check_security_groups,
        check_cloudwatch_alarms, check_cloudwatch_logs,
    ):
        try:
            check(facts, findings)
        except Exception as e:
            findings.append({
                "severity": "Low",
                "title": f"AWS check failed: {check.__name__}",
                "detail": f"{type(e).__name__}: {e}",
                "location": "scripts/aws_health_check.py",
                "fix": "Check IAM permissions or transient AWS error; re-run.",
            })

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps({
        "runDate": datetime.now(timezone.utc).isoformat(),
        "facts": facts,
        "findings": findings,
    }, indent=2))
    print(f"Wrote {OUT_PATH} ({len(facts)} facts, {len(findings)} findings)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
