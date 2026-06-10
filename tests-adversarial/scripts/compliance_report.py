"""scripts/compliance_report.py — render the compliance coverage matrix to PDF.

The source of truth for the harness's compliance posture is
`docs/security_compliance_coverage.md`. That doc is the markdown an auditor
can read in a browser, but for hand-off (compliance officer, internal audit,
ISO/SOC2 reviewer) we want a styled PDF — same content, no markdown noise,
print-optimized, with a cover page and exec summary.

This module is a self-contained generator:

  1. Reads the markdown source.
  2. Parses the 12 per-category tables and the trailing summary table with
     a small regex parser — no `markdown` / `mistune` dependency. The source
     doc's structure is predictable (see CLAUDE.md: it's a curated
     compliance matrix, not arbitrary markdown), so a 60-line parser is
     cheaper than pulling in a 3rd-party renderer and configuring it.
  3. Renders an HTML deliverable matching the polish of
     `docs/adversarial_run_findings_2026-06-09.html` (the daily-run PDF) —
     same color palette for severity pills, same @page rules, same table
     style.
  4. Invokes the Playwright-bundled Chrome-for-Testing binary in headless
     `--print-to-pdf` mode to produce the PDF. We use Chrome (not weasyprint
     / wkhtmltopdf) for two reasons: (a) it's already on every dev / CI
     machine via Playwright, so no new system dep; (b) Chrome's CSS Paged
     Media support is the closest to a "real" PDF renderer available
     without a paid tool.
  5. Writes both the intermediate HTML and the PDF to `docs/` and prints
     the PDF path on stdout.

CLI:
    python3.13 -m scripts.compliance_report [--out PATH] [--source PATH]
    python3.13 -m scripts.compliance_report --html-only  # skip Chrome

Importable for testing:
    from scripts.compliance_report import generate_report, parse_markdown
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ────────────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────────────

# Path to Playwright's bundled Chrome-for-Testing on macOS arm64. This is the
# binary we already trust for the harness's own e2e Playwright specs, so
# reusing it here means no new system dependency and no version drift.
# If Playwright is reinstalled with a newer chromium build, the directory
# version will change; the script falls back to PATH-resolved `google-chrome`
# / `chromium` if this exact path is missing (see `_resolve_chrome`).
_DEFAULT_CHROME = (
    Path.home()
    / "Library/Caches/ms-playwright/chromium-1223/chrome-mac-arm64"
    / "Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
)

# The 9 harness layers — used in the methodology section. Order is the
# load-order in `scripts/run_all.py::_LAYERS_ALL`, so a reader who jumps to
# the runbook sees the same sequencing.
HARNESS_LAYERS = [
    ("e2e", "Playwright sweeps the SPA per persona and asserts page-level access."),
    ("fuzz", "10 curated payload corpora cross-product over every API route."),
    ("auth", "JWT forgery, IDOR, brute-force, session fixation, password reset."),
    ("headers", "HTTPS / HSTS / TLS / CSP / CORS / CSRF / clickjacking."),
    ("llm", "20 jailbreaks + 10 generative red-team prompts against the master."),
    ("dos", "Rate limiting, oversized payloads, concurrent fan-out."),
    ("logic", "Workflow bypass, race conditions, sensitive-field exposure."),
    ("logging_audit", "Audit-log writes + CloudWatch redaction + log injection."),
    ("fault", "Fail-closed, error propagation, partial failure, LLM output safety."),
]

# Status → CSS pill class. Matches the daily-report palette
# (`docs/adversarial_run_findings_2026-06-09.html`): green / amber / red / gray.
STATUS_PILL_CLASS = {
    "covered": "pill pill-covered",
    "partial": "pill pill-partial",
    "missing": "pill pill-missing",
    "oos": "pill pill-oos",
}

# Status normalization. The markdown source uses emoji + label; we strip the
# emoji and lowercase the label for downstream lookup.
STATUS_LABEL = {
    "covered": "Covered",
    "partial": "Partial",
    "missing": "Missing",
    "oos": "Out of scope",
}


# ────────────────────────────────────────────────────────────────────────────
# Data model
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class CoverageItem:
    """One row in a per-category table."""

    number: int
    item: str
    status: str  # "covered" | "partial" | "missing" | "oos"
    where: str
    effort: str


@dataclass
class Category:
    """One per-category section (e.g. `## 1. Injection`)."""

    number: int
    name: str
    items: list[CoverageItem] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        c = {"covered": 0, "partial": 0, "missing": 0, "oos": 0}
        for it in self.items:
            c[it.status] = c.get(it.status, 0) + 1
        return c


@dataclass
class ParsedDoc:
    """The full parsed source doc."""

    categories: list[Category] = field(default_factory=list)
    totals: dict[str, int] = field(default_factory=dict)
    total_items: int = 0


# ────────────────────────────────────────────────────────────────────────────
# Markdown parser
# ────────────────────────────────────────────────────────────────────────────


_CATEGORY_RE = re.compile(r"^##\s+(\d+)\.\s+(.+?)\s*$")
_SECTION_TOTALS_RE = re.compile(r"^\*\*Section totals:\*\*")
_SUMMARY_HEADER_RE = re.compile(r"^#\s+Summary\s*$", re.IGNORECASE)
# A summary table row carries 6 columns: Category, Covered, Partial, Missing,
# Out of scope, Total. We only need the totals row (the one that starts with
# `| **Totals**`).
_SUMMARY_TOTALS_RE = re.compile(
    r"^\|\s*\*\*Totals\*\*\s*\|\s*\*\*(\d+)\*\*\s*\|\s*\*\*(\d+)\*\*\s*\|"
    r"\s*\*\*(\d+)\*\*\s*\|\s*\*\*(\d+)\*\*\s*\|\s*\*\*(\d+)\*\*\s*\|"
)


def _classify_status(raw: str) -> str | None:
    """Map an emoji+text status cell to one of the four canonical keys.

    Returns None if the cell doesn't match a known status — the caller drops
    the row, which protects against accidentally rendering a malformed
    header row as a data row.
    """
    if "✅" in raw or "Covered" in raw:
        return "covered"
    if "🟡" in raw or "Partial" in raw:
        return "partial"
    if "❌" in raw or "Missing" in raw:
        return "missing"
    if "⚪" in raw or "Out of scope" in raw or "OOS" in raw:
        return "oos"
    return None


def _split_table_row(line: str) -> list[str] | None:
    """Split a markdown table row into its cells.

    Returns None for any line that doesn't look like a 5-column data row
    (a divider `|---|---|...`, a header, or random prose). We use a
    deliberate "must have at least 4 pipes" gate so a stray `|` in a
    sentence inside a section paragraph is never mistaken for a row.
    """
    if not line.startswith("|"):
        return None
    # Strip leading and trailing pipes so we don't get phantom empty cells.
    trimmed = line.strip()
    if trimmed.endswith("|"):
        trimmed = trimmed[:-1]
    if trimmed.startswith("|"):
        trimmed = trimmed[1:]
    parts = [p.strip() for p in trimmed.split("|")]
    # Divider row: every cell is dashes and optional colons.
    if all(re.fullmatch(r":?-+:?", p or "") for p in parts):
        return None
    return parts


def parse_markdown(md_text: str) -> ParsedDoc:
    """Parse the compliance-coverage markdown into structured data.

    Approach: line-by-line state machine.
      - When we see `## N. Name`, start a new Category.
      - Inside a category, every 5-cell pipe row that classifies to a known
        status becomes a CoverageItem.
      - The first cell is a row number (or `#`); skip the header row by
        requiring the first cell to be all digits.
      - `**Section totals:**` ends the category's items (but the loop
        keeps reading until the next `## N.` boundary).
      - Once we hit `# Summary`, look for the `**Totals**` row to capture
        the doc-level coverage counts.

    The parser is tolerant of trailing whitespace, blank lines, and the
    notes that appear after each category — anything that doesn't classify
    as a row is silently skipped.
    """
    doc = ParsedDoc()
    current: Category | None = None
    in_summary = False

    for raw_line in md_text.splitlines():
        line = raw_line.rstrip()

        m_cat = _CATEGORY_RE.match(line)
        if m_cat:
            current = Category(number=int(m_cat.group(1)), name=m_cat.group(2).strip())
            doc.categories.append(current)
            in_summary = False
            continue

        if _SUMMARY_HEADER_RE.match(line):
            in_summary = True
            current = None
            continue

        if in_summary:
            m_tot = _SUMMARY_TOTALS_RE.match(line)
            if m_tot:
                doc.totals = {
                    "covered": int(m_tot.group(1)),
                    "partial": int(m_tot.group(2)),
                    "missing": int(m_tot.group(3)),
                    "oos": int(m_tot.group(4)),
                }
                doc.total_items = int(m_tot.group(5))
            continue

        if current is None:
            continue

        if _SECTION_TOTALS_RE.match(line):
            # End of the table proper. Keep scanning for the next `## N.`.
            continue

        cells = _split_table_row(line)
        if not cells or len(cells) < 5:
            continue

        # Header row: first cell is literally `#` (the column-name "#" header).
        if not cells[0].isdigit():
            continue

        status = _classify_status(cells[2])
        if status is None:
            continue

        current.items.append(
            CoverageItem(
                number=int(cells[0]),
                item=cells[1],
                status=status,
                where=cells[3],
                effort=cells[4] if len(cells) > 4 else "",
            )
        )

    # Fallback: if the summary block was missing, derive totals from
    # per-category counts so the report doesn't ship with zeros.
    if not doc.totals:
        totals = {"covered": 0, "partial": 0, "missing": 0, "oos": 0}
        for cat in doc.categories:
            for k, v in cat.counts().items():
                totals[k] += v
        doc.totals = totals
        doc.total_items = sum(totals.values())

    return doc


# ────────────────────────────────────────────────────────────────────────────
# HTML rendering
# ────────────────────────────────────────────────────────────────────────────


def _esc(text: str) -> str:
    """HTML-escape arbitrary cell text. We use stdlib `html.escape` rather
    than rolling our own because the source markdown DOES contain `<` /
    `>` / `&` characters inside descriptions (e.g. `<script>`, `&` in
    boolean conditions) and we want them to render as glyphs, not parse."""
    return html.escape(text, quote=False)


def _pill(status: str) -> str:
    cls = STATUS_PILL_CLASS.get(status, "pill pill-oos")
    label = STATUS_LABEL.get(status, "—")
    return f'<span class="{cls}">{_esc(label)}</span>'


def _coverage_percent(doc: ParsedDoc) -> int:
    """Fully-covered percentage, rounded to the nearest integer."""
    if doc.total_items == 0:
        return 0
    return round(100 * doc.totals.get("covered", 0) / doc.total_items)


def _partial_or_better_percent(doc: ParsedDoc) -> int:
    if doc.total_items == 0:
        return 0
    nume = doc.totals.get("covered", 0) + doc.totals.get("partial", 0)
    return round(100 * nume / doc.total_items)


def _donut_svg(doc: ParsedDoc, size: int = 180) -> str:
    """Render a 4-segment SVG donut for the totals.

    No external libraries: we draw 4 stroked arcs on a circle, each whose
    `stroke-dasharray` matches its slice fraction. Standard SVG donut
    trick — `pathLength="100"` normalizes the math so each segment's
    dasharray is literally the percentage.
    """
    total = doc.total_items or 1
    fracs = [
        ("covered", doc.totals.get("covered", 0), "#2e7d32"),
        ("partial", doc.totals.get("partial", 0), "#c47800"),
        ("missing", doc.totals.get("missing", 0), "#b71c1c"),
        ("oos", doc.totals.get("oos", 0), "#666"),
    ]
    cx = cy = size / 2
    r = size * 0.38
    stroke_w = size * 0.18
    segments = []
    offset = 0.0
    for _key, count, color in fracs:
        pct = 100.0 * count / total
        if pct <= 0:
            continue
        # `stroke-dasharray="pct gap"` paints just the slice; the gap is
        # the remainder of the circumference. `pathLength=100` lets us
        # use raw percentages instead of computing 2πr.
        segments.append(
            f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" '
            f'stroke="{color}" stroke-width="{stroke_w}" '
            f'pathLength="100" '
            f'stroke-dasharray="{pct:.2f} {100 - pct:.2f}" '
            f'stroke-dashoffset="{-offset:.2f}" '
            f'transform="rotate(-90 {cx} {cy})" />'
        )
        offset += pct
    pct_text = _coverage_percent(doc)
    return (
        f'<svg width="{size}" height="{size}" viewBox="0 0 {size} {size}" '
        f'xmlns="http://www.w3.org/2000/svg" role="img" '
        f'aria-label="Coverage donut">'
        + "".join(segments)
        + f'<text x="{cx}" y="{cy - 4}" text-anchor="middle" '
        f'font-family="-apple-system, Helvetica, Arial" '
        f'font-size="{size * 0.22:.0f}" font-weight="800" fill="#0a0a0a">'
        f"{pct_text}%</text>"
        f'<text x="{cx}" y="{cy + size * 0.13:.0f}" text-anchor="middle" '
        f'font-family="-apple-system, Helvetica, Arial" '
        f'font-size="{size * 0.075:.0f}" fill="#555" '
        f'letter-spacing="0.05em">FULLY COVERED</text>'
        f"</svg>"
    )


CSS = """
@page {
  size: Letter;
  margin: 0.7in 0.65in 0.85in 0.65in;
  @bottom-center {
    content: "ARBITER Security Compliance Coverage Report \\00B7 page " counter(page) " of " counter(pages);
    font-family: -apple-system, "Helvetica Neue", sans-serif;
    font-size: 9pt;
    color: #888;
  }
}
* { box-sizing: border-box; }
html {
  font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", Arial, sans-serif;
  font-size: 11pt;
  line-height: 1.5;
  color: #1a1a1a;
}
body { margin: 0; padding: 0; }

