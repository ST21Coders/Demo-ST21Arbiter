"""Team / tag ownership enrichment for ARBITER scan findings.

The scan engine (agents/master_orchestrator/scan_rule_pack.py) stays pure and
ownership-agnostic — it emits findings carrying rule_key / domain / source_pair.
This module is a POST-scan step run inside the scanner Lambda: it loads a small
deterministic rules table and stamps each finding with the owning team, the
consuming team, the platform/managing team, and a tag set. Keeping ownership in
a DynamoDB table (not in the matchers) means the org model can change without
rebuilding + redeploying the AgentCore image.

`enrich_findings()` is pure (rules passed in) so it is unit-testable without AWS.
`load_ownership_rules()` is the only AWS-touching part.

Rule item shape (one DynamoDB row per rule):
    {
      "rule_id":       "rule-uc01",
      "priority":      10,                 # lower = higher precedence
      "match":         {"rule_key": "UC01"},   # all present keys must equal the finding's
      "owner_team":    "data-governance",
      "consumer_team": "app-dev",
      "platform_team": "network-eng",
      "tags":          ["application", "network"]
    }

Matching is first-match by ascending priority. A wildcard rule (empty "match")
matches everything and should be seeded as the lowest-precedence default so no
finding is ever left without an owner.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger()

OWNERSHIP_FIELDS = ("owner_team", "consumer_team", "platform_team")


def load_ownership_rules(table) -> list[dict[str, Any]]:
    """Scan the ownership-rules table and return rules sorted by priority.

    Returns [] on any failure (the caller logs loudly and findings are written
    without ownership rather than the scan failing entirely).
    """
    rules: list[dict] = []
    resp = table.scan()
    rules.extend(resp.get("Items", []))
    # Paginate defensively, though the rule set is tiny.
    while "LastEvaluatedKey" in resp:
        resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
        rules.extend(resp.get("Items", []))
    rules.sort(key=lambda r: _as_int(r.get("priority"), 999))
    return rules


def _as_int(v, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _rule_matches(match: dict, finding: dict) -> bool:
    """True if every predicate in `match` equals the finding's field.

    An empty match dict matches everything (the wildcard default).
    """
    for key, expected in (match or {}).items():
        if finding.get(key) != expected:
            return False
    return True


def enrich_findings(findings: list[dict], rules: list[dict]) -> list[dict]:
    """Stamp each finding in-place with owner/consumer/platform team + tags.

    Pure given `rules`. First rule (by ascending priority) whose predicates all
    match wins. Findings with no matching rule are left with blank ownership +
    empty tags (the seeded wildcard default should prevent this; a blank is a
    signal that a rule is missing, surfaced by the scanner's sanity log).
    """
    # Sort here too (not just in load_ownership_rules) so the function is correct
    # regardless of input order — priority always decides, never list position.
    ordered = sorted(rules, key=lambda r: _as_int(r.get("priority"), 999))
    for f in findings:
        matched = None
        for r in ordered:
            if _rule_matches(r.get("match") or {}, f):
                matched = r
                break
        if matched:
            f["owner_team"] = matched.get("owner_team", "") or ""
            f["consumer_team"] = matched.get("consumer_team", "") or ""
            f["platform_team"] = matched.get("platform_team", "") or ""
            f["tags"] = list(matched.get("tags") or [])
        else:
            for field in OWNERSHIP_FIELDS:
                f.setdefault(field, "")
            f.setdefault("tags", [])
    return findings
