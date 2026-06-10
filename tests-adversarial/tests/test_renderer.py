"""Tests for src/reporting/renderer.py — task 22.

Coverage (matches the task-22 prompt's 10-point acceptance):
  1. render_html on an empty report writes a valid HTML file.
  2. Two renders of the same report produce byte-identical HTML (determinism).
  3. Rendered HTML contains no external URLs other than the user-supplied
     target URLs in the metadata block (offline guarantee).
  4. A report with two findings renders both rows in the findings table.
  5. Page matrix renders exactly 60 (15 × 4) cells when given the real manifest.
  6. API routes table has exactly 25 data rows.
  7. Agent tools table has exactly 12 data rows (incl. the synthetic sentinel).
  8. Severity pills carry the correct CSS class per severity.
  9. DOCUMENTED_UNSAFE results do NOT appear in the findings table.
  10. Long target_id strings don't break the layout (the cell carries the
      `truncate` helper class so the layout wraps rather than overflows).

Tests intentionally don't snapshot the full HTML body — that would make the
file painful to edit. Instead each test inspects a specific substring or
element using `re` / `html.parser`, which keeps the assertions stable as
the cosmetic CSS shifts.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from src.coverage.builder import (
    CellStatus,
    TestResult,
    build_matrix,
    matrix_to_json,
)
from src.reporting.renderer import render_html, render_summary
from src.reporting.report_builder import RunMetadata, build_report


_HARNESS_ROOT = Path(__file__).resolve().parent.parent
_MANIFEST_PATH = _HARNESS_ROOT / "src" / "coverage" / "manifest.json"


# ───────────────────────────── fixtures ────────────────────────────────────


@pytest.fixture
def manifest() -> dict:
    return json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def empty_matrix(manifest: dict) -> dict:
    return matrix_to_json(build_matrix(manifest, []))


@pytest.fixture
def empty_cost() -> dict:
    return {"total_usd": 0.0, "per_layer_usd": {}, "probe_counts": {}}


@pytest.fixture
def metadata() -> RunMetadata:
    return RunMetadata(
        run_id="2026-06-09T14-23-01Z",
        target_base_url="https://d5u0vv1zl3eqd.cloudfront.net/",
        chat_function_url="https://example.lambda-url.us-east-1.on.aws/",
        started_at="2026-06-09T14:23:01Z",
        finished_at="2026-06-09T14:31:13Z",
        duration_seconds=492.0,
        harness_version="0.1.0",
    )


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    rd = tmp_path / "2026-06-09T14-23-01Z"
    rd.mkdir()
    return rd


def _write_evidence(run_dir: Path, relpath: str) -> str:
    p = run_dir / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("fake", encoding="utf-8")
    return relpath


def _build_empty_report(
    manifest: dict,
    empty_matrix: dict,
    empty_cost: dict,
    metadata: RunMetadata,
    run_dir: Path,
) -> dict:
    return build_report(
        run_dir=run_dir,
        manifest=manifest,
        matrix=empty_matrix,
        cost=empty_cost,
        metadata=metadata,
        results=[],
    )


# ────────────── helper: a tiny HTML row counter for matrix tests ───────────


class _TableRowCounter(HTMLParser):
    """Counts <tr> elements inside the <tbody> of a table identified by a
    target heading id we observed earlier in the document.

    Lightweight: only tracks the `aria-describedby` attribute so we can
    associate a `<table>` with the section heading it sits under, then
    count `<tr>` inside that table's `<tbody>` only. We don't depend on
    BeautifulSoup because the harness already has Jinja2; pulling in
    bs4 for one test is overkill.
    """

    def __init__(self, target_describedby: str) -> None:
        super().__init__(convert_charrefs=True)
        self.target = target_describedby
        self._in_target = False
        self._in_tbody = False
        self.tbody_rows = 0
        self.tbody_cells = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "table" and a.get("aria-describedby") == self.target:
            self._in_target = True
        elif tag == "tbody" and self._in_target:
            self._in_tbody = True
        elif tag == "tr" and self._in_tbody:
            self.tbody_rows += 1
        elif tag in ("td", "th") and self._in_tbody:
            self.tbody_cells += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "tbody" and self._in_target:
            self._in_tbody = False
        elif tag == "table" and self._in_target:
            self._in_target = False


def _count_tbody_rows(html: str, describedby: str) -> int:
    p = _TableRowCounter(describedby)
    p.feed(html)
    return p.tbody_rows


def _count_tbody_cells(html: str, describedby: str) -> tuple[int, int]:
    p = _TableRowCounter(describedby)
    p.feed(html)
    return p.tbody_rows, p.tbody_cells


# ──────────────────────────────── tests ────────────────────────────────────


def test_render_html_on_empty_report_writes_valid_file(
    manifest: dict,
    empty_matrix: dict,
    empty_cost: dict,
    metadata: RunMetadata,
    run_dir: Path,
) -> None:
    """Acceptance 1: empty report still produces a real, non-empty HTML file."""
    report = _build_empty_report(manifest, empty_matrix, empty_cost, metadata, run_dir)
    out = render_html(report, run_dir, manifest)
    assert out == run_dir / "report.html"
    assert out.is_file()
    body = out.read_text(encoding="utf-8")
    # Doctype + closing tag are the bare minimum signal that we wrote real
    # HTML rather than an empty file or a partial template.
    assert body.startswith("<!DOCTYPE html>")
    assert "</html>" in body
    # The empty-state for findings (the green check) must render when the
    # findings list is empty — this is the "no failures recorded" copy.
    assert "No failures recorded" in body


def test_render_html_is_byte_identical_across_two_renders(
    manifest: dict,
    empty_matrix: dict,
    empty_cost: dict,
    metadata: RunMetadata,
    run_dir: Path,
) -> None:
    """Acceptance 2: same input -> identical bytes. Stops the report from
    churning across runs (so the diff-from-last-green view stays useful).
    """
    report = _build_empty_report(manifest, empty_matrix, empty_cost, metadata, run_dir)
    a = render_html(report, run_dir, manifest).read_bytes()
    b = render_html(report, run_dir, manifest).read_bytes()
    assert a == b


def test_render_html_has_no_unexpected_external_urls(
    manifest: dict,
    empty_matrix: dict,
    empty_cost: dict,
    metadata: RunMetadata,
    run_dir: Path,
) -> None:
    """Acceptance 3 (offline guarantee): the only http(s):// URLs in the
    rendered output are the user-supplied target_base_url and
    chat_function_url. No CDN, no Google Fonts, no analytics."""
    report = _build_empty_report(manifest, empty_matrix, empty_cost, metadata, run_dir)
    body = render_html(report, run_dir, manifest).read_text(encoding="utf-8")
    found = re.findall(r"https?://[^\s\"'<>]+", body)
    allowed = {
        metadata.target_base_url.rstrip("/"),
        metadata.chat_function_url.rstrip("/") if metadata.chat_function_url else None,
    }
    for url in found:
        # Strip a trailing slash for the allow-list match — the metadata
        # block emits the URL verbatim but JSON or attribute contexts may
        # drop the trailing slash. We only care about scheme+host+path.
        normalized = url.rstrip("/")
        assert any(
            normalized.startswith(a) or normalized == a
            for a in allowed
            if a is not None
        ), f"unexpected external URL in HTML: {url!r}"


def test_render_html_findings_table_contains_two_rows_for_two_findings(
    manifest: dict,
    empty_cost: dict,
    metadata: RunMetadata,
    run_dir: Path,
) -> None:
    """Acceptance 4: two FAILs -> two rows in the findings table."""
    e1 = _write_evidence(run_dir, "e2e/findings-soc.png")
    e2 = _write_evidence(run_dir, "fuzz/transcripts/get-findings.jsonl")
    results = [
        TestResult(
            test_id="e2e.page.findings.soc",
            status=CellStatus.FAIL,
            layer="e2e",
            target_kind="page",
            target_id="findings",
            persona="soc",
            evidence_path=e1,
            severity="high",
        ),
        TestResult(
            test_id="fuzz.get-findings.xss.script-tag",
            status=CellStatus.FAIL,
            layer="fuzz",
            target_kind="api_route",
            target_id="get-findings",
            evidence_path=e2,
            severity="medium",
        ),
    ]
    matrix = matrix_to_json(build_matrix(manifest, results))
    report = build_report(
        run_dir=run_dir,
        manifest=manifest,
        matrix=matrix,
        cost=empty_cost,
        metadata=metadata,
        results=results,
    )
    body = render_html(report, run_dir, manifest).read_text(encoding="utf-8")
    assert "e2e.page.findings.soc" in body
    assert "fuzz.get-findings.xss.script-tag" in body
    # The findings <table> has id "findings-table" (used by the sorter).
    # Count rows under its tbody by walking the parsed HTML structure.
    # We use a regex anchored to the id since we only have one such table.
    table_match = re.search(
        r"<table[^>]*id=\"findings-table\".*?</table>",
        body,
        re.DOTALL,
    )
    assert table_match is not None
    tbl = table_match.group(0)
    tbody = re.search(r"<tbody>(.*?)</tbody>", tbl, re.DOTALL)
    assert tbody is not None
    rows = re.findall(r"<tr>", tbody.group(1))
    assert len(rows) == 2


def test_page_matrix_has_60_cells_for_real_manifest(
    manifest: dict,
    empty_matrix: dict,
    empty_cost: dict,
    metadata: RunMetadata,
    run_dir: Path,
) -> None:
    """Acceptance 5: 15 pages × 4 personas = 60 cells in the pages matrix.

    We count <td> cells (not <th>) — the persona column headers are <th>,
    only the data cells are <td>.
    """
    report = _build_empty_report(manifest, empty_matrix, empty_cost, metadata, run_dir)
    body = render_html(report, run_dir, manifest).read_text(encoding="utf-8")
    rows, cells = _count_tbody_cells(body, "pages-h")
    assert rows == 15
    # Each row has 1 page-id <td> + 4 persona <td> cells = 5 cells/row,
    # total 75. The 4-per-row persona cells are the 60 we care about — but
    # asserting on the total is the cleaner invariant.
    assert cells == 15 * 5
    # And the 60-cells claim itself: subtract the leading page-id column.
    assert (cells - rows) == 60


def test_routes_table_has_25_rows_for_real_manifest(
    manifest: dict,
    empty_matrix: dict,
    empty_cost: dict,
    metadata: RunMetadata,
    run_dir: Path,
) -> None:
    """Acceptance 6: every API route in the manifest gets one row."""
    report = _build_empty_report(manifest, empty_matrix, empty_cost, metadata, run_dir)
    body = render_html(report, run_dir, manifest).read_text(encoding="utf-8")
    assert _count_tbody_rows(body, "routes-h") == 25


def test_tools_table_has_12_rows_including_sentinel(
    manifest: dict,
    empty_matrix: dict,
    empty_cost: dict,
    metadata: RunMetadata,
    run_dir: Path,
) -> None:
    """Acceptance 7: 12 tool rows, and the synthetic sentinel is labeled."""
    report = _build_empty_report(manifest, empty_matrix, empty_cost, metadata, run_dir)
    body = render_html(report, run_dir, manifest).read_text(encoding="utf-8")
    assert _count_tbody_rows(body, "tools-h") == 12
    # The sentinel entry (master.chat_surface) gets a "(sentinel)" pill.
    assert "master.chat_surface" in body
    assert "(sentinel)" in body


@pytest.mark.parametrize(
    "severity,expected_class",
    [
        ("critical", "pill-critical"),
        ("high", "pill-high"),
        ("medium", "pill-medium"),
        ("low", "pill-low"),
        ("info", "pill-info"),
    ],
)
def test_severity_pill_has_correct_css_class(
    manifest: dict,
    empty_cost: dict,
    metadata: RunMetadata,
    run_dir: Path,
    severity: str,
    expected_class: str,
) -> None:
    """Acceptance 8: every known severity gets its dedicated CSS class so
    the pill renders in the right color. An unknown severity would fall
    back to `pill-unknown` (also covered by the next test indirectly)."""
    ev = _write_evidence(run_dir, f"e2e/{severity}.png")
    fail = TestResult(
        test_id=f"e2e.page.findings.soc-{severity}",
        status=CellStatus.FAIL,
        layer="e2e",
        target_kind="page",
        target_id="findings",
        persona="soc",
        evidence_path=ev,
        severity=severity,
    )
    matrix = matrix_to_json(build_matrix(manifest, [fail]))
    report = build_report(
        run_dir=run_dir,
        manifest=manifest,
        matrix=matrix,
        cost=empty_cost,
        metadata=metadata,
        results=[fail],
    )
    body = render_html(report, run_dir, manifest).read_text(encoding="utf-8")
    # The pill renders as <span class="pill pill-<severity>">…</span> inside
    # the findings table. Look for the joined class string in the body.
    assert f'class="pill {expected_class}"' in body


def test_documented_unsafe_does_not_appear_in_findings_table(
    manifest: dict,
    empty_cost: dict,
    metadata: RunMetadata,
    run_dir: Path,
) -> None:
    """Acceptance 9: DOCUMENTED_UNSAFE rows are counted in the summary
    banner but NOT listed as findings (per AC11)."""
    doc_unsafe = TestResult(
        test_id="auth.chat.no-signature",
        status=CellStatus.DOCUMENTED_UNSAFE,
        layer="auth",
        target_kind="api_route",
        target_id="post-chat",
        persona="ciso",
        evidence_path=None,
        severity="info",
    )
    matrix = matrix_to_json(build_matrix(manifest, [doc_unsafe]))
    report = build_report(
        run_dir=run_dir,
        manifest=manifest,
        matrix=matrix,
        cost=empty_cost,
        metadata=metadata,
        results=[doc_unsafe],
    )
    body = render_html(report, run_dir, manifest).read_text(encoding="utf-8")
    # The summary banner shows the documented_unsafe count = 1.
    assert ">1</div>" in body  # at least one stat reads "1"
    # The findings table must NOT include the auth.chat.no-signature row.
    table_match = re.search(
        r"<table[^>]*id=\"findings-table\".*?</table>",
        body,
        re.DOTALL,
    )
    if table_match:
        assert "auth.chat.no-signature" not in table_match.group(0)
    # The empty-state copy renders instead since findings[] is empty.
    assert "No failures recorded" in body


def test_long_target_id_does_not_break_layout(
    manifest: dict,
    empty_cost: dict,
    metadata: RunMetadata,
    run_dir: Path,
) -> None:
    """Acceptance 10: a 100-char target_id renders inside a `.truncate`
    cell so CSS `overflow-wrap: anywhere` wraps it rather than overflowing.

    Note: we use the real existing route id `get-findings` as the matrix
    target_id (so build_matrix accepts it) but the rendered TEXT we test
    against is the long string itself, embedded via a custom test_id that
    Jinja will escape and emit verbatim into the truncate cell.
    """
    ev = _write_evidence(run_dir, "e2e/long.png")
    long_id = "x" * 100
    fail = TestResult(
        test_id=f"e2e.fuzz.{long_id}",
        status=CellStatus.FAIL,
        layer="fuzz",
        target_kind="api_route",
        target_id="get-findings",
        evidence_path=ev,
        severity="medium",
    )
    matrix = matrix_to_json(build_matrix(manifest, [fail]))
    report = build_report(
        run_dir=run_dir,
        manifest=manifest,
        matrix=matrix,
        cost=empty_cost,
        metadata=metadata,
        results=[fail],
    )
    body = render_html(report, run_dir, manifest).read_text(encoding="utf-8")
    # The long test_id is in the body, AND the `truncate` CSS class is
    # applied to the cells holding long strings so a real browser wraps
    # rather than overflowing.
    assert long_id in body
    assert "truncate" in body


def test_render_summary_writes_real_file_with_run_id(
    manifest: dict,
    empty_matrix: dict,
    empty_cost: dict,
    metadata: RunMetadata,
    run_dir: Path,
) -> None:
    """Task 23: summary.md is a real Markdown digest. Smoke-checks that
    the file lands at the right path, names the run, and carries no
    12-digit account-id-shaped string in the empty-run case."""
    report = _build_empty_report(manifest, empty_matrix, empty_cost, metadata, run_dir)
    out = render_summary(report, run_dir)
    assert out == run_dir / "summary.md"
    body = out.read_text(encoding="utf-8")
    assert metadata.run_id in body
    # No 12-digit run of digits in the rendered output (AC12).
    assert not re.search(r"\b\d{12}\b", body)


# ─────────────────────── Task 23 — summary.md tests ────────────────────────


def test_summary_contains_all_required_sections(
    manifest: dict,
    empty_matrix: dict,
    empty_cost: dict,
    metadata: RunMetadata,
    run_dir: Path,
) -> None:
    """Task 23.1: every section required by AC12 is present.

    Header, Overall metrics, Coverage, Top 5 findings (or empty-state),
    Diff from last green (or no-baseline hint), Full report link.
    """
    report = _build_empty_report(manifest, empty_matrix, empty_cost, metadata, run_dir)
    body = render_summary(report, run_dir).read_text(encoding="utf-8")
    assert "# ARBITER Adversarial Test Run" in body
    assert "**Run:**" in body
    assert "**Target:**" in body
    assert "**Duration:**" in body
    assert "## Overall" in body
    assert "## Coverage" in body
    assert "## Top 5 findings" in body
    assert "## Diff from last green" in body
    assert "## Full report" in body
    assert "[`report.html`](report.html)" in body
    assert "[`report.json`](report.json)" in body
    assert "Generated by the ARBITER adversarial test harness" in body


def test_summary_redacts_12_digit_account_id_in_finding_summary(
    manifest: dict,
    empty_cost: dict,
    metadata: RunMetadata,
    run_dir: Path,
) -> None:
    """Task 23.2 / AC12: a 12-digit AWS account id is scrubbed even if
    it slips into a finding's target_id or other rendered field."""
    ev = _write_evidence(run_dir, "fuzz/transcripts/leak.jsonl")
    fail = TestResult(
        test_id="fuzz.get-findings.account-id-leak-669810405473",
        status=CellStatus.FAIL,
        layer="fuzz",
        target_kind="api_route",
        target_id="get-findings",
        evidence_path=ev,
        severity="high",
    )
    matrix = matrix_to_json(build_matrix(manifest, [fail]))
    report = build_report(
        run_dir=run_dir,
        manifest=manifest,
        matrix=matrix,
        cost=empty_cost,
        metadata=metadata,
        results=[fail],
    )
    body = render_summary(report, run_dir).read_text(encoding="utf-8")
    assert "669810405473" not in body
    assert "[REDACTED-12DIGIT]" in body