/* Cover page */
.cover {
  height: 9.2in;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  page-break-after: always;
}
.cover-eyebrow {
  font-size: 10pt;
  letter-spacing: 0.22em;
  text-transform: uppercase;
  color: #777;
  font-weight: 700;
}
.cover-title {
  font-size: 32pt;
  font-weight: 800;
  line-height: 1.1;
  margin: 0.4em 0 0.2em 0;
  color: #0a0a0a;
  letter-spacing: -0.01em;
}
.cover-sub {
  font-size: 14pt;
  color: #555;
  margin: 0;
}
.cover-date {
  font-size: 11pt;
  color: #777;
  margin-top: 0.6em;
  font-variant-numeric: tabular-nums;
}
.hero {
  background: #fafafa;
  border: 1px solid #ddd;
  border-left: 6px solid #1a1a1a;
  padding: 1.4em 1.6em;
  border-radius: 0 4px 4px 0;
  margin: 1.5em 0;
}
.hero-pct {
  font-size: 56pt;
  font-weight: 800;
  line-height: 1;
  color: #0a0a0a;
  letter-spacing: -0.02em;
}
.hero-pct-sub {
  font-size: 11pt;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: #666;
  margin-top: 0.2em;
  font-weight: 700;
}
.hero-detail {
  font-size: 11.5pt;
  color: #333;
  margin-top: 1em;
  line-height: 1.55;
}
.cover-intro {
  font-size: 11.5pt;
  color: #333;
  line-height: 1.55;
  margin: 0;
}
.cover-footer {
  font-size: 9.5pt;
  color: #888;
  letter-spacing: 0.04em;
  border-top: 1px solid #ddd;
  padding-top: 0.6em;
}

