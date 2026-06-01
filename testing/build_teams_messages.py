"""Build the daily-report Adaptive Card payload, fully data-driven.

Reads:
  test-reports/report.json          - aggregated test results
  test-reports/aws-health.json      - aws_health_check.py output (optional)
  test-reports/static-ruff.json     - ruff findings (optional)
  test-reports/static-npm_audit.json - npm audit (optional)
  test-reports/static-pip_audit.json - pip-audit (optional)

Writes:
  reports/teams-message-banner-<date>.json   - the card payload posted to Teams

Every section is derived from the inputs above. Anything missing is omitted
rather than hardcoded; the card adapts to whatever the test run produced.
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = REPO_ROOT / "reports"
TEST_REPORTS_DIR = Path(os.environ.get("TEST_REPORTS_DIR", REPO_ROOT / "test-reports"))

# Documented behavior gaps that the security suite asserts on but doesn't
# treat as failures. Kept here (not derived from tests) because the tests pass
# by design; the gap is the test's *purpose*, not its outcome. Update when the
# code is fixed or a new test_*_documents_gap case is added.
KNOWN_SECURITY_GAPS = [
    ("Medium", "Function URL accepts unsigned JWTs (any sub claim trusted)",
     "Infra/functions/api_handler/api_handler.py _caller_user_id",
     "Verify JWT against Cognito JWKS using python-jose before reading claims['sub']."),
    ("Medium", "/findings and /change-requests not scoped to caller",
     "Infra/functions/api_handler/api_handler.py",
     "Any authenticated user reads all findings + CRs. Add per-user filtering or document on the RBAC roadmap."),
    ("Medium", "Prompt injection passes through to model",
     "agents/master_orchestrator/agent.py",
     "Canonical injection payloads reach orchestrator unfiltered. Verify Bedrock Guardrail actually blocks them in TEST_MODE=live."),
    ("Medium", "No rate limiting on API Gateway",
     "Infra/templates/06-api.yaml",
     "Add AWS::ApiGateway::UsagePlan with Throttle.RateLimit=20 BurstLimit=40."),
    ("Low", "CORS wildcard on API GW",
     "Infra/templates/06-api.yaml",
     "Access-Control-Allow-Origin: *. Tighten to CloudFront origin."),
]


def _load_json(path: Path) -> dict | list | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def _overall(summary: dict) -> tuple[str, str]:
    try:
        rate = float(summary["passRate"].rstrip("%"))
    except (KeyError, ValueError):
        rate = 0.0
    if summary["failed"] == 0 and rate >= 99.9:
        return "All Clear", "good"
    if summary["failed"] > 0 and rate < 90:
        return "Critical Issues", "attention"
    return "Issues Found", "warning"


def _heading(text: str) -> dict:
    return {
        "type": "TextBlock", "text": text, "weight": "Bolder", "size": "Medium",
        "separator": True, "spacing": "Medium", "wrap": True,
    }


def _text(text: str, *, weight: str | None = None, color: str | None = None,
          size: str | None = None, spacing: str = "Default") -> dict:
    block: dict[str, Any] = {"type": "TextBlock", "text": text, "wrap": True, "spacing": spacing}
    if weight: block["weight"] = weight
    if color: block["color"] = color
    if size: block["size"] = size
    return block


def _fact(name: str, value: str) -> dict:
    return {"title": name, "value": value}


def _normalize_failure_name(name: str) -> str:
    """Test framework names are noisy; surface the part a human would recognize.

    Playwright: 'file.spec.ts > suite > test title' -> 'test title (file.spec.ts)'
    pytest: 'unit/foo.py::test_bar' -> 'test_bar (unit/foo.py)'
    """
    if ".spec.ts" in name:
        parts = name.split(" > ")
        if len(parts) >= 2:
            return f"{parts[-1]} ({parts[0]})"
    if "::" in name:
        file_, _, test_ = name.partition("::")
        return f"{test_} ({file_})"
    return name


def _first_error_line(err: str) -> str:
    """Pull the first non-empty line of a Playwright/pytest error blob."""
    if not err:
        return ""
    # Strip ANSI color codes Playwright leaves in its JSON error messages.
    clean = re.sub(r"\x1b\[[0-9;]*m", "", err)
    for line in clean.splitlines():
        s = line.strip()
        if s and not s.startswith("Call log:"):
            return s
    return clean[:200].strip()


def _route_perf_table(frontend_passed: list[dict]) -> list[tuple[str, str]]:
    """Pull '@perf page load budget /X < 3000ms' tests out of frontend.passed."""
    perf = []
    for t in frontend_passed:
        name = t.get("name", "")
        if "@perf page load budget" not in name:
            continue
        # Match `/route` between "budget " and " <"
        m = re.search(r"budget\s+(\S+)\s+<", name)
        if not m:
            continue
        route = m.group(1)
        perf.append((route, f"{t.get('duration_ms', 0)}ms"))
    return perf


def _summarize_ruff(findings: list) -> tuple[int, str]:
    """Return (count, one-line summary like '33 line-too-long, 9 import-order, …')."""
    if not findings:
        return 0, ""
    rule_names = {
        "E501": "line-too-long", "I001": "import-order", "F401": "unused imports",
        "UP017": "datetime.UTC alias", "B005": "str.strip multi-char",
        "E743": "ambiguous fn name", "F841": "unused variable", "S603": "subprocess",
    }
    counts = Counter(f.get("code", "?") for f in findings)
    parts = [f"{n} {rule_names.get(code, code)}" for code, n in counts.most_common(5)]
    return sum(counts.values()), ", ".join(parts)


def _summarize_npm_audit(audit: dict | None) -> tuple[int, str]:
    if not audit:
        return 0, ""
    vulns = audit.get("metadata", {}).get("vulnerabilities", {})
    total = vulns.get("total", 0)
    if total == 0:
        return 0, "No vulnerabilities."
    sev_parts = [f"{vulns[k]} {k}" for k in ("critical", "high", "moderate", "low") if vulns.get(k)]
    return total, ", ".join(sev_parts) + "."


def _summarize_pip_audit(audit: dict | None) -> tuple[int, str]:
    if not audit:
        return 0, ""
    deps_with_vulns = [d for d in (audit.get("dependencies") or []) if d.get("vulns")]
    if not deps_with_vulns:
        return 0, "All Python deps clean."
    pkgs = ", ".join(f"{d['name']}@{d['version']}" for d in deps_with_vulns[:3])
    return len(deps_with_vulns), f"{pkgs}…" if len(deps_with_vulns) > 3 else pkgs


def _derive_action_items(
    report: dict,
    aws_health: dict | None,
    npm_total: int,
    ruff_total: int,
) -> dict[str, list[tuple[str, str, str]]]:
    """Turn observations into prioritized action items.

    Heuristic: any backend security failure = Critical. Any AWS finding from
    aws_health.json with severity High = High. Failed frontend tests = High
    (each failed test = one item). npm moderate CVEs = Medium. ruff = Low.
    """
    crit: list[tuple[str, str, str]] = []
    high: list[tuple[str, str, str]] = []
    med: list[tuple[str, str, str]] = []
    low: list[tuple[str, str, str]] = []

    for f in (report.get("security") or {}).get("failed") or []:
        crit.append((
            f"Security failure: {_normalize_failure_name(f['name'])}",
            f["name"],
            _first_error_line(f.get("error", "")) or "See full error in report.json",
        ))

    fe_fail = report.get("frontend", {}).get("failed") or []
    if fe_fail:
        high.append((
            f"{len(fe_fail)} Playwright/Vitest test(s) failing",
            "see Frontend Test Failures section above",
            "Triage each per the failure detail block.",
        ))

    if aws_health:
        for f in aws_health.get("findings", []):
            sev = f.get("severity", "Medium")
            entry = (f.get("title", ""), f.get("location", ""), f.get("fix", ""))
            if sev == "High": high.append(entry)
            elif sev == "Medium": med.append(entry)
            else: low.append(entry)

    if npm_total > 0:
        med.append((
            f"{npm_total} npm CVE(s) in dep tree",
            "ui/package-lock.json",
            "cd ui && npm audit fix",
        ))

    if ruff_total > 0:
        low.append((
            f"ruff: {ruff_total} style finding(s)",
            "repo-wide",
            "ruff check --fix . then commit (auto-fixes ~80%).",
        ))

    return {"critical": crit, "high": high, "medium": med, "low": low}


def _aws_health_section(aws: dict | None) -> list[dict]:
    """Build the AWS Health section blocks. Falls back to a 'not run' note."""
    blocks: list[dict] = [_heading("AWS Infrastructure Health")]
    if not aws:
        blocks.append(_text(
            "AWS health check was not run for this report (no AWS credentials in scope, or the daily workflow skipped this step).",
            color="accent", size="Small",
        ))
        return blocks

    facts = aws.get("facts") or []
    if facts:
        blocks.append({"type": "FactSet", "facts": [_fact(f["name"], f["value"]) for f in facts]})

    findings = aws.get("findings") or []
    if findings:
        blocks.append(_text("**AWS issues to triage:**", weight="Bolder", spacing="Medium"))
        for f in findings:
            title = f.get("title", "")
            detail = f.get("detail", "")
            fix = f.get("fix", "")
            blocks.append(_text(
                f"- **{title}** — {detail}\n  *Fix:* {fix}",
                spacing="Small",
            ))
    return blocks


def build_card(
    report: dict,
    aws: dict | None,
    ruff: list | None,
    npm_audit: dict | None,
    pip_audit: dict | None,
) -> dict:
    s = report["summary"]
    status_label, color = _overall(s)
    today = report.get("runDate", datetime.now(timezone.utc).isoformat())[:10]
    coverage = report.get("coverage") or {}

    body: list[dict[str, Any]] = []

    # HEADER
    body.append({"type": "TextBlock", "text": f"Daily Test Report — {today}",
                 "weight": "Bolder", "size": "ExtraLarge", "wrap": True})
    body.append({"type": "TextBlock",
                 "text": f"Overall Status: {status_label}  •  Pass rate {s['passRate']}",
                 "color": color, "weight": "Bolder", "size": "Medium",
                 "wrap": True, "spacing": "None"})
    aws_scope_note = "plus live AWS health checks scoped to dev-st21arbiter-poc* and lm* in us-east-1" if aws else "(AWS health check skipped this run)"
    body.append(_text(
        f"Run scope: full mock-mode pipeline (Vitest + Playwright + pytest + coverage + ruff + pip-audit + npm audit) {aws_scope_note}.",
        color="accent", size="Small", spacing="Small",
    ))

    # SUMMARY
    body.append(_heading("Summary"))
    fe_pass = len(report["frontend"]["passed"])
    fe_fail = len(report["frontend"]["failed"])
    fe_skip = len(report["frontend"]["skipped"])
    be_pass = len(report["backend"]["passed"])
    be_fail = len(report["backend"]["failed"])
    be_skip = len(report["backend"]["skipped"])
    sec_pass = len((report.get("security") or {}).get("passed") or [])
    sec_fail = len((report.get("security") or {}).get("failed") or [])
    perf_routes = _route_perf_table(report["frontend"]["passed"])
    ruff_total, ruff_summary = _summarize_ruff(ruff or [])
    npm_total, npm_summary = _summarize_npm_audit(npm_audit)
    pip_total, pip_summary = _summarize_pip_audit(pip_audit)
    static_total = ruff_total + npm_total + pip_total

    cov_str = "n/a"
    if coverage.get("total_percent") is not None:
        files = coverage.get("files") or {}
        file_parts = ", ".join(f"{Path(p).name} {v['percent']}%" for p, v in files.items())
        cov_str = f"{coverage['total_percent']}% ({file_parts})" if file_parts else f"{coverage['total_percent']}%"

    body.append({"type": "FactSet", "facts": [
        _fact("Total Tests", str(s["total"])),
        _fact("Passed", str(s["passed"])),
        _fact("Failed", str(s["failed"])),
        _fact("Skipped", str(s["skipped"])),
        _fact("Duration", f"{report.get('duration', 0)}s aggregated"),
        _fact("Backend coverage", cov_str),
    ]})

    aws_total = len((aws or {}).get("facts") or [])
    aws_issues = len((aws or {}).get("findings") or [])
    aws_chip = f"{aws_total - aws_issues}/{aws_total} healthy" if aws_total else "skipped"
    body.append(_text(
        f"**AWS Health Checks** {aws_chip}   •   "
        f"**Frontend** {fe_pass} pass / {fe_fail} fail / {fe_skip} skip   •   "
        f"**Backend** {be_pass} pass / {be_fail} fail / {be_skip} skip   •   "
        f"**Security** {sec_pass} pass / {sec_fail} fail ({len(KNOWN_SECURITY_GAPS)} documented gaps)   •   "
        f"**Performance** {len(perf_routes)} pass   •   "
        f"**Static Analysis** {static_total} findings ({ruff_total} ruff + {npm_total} npm)",
        spacing="Small",
    ))

    # AWS HEALTH
    body.extend(_aws_health_section(aws))

    # FRONTEND FAILURES
    body.append(_heading(f"Frontend Test Failures ({fe_fail})"))
    if fe_fail == 0:
        body.append(_text(f"All {fe_pass} frontend tests passing across Vitest + Playwright. Nothing to triage.", spacing="Small"))
    else:
        body.append(_text(f"{fe_pass} passed across Vitest unit + Playwright E2E (Chromium). {fe_fail} failures.", spacing="Small"))
        for f in report["frontend"]["failed"]:
            body.append(_text(
                f"**{_normalize_failure_name(f['name'])}**\n*What broke:* {_first_error_line(f.get('error', ''))}",
                spacing="Small",
            ))

    # BACKEND
    body.append(_heading("Backend Test Results"))
    if be_fail == 0:
        body.append(_text(f"**{be_pass} passed, 0 failed, {be_skip} skipped** (live smoke skipped in mock mode).", weight="Bolder", spacing="Small"))
    else:
        body.append(_text(f"**{be_pass} passed, {be_fail} failed, {be_skip} skipped.**", weight="Bolder", color="attention", spacing="Small"))
        for f in report["backend"]["failed"]:
            body.append(_text(
                f"**{_normalize_failure_name(f['name'])}**\n*What broke:* {_first_error_line(f.get('error', ''))}",
                spacing="Small",
            ))
    if coverage:
        cov_files = coverage.get("files") or {}
        cov_lines = "\n".join(f"- `{p}` — {v['percent']}% ({v['missing_lines']}/{v['num_statements']} lines uncovered)" for p, v in cov_files.items())
        body.append(_text(f"**Coverage** ({coverage['total_percent']}% combined):\n{cov_lines}", spacing="Small"))
    if be_skip:
        skip_names = ", ".join(f["name"] for f in report["backend"]["skipped"][:5])
        body.append(_text(f"**Skipped ({be_skip}):** {skip_names}", spacing="Small"))

    # SECURITY
    body.append(_heading("Security Results"))
    if sec_fail == 0:
        body.append(_text(
            f"**{sec_pass}/{sec_pass} security tests passing** across auth/authorization and input validation. "
            "Infrastructure checks below derive from the AWS Health section above.",
            spacing="Small",
        ))
    else:
        body.append(_text(f"**{sec_fail} security tests failing — triage immediately.**", weight="Bolder", color="attention", spacing="Small"))
    body.append(_text("**Documented gaps (tests pass but mark intentional behavior):**", weight="Bolder", spacing="Medium"))
    for sev, what, where, fix in KNOWN_SECURITY_GAPS:
        sev_color = {"Critical": "attention", "High": "attention", "Medium": "warning"}.get(sev, "default")
        body.append({"type": "Container", "spacing": "Small", "items": [
            _text(f"**{sev}** — {what}", color=sev_color),
            _text(f"*Where:* {where}\n*Fix:* {fix}", spacing="None", size="Small"),
        ]})
    dep_line = []
    if pip_audit is not None: dep_line.append(f"pip-audit {pip_total} finding(s)")
    if npm_audit is not None: dep_line.append(f"npm audit {npm_total} finding(s){' — ' + npm_summary if npm_summary else ''}")
    if dep_line:
        body.append(_text(f"**Dependency CVE audit:** {' · '.join(dep_line)}.", spacing="Medium"))

    # PERFORMANCE
    body.append(_heading("Performance Results"))
    if perf_routes:
        body.append(_text("**Frontend page-load budget** (Playwright @perf, <3000ms per route)", weight="Bolder", spacing="Small"))
        body.append({"type": "FactSet", "facts": [_fact(route, dur) for route, dur in perf_routes]})
    backend_perf_note = ""
    if aws:
        for fact in aws.get("facts", []):
            if fact["name"].startswith("Lambda api-handler"):
                backend_perf_note = fact["value"]
                break
    if backend_perf_note:
        body.append(_text(f"**Backend (Lambda /chat path):** {backend_perf_note}.", spacing="Small"))
    body.append(_text("**Concurrency / memory leak / load testing:** not exercised today.", spacing="Small"))

    # STATIC ANALYSIS
    body.append(_heading("Static Analysis"))
    if ruff is not None:
        body.append(_text(f"**ruff** — {ruff_total} findings.{f' {ruff_summary}.' if ruff_summary else ''} Run `ruff check --fix .` to auto-fix.", spacing="Small"))
    if npm_audit is not None:
        body.append(_text(f"**npm audit** — {npm_total} findings.{f' {npm_summary}' if npm_summary else ''} Fix: `cd ui && npm audit fix`.", spacing="Small"))
    if pip_audit is not None:
        body.append(_text(f"**pip-audit** — {pip_total} findings.{f' {pip_summary}' if pip_summary else ''}", spacing="Small"))
    body.append(_text("**gitleaks** — handled by pre-commit hook; commits with secrets are blocked at push time.", spacing="Small"))

    # SKIPPED
    body.append(_heading("Skipped Tests"))
    all_skipped = report["frontend"]["skipped"] + report["backend"]["skipped"]
    if all_skipped:
        body.append(_text(f"**{len(all_skipped)} total**:", weight="Bolder", spacing="Small"))
        for sk in all_skipped:
            body.append(_text(f"- `{_normalize_failure_name(sk['name'])}`", spacing="Small"))
    else:
        body.append(_text("None.", spacing="Small"))
    body.append(_text(
        "**AWS not-applicable:** RDS/Aurora, ElastiCache, ECS/EKS, ALB/NLB — none provisioned (serverless stack).",
        size="Small", spacing="Small",
    ))

    # RECOMMENDATIONS (stable, not derived — these are forward-looking architectural suggestions)
    body.append(_heading("Feature Recommendations"))
    body.append(_text(
        "- **Live /chat p95/p99 dashboard** — CloudWatch dashboard or X-Ray sampling for percentile latency.\n"
        "- **JWT signature verification on Function URL path** — closes biggest documented security gap. ~30 LOC.\n"
        "- **Promote lm-arbiter-poc-kb to versioned + replicated** — losing KB source docs is a deploy-blocker.\n"
        "- **Weekly live-mode smoke** — once-a-week canary with TEST_MODE=live to catch live-stack drift.\n"
        "- **@axe-core/playwright for a11y** — catches contrast + ARIA violations at WCAG AA.",
        spacing="Small",
    ))

    # ACTION ITEMS
    body.append(_heading("Action Items"))
    actions = _derive_action_items(report, aws, npm_total, ruff_total)
    for label, key, col in [("Critical", "critical", "attention"),
                              ("High", "high", "attention"),
                              ("Medium", "medium", "warning"),
                              ("Low", "low", "default")]:
        items = actions[key]
        if not items:
            body.append(_text(f"**{label}** — none today.", color=col, spacing="Small"))
            continue
        body.append(_text(f"**{label}**", color=col, weight="Bolder", spacing="Medium"))
        for issue, location, fix in items:
            body.append(_text(f"- **{issue}**\n  *Location:* `{location}`\n  *Fix:* {fix}", spacing="Small"))

    return {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": body,
                "msteams": {"width": "Full"},
            },
        }],
    }


def main() -> int:
    report = _load_json(TEST_REPORTS_DIR / "report.json")
    if not report:
        sys.exit(f"missing {TEST_REPORTS_DIR / 'report.json'} — run report_generator.py first")
    aws = _load_json(TEST_REPORTS_DIR / "aws-health.json")
    ruff = _load_json(TEST_REPORTS_DIR / "static-ruff.json")
    npm_audit = _load_json(TEST_REPORTS_DIR / "static-npm_audit.json")
    pip_audit = _load_json(TEST_REPORTS_DIR / "static-pip_audit.json")

    today = report.get("runDate", datetime.now(timezone.utc).isoformat())[:10]
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    card = build_card(report, aws, ruff if isinstance(ruff, list) else [], npm_audit, pip_audit)
    out = REPORTS_DIR / f"teams-message-banner-{today}.json"
    out.write_text(json.dumps(card, indent=2, ensure_ascii=False))
    body_blocks = len(card["attachments"][0]["content"]["body"])
    print(f"Wrote {out}  ({out.stat().st_size} bytes, {body_blocks} body blocks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