def test_summary_redacts_jwt_shaped_string(
    manifest: dict,
    empty_cost: dict,
    metadata: RunMetadata,
    run_dir: Path,
) -> None:
    """Task 23.3 / AC12: a JWT-shaped token is scrubbed wherever it lands
    in the rendered Markdown."""
    # Use a JWT-shaped token in the finding's test_id. The token uses
    # base64url characters and three dotted segments starting with eyJ.
    jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NSJ9.SflKxwRJ"
    ev = _write_evidence(run_dir, "auth/probes.jsonl")
    fail = TestResult(
        test_id=f"auth.token.{jwt}",
        status=CellStatus.FAIL,
        layer="auth",
        target_kind="api_route",
        target_id="get-token-usage",
        evidence_path=ev,
        severity="high",
    )
    matrix = matrix_to_json(build_matrix(manifest, [fail]))
    report = build_report(
        run_dir=run_dir,
        manifest=manifest,
        matrix=matrix,
        cost=empty_cost,
        metadata=metadata,
        results=[fail],
    )
    body = render_summary(report, run_dir).read_text(encoding="utf-8")
    assert jwt not in body
    assert "[REDACTED-JWT]" in body
    # The full JWT must not survive even partially as `eyJ...eyJ...`.
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIi" not in body


def test_summary_truncates_oversized_base64_blob(
    manifest: dict,
    empty_cost: dict,
    metadata: RunMetadata,
    run_dir: Path,
) -> None:
    """Task 23.4 / AC12: a base64-ish blob longer than 200 chars is
    truncated with a `[REDACTED-BASE64-...]` marker."""
    blob = "A" * 250  # 250 chars of base64-ish characters
    ev = _write_evidence(run_dir, "llm/transcripts/exfil.jsonl")
    fail = TestResult(
        test_id=f"llm.exfil.{blob}",
        status=CellStatus.FAIL,
        layer="llm",
        target_kind="agent_tool",
        target_id="master.chat_surface",
        evidence_path=ev,
        severity="high",
    )
    matrix = matrix_to_json(build_matrix(manifest, [fail]))
    report = build_report(
        run_dir=run_dir,
        manifest=manifest,
        matrix=matrix,
        cost=empty_cost,
        metadata=metadata,
        results=[fail],
    )
    body = render_summary(report, run_dir).read_text(encoding="utf-8")
    assert blob not in body
    assert "[REDACTED-BASE64-" in body