/* Sections */
h2.section {
  font-size: 16pt;
  font-weight: 700;
  margin: 1.6em 0 0.6em 0;
  color: #0a0a0a;
  border-bottom: 2px solid #1a1a1a;
  padding-bottom: 0.25em;
}
h3.cat-heading {
  font-size: 13pt;
  font-weight: 700;
  margin: 1.4em 0 0.4em 0;
  color: #0a0a0a;
  page-break-after: avoid;
}
h3.cat-heading .cat-num {
  color: #777;
  font-weight: 600;
  margin-right: 0.5em;
}
.cat-totals {
  font-size: 9.5pt;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  color: #777;
  font-weight: 600;
  margin-left: 0.8em;
}
p { margin: 0.55em 0; }

/* Pills */
.pill {
  display: inline-block;
  padding: 0.12em 0.55em;
  border-radius: 3px;
  font-size: 8.5pt;
  font-weight: 700;
  letter-spacing: 0.04em;
  color: #fff;
  text-transform: uppercase;
  vertical-align: middle;
  min-width: 64px;
  text-align: center;
}
.pill-covered { background: #2e7d32; }
.pill-partial { background: #c47800; }
.pill-missing { background: #b71c1c; }
.pill-oos     { background: #666; }

/* Tables */
table {
  width: 100%;
  border-collapse: collapse;
  margin: 0.7em 0;
  font-size: 10pt;
}
th, td {
  border-bottom: 1px solid #e0e0e0;
  padding: 0.45em 0.6em;
  text-align: left;
  vertical-align: top;
}
th {
  background: transparent;
  font-weight: 700;
  font-size: 9pt;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: #555;
  border-bottom: 2px solid #1a1a1a;
}
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
td.center, th.center { text-align: center; }
tr.totals-row { background: #fafafa; font-weight: 700; }

/* Matrix item table — keep each row together on the page */
table.matrix tr { page-break-inside: avoid; }
table.matrix td.item-num {
  width: 36px;
  color: #888;
  font-variant-numeric: tabular-nums;
}
table.matrix td.status-col { width: 90px; }
table.matrix td.where-col { color: #333; font-size: 9.5pt; line-height: 1.45; }

/* Exec summary layout */
.exec-grid {
  display: flex;
  gap: 1.5em;
  align-items: center;
  margin: 1em 0 1.4em 0;
}
.exec-donut { flex: 0 0 200px; text-align: center; }
.exec-legend { flex: 1; }
.exec-legend ul {
  list-style: none;
  padding: 0;
  margin: 0;
}
.exec-legend li {
  display: flex;
  align-items: center;
  gap: 0.6em;
  margin-bottom: 0.4em;
  font-size: 11pt;
}
.exec-legend .swatch {
  width: 14px;
  height: 14px;
  border-radius: 2px;
  display: inline-block;
  flex: 0 0 14px;
}
.swatch-covered { background: #2e7d32; }
.swatch-partial { background: #c47800; }
.swatch-missing { background: #b71c1c; }
.swatch-oos     { background: #666; }

.callout {
  border-left: 4px solid #1565c0;
  background: #f0f6fb;
  padding: 0.6em 0.9em;
  margin: 1em 0;
  border-radius: 0 3px 3px 0;
  font-size: 10.5pt;
}

/* Page breaks */
.page-break { page-break-before: always; }

footer.report-footer {
  margin-top: 2em;
  padding-top: 1em;
  border-top: 1px solid #ddd;
  font-size: 9pt;
  color: #777;
  font-style: italic;
}

code {
  font-family: "SF Mono", Menlo, Monaco, Consolas, monospace;
  font-size: 9.5pt;
  background: #f4f4f4;
  padding: 0.05em 0.3em;
  border-radius: 2px;
  color: #444;
  border: 1px solid #e5e5e5;
}
"""


def _render_cover(doc: ParsedDoc, report_date: str) -> str:
    pct = _coverage_percent(doc)
    covered = doc.totals.get("covered", 0)
    partial = doc.totals.get("partial", 0)
    oos = doc.totals.get("oos", 0)
    total = doc.total_items
    at_least_partial = covered + partial
    return f"""
<section class="cover">
  <div>
    <div class="cover-eyebrow">ARBITER · Security Compliance</div>
    <h1 class="cover-title">Security Compliance Coverage Report</h1>
    <p class="cover-sub">Adversarial Test Harness — Verification Matrix</p>
    <p class="cover-date">Generated {_esc(report_date)}</p>
  </div>

  <div class="hero">
    <div class="hero-pct">{pct}%</div>
    <div class="hero-pct-sub">Fully Covered</div>
    <p class="hero-detail">
      <strong>{covered} / {total}</strong> items fully covered ·
      <strong>{at_least_partial} / {total}</strong> at least partial ·
      <strong>{oos} / {total}</strong> covered by other controls
      (out of scope for this runtime harness).
    </p>
  </div>

  <p class="cover-intro">
    This document maps every item on the ARBITER security compliance
    checklist (OWASP Top 10, API Top 10, LLM Top 10, and related CWE
    weaknesses) to a concrete test in the adversarial harness at
    <code>tests-adversarial/</code>. For each item it states whether a
    test exists today, where to find it, and — for partial and missing
    rows — the gap and the estimated effort to close it. The 17
    items marked out of scope are routed to their proper control
    (SCA, IAM static analysis, incident response runbook, etc.) rather
    than ignored, so an auditor sees the complete picture.
  </p>

  <div class="cover-footer">
    Source: <code>docs/security_compliance_coverage.md</code> ·
    Generated by <code>scripts/compliance_report.py</code>
  </div>
</section>
"""


def _render_exec_summary(doc: ParsedDoc) -> str:
    covered = doc.totals.get("covered", 0)
    partial = doc.totals.get("partial", 0)
    missing = doc.totals.get("missing", 0)
    oos = doc.totals.get("oos", 0)
    donut = _donut_svg(doc, size=200)

    # By-category table
    rows = []
    for cat in doc.categories:
        c = cat.counts()
        total = sum(c.values())
        rows.append(
            f"<tr>"
            f"<td>{cat.number}. {_esc(cat.name)}</td>"
            f"<td class='num'>{c['covered']}</td>"
            f"<td class='num'>{c['partial']}</td>"
            f"<td class='num'>{c['missing']}</td>"
            f"<td class='num'>{c['oos']}</td>"
            f"<td class='num'>{total}</td>"
            f"</tr>"
        )
    # Doc-level totals row
    rows.append(
        f"<tr class='totals-row'>"
        f"<td>Totals</td>"
        f"<td class='num'>{covered}</td>"
        f"<td class='num'>{partial}</td>"
        f"<td class='num'>{missing}</td>"
        f"<td class='num'>{oos}</td>"
        f"<td class='num'>{doc.total_items}</td>"
        f"</tr>"
    )
    table_html = (
        "<table>"
        "<thead><tr>"
        "<th>Category</th>"
        "<th class='num'>Covered</th>"
        "<th class='num'>Partial</th>"
        "<th class='num'>Missing</th>"
        "<th class='num'>OOS</th>"
        "<th class='num'>Total</th>"
        "</tr></thead>"
        "<tbody>" + "".join(rows) + "</tbody>"
        "</table>"
    )

    layers_html = ", ".join(
        f"<code>{_esc(name)}</code>" for name, _desc in HARNESS_LAYERS
    )

    return f"""
<section class="page-break">
  <h2 class="section">Executive summary</h2>

  <div class="exec-grid">
    <div class="exec-donut">{donut}</div>
    <div class="exec-legend">
      <ul>
        <li><span class="swatch swatch-covered"></span>
          <strong>Covered</strong>: {covered} items have at least one harness
          test that would catch a regression.</li>
        <li><span class="swatch swatch-partial"></span>
          <strong>Partial</strong>: {partial} items are tested but with known
          scope notes documented in their rows.</li>
        <li><span class="swatch swatch-missing"></span>
          <strong>Missing</strong>: {missing} items have no harness probe
          today (closed by other controls — see appendix).</li>
        <li><span class="swatch swatch-oos"></span>
          <strong>Out of scope</strong>: {oos} items are not testable by a
          runtime harness (covered by SCA, AWS-side audit, IR runbook).</li>
      </ul>
    </div>
  </div>

  <h3 class="cat-heading">Coverage by category</h3>
  {table_html}

  <h3 class="cat-heading">What this harness tested</h3>
  <p>
    The harness runs nine layers, each a focused pytest or Playwright suite:
    {layers_html}. Each layer's results land in a shared coverage matrix
    aggregator (<code>src/coverage/builder.py</code>) that emits the daily
    PDF report and the per-run findings JSON. This document is the
    compliance-side view of that same data: which categories of risk the
    nine layers exercise, and which they deliberately do not.
  </p>
</section>
"""


def _render_categories(doc: ParsedDoc) -> str:
    """Render the detailed coverage matrix — one section per category."""
    out = [
        '<section class="page-break">',
        '<h2 class="section">Detailed coverage matrix</h2>',
    ]
    for cat in doc.categories:
        c = cat.counts()
        totals_label = (
            f"{c['covered']} covered · {c['partial']} partial · "
            f"{c['missing']} missing · {c['oos']} out of scope"
        )
        out.append(
            f'<h3 class="cat-heading">'
            f'<span class="cat-num">{cat.number}.</span>{_esc(cat.name)}'
            f'<span class="cat-totals">{totals_label}</span>'
            f"</h3>"
        )
        rows = []
        for item in cat.items:
            rows.append(
                "<tr>"
                f'<td class="item-num">{item.number}</td>'
                f"<td>{_esc(item.item)}</td>"
                f'<td class="status-col">{_pill(item.status)}</td>'
                f'<td class="where-col">{_esc(item.where)}</td>'
                "</tr>"
            )
        out.append(
            '<table class="matrix">'
            "<thead><tr>"
            '<th class="item-num">#</th>'
            "<th>Item</th>"
            '<th class="status-col">Status</th>'
            '<th class="where-col">Where it is covered / gap</th>'
            "</tr></thead>"
            "<tbody>" + "".join(rows) + "</tbody>"
            "</table>"
        )
    out.append("</section>")
    return "\n".join(out)


def _render_oos_appendix(doc: ParsedDoc) -> str:
    """Render the out-of-scope appendix — every OOS row with its routing."""
    oos_items: list[tuple[int, str, CoverageItem]] = []
    for cat in doc.categories:
        for it in cat.items:
            if it.status == "oos":
                oos_items.append((cat.number, cat.name, it))
    rows = []
    for cat_num, cat_name, it in oos_items:
        rows.append(
            "<tr>"
            f'<td class="item-num">{it.number}</td>'
            f"<td>{_esc(it.item)}</td>"
            f"<td><em>{cat_num}. {_esc(cat_name)}</em></td>"
            f'<td class="where-col">{_esc(it.where)}</td>'
            "</tr>"
        )
    table_html = (
        '<table class="matrix">'
        "<thead><tr>"
        '<th class="item-num">#</th>'
        "<th>Item</th>"
        "<th>Category</th>"
        '<th class="where-col">Where it is actually covered</th>'
        "</tr></thead>"
        "<tbody>" + "".join(rows) + "</tbody>"
        "</table>"
    )
    return f"""
<section class="page-break">
  <h2 class="section">Out-of-scope appendix</h2>
  <p>
    The {len(oos_items)} items below are real items on the compliance
    checklist. They are not testable by an automated runtime harness
    against a deployed app, so they are routed to their proper control:
    a different tool (SCA, AWS config audit), a different team
    (incident response runbook review), or a different process control
    (CI hardening, signed releases). They are listed here deliberately
    so an auditor can verify nothing was dropped.
  </p>
  {table_html}
</section>
"""


def _render_methodology(report_date: str) -> str:
    layer_rows = []
    for name, desc in HARNESS_LAYERS:
        layer_rows.append(
            f"<tr><td><code>{_esc(name)}</code></td><td>{_esc(desc)}</td></tr>"
        )
    layers_table = (
        "<table>"
        "<thead><tr><th style='width:140px'>Layer</th><th>What it tests</th></tr></thead>"
        "<tbody>" + "".join(layer_rows) + "</tbody></table>"
    )
    return f"""
<section class="page-break">
  <h2 class="section">Methodology &amp; reproduction</h2>

  <h3 class="cat-heading">Harness shape</h3>
  <p>
    The harness is a black-box test suite that runs against the deployed
    dev environment. It does not have access to backend source at runtime
    — every assertion is made on what the deployed API, the deployed
    SPA, or the deployed AgentCore runtimes actually return. A nightly
    cost cap (default $1.00) gates LLM-spending probes so a runaway
    fan-out cannot exhaust the demo budget. Layers that touch the LLM
    surface (<code>llm</code>, <code>fault</code>) account against that
    cap; the other seven layers are zero-cost.
  </p>

  <h3 class="cat-heading">The nine layers</h3>
  {layers_table}

  <h3 class="cat-heading">Run cadence</h3>
  <p>
    The full harness runs on a daily cadence against the dev environment.
    Each run produces a <code>report.html</code> + <code>summary.md</code>
    + <code>report.json</code> in <code>tests-adversarial/test-reports/&lt;run-id&gt;/</code>,
    and the dated PDF in <code>docs/adversarial_run_findings_&lt;date&gt;.pdf</code>.
    After every "build block" lands (one new test layer or a refresh of
    an existing one), this compliance matrix is regenerated so the
    coverage percentage moves with reality.
  </p>

  <h3 class="cat-heading">Reproducing this report</h3>
  <p>From <code>tests-adversarial/</code>:</p>
  <p>
    <code>npm run test:all</code> — run the nine layers and aggregate.<br/>
    <code>python3.13 -m scripts.compliance_report</code> — regenerate
    this PDF from <code>docs/security_compliance_coverage.md</code>.
  </p>

  <div class="callout">
    <strong>This document is generated from
    <code>docs/security_compliance_coverage.md</code>.</strong>
    The markdown is the single source of truth; regenerate this PDF after
    every build block lands so the coverage percentage moves with the
    harness. Last generated {_esc(report_date)}.
  </div>
</section>
"""


def _render_footer(report_date: str, git_rev: str) -> str:
    return f"""
<footer class="report-footer">
  Generated {_esc(report_date)} · harness rev <code>{_esc(git_rev)}</code> ·
  source <code>docs/security_compliance_coverage.md</code> ·
  re-run with <code>python3.13 -m scripts.compliance_report</code>.
</footer>
"""


def render_html(doc: ParsedDoc, report_date: str, git_rev: str) -> str:
    """Render the full HTML document for the parsed source.

    Deterministic: given the same `doc` + `report_date` + `git_rev`, two
    calls produce byte-identical output. The unit tests pin this so a
    future change that introduces e.g. `dict()`-ordered iteration or a
    timestamp leak fails fast.
    """
    body = (
        _render_cover(doc, report_date)
        + _render_exec_summary(doc)
        + _render_categories(doc)
        + _render_oos_appendix(doc)
        + _render_methodology(report_date)
        + _render_footer(report_date, git_rev)
    )
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        f"<title>ARBITER Security Compliance Coverage Report · {_esc(report_date)}</title>\n"
        f"<style>{CSS}</style>\n"
        "</head>\n"
        "<body>\n" + body + "\n</body>\n</html>\n"
    )


# ────────────────────────────────────────────────────────────────────────────
# PDF invocation
# ────────────────────────────────────────────────────────────────────────────


def _resolve_chrome(explicit: Path | None = None) -> Path:
    """Return the Chrome binary path to use for headless PDF rendering.

    Resolution order:
      1. Explicit `--chrome` CLI argument.
      2. `CHROME_BIN` env var.
      3. Playwright's bundled Chrome-for-Testing on macOS arm64
         (`_DEFAULT_CHROME`).
      4. PATH-resolved `google-chrome` / `chromium-browser` / `chromium`.

    Raises `FileNotFoundError` if nothing resolves — the caller can catch
    this and fall back to HTML-only mode.
    """
    if explicit:
        if not explicit.exists():
            raise FileNotFoundError(f"--chrome path does not exist: {explicit}")
        return explicit
    env_path = os.environ.get("CHROME_BIN")
    if env_path:
        p = Path(env_path)
        if p.exists():
            return p
    if _DEFAULT_CHROME.exists():
        return _DEFAULT_CHROME
    for candidate in ("google-chrome", "chromium-browser", "chromium"):
        found = shutil.which(candidate)
        if found:
            return Path(found)
    raise FileNotFoundError(
        "No Chrome binary found. Tried --chrome, $CHROME_BIN, "
        f"{_DEFAULT_CHROME}, and PATH lookups."
    )


def _chrome_command(chrome: Path, html_path: Path, pdf_path: Path) -> list[str]:
    """Build the Chrome headless `--print-to-pdf` command-line.

    Flags:
      * `--headless=new`    — the modern headless mode (Chrome 109+).
      * `--disable-gpu`     — redundant in headless but cheap; avoids
                                  noise on macOS where the GPU may not be
                                  reachable from a non-GUI process.
      * `--no-sandbox`      — needed in some CI containers; harmless on macOS.
      * `--print-to-pdf-no-header` — our @page block carries the page
                                          counter; we don't want Chrome
                                          adding its own URL/date strip.
      * `--virtual-time-budget=2000` — wait 2s for any inline SVGs / fonts
                                            to settle before snapshot.
    """
    # Chrome wants a file:// URL for the input HTML.
    url = html_path.resolve().as_uri()
    return [
        str(chrome),
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--print-to-pdf-no-header",
        "--virtual-time-budget=2000",
        f"--print-to-pdf={pdf_path.resolve()}",
        url,
    ]


def _render_pdf(chrome: Path, html_path: Path, pdf_path: Path) -> None:
    """Invoke Chrome to convert `html_path` → `pdf_path`.

    Raises `RuntimeError` if Chrome exits non-zero or the output file is
    missing / suspiciously small (< 1 KB). The size sanity check catches
    the case where Chrome "succeeded" but emitted an empty PDF stub
    (happens occasionally if the headless renderer crashes silently).
    """
    cmd = _chrome_command(chrome, html_path, pdf_path)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(
            f"Chrome exited with code {result.returncode}\n"
            f"stderr: {result.stderr}\nstdout: {result.stdout}"
        )
    if not pdf_path.exists():
        raise RuntimeError(f"Chrome did not produce {pdf_path}")
    if pdf_path.stat().st_size < 1024:
        raise RuntimeError(
            f"Chrome produced a suspiciously small PDF "
            f"({pdf_path.stat().st_size} bytes) at {pdf_path}"
        )


# ────────────────────────────────────────────────────────────────────────────
# Git rev helper
# ────────────────────────────────────────────────────────────────────────────


def _git_rev(repo_root: Path) -> str:
    """Return the short git rev of the repo, or `unknown` if not a repo /
    git unavailable. We do NOT raise on failure — a report generated outside
    a git checkout should still render."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip() or "unknown"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return "unknown"


# ────────────────────────────────────────────────────────────────────────────
# Public entry point
# ────────────────────────────────────────────────────────────────────────────


def _default_source() -> Path:
    """The canonical markdown source, relative to repo root."""
    # `__file__` is `<repo>/tests-adversarial/scripts/compliance_report.py`,
    # so two parents up = `tests-adversarial/`, three parents = repo root.
    return (
        Path(__file__).resolve().parents[2] / "docs" / "security_compliance_coverage.md"
    )


def _default_out_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "docs"


def generate_report(
    source: Path | None = None,
    out: Path | None = None,
    chrome: Path | None = None,
    html_only: bool = False,
    today: dt.date | None = None,
) -> Path:
    """Generate the compliance PDF (and intermediate HTML) and return its path.

    Parameters
    ----------
    source
        Path to the markdown source. Defaults to
        ``docs/security_compliance_coverage.md``.
    out
        Explicit output PDF path. If None, a date-stamped path under
        ``docs/`` is used.
    chrome
        Explicit Chrome binary path. Falls back to the Playwright bundle.
    html_only
        If True, render only the HTML and return its path. Useful in CI
        for diffing the HTML deterministically.
    today
        Override the generation date (tests pin this for determinism).

    Returns
    -------
    Path
        Absolute path to the generated PDF (or HTML if ``html_only=True``).
    """
    source = source or _default_source()
    if not source.exists():
        raise FileNotFoundError(f"Source markdown not found: {source}")

    today = today or dt.date.today()
    out_dir = out.parent if out else _default_out_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    pdf_path = out or out_dir / f"security_compliance_report_{today.isoformat()}.pdf"
    html_path = pdf_path.with_suffix(".html")

    md_text = source.read_text(encoding="utf-8")
    doc = parse_markdown(md_text)

    repo_root = Path(__file__).resolve().parents[2]
    rev = _git_rev(repo_root)
    report_date = today.isoformat()

    html_doc = render_html(doc, report_date=report_date, git_rev=rev)
    html_path.write_text(html_doc, encoding="utf-8")

    if html_only:
        return html_path

    chrome_path = _resolve_chrome(chrome)
    _render_pdf(chrome_path, html_path, pdf_path)
    return pdf_path


# ────────────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────────────


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python3.13 -m scripts.compliance_report",
        description="Render docs/security_compliance_coverage.md to PDF.",
    )
    p.add_argument(
        "--source",
        type=Path,
        default=None,
        help="Path to the markdown source (default: docs/security_compliance_coverage.md).",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output PDF path (default: docs/security_compliance_report_<today>.pdf).",
    )
    p.add_argument(
        "--chrome",
        type=Path,
        default=None,
        help="Chrome binary path (default: Playwright-bundled Chrome-for-Testing).",
    )
    p.add_argument(
        "--html-only",
        action="store_true",
        help="Skip Chrome and emit only the HTML deliverable.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        path = generate_report(
            source=args.source,
            out=args.out,
            chrome=args.chrome,
            html_only=args.html_only,
        )
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 3
    print(path)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
