"""Tests for scripts/promote_baseline.py — task 24.

Covers AC14 (a green run promoted via the script lands the baseline file
that the next run's diff reads). Test inventory:

  1. Promoting a green run writes .baseline/last-green.json.
  2. Non-green runs are refused with exit code 2.
  3. The written baseline has the expected shape (test_id → status map).
  4. Default run-dir resolution picks the most recent timestamped dir.
  5. Missing reports dir → exit 1 with error.
  6. Missing report.json under the run dir → exit 1.
  7. Non-timestamp-shaped sibling dirs (e.g. `.baseline/`) are skipped
     when auto-resolving "most recent".
  8. After promotion, the next run's diff reads it correctly (integration).
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts import promote_baseline as pb
from src.coverage.builder import CellStatus, TestResult
from src.reporting.diff import build_diff, load_baseline


# ──────────────────────────── small helpers ─────────────────────────────


def _write_report_json(run_dir: Path, *, failed: int = 0) -> Path:
    """Drop a minimal report.json under run_dir matching the shape
    produced by report_builder.build_report."""
    report = {
        "schema_version": "1.0.0",
        "metadata": {
            "run_id": run_dir.name,
            "target_base_url": "https://d5u0vv1zl3eqd.cloudfront.net/",
            "started_at": "2026-06-09T14:23:01Z",
            "finished_at": "2026-06-09T14:31:13Z",
            "duration_seconds": 492.0,
            "harness_version": "0.1.0",
        },
        "summary": {
            "total_tests_run": 5,
            "passed": 5 - failed,
            "failed": failed,
            "skipped": 0,
            "documented_unsafe": 0,
        },
        "findings": [],
        "cost": {"actual_usd": 0.0, "cap_usd": 1.0, "under_budget": True},
    }
    path = run_dir / "report.json"
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return path


def _write_layer_results(run_dir: Path, layer: str, rows: list[dict]) -> Path:
    """Drop a layer results.json file under <run_dir>/<layer>/."""
    layer_dir = run_dir / layer
    layer_dir.mkdir(parents=True, exist_ok=True)
    path = layer_dir / "results.json"
    path.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    return path


def _green_run(reports_dir: Path, run_id: str = "2026-06-09T14-23-01Z") -> Path:
    """Set up a complete green run with results in two layers."""
    run_dir = reports_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    _write_report_json(run_dir, failed=0)
    _write_layer_results(
        run_dir,
        "e2e",
        [
            {
                "test_id": "e2e.page.findings.ciso",
                "status": "pass",
                "target_kind": "page",
                "target_id": "findings",
                "persona": "ciso",
            },
            {
                "test_id": "e2e.page.findings.soc",
                "status": "pass",
                "target_kind": "page",
                "target_id": "findings",
                "persona": "soc",
            },
        ],
    )
    _write_layer_results(
        run_dir,
        "fuzz",
        [
            {
                "test_id": "fuzz.findings.oversized-body",
                "status": "pass",
                "target_kind": "api_route",
                "target_id": "findings",
                "severity": None,
            }
        ],
    )
    return run_dir


# ──────────────────────────────── tests ─────────────────────────────────


def test_promoting_green_run_writes_baseline_file(tmp_path: Path) -> None:
    """AC14: promote → baseline file appears at the canonical path."""
    reports_dir = tmp_path / "test-reports"
    reports_dir.mkdir()
    run_dir = _green_run(reports_dir)

    exit_code = pb.main(run_dir=run_dir, reports_dir=reports_dir)
    assert exit_code == 0

    baseline_path = reports_dir / ".baseline" / "last-green.json"
    assert baseline_path.is_file()


def test_refusing_to_promote_non_green_run_exits_2(tmp_path: Path) -> None:
    """A run with any failures returns exit 2 (not 1) so a wrapper can
    distinguish 'wasn't promotable' from 'harness broken'."""
    reports_dir = tmp_path / "test-reports"
    reports_dir.mkdir()
    run_dir = reports_dir / "2026-06-09T14-23-01Z"
    run_dir.mkdir()
    _write_report_json(run_dir, failed=3)

    exit_code = pb.main(run_dir=run_dir, reports_dir=reports_dir)
    assert exit_code == 2

    # And no baseline file was written.
    assert not (reports_dir / ".baseline" / "last-green.json").exists()


def test_baseline_file_has_expected_shape(tmp_path: Path) -> None:
    """The written baseline maps test_id → {status, severity, target_id,
    target_kind, layer}, and carries run_id + finished_at metadata."""
    reports_dir = tmp_path / "test-reports"
    reports_dir.mkdir()
    run_dir = _green_run(reports_dir)
    pb.main(run_dir=run_dir, reports_dir=reports_dir)

    baseline = json.loads(
        (reports_dir / ".baseline" / "last-green.json").read_text(encoding="utf-8")
    )

    assert baseline["schema_version"] == pb.BASELINE_SCHEMA_VERSION
    assert baseline["run_id"] == "2026-06-09T14-23-01Z"
    assert baseline["finished_at"] == "2026-06-09T14:31:13Z"
    assert isinstance(baseline["tests"], dict)
    assert "e2e.page.findings.ciso" in baseline["tests"]
    assert "fuzz.findings.oversized-body" in baseline["tests"]
    # One row's keys:
    row = baseline["tests"]["e2e.page.findings.ciso"]
    assert row["status"] == "pass"
    assert row["target_id"] == "findings"
    assert row["target_kind"] == "page"
    assert row["layer"] == "e2e"


