"""src/reporting/diff.py — compare current run results to last-green baseline.

Task 24 of the full-app adversarial testing plan. The report builder calls
`build_diff(...)` once per run, after every layer has emitted its
results.json and after the coverage matrix has been built. The diff feeds
two surfaces:

  - `report.json["diff_from_last_green"]` — the machine-readable section
    the renderer drills into for the "Diff from last green" block.
  - `summary.md` — the forwardable digest's diff bullets (handled by the
    renderer's existing `_diff_for_template`).

────────────────────────────────────────────────────────────────────────────
Acceptance criteria this module enforces
────────────────────────────────────────────────────────────────────────────

  AC14 (diff visible after promotion): once a green run has been promoted
       via `scripts/promote_baseline.py`, the NEXT run's diff carries
       `summary.has_baseline = True` and the baseline_run_id matches the
       promoted run's id.

  AC15 (no-baseline graceful state): a run with no prior baseline (first-
       ever run, or `.baseline/` missing) returns the empty-shape diff
       with `has_baseline = False` and a `promotable_note`. Never crashes.

────────────────────────────────────────────────────────────────────────────
Diff transition vocabulary
────────────────────────────────────────────────────────────────────────────

For every (test_id) seen in the current run OR the baseline, we classify
the transition into one of these buckets (the report renders one section
per bucket):

  - "new_failure"  : baseline=pass/absent, current=fail. Most actionable.
                     Listed in `new_failures[]` with severity surfaced.
  - "resolved"     : baseline=fail, current=pass. Listed in `resolved[]`.
  - "new_test"     : baseline=absent (test didn't exist), current=anything.
                     Tracks coverage growth across runs.
  - "removed_test" : baseline=present, current=absent. Tracks regressions
                     in coverage (a test was deleted or a layer stopped
                     running).
  - "flapping"     : documented_unsafe ↔ fail transitions. AC11 documents
                     these as "if the contract tightens we want to know"
                     — they're not new_failures (no new defect) but they're
                     not silent either.
  - "unchanged"    : same status both runs. Not emitted in the diff —
                     would drown the report.

────────────────────────────────────────────────────────────────────────────
Baseline shape (read from .baseline/last-green.json)
────────────────────────────────────────────────────────────────────────────

    {
      "run_id": "2026-06-08T14-23-01Z",
      "finished_at": "2026-06-08T14:31:13Z",
      "tests": {
        "e2e.page.findings.ciso": {
          "status": "pass",
          "severity": null,
          "target_id": "findings",
          "target_kind": "page",
          "layer": "e2e"
        },
        ...
      }
    }

Writing this shape is the job of `scripts/promote_baseline.py`; this
module is read-only against it.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

# ─────────────────────────── public exceptions ─────────────────────────────


class BaselineError(RuntimeError):
    """Base for all errors raised from this module."""


class CorruptBaselineError(BaselineError):
    """The baseline file exists but is not valid JSON or is missing
    required structure. We refuse to silently ignore it because a corrupt
    baseline could mask a real regression.
    """


# ─────────────────────────────── data model ────────────────────────────────


# Statuses we treat as "this test failed" when computing transitions.
# Mirrors the failure tally in `coverage.builder._FAILURE_STATUSES` and
# `report_builder` — DOCUMENTED_UNSAFE is explicitly NOT a failure here,
# it's a separate transition class (the "flapping" bucket).
_FAILURE_STATUSES: frozenset[str] = frozenset({"fail"})

# Statuses we treat as "this test passed" for resolution detection.
# A SKIPPED -> any transition isn't "resolved" in the AC14 sense — only a
# real green-from-red flip counts.
_PASS_STATUSES: frozenset[str] = frozenset({"pass"})


@dataclass(frozen=True)
class DiffEntry:
    """One row in any of the diff buckets.

    `baseline_status` and `current_status` are the raw string statuses
    (matching `CellStatus.value` from the coverage builder). `transition`
    is one of the labels described in the module docstring.

    `severity` is the severity tag carried on the *current* result — used
    so the renderer can rank new_failures by tier. Resolved entries pass
    through the baseline's severity (so the operator sees what tier of
    finding got fixed).

    `target_id` enables stable secondary ordering (alphabetic) so the diff
    block doesn't churn on ties across runs.
    """

    test_id: str
    baseline_status: str
    current_status: str
    transition: str
    severity: str | None
    target_id: str


# ─────────────────────────────── constants ─────────────────────────────────


# Schema of the baseline file. Bumping this requires `promote_baseline.py`
# to also bump it — the diff loader rejects unknown versions with a
# CorruptBaselineError so a stale baseline produced by an older harness
# doesn't silently mis-render.
BASELINE_SCHEMA_VERSION = "1.0.0"

# Standard "no baseline" promotable hint shown in the diff block when the
# baseline file is missing. Worded as a hint to the operator, not as an
# error — the AC15 phrasing is explicit.
_NO_BASELINE_NOTE = "no baseline; this run will be promotable"


# ─────────────────────────────── helpers ───────────────────────────────────


def _classify(baseline_status: str, current_status: str) -> str:
    """Pick one of the transition labels from the two statuses.

    Precedence (most-actionable first):
      1. test in baseline but not in current → "removed_test"
      2. test in current but not in baseline → "new_test"
      3. documented_unsafe ↔ fail in either direction → "flapping"
      4. baseline pass-or-absent + current fail → "new_failure"
      5. baseline fail + current pass → "resolved"
      6. otherwise → "unchanged"

    "absent" is the sentinel string this module uses for "not present"; it
    is NEVER a CellStatus value, so callers passing a real status will
    always match the first applicable rule.
    """
    # Removed / new — pure presence checks first.
    if current_status == "absent":
        return "removed_test"
    if baseline_status == "absent":
        # New test, but if it's a fail we still want to surface it in
        # new_failures[] so the operator sees it. We special-case this in
        # build_diff itself; the transition label stays "new_test" to
        # distinguish from a regression on a previously-passing test.
        return "new_test"
    # Flapping: documented_unsafe transitions matter (AC11).
    if (
        baseline_status == "documented_unsafe" and current_status in _FAILURE_STATUSES
    ) or (
        current_status == "documented_unsafe" and baseline_status in _FAILURE_STATUSES
    ):
        return "flapping"
    # Regressions / resolutions on existing tests.
    if baseline_status not in _FAILURE_STATUSES and current_status in _FAILURE_STATUSES:
        return "new_failure"
    if baseline_status in _FAILURE_STATUSES and current_status in _PASS_STATUSES:
        return "resolved"
    return "unchanged"


def _empty_diff_payload() -> dict:
    """The shape returned when no baseline exists (AC15).

    Lists are kept as empty lists (not None) so downstream consumers can
    safely call `len(...)` and iterate without per-key None-checks. The
    `summary` block carries `has_baseline = False` plus the promotable
    note the renderer turns into a hint line.
    """
    return {
        "baseline_run_id": None,
        "baseline_finished_at": None,
        "new_failures": [],
        "resolved": [],
        "new_tests": [],
        "removed_tests": [],
        "flapping": [],
        "summary": {
            "new_failure_count": 0,
            "resolved_count": 0,
            "net_change": 0,
            "has_baseline": False,
            "promotable_note": _NO_BASELINE_NOTE,
        },
    }


def _result_to_dict(result) -> dict:
    """Pull the fields we care about off a TestResult-shaped object.

    Accepts either a `coverage.builder.TestResult` dataclass instance OR a
    plain dict (e.g. when callers have already serialized). Returns a dict
    with `test_id`, `status` (string, not enum), `severity`, `target_id`.

    Defensive against missing fields so an out-of-shape input doesn't
    crash the whole diff build — a malformed row simply gets `absent` as
    its status and is ignored.
    """
    # Dict path — used by tests that hand-build the input.
    if isinstance(result, dict):
        status = result.get("status")
        if hasattr(status, "value"):
            status = status.value
        return {
            "test_id": result.get("test_id") or "",
            "status": str(status) if status is not None else "absent",
            "severity": result.get("severity"),
            "target_id": result.get("target_id") or "",
            "target_kind": result.get("target_kind") or "",
            "layer": result.get("layer") or "",
        }
    # Dataclass path — the production caller (report_builder) passes
    # TestResult instances directly.
    status = getattr(result, "status", None)
    if hasattr(status, "value"):
        status = status.value
    return {
        "test_id": getattr(result, "test_id", "") or "",
        "status": str(status) if status is not None else "absent",
        "severity": getattr(result, "severity", None),
        "target_id": getattr(result, "target_id", "") or "",
        "target_kind": getattr(result, "target_kind", "") or "",
        "layer": getattr(result, "layer", "") or "",
    }


# ─────────────────────────── public entry points ───────────────────────────


def load_baseline(baseline_dir: Path) -> dict | None:
    """Read `<baseline_dir>/last-green.json` and return its parsed dict, or
    None if the file is missing.

    Parameters
    ----------
    baseline_dir : Path
        The `.baseline/` directory. We do NOT walk a parent path looking
        for a baseline — only this exact directory is consulted, so a
        per-run override (e.g. for a regression-bisect workflow) is just
        a new directory.

    Returns
    -------
    dict | None
        The parsed JSON dict (with `run_id`, `finished_at`, `tests`), or
        None if the file doesn't exist (AC15 — first-ever run case).

    Raises
    ------
    CorruptBaselineError
        The file exists but is not valid JSON, or lacks the required
        `tests` key. Silently treating a corrupt baseline as "no baseline"
        could mask a real regression — fail loudly instead.
    """
    path = baseline_dir / "last-green.json"
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CorruptBaselineError(
            f"baseline at {path} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise CorruptBaselineError(
            f"baseline at {path} must be a JSON object, got {type(raw).__name__}"
        )
    tests = raw.get("tests")
    if not isinstance(tests, dict):
        raise CorruptBaselineError(
            f"baseline at {path} is missing required 'tests' object"
        )
    return raw


def build_diff(current_results: Iterable, baseline: dict | None) -> dict:
    """Compare the current run's results to the baseline and return the
    `diff_from_last_green` payload.

    Parameters
    ----------
    current_results : Iterable
        The flat list of TestResult-shaped objects from the current run
        (output of `coverage.builder.load_results(run_dir)`). May also be
        an iterable of dicts.
    baseline : dict | None
        The parsed baseline dict (output of `load_baseline(...)`), or None
        if no baseline exists. The AC15 empty-shape is returned for None.

    Returns
    -------
    dict
        Shape matches spec §6.3's `diff_from_last_green`. Always has the
        same top-level keys regardless of whether a baseline exists, so
        downstream consumers can rely on key presence.
    """
    if baseline is None:
        return _empty_diff_payload()

    # Index the current run by test_id. Dropping duplicates would be a
    # bug — the builder already enforces one row per (test_id) when it
    # places results into the matrix, and we want to surface any
    # duplicate here. Last-write-wins is fine: if a test_id legitimately
    # appears twice we want the most recent status anyway.
    current_by_id: dict[str, dict] = {}
    for result in current_results:
        row = _result_to_dict(result)
        if not row["test_id"]:
            # Defensive: skip malformed rows rather than poisoning the
            # diff. The report_builder already validates real results.
            continue
        current_by_id[row["test_id"]] = row

    baseline_tests: dict[str, dict] = baseline.get("tests") or {}

    # Compute the union of test ids, then walk it once. Sorting before
    # walking guarantees deterministic output ordering — the diff section
    # is a forwardable artifact and a churning order would muddle PR
    # reviews of the report.
    all_ids = sorted(set(current_by_id.keys()) | set(baseline_tests.keys()))

    new_failures: list[DiffEntry] = []
    resolved: list[DiffEntry] = []
    new_tests: list[DiffEntry] = []
    removed_tests: list[DiffEntry] = []
    flapping: list[DiffEntry] = []

    for test_id in all_ids:
        current_row = current_by_id.get(test_id)
        baseline_row = baseline_tests.get(test_id)

        current_status = current_row["status"] if current_row else "absent"
        baseline_status = (
            baseline_row.get("status") if isinstance(baseline_row, dict) else "absent"
        ) or "absent"

        transition = _classify(baseline_status, current_status)

        # Severity / target_id sourced from whichever side carries them;
        # current run wins because it's the more recent observation.
        severity = (
            (current_row or {}).get("severity") or (baseline_row or {}).get("severity")
            if isinstance(baseline_row, dict)
            else (current_row or {}).get("severity")
        )
        target_id = (
            (current_row or {}).get("target_id")
            or (
                baseline_row.get("target_id")
                if isinstance(baseline_row, dict)
                else None
            )
            or ""
        )

        entry = DiffEntry(
            test_id=test_id,
            baseline_status=baseline_status,
            current_status=current_status,
            transition=transition,
            severity=severity,
            target_id=target_id,
        )

        if transition == "new_failure":
            new_failures.append(entry)
        elif transition == "resolved":
            resolved.append(entry)
        elif transition == "new_test":
            new_tests.append(entry)
            # A brand-new test that lands as a failure ALSO shows up in
            # new_failures — the operator should see it as a regression
            # surface, not just a coverage-growth note. The transition
            # label stays "new_test" on the entry in new_failures so the
            # renderer can label it accurately.
            if current_status in _FAILURE_STATUSES:
                new_failures.append(entry)
        elif transition == "removed_test":
            removed_tests.append(entry)
        elif transition == "flapping":
            flapping.append(entry)
        # "unchanged" → not emitted, by design.

    new_failure_count = len(new_failures)
    resolved_count = len(resolved)

    return {
        "baseline_run_id": baseline.get("run_id"),
        "baseline_finished_at": baseline.get("finished_at"),
        "new_failures": [asdict(e) for e in new_failures],
        "resolved": [asdict(e) for e in resolved],
        "new_tests": [asdict(e) for e in new_tests],
        "removed_tests": [asdict(e) for e in removed_tests],
        "flapping": [asdict(e) for e in flapping],
        "summary": {
            "new_failure_count": new_failure_count,
            "resolved_count": resolved_count,
            # Positive net_change = regressions outweigh fixes. Negative =
            # net improvement since last green. The operator can read the
            # sign at a glance without parsing the full lists.
            "net_change": new_failure_count - resolved_count,
            "has_baseline": True,
        },
    }


__all__ = [
    "BASELINE_SCHEMA_VERSION",
    "BaselineError",
    "CorruptBaselineError",
    "DiffEntry",
    "build_diff",
    "load_baseline",
]
