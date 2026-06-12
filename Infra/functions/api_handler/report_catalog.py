"""Report catalog — the report types and formats the synchronous /reports backend
can generate. Adapted from arbiter-poc's compliance/report_catalog.py, trimmed to
the set ARBITER (ST21) can build from its conflicts / audit / framework data.

Each spec: id, title, description, category, audience, formats (ordered),
default_format, icon (lucide-react name for the UI), estimated_seconds,
parameters (UI form), tags. catalog_by_id() resolves a spec for validation.
"""
from __future__ import annotations

FRAMEWORK_PARAM = {
    "id": "frameworks", "label": "Frameworks", "type": "multi_select",
    "default": ["naic", "pci-dss", "sox", "nist", "iso27001"],
    "options": [
        {"id": "naic",     "label": "NAIC MDL-668"},
        {"id": "pci-dss",  "label": "PCI-DSS 4.0"},
        {"id": "sox",      "label": "SOX"},
        {"id": "nist",     "label": "NIST CSF 2.0"},
        {"id": "iso27001", "label": "ISO 27001:2022"},
    ],
}

SEVERITY_PARAM = {
    "id": "severity", "label": "Severity", "type": "multi_select",
    "default": ["CRITICAL", "HIGH", "MEDIUM", "LOW"],
    "options": [
        {"id": "CRITICAL", "label": "Critical"},
        {"id": "HIGH",     "label": "High"},
        {"id": "MEDIUM",   "label": "Medium"},
        {"id": "LOW",      "label": "Low"},
    ],
}

STATUS_PARAM = {
    "id": "status", "label": "Status", "type": "multi_select",
    "default": ["OPEN", "IN_REVIEW", "RESOLVED"],
    "options": [
        {"id": "OPEN",      "label": "Open"},
        {"id": "IN_REVIEW", "label": "In review"},
        {"id": "RESOLVED",  "label": "Resolved"},
    ],
}

REPORT_CATALOG = [
    {
        "id": "executive_compliance",
        "title": "Executive Compliance Briefing",
        "description": "Board-ready summary: overall score, per-framework breakdown, and the top open risks. Reflects current posture at the moment of generation.",
        "category": "Compliance",
        "audience": "CISO, Board, Executive Risk Committee",
        "formats": ["pdf"],
        "default_format": "pdf",
        "icon": "FileText",
        "estimated_seconds": 3,
        "parameters": [FRAMEWORK_PARAM],
        "tags": ["board", "summary", "narrative"],
    },
    {
        "id": "technical_compliance",
        "title": "Technical Compliance Report",
        "description": "Per-framework control posture: every control, its PASS/FAIL state, the linked conflict, severity and status. The auditor hand-off.",
        "category": "Compliance",
        "audience": "QSA, External Auditor, GRC Analyst",
        "formats": ["pdf", "xlsx"],
        "default_format": "pdf",
        "icon": "FileSpreadsheet",
        "estimated_seconds": 5,
        "parameters": [FRAMEWORK_PARAM, SEVERITY_PARAM, STATUS_PARAM],
        "tags": ["audit-handoff", "framework-detail"],
    },
    {
        "id": "conflict_register",
        "title": "Conflict Register",
        "description": "The full conflict inventory — id, severity, status, domains, regulatory mappings and remediation — as a flat export.",
        "category": "Risk",
        "audience": "GRC Analyst, Risk Owner",
        "formats": ["csv", "xlsx", "json"],
        "default_format": "csv",
        "icon": "Table",
        "estimated_seconds": 2,
        "parameters": [SEVERITY_PARAM, STATUS_PARAM],
        "tags": ["register", "inventory"],
    },
    {
        "id": "audit_trail",
        "title": "Audit Trail Export",
        "description": "Chronological audit-log events — scans, change requests, approvals, ingestion — for evidence and forensics.",
        "category": "Audit",
        "audience": "External Auditor, Regulator",
        "formats": ["csv", "json"],
        "default_format": "csv",
        "icon": "ScrollText",
        "estimated_seconds": 2,
        "parameters": [],
        "tags": ["audit", "evidence"],
    },
    {
        "id": "evidence_package",
        "title": "Evidence Package",
        "description": "ZIP bundle: technical compliance PDF, conflict register CSV, audit-trail JSON and a scores snapshot. The single artifact you hand an auditor.",
        "category": "Audit",
        "audience": "External Auditor, QSA, Regulator",
        "formats": ["zip"],
        "default_format": "zip",
        "icon": "Package",
        "estimated_seconds": 6,
        "parameters": [FRAMEWORK_PARAM],
        "tags": ["audit-ready", "complete"],
    },
]

CATEGORIES = ["Compliance", "Risk", "Audit"]


def catalog_by_id(report_id: str) -> dict | None:
    for spec in REPORT_CATALOG:
        if spec["id"] == report_id:
            return spec
    return None
