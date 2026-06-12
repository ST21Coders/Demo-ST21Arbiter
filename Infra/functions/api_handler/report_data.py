"""Compliance framework model for report generation.

Python mirror of ui/src/lib/governanceScoring.js — KEEP IN SYNC. Same framework
ids, display names, fixed scores, and control→finding (UC) evaluation, so the
generated reports match the Governance page exactly. A control FAILs when its
linked finding (referenced by an ARBITER-UCxx tag in the control note) is OPEN or
IN_REVIEW; otherwise it uses the control's defaultStatus.
"""
from __future__ import annotations

import re

_UC_REF_RE = re.compile(r"ARBITER-UC\d+")

# Verbatim port of FRAMEWORKS in ui/src/lib/governanceScoring.js.
FRAMEWORKS = [
    {
        "id": "naic", "name": "NAIC MDL-668", "score": 65, "accent": "#4f46e5",
        "controls": [
            {"id": "MDL-668 §3", "name": "Data Residency", "note": "ARBITER-UC09: claims data replicating to eu-west-1", "defaultStatus": "FAIL"},
            {"id": "MDL-668 §4", "name": "Authorised Transfers", "note": "ARBITER-UC10: DLP blocking authorised vendor transfers", "defaultStatus": "FAIL"},
            {"id": "MDL-668 §5", "name": "Universal MFA Coverage", "note": "ARBITER-UC05: MFA limited to admins", "defaultStatus": "FAIL"},
            {"id": "MDL-668 §6", "name": "Approved SaaS Access", "note": "ARBITER-UC01: approved cloud storage blocked", "defaultStatus": "FAIL"},
            {"id": "MDL-668 §7", "name": "Vendor Remote Support", "note": "ARBITER-UC02: approved vendor tools blocked", "defaultStatus": "FAIL"},
            {"id": "MDL-668 §8", "name": "Third-Party Country Access", "note": "ARBITER-UC11: ZTNA geo restriction blocks approved vendors", "defaultStatus": "FAIL"},
            {"id": "MDL-668 §9", "name": "Enforcement Consistency", "note": "ARBITER-UC14: anonymizer enforcement bypass across tools", "defaultStatus": "FAIL"},
        ],
    },
    {
        "id": "pci-dss", "name": "PCI-DSS 4.0", "score": 73, "accent": "#0284c7",
        "controls": [
            {"id": "4.1", "name": "SSL/TLS Inspection", "note": "ARBITER-UC04: SSL bypass for finance domains", "defaultStatus": "FAIL"},
            {"id": "8.4", "name": "MFA Coverage", "note": "ARBITER-UC05: MFA limited to admins", "defaultStatus": "FAIL"},
            {"id": "6.2", "name": "Approved SaaS Access", "note": "ARBITER-UC01: approved business app blocked by Zscaler", "defaultStatus": "FAIL"},
            {"id": "6.3", "name": "Vendor Tool Access", "note": "ARBITER-UC02: remote support tools blocked for vendors", "defaultStatus": "FAIL"},
            {"id": "1.2", "name": "IoT Enforcement", "note": "ARBITER-UC06: IoT devices in monitor-only mode", "defaultStatus": "FAIL"},
            {"id": "6.4", "name": "Browser Policy Alignment", "note": "ARBITER-UC03: Firefox blocked despite policy approval", "defaultStatus": "FAIL"},
        ],
    },
    {
        "id": "sox", "name": "SOX", "score": 74, "accent": "#059669",
        "controls": [
            {"id": "SOX ITGC-1", "name": "Public Entry Controls", "note": "ARBITER-UC07: production ALB missing WAF", "defaultStatus": "FAIL"},
            {"id": "SOX ITGC-2", "name": "Production Segmentation", "note": "ARBITER-UC08: dev-to-prod VPC peering violates segmentation", "defaultStatus": "FAIL"},
            {"id": "SOX ITGC-3", "name": "Regulated Data Residency", "note": "ARBITER-UC09: claims data replicating out of region", "defaultStatus": "FAIL"},
            {"id": "SOX ITGC-4", "name": "Department Exceptions", "note": "ARBITER-UC12: social media exemptions not enforced", "defaultStatus": "FAIL"},
        ],
    },
    {
        "id": "nist", "name": "NIST CSF 2.0", "score": 63, "accent": "#d97706",
        "controls": [
            {"id": "PR.AA-01", "name": "MFA Coverage", "note": "ARBITER-UC05: MFA enforcement limited to admins", "defaultStatus": "FAIL"},
            {"id": "PR.PS-01", "name": "Public Workload Protection", "note": "ARBITER-UC07: production ALB exposed without WAF", "defaultStatus": "FAIL"},
            {"id": "PR.DS-01", "name": "Cross-Region Data Controls", "note": "ARBITER-UC09: claims data replicating to eu-west-1", "defaultStatus": "FAIL"},
            {"id": "PR.AA-02", "name": "Approved Cloud Storage", "note": "ARBITER-UC01: Dropbox Business approved but blocked", "defaultStatus": "FAIL"},
            {"id": "PR.PT-01", "name": "IoT Blocking", "note": "ARBITER-UC06: IoT devices in monitor-only mode", "defaultStatus": "FAIL"},
            {"id": "PR.IR-01", "name": "Default-Deny Egress", "note": "ARBITER-UC13: Palo Alto permits any/any outbound", "defaultStatus": "FAIL"},
            {"id": "PR.AA-03", "name": "Browser Standard", "note": "ARBITER-UC03: approved browser blocked", "defaultStatus": "FAIL"},
            {"id": "PR.AA-04", "name": "Vendor Country Access", "note": "ARBITER-UC11: approved vendor countries blocked", "defaultStatus": "FAIL"},
        ],
    },
    {
        "id": "iso27001", "name": "ISO 27001:2022", "score": 63, "accent": "#7c3aed",
        "controls": [
            {"id": "A.5.15", "name": "Access Control", "note": "ARBITER-UC05: MFA enforcement limited to admins", "defaultStatus": "FAIL"},
            {"id": "A.8.20", "name": "Network Security", "note": "ARBITER-UC08: production segmentation failure", "defaultStatus": "FAIL"},
            {"id": "A.5.23", "name": "Cloud Services", "note": "ARBITER-UC09: regulated data replicated cross-region", "defaultStatus": "FAIL"},
            {"id": "A.5.10", "name": "Use of Information Assets", "note": "ARBITER-UC02: approved remote support tools blocked", "defaultStatus": "FAIL"},
            {"id": "A.5.34", "name": "Privacy and PII Protection", "note": "ARBITER-UC10: authorised data transfers blocked by DLP", "defaultStatus": "FAIL"},
            {"id": "A.8.22", "name": "Segregation of Networks", "note": "ARBITER-UC13: firewall egress permits any/any", "defaultStatus": "FAIL"},
            {"id": "A.8.1", "name": "User Endpoint Devices", "note": "ARBITER-UC03: approved browser blocked by enforcement", "defaultStatus": "FAIL"},
            {"id": "A.5.19", "name": "Supplier Relationships", "note": "ARBITER-UC11: approved vendor countries blocked", "defaultStatus": "FAIL"},
        ],
    },
]

