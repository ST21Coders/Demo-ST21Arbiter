"""Tests for src/reporting/diff.py — task 24.

Covers AC14 (diff visible after a green run is promoted) and AC15 (no
baseline yet → graceful empty-shape, not a crash). Each test case
mirrors one transition class in the diff vocabulary so a regression in
classification is caught immediately.

Test inventory:
  1. None baseline → empty-shape with has_baseline=False + promotable note.
  2. Baseline pass + current fail → new_failures has 1, resolved empty.
  3. Baseline fail + current pass → resolved has 1, new_failures empty.
  4. Same status both runs → entry in neither list (unchanged).
  5. documented_unsafe → fail transition → flapping.
  6. In baseline but not current → removed_tests.
  7. In current but not baseline → new_tests.
  8. net_change = new_failure_count - resolved_count.
  9. load_baseline: missing file → None, corrupt JSON → CorruptBaselineError.
  10. Deterministic ordering of diff lists (alphabetic by test_id).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.coverage.builder import CellStatus, TestResult
from src.reporting.diff import (
    CorruptBaselineError,
    build_diff,
    load_baseline,
)


# ──────────────────────────── small helpers ─────────────────────────────


def _result(
    test_id: str,
    status: CellStatus,
    *,
    target_id: str = "findings",
    severity: str | None = None,
) -> TestResult:
    """Build a TestResult with sensible defaults for diff tests.

    We pick `target_kind="api_route"` so we don't need to set `persona`
    (the page kind requires it). The diff module only cares about
    test_id / status / severity / target_id, so the kind is incidental
    for these tests.
    """
    return TestResult(
        test_id=test_id,
        status=status,
        layer="fuzz",
        target_kind="api_route",
        target_id=target_id,
        severity=severity,
    )


def _baseline_with(tests: dict) -> dict:
    """Wrap a `{test_id: row}` map in the full baseline file shape."""
    return {
        "schema_version": "1.0.0",
        "run_id": "2026-06-08T14-23-01Z",
        "finished_at": "2026-06-08T14:31:13Z",
        "tests": tests,
    }


# ──────────────────────────────── tests ─────────────────────────────────


def test_no_baseline_returns_empty_shape_with_promotable_note() -> None:
    """AC15: a None baseline returns the empty-shape diff payload.

    Every diff bucket is an empty list (not None), so downstream
    consumers can `len(...)` without per-key None-checks. The summary
    block has has_baseline=False AND the canonical promotable_note.
    """
    diff = build_diff([_result("e2e.page.findings.ciso", CellStatus.PASS)], None)

    assert diff["baseline_run_id"] is None
    assert diff["baseline_finished_at"] is None
    assert diff["new_failures"] == []
    assert diff["resolved"] == []
    assert diff["new_tests"] == []
    assert diff["removed_tests"] == []
    assert diff["flapping"] == []
    assert diff["summary"]["has_baseline"] is False
    assert (
        diff["summary"]["promotable_note"] == "no baseline; this run will be promotable"
    )
    assert diff["summary"]["new_failure_count"] == 0
    assert diff["summary"]["resolved_count"] == 0
    assert diff["summary"]["net_change"] == 0


def test_pass_in_baseline_becomes_fail_in_current_is_new_failure() -> None:
    """Test class: regression. Baseline pass + current fail."""
    baseline = _baseline_with(
        {
            "fuzz.findings.oversized-body": {
                "status": "pass",
                "severity": None,
                "target_id": "findings",
            }
        }
    )
    results = [
        _result(
            "fuzz.findings.oversized-body",
            CellStatus.FAIL,
            severity="high",
        )
    ]

    diff = build_diff(results, baseline)

    assert len(diff["new_failures"]) == 1
    assert diff["new_failures"][0]["test_id"] == "fuzz.findings.oversized-body"
    assert diff["new_failures"][0]["transition"] == "new_failure"
    assert diff["new_failures"][0]["baseline_status"] == "pass"
    assert diff["new_failures"][0]["current_status"] == "fail"
    assert diff["new_failures"][0]["severity"] == "high"
    assert diff["resolved"] == []
    assert diff["summary"]["new_failure_count"] == 1
    assert diff["summary"]["resolved_count"] == 0
    assert diff["summary"]["has_baseline"] is True


def test_fail_in_baseline_becomes_pass_in_current_is_resolved() -> None:
    """Test class: improvement. Baseline fail + current pass."""
    baseline = _baseline_with(
        {
            "fuzz.findings.oversized-body": {
                "status": "fail",
                "severity": "high",
                "target_id": "findings",
            }
        }
    )
    results = [_result("fuzz.findings.oversized-body", CellStatus.PASS)]

    diff = build_diff(results, baseline)

    assert diff["new_failures"] == []
    assert len(diff["resolved"]) == 1
    assert diff["resolved"][0]["test_id"] == "fuzz.findings.oversized-body"
    assert diff["resolved"][0]["transition"] == "resolved"
    assert diff["resolved"][0]["baseline_status"] == "fail"
    assert diff["resolved"][0]["current_status"] == "pass"
    assert diff["summary"]["new_failure_count"] == 0
    assert diff["summary"]["resolved_count"] == 1


def test_same_status_both_runs_emits_no_entry() -> None:
    """`unchanged` transitions are intentionally omitted from the diff —
    they'd drown the report on the typical green run."""
    baseline = _baseline_with(
        {
            "e2e.page.findings.ciso": {
                "status": "pass",
                "target_id": "findings",
            },
            "fuzz.findings.oversized-body": {
                "status": "fail",
                "target_id": "findings",
            },
        }
    )
    results = [
        _result("e2e.page.findings.ciso", CellStatus.PASS),
        _result("fuzz.findings.oversized-body", CellStatus.FAIL),
    ]

    diff = build_diff(results, baseline)

    # Same statuses on both sides → unchanged → not emitted anywhere.
    assert diff["new_failures"] == []
    assert diff["resolved"] == []
    assert diff["new_tests"] == []
    assert diff["removed_tests"] == []
    assert diff["flapping"] == []