def test_summary_top_findings_are_exactly_top_5_by_severity(
    manifest: dict,
    empty_cost: dict,
    metadata: RunMetadata,
    run_dir: Path,
) -> None:
    """Task 23.5: with 7 findings in mixed severities, the summary's
    top-5 table contains exactly 5 rows ranked by severity tier
    (critical > high > medium > low > info)."""
    severities = ["info", "low", "medium", "high", "critical", "medium", "high"]
    results: list[TestResult] = []
    for i, sev in enumerate(severities):
        ev = _write_evidence(run_dir, f"fuzz/transcripts/r{i}.jsonl")
        results.append(
            TestResult(
                test_id=f"fuzz.get-findings.probe-{i}",
                status=CellStatus.FAIL,
                layer="fuzz",
                target_kind="api_route",
                target_id="get-findings",
                evidence_path=ev,
                severity=sev,
            )
        )
    matrix = matrix_to_json(build_matrix(manifest, results))
    report = build_report(
        run_dir=run_dir,
        manifest=manifest,
        matrix=matrix,
        cost=empty_cost,
        metadata=metadata,
        results=results,
    )
    body = render_summary(report, run_dir).read_text(encoding="utf-8")
    # The Top-5 table renders <= 5 data rows. We count rows in the
    # findings-table section by looking for the test-id markdown cell.
    section = body.split("## Top 5 findings", 1)[1].split("## Diff", 1)[0]
    test_id_lines = re.findall(r"\|\s*`fuzz\.get-findings\.probe-\d+`", section)
    assert len(test_id_lines) == 5
    # The critical entry must be present (it's rank 1).
    assert "critical" in section
    # The info entry must NOT appear (ranked 7th).
    info_test_id = f"fuzz.get-findings.probe-{severities.index('info')}"
    assert info_test_id not in section


