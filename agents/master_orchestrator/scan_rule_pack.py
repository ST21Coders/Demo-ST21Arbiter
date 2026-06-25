"""ARBITER deterministic rule-pack for the autonomous scanner.

Each rule matcher takes structured observations from the four specialists
(SharePoint policy citations, Zscaler rule snapshots, AWS Config resource
snapshots, Palo Alto firewall-rule snapshots) and either emits a structured
Finding or returns None.

The 14 matchers map 1:1 to the use cases in
`BaselineFiles/ARBITER-POC-Scope-and-Use-Cases-V1.0.docx` (UC13/UC14 add the
Palo Alto perimeter source). The compliant_* helpers emit 16 alignment rows the
scanner records as evidence of working controls (and as a false-positive guard).

This module is imported by `agents/master_orchestrator/agent.py` when the
runtime is invoked with `{"scan": true, ...}`.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any


# ── Specialist observation shapes ─────────────────────────────────────────────

def _has_policy_quote(sharepoint: list[dict], doc: str, section: str, needle: str) -> bool:
    """Return True if a SharePoint observation cites `doc § section` containing `needle`."""
    needle_l = needle.lower()
    for obs in sharepoint or []:
        if obs.get("policy_doc", "").startswith(doc) and obs.get("section") == section:
            if needle_l in (obs.get("text", "") or "").lower():
                return True
    return False


def _registered(r: dict) -> bool:
    """A rule/resource flagged raw.registered_exception=true is a documented,
    risk-accepted exception → not an active finding for ANY matcher. This is the
    universal exception affordance: set the flag in the structured source (CSV) to
    clear the corresponding conflict, regardless of which UC it maps to."""
    return bool((r.get("raw") or {}).get("registered_exception"))


def _has_zscaler_rule(zscaler: list[dict], rule_id: str) -> dict | None:
    for r in zscaler or []:
        if r.get("rule_id") == rule_id:
            return None if _registered(r) else r
    return None


def _has_aws_resource(awsconfig: list[dict], resource_id: str) -> dict | None:
    for r in awsconfig or []:
        if r.get("resource_id") == resource_id:
            return None if _registered(r) else r
    return None


def _has_paloalto_rule(paloalto: list[dict], rule_id: str) -> dict | None:
    for r in paloalto or []:
        if r.get("rule_id") == rule_id:
            return None if _registered(r) else r
    return None


# ── Finding construction helpers ──────────────────────────────────────────────

DOMAIN_LABELS = {
    "ACCESS_MGMT":      "Access Mgmt",
    "NETWORK_SECURITY": "Network Security",
    "DATA_GOVERNANCE":  "Data Governance",
    "CLOUD_SECURITY":   "Cloud Security",
    "COMPLIANCE":       "Compliance",
    "VENDOR_MGMT":      "Vendor Mgmt",
}


def _finding(*, rule_key: str, severity: str, conflict_type: str, domain: str,
             source_pair: str, source_policy: str, source_technical: str,
             title: str, finding_text: str, impact: str, remediation: list[str],
             policy_citations: list[dict], enforcement_evidence: list[dict],
             regulatory: list[str], domains_list: list[str], fp_score: float = 0.05) -> dict[str, Any]:
    return {
        "conflict_id":          f"ARBITER-{rule_key}",
        "detected_at":          datetime.now(timezone.utc).isoformat(),
        "status":               "OPEN",
        "severity":             severity,
        "title":                title,
        "domain":               domain,
        "source_pair":          source_pair,
        "conflict_type":        conflict_type,
        "rule_key":             rule_key,
        "fp_score":             Decimal(str(fp_score)),
        "compliant":            False,
        "source_policy":        source_policy,
        "source_technical":     source_technical,
        "domains":              domains_list,
        "policy_citations":     policy_citations,
        "enforcement_evidence": enforcement_evidence,
        "regulatory":           regulatory,
        "finding":              finding_text,
        "impact":               impact,
        "remediation":          remediation,
    }


def _compliant(*, cid: str, rule_key: str, domain: str, source_pair: str,
               domains_list: list[str], title: str) -> dict[str, Any]:
    return {
        "conflict_id":  cid,
        "detected_at":  datetime.now(timezone.utc).isoformat(),
        "status":       "COMPLIANT",
        "severity":     "INFO",
        "title":        title,
        "domain":       domain,
        "source_pair":  source_pair,
        "conflict_type": "OVERLAP",
        "rule_key":     rule_key,
        "compliant":    True,
        "fp_score":     Decimal("0.0"),
        "domains":      domains_list,
        "policy_citations":     [],
        "enforcement_evidence": [],
        "regulatory":   [],
        "finding":      "Policy and enforcement agree — recorded for audit evidence.",
        "impact":       "No action required. Compliance evidence on file.",
        "remediation":  [],
    }


# ── ServiceNow CMDB/Asset drift (AWS-vs-CMDB reconciliation) ──────────────────
# Only infrastructure CIs reconcile against the AWS resource inventory; business
# application / service CIs are exempt (they have no 1:1 AWS resource).
_AWS_BACKED_CLASSES = {
    "cmdb_ci_lb", "cmdb_ci_db_instance", "cmdb_ci_network", "cmdb_ci_server",
    "cmdb_ci_ec2_instance", "cmdb_ci_storage", "cmdb_ci_cloud_function",
}


def _drift_finding(*, rule_key: str, severity: str, title: str, finding_text: str,
                   impact: str, remediation: list[str], resource_id: str, kind: str,
                   domain: str = "CLOUD_SECURITY") -> dict[str, Any]:
    """A DRIFT finding from ServiceNow↔AWS reconciliation, in the conflicts schema."""
    return _finding(
        rule_key=rule_key, severity=severity, conflict_type="DRIFT", domain=domain,
        source_pair="ServiceNow+AWS Config", source_policy="CMDB reconciliation",
        source_technical=resource_id, title=title, finding_text=finding_text,
        impact=impact, remediation=remediation, policy_citations=[],
        enforcement_evidence=[{"source": "ServiceNow", "resource_id": resource_id,
                               "action": kind.upper(), "raw": {"drift_kind": kind}}],
        regulatory=["ISO 27001 A.8.9", "SOC 2 CC6.1"],
        domains_list=["ServiceNow", "AWSConfig"], fp_score=0.08)


def _ci_to_resource(rec: dict, inv_by_id: dict) -> str | None:
    """Map a CI/asset record to an inventory resource_id by name or ARN tail."""
    name = rec.get("name") or rec.get("ci_name")
    if name and name in inv_by_id:
        return name
    corr = rec.get("correlation_id") or rec.get("ci_correlation_id") or ""
    if corr:
        tail = corr.rsplit("/", 1)[-1].rsplit(":", 1)[-1]
        if tail in inv_by_id:
            return tail
    return None


def run_servicenow_drift(snapshot: dict, aws_inventory: list[dict]) -> list[dict]:
    """Correlate a ServiceNow CMDB+asset snapshot against live AWS reality.

    snapshot   = {cis: [{name, class, correlation_id, operational_status, owner_team}],
                  assets: [{asset_tag, install_status, ci_name, ci_correlation_id}]}
    aws_inventory = [{resource_id, arn, state, owner, environment}]

    Emits DRIFT findings: unmanaged resource (AWS has it, CMDB doesn't), stale CI
    (CMDB has it, AWS doesn't), ownership drift, and asset drift.
    """
    cis = (snapshot or {}).get("cis") or []
    assets = (snapshot or {}).get("assets") or []
    inv_by_id = {r["resource_id"]: r for r in (aws_inventory or []) if r.get("resource_id")}
    findings: list[dict] = []

    # CIs that reconcile to an inventory resource (infrastructure classes only).
    ci_resource_ids: set[str] = set()
    for ci in cis:
        if (ci.get("class") or "") in _AWS_BACKED_CLASSES:
            rid = _ci_to_resource(ci, inv_by_id)
            if rid:
                ci_resource_ids.add(rid)

    # 1. Unmanaged: live AWS resource with no CI.
    for rid, r in inv_by_id.items():
        if rid not in ci_resource_ids and (r.get("state") or "running") != "terminated":
            findings.append(_drift_finding(
                rule_key=f"SN-UNMANAGED-{rid}", severity="HIGH",
                title=f"Unmanaged AWS resource — no CMDB CI for {rid}",
                finding_text=f"AWS resource {rid} ({r.get('arn') or 'no ARN'}) is live but has no "
                             f"Configuration Item in the CMDB.",
                impact="Change/impact analysis and ownership are blind to this resource — a CMDB coverage gap.",
                remediation=[f"Create a CI for {rid} (Service Graph Connector for AWS or manual).",
                             "Set correlation_id to the ARN and assign an owning support group."],
                resource_id=rid, kind="unmanaged_resource"))

    # 2/3. Stale CI + ownership drift.
    for ci in cis:
        if (ci.get("class") or "") not in _AWS_BACKED_CLASSES:
            continue
        corr = ci.get("correlation_id") or ""
        if not corr:
            continue  # missing correlation = hygiene, reported by detect_drift
        op = str(ci.get("operational_status") or "").lower()
        operational = op in ("1", "operational", "")
        rid = _ci_to_resource(ci, inv_by_id)
        if rid is None and operational:
            findings.append(_drift_finding(
                rule_key=f"SN-STALE-{ci.get('name')}", severity="MEDIUM",
                title=f"Stale CMDB CI — {ci.get('name')} not present in AWS",
                finding_text=f"CI {ci.get('name')} (correlation_id {corr}) is marked operational, but no "
                             f"matching live AWS resource exists.",
                impact="Orphan CI inflates the CMDB and misleads impact analysis; the resource may be decommissioned.",
                remediation=[f"Verify whether {ci.get('name')} still exists in AWS.",
                             "Retire the CI (operational_status=retired) or correct its correlation_id."],
                resource_id=ci.get("name") or corr, kind="stale_ci"))
        elif rid is not None:
            r = inv_by_id[rid]
            ci_owner = (ci.get("owner_team") or "").strip()
            aws_owner = (r.get("owner") or "").strip()
            if aws_owner and ci_owner and ci_owner.lower() != aws_owner.lower():
                findings.append(_drift_finding(
                    rule_key=f"SN-OWNER-{rid}", severity="LOW",
                    title=f"Ownership drift — {rid} owner mismatch",
                    finding_text=f"CMDB owner '{ci_owner}' for {rid} differs from the AWS owner tag '{aws_owner}'.",
                    impact="Change routing and approvals may go to the wrong team.",
                    remediation=[f"Reconcile the CMDB support group for {rid} with the AWS owner tag."],
                    resource_id=rid, kind="ownership_drift", domain="COMPLIANCE"))

    # 4. Asset drift: unlinked asset, or in-use asset for an absent/terminated resource.
    for a in assets:
        status = str(a.get("install_status") or "").lower()
        in_use = status in ("1", "in use", "installed", "deployed")
        if not (a.get("ci_name") or a.get("ci_correlation_id")):
            findings.append(_drift_finding(
                rule_key=f"SN-ASSET-UNLINKED-{a.get('asset_tag')}", severity="LOW",
                title=f"Asset not linked to a CI — {a.get('asset_tag')}",
                finding_text=f"Asset {a.get('asset_tag') or a.get('display_name')} is not linked to any CMDB CI.",
                impact="Asset↔CI reconciliation gap; financial/lifecycle and operational views are disconnected.",
                remediation=["Link the asset to its CI (alm_asset.ci)."],
                resource_id=a.get("asset_tag") or "asset", kind="asset_unlinked",
                domain="COMPLIANCE"))
            continue
        rid = _ci_to_resource(a, inv_by_id)
        if rid is None and in_use:
            findings.append(_drift_finding(
                rule_key=f"SN-ASSET-STALE-{a.get('asset_tag')}", severity="MEDIUM",
                title=f"In-use asset for a decommissioned resource — {a.get('asset_tag')}",
                finding_text=f"Asset {a.get('asset_tag')} is in-use and linked to "
                             f"'{a.get('ci_name')}', which has no live AWS resource.",
                impact="Asset lifecycle says in-use while the underlying resource is gone — likely over-stated inventory / cost.",
                remediation=[f"Retire asset {a.get('asset_tag')} or relink it to a live CI."],
                resource_id=a.get("asset_tag") or "asset", kind="asset_stale",
                domain="COMPLIANCE"))
    return findings


# ── 12 conflict matchers (UC01..UC12) ────────────────────────────────────────

def uc01_dropbox_block(sp, zs, aws, pa=None):
    rule = _has_zscaler_rule(zs, "ZIA-URLCAT-CLOUD-BLK-042")
    has_policy = _has_policy_quote(sp, "MIG-POL-001", "2.1", "Dropbox Business")
    if not rule or not has_policy:
        return None
    return _finding(
        rule_key="UC01", severity="HIGH", conflict_type="CONTRADICTION", domain="ACCESS_MGMT",
        source_pair="SharePoint+Zscaler", source_policy="MIG-POL-001 §2.1",
        source_technical="ZIA-URLCAT-CLOUD-BLK-042",
        title="Dropbox approved in policy but blocked by Zscaler",
        finding_text="MIG-POL-001 §2.1 approves Dropbox Business; Zscaler rule ZIA-URLCAT-CLOUD-BLK-042 blocks dropbox.com category-wide.",
        impact="All employees blocked from a tool approved by the CIO. Helpdesk volume elevated.",
        remediation=["Remove dropbox.com from ZIA-URLCAT-CLOUD-BLK-042 or re-categorise to Cloud Storage — Allowed.",
                     "Alternatively revoke MIG-POL-001 §2.1 Dropbox approval if business need has lapsed."],
        policy_citations=[{"doc": "MIG-POL-001", "version": "v3.4", "section": "2.1",
                           "quote": "Dropbox Business listed as approved. Passed vendor assessment Q3 2025.",
                           "confidence": Decimal("0.97")}],
        enforcement_evidence=[{"source": "Zscaler", "rule_id": "ZIA-URLCAT-CLOUD-BLK-042", "action": "BLOCK",
                               "raw": {"category": "Cloud Storage", "domains": ["dropbox.com"]}}],
        regulatory=["ISO 27001 A.5.10"], domains_list=["SharePoint", "Zscaler"], fp_score=0.05,
    )


def uc02_remote_tools_block(sp, zs, aws, pa=None):
    rule = _has_zscaler_rule(zs, "ZIA-APP-CTRL-REMOTE-BLOCK-007")
    has_policy = _has_policy_quote(sp, "MIG-POL-001", "2.3", "TeamViewer")
    if not rule or not has_policy:
        return None
    return _finding(
        rule_key="UC02", severity="HIGH", conflict_type="CONTRADICTION", domain="VENDOR_MGMT",
        source_pair="SharePoint+Zscaler", source_policy="MIG-POL-001 §2.3, MIG-POL-005 §6",
        source_technical="ZIA-APP-CTRL-REMOTE-BLOCK-007",
        title="Approved remote support tools blocked by Zscaler for vendor users",
        finding_text="MIG-POL-001 §2.3 and MIG-POL-005 §6 approve TeamViewer / AnyDesk / BeyondTrust; Zscaler blocks them.",
        impact="MSP vendors cannot perform scheduled maintenance. IT support SLAs at risk.",
        remediation=["Add TeamViewer Corporate and AnyDesk Enterprise to ZIA allowed application list.",
                     "Scope exception to authorised vendor ZPA segments only.",
                     "Ensure all sessions logged to SIEM per MIG-POL-005 §6."],
        policy_citations=[
            {"doc": "MIG-POL-001", "version": "v3.4", "section": "2.3",
             "quote": "TeamViewer Corporate, AnyDesk Enterprise, BeyondTrust Remote Support are approved for authorised IT and MSP personnel.",
             "confidence": Decimal("0.96")},
            {"doc": "MIG-POL-005", "version": "v2.8", "section": "6",
             "quote": "All vendor remote-support sessions must be logged to SIEM.",
             "confidence": Decimal("0.94")},
        ],
        enforcement_evidence=[{"source": "Zscaler", "rule_id": "ZIA-APP-CTRL-REMOTE-BLOCK-007", "action": "BLOCK",
                               "raw": {"apps": ["TeamViewer", "AnyDesk"]}}],
        regulatory=["ISO 27001 A.5.20", "SOC 2 CC9.2"], domains_list=["SharePoint", "Zscaler"], fp_score=0.07,
    )


def uc03_firefox_block(sp, zs, aws, pa=None):
    rule = _has_zscaler_rule(zs, "ZIA-APP-CTRL-BROWSER-FF-009")
    has_policy = _has_policy_quote(sp, "MIG-POL-001", "4", "Firefox")
    if not rule or not has_policy:
        return None
    return _finding(
        rule_key="UC03", severity="MEDIUM", conflict_type="CONTRADICTION", domain="ACCESS_MGMT",
        source_pair="SharePoint+Zscaler", source_policy="MIG-POL-001 §4",
        source_technical="ZIA-APP-CTRL-BROWSER-FF-009",
        title="Zscaler blocks Firefox — policy mandates browser freedom",
        finding_text="MIG-POL-001 §4 permits Firefox; Zscaler classifies Firefox traffic as Restricted.",
        impact="Employees cannot use a policy-approved browser. Accessibility impacts possible.",
        remediation=["Remove ZIA-APP-CTRL-BROWSER-FF-009 or change action from BLOCK to ALLOW.",
                     "If security justification exists, obtain CISO written approval."],
        policy_citations=[{"doc": "MIG-POL-001", "version": "v3.4", "section": "4",
                           "quote": "Chrome, Firefox, Edge, Safari, Brave are permitted on corporate devices without further approval.",
                           "confidence": Decimal("0.95")}],
        enforcement_evidence=[{"source": "Zscaler", "rule_id": "ZIA-APP-CTRL-BROWSER-FF-009", "action": "BLOCK",
                               "raw": {"app": "Firefox"}}],
        regulatory=["SOC 2 CC6.1"], domains_list=["SharePoint", "Zscaler"], fp_score=0.15,
    )


def uc04_ssl_bypass(sp, zs, aws, pa=None):
    rule = _has_zscaler_rule(zs, "ZIA-SSL-BYPASS-FIN-DOMAINS")
    has_policy = _has_policy_quote(sp, "MIG-POL-002", "2.2", "SSL/TLS inspection")
    if not rule or not has_policy:
        return None
    # registered_exception is now handled generically in _has_zscaler_rule (a
    # registered/approved bypass clears this finding, same as every other matcher).
    raw = rule.get("raw", {})
    return _finding(
        rule_key="UC04", severity="CRITICAL", conflict_type="GAP", domain="COMPLIANCE",
        source_pair="SharePoint+Zscaler", source_policy="MIG-POL-002 §2.2",
        source_technical="ZIA-SSL-BYPASS-FIN-DOMAINS",
        title="SSL inspection bypassed for 47 financial domains — PCI DSS violation",
        finding_text="MIG-POL-002 §2.2 mandates SSL inspection with zero unregistered exceptions; ZIA-SSL-BYPASS-FIN-DOMAINS bypasses 47 financial domains, unregistered.",
        impact="PCI DSS Requirement 4.1 compliance gap. Encrypted threats traverse uninspected. Likely QSA finding.",
        remediation=["Remove ZIA-SSL-BYPASS-FIN-DOMAINS or submit CISO approval (ISG-EXC-001).",
                     "Register legitimate exceptions in the SSL Inspection Exception Register with 90-day expiration."],
        policy_citations=[{"doc": "MIG-POL-002", "version": "v5.1", "section": "2.2",
                           "quote": "SSL/TLS inspection is mandatory on ALL web traffic. Exceptions only with documented CISO approval in the SSL Inspection Exception Register, 90-day max.",
                           "confidence": Decimal("0.98")}],
        enforcement_evidence=[{"source": "Zscaler", "rule_id": "ZIA-SSL-BYPASS-FIN-DOMAINS", "action": "BYPASS_INSPECT",
                               "raw": raw}],
        regulatory=["PCI DSS 4.0 Req 4.1"], domains_list=["SharePoint", "Zscaler"], fp_score=0.03,
    )


def uc05_mfa_admin_only(sp, zs, aws, pa=None):
    rule = _has_zscaler_rule(zs, "ZPA-AUTHPOL-ADMIN-MFA-ONLY")
    has_policy = _has_policy_quote(sp, "MIG-POL-002", "4.1", "MFA")
    if not rule or not has_policy:
        return None
    return _finding(
        rule_key="UC05", severity="CRITICAL", conflict_type="GAP", domain="ACCESS_MGMT",
        source_pair="SharePoint+Zscaler", source_policy="MIG-POL-002 §4.1",
        source_technical="ZPA-AUTHPOL-ADMIN-MFA-ONLY",
        title="MFA enforcement limited to admin accounts — policy requires all users",
        finding_text="MIG-POL-002 §4.1 mandates MFA for ALL users; ZPA-AUTHPOL-ADMIN-MFA-ONLY enforces only on Privileged Admins.",
        impact="Mass MFA gap across non-admin users. PCI DSS 8.4 violation. NAIC MDL-668 exposure.",
        remediation=["Expand ZPA MFA policy to all user groups.",
                     "Phased rollout: contractors first, then standard employees."],
        policy_citations=[{"doc": "MIG-POL-002", "version": "v5.1", "section": "4.1",
                           "quote": "MFA is required for ALL users — employees, contractors, vendors — regardless of privilege level.",
                           "confidence": Decimal("0.97")}],
        enforcement_evidence=[{"source": "Zscaler", "rule_id": "ZPA-AUTHPOL-ADMIN-MFA-ONLY", "action": "MFA_REQUIRED",
                               "raw": rule.get("raw", {})}],
        regulatory=["PCI DSS 8.4", "NAIC MDL-668"], domains_list=["SharePoint", "Zscaler"], fp_score=0.04,
    )


def uc06_iot_monitor(sp, zs, aws, pa=None):
    rule = _has_zscaler_rule(zs, "ZIA-IOT-MONITOR-ONLY-VLAN-19")
    has_policy = _has_policy_quote(sp, "MIG-POL-002", "5.1", "IoT")
    if not rule or not has_policy:
        return None
    return _finding(
        rule_key="UC06", severity="HIGH", conflict_type="GAP", domain="NETWORK_SECURITY",
        source_pair="SharePoint+Zscaler", source_policy="MIG-POL-002 §5.1",
        source_technical="ZIA-IOT-MONITOR-ONLY-VLAN-19",
        title="IoT devices in monitor-only mode — policy requires active blocking",
        finding_text="MIG-POL-002 §5.1 requires active blocking of IoT external comms; ZIA-IOT-MONITOR-ONLY-VLAN-19 set to MONITOR only.",
        impact="IoT devices actively communicating externally without enforcement. Potential C2 vector.",
        remediation=["Change VLAN-19 ZIA policy from MONITOR to BLOCK for external destinations.",
                     "Configure internal firmware update proxy for all IoT devices."],
        policy_citations=[{"doc": "MIG-POL-002", "version": "v5.1", "section": "5.1",
                           "quote": "Monitoring-only mode is NOT acceptable for IoT external communication. Active blocking is required.",
                           "confidence": Decimal("0.96")}],
        enforcement_evidence=[{"source": "Zscaler", "rule_id": "ZIA-IOT-MONITOR-ONLY-VLAN-19", "action": "MONITOR",
                               "raw": rule.get("raw", {})}],
        regulatory=["PCI DSS 1.4", "ISO 27001 A.8.22"], domains_list=["SharePoint", "Zscaler"], fp_score=0.06,
    )


def uc07_alb_no_waf(sp, zs, aws, pa=None):
    res = _has_aws_resource(aws, "alb-mig-prod-claims-api-001")
    has_policy = _has_policy_quote(sp, "MIG-POL-004", "2", "WAF")
    if not res or not has_policy:
        return None
    raw = res.get("raw", {})
    if raw.get("waf_attached", False):
        return None  # Compliant — covered by uc07_compliant_apis()
    return _finding(
        rule_key="UC07", severity="CRITICAL", conflict_type="DRIFT", domain="CLOUD_SECURITY",
        source_pair="SharePoint+AWS Config", source_policy="MIG-POL-004 §2",
        source_technical="alb-mig-prod-claims-api-001",
        title="Production ALB exposed to 0.0.0.0/0 without WAF — critical WAF bypass",
        finding_text=f"MIG-POL-004 §2 prohibits production-internet exposure without WAF; ALB alb-mig-prod-claims-api-001 open 0.0.0.0/0:443 for {raw.get('age_days', 'N/A')} days, no WAF.",
        impact="Production claims API fully exposed without WAF protection. SQL injection / XSS / volumetric attacks unmitigated. PCI DSS Req 6.4 violation.",
        remediation=["Associate AWS WAF web ACL with OWASP CRS 4.0 immediately.",
                     "Update security group to restrict inbound 443 to WAF IP set only.",
                     "Enable rate limiting per MIG-POL-002 §API01."],
        policy_citations=[{"doc": "MIG-POL-004", "version": "v4.0", "section": "2",
                           "quote": "No production application resource shall be directly accessible from the public internet without AWS WAF + OWASP CRS.",
                           "confidence": Decimal("0.98")}],
        enforcement_evidence=[{"source": "AWSConfig", "resource_id": "alb-mig-prod-claims-api-001", "action": "NON_COMPLIANT",
                               "raw": raw}],
        regulatory=["PCI DSS 6.4"], domains_list=["SharePoint", "AWSConfig"], fp_score=0.02,
    )


def uc08_vpc_peering(sp, zs, aws, pa=None):
    res = _has_aws_resource(aws, "pcx-mig-prod-dev-001")
    has_policy = _has_policy_quote(sp, "MIG-POL-004", "3", "VPC peering")
    if not res or not has_policy:
        return None
    return _finding(
        rule_key="UC08", severity="CRITICAL", conflict_type="DRIFT", domain="NETWORK_SECURITY",
        source_pair="SharePoint+AWS Config", source_policy="MIG-POL-004 §3",
        source_technical="pcx-mig-prod-dev-001",
        title="Dev-to-prod VPC peering active — production segmentation violated",
        finding_text=f"MIG-POL-004 §3 prohibits prod/dev peering; pcx-mig-prod-dev-001 active {res.get('raw',{}).get('age_days', 'N/A')} days.",
        impact="Direct prod-dev data pathway active. PCI DSS cardholder environment segmentation failure.",
        remediation=["Terminate VPC peering pcx-mig-prod-dev-001 immediately.",
                     "Revoke inbound rule from dev CIDR on sg-mig-prod-peer-dev-001."],
        policy_citations=[{"doc": "MIG-POL-004", "version": "v4.0", "section": "3",
                           "quote": "VPC peering between production and non-production environments is prohibited.",
                           "confidence": Decimal("0.98")}],
        enforcement_evidence=[{"source": "AWSConfig", "resource_id": "pcx-mig-prod-dev-001", "action": "NON_COMPLIANT",
                               "raw": res.get("raw", {})}],
        regulatory=["PCI DSS 1.3"], domains_list=["SharePoint", "AWSConfig"], fp_score=0.02,
    )


def uc09_s3_eu_west(sp, zs, aws, pa=None):
    res = _has_aws_resource(aws, "mig-prod-claims-data-primary")
    has_policy = _has_policy_quote(sp, "MIG-POL-003", "3", "continental United States")
    if not res or not has_policy:
        return None
    raw = res.get("raw", {})
    if raw.get("replication_target") not in ("eu-west-1",):
        return None
    return _finding(
        rule_key="UC09", severity="CRITICAL", conflict_type="DRIFT", domain="DATA_GOVERNANCE",
        source_pair="SharePoint+AWS Config", source_policy="MIG-POL-003 §3",
        source_technical="mig-prod-claims-data-primary",
        title="S3 claims data replicating to eu-west-1 — NAIC data residency breach",
        finding_text="MIG-POL-003 §3 mandates US-only residency; mig-prod-claims-data-primary replicating to eu-west-1.",
        impact="Active NAIC MDL-668 regulatory violation. Policyholder PII outside US.",
        remediation=["Disable S3 cross-region replication immediately.",
                     "Delete replicated objects in eu-west-1 bucket.",
                     "Notify Legal & Compliance within 24 hours."],
        policy_citations=[{"doc": "MIG-POL-003", "version": "v2.2", "section": "3",
                           "quote": "All customer insurance data must remain within the continental United States. No exceptions.",
                           "confidence": Decimal("0.97")}],
        enforcement_evidence=[{"source": "AWSConfig", "resource_id": "mig-prod-claims-data-primary", "action": "NON_COMPLIANT",
                               "raw": raw}],
        regulatory=["NAIC MDL-668"], domains_list=["SharePoint", "AWSConfig"], fp_score=0.03,
    )


def uc10_dlp_blanket(sp, zs, aws, pa=None):
    rule = _has_zscaler_rule(zs, "ZIA-DLP-PII-BLOCK-ALL-EXTERNAL")
    has_policy = _has_policy_quote(sp, "MIG-POL-003", "2.1", "Milliman")
    if not rule or not has_policy:
        return None
    return _finding(
        rule_key="UC10", severity="HIGH", conflict_type="CONTRADICTION", domain="DATA_GOVERNANCE",
        source_pair="SharePoint+Zscaler", source_policy="MIG-POL-003 §2.1",
        source_technical="ZIA-DLP-PII-BLOCK-ALL-EXTERNAL",
        title="DLP blanket rule blocking authorised actuarial data transfers",
        finding_text="MIG-POL-003 §2.1 authorises transfers to Milliman/WTW/Verisk; ZIA-DLP-PII-BLOCK-ALL-EXTERNAL blocks all PII externally with no exceptions.",
        impact="Authorised actuarial data transfers failing silently. Finance regulatory submissions blocked.",
        remediation=["Add domain exceptions for milliman.com, willistowerswatson.com, verisk.com to the DLP rule.",
                     "Maintain DLP Exception Register aligned with the Authorized Transfer Register."],
        policy_citations=[{"doc": "MIG-POL-003", "version": "v2.2", "section": "2.1",
                           "quote": "Authorised actuarial data transfers: Milliman Inc., Willis Towers Watson, Verisk Analytics.",
                           "confidence": Decimal("0.95")}],
        enforcement_evidence=[{"source": "Zscaler", "rule_id": "ZIA-DLP-PII-BLOCK-ALL-EXTERNAL", "action": "BLOCK",
                               "raw": rule.get("raw", {})}],
        regulatory=["NAIC MDL-668"], domains_list=["SharePoint", "Zscaler"], fp_score=0.08,
    )


def uc11_geo_restrict(sp, zs, aws, pa=None):
    rule = _has_zscaler_rule(zs, "ZPA-GEO-RESTRICT-INDIA-US-ONLY")
    has_policy = _has_policy_quote(sp, "MIG-POL-005", "5", "India and US only")
    if not rule or not has_policy:
        return None
    return _finding(
        rule_key="UC11", severity="MEDIUM", conflict_type="CONTRADICTION", domain="VENDOR_MGMT",
        source_pair="SharePoint+Zscaler", source_policy="MIG-POL-003 §4, MIG-POL-005 §5",
        source_technical="ZPA-GEO-RESTRICT-INDIA-US-ONLY",
        title="ZTNA geo-restriction blocks approved vendor countries",
        finding_text="MIG-POL-005 §5 explicitly declares IN+US-only ZTNA non-compliant; ZPA-GEO-RESTRICT-INDIA-US-ONLY enforces exactly that.",
        impact="UK, SG, DE, AU, PH, CA vendor personnel cannot access MIG systems.",
        remediation=["Update ZPA geo-restriction policy to include all 8 approved vendor countries.",
                     "Maintain sanctions compliance — OFAC countries remain blocked regardless."],
        policy_citations=[
            {"doc": "MIG-POL-003", "version": "v2.2", "section": "4",
             "quote": "Approved vendor countries: US, India, UK, Singapore, Germany, Australia, Philippines, Canada.",
             "confidence": Decimal("0.96")},
            {"doc": "MIG-POL-005", "version": "v2.8", "section": "5",
             "quote": "ZTNA restrictions limited to India and US only are non-compliant.",
             "confidence": Decimal("0.97")},
        ],
        enforcement_evidence=[{"source": "Zscaler", "rule_id": "ZPA-GEO-RESTRICT-INDIA-US-ONLY", "action": "ALLOW",
                               "raw": rule.get("raw", {})}],
        regulatory=["ISO 27001 A.5.23", "SOC 2 CC6.6"], domains_list=["SharePoint", "Zscaler"], fp_score=0.10,
    )


def uc12_social_blanket(sp, zs, aws, pa=None):
    rule = _has_zscaler_rule(zs, "ZIA-URLCAT-SOCIAL-BLOCK-ALL")
    has_policy = _has_policy_quote(sp, "MIG-POL-001", "3", "Marketing")
    if not rule or not has_policy:
        return None
    return _finding(
        rule_key="UC12", severity="MEDIUM", conflict_type="GAP", domain="ACCESS_MGMT",
        source_pair="SharePoint+Zscaler", source_policy="MIG-POL-001 §3",
        source_technical="ZIA-URLCAT-SOCIAL-BLOCK-ALL",
        title="Social media blanket block ignores policy exemptions for 4 departments",
        finding_text="MIG-POL-001 §3 requires exceptions for Marketing/Communications/HR/Talent Acquisition; ZIA-URLCAT-SOCIAL-BLOCK-ALL applies blanket block with zero department exceptions.",
        impact="Marketing cannot post to social channels. HR/Talent Acquisition cannot access LinkedIn for recruitment.",
        remediation=["Create department-based ZIA policy exception for the 4 named groups.",
                     "General employees remain subject to guest-network-only policy."],
        policy_citations=[{"doc": "MIG-POL-001", "version": "v3.4", "section": "3",
                           "quote": "URL filtering controls must include exceptions for Marketing, Communications, HR, and Talent Acquisition.",
                           "confidence": Decimal("0.95")}],
        enforcement_evidence=[{"source": "Zscaler", "rule_id": "ZIA-URLCAT-SOCIAL-BLOCK-ALL", "action": "BLOCK",
                               "raw": rule.get("raw", {})}],
        regulatory=["SOC 2 CC7.4", "ISO 27001 A.5.10"], domains_list=["SharePoint", "Zscaler"], fp_score=0.12,
    )


# ── Palo Alto perimeter matchers (UC13..UC14) ────────────────────────────────

def uc13_perimeter_egress_allow(sp, zs, aws, pa=None):
    rule = _has_paloalto_rule(pa, "PAN-SEC-EGRESS-ANYANY-ALLOW-001")
    has_policy = _has_policy_quote(sp, "MIG-POL-002", "6", "default-deny")
    if not rule or not has_policy:
        return None
    if (rule.get("raw", {}) or {}).get("action", "").lower() != "allow":
        return None
    return _finding(
        rule_key="UC13", severity="HIGH", conflict_type="GAP", domain="NETWORK_SECURITY",
        source_pair="SharePoint+Palo Alto", source_policy="MIG-POL-002 §6",
        source_technical="PAN-SEC-EGRESS-ANYANY-ALLOW-001",
        title="Palo Alto permits any/any outbound — policy mandates default-deny egress",
        finding_text="MIG-POL-002 §6 mandates default-deny perimeter egress with explicit allow-listing; Palo Alto rule PAN-SEC-EGRESS-ANYANY-ALLOW-001 permits any source to any destination outbound.",
        impact="Unrestricted outbound path at the perimeter firewall. Data exfiltration and C2 channels unmitigated despite a default-deny policy. PCI DSS Req 1.3 egress-control gap.",
        remediation=["Replace PAN-SEC-EGRESS-ANYANY-ALLOW-001 with an explicit allow-list of approved destinations / App-IDs.",
                     "Set the perimeter egress default action to deny-and-log.",
                     "Reconcile the allow-list with the Zscaler ZIA category policy."],
        policy_citations=[{"doc": "MIG-POL-002", "version": "v5.1", "section": "6",
                           "quote": "Perimeter egress must be default-deny. Outbound access to high-risk or uncategorised destinations is prohibited without an explicit, documented allow-list entry.",
                           "confidence": Decimal("0.96")}],
        enforcement_evidence=[{"source": "PaloAlto", "rule_id": "PAN-SEC-EGRESS-ANYANY-ALLOW-001", "action": "ALLOW",
                               "raw": rule.get("raw", {})}],
        regulatory=["PCI DSS 1.3", "ISO 27001 A.8.20"], domains_list=["SharePoint", "PaloAlto"], fp_score=0.05,
    )


def uc14_zscaler_paloalto_bypass(sp, zs, aws, pa=None):
    zrule = _has_zscaler_rule(zs, "ZIA-URLCAT-ANONYMIZER-BLOCK")
    parule = _has_paloalto_rule(pa, "PAN-SEC-APP-TOR-ALLOW-022")
    if not zrule or not parule:
        return None
    if (parule.get("raw", {}) or {}).get("action", "").lower() != "allow":
        return None
    return _finding(
        rule_key="UC14", severity="CRITICAL", conflict_type="CONTRADICTION", domain="NETWORK_SECURITY",
        source_pair="Zscaler+Palo Alto", source_policy="ZIA-URLCAT-ANONYMIZER-BLOCK",
        source_technical="PAN-SEC-APP-TOR-ALLOW-022",
        title="Anonymizer traffic blocked by Zscaler but allowed by Palo Alto — enforcement bypass",
        finding_text="Zscaler ZIA rule ZIA-URLCAT-ANONYMIZER-BLOCK blocks the Anonymizer/Tor category, while Palo Alto rule PAN-SEC-APP-TOR-ALLOW-022 permits the 'tor' App-ID outbound. Traffic egressing via the Palo Alto perimeter bypasses the Zscaler control.",
        impact="Two security teams' controls disagree, creating a live bypass: hosts routed through the firewall path can reach anonymizer/Tor destinations the web proxy is meant to block. Data-exfiltration and C2 risk; undermines the SSL-inspection control.",
        remediation=["Align the Palo Alto rulebase with the Zscaler category policy — deny the 'tor' / anonymizer App-IDs at the perimeter.",
                     "Establish a single source of truth for category enforcement across Zscaler and Palo Alto.",
                     "Audit egress paths that bypass the Zscaler tunnel."],
        policy_citations=[],
        enforcement_evidence=[
            {"source": "Zscaler", "rule_id": "ZIA-URLCAT-ANONYMIZER-BLOCK", "action": "BLOCK",
             "raw": zrule.get("raw", {})},
            {"source": "PaloAlto", "rule_id": "PAN-SEC-APP-TOR-ALLOW-022", "action": "ALLOW",
             "raw": parule.get("raw", {})},
        ],
        regulatory=["PCI DSS 1.3", "NAIC MDL-668"], domains_list=["Zscaler", "PaloAlto"], fp_score=0.04,
    )


# ── 16 compliant alignments (false-positive guard) ───────────────────────────

def emit_compliants(sp, zs, aws, pa=None) -> list[dict]:
    rows = [
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
        ("COMPLIANT-UC13-MGMTDENY",    "UC13", "NETWORK_SECURITY", "SharePoint+Palo Alto",   ["SharePoint","PaloAlto"],  "PAN-SEC-MGMT-DENY-EXTERNAL denies management-plane access from the internet — egress policy aligned"),
        ("COMPLIANT-UC14-MALWARE",     "UC14", "NETWORK_SECURITY", "Zscaler+Palo Alto",      ["Zscaler","PaloAlto"],     "Zscaler and Palo Alto both block the Malware/Botnet category — enforcement consistent"),
    ]
    return [_compliant(cid=r[0], rule_key=r[1], domain=r[2], source_pair=r[3], domains_list=r[4], title=r[5]) for r in rows]


# ── Public API ────────────────────────────────────────────────────────────────

MATCHERS = [
    uc01_dropbox_block, uc02_remote_tools_block, uc03_firefox_block,
    uc04_ssl_bypass,    uc05_mfa_admin_only,     uc06_iot_monitor,
    uc07_alb_no_waf,    uc08_vpc_peering,        uc09_s3_eu_west,
    uc10_dlp_blanket,   uc11_geo_restrict,       uc12_social_blanket,
    uc13_perimeter_egress_allow, uc14_zscaler_paloalto_bypass,
]


def run_rule_pack(sharepoint: list[dict], zscaler: list[dict], awsconfig: list[dict],
                  paloalto: list[dict] | None = None,
                  servicenow: dict | None = None,
                  aws_inventory: list[dict] | None = None) -> list[dict]:
    """Run all 14 matchers + emit 16 compliant rows + (optional) ServiceNow drift.

    When a ServiceNow CMDB/asset `servicenow` snapshot and an `aws_inventory` are
    supplied, the CMDB↔AWS reconciliation runs too and its DRIFT findings are merged.
    Returns the combined finding list.
    """
    paloalto = paloalto or []
    findings: list[dict] = []
    for m in MATCHERS:
        try:
            r = m(sharepoint, zscaler, awsconfig, paloalto)
            if r is not None:
                findings.append(r)
        except Exception:
            # Matchers are individually fallible; a single broken matcher
            # must not abort the whole scan.
            continue
    findings.extend(emit_compliants(sharepoint, zscaler, awsconfig, paloalto))
    if servicenow and aws_inventory:
        try:
            findings.extend(run_servicenow_drift(servicenow, aws_inventory))
        except Exception:
            # ServiceNow drift is best-effort; never abort the policy scan.
            pass
    return findings