def test_documented_unsafe_to_fail_lands_in_flapping() -> None:
    """AC11 + task 24: documented_unsafe ↔ fail is the `flapping` bucket.

    Going from documented_unsafe → fail means the platform's contract
    tightened (or broke) — the operator wants to see it but it isn't a
    'new failure' in the regression sense.
    """
    baseline = _baseline_with(
        {
            "auth.chat.no-signature": {
                "status": "documented_unsafe",
                "target_id": "chat",
            }
        }
    )
    results = [_result("auth.chat.no-signature", CellStatus.FAIL, target_id="chat")]

    diff = build_diff(results, baseline)

    assert diff["new_failures"] == []
    assert diff["resolved"] == []
    assert len(diff["flapping"]) == 1
    assert diff["flapping"][0]["transition"] == "flapping"
    assert diff["flapping"][0]["baseline_status"] == "documented_unsafe"
    assert diff["flapping"][0]["current_status"] == "fail"


def test_test_in_baseline_but_not_current_is_removed_test() -> None:
    """Coverage shrank: the operator deserves to see it."""
    baseline = _baseline_with(
        {
            "fuzz.findings.removed-probe": {
                "status": "pass",
                "target_id": "findings",
            }
        }
    )
    results: list[TestResult] = []  # the test isn't in the current run

    diff = build_diff(results, baseline)

    assert len(diff["removed_tests"]) == 1
    assert diff["removed_tests"][0]["test_id"] == "fuzz.findings.removed-probe"
    assert diff["removed_tests"][0]["current_status"] == "absent"
    assert diff["removed_tests"][0]["transition"] == "removed_test"
    assert diff["new_failures"] == []


def test_test_in_current_but_not_baseline_is_new_test() -> None:
    """Coverage grew: tracked, but it isn't a regression unless it fails."""
    baseline = _baseline_with({})  # empty baseline
    results = [_result("fuzz.findings.added-probe", CellStatus.PASS)]

    diff = build_diff(results, baseline)

    assert len(diff["new_tests"]) == 1
    assert diff["new_tests"][0]["test_id"] == "fuzz.findings.added-probe"
    assert diff["new_tests"][0]["transition"] == "new_test"
    assert diff["new_tests"][0]["baseline_status"] == "absent"
    assert diff["new_tests"][0]["current_status"] == "pass"
    # A new test that passes does NOT show up in new_failures.
    assert diff["new_failures"] == []


def test_new_test_that_fails_lands_in_both_new_tests_and_new_failures() -> None:
    """A brand-new test landing as a failure is reported in both lists:
    it's coverage growth AND a regression surface the operator should see
    in the ranked findings."""
    baseline = _baseline_with({})
    results = [_result("fuzz.findings.added-fail", CellStatus.FAIL, severity="medium")]

    diff = build_diff(results, baseline)

    assert len(diff["new_tests"]) == 1
    assert len(diff["new_failures"]) == 1
    assert diff["new_failures"][0]["test_id"] == "fuzz.findings.added-fail"
    assert diff["summary"]["new_failure_count"] == 1


