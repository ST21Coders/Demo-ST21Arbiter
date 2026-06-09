"""
Seed mock data into ARBITER DDB tables for the demo.

Inserts:
  - 12 conflicts  (ARBITER-UC01..UC12) with the new schema fields
                  (conflict_type, domain, policy_citations, enforcement_evidence,
                  scan_run_id="seed-bootstrap", rule_key, fp_score, compliant=false)
  - 14 compliant rows (COMPLIANT-UC*-*) — same shape, compliant=true
  - 2 change-requests (UC07 + UC08) with full approver chain
  - 8 audit-log entries spanning the last 24h
  - 1 scan-runs row (seed-bootstrap)

Tables are dual-written when conflicts-v2 / scan-runs exist (post-Step-2 deploy);
falls back gracefully when they don't.

Usage:
  source scripts/.venv/bin/activate
  PROJECT=st21arbiter-poc python3 seed_mock_data.py
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

REGION  = os.environ.get("AWS_REGION", "us-east-1")
ENV     = os.environ.get("ENVIRONMENT", "dev")
PROJECT = os.environ.get("PROJECT", "st21arbiter-poc")

PREFIX = f"{ENV}-{PROJECT}"
T_CONFLICTS    = f"{PREFIX}-conflicts"
T_CONFLICTS_V2 = f"{PREFIX}-conflicts-v2"
T_CHANGE_REQS  = f"{PREFIX}-change-requests"
T_AUDIT        = f"{PREFIX}-audit-log"
T_SCAN_RUNS    = f"{PREFIX}-scan-runs"
T_OWNERSHIP_RULES = f"{PREFIX}-ownership-rules"

ddb = boto3.resource("dynamodb", region_name=REGION)
ddb_client = boto3.client("dynamodb", region_name=REGION)

NOW = datetime.now(timezone.utc)
SCAN_RUN_ID = "seed-bootstrap"


def iso(seconds: int = 0) -> str:
    return (NOW + timedelta(seconds=seconds)).isoformat()


def table_exists(name: str) -> bool:
    try:
        ddb_client.describe_table(TableName=name)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            return False
        raise


# ── Conflicts (12 UCs) ─────────────────────────────────────────────────────────

UC_DATA = [
    # (rule_key, severity, domain, source_pair, title, source_policy, source_tech,
    #  policy_citations, enforcement_evidence, regulatory, fp_score, conflict_type, domains_list, age_seconds)
    ("UC01", "HIGH", "ACCESS_MGMT", "SharePoint+Zscaler",
     "Dropbox approved in policy but blocked by Zscaler",
     "MIG-POL-001-CS01", "ZIA-URLCAT-CLOUD-BLK-042",
     [{"doc": "MIG-POL-001", "version": "v3.4", "section": "2.1",
       "quote": "Dropbox Business listed as approved. Passed vendor assessment Q3 2025.",
       "confidence": Decimal("0.97")}],
     [{"source": "Zscaler", "rule_id": "ZIA-URLCAT-CLOUD-BLK-042", "action": "BLOCK",
       "raw": {"category": "Cloud Storage", "domains": ["dropbox.com"]}}],
     [], Decimal("0.05"), "CONTRADICTION", ["SharePoint", "Zscaler"], -7200),

    ("UC02", "HIGH", "VENDOR_MGMT", "SharePoint+Zscaler",
     "Approved remote support tools blocked by Zscaler for vendor users",
     "MIG-POL-001-RA01", "ZIA-APP-CTRL-REMOTE-BLOCK-007",
     [{"doc": "MIG-POL-001", "version": "v3.4", "section": "2.3",
       "quote": "TeamViewer Corporate, AnyDesk Enterprise, BeyondTrust Remote Support are approved for authorised IT and MSP personnel.",
       "confidence": Decimal("0.96")},
      {"doc": "MIG-POL-005", "version": "v2.8", "section": "6",
       "quote": "All vendor remote-support sessions must be logged to SIEM.",
       "confidence": Decimal("0.94")}],
     [{"source": "Zscaler", "rule_id": "ZIA-APP-CTRL-REMOTE-BLOCK-007", "action": "BLOCK",
       "raw": {"apps": ["TeamViewer", "AnyDesk"]}}],
     [], Decimal("0.07"), "CONTRADICTION", ["SharePoint", "Zscaler"], -14400),

    ("UC03", "MEDIUM", "ACCESS_MGMT", "SharePoint+Zscaler",
     "Zscaler blocks Firefox — policy mandates browser freedom",
     "MIG-POL-001-WB01", "ZIA-APP-CTRL-BROWSER-FF-009",
     [{"doc": "MIG-POL-001", "version": "v3.4", "section": "4",
       "quote": "Chrome, Firefox, Edge, Safari, Brave are permitted on corporate devices without further approval.",
       "confidence": Decimal("0.95")}],
     [{"source": "Zscaler", "rule_id": "ZIA-APP-CTRL-BROWSER-FF-009", "action": "BLOCK",
       "raw": {"app": "Firefox"}}],
     [], Decimal("0.15"), "CONTRADICTION", ["SharePoint", "Zscaler"], -21600),

    ("UC04", "CRITICAL", "COMPLIANCE", "SharePoint+Zscaler",
     "SSL inspection bypassed for 47 financial domains — PCI DSS violation",
     "MIG-POL-002-SSL01", "ZIA-SSL-BYPASS-FIN-DOMAINS",
     [{"doc": "MIG-POL-002", "version": "v5.1", "section": "2.2",
       "quote": "SSL/TLS inspection is mandatory on ALL web traffic. Exceptions only with documented CISO approval in the SSL Inspection Exception Register, 90-day max.",
       "confidence": Decimal("0.98")}],
     [{"source": "Zscaler", "rule_id": "ZIA-SSL-BYPASS-FIN-DOMAINS", "action": "BYPASS_INSPECT",
       "raw": {"domains_count": 47, "registered_exception": False}}],
     ["PCI DSS 4.0 Req 4.1"], Decimal("0.03"), "GAP", ["SharePoint", "Zscaler"], -1800),

    ("UC05", "CRITICAL", "ACCESS_MGMT", "SharePoint+Zscaler",
     "MFA enforcement limited to admin accounts — policy requires all users",
     "MIG-POL-002-MFA01", "ZPA-AUTHPOL-ADMIN-MFA-ONLY",
     [{"doc": "MIG-POL-002", "version": "v5.1", "section": "4.1",
       "quote": "MFA is required for ALL users — employees, contractors, vendors — regardless of privilege level.",
       "confidence": Decimal("0.97")}],
     [{"source": "Zscaler", "rule_id": "ZPA-AUTHPOL-ADMIN-MFA-ONLY", "action": "MFA_REQUIRED",
       "raw": {"scope": "Privileged Admins", "non_admin_users_unprotected": 4200}}],
     ["PCI DSS 8.4", "NAIC MDL-668"], Decimal("0.04"), "GAP", ["SharePoint", "Zscaler"], -5400),

    ("UC06", "HIGH", "NETWORK_SECURITY", "SharePoint+Zscaler",
     "IoT devices in monitor-only mode — policy requires active blocking",
     "MIG-POL-002-IOT01", "ZIA-IOT-MONITOR-ONLY-VLAN-19",
     [{"doc": "MIG-POL-002", "version": "v5.1", "section": "5.1",
       "quote": "Monitoring-only mode is NOT acceptable for IoT external communication. Active blocking is required.",
       "confidence": Decimal("0.96")}],
     [{"source": "Zscaler", "rule_id": "ZIA-IOT-MONITOR-ONLY-VLAN-19", "action": "MONITOR",
       "raw": {"vlan": 19, "devices": 43}}],
     [], Decimal("0.06"), "GAP", ["SharePoint", "Zscaler"], -10800),

    ("UC07", "CRITICAL", "CLOUD_SECURITY", "SharePoint+AWS Config",
     "Production ALB exposed to 0.0.0.0/0 without WAF — critical WAF bypass",
     "MIG-POL-004-WAF01", "alb-mig-prod-claims-api-001",
     [{"doc": "MIG-POL-004", "version": "v4.0", "section": "2",
       "quote": "No production application resource shall be directly accessible from the public internet without AWS WAF + OWASP CRS.",
       "confidence": Decimal("0.98")}],
     [{"source": "AWSConfig", "resource_id": "alb-mig-prod-claims-api-001", "action": "NON_COMPLIANT",
       "raw": {"security_group": "sg-mig-prod-alb-open", "ingress": "0.0.0.0/0:443",
               "waf_attached": False, "age_days": 47}}],
     ["PCI DSS 6.4"], Decimal("0.02"), "DRIFT", ["SharePoint", "AWSConfig"], -3600),

    ("UC08", "CRITICAL", "NETWORK_SECURITY", "SharePoint+AWS Config",
     "Dev-to-prod VPC peering active — production segmentation violated",
     "MIG-POL-004-SEG01", "pcx-mig-prod-dev-001",
     [{"doc": "MIG-POL-004", "version": "v4.0", "section": "3",
       "quote": "VPC peering between production and non-production environments is prohibited.",
       "confidence": Decimal("0.98")}],
     [{"source": "AWSConfig", "resource_id": "pcx-mig-prod-dev-001", "action": "NON_COMPLIANT",
       "raw": {"prod_vpc": "vpc-mig-prod-001", "dev_vpc": "vpc-mig-dev-002", "age_days": 78}}],
     ["PCI DSS 1.3"], Decimal("0.02"), "DRIFT", ["SharePoint", "AWSConfig"], -2700),

    ("UC09", "CRITICAL", "DATA_GOVERNANCE", "SharePoint+AWS Config",
     "S3 claims data replicating to eu-west-1 — NAIC data residency breach",
     "MIG-POL-003-DR01", "mig-prod-claims-data-primary",
     [{"doc": "MIG-POL-003", "version": "v2.2", "section": "3",
       "quote": "All customer insurance data must remain within the continental United States. No exceptions.",
       "confidence": Decimal("0.97")}],
     [{"source": "AWSConfig", "resource_id": "mig-prod-claims-data-primary", "action": "NON_COMPLIANT",
       "raw": {"replication_target": "eu-west-1", "pii_tier": 1, "age_days": 134}}],
     ["NAIC MDL-668"], Decimal("0.03"), "DRIFT", ["SharePoint", "AWSConfig"], -300),

    ("UC10", "HIGH", "DATA_GOVERNANCE", "SharePoint+Zscaler",
     "DLP blanket rule blocking authorised actuarial data transfers",
     "MIG-POL-003-DT01", "ZIA-DLP-PII-BLOCK-ALL-EXTERNAL",
     [{"doc": "MIG-POL-003", "version": "v2.2", "section": "2.1",
       "quote": "Authorised actuarial data transfers: Milliman Inc., Willis Towers Watson, Verisk Analytics.",
       "confidence": Decimal("0.95")}],
     [{"source": "Zscaler", "rule_id": "ZIA-DLP-PII-BLOCK-ALL-EXTERNAL", "action": "BLOCK",
       "raw": {"exceptions": []}}],
     ["NAIC MDL-668"], Decimal("0.08"), "CONTRADICTION", ["SharePoint", "Zscaler"], -18000),

    ("UC11", "MEDIUM", "VENDOR_MGMT", "SharePoint+Zscaler",
     "ZTNA geo-restriction blocks approved vendor countries",
     "MIG-POL-003-VA01", "ZPA-GEO-RESTRICT-INDIA-US-ONLY",
     [{"doc": "MIG-POL-003", "version": "v2.2", "section": "4",
       "quote": "Approved vendor countries: US, India, UK, Singapore, Germany, Australia, Philippines, Canada.",
       "confidence": Decimal("0.96")},
      {"doc": "MIG-POL-005", "version": "v2.8", "section": "5",
       "quote": "ZTNA restrictions limited to India and US only are non-compliant.",
       "confidence": Decimal("0.97")}],
     [{"source": "Zscaler", "rule_id": "ZPA-GEO-RESTRICT-INDIA-US-ONLY", "action": "ALLOW",
       "raw": {"countries": ["IN", "US"]}}],
     [], Decimal("0.10"), "CONTRADICTION", ["SharePoint", "Zscaler"], -25200),

    ("UC12", "MEDIUM", "ACCESS_MGMT", "SharePoint+Zscaler",
     "Social media blanket block ignores policy exemptions for 4 departments",
     "MIG-POL-001-SM01", "ZIA-URLCAT-SOCIAL-BLOCK-ALL",
     [{"doc": "MIG-POL-001", "version": "v3.4", "section": "3",
       "quote": "URL filtering controls must include exceptions for Marketing, Communications, HR, and Talent Acquisition.",
       "confidence": Decimal("0.95")}],
     [{"source": "Zscaler", "rule_id": "ZIA-URLCAT-SOCIAL-BLOCK-ALL", "action": "BLOCK",
       "raw": {"department_exceptions": []}}],
     [], Decimal("0.12"), "GAP", ["SharePoint", "Zscaler"], -28800),
]


# ── Team / tag ownership (placeholder taxonomy — swap for Meridian's real org) ──
# owner_team    = team that owns/authors the policy intent
# consumer_team = team affected/blocked by the conflict
# platform_team = team that manages the enforcing control (Zscaler / AWS / etc.)
# Keyed by rule_key so it drives BOTH the seeded findings (live-mode demo before a
# real scan) AND the ownership-rules table (which the scanner's enrichment.py reads).
OWNERSHIP = {
    "UC01": {"owner_team": "data-governance",  "consumer_team": "app-dev",     "platform_team": "network-eng",       "tags": ["application", "network"]},
    "UC02": {"owner_team": "vendor-mgmt",       "consumer_team": "app-dev",     "platform_team": "network-eng",       "tags": ["vendor", "network"]},
    "UC03": {"owner_team": "data-governance",   "consumer_team": "app-dev",     "platform_team": "network-eng",       "tags": ["application", "network"]},
    "UC04": {"owner_team": "platform-security", "consumer_team": "cloud-infra", "platform_team": "network-eng",       "tags": ["network", "data-residency"]},
    "UC05": {"owner_team": "platform-security", "consumer_team": "app-dev",     "platform_team": "platform-security", "tags": ["identity"]},
    "UC06": {"owner_team": "network-eng",       "consumer_team": "cloud-infra", "platform_team": "network-eng",       "tags": ["network", "infrastructure"]},
    "UC07": {"owner_team": "cloud-infra",       "consumer_team": "app-dev",     "platform_team": "cloud-infra",       "tags": ["infrastructure", "network"]},
    "UC08": {"owner_team": "cloud-infra",       "consumer_team": "app-dev",     "platform_team": "cloud-infra",       "tags": ["infrastructure", "network"]},
    "UC09": {"owner_team": "data-governance",   "consumer_team": "cloud-infra", "platform_team": "cloud-infra",       "tags": ["data-residency", "infrastructure"]},
    "UC10": {"owner_team": "data-governance",   "consumer_team": "app-dev",     "platform_team": "network-eng",       "tags": ["data-residency", "application"]},
    "UC11": {"owner_team": "vendor-mgmt",       "consumer_team": "app-dev",     "platform_team": "network-eng",       "tags": ["vendor", "network"]},
    "UC12": {"owner_team": "data-governance",   "consumer_team": "app-dev",     "platform_team": "network-eng",       "tags": ["application", "network"]},
}
_OWNERSHIP_DEFAULT = {"owner_team": "unassigned", "consumer_team": "", "platform_team": "", "tags": ["untriaged"]}


def _ownership_for(rule_key):
    return OWNERSHIP.get(rule_key, _OWNERSHIP_DEFAULT)


def ownership_rule_rows():
    """Rows for the ownership-rules table — one per UC (match on rule_key) plus a
    wildcard default. Mirrors enrichment.py's expected shape so live scans
    produce the same team data the seeded findings show."""
    rows, prio = [], 10
    for rk in sorted(OWNERSHIP):
        rows.append({"rule_id": f"rule-{rk.lower()}", "priority": prio,
                     "match": {"rule_key": rk}, **OWNERSHIP[rk]})
        prio += 10
    rows.append({"rule_id": "rule-default", "priority": 999, "match": {}, **_OWNERSHIP_DEFAULT})
    return rows


def conflict_item(uc):
    (rule_key, severity, domain, source_pair, title, src_pol, src_tech,
     pol_cites, enf_evid, regulatory, fp_score, c_type, domains_list, age) = uc
    return {
        **_ownership_for(rule_key),
        "conflict_id":          f"ARBITER-{rule_key}",
        "detected_at":          iso(age),
        "status":               "OPEN",
        "severity":             severity,
        "title":                title,
        "domain":               domain,
        "source_pair":          source_pair,
        "conflict_type":        c_type,
        "rule_key":             rule_key,
        "scan_run_id":          SCAN_RUN_ID,
        "fp_score":             fp_score,
        "compliant":            False,
        "source_policy":        src_pol,
        "source_technical":     src_tech,
        "domains":              domains_list,
        "policy_citations":     pol_cites,
        "enforcement_evidence": enf_evid,
        "regulatory":           regulatory,
        "finding":              f"{src_pol} conflicts with {src_tech}. See policy_citations and enforcement_evidence.",
        "impact":               "Business or compliance impact — see policy citations.",
        "remediation":          [
            f"Reconcile {src_pol} clause against {src_tech}.",
            "Submit change request through ARBITER Action Center.",
        ],
    }


# ── 14 compliant alignments (false-positive guard rows) ───────────────────────

COMPLIANT_ROWS = [
    ("COMPLIANT-UC01-BOX",         "UC01", "ACCESS_MGMT",      "SharePoint+Zscaler",     ["SharePoint","Zscaler"],   "Box.com approved and accessible — policy ↔ enforcement aligned"),
    ("COMPLIANT-UC02-BEYONDTRUST", "UC02", "VENDOR_MGMT",      "SharePoint+Zscaler",     ["SharePoint","Zscaler"],   "BeyondTrust Remote Support whitelisted and SIEM-logged"),
    ("COMPLIANT-UC03-CHROME",      "UC03", "ACCESS_MGMT",      "SharePoint+Zscaler",     ["SharePoint","Zscaler"],   "Chrome Enterprise permitted — browser policy aligned"),
    ("COMPLIANT-UC04-HEALTHCARE",  "UC04", "COMPLIANCE",       "SharePoint+Zscaler",     ["SharePoint","Zscaler"],   "Healthcare category SSL-inspected per policy"),
    ("COMPLIANT-UC04-GOV",         "UC04", "COMPLIANCE",       "SharePoint+Zscaler",     ["SharePoint","Zscaler"],   "Government category SSL-inspected per policy"),
    ("COMPLIANT-UC05-ADMIN",       "UC05", "ACCESS_MGMT",      "SharePoint+Zscaler",     ["SharePoint","Zscaler"],   "Privileged Admin MFA actively enforced (sub-control compliant)"),
    ("COMPLIANT-UC06-PRINTERS",    "UC06", "NETWORK_SECURITY", "SharePoint+Zscaler",     ["SharePoint","Zscaler"],   "VLAN-12 managed printers blocked from external — IoT policy aligned"),
    ("COMPLIANT-UC07-API002",      "UC07", "CLOUD_SECURITY",   "SharePoint+AWS Config",  ["SharePoint","AWSConfig"], "alb-mig-prod-api-002 protected by AWS WAF + OWASP CRS"),
    ("COMPLIANT-UC07-PORTAL003",   "UC07", "CLOUD_SECURITY",   "SharePoint+AWS Config",  ["SharePoint","AWSConfig"], "alb-mig-prod-portal-003 protected by AWS WAF + OWASP CRS"),
    ("COMPLIANT-UC08-TGW",         "UC08", "NETWORK_SECURITY", "SharePoint+AWS Config",  ["SharePoint","AWSConfig"], "Cross-prod-account routing via Transit Gateway — segmentation preserved"),
    ("COMPLIANT-UC09-USREP",       "UC09", "DATA_GOVERNANCE",  "SharePoint+AWS Config",  ["SharePoint","AWSConfig"], "mig-prod-customer-data-secondary replication us-east-1 → us-west-2 (in-region)"),
    ("COMPLIANT-UC10-INTERNAL",    "UC10", "DATA_GOVERNANCE",  "SharePoint+Zscaler",     ["SharePoint","Zscaler"],   "Internal-only data flows correctly unblocked by DLP"),
    ("COMPLIANT-UC11-US",          "UC11", "VENDOR_MGMT",      "SharePoint+Zscaler",     ["SharePoint","Zscaler"],   "US-based vendor access permitted by ZTNA — country list compliant"),
    ("COMPLIANT-UC12-GENERAL",     "UC12", "ACCESS_MGMT",      "SharePoint+Zscaler",     ["SharePoint","Zscaler"],   "General employee social-media block applied as intended"),
]


def compliant_item(row, age_seconds):
    cid, rule_key, domain, source_pair, domains_list, title = row
    return {
        **_ownership_for(rule_key),
        "conflict_id":  cid,
        "detected_at":  iso(age_seconds),
        "status":       "COMPLIANT",
        "severity":     "INFO",
        "title":        title,
        "domain":       domain,
        "source_pair":  source_pair,
        "conflict_type": "OVERLAP",
        "rule_key":     rule_key,
        "scan_run_id":  SCAN_RUN_ID,
        "compliant":    True,
        "fp_score":     Decimal("0.0"),
        "domains":      domains_list,
        "finding":      "Policy and enforcement agree — recorded for audit evidence.",
        "impact":       "No action required. Compliance evidence on file.",
        "policy_citations":     [],
        "enforcement_evidence": [],
        "regulatory":   [],
        "remediation":  [],
    }


# ── Change requests ───────────────────────────────────────────────────────────

CHANGE_REQUESTS = [
    {
        "cr_id": "CR-20260519-WAF001",
        "status": "PENDING_APPROVAL",
        "conflict_id": "ARBITER-UC07",
        "linked_conflict_id": "ARBITER-UC07",
        "action_type": "SECURITY_FIX",
        "target_resource": "alb-mig-prod-claims-api-001",
        "target_environment": "PROD",
        "severity": "CRITICAL",
        "description": "Associate AWS WAF web ACL with OWASP CRS 4.0 to production claims API load balancer",
        "requested_by": "grc_priya@meridianinsurance.com",
        "justification": "Production ALB directly internet-accessible without WAF. PCI DSS Req 6.4 violation. Immediate remediation required per MIG-POL-004-WAF01.",
        "created_at": iso(-3600),
        "approvers": [
            {"role": "ciso", "email": "ciso_diana@meridianinsurance.com", "status": "PENDING", "description": "CISO approval required for PROD CRITICAL"},
            {"role": "vp_network", "email": "vp-network@meridianinsurance.com", "status": "PENDING", "description": "VP Network Engineering approval required"},
        ],
        "total_approvers_needed": 2,
        "total_approvals_received": 0,
    },
    {
        "cr_id": "CR-20260519-VPC002",
        "status": "PENDING_APPROVAL",
        "conflict_id": "ARBITER-UC08",
        "linked_conflict_id": "ARBITER-UC08",
        "action_type": "SECURITY_FIX",
        "target_resource": "pcx-mig-prod-dev-001",
        "target_environment": "PROD",
        "severity": "CRITICAL",
        "description": "Terminate dev-to-prod VPC peering and revoke sg-mig-prod-peer-dev-001 inbound rule",
        "requested_by": "grc_priya@meridianinsurance.com",
        "justification": "Active prod-dev peering for 78 days. PCI DSS segmentation failure. Violates MIG-POL-004-SEG01.",
        "created_at": iso(-7200),
        "approvers": [
            {"role": "ciso", "email": "ciso_diana@meridianinsurance.com", "status": "PENDING", "description": "CISO approval required for PROD CRITICAL"},
            {"role": "vp_network", "email": "vp-network@meridianinsurance.com", "status": "APPROVED", "description": "VP Network Engineering approved"},
        ],
        "total_approvers_needed": 2,
        "total_approvals_received": 1,
    },
]


# ── Audit log entries ─────────────────────────────────────────────────────────

AUDIT = [
    {"event_id": "1", "timestamp": iso(0),         "action_type": "SCAN_COMPLETED",    "resource": SCAN_RUN_ID,                    "user": "system",                          "status": "COMPLETED",        "details": json.dumps({"conflicts_found": 12, "compliant": 14, "critical": 4, "high": 4, "medium": 4})},
    {"event_id": "2", "timestamp": iso(-1800),     "action_type": "CR_CREATED",        "resource": "alb-mig-prod-claims-api-001",  "user": "grc_priya@meridianinsurance.com", "status": "PENDING_APPROVAL", "details": json.dumps({"cr_id": "CR-20260519-WAF001", "conflict_id": "ARBITER-UC07"})},
    {"event_id": "3", "timestamp": iso(-3600),     "action_type": "CR_CREATED",        "resource": "pcx-mig-prod-dev-001",         "user": "grc_priya@meridianinsurance.com", "status": "PENDING_APPROVAL", "details": json.dumps({"cr_id": "CR-20260519-VPC002", "conflict_id": "ARBITER-UC08"})},
    {"event_id": "4", "timestamp": iso(-5400),     "action_type": "CR_APPROVED",       "resource": "CR-20260519-VPC002",           "user": "vp-network@meridianinsurance.com","status": "APPROVED",         "details": json.dumps({"approver_role": "vp_network"})},
    {"event_id": "5", "timestamp": iso(-9000),     "action_type": "INGESTION_COMPLETE","resource": "MIG-POL-001..005",             "user": "system",                          "status": "COMPLETED",        "details": json.dumps({"documents_indexed": 5})},
    {"event_id": "6", "timestamp": iso(-14400),    "action_type": "SCAN_COMPLETED",    "resource": "scan-2026-05-28T18:00:00Z",    "user": "system",                          "status": "COMPLETED",        "details": json.dumps({"conflicts_found": 12, "compliant": 14})},
    {"event_id": "7", "timestamp": iso(-43200),    "action_type": "JIRA_LINKED",       "resource": "ARBITER-UC07",                 "user": "soc_marcus@meridianinsurance.com","status": "COMPLETED",        "details": json.dumps({"jira_ticket_key": "MIG-MOCK-12345"})},
    {"event_id": "8", "timestamp": iso(-86400),    "action_type": "SCAN_COMPLETED",    "resource": "scan-2026-05-27T06:00:00Z",    "user": "system",                          "status": "COMPLETED",        "details": json.dumps({"conflicts_found": 12, "compliant": 14})},
]


# ── Scan-runs row ─────────────────────────────────────────────────────────────

SCAN_RUN_ROW = {
    "scan_run_id":   SCAN_RUN_ID,
    "started_at":    iso(-3600),
    "finished_at":   iso(-3580),
    "status":        "COMPLETED",
    "triggered_by":  "seed-bootstrap",
    "duration_ms":   20000,
    "rule_pack_version": "v1",
    "totals": {
        "conflicts": 12, "compliant": 14,
        "critical": 4, "high": 4, "medium": 4, "low": 0,
    },
    "source_versions": {
        "sharepoint_kb_ingestion_id": "seed",
        "zscaler_ruleset_sha":        "seed",
        "awsconfig_snapshot_sha":     "seed",
    },
}


# ── Insertion ─────────────────────────────────────────────────────────────────

def put_into(table_name: str, items: list[dict]) -> int:
    if not table_exists(table_name):
        print(f"  (skipped — table {table_name} does not exist)")
        return 0
    table = ddb.Table(table_name)
    with table.batch_writer() as bw:
        for item in items:
            bw.put_item(Item=item)
    return len(items)


def main() -> int:
    conflicts = [conflict_item(uc) for uc in UC_DATA]
    compliant_age = -1800
    compliant = []
    for row in COMPLIANT_ROWS:
        compliant.append(compliant_item(row, compliant_age))
        compliant_age -= 600

    print(f"Seeding into region={REGION} prefix={PREFIX}")
    print()

    n = put_into(T_CONFLICTS, conflicts + compliant)
    print(f"  ✓ conflicts table     : wrote {n} rows ({len(conflicts)} conflicts + {len(compliant)} compliant)")

    n = put_into(T_CONFLICTS_V2, conflicts + compliant)
    print(f"  ✓ conflicts-v2 table  : wrote {n} rows (post-Step-2 dual-write)")

    n = put_into(T_CHANGE_REQS, CHANGE_REQUESTS)
    print(f"  ✓ change-requests     : wrote {n} rows")

    n = put_into(T_AUDIT, AUDIT)
    print(f"  ✓ audit-log           : wrote {n} rows")

    n = put_into(T_SCAN_RUNS, [SCAN_RUN_ROW])
    print(f"  ✓ scan-runs           : wrote {n} rows (post-Step-2)")

    n = put_into(T_OWNERSHIP_RULES, ownership_rule_rows())
    print(f"  ✓ ownership-rules     : wrote {n} rows (12 UC + wildcard default)")

    print()
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
