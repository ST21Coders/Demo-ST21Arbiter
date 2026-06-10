"""tests/test_compliance_report.py — pin behavior of the compliance PDF
generator at `scripts/compliance_report.py`.

We test four things:
  * Markdown parser correctness on a synthetic minimal sample.
  * HTML renderer emits the expected severity pills and section headers.
  * Chrome subprocess command-line is correct (no real Chrome invocation —
    the test mocks subprocess.run via monkeypatch).
  * Determinism: rendering the same parsed doc twice produces byte-
    identical HTML.

The minimal synthetic markdown sample is intentionally short — it has 2
categories, one with mixed statuses, one with a single covered item. That
exercises every classifier branch (covered / partial / missing / oos) and
the summary-totals row parsing path without dragging in the real 549-line
source.
"""

from __future__ import annotations

import datetime as dt
import subprocess
from pathlib import Path

import pytest

from scripts import compliance_report as cr

# ────────────────────────────────────────────────────────────────────────────
# Synthetic markdown sample
# ────────────────────────────────────────────────────────────────────────────

SAMPLE_MD = """# ARBITER Adversarial Harness — Coverage Matrix

**Date:** 2026-06-10
**Reference standard:** internal compliance checklist

---

## 1. Injection

| # | Item | Status | Where / Gap | Effort to close |
|---|---|---|---|---|
| 1 | SQL injection | ✅ Covered | fuzz/corpus/sqli.json (9 payloads) | – |
| 2 | NoSQL injection | 🟡 Partial | partial corpus | small |
| 3 | LDAP injection | ❌ Missing | no probe today | small |
| 4 | MFA bypass | ⚪ Out of scope | covered by Cognito hosted UI | – |

**Section totals:** 1 covered · 1 partial · 1 missing · 1 out-of-scope

---

## 2. Auth

| # | Item | Status | Where / Gap | Effort to close |
|---|---|---|---|---|
| 5 | JWT validation | ✅ Covered | auth/test_chat_no_signature.py | – |

**Section totals:** 1 covered

---

# Summary

| Category | Covered | Partial | Missing | Out of scope | Total |
|---|---:|---:|---:|---:|---:|
| 1. Injection | 1 | 1 | 1 | 1 | 4 |
| 2. Auth | 1 | 0 | 0 | 0 | 1 |
| **Totals** | **2** | **1** | **1** | **1** | **5** |
"""


# ────────────────────────────────────────────────────────────────────────────
# Parser
# ────────────────────────────────────────────────────────────────────────────


class TestParseMarkdown:
    def test_extracts_two_categories(self):
        doc = cr.parse_markdown(SAMPLE_MD)
        assert len(doc.categories) == 2
        assert doc.categories[0].number == 1
        assert doc.categories[0].name == "Injection"
        assert doc.categories[1].number == 2
        assert doc.categories[1].name == "Auth"

    def test_first_category_has_four_items_one_per_status(self):
        doc = cr.parse_markdown(SAMPLE_MD)
        items = doc.categories[0].items
        assert len(items) == 4
        statuses = [it.status for it in items]
        assert statuses.count("covered") == 1
        assert statuses.count("partial") == 1
        assert statuses.count("missing") == 1
        assert statuses.count("oos") == 1

    def test_second_category_has_single_covered_item(self):
        doc = cr.parse_markdown(SAMPLE_MD)
        items = doc.categories[1].items
        assert len(items) == 1
        assert items[0].status == "covered"
        assert items[0].number == 5
        assert "JWT validation" in items[0].item

    def test_item_fields_preserved_verbatim(self):
        doc = cr.parse_markdown(SAMPLE_MD)
        sqli = doc.categories[0].items[0]
        assert sqli.item == "SQL injection"
        assert "fuzz/corpus/sqli.json" in sqli.where

    def test_summary_totals_parsed(self):
        doc = cr.parse_markdown(SAMPLE_MD)
        assert doc.totals == {"covered": 2, "partial": 1, "missing": 1, "oos": 1}
        assert doc.total_items == 5

    def test_category_counts_helper(self):
        doc = cr.parse_markdown(SAMPLE_MD)
        c1 = doc.categories[0].counts()
        assert c1 == {"covered": 1, "partial": 1, "missing": 1, "oos": 1}
        c2 = doc.categories[1].counts()
        assert c2 == {"covered": 1, "partial": 0, "missing": 0, "oos": 0}

    def test_classify_status_branches(self):
        assert cr._classify_status("✅ Covered") == "covered"
        assert cr._classify_status("🟡 Partial") == "partial"
        assert cr._classify_status("❌ Missing") == "missing"
        assert cr._classify_status("⚪ Out of scope") == "oos"
        assert cr._classify_status("???") is None

    def test_divider_rows_are_skipped(self):
        # The synthetic sample's `|---|---|...` row should NOT become an
        # item. If the parser misclassified dividers, the first category
        # would have an extra phantom row.
        doc = cr.parse_markdown(SAMPLE_MD)
        for cat in doc.categories:
            for it in cat.items:
                assert it.number > 0  # real rows always carry a number

    def test_real_source_doc_parses(self):
        # Smoke check against the live source. The real doc has 12
        # categories and 79 items per its trailing summary table.
        source = (
            Path(__file__).resolve().parents[2]
            / "docs"
            / "security_compliance_coverage.md"
        )
        if not source.exists():
            pytest.skip("Real source markdown not present in this checkout")
        doc = cr.parse_markdown(source.read_text(encoding="utf-8"))
        assert len(doc.categories) == 12
        assert doc.total_items == 79
        assert doc.totals["covered"] == 56
        assert doc.totals["partial"] == 3
        assert doc.totals["missing"] == 3
        assert doc.totals["oos"] == 17

    def test_totals_synthesized_when_summary_missing(self):
        # Strip the `# Summary` block and verify the parser falls back to
        # per-category counts.
        truncated = SAMPLE_MD.split("# Summary")[0]
        doc = cr.parse_markdown(truncated)
        assert doc.total_items == 5
        assert doc.totals["covered"] == 2


