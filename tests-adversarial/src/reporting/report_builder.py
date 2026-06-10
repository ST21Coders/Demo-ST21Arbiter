"""src/reporting/report_builder.py — assembles the polished report.json dict.

This is task 21 of the full-app adversarial testing plan. The orchestrator
(task 25) calls `build_report(...)` once per run, after every layer has
produced its `results.json` and after the coverage matrix + cost tracker have
been fully populated. The function returns a JSON-ready dict matching the
shape declared in `Documents/full_app_adversarial_testing_spec.md` §6.3.

────────────────────────────────────────────────────────────────────────────
Acceptance criteria this module enforces
────────────────────────────────────────────────────────────────────────────

  AC2 (sibling files): the orchestrator writes the dict this returns to
       `report.json`. The shape includes `metadata`, `coverage`, `cost`,
       `findings`, `summary`, and `diff_from_last_green` — exactly the keys
       the renderer (task 22) and summary builder (task 23) consume.

  AC3 (cost cap): the `cost` block exposes `actual_usd`, `cap_usd`, and
       `under_budget` derived together so a test on the dict can assert
       `actual_usd < cap_usd` and `under_budget is True` in one shot.

  AC20 (evidence-on-disk): every entry in `findings[]` must carry a non-empty
       `evidence_path` that resolves to an existing file under `run_dir`. If
       any FAIL result lacks evidence or points at a missing file,
       `MissingEvidenceError` is raised BEFORE any partial report is built,
       so the orchestrator never writes a broken `report.json`.

────────────────────────────────────────────────────────────────────────────
Inputs
────────────────────────────────────────────────────────────────────────────

  run_dir   : the run's output directory (e.g. test-reports/2026-06-09T…Z/).
              Used to resolve relative `evidence_path` strings into real
              filesystem checks (AC20).
  manifest  : the parsed `src/coverage/manifest.json`. Provides total counts
              (15 pages × 4 personas = 60, 25 routes, 12 tools).
  matrix    : output of `coverage.builder.matrix_to_json(...)`. Already a
              JSON-ready dict with `pages`, `api_routes`, `agent_tools`, and
              `summary`. This module embeds it verbatim under `coverage`.
  cost      : output of `cost.tracker.CostTracker.as_dict(...)`. Shape:
              `{total_usd: float, per_layer_usd: {layer: float},
                probe_counts: {layer: int}}`.
  metadata  : a `RunMetadata` dataclass the orchestrator constructs once
              with run timestamps + target URLs + harness version.

The function does NOT read `results.json` files itself — the coverage builder
already did that. The orchestrator passes both the raw `results` list and
the built matrix so this module can ground findings in actual TestResult
objects (rather than re-parsing matrix cells, which would lose evidence_path
and severity).

────────────────────────────────────────────────────────────────────────────
Findings ranking
────────────────────────────────────────────────────────────────────────────

`findings[]` is ranked by severity tier (critical > high > medium > low >
info), then by `target_id` alphabetically for deterministic ties. Stable
ordering is required for the diff-from-last-green section (task 24) to be
meaningful across runs.

Severity strings beyond the five known tiers are sorted to the end (after
"info") in their lexical order, so an unexpected severity is visible in the
report but doesn't crash the builder.

DOCUMENTED_UNSAFE results are NOT included in `findings[]` (per AC11 — they
confirm a known contract). They ARE counted in `summary.documented_unsafe`
so the operator can see at a glance how many AC11-style probes ran.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from src.coverage.builder import CellStatus, TestResult
from src.reporting.diff import build_diff


# ──────────────────────────── public exceptions ────────────────────────────


class ReportBuilderError(RuntimeError):
    """Base for all errors raised from this module."""


class MissingEvidenceError(ReportBuilderError):
    """A FAIL result has no evidence_path, or its evidence_path doesn't
    resolve to an existing file under the run directory (violates AC20).
    """


# ───────────────────────────── data model ──────────────────────────────────


@dataclass(frozen=True)
class RunMetadata:
    """Per-run metadata supplied by the orchestrator.

    Held frozen so a single instance can be reused safely if the orchestrator
    builds the report twice (e.g. for a dry-run preview followed by the real
    write). The values mirror spec §6.3's `metadata` block.
    """

    run_id: str  # UTC ISO timestamp string (e.g. "2026-06-09T14-23-01Z")
    target_base_url: str  # the CloudFront URL under test
    chat_function_url: (
        str | None
    )  # Lambda Function URL for /chat (None if not configured)
    started_at: str  # ISO timestamp
    finished_at: str  # ISO timestamp
    duration_seconds: float
    harness_version: str  # from tests-adversarial/package.json or a constant


# ───────────────────────────── constants ───────────────────────────────────


# Schema version of the report.json shape this module emits. Bump on
# backwards-incompatible changes so downstream consumers (renderer, diff
# builder, future CI integrations) can fail fast on unknown versions rather
# than silently misinterpret a new shape.
SCHEMA_VERSION = "1.0.0"

# Default Bedrock cost cap (USD) per AC3. The orchestrator may override via
# `BEDROCK_COST_CAP_USD` env var but the report still records the cap that
# was actually in force for this run, so a forwarded report shows what the
# cap was at run time.
DEFAULT_COST_CAP_USD = 1.00

# Severity tier ranking. Lower index = more severe = sorted earlier in
# `findings[]`. Anything outside this set sorts after "info" (see
# `_severity_rank`).
_SEVERITY_TIERS: tuple[str, ...] = ("critical", "high", "medium", "low", "info")


# ─────────────────────────── helpers ───────────────────────────────────────


def _severity_rank(severity: str | None) -> tuple[int, str]:
    """Sort key for findings ranking.

    Returns `(tier_index, severity_label)` so:
      - critical sorts before high sorts before medium ... sorts before info
      - an unrecognized severity (or None) sorts after all known tiers, in
        lexical order of the label itself ("" for None) — so it's visible in
        the report rather than silently swallowed.
    """
    label = severity or ""
    try:
        return (_SEVERITY_TIERS.index(label), label)
    except ValueError:
        # Unknown severity — sort after every known tier, then alphabetically.
        return (len(_SEVERITY_TIERS), label)


def _check_evidence_exists(run_dir: Path, evidence_path: str, test_id: str) -> str:
    """Resolve `evidence_path` under `run_dir` and confirm the file exists.

    Returns the original `evidence_path` string (the caller stores the
    relative form in the report so a forwarded run directory is portable).

    Raises MissingEvidenceError if:
      - `evidence_path` is None or empty (defensive — builder.py already
        validates this for FAIL rows, but we re-check here so a downstream
        caller that bypasses build_matrix still gets caught), OR
      - the resolved path doesn't exist on disk under `run_dir`.

    We use `(run_dir / evidence_path).resolve()` so a relative path like
    `e2e/screenshots/foo.png` is anchored to the run dir, while an already-
    absolute path is left alone. Either way the existence check is the
    same.
    """
    if not evidence_path:
        raise MissingEvidenceError(
            f"finding '{test_id}' has no evidence_path — AC20 requires every "
            f"failure to point at an on-disk artifact under {run_dir}."
        )
    candidate = Path(evidence_path)
    resolved = candidate if candidate.is_absolute() else (run_dir / candidate)
    if not resolved.is_file():
        raise MissingEvidenceError(
            f"finding '{test_id}' references evidence_path '{evidence_path}' "
            f"which does not resolve to an existing file under {run_dir} "
            f"(resolved to {resolved})."
        )
    return evidence_path


def _summary_short_text(result: TestResult) -> str:
    """One-line human summary for `findings[].summary`.

    Pulled from the result's `skipped_reason` (if any — though skipped
    results don't reach findings), then falls back to a synthesized
    "<layer> failure on <kind>:<id>" string. The orchestrator (task 25) is
    free to enrich this later by re-parsing transcripts; for now the test
    id + target are the most useful one-liner because they are stable
    across runs and grep-friendly.
    """
    return (
        f"{result.layer} {result.status.value} on {result.target_kind} "
        f"'{result.target_id}'"
    )


# ─────────────────────── section builders (private) ────────────────────────


def _build_metadata_block(metadata: RunMetadata) -> dict:
    """The top-level `metadata` block — see spec §6.3.

    Built as an ordinary dict literal so insertion order matches the spec
    schema, and `json.dumps(..., sort_keys=False)` gives the spec's order.
    """
    return {
        "run_id": metadata.run_id,
        "target_base_url": metadata.target_base_url,
        "chat_function_url": metadata.chat_function_url,
        "started_at": metadata.started_at,
        "finished_at": metadata.finished_at,
        "duration_seconds": metadata.duration_seconds,
        "harness_version": metadata.harness_version,
    }


def _build_cost_block(cost: dict, cap_usd: float) -> dict:
    """The top-level `cost` block.

    Inputs come from `CostTracker.as_dict()` which exposes `total_usd`,
    `per_layer_usd`, and `probe_counts`. We rename `total_usd` -> `actual_usd`
    in the report shape because the report distinguishes a planned cap from
    the observed spend; sticking to the tracker's internal name would be
    confusing in the forwarded report.

    `under_budget` is a derived bool: True iff actual < cap. The strict-less
    comparison is deliberate — equal-to-cap means the cap is exhausted, and
    the AC3 phrasing "under 1.00" excludes equality.
    """
    actual = float(cost.get("total_usd", 0.0))
    return {
        "cap_usd": float(cap_usd),
        "actual_usd": actual,
        "per_layer_usd": dict(cost.get("per_layer_usd", {})),
        "probe_counts": dict(cost.get("probe_counts", {})),
        "under_budget": actual < float(cap_usd),
    }


def _extract_findings(run_dir: Path, results: Iterable[TestResult]) -> list[dict]:
    """Walk the raw results list, pull out every FAIL, and produce one
    finding dict per FAIL. Each finding is validated for on-disk evidence
    (AC20) BEFORE the list is returned, so a missing artifact short-circuits
    the whole build.

    DOCUMENTED_UNSAFE is NOT included — those confirm a documented contract,
    not a regression (per AC11). They're tallied in the summary instead.

    The returned list is then ranked by `_severity_rank` and target_id.
    """
    findings: list[dict] = []
    for result in results:
        if result.status != CellStatus.FAIL:
            continue
        # AC20 enforcement: every FAIL must have an existing evidence file.
        # builder.py validates evidence_path is set, but it does NOT check
        # that the file exists — that's this module's job because only the
        # report builder knows about `run_dir`.
        evidence_path = _check_evidence_exists(
            run_dir, result.evidence_path or "", result.test_id
        )
        findings.append(
            {
                "test_id": result.test_id,
                "severity": result.severity,
                "layer": result.layer,
                "target_kind": result.target_kind,
                "target_id": result.target_id,
                "summary": _summary_short_text(result),
                "evidence_path": evidence_path,
            }
        )
    # Rank: severity tier ascending (critical first), then target_id
    # alphabetically for stable tie-breaking across runs (so the
    # diff-from-last-green block in task 24 doesn't churn on ties).
    findings.sort(
        key=lambda f: (_severity_rank(f["severity"]), f["target_id"], f["test_id"])
    )
    return findings


def _build_summary_block(
    matrix: dict,
    results: Iterable[TestResult],
    findings: list[dict],
) -> dict:
    """The top-level `summary` block — counts the operator skims first.

    Pulls totals from the matrix's `summary` sub-dict (which the coverage
    builder already computed) so we don't double-count. Adds aggregate
    pass/fail/skip/documented_unsafe counts across every TestResult, plus a
    `failures_by_severity` breakdown driven from the ranked findings list.
    """
    results = list(results)
    matrix_summary = matrix.get("summary", {})

    # Aggregate pass/fail/skipped/documented_unsafe across every result.
    passed = sum(1 for r in results if r.status == CellStatus.PASS)
    failed = sum(1 for r in results if r.status == CellStatus.FAIL)
    skipped = sum(1 for r in results if r.status == CellStatus.SKIPPED)
    documented_unsafe = sum(
        1 for r in results if r.status == CellStatus.DOCUMENTED_UNSAFE
    )
    # TOOL_INVOKED / PROMPT_ONLY are tool-coverage signals, not pass/fail —
    # but the operator wants a single "what ran today" count, so they're
    # rolled into `total_tests_run` alongside passes and failures.
    total = sum(
        1
        for r in results
        if r.status
        in {
            CellStatus.PASS,
            CellStatus.FAIL,
            CellStatus.SKIPPED,
            CellStatus.DOCUMENTED_UNSAFE,
            CellStatus.TOOL_INVOKED,
            CellStatus.PROMPT_ONLY,
        }
    )

    # Failure counts by severity tier — only the five known labels appear in
    # the dict so a forwarded report has a consistent shape. Unknown
    # severities are folded into "info" so they're still visible somewhere.
    failures_by_severity: dict[str, int] = {tier: 0 for tier in _SEVERITY_TIERS}
    for finding in findings:
        sev = finding.get("severity") or "info"
        if sev not in failures_by_severity:
            sev = "info"
        failures_by_severity[sev] += 1

    return {
        "total_tests_run": total,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "documented_unsafe": documented_unsafe,
        "pages_covered_label": matrix_summary.get("pages_covered_label", "0/0"),
        "routes_covered_label": matrix_summary.get("routes_covered_label", "0/0"),
        "tools_covered_label": matrix_summary.get("tools_covered_label", "0/0"),
        "failures_by_severity": failures_by_severity,
    }


# ─────────────────────────── public entry point ────────────────────────────


def build_report(
    run_dir: Path,
    manifest: dict,
    matrix: dict,
    cost: dict,
    metadata: RunMetadata,
    results: Iterable[TestResult] | None = None,
    cap_usd: float = DEFAULT_COST_CAP_USD,
    baseline: dict | None = None,
) -> dict:
    """Assemble the report.json dict from layer outputs + manifest + cost.

    Parameters
    ----------
    run_dir : Path
        The run's output directory. Used to verify every `evidence_path`
        resolves to an existing file (AC20).
    manifest : dict
        The parsed `src/coverage/manifest.json`. Reserved for future use
        (e.g. embedding manifest version into the report); currently the
        matrix already carries all the structure the renderer needs, so
        this parameter is accepted but not unpacked. Keeping it in the
        signature now means task 25 can wire it without an API change.
    matrix : dict
        Output of `coverage.builder.matrix_to_json(...)`. Embedded verbatim
        under `coverage`.
    cost : dict
        Output of `cost.tracker.CostTracker.as_dict(...)`.
    metadata : RunMetadata
        Per-run timestamps + URLs + harness version, supplied by the
        orchestrator.
    results : Iterable[TestResult], optional
        The flat list of TestResult objects (output of
        `coverage.builder.load_results(run_dir)`). Used to extract findings
        with their evidence paths and severities, and to drive the summary
        counts. If None, the report has no findings and zeroed summary
        counts — the "empty run" case (no layer ran yet) is a valid input.
    cap_usd : float
        The Bedrock cost cap in force for this run. Defaults to
        `DEFAULT_COST_CAP_USD` ($1.00) per AC3.
    baseline : dict | None, optional
        The parsed last-green baseline dict (output of
        `reporting.diff.load_baseline(...)`). If None, the diff section
        renders as the AC15 "no baseline; this run will be promotable"
        empty-shape. If supplied, the diff section is computed against
        the baseline (AC14).

    Returns
    -------
    dict
        JSON-ready dict matching spec §6.3. Caller is responsible for
        `json.dumps(..., indent=2, sort_keys=False)` and writing it to
        `report.json`. Keeping write separate from build lets the
        orchestrator dry-run the schema before committing to disk.

    Raises
    ------
    MissingEvidenceError
        A FAIL result has no `evidence_path`, or the path doesn't resolve
        to an existing file under `run_dir`. AC20.
    """
    results_list = list(results) if results is not None else []

    # Extract findings FIRST — if AC20 is violated we want to fail before
    # building the rest of the dict, so a broken report.json never lands
    # on disk.
    findings = _extract_findings(run_dir, results_list)

    # Build the diff-from-last-green block (task 24). `build_diff` handles
    # both the with-baseline (AC14) and without-baseline (AC15) cases —
    # passing `baseline=None` returns the empty-shape with `has_baseline:
    # False`, so downstream consumers always get the same key shape.
    diff_block = build_diff(results_list, baseline)

    return {
        "schema_version": SCHEMA_VERSION,
        "metadata": _build_metadata_block(metadata),
        "coverage": matrix,
        "cost": _build_cost_block(cost, cap_usd),
        "findings": findings,
        "summary": _build_summary_block(matrix, results_list, findings),
        "diff_from_last_green": diff_block,
    }


def serialize_report(report: dict) -> str:
    """Serialize a report dict to a deterministic JSON string.

    Uses `sort_keys=False` so insertion order is preserved — `build_report`
    constructs the dict in spec order, and we want the on-disk file to
    match that order for forwardability + grep-ability.

    Indentation is fixed at 2 spaces (matches the project's other JSON
    files, e.g. `Infra/params/dev.json`). Trailing newline appended so the
    file is POSIX-clean.
    """
    return json.dumps(report, indent=2, sort_keys=False) + "\n"


__all__ = [
    "SCHEMA_VERSION",
    "DEFAULT_COST_CAP_USD",
    "RunMetadata",
    "MissingEvidenceError",
    "ReportBuilderError",
    "build_report",
    "serialize_report",
]