def test_net_change_equals_new_failures_minus_resolved() -> None:
    """AC summary: net_change exposes the regression-vs-improvement
    balance in one int. Positive = net regression, negative = net
    improvement, zero = wash."""
    baseline = _baseline_with(
        {
            "t1": {"status": "pass", "target_id": "x"},  # → fail (new_failure)
            "t2": {"status": "pass", "target_id": "x"},  # → fail (new_failure)
            "t3": {"status": "fail", "target_id": "x"},  # → pass (resolved)
        }
    )
    results = [
        _result("t1", CellStatus.FAIL),
        _result("t2", CellStatus.FAIL),
        _result("t3", CellStatus.PASS),
    ]

    diff = build_diff(results, baseline)

    assert diff["summary"]["new_failure_count"] == 2
    assert diff["summary"]["resolved_count"] == 1
    assert diff["summary"]["net_change"] == 1  # +1 net regression


def test_load_baseline_returns_none_when_missing(tmp_path: Path) -> None:
    """AC15: a missing baseline file is not an error — it's the first-run
    state. load_baseline returns None and the caller passes that to
    build_diff to get the empty-shape."""
    assert load_baseline(tmp_path) is None


def test_load_baseline_raises_on_corrupt_json(tmp_path: Path) -> None:
    """A baseline that exists but isn't valid JSON could mask a real
    regression if silently treated as 'no baseline'. We raise instead."""
    (tmp_path / "last-green.json").write_text("not valid json {", encoding="utf-8")
    with pytest.raises(CorruptBaselineError):
        load_baseline(tmp_path)


def test_load_baseline_raises_when_tests_key_missing(tmp_path: Path) -> None:
    """The diff loader can't operate on a baseline without a `tests` map,
    and silently zeroing it out would mask the issue."""
    (tmp_path / "last-green.json").write_text(
        json.dumps({"run_id": "x"}), encoding="utf-8"
    )
    with pytest.raises(CorruptBaselineError):
        load_baseline(tmp_path)


def test_load_baseline_round_trip(tmp_path: Path) -> None:
    """Happy path: a well-formed baseline round-trips into build_diff."""
    payload = {
        "schema_version": "1.0.0",
        "run_id": "2026-06-08T14-23-01Z",
        "finished_at": "2026-06-08T14:31:13Z",
        "tests": {
            "fuzz.findings.oversized-body": {
                "status": "pass",
                "severity": None,
                "target_id": "findings",
            }
        },
    }
    (tmp_path / "last-green.json").write_text(json.dumps(payload), encoding="utf-8")
    baseline = load_baseline(tmp_path)
    assert baseline is not None
    assert baseline["run_id"] == "2026-06-08T14-23-01Z"
    diff = build_diff(
        [_result("fuzz.findings.oversized-body", CellStatus.PASS)],
        baseline,
    )
    # Same status on both sides → unchanged → empty buckets.
    assert diff["new_failures"] == []
    assert diff["resolved"] == []
    assert diff["summary"]["has_baseline"] is True
    assert diff["baseline_run_id"] == "2026-06-08T14-23-01Z"


def test_diff_ordering_is_deterministic() -> None:
    """The diff section is a forwardable artifact; a churning order would
    muddle PR reviews. All buckets are sorted by test_id ascending."""
    baseline = _baseline_with(
        {
            "zzz.test.a": {"status": "pass", "target_id": "x"},
            "aaa.test.a": {"status": "pass", "target_id": "x"},
            "mmm.test.a": {"status": "pass", "target_id": "x"},
        }
    )
    results = [
        _result("zzz.test.a", CellStatus.FAIL),
        _result("aaa.test.a", CellStatus.FAIL),
        _result("mmm.test.a", CellStatus.FAIL),
    ]
    diff = build_diff(results, baseline)
    ids = [entry["test_id"] for entry in diff["new_failures"]]
    assert ids == sorted(ids)


def test_dict_input_works_alongside_dataclass_input() -> None:
    """The harness's diff caller (report_builder) passes TestResult
    dataclass instances, but unit tests and future callers may pass
    dicts. Both must work to keep the diff module decoupled from the
    dataclass type."""
    baseline = _baseline_with(
        {
            "fuzz.findings.x": {"status": "pass", "target_id": "findings"},
        }
    )
    # Status as a plain string (not the enum). This is what a hand-built
    # results.json row looks like before coverage.builder parses it.
    diff = build_diff(
        [{"test_id": "fuzz.findings.x", "status": "fail", "severity": "high"}],
        baseline,
    )
    assert len(diff["new_failures"]) == 1
    assert diff["new_failures"][0]["test_id"] == "fuzz.findings.x"