# ────────────────────────────────────────────────────────────────────────────
# HTML renderer
# ────────────────────────────────────────────────────────────────────────────


class TestRenderHtml:
    def _render(self) -> str:
        doc = cr.parse_markdown(SAMPLE_MD)
        return cr.render_html(doc, report_date="2026-06-10", git_rev="deadbee")

    def test_emits_all_four_pill_classes(self):
        html = self._render()
        assert "pill-covered" in html
        assert "pill-partial" in html
        assert "pill-missing" in html
        assert "pill-oos" in html

    def test_emits_section_headers(self):
        html = self._render()
        assert "Executive summary" in html
        assert "Detailed coverage matrix" in html
        assert "Out-of-scope appendix" in html
        assert "Methodology" in html
        assert "Security Compliance Coverage Report" in html

    def test_cover_page_has_coverage_percent(self):
        html = self._render()
        # 2/5 covered = 40%
        assert "40%" in html

    def test_categories_rendered_in_order(self):
        html = self._render()
        i_inj = html.index("Injection")
        i_auth = html.index("Auth")
        assert i_inj < i_auth

    def test_oos_appendix_only_contains_oos_items(self):
        html = self._render()
        # The OOS section heading marks the start of the appendix.
        start = html.index("Out-of-scope appendix")
        end = html.index("Methodology")
        appendix = html[start:end]
        # The synthetic sample's only OOS item is #4 "MFA bypass".
        assert "MFA bypass" in appendix
        # `SQL injection` (covered) must NOT appear in the appendix table.
        assert "SQL injection" not in appendix

    def test_git_rev_in_footer(self):
        html = self._render()
        assert "deadbee" in html

    def test_report_date_in_cover(self):
        html = self._render()
        assert "2026-06-10" in html

    def test_html_well_formed_envelope(self):
        html = self._render()
        assert html.startswith("<!DOCTYPE html>")
        assert html.rstrip().endswith("</html>")

    def test_summary_table_includes_doc_totals_row(self):
        html = self._render()
        # The `Totals` row in the exec-summary by-category table.
        assert "totals-row" in html

    def test_html_escapes_special_chars_in_item_text(self):
        # Construct a sample with `<script>` in an item description and
        # verify the rendered HTML escapes it rather than letting Chrome
        # parse it as a real tag.
        md = (
            "## 1. XSS\n\n"
            "| # | Item | Status | Where / Gap | Effort |\n"
            "|---|---|---|---|---|\n"
            "| 1 | XSS via <script> tag | ✅ Covered | fuzz/corpus/xss.json | – |\n\n"
            "**Section totals:** 1 covered\n"
        )
        doc = cr.parse_markdown(md)
        html = cr.render_html(doc, report_date="2026-06-10", git_rev="r")
        # The raw `<script>` substring must not appear unescaped in the
        # rendered HTML body — `html.escape` turns `<` into `&lt;`.
        assert "<script>" not in html
        assert "&lt;script&gt;" in html


# ────────────────────────────────────────────────────────────────────────────
# Determinism
# ────────────────────────────────────────────────────────────────────────────


class TestDeterminism:
    def test_two_renders_produce_identical_html(self):
        doc1 = cr.parse_markdown(SAMPLE_MD)
        doc2 = cr.parse_markdown(SAMPLE_MD)
        h1 = cr.render_html(doc1, report_date="2026-06-10", git_rev="abcdef0")
        h2 = cr.render_html(doc2, report_date="2026-06-10", git_rev="abcdef0")
        assert h1 == h2


# ────────────────────────────────────────────────────────────────────────────
# Chrome command-line
# ────────────────────────────────────────────────────────────────────────────


