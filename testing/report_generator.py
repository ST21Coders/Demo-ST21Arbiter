"""Aggregate Vitest, Playwright, and pytest results into one structured report.

Reads:
  - test-reports/playwright-results.json   (Playwright JSON reporter)
  - test-reports/pytest-results.json       (pytest-json-report)
  - test-reports/vitest-results.json       (Vitest --reporter=json)

Writes:
  - test-reports/report.json               (the canonical aggregated report)

The shape matches the spec in the test-pipeline brief; downstream Teams
posting and recommendations engine both consume report.json only.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPORTS_DIR = Path(os.environ.get("TEST_REPORTS_DIR", "test-reports"))
SLOW_PAGE_MS = int(os.environ.get("SLOW_PAGE_MS", "3000"))
SLOW_ENDPOINT_MS = int(os.environ.get("SLOW_ENDPOINT_MS", "500"))


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as e:
        print(f"warn: {path} is not valid JSON: {e}", file=sys.stderr)
        return None


def _parse_playwright(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Walk Playwright's nested suites tree and flatten to pass/fail lists.

    Playwright's JSON reporter nests: config → suites[] → suites[] → specs[]
    → tests[] → results[]. We collect leaf specs and bucket them by outcome.
    """
    passed: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    slow_pages: list[dict[str, Any]] = []

    if not raw:
        return {"passed": passed, "failed": failed, "skipped": skipped, "slow_pages": slow_pages}

    def walk(suites: list[dict[str, Any]], path: list[str]) -> None:
        for s in suites or []:
            title = s.get("title", "")
            new_path = path + [title] if title else path
            for spec in s.get("specs") or []:
                spec_title = " > ".join(new_path + [spec.get("title", "")])
                for t in spec.get("tests") or []:
                    last = (t.get("results") or [{}])[-1]
                    status = last.get("status", "unknown")
                    duration = last.get("duration", 0)
                    entry: dict[str, Any] = {"name": spec_title, "duration_ms": duration}
                    if status == "passed":
                        passed.append(entry)
                        if duration > SLOW_PAGE_MS:
                            slow_pages.append({"name": spec_title, "duration_ms": duration})
                    elif status == "skipped":
                        skipped.append(entry)
                    else:
                        # Pull the first error message and the screenshot path
                        # (Playwright stores attachments alongside the result).
                        err = (last.get("errors") or [{}])[0].get("message", "") if last.get("errors") else last.get("error", {}).get("message", "")
                        screenshot = ""
                        for att in last.get("attachments") or []:
                            if att.get("name", "").startswith("screenshot"):
                                screenshot = att.get("path", "")
                                break
                        failed.append({
                            "name": spec_title,
                            "duration_ms": duration,
                            "error": err,
                            "screenshotPath": screenshot,
                        })
            walk(s.get("suites") or [], new_path)

    walk(raw.get("suites") or [], [])
    return {"passed": passed, "failed": failed, "skipped": skipped, "slow_pages": slow_pages}


def _parse_pytest(raw: dict[str, Any] | None) -> dict[str, Any]:
    passed: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    if not raw:
        return {"passed": passed, "failed": failed, "skipped": skipped}
    for t in raw.get("tests") or []:
        name = t.get("nodeid", t.get("name", "unknown"))
        outcome = t.get("outcome", "unknown")
        duration_ms = int((t.get("call", {}).get("duration", 0) or 0) * 1000)
        entry = {"name": name, "duration_ms": duration_ms}
        if outcome == "passed":
            passed.append(entry)
        elif outcome == "skipped":
            skipped.append(entry)
        else:
            longrepr = t.get("call", {}).get("longrepr", "") or t.get("longrepr", "")
            failed.append({**entry, "error": str(longrepr)[:2000], "stack": ""})
    return {"passed": passed, "failed": failed, "skipped": skipped}