def test_default_run_dir_picks_most_recent(tmp_path: Path) -> None:
    """When called without an explicit run_dir, the script promotes the
    lexically-last timestamp dir (which is also the most recent for
    ISO-8601 UTC stamps)."""
    reports_dir = tmp_path / "test-reports"
    reports_dir.mkdir()
    # Older run.
    _green_run(reports_dir, run_id="2026-06-08T14-23-01Z")
    # Newer run.
    newer = _green_run(reports_dir, run_id="2026-06-09T14-23-01Z")

    exit_code = pb.main(run_dir=None, reports_dir=reports_dir)
    assert exit_code == 0
    baseline = json.loads(
        (reports_dir / ".baseline" / "last-green.json").read_text(encoding="utf-8")
    )
    assert baseline["run_id"] == newer.name


def test_missing_reports_dir_exits_1(tmp_path: Path) -> None:
    """Missing test-reports/ tree → exit 1 with an error to stderr."""
    nonexistent = tmp_path / "does-not-exist"
    exit_code = pb.main(run_dir=None, reports_dir=nonexistent)
    assert exit_code == 1


def test_missing_report_json_exits_1(tmp_path: Path) -> None:
    """A run dir without report.json was never completed; exit 1."""
    reports_dir = tmp_path / "test-reports"
    reports_dir.mkdir()
    run_dir = reports_dir / "2026-06-09T14-23-01Z"
    run_dir.mkdir()
    # No report.json written.

    exit_code = pb.main(run_dir=run_dir, reports_dir=reports_dir)
    assert exit_code == 1


def test_non_timestamp_sibling_dirs_are_skipped(tmp_path: Path) -> None:
    """`.baseline/` and other non-timestamp dirs must not be picked up as
    candidate runs by the auto-resolution."""
    reports_dir = tmp_path / "test-reports"
    reports_dir.mkdir()
    (reports_dir / ".baseline").mkdir()  # would lexically sort first
    (reports_dir / "scratch").mkdir()  # not a timestamp shape
    _green_run(reports_dir, run_id="2026-06-09T14-23-01Z")

    exit_code = pb.main(run_dir=None, reports_dir=reports_dir)
    assert exit_code == 0
    baseline = json.loads(
        (reports_dir / ".baseline" / "last-green.json").read_text(encoding="utf-8")
    )
    assert baseline["run_id"] == "2026-06-09T14-23-01Z"


def test_no_runs_at_all_exits_1(tmp_path: Path) -> None:
    """Empty reports dir + no explicit run_dir → exit 1, not 0."""
    reports_dir = tmp_path / "test-reports"
    reports_dir.mkdir()
    exit_code = pb.main(run_dir=None, reports_dir=reports_dir)
    assert exit_code == 1


def test_promoted_baseline_feeds_next_run_diff(tmp_path: Path) -> None:
    """End-to-end (AC14): promote a green run, then verify build_diff
    against that baseline correctly classifies a new failure on the
    next run."""
    reports_dir = tmp_path / "test-reports"
    reports_dir.mkdir()
    run_dir = _green_run(reports_dir)
    assert pb.main(run_dir=run_dir, reports_dir=reports_dir) == 0

    # Now simulate the next run: same test, this time FAIL.
    baseline = load_baseline(reports_dir / ".baseline")
    assert baseline is not None
    assert baseline["run_id"] == run_dir.name

    next_results = [
        TestResult(
            test_id="fuzz.findings.oversized-body",
            status=CellStatus.FAIL,
            layer="fuzz",
            target_kind="api_route",
            target_id="findings",
            evidence_path="fuzz/transcripts/findings.jsonl",
            severity="high",
        ),
    ]
    diff = build_diff(next_results, baseline)
    assert diff["summary"]["has_baseline"] is True
    assert diff["baseline_run_id"] == run_dir.name
    assert len(diff["new_failures"]) == 1
    assert diff["new_failures"][0]["test_id"] == "fuzz.findings.oversized-body"


def test_explicit_run_dir_overrides_auto_resolution(tmp_path: Path) -> None:
    """The CLI lets the operator pick an older run to promote — useful
    when bisecting a flaky regression. The newer run is ignored."""
    reports_dir = tmp_path / "test-reports"
    reports_dir.mkdir()
    older = _green_run(reports_dir, run_id="2026-06-08T14-23-01Z")
    _green_run(reports_dir, run_id="2026-06-09T14-23-01Z")

    exit_code = pb.main(run_dir=older, reports_dir=reports_dir)
    assert exit_code == 0
    baseline = json.loads(
        (reports_dir / ".baseline" / "last-green.json").read_text(encoding="utf-8")
    )
    assert baseline["run_id"] == "2026-06-08T14-23-01Z"


def test_main_handles_run_dir_path_not_existing(tmp_path: Path) -> None:
    """A typo'd run_dir path returns 1 — explicit error, not silent."""
    reports_dir = tmp_path / "test-reports"
    reports_dir.mkdir()
    exit_code = pb.main(
        run_dir=reports_dir / "no-such-run",
        reports_dir=reports_dir,
    )
    assert exit_code == 1


def test_corrupt_report_json_exits_1(tmp_path: Path) -> None:
    """A report.json that isn't valid JSON → exit 1 with the error
    surfaced rather than crashing with a stack trace."""
    reports_dir = tmp_path / "test-reports"
    reports_dir.mkdir()
    run_dir = reports_dir / "2026-06-09T14-23-01Z"
    run_dir.mkdir()
    (run_dir / "report.json").write_text("not valid json {", encoding="utf-8")
    exit_code = pb.main(run_dir=run_dir, reports_dir=reports_dir)
    assert exit_code == 1