FRAMEWORK_NAMES = {fw["id"]: fw["name"] for fw in FRAMEWORKS}
_OPEN_STATUSES = {"OPEN", "IN_REVIEW"}


def extract_uc(note: str) -> str | None:
    m = _UC_REF_RE.search(note or "")
    return m.group(0) if m else None


def evaluate_control(ctrl: dict, finding_by_uc: dict) -> dict:
    uc = extract_uc(ctrl.get("note"))
    linked = finding_by_uc.get(uc) if uc else None
    if not linked:
        return {"ctrl": ctrl, "uc": uc, "linked": None,
                "status": ctrl.get("defaultStatus") or "PASS", "severity": None}
    status = "FAIL" if (linked.get("status") or "").upper() in _OPEN_STATUSES else "PASS"
    return {"ctrl": ctrl, "uc": uc, "linked": linked, "status": status,
            "severity": linked.get("severity")}


def evaluate_framework(framework: dict, finding_by_uc: dict) -> dict:
    evals = [evaluate_control(c, finding_by_uc) for c in framework["controls"]]
    open_evals = [e for e in evals if e["status"] == "FAIL"]
    sev_counts: dict[str, int] = {}
    for e in open_evals:
        sev = e["severity"] or "HIGH"
        sev_counts[sev] = sev_counts.get(sev, 0) + 1
    return {
        "id": framework["id"], "name": framework["name"], "score": framework["score"],
        "accent": framework["accent"], "evals": evals,
        "open_count": len(open_evals),
        "critical_count": sev_counts.get("CRITICAL", 0),
        "high_count": sev_counts.get("HIGH", 0),
        "medium_count": sev_counts.get("MEDIUM", 0),
        "low_count": sev_counts.get("LOW", 0),
        "pass_count": len(evals) - len(open_evals),
    }


def framework_summaries(findings: list[dict], framework_ids: list[str] | None = None) -> list[dict]:
    by_uc: dict[str, dict] = {}
    for f in findings:
        cid = f.get("conflict_id")
        if cid:
            by_uc[cid] = f
    out = [evaluate_framework(fw, by_uc) for fw in FRAMEWORKS]
    if framework_ids:
        wanted = set(framework_ids)
        out = [s for s in out if s["id"] in wanted]
    return out


def overall_score(summaries: list[dict]) -> int:
    if not summaries:
        return 0
    return round(sum(s["score"] for s in summaries) / len(summaries))