def _parse_vitest(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Vitest's JSON reporter is Jest-shaped: testResults[].assertionResults[]."""
    passed: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    if not raw:
        return {"passed": passed, "failed": failed, "skipped": skipped}
    for file in raw.get("testResults") or []:
        for a in file.get("assertionResults") or []:
            name = " > ".join(a.get("ancestorTitles") or []) + " > " + a.get("title", "")
            duration_ms = int(a.get("duration") or 0)
            status = a.get("status", "unknown")
            entry = {"name": name.strip(" > "), "duration_ms": duration_ms}
            if status == "passed":
                passed.append(entry)
            elif status in ("skipped", "pending"):
                skipped.append(entry)
            else:
                msgs = a.get("failureMessages") or []
                failed.append({**entry, "error": (msgs[0] if msgs else "")[:2000], "stack": ""})
    return {"passed": passed, "failed": failed, "skipped": skipped}


def _bucketize(items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Split frontend/backend test lists by category prefix in the test name.
    Categories we surface separately: security, perf, accessibility, smoke."""
    out = {"security": [], "perf": [], "accessibility": [], "other": []}
    for item in items:
        name = item.get("name", "").lower()
        if "security/" in name or "test_auth_and_authorization" in name:
            out["security"].append(item)
        elif "@perf" in name or "performance" in name or "perf " in name:
            out["perf"].append(item)
        elif "accessibility" in name or "@a11y" in name:
            out["accessibility"].append(item)
        else:
            out["other"].append(item)
    return out


def _recommend(report: dict[str, Any]) -> list[str]:
    """Generate human-readable recommendations from failure patterns."""
    recs: list[str] = []
    fe_fail = report["frontend"]["failed"]
    be_fail = report["backend"]["failed"]

    # Pattern: login / auth failures
    auth_failed = [t for t in fe_fail + be_fail
                   if "auth" in t["name"].lower() or "login" in t["name"].lower()]
    if auth_failed:
        recs.append(
            f"{len(auth_failed)} auth-related test(s) failed — check the test user password "
            "(NEW_PASSWORD_REQUIRED challenge?) and Cognito client app secret rotation."
        )

    # Pattern: many route loads failed → likely a build-time or routing break
    route_failures = [t for t in fe_fail if "route loads" in t["name"]]
    if len(route_failures) >= 3:
        recs.append(
            f"{len(route_failures)} route smoke tests failed — likely a UI build break "
            "or top-level error boundary. Check the latest commits to ui/src/App.jsx."
        )

    # Pattern: all backend tests failed → moto setup or import issue
    be_total = len(report["backend"]["passed"]) + len(be_fail)
    if be_total > 0 and len(be_fail) == be_total:
        recs.append(
            "ALL backend tests failed — likely an import error in api_handler.py or a "
            "broken conftest fixture. Inspect the first stack trace before triaging individual cases."
        )

    # Pattern: security test failures — high signal
    sec_failed = [t for t in be_fail if "security/" in t["name"].lower()
                  or "auth_and_authorization" in t["name"].lower()]
    if sec_failed:
        recs.append(
            f"{len(sec_failed)} security test(s) failed — investigate IMMEDIATELY. "
            "Check docs/SECURITY_AUDIT.md for related known issues."
        )

    # Pattern: slow pages
    slow = report["performance"]["slow_pages"]
    if slow:
        recs.append(
            f"{len(slow)} page(s) load in > {SLOW_PAGE_MS}ms: "
            + ", ".join(p["name"] for p in slow[:3])
            + (" …" if len(slow) > 3 else "")
        )

    # Pattern: lint / static analysis findings
    static = report.get("static_analysis") or {}
    total_static = sum(len(v) for v in static.values())
    if total_static > 0:
        breakdown = ", ".join(f"{k}={len(v)}" for k, v in static.items() if v)
        recs.append(f"{total_static} static-analysis finding(s) ({breakdown}). Run locally to fix.")

    if not recs and report["summary"]["failed"] == 0:
        recs.append("All tests passing. No action needed.")
    return recs


def _parse_coverage() -> dict[str, Any]:
    """Read pytest-cov's coverage.json output.

    Returns per-file + total percent_covered. Absent when pytest wasn't run
    with --cov; the report still works without it.
    """
    f = REPORTS_DIR / "coverage.json"
    if not f.exists():
        return {}
    try:
        raw = json.loads(f.read_text())
    except json.JSONDecodeError:
        return {}
    totals = raw.get("totals", {})
    files = {}
    for path, data in (raw.get("files") or {}).items():
        short = path.split("/Demo-ST21Arbiter/", 1)[-1]
        files[short] = {
            "percent": round(data.get("summary", {}).get("percent_covered", 0), 1),
            "missing_lines": data.get("summary", {}).get("missing_lines", 0),
            "num_statements": data.get("summary", {}).get("num_statements", 0),
        }
    return {
        "total_percent": round(totals.get("percent_covered", 0), 1),
        "missing_lines": totals.get("missing_lines", 0),
        "num_statements": totals.get("num_statements", 0),
        "files": files,
    }


def _parse_static_analysis() -> dict[str, list[dict[str, Any]]]:
    """Read static-analysis tool outputs from test-reports/static-*.json.

    Each tool writes its own JSON file. Missing files are treated as 'tool
    not run' (empty list). The aggregator never fails because a tool didn't
    run — only because the tool ran and found issues."""
    out: dict[str, list[dict[str, Any]]] = {"ruff": [], "eslint": [], "gitleaks": [],
                                              "npm_audit": [], "pip_audit": []}
    for tool in out.keys():
        f = REPORTS_DIR / f"static-{tool}.json"
        if not f.exists():
            continue
        try:
            raw = json.loads(f.read_text())
        except json.JSONDecodeError:
            continue
        # Normalize each tool's output to {name, severity, location, message}.
        if tool == "ruff":
            for item in raw if isinstance(raw, list) else []:
                out[tool].append({
                    "name": item.get("code", "RUFF"),
                    "severity": "warning",
                    "location": f"{item.get('filename', '')}:{(item.get('location') or {}).get('row', '')}",
                    "message": item.get("message", ""),
                })
        elif tool == "eslint":
            for file in raw if isinstance(raw, list) else []:
                for msg in file.get("messages", []):
                    out[tool].append({
                        "name": msg.get("ruleId", "eslint"),
                        "severity": "error" if msg.get("severity") == 2 else "warning",
                        "location": f"{file.get('filePath', '')}:{msg.get('line', '')}",
                        "message": msg.get("message", ""),
                    })
        # gitleaks / npm-audit / pip-audit shapes vary; left as raw passthrough
        elif isinstance(raw, dict) and "findings" in raw:
            out[tool] = raw["findings"]
        elif isinstance(raw, list):
            out[tool] = raw
    return out


def build_report() -> dict[str, Any]:
    pw = _parse_playwright(_load_json(REPORTS_DIR / "playwright-results.json"))
    py = _parse_pytest(_load_json(REPORTS_DIR / "pytest-results.json"))
    vi = _parse_vitest(_load_json(REPORTS_DIR / "vitest-results.json"))

    fe_passed = pw["passed"] + vi["passed"]
    fe_failed = pw["failed"] + vi["failed"]
    fe_skipped = pw["skipped"] + vi["skipped"]
    be_passed = py["passed"]
    be_failed = py["failed"]
    be_skipped = py["skipped"]

    total = len(fe_passed) + len(fe_failed) + len(fe_skipped) + len(be_passed) + len(be_failed) + len(be_skipped)
    passed = len(fe_passed) + len(be_passed)
    failed = len(fe_failed) + len(be_failed)
    skipped = len(fe_skipped) + len(be_skipped)
    pass_rate = f"{(passed / total * 100):.1f}%" if total else "0%"

    duration_s = 0
    for src in (REPORTS_DIR / "playwright-results.json",
                REPORTS_DIR / "pytest-results.json",
                REPORTS_DIR / "vitest-results.json"):
        raw = _load_json(src)
        if not raw:
            continue
        # Each tool reports duration differently. Sum what we can find.
        if "stats" in raw:
            duration_s += int((raw["stats"].get("duration") or 0) / 1000)
        if "duration" in raw:
            duration_s += int(raw["duration"] or 0)

    # Bucket backend tests so the Teams card and downstream consumers can
    # surface security failures as a distinct call-out.
    be_passed_buckets = _bucketize(be_passed)
    be_failed_buckets = _bucketize(be_failed)

    report = {
        "runDate": datetime.now(timezone.utc).isoformat(),
        "duration": duration_s,
        "summary": {
            "total": total, "passed": passed, "failed": failed,
            "skipped": skipped, "passRate": pass_rate,
        },
        "frontend": {"passed": fe_passed, "failed": fe_failed, "skipped": fe_skipped},
        "backend": {"passed": be_passed, "failed": be_failed, "skipped": be_skipped},
        "security": {
            "passed": be_passed_buckets["security"],
            "failed": be_failed_buckets["security"],
        },
        "performance": {
            "slow_pages": pw["slow_pages"],
            "slow_endpoints": [],  # populated when backend perf tests are added in Phase 4
        },
        "static_analysis": _parse_static_analysis(),
        "coverage": _parse_coverage(),
        "recommendations": [],
    }
    report["recommendations"] = _recommend(report)
    return report


def main() -> int:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report = build_report()
    out = REPORTS_DIR / "report.json"
    out.write_text(json.dumps(report, indent=2))
    print(f"Wrote {out}")
    print(f"Summary: {report['summary']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