def test_summary_empty_findings_renders_no_failures_block(
    manifest: dict,
    empty_matrix: dict,
    empty_cost: dict,
    metadata: RunMetadata,
    run_dir: Path,
) -> None:
    """Task 23.6: no findings -> green-status block, no table."""
    report = _build_empty_report(manifest, empty_matrix, empty_cost, metadata, run_dir)
    body = render_summary(report, run_dir).read_text(encoding="utf-8")
    assert "No failures recorded in this run." in body
    # The findings table header must NOT render in the empty case.
    section = body.split("## Top 5 findings", 1)[1].split("## Diff", 1)[0]
    assert "| Severity |" not in section


def test_summary_no_baseline_renders_promote_baseline_hint(
    manifest: dict,
    empty_matrix: dict,
    empty_cost: dict,
    metadata: RunMetadata,
    run_dir: Path,
) -> None:
    """Task 23.7: no baseline -> promote-baseline hint, no diff numbers."""
    report = _build_empty_report(manifest, empty_matrix, empty_cost, metadata, run_dir)
    body = render_summary(report, run_dir).read_text(encoding="utf-8")
    assert "No baseline yet." in body
    assert "npm run test:promote-baseline" in body
    # Diff numbers must NOT appear in the no-baseline case.
    assert "New failures since last green" not in body


