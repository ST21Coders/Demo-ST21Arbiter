#!/usr/bin/env python3
"""Seed a ServiceNow (PDI) CMDB with the ARBITER demo fixtures.

Plane-1 stand-in for the build-now milestone: instead of running the Service
Graph Connector for AWS, this hand-loads a small, relationship-rich CMDB on a
ServiceNow Personal Developer Instance so the servicenow_specialist's
impact-analysis workflow is demonstrable end to end. The CI set + ownership
mirror the master orchestrator's _seed_awsconfig_observations() fixtures and the
api_handler TEAM_ROUTING groups, so the same resource ids resolve.

The contract surface (cmdb_ci / cmdb_rel_ci / sys_user_group / change_request)
is identical to what SGC-for-AWS produces, so this can be swapped for real
ingestion later with zero Plane-2 code change.

Credentials (precedence): env vars, else Secrets Manager dev/<project>/servicenow.
  SN_INSTANCE_URL   e.g. https://dev123456.service-now.com
  SN_USERNAME / SN_PASSWORD          (basic auth), or
  SN_CLIENT_ID / SN_CLIENT_SECRET    (OAuth2 client-credentials)

Usage:
  source scripts/.venv/bin/activate
  SN_INSTANCE_URL=https://devNNNNN.service-now.com SN_USERNAME=admin SN_PASSWORD=... \
    python3 scripts/seed_servicenow_cmdb.py
  # or, reading the same secret the agent uses:
  PROJECT=st21arbiter-poc python3 scripts/seed_servicenow_cmdb.py --from-secret
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("seed_servicenow_cmdb")

ENV = os.environ.get("ENVIRONMENT", "dev")
PROJECT = os.environ.get("PROJECT", "st21arbiter-poc")
REGION = os.environ.get("AWS_REGION", "us-east-1")
ACCOUNT = os.environ.get("AWS_ACCOUNT_ID", "669810405473")
HTTP_TIMEOUT = 30

# Owning teams → ServiceNow group display names. Keys mirror api_handler TEAM_ROUTING.
GROUPS = {
    "platform-security": "Platform Security",
    "network-eng":       "Network Engineering",
    "cloud-infra":       "Cloud Infrastructure",
    "data-governance":   "Data Governance",
    "app-dev":           "Application Development",
    "vendor-mgmt":       "Vendor Management",
}

# CIs: (name, sys_class_name, owning-team-key, synthetic ARN for correlation_id).
# Names match _seed_awsconfig_observations() resource ids so a user can query by
# either the resource id (name match) or the ARN (correlation_id match).
CIS = [
    ("Claims API",                    "cmdb_ci_appl",        "app-dev",         f"arn:aws:application:{REGION}:{ACCOUNT}:app/claims-api"),
    ("alb-mig-prod-claims-api-001",   "cmdb_ci_lb",          "cloud-infra",     f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:loadbalancer/app/alb-mig-prod-claims-api-001"),
    ("mig-prod-claims-data-primary",  "cmdb_ci_db_instance", "data-governance", f"arn:aws:rds:{REGION}:{ACCOUNT}:db:mig-prod-claims-data-primary"),
    ("pcx-mig-prod-dev-001",          "cmdb_ci_network",     "network-eng",     f"arn:aws:ec2:{REGION}:{ACCOUNT}:vpc-peering-connection/pcx-mig-prod-dev-001"),
    ("vpc-mig-prod-001",              "cmdb_ci_network",     "network-eng",     f"arn:aws:ec2:{REGION}:{ACCOUNT}:vpc/vpc-mig-prod-001"),
    ("vpc-mig-dev-002",               "cmdb_ci_network",     "network-eng",     f"arn:aws:ec2:{REGION}:{ACCOUNT}:vpc/vpc-mig-dev-002"),
    # Compliant guard resources from the fixtures (so the CMDB isn't all-prod-broken).
    ("alb-mig-prod-api-002",          "cmdb_ci_lb",          "cloud-infra",     f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:loadbalancer/app/alb-mig-prod-api-002"),
    ("mig-prod-customer-data-secondary", "cmdb_ci_db_instance", "data-governance", f"arn:aws:rds:{REGION}:{ACCOUNT}:db:mig-prod-customer-data-secondary"),
]

# Relationships: (parent_name, type_display, child_name). The "Depends on::Used
# by" type makes parent depend on child; the specialist's traversal surfaces
# both directions, so changing a child reports its dependent parents as affected.
RELS = [
    ("Claims API", "Depends on::Used by", "alb-mig-prod-claims-api-001"),
    ("Claims API", "Depends on::Used by", "mig-prod-claims-data-primary"),
    ("pcx-mig-prod-dev-001", "Connects to::Connected by", "vpc-mig-prod-001"),
    ("pcx-mig-prod-dev-001", "Connects to::Connected by", "vpc-mig-dev-002"),
]


def _load_creds(from_secret: bool) -> dict[str, str]:
    """Env vars win; --from-secret reads dev/<project>/servicenow."""
    if not from_secret and os.environ.get("SN_INSTANCE_URL"):
        return {
            "instance_url": os.environ["SN_INSTANCE_URL"],
            "username": os.environ.get("SN_USERNAME", ""),
            "password": os.environ.get("SN_PASSWORD", ""),
            "client_id": os.environ.get("SN_CLIENT_ID", ""),
            "client_secret": os.environ.get("SN_CLIENT_SECRET", ""),
        }
    import boto3
    sm = boto3.client("secretsmanager", region_name=REGION)
    sid = f"{ENV}/{PROJECT}/servicenow"
    log.info("Loading ServiceNow creds from secret %s", sid)
    return json.loads(sm.get_secret_value(SecretId=sid)["SecretString"])


class SN:
    def __init__(self, creds: dict[str, str]):
        self.base = creds["instance_url"].rstrip("/")
        self.s = requests.Session()
        self.s.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
        if creds.get("client_id") and creds.get("client_secret"):
            tok = requests.post(f"{self.base}/oauth_token.do", data={
                "grant_type": "client_credentials",
                "client_id": creds["client_id"], "client_secret": creds["client_secret"],
            }, timeout=HTTP_TIMEOUT)
            tok.raise_for_status()
            self.s.headers["Authorization"] = f"Bearer {tok.json()['access_token']}"
        else:
            self.s.auth = (creds.get("username", ""), creds.get("password", ""))

    def find(self, table: str, query: str) -> str | None:
        r = self.s.get(f"{self.base}/api/now/table/{table}",
                       params={"sysparm_query": query, "sysparm_fields": "sys_id",
                               "sysparm_limit": "1"}, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        rows = r.json().get("result", [])
        return rows[0]["sys_id"] if rows else None

    def upsert(self, table: str, find_query: str, body: dict, display_value=True) -> str:
        """Return existing sys_id (idempotent) or create and return the new one."""
        sid = self.find(table, find_query)
        if sid:
            return sid
        r = self.s.post(f"{self.base}/api/now/table/{table}",
                        params={"sysparm_input_display_value": "true" if display_value else "false"},
                        data=json.dumps(body), timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        return r.json()["result"]["sys_id"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-secret", action="store_true",
                    help="Read creds from Secrets Manager dev/<project>/servicenow instead of env vars")
    args = ap.parse_args()

    sn = SN(_load_creds(args.from_secret))
    log.info("Seeding CMDB on %s", sn.base)

    # 1. Groups
    group_sids: dict[str, str] = {}
    for key, name in GROUPS.items():
        group_sids[key] = sn.upsert("sys_user_group", f"name={name}", {"name": name})
        log.info("  group %-18s → %s", name, group_sids[key])

    # 2. CIs (support_group passed by display name via input_display_value)
    ci_sids: dict[str, str] = {}
    for name, cls, team_key, arn in CIS:
        ci_sids[name] = sn.upsert(cls, f"name={name}", {
            "name": name,
            "correlation_id": arn,
            "support_group": GROUPS[team_key],
            "operational_status": "1",
        })
        log.info("  CI    %-32s [%s] → %s", name, cls, ci_sids[name])

    # 3. Relationships (parent/child by sys_id, type by display name)
    for parent, rel_type, child in RELS:
        p, c = ci_sids[parent], ci_sids[child]
        sn.upsert("cmdb_rel_ci", f"parent={p}^child={c}",
                  {"parent": p, "child": c, "type": rel_type})
        log.info("  rel   %s --%s--> %s", parent, rel_type, child)

    log.info("Done. Try: query the specialist with resource "
             "'alb-mig-prod-claims-api-001' (impact_analysis).")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log.exception("Seed failed: %s", e)
        sys.exit(1)
