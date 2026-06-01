"""Post the aggregated report.json to a Microsoft Teams Incoming Webhook.

Renders as a MessageCard (the legacy connector schema — universally
supported by every Teams channel that has an Incoming Webhook URL, no
Adaptive Card runtime required).

Env vars:
  TEAMS_WEBHOOK_URL   — full webhook URL. If unset, falls back to writing
                        test-reports/teams-payload-<date>.json so we
                        never silently swallow the report.
  CI_RUN_URL          — optional. If set (e.g. GitHub Actions run URL),
                        included as a "View run" button.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

REPORTS_DIR = Path(os.environ.get("TEST_REPORTS_DIR", "test-reports"))


def _clean_webhook(raw: str) -> str:
    """Extract a clean webhook URL from a raw env value.

    Clipboard copies (especially from web UIs) can include invisible Unicode
    characters at the start — BOM (U+FEFF), zero-width space (U+200B),
    non-breaking space (U+00A0). Python's str.strip() does not remove those.
    `requests` then sees a URL that doesn't begin with 'https://' and raises
    MissingSchema. Defend by stripping the bad prefixes AND falling back to
    a regex extraction of the first http(s)://... substring."""
    import re
    s = (raw or "").strip().strip("﻿​\xa0‌‍").strip()
    if s.startswith(("http://", "https://")):
        return s
    m = re.search(r"https?://\S+", s)
    return m.group(0) if m else ""


WEBHOOK = _clean_webhook(os.environ.get("TEAMS_WEBHOOK_URL", ""))
CI_RUN_URL = os.environ.get("CI_RUN_URL", "").strip()
QA_AUDIT_URL = os.environ.get(
    "QA_AUDIT_URL",
    "https://github.com/ST21Coders/Demo-ST21Arbiter/blob/main/docs/QA_AUDIT_REPORT.md",
).strip()
MAX_FAILURES_SHOWN = 10  # Teams MessageCard render struggles past ~15 sections
COVERAGE_TARGET = 75.0   # combined line-coverage gate; see docs/TEST_BACKLOG.md


def _color_for(pass_rate: str) -> str:
    """Hex color (no #) by pass-rate band — matches the brief's thresholds."""
    try:
        rate = float(pass_rate.rstrip("%"))
    except ValueError:
        rate = 0.0
    if rate >= 90:
        return "2EB886"  # green
    if rate >= 70:
        return "DAA038"  # yellow
    return "D03731"     # red


