"""Regenerate the ARBITER demo policy corpus with verbatim clauses.

Produces:
  BaselineFiles/_source/MIG-POL-001..005.md     (markdown source — Bedrock KB ingests directly)
  BaselineFiles/zscaler/LM_ZIA_Rules_Cited.json (every Zscaler rule UC01..UC12 references)
  BaselineFiles/aws-config/AWS-Config-Snapshot-v2.json (non-compliant + compliant guard resources)

Each MIG-POL document contains the EXACT clause text the use-case doc quotes
(MIG-POL-001 §2.1 "Dropbox Business listed as approved. Passed vendor
assessment Q3 2025."  etc.) so chat citations match the demo script word-for-word.

The script can also:
  --sync     aws s3 sync BaselineFiles/ to the processed bucket
  --ingest   start a Bedrock KB ingestion job and poll for COMPLETE
  --backup   move existing PDFs to BaselineFiles/_archive/<timestamp>/

Usage:
  source scripts/.venv/bin/activate
  python3 scripts/generate_baseline_corpus.py --backup --sync --ingest

Env vars:
  PROJECT            default: st21arbiter-poc
  ENVIRONMENT        default: dev
  AWS_REGION         default: us-east-1
  KB_ID              default: SQCLG3W09Y
  KB_DATA_SOURCE_ID  default: NM2FVXL5T6
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

REGION  = os.environ.get("AWS_REGION", "us-east-1")
ENV     = os.environ.get("ENVIRONMENT", "dev")
PROJECT = os.environ.get("PROJECT", "st21arbiter-poc")
KB_ID   = os.environ.get("KB_ID", "SQCLG3W09Y")
KB_DS   = os.environ.get("KB_DATA_SOURCE_ID", "NM2FVXL5T6")
PROCESSED_BUCKET = os.environ.get("PROCESSED_BUCKET", f"{ENV}-{PROJECT}-processed")

REPO = Path(__file__).resolve().parent.parent
BASELINE = REPO / "BaselineFiles"
SOURCE   = BASELINE / "_source"
ARCHIVE  = BASELINE / "_archive"


@dataclass
class Section:
    number: str
    title: str
    body: str


@dataclass
class Policy:
    doc_id: str
    version: str
    title: str
    effective: str
    owner: str
    sections: list[Section]


# ── The 5 MIG-POL documents with verbatim clauses ─────────────────────────────

MIG_POL_001 = Policy(
    doc_id="MIG-POL-001",
    version="v3.4",
    title="Acceptable Use Policy",
    effective="January 2026",
    owner="CISO Office",
    sections=[
        Section("1", "Purpose", "Defines acceptable use of MIG information systems, cloud services, browsers, and remote-access tooling for all employees, contractors, and authorised vendor personnel."),
        Section("2.1", "Approved Cloud Storage", "Dropbox Business listed as approved. Passed vendor assessment Q3 2025. Use must follow the data classification requirements in MIG-POL-003 §2."),
        Section("2.2", "Approved Collaboration", "Microsoft 365, SharePoint Online, Box.com Enterprise are approved for general collaboration."),
        Section("2.3", "Approved Remote Support Tools", "TeamViewer Corporate, AnyDesk Enterprise, BeyondTrust Remote Support are approved for authorised IT and MSP personnel. All sessions must comply with MIG-POL-005 §6 SIEM logging requirements."),
        Section("3", "Social Media Exceptions", "URL filtering controls must include exceptions for Marketing, Communications, HR, and Talent Acquisition. These groups require unrestricted social-media access for business-critical activities including employer branding, talent recruitment, and regulatory communication."),
        Section("4", "Web Browsers", "Chrome, Firefox, Edge, Safari, Brave are permitted on corporate devices without further approval. Browser restrictions for security justification require documented CISO approval."),
        Section("5", "Enforcement", "Violations are reported through ARBITER and triaged via the standard remediation workflow. The CISO has discretion to grant time-bound exceptions in writing."),
    ],
)

MIG_POL_002 = Policy(
    doc_id="MIG-POL-002",
    version="v5.1",
    title="Information Security Policy",
    effective="February 2026",
    owner="CISO Office",
    sections=[
        Section("1", "Purpose", "Establishes mandatory information-security controls covering web traffic inspection, identity, IoT, and API rate-limiting."),
        Section("2.2", "SSL/TLS Inspection", "SSL/TLS inspection is mandatory on ALL web traffic. Exceptions only with documented CISO approval in the SSL Inspection Exception Register, 90-day max. PCI DSS 4.0 Requirement 4.1 applies."),
        Section("4.1", "MFA Enforcement", "MFA is required for ALL users — employees, contractors, vendors — regardless of privilege level. PCI DSS 8.4 applies. Coverage gaps must be closed within 30 days of detection."),
        Section("5.1", "IoT External Communication", "Monitoring-only mode is NOT acceptable for IoT external communication. Active blocking is required. Internal firmware-update proxying must be enforced for all IoT devices."),
        Section("API01", "API Rate Limiting", "All production API resources must enforce a baseline of 2,000 requests per IP per 5 minutes. WAF logs must reach SIEM within 60 seconds."),
    ],
)

MIG_POL_003 = Policy(
    doc_id="MIG-POL-003",
    version="v2.2",
    title="Data Sharing and Transfer Policy",
    effective="November 2025",
    owner="GRC Office (with Legal & Privacy)",
    sections=[
        Section("1", "Purpose", "Governs movement of customer insurance data inside and outside MIG, the authorised vendors that may receive it, and the cross-border boundaries that apply."),
        Section("2.1", "Authorised Actuarial Transfers", "Authorised actuarial data transfers: Milliman Inc., Willis Towers Watson, Verisk Analytics. DLP rules must include domain exceptions for these vendors."),
        Section("2.2", "Authorised Financial Transfers", "Authorised finance vendor transfers: Stripe, ACI Worldwide, Deloitte, PwC. DLP rules must include exceptions; otherwise audit submissions fail silently."),
        Section("3", "Data Residency", "All customer insurance data must remain within the continental United States. No exceptions. NAIC MDL-668 applies. State commissioner disclosure may be required for any breach."),
        Section("4", "Vendor Access Geographies", "Approved vendor countries: US, India, UK, Singapore, Germany, Australia, Philippines, Canada. Geographic restrictions narrower than this set are non-compliant unless mandated by sanctions."),
    ],
)

MIG_POL_004 = Policy(
    doc_id="MIG-POL-004",
    version="v4.0",
    title="Network Security Standard",
    effective="December 2025",
    owner="VP Network Engineering (with CISO)",
    sections=[
        Section("1", "Purpose", "Defines minimum network-security controls for production resources, including WAF requirements, segmentation, IoT, and the prohibition on temporary perimeter exceptions."),
        Section("2", "WAF Requirements", "No production application resource shall be directly accessible from the public internet without AWS WAF + OWASP CRS. Temporary exceptions are not permitted. PCI DSS 6.4 applies to any cardholder-data-touching resource."),
        Section("3", "Network Segmentation", "VPC peering between production and non-production environments is prohibited. Cross-account routing through approved Transit Gateway segments remains compliant."),
        Section("4", "IoT Segmentation", "IoT external communication must be blocked. Monitoring-only mode is not acceptable. Internal firmware-update proxying must be enforced."),
    ],
)

MIG_POL_005 = Policy(
    doc_id="MIG-POL-005",
    version="v2.8",
    title="Vendor Access and Third-Party Risk Policy",
    effective="October 2025",
    owner="GRC Office (with Vendor Management)",
    sections=[
        Section("1", "Purpose", "Governs third-party vendor access to MIG systems, identity provisioning, geographic restrictions, and remote-support session controls."),
        Section("5", "Geo Restrictions", "ZTNA restrictions limited to India and US only are non-compliant. All eight approved vendor countries from MIG-POL-003 §4 must be permitted."),
        Section("6", "Remote Support", "All vendor remote-support sessions must be logged to SIEM. TeamViewer, AnyDesk, BeyondTrust are the approved tools (see MIG-POL-001 §2.3)."),
        Section("SANC01", "Sanctions Compliance", "OFAC-sanctioned countries remain blocked regardless of vendor policy. Sanctions controls take precedence over access permissions."),
    ],
)

POLICIES = [MIG_POL_001, MIG_POL_002, MIG_POL_003, MIG_POL_004, MIG_POL_005]


# ── Zscaler ruleset (every cited rule) ────────────────────────────────────────

ZSCALER_RULES = [
    {"rule_id": "ZIA-URLCAT-CLOUD-BLK-042",       "name": "Cloud Storage Block",   "category": "Cloud Storage",  "action": "BLOCK", "scope": "AllUsers",           "domains": ["dropbox.com"],                              "notes": "Conflicts with MIG-POL-001 §2.1"},
    {"rule_id": "ZIA-APP-CTRL-REMOTE-BLOCK-007",  "name": "Remote Tools Block",     "category": "Remote Access","action": "BLOCK", "scope": "AllUsers",           "apps": ["TeamViewer", "AnyDesk"],                       "notes": "Conflicts with MIG-POL-001 §2.3 and MIG-POL-005 §6"},
    {"rule_id": "ZIA-APP-CTRL-BROWSER-FF-009",    "name": "Firefox Restrict",       "category": "Browser",      "action": "BLOCK", "scope": "AllUsers",           "apps": ["Firefox"],                                      "notes": "Conflicts with MIG-POL-001 §4"},
    {"rule_id": "ZIA-SSL-BYPASS-FIN-DOMAINS",     "name": "SSL Bypass Finance",     "category": "SSL/TLS",      "action": "BYPASS_INSPECT", "domains_count": 47,    "registered_exception": False,                            "notes": "Conflicts with MIG-POL-002 §2.2 (PCI DSS 4.1)"},
    {"rule_id": "ZPA-AUTHPOL-ADMIN-MFA-ONLY",     "name": "MFA Admin-Only",         "category": "Identity",     "action": "MFA_REQUIRED", "scope": "Privileged Admins", "non_admin_users_unprotected": 4200,                "notes": "Conflicts with MIG-POL-002 §4.1 (PCI DSS 8.4)"},
    {"rule_id": "ZIA-IOT-MONITOR-ONLY-VLAN-19",   "name": "IoT Monitor",            "category": "IoT",           "action": "MONITOR", "vlan": 19, "devices": 43,           "notes": "Conflicts with MIG-POL-002 §5.1"},
    {"rule_id": "ZIA-DLP-PII-BLOCK-ALL-EXTERNAL", "name": "Blanket DLP PII",        "category": "DLP",           "action": "BLOCK", "exceptions": [],                            "notes": "Conflicts with MIG-POL-003 §2.1 and §2.2"},
    {"rule_id": "ZPA-GEO-RESTRICT-INDIA-US-ONLY", "name": "Geo Allow IN+US",        "category": "ZTNA",          "action": "ALLOW", "countries": ["IN", "US"],                   "notes": "Conflicts with MIG-POL-003 §4 and MIG-POL-005 §5"},
    {"rule_id": "ZIA-URLCAT-SOCIAL-BLOCK-ALL",    "name": "Social Block All",       "category": "URL Filtering","action": "BLOCK", "scope": "AllEmployees", "department_exceptions": [], "notes": "Conflicts with MIG-POL-001 §3"},
    # Compliant guard rules — the rule-pack must NOT flag these.
    {"rule_id": "ZIA-URLCAT-BOX-ALLOW-001",       "name": "Box.com Allow",          "category": "Cloud Storage", "action": "ALLOW", "scope": "AllUsers",           "domains": ["box.com"],                                   "notes": "Compliant with MIG-POL-001 §2.2"},
    {"rule_id": "ZIA-URLCAT-CHROME-ALLOW-002",    "name": "Chrome Allow",            "category": "Browser",      "action": "ALLOW", "scope": "AllUsers",           "apps": ["Chrome Enterprise"],                            "notes": "Compliant with MIG-POL-001 §4"},
    {"rule_id": "ZIA-SSL-INSPECT-HEALTHCARE-001", "name": "Healthcare SSL Inspect", "category": "SSL/TLS",      "action": "INSPECT","domains_count": 18, "registered_exception": True, "notes": "Compliant with MIG-POL-002 §2.2"},
]


# ── AWS Config snapshot (non-compliant resources + compliant guards) ───────────

AWS_CONFIG_RESOURCES = [
    # Non-compliant — emit findings UC07/08/09.
    {"resource_id": "alb-mig-prod-claims-api-001",    "type": "AWS::ElasticLoadBalancingV2::LoadBalancer", "compliance": "NON_COMPLIANT", "raw": {"security_group": "sg-mig-prod-alb-open", "ingress": "0.0.0.0/0:443", "waf_attached": False, "age_days": 47}, "notes": "Conflicts with MIG-POL-004 §2"},
    {"resource_id": "pcx-mig-prod-dev-001",            "type": "AWS::EC2::VPCPeeringConnection",            "compliance": "NON_COMPLIANT", "raw": {"prod_vpc": "vpc-mig-prod-001", "dev_vpc": "vpc-mig-dev-002", "age_days": 78}, "notes": "Conflicts with MIG-POL-004 §3"},
    {"resource_id": "mig-prod-claims-data-primary",    "type": "AWS::S3::Bucket",                            "compliance": "NON_COMPLIANT", "raw": {"replication_target": "eu-west-1", "pii_tier": 1, "age_days": 134}, "notes": "Conflicts with MIG-POL-003 §3 (NAIC MDL-668)"},
    # Compliant guards — must NOT be flagged.
    {"resource_id": "alb-mig-prod-api-002",            "type": "AWS::ElasticLoadBalancingV2::LoadBalancer", "compliance": "COMPLIANT",      "raw": {"waf_attached": True, "owasp_crs": "v4.0"}, "notes": "Compliant with MIG-POL-004 §2"},
    {"resource_id": "alb-mig-prod-portal-003",         "type": "AWS::ElasticLoadBalancingV2::LoadBalancer", "compliance": "COMPLIANT",      "raw": {"waf_attached": True, "owasp_crs": "v4.0"}, "notes": "Compliant with MIG-POL-004 §2"},
    {"resource_id": "tgw-mig-prod-prod-001",           "type": "AWS::EC2::TransitGateway",                   "compliance": "COMPLIANT",      "raw": {"segments": ["prod-east", "prod-west"]}, "notes": "Compliant with MIG-POL-004 §3"},
    {"resource_id": "mig-prod-customer-data-secondary","type": "AWS::S3::Bucket",                            "compliance": "COMPLIANT",      "raw": {"replication_target": "us-west-2", "pii_tier": 1}, "notes": "Compliant with MIG-POL-003 §3"},
]


# ── Render markdown for a policy ──────────────────────────────────────────────

def render_policy_md(p: Policy) -> str:
    lines = [
        f"# {p.doc_id} — {p.title}",
        "",
        f"**Document ID:** {p.doc_id}  ",
        f"**Version:** {p.version}  ",
        f"**Effective:** {p.effective}  ",
        f"**Owner:** {p.owner}  ",
        "",
        "---",
        "",
    ]
    for s in p.sections:
        lines.append(f"## §{s.number} {s.title}")
        lines.append("")
        lines.append(s.body)
        lines.append("")
    return "\n".join(lines)


# ── Disk writes ───────────────────────────────────────────────────────────────

def write_corpus(backup: bool) -> dict[str, Path]:
    """Write all markdown + JSON sources to disk. Returns a manifest of written files."""
    SOURCE.mkdir(parents=True, exist_ok=True)

    if backup:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        bak = ARCHIVE / ts
        bak.mkdir(parents=True, exist_ok=True)
        for p in BASELINE.glob("MIG-POL-*.pdf"):
            shutil.copy2(p, bak / p.name)
        print(f"  ✓ backed up existing PDFs → {bak.relative_to(REPO)}")

    written: dict[str, Path] = {}
    # MIG-POL-001..005 markdown
    for p in POLICIES:
        out = SOURCE / f"{p.doc_id}-{p.title.replace(' ', '-')}-{p.version}.md"
        out.write_text(render_policy_md(p), encoding="utf-8")
        written[p.doc_id] = out
        print(f"  ✓ wrote {out.relative_to(REPO)}")

    # Zscaler — single consolidated JSON
    zs_path = BASELINE / "zscaler" / "LM_ZIA_Rules_Cited.json"
    zs_path.parent.mkdir(parents=True, exist_ok=True)
    zs_path.write_text(json.dumps(ZSCALER_RULES, indent=2), encoding="utf-8")
    written["zscaler"] = zs_path
    print(f"  ✓ wrote {zs_path.relative_to(REPO)}")

    # AWS Config snapshot v2
    cfg_path = BASELINE / "aws-config" / "by-resource-type" / "AWS-Config-Snapshot-v2.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps({"resources": AWS_CONFIG_RESOURCES, "generated_at": datetime.now(timezone.utc).isoformat()}, indent=2), encoding="utf-8")
    written["awsconfig"] = cfg_path
    print(f"  ✓ wrote {cfg_path.relative_to(REPO)}")

    return written


def sync_to_s3() -> None:
    """aws s3 sync the markdown + JSON files to the processed bucket under /baseline/."""
    s3 = boto3.client("s3", region_name=REGION)
    bucket = PROCESSED_BUCKET
    prefix = "baseline/"
    # Sync MIG-POL markdown sources
    for p in POLICIES:
        local = SOURCE / f"{p.doc_id}-{p.title.replace(' ', '-')}-{p.version}.md"
        if not local.exists():
            continue
        key = f"{prefix}sharepoint/{local.name}"
        try:
            s3.upload_file(str(local), bucket, key, ExtraArgs={"ContentType": "text/markdown"})
            print(f"  ✓ uploaded s3://{bucket}/{key}")
        except ClientError as e:
            print(f"  ✗ upload failed {key}: {e}")
    # Zscaler + AWS Config
    for local, key in [
        (BASELINE / "zscaler" / "LM_ZIA_Rules_Cited.json",                              f"{prefix}zscaler/LM_ZIA_Rules_Cited.json"),
        (BASELINE / "aws-config" / "by-resource-type" / "AWS-Config-Snapshot-v2.json",  f"{prefix}aws-config/AWS-Config-Snapshot-v2.json"),
    ]:
        if not local.exists():
            continue
        try:
            s3.upload_file(str(local), bucket, key, ExtraArgs={"ContentType": "application/json"})
            print(f"  ✓ uploaded s3://{bucket}/{key}")
        except ClientError as e:
            print(f"  ✗ upload failed {key}: {e}")


def ingest_kb() -> None:
    """Start a Bedrock KB ingestion job and poll for COMPLETE."""
    bra = boto3.client("bedrock-agent", region_name=REGION)
    try:
        resp = bra.start_ingestion_job(knowledgeBaseId=KB_ID, dataSourceId=KB_DS)
    except ClientError as e:
        print(f"  ✗ ingestion start failed: {e}")
        return
    job_id = resp["ingestionJob"]["ingestionJobId"]
    print(f"  ✓ started ingestion job {job_id}; polling…")
    for _ in range(60):  # max 10 min @ 10s
        time.sleep(10)
        info = bra.get_ingestion_job(knowledgeBaseId=KB_ID, dataSourceId=KB_DS, ingestionJobId=job_id)
        status = info["ingestionJob"]["status"]
        print(f"    status: {status}")
        if status in ("COMPLETE", "FAILED", "STOPPED"):
            if status == "COMPLETE":
                stats = info["ingestionJob"].get("statistics", {})
                print(f"  ✓ ingestion complete: {stats}")
            else:
                print(f"  ✗ ingestion ended with status={status}")
            return
    print("  ✗ ingestion job timeout (>10 min); inspect with `aws bedrock-agent get-ingestion-job …`")


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backup",  action="store_true", help="Copy existing PDFs to BaselineFiles/_archive/<ts>/")
    parser.add_argument("--sync",    action="store_true", help="aws s3 sync corpus to the processed bucket")
    parser.add_argument("--ingest",  action="store_true", help="Trigger Bedrock KB ingestion job and poll for COMPLETE")
    args = parser.parse_args()

    print(f"Generating ARBITER baseline corpus → {BASELINE.relative_to(REPO)}")
    print()
    write_corpus(backup=args.backup)
    print()
    if args.sync:
        print(f"Syncing to s3://{PROCESSED_BUCKET}/baseline/")
        print()
        sync_to_s3()
        print()
    if args.ingest:
        print(f"Triggering Bedrock KB ingestion (KB={KB_ID}, DS={KB_DS})")
        print()
        ingest_kb()
        print()
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