class TestChromeCommand:
    def test_command_line_shape(self, tmp_path: Path):
        chrome = tmp_path / "fake-chrome"
        chrome.write_text("#!/bin/sh\nexit 0\n")
        chrome.chmod(0o755)
        html = tmp_path / "in.html"
        html.write_text("<html></html>")
        pdf = tmp_path / "out.pdf"

        cmd = cr._chrome_command(chrome, html, pdf)
        assert cmd[0] == str(chrome)
        assert "--headless=new" in cmd
        assert "--print-to-pdf-no-header" in cmd
        # The print-to-pdf flag must carry an ABSOLUTE path so Chrome
        # writes to the expected location regardless of its cwd.
        assert any(arg == f"--print-to-pdf={pdf.resolve()}" for arg in cmd)
        # The last arg should be a `file://` URL to the input.
        assert cmd[-1].startswith("file://")
        assert cmd[-1].endswith(str(html.resolve()))

    def test_render_pdf_invokes_chrome_with_expected_args(
        self, tmp_path: Path, monkeypatch
    ):
        chrome = tmp_path / "fake-chrome"
        chrome.write_text("#!/bin/sh\nexit 0\n")
        chrome.chmod(0o755)
        html = tmp_path / "in.html"
        html.write_text("<html></html>")
        pdf = tmp_path / "out.pdf"

        captured: dict = {}

        def fake_run(cmd, capture_output, text, timeout):
            captured["cmd"] = cmd
            # Simulate Chrome writing a real-looking PDF (must be > 1 KB
            # per the size sanity check in `_render_pdf`).
            pdf.write_bytes(b"%PDF-1.4\n" + b"x" * 2048 + b"\n%%EOF\n")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(cr.subprocess, "run", fake_run)

        cr._render_pdf(chrome, html, pdf)

        assert captured["cmd"][0] == str(chrome)
        assert "--headless=new" in captured["cmd"]
        assert any("--print-to-pdf=" in arg for arg in captured["cmd"])
        assert pdf.exists()

    def test_render_pdf_raises_on_chrome_nonzero(self, tmp_path: Path, monkeypatch):
        chrome = tmp_path / "fake-chrome"
        chrome.write_text("#!/bin/sh\nexit 1\n")
        chrome.chmod(0o755)
        html = tmp_path / "in.html"
        html.write_text("<html></html>")
        pdf = tmp_path / "out.pdf"

        def fake_run(cmd, capture_output, text, timeout):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

        monkeypatch.setattr(cr.subprocess, "run", fake_run)

        with pytest.raises(RuntimeError, match="Chrome exited with code 1"):
            cr._render_pdf(chrome, html, pdf)

    def test_render_pdf_raises_on_tiny_pdf(self, tmp_path: Path, monkeypatch):
        chrome = tmp_path / "fake-chrome"
        chrome.write_text("#!/bin/sh\nexit 0\n")
        chrome.chmod(0o755)
        html = tmp_path / "in.html"
        html.write_text("<html></html>")
        pdf = tmp_path / "out.pdf"

        def fake_run(cmd, capture_output, text, timeout):
            # Tiny output — < 1 KB sanity check fires.
            pdf.write_bytes(b"%PDF-1.4\n")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(cr.subprocess, "run", fake_run)

        with pytest.raises(RuntimeError, match="suspiciously small"):
            cr._render_pdf(chrome, html, pdf)


# ────────────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────────────


class TestCli:
    def test_html_only_mode_skips_chrome(self, tmp_path: Path):
        # End-to-end html-only path: parses the synthetic sample,
        # renders, writes the HTML file, and returns its path. No
        # subprocess.run is called because `html_only=True` short-
        # circuits before `_resolve_chrome`.
        source = tmp_path / "src.md"
        source.write_text(SAMPLE_MD)
        out_pdf = tmp_path / "report.pdf"

        path = cr.generate_report(
            source=source,
            out=out_pdf,
            html_only=True,
            today=dt.date(2026, 6, 10),
        )
        assert path.suffix == ".html"
        assert path.exists()
        assert path.read_text(encoding="utf-8").startswith("<!DOCTYPE html>")

    def test_generate_report_missing_source_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            cr.generate_report(source=tmp_path / "nope.md", html_only=True)


# ────────────────────────────────────────────────────────────────────────────
# Wiring: package.json
# ────────────────────────────────────────────────────────────────────────────


def test_package_json_has_compliance_report_script():
    """The npm script is the documented entry point; if a future refactor
    removes it, the docs and the spec drift apart."""
    import json

    pkg = Path(__file__).resolve().parents[1] / "package.json"
    data = json.loads(pkg.read_text(encoding="utf-8"))
    assert "compliance-report" in data["scripts"]
    assert "scripts.compliance_report" in data["scripts"]["compliance-report"]