def _format_failures(items: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    """Each failure → a Teams MessageCard section."""
    sections = []
    for f in items[:MAX_FAILURES_SHOWN]:
        err = (f.get("error") or "").strip()
        # Strip very long stacks — Teams will truncate the card otherwise.
        if len(err) > 600:
            err = err[:600] + "…"
        sections.append({
            "activityTitle": f"**{kind}** — {f['name']}",
            "text": f"```\n{err}\n```" if err else "_(no error message captured)_",
        })
    if len(items) > MAX_FAILURES_SHOWN:
        sections.append({
            "activityTitle": f"_…and {len(items) - MAX_FAILURES_SHOWN} more {kind} failure(s) not shown_",
        })
    return sections


def build_card(report: dict[str, Any]) -> dict[str, Any]:
    s = report["summary"]
    color = _color_for(s["passRate"])
    today = report.get("runDate", datetime.now(timezone.utc).isoformat())[:10]

    sections: list[dict[str, Any]] = [
        {
            "activityTitle": f"## Daily Test Report — {today}",
            "activitySubtitle": f"{s['passRate']} pass rate • {s['passed']}/{s['total']} passed • {report['duration']}s",
            "facts": [
                {"name": "Total", "value": str(s["total"])},
                {"name": "Passed", "value": str(s["passed"])},
                {"name": "Failed", "value": str(s["failed"])},
                {"name": "Skipped", "value": str(s["skipped"])},
            ],
        }
    ]

    if report["frontend"]["failed"]:
        sections.append({"activityTitle": f"### Frontend failures ({len(report['frontend']['failed'])})"})
        sections.extend(_format_failures(report["frontend"]["failed"], "Frontend"))

    if report["backend"]["failed"]:
        sections.append({"activityTitle": f"### Backend failures ({len(report['backend']['failed'])})"})
        sections.extend(_format_failures(report["backend"]["failed"], "Backend"))

    # Security failures are a separate, top-of-mind callout. A passing security
    # suite is still good news worth showing — green count gives confidence.
    sec = report.get("security") or {}
    sec_failed = sec.get("failed") or []
    sec_passed = sec.get("passed") or []
    if sec_failed:
        sections.append({"activityTitle": f"### SECURITY failures ({len(sec_failed)}) — investigate first"})
        sections.extend(_format_failures(sec_failed, "Security"))
    elif sec_passed:
        sections.append({
            "activityTitle": "### Security",
            "text": f"All {len(sec_passed)} security tests passing.",
        })

    # Coverage section — appears whenever pytest-cov ran.
    cov = report.get("coverage") or {}
    if cov:
        total_pct = cov.get("total_percent", 0)
        # Per-file table (sorted by lowest coverage first — those need the most attention).
        per_file = sorted(
            (cov.get("files") or {}).items(),
            key=lambda kv: kv[1].get("percent", 0),
        )
        gate = "PASS" if total_pct >= COVERAGE_TARGET else "BELOW TARGET"
        rows = [f"**{total_pct}% combined** (target {COVERAGE_TARGET}%) — {gate}"]
        for path, data in per_file[:5]:
            rows.append(f"- `{path}` — {data['percent']}% ({data['missing_lines']}/{data['num_statements']} missed)")
        sections.append({
            "activityTitle": "### Code coverage",
            "text": "\n".join(rows),
        })

    # Static analysis breakdown — only show if any tool ran AND found things.
    static = report.get("static_analysis") or {}
    static_total = sum(len(v) for v in static.values())
    if static_total > 0:
        rows = [f"- {tool}: {len(items)}" for tool, items in static.items() if items]
        sections.append({
            "activityTitle": f"### Static analysis ({static_total})",
            "text": "\n".join(rows),
        })

    slow_pages = report["performance"]["slow_pages"]
    if slow_pages:
        slow_text = "\n".join(f"- {p['name']} ({p['duration_ms']}ms)" for p in slow_pages[:5])
        sections.append({
            "activityTitle": "### Slow pages",
            "text": slow_text,
        })

    if report["recommendations"]:
        recs = "\n".join(f"- {r}" for r in report["recommendations"])
        sections.append({"activityTitle": "### Recommendations", "text": recs})

    card: dict[str, Any] = {
        "@type": "MessageCard",
        "@context": "https://schema.org/extensions",
        "themeColor": color,
        "summary": f"ARBITER tests {s['passRate']} pass — {s['failed']} failed",
        "sections": sections,
    }
    actions: list[dict[str, Any]] = []
    if CI_RUN_URL:
        actions.append({
            "@type": "OpenUri",
            "name": "View CI run",
            "targets": [{"os": "default", "uri": CI_RUN_URL}],
        })
    if QA_AUDIT_URL:
        actions.append({
            "@type": "OpenUri",
            "name": "Open QA audit report",
            "targets": [{"os": "default", "uri": QA_AUDIT_URL}],
        })
    if actions:
        card["potentialAction"] = actions
    return card


def post(card: dict[str, Any]) -> bool:
    if not WEBHOOK:
        return False
    try:
        r = requests.post(WEBHOOK, json=card, timeout=15)
        # Two success signals depending on the webhook backend:
        # - Legacy O365 Incoming Webhook: 200 OK with body "1"
        # - Power Automate Workflow (current): 202 Accepted, empty body
        #   (the workflow runs async; the trigger HTTP call only acknowledges)
        # Either is fine — treat any 2xx as success.
        if 200 <= r.status_code < 300:
            return True
        print(f"Teams webhook returned {r.status_code}: {r.text[:200]}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"Teams webhook POST failed: {e}", file=sys.stderr)
        return False


def _render_preview_html(card: dict[str, Any], report: dict[str, Any]) -> str:
    """Render the MessageCard as HTML that mimics Teams' rendering.

    Lets the user eyeball what the channel will see before wiring a webhook.
    Not pixel-perfect — Teams' renderer is closed — but conveys layout, color
    band, facts, and section text.
    """
    color = "#" + card.get("themeColor", "808080")
    summary = card.get("summary", "")
    sections_html: list[str] = []
    for sec in card.get("sections") or []:
        title = sec.get("activityTitle", "")
        subtitle = sec.get("activitySubtitle", "")
        text = sec.get("text", "")
        facts = sec.get("facts") or []
        facts_html = ""
        if facts:
            facts_html = "<table class='facts'>" + "".join(
                f"<tr><th>{f['name']}</th><td>{f['value']}</td></tr>" for f in facts
            ) + "</table>"
        # Convert MD-ish ``` blocks to <pre>; everything else passes through.
        text_html = text.replace("```\n", "<pre>").replace("\n```", "</pre>")
        text_html = text_html.replace("\n", "<br>")
        sections_html.append(
            f"<div class='section'>"
            f"<div class='title'>{title}</div>"
            f"<div class='subtitle'>{subtitle}</div>"
            f"{facts_html}"
            f"<div class='text'>{text_html}</div>"
            f"</div>"
        )
    actions_html = ""
    for a in card.get("potentialAction") or []:
        for t in a.get("targets") or []:
            actions_html += f"<a class='btn' href='{t['uri']}'>{a.get('name', 'Open')}</a>"

    return f"""<!doctype html>
<html><head><meta charset='utf-8'><title>Teams card preview</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; background: #f3f2f1; margin: 0; padding: 32px; color: #252423; }}
  .card {{ max-width: 720px; margin: 0 auto; background: white; border-radius: 6px;
           box-shadow: 0 1px 3px rgba(0,0,0,0.1); overflow: hidden; }}
  .bar {{ height: 4px; background: {color}; }}
  .summary {{ padding: 12px 20px; color: #605e5c; font-size: 13px; border-bottom: 1px solid #edebe9; }}
  .section {{ padding: 16px 20px; border-bottom: 1px solid #edebe9; }}
  .section:last-child {{ border-bottom: none; }}
  .title {{ font-size: 16px; font-weight: 600; margin-bottom: 4px; }}
  .subtitle {{ color: #605e5c; font-size: 13px; margin-bottom: 12px; }}
  .facts {{ border-collapse: collapse; margin: 8px 0; }}
  .facts th {{ text-align: left; padding: 4px 16px 4px 0; color: #605e5c; font-weight: 400; }}
  .facts td {{ padding: 4px 0; font-weight: 600; }}
  .text {{ font-size: 14px; line-height: 1.5; }}
  pre {{ background: #f3f2f1; padding: 10px; border-radius: 4px; font-size: 12px;
         overflow-x: auto; white-space: pre-wrap; word-break: break-word; }}
  .actions {{ padding: 16px 20px; background: #faf9f8; }}
  .btn {{ display: inline-block; padding: 8px 16px; background: #6264a7; color: white;
          text-decoration: none; border-radius: 4px; font-size: 14px; }}
  .meta {{ max-width: 720px; margin: 16px auto 0; color: #605e5c; font-size: 12px; }}
</style></head>
<body>
  <div class='card'>
    <div class='bar'></div>
    <div class='summary'>{summary}</div>
    {''.join(sections_html)}
    {f"<div class='actions'>{actions_html}</div>" if actions_html else ""}
  </div>
  <div class='meta'>
    Preview of the Teams MessageCard that would be posted to <code>TEAMS_WEBHOOK_URL</code>.
    Generated by testing/teams_poster.py — not pixel-perfect.
  </div>
</body></html>
"""


def main() -> int:
    report_path = REPORTS_DIR / "report.json"
    if not report_path.exists():
        print(f"No report at {report_path} — run report_generator.py first.", file=sys.stderr)
        return 2
    report = json.loads(report_path.read_text())
    card = build_card(report)

    # Always write the preview artifacts so a human can inspect what Teams
    # would receive, regardless of whether the webhook is configured.
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    date = report.get("runDate", datetime.now(timezone.utc).isoformat())[:10]
    payload_path = REPORTS_DIR / f"teams-payload-{date}.json"
    payload_path.write_text(json.dumps(card, indent=2))
    preview_path = REPORTS_DIR / f"teams-preview-{date}.html"
    preview_path.write_text(_render_preview_html(card, report))
    print(f"Wrote {payload_path}")
    print(f"Wrote {preview_path}  ← open this in a browser to see the card")

    if not WEBHOOK:
        print("TEAMS_WEBHOOK_URL not set — preview only, no post attempted.")
        return 0
    if post(card):
        print("Posted report to Teams.")
        return 0
    print("Teams POST failed — see preview artifacts above.", file=sys.stderr)
    return 0  # not a hard failure; report + preview are still on disk


if __name__ == "__main__":
    sys.exit(main())
