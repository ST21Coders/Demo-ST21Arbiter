"""Tests for src/reporting/report_builder.py — task 21.

Coverage:
  1. Empty results produces a valid report (all-zero counts, empty findings).
  2. Single FAIL with valid evidence produces one finding in findings[].
  3. FAIL without evidence raises MissingEvidenceError.
  4. FAIL whose evidence_path points to a missing file raises MissingEvidenceError.
  5. Findings are ranked by severity (critical first).
  6. Severity ties are broken by target_id alphabetically.
  7. cost.under_budget is True when actual < cap, False when ≥ cap.
  8. schema_version is "1.0.0".
  9. DOCUMENTED_UNSAFE counted in summary, NOT in findings[].
  10. Tools-covered label reflects the 12-entry manifest.
  11. JSON serialization is deterministic (same inputs -> same bytes).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.coverage.builder import (
    CellStatus,
    TestResult,
    build_matrix,
    matrix_to_json,
)
from src.reporting.report_builder import (
    DEFAULT_COST_CAP_USD,
    SCHEMA_VERSION,
    MissingEvidenceError,
    RunMetadata,
    build_report,
    serialize_report,
)


_HARNESS_ROOT = Path(__file__).resolve().parent.parent
_MANIFEST_PATH = _HARNESS_ROOT / "src" / "coverage" / "manifest.json"


# ──────────────────────────────── fixtures ─────────────────────────────────


@pytest.fixture
def manifest() -> dict:
    """The real, committed manifest. Tests depend on its current shape
    (15 pages × 4 personas = 60 cells, 25 api_routes, 12 agent_tools)."""
    return json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def empty_matrix(manifest: dict) -> dict:
    """A coverage matrix built with no results — every cell uncovered."""
    return matrix_to_json(build_matrix(manifest, []))


@pytest.fixture
def empty_cost() -> dict:
    """The shape CostTracker.as_dict() returns for a run with no records."""
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
    """Per-test run directory; tests create evidence files inside it."""
    rd = tmp_path / "2026-06-09T14-23-01Z"
    rd.mkdir()
    return rd


def _write_evidence(run_dir: Path, relpath: str) -> str:
    """Create a fake evidence file under run_dir and return its relative
    path. Returned form matches what the layer adapters write into
    `evidence_path` on TestResult."""
    target = run_dir / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("fake evidence", encoding="utf-8")
    return relpath


# ──────────────────────────────── tests ────────────────────────────────────


def test_empty_results_produces_valid_report(
    manifest: dict,
    empty_matrix: dict,
    empty_cost: dict,
    metadata: RunMetadata,
    run_dir: Path,
) -> None:
    """An empty-results run should still produce a fully-formed report.

    No layer ran -> no findings, no costs. All structural keys present so the
    renderer can render a "ran but nothing happened" page without special-casing.
    """
    report = build_report(
        run_dir=run_dir,
        manifest=manifest,
        matrix=empty_matrix,
        cost=empty_cost,
        metadata=metadata,
        results=[],
    )
    assert report["schema_version"] == "1.0.0"
    assert report["metadata"]["run_id"] == "2026-06-09T14-23-01Z"
    assert report["coverage"] is empty_matrix
    assert report["findings"] == []
    assert report["cost"]["actual_usd"] == 0.0
    assert report["cost"]["cap_usd"] == DEFAULT_COST_CAP_USD
    assert report["cost"]["under_budget"] is True
    assert report["summary"]["total_tests_run"] == 0
    assert report["summary"]["passed"] == 0
    assert report["summary"]["failed"] == 0
    assert report["summary"]["documented_unsafe"] == 0
    # Task 24: the diff block is always present (never None). With no
    # baseline supplied, it renders as the AC15 empty-shape with the
    # promotable note.
    diff = report["diff_from_last_green"]
    assert isinstance(diff, dict)
    assert diff["summary"]["has_baseline"] is False
    assert diff["summary"]["promotable_note"] == "no baseline; this run will be promotable"
    assert diff["new_failures"] == []
    assert diff["resolved"] == []


def test_single_fail_with_evidence_produces_one_finding(
    manifest: dict,
    empty_cost: dict,
    metadata: RunMetadata,
    run_dir: Path,
) -> None:
    """A single FAIL row -> exactly one finding with the right shape."""
    evidence = _write_evidence(run_dir, "e2e/screenshots/findings-soc-fail.png")
    fail = TestResult(
        test_id="e2e.page.findings.soc",
        status=CellStatus.FAIL,
        layer="e2e",
        target_kind="page",
        target_id="findings",
        persona="soc",
        evidence_path=evidence,
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
    assert len(report["findings"]) == 1
    finding = report["findings"][0]
    assert finding["test_id"] == "e2e.page.findings.soc"
    assert finding["severity"] == "high"
    assert finding["layer"] == "e2e"
    assert finding["target_kind"] == "page"
    assert finding["target_id"] == "findings"
    assert finding["evidence_path"] == evidence
    assert finding["summary"]  # non-empty


def test_fail_without_evidence_raises_missing_evidence_error(
    manifest: dict,
    empty_matrix: dict,
    empty_cost: dict,
    metadata: RunMetadata,
    run_dir: Path,
) -> None:
    """A FAIL TestResult with no evidence_path -> MissingEvidenceError.

    builder.py raises this at matrix-build time too, but here we exercise
    the report builder's own AC20 guard (the path where someone constructed
    the matrix differently and the FAIL still needs to be caught).
    """
    fail = TestResult(
        test_id="e2e.page.findings.soc",
        status=CellStatus.FAIL,
        layer="e2e",
        target_kind="page",
        target_id="findings",
        persona="soc",
        evidence_path=None,  # AC20 violation
        severity="high",
    )
    with pytest.raises(MissingEvidenceError) as excinfo:
        build_report(
            run_dir=run_dir,
            manifest=manifest,
            matrix=empty_matrix,
            cost=empty_cost,
            metadata=metadata,
            results=[fail],
        )
    assert "evidence_path" in str(excinfo.value)
    assert "e2e.page.findings.soc" in str(excinfo.value)


def test_fail_with_nonexistent_evidence_raises_missing_evidence_error(
    manifest: dict,
    empty_matrix: dict,
    empty_cost: dict,
    metadata: RunMetadata,
    run_dir: Path,
) -> None:
    """evidence_path points somewhere — but no such file exists -> raise."""
    fail = TestResult(
        test_id="auth.token-usage.soc-forbidden",
        status=CellStatus.FAIL,
        layer="auth",
        target_kind="api_route",
        target_id="get-token-usage",
        evidence_path="auth/probes/does-not-exist.jsonl",
        severity="high",
    )
    with pytest.raises(MissingEvidenceError) as excinfo:
        build_report(
            run_dir=run_dir,
            manifest=manifest,
            matrix=empty_matrix,
            cost=empty_cost,
            metadata=metadata,
            results=[fail],
        )
    assert "does-not-exist.jsonl" in str(excinfo.value)


def test_findings_ranked_by_severity(
    manifest: dict,
    empty_cost: dict,
    metadata: RunMetadata,
    run_dir: Path,
) -> None:
    """critical > high > medium > low > info."""
    rows = []
    for severity in ("low", "critical", "medium", "info", "high"):
        ev = _write_evidence(run_dir, f"e2e/{severity}.png")
        rows.append(
            TestResult(
                test_id=f"e2e.page.findings.{severity}-test",
                status=CellStatus.FAIL,
                layer="e2e",
                target_kind="page",
                target_id="findings",
                persona="soc",
                evidence_path=ev,
                severity=severity,
            )
        )
    matrix = matrix_to_json(build_matrix(manifest, [rows[0]]))  # any matrix is fine
    report = build_report(
        run_dir=run_dir,
        manifest=manifest,
        matrix=matrix,
        cost=empty_cost,
        metadata=metadata,
        results=rows,
    )
    assert [f["severity"] for f in report["findings"]] == [
        "critical",
        "high",
        "medium",
        "low",
        "info",
    ]


def test_findings_ties_broken_alphabetically_by_target_id(
    manifest: dict,
    empty_cost: dict,
    metadata: RunMetadata,
    run_dir: Path,
) -> None:
    """Same severity -> stable tiebreak by target_id ascending."""
    rows = []
    # Insert in reverse-alphabetic order so the sort actually has work to do.
    for target_id in ("heatmap", "findings", "dashboard"):
        ev = _write_evidence(run_dir, f"e2e/{target_id}.png")
        rows.append(
            TestResult(
                test_id=f"e2e.page.{target_id}.soc",
                status=CellStatus.FAIL,
                layer="e2e",
                target_kind="page",
                target_id=target_id,
                persona="soc",
                evidence_path=ev,
                severity="high",
            )
        )
    matrix = matrix_to_json(build_matrix(manifest, rows))
    report = build_report(
        run_dir=run_dir,
        manifest=manifest,
        matrix=matrix,
        cost=empty_cost,
        metadata=metadata,
        results=rows,
    )
    assert [f["target_id"] for f in report["findings"]] == [
        "dashboard",
        "findings",
        "heatmap",
    ]


def test_cost_under_budget_flag(
    manifest: dict,
    empty_matrix: dict,
    metadata: RunMetadata,
    run_dir: Path,
) -> None:
    """actual < cap -> under_budget True; actual >= cap -> False."""
    under = {"total_usd": 0.42, "per_layer_usd": {"llm": 0.42}, "probe_counts": {}}
    report_under = build_report(
        run_dir=run_dir,
        manifest=manifest,
        matrix=empty_matrix,
        cost=under,
        metadata=metadata,
        results=[],
    )
    assert report_under["cost"]["under_budget"] is True
    assert report_under["cost"]["actual_usd"] == 0.42

    over = {"total_usd": 1.50, "per_layer_usd": {"llm": 1.50}, "probe_counts": {}}
    report_over = build_report(
        run_dir=run_dir,
        manifest=manifest,
        matrix=empty_matrix,
        cost=over,
        metadata=metadata,
        results=[],
    )
    assert report_over["cost"]["under_budget"] is False

    # Equal-to-cap is NOT under (AC3 says "less than 1.00").
    equal = {"total_usd": 1.00, "per_layer_usd": {}, "probe_counts": {}}
    report_equal = build_report(
        run_dir=run_dir,
        manifest=manifest,
        matrix=empty_matrix,
        cost=equal,
        metadata=metadata,
        results=[],
    )
    assert report_equal["cost"]["under_budget"] is False


def test_schema_version_is_pinned(
    manifest: dict,
    empty_matrix: dict,
    empty_cost: dict,
    metadata: RunMetadata,
    run_dir: Path,
) -> None:
    assert SCHEMA_VERSION == "1.0.0"
    report = build_report(
        run_dir=run_dir,
        manifest=manifest,
        matrix=empty_matrix,
        cost=empty_cost,
        metadata=metadata,
        results=[],
    )
    assert report["schema_version"] == "1.0.0"


def test_documented_unsafe_counted_but_not_in_findings(
    manifest: dict,
    empty_cost: dict,
    metadata: RunMetadata,
    run_dir: Path,
) -> None:
    """AC11: documented-unsafe rows confirm a known contract -> NOT findings.

    But the summary still records the count so the operator can see at a
    glance how many "we know about this" probes ran today.
    """
    doc = TestResult(
        test_id="auth.chat.no-signature",
        status=CellStatus.DOCUMENTED_UNSAFE,
        layer="auth",
        target_kind="api_route",
        target_id="post-chat",
    )
    matrix = matrix_to_json(build_matrix(manifest, [doc]))
    report = build_report(
        run_dir=run_dir,
        manifest=manifest,
        matrix=matrix,
        cost=empty_cost,
        metadata=metadata,
        results=[doc],
    )
    assert report["findings"] == []
    assert report["summary"]["documented_unsafe"] == 1
    assert report["summary"]["failed"] == 0


def test_tools_covered_label_reflects_12_entry_manifest(
    manifest: dict,
    empty_matrix: dict,
    empty_cost: dict,
    metadata: RunMetadata,
    run_dir: Path,
) -> None:
    """The manifest has 12 agent_tools (incl. master.chat_surface sentinel).

    No results means 0/12 covered. This guards against a future regression
    where the sentinel tool is silently dropped from the manifest.
    """
    report = build_report(
        run_dir=run_dir,
        manifest=manifest,
        matrix=empty_matrix,
        cost=empty_cost,
        metadata=metadata,
        results=[],
    )
    assert report["summary"]["tools_covered_label"] == "0/12"


def test_json_output_is_deterministic(
    manifest: dict,
    empty_cost: dict,
    metadata: RunMetadata,
    run_dir: Path,
) -> None:
    """Building twice from the same inputs must produce identical bytes.

    The diff-from-last-green block (task 24) leans on this: if today's run
    serializes differently from yesterday's for the same logical state, the
    diff is full of false positives.
    """
    ev = _write_evidence(run_dir, "e2e/det.png")
    fail = TestResult(
        test_id="e2e.page.findings.soc",
        status=CellStatus.FAIL,
        layer="e2e",
        target_kind="page",
        target_id="findings",
        persona="soc",
        evidence_path=ev,
        severity="high",
    )
    matrix = matrix_to_json(build_matrix(manifest, [fail]))
    report_a = build_report(
        run_dir=run_dir,
        manifest=manifest,
        matrix=matrix,
        cost=empty_cost,
        metadata=metadata,
        results=[fail],
    )
    report_b = build_report(
        run_dir=run_dir,
        manifest=manifest,
        matrix=matrix,
        cost=empty_cost,
        metadata=metadata,
        results=[fail],
    )
    assert serialize_report(report_a) == serialize_report(report_b)


def test_serialize_report_round_trips_via_json(
    manifest: dict,
    empty_matrix: dict,
    empty_cost: dict,
    metadata: RunMetadata,
    run_dir: Path,
) -> None:
    """The serialized form must parse back into the same dict."""
    report = build_report(
        run_dir=run_dir,
        manifest=manifest,
        matrix=empty_matrix,
        cost=empty_cost,
        metadata=metadata,
        results=[],
    )
    s = serialize_report(report)
    assert s.endswith("\n")  # POSIX-clean
    parsed = json.loads(s)
    assert parsed == report


def test_findings_with_unknown_severity_sort_after_known_tiers(
    manifest: dict,
    empty_cost: dict,
    metadata: RunMetadata,
    run_dir: Path,
) -> None:
    """An unknown severity label is sorted to the end + folded into 'info'
    in the failures_by_severity summary, so a typo doesn't crash the report
    but is still visible."""
    ev_good = _write_evidence(run_dir, "e2e/good.png")
    ev_weird = _write_evidence(run_dir, "e2e/weird.png")
    rows = [
        TestResult(
            test_id="e2e.page.findings.soc",
            status=CellStatus.FAIL,
            layer="e2e",
            target_kind="page",
            target_id="findings",
            persona="soc",
            evidence_path=ev_good,
            severity="high",
        ),
        TestResult(
            test_id="e2e.page.heatmap.soc",
            status=CellStatus.FAIL,
            layer="e2e",
            target_kind="page",
            target_id="heatmap",
            persona="soc",
            evidence_path=ev_weird,
            severity="urgentest",  # not a known tier
        ),
    ]
    matrix = matrix_to_json(build_matrix(manifest, rows))
    report = build_report(
        run_dir=run_dir,
        manifest=manifest,
        matrix=matrix,
        cost=empty_cost,
        metadata=metadata,
        results=rows,
    )
    severities = [f["severity"] for f in report["findings"]]
    assert severities[0] == "high"
    assert severities[1] == "urgentest"
    # Unknown severity rolled into "info" bucket in the summary.
    assert report["summary"]["failures_by_severity"]["info"] == 1
    assert report["summary"]["failures_by_severity"]["high"] == 1


def test_cost_per_layer_block_is_passed_through(
    manifest: dict,
    empty_matrix: dict,
    metadata: RunMetadata,
    run_dir: Path,
) -> None:
    """The per_layer_usd dict from CostTracker.as_dict() must survive intact."""
    cost = {
        "total_usd": 0.0042,
        "per_layer_usd": {"e2e": 0.0, "fuzz": 0.0, "auth": 0.0, "llm": 0.0042},
        "probe_counts": {"llm": 30},
    }
    report = build_report(
        run_dir=run_dir,
        manifest=manifest,
        matrix=empty_matrix,
        cost=cost,
        metadata=metadata,
        results=[],
    )
    assert report["cost"]["per_layer_usd"] == {
        "e2e": 0.0,
        "fuzz": 0.0,
        "auth": 0.0,
        "llm": 0.0042,
    }
    assert report["cost"]["probe_counts"] == {"llm": 30}


def test_summary_uses_matrix_coverage_labels(
    manifest: dict,
    empty_cost: dict,
    metadata: RunMetadata,
    run_dir: Path,
) -> None:
    """Coverage labels in summary come from the matrix's own summary block
    rather than being recomputed — keeps the two parts of the report in
    lockstep so a future change to the matrix builder doesn't desync."""
    rows = []
    # Cover one page-persona cell.
    rows.append(
        TestResult(
            test_id="e2e.page.dashboard.ciso",
            status=CellStatus.PASS,
            layer="e2e",
            target_kind="page",
            target_id="dashboard",
            persona="ciso",
        )
    )
    matrix = matrix_to_json(build_matrix(manifest, rows))
    report = build_report(
        run_dir=run_dir,
        manifest=manifest,
        matrix=matrix,
        cost=empty_cost,
        metadata=metadata,
        results=rows,
    )
    # 1 of 60 cells covered, 0 of 25 routes, 0 of 12 tools.
    assert report["summary"]["pages_covered_label"] == "1/60"
    assert report["summary"]["routes_covered_label"] == "0/25"
    assert report["summary"]["tools_covered_label"] == "0/12"