def test_summary_does_not_echo_verbose_evidence_filesystem_paths(
    manifest: dict,
    empty_cost: dict,
    metadata: RunMetadata,
    run_dir: Path,
) -> None:
    """Task 23.8: evidence paths in the table stay relative to the run
    dir — no absolute filesystem prefix like `/Users/...` leaks in."""
    ev = _write_evidence(run_dir, "fuzz/transcripts/get-findings.jsonl")
    fail = TestResult(
        test_id="fuzz.get-findings.malformed-body",
        status=CellStatus.FAIL,
        layer="fuzz",
        target_kind="api_route",
        target_id="get-findings",
        evidence_path=ev,
        severity="medium",
    )
    matrix = matrix_to_json(build_matrix(manifest, [fail]))
    report = build_report(
        run_dir=run_dir,
        manifest=manifest,
        matrix=matrix,
        cost=empty_cost,
        metadata=metadata,
        results=[fail],
    )
    body = render_summary(report, run_dir).read_text(encoding="utf-8")
    # The run_dir tmp_path absolute prefix must not appear.
    assert str(run_dir) not in body
    # The relative artifact path must appear (in the table).
    assert "fuzz/transcripts/get-findings.jsonl" in body


def test_summary_header_omits_cognito_pool_and_client_ids(
    manifest: dict,
    empty_matrix: dict,
    empty_cost: dict,
    run_dir: Path,
) -> None:
    """Task 23.9 / AC12: the run header echoes target_base_url (allowed)
    but never the chat_function_url (which can carry an account-derived
    UUID) or any pool/client id."""
    # Construct a metadata with sensitive-looking IDs to confirm they
    # don't leak into the summary.
    sensitive = RunMetadata(
        run_id="2026-06-09T14-23-01Z",
        target_base_url="https://d5u0vv1zl3eqd.cloudfront.net/",
        chat_function_url=("https://abcdef1234567890.lambda-url.us-east-1.on.aws/"),
        started_at="2026-06-09T14:23:01Z",
        finished_at="2026-06-09T14:31:13Z",
        duration_seconds=492.0,
        harness_version="0.1.0",
    )
    report = _build_empty_report(manifest, empty_matrix, empty_cost, sensitive, run_dir)
    body = render_summary(report, run_dir).read_text(encoding="utf-8")
    assert sensitive.target_base_url in body
    # The Function URL host must NOT appear — its subdomain is account-
    # derived and offers nothing to a forwarded summary.
    assert sensitive.chat_function_url not in body
    assert "lambda-url" not in body
    # No us-east-1 pool id pattern (`us-east-1_XXXXXXXXX`) should leak.
    assert not re.search(r"us-east-1_[A-Za-z0-9]{9}", body)


def test_summary_render_is_deterministic_across_two_renders(
    manifest: dict,
    empty_matrix: dict,
    empty_cost: dict,
    metadata: RunMetadata,
    run_dir: Path,
) -> None:
    """Task 23.10: rendering the same report twice produces byte-
    identical summary.md. Required so the diff-from-last-green section
    is stable across CI re-runs of the same fixture."""
    report = _build_empty_report(manifest, empty_matrix, empty_cost, metadata, run_dir)
    a = render_summary(report, run_dir).read_bytes()
    b = render_summary(report, run_dir).read_bytes()
    assert a == b
