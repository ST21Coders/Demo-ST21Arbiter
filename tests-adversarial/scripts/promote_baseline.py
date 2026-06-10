"""scripts/promote_baseline.py — copy a green run's per-test-id status to
`.baseline/last-green.json`.

Task 24 of the full-app adversarial testing plan. Manual promotion is
intentional (per spec §7.1): "the harness does not auto-promote, so a
flake doesn't overwrite a known-good baseline." The user runs this only
after eyeballing a report and deciding it represents a stable surface.

Usage:
    python -m scripts.promote_baseline                 # most recent run
    python -m scripts.promote_baseline <run_dir>       # explicit dir
    python -m scripts.promote_baseline --reports-dir <test-reports/>

Exit codes:
    0 — promoted successfully
    1 — error (no run found, missing report.json, IO failure)
    2 — refused: run is not green (has failures)

The 0/1/2 split lets a wrapper script distinguish "harness broken" from
"the run wasn't promotable", which matters when the orchestrator chains
multiple actions.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

# Schema version of the baseline file. Bumping here MUST go in lockstep
# with `src/reporting/diff.BASELINE_SCHEMA_VERSION` — the diff loader
# rejects unknown versions.
BASELINE_SCHEMA_VERSION = "1.0.0"

# Default location of the test-reports tree, relative to the harness root.
# Resolved against the directory THIS script lives in so callers don't
# need to be in the right cwd.
_HARNESS_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_REPORTS_DIR = _HARNESS_ROOT / "test-reports"

# UTC-ISO timestamp shape produced by `run_all.py` (e.g.
# `2026-06-09T14-23-01Z`). We accept the colon-form too because a future
# refactor might switch to it.
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}[:-]\d{2}[:-]\d{2}Z?$")


# ─────────────────────────────── helpers ───────────────────────────────────


def _find_most_recent_run(reports_dir: Path) -> Path | None:
    """Pick the most recent timestamped run directory under `reports_dir`.

    Skips `.baseline/` and any non-timestamp-shaped name so a stray dir
    doesn't accidentally get promoted. Returns None if no run dir exists.
    """
    if not reports_dir.is_dir():
        return None
    candidates = [
        child
        for child in reports_dir.iterdir()
        if child.is_dir()
        and not child.name.startswith(".")
        and _TIMESTAMP_RE.match(child.name)
    ]
    if not candidates:
        return None
    # Lexical sort works because the timestamps are ISO-8601 — the
    # alphabetic order IS the chronological order.
    candidates.sort(key=lambda p: p.name)
    return candidates[-1]


def _load_report_json(run_dir: Path) -> dict:
    """Read `<run_dir>/report.json` or raise FileNotFoundError.

    We don't try to be helpful about partial reads — a missing or
    corrupt report.json means the run never finished successfully, which
    is a strong signal the run is NOT promotable. Caller surfaces the
    error to the operator.
    """
    path = run_dir / "report.json"
    if not path.exists():
        raise FileNotFoundError(
            f"no report.json under {run_dir} — was the run completed?"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _is_green(report: dict) -> bool:
    """A run is green iff its summary shows zero failures.

    The `summary` block exposes both `failed` (the aggregate test count)
    and, in some shapes, `failures` (the legacy matrix-builder key).
    Either being non-zero is grounds to refuse. The check is intentionally
    strict: we DON'T forgive DOCUMENTED_UNSAFE here because it's already
    counted separately and a green run can carry documented-unsafe rows.
    """
    summary = report.get("summary") or {}
    failed = int(summary.get("failed", 0) or 0)
    legacy_failures = int(summary.get("failures", 0) or 0)
    return failed == 0 and legacy_failures == 0


def _iter_result_rows(run_dir: Path) -> Iterable[dict]:
    """Yield every per-test-id row from the run's layer results.json files.

    Walks the four canonical layers (e2e/fuzz/auth/llm) in order. Missing
    layer = layer didn't run; not an error. Malformed rows are skipped
    with a warning to stderr rather than aborting — promotion should not
    fail because one layer wrote a slightly-wrong row, but the operator
    deserves to see the warning.
    """
    for layer in ("e2e", "fuzz", "auth", "llm"):
        path = run_dir / layer / "results.json"
        if not path.exists():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(
                f"warning: {path} is not valid JSON ({exc}); skipping",
                file=sys.stderr,
            )
            continue
        if not isinstance(raw, list):
            print(
                f"warning: {path} should be a JSON list, got "
                f"{type(raw).__name__}; skipping",
                file=sys.stderr,
            )
            continue
        for idx, row in enumerate(raw):
            if not isinstance(row, dict) or "test_id" not in row:
                print(
                    f"warning: {path}[{idx}] missing test_id; skipping",
                    file=sys.stderr,
                )
                continue
            # Inject the layer if the row didn't carry one — matches the
            # default-layer behavior in coverage.builder.load_results.
            row.setdefault("layer", layer)
            yield row


def _build_baseline_dict(run_dir: Path, report: dict) -> tuple[dict, int]:
    """Build the baseline JSON dict from a run's results.

    Returns (baseline_dict, test_count). The dict shape is the one
    documented in `src/reporting/diff.py`'s module docstring: an outer
    `run_id` + `finished_at` + `tests` map of test_id → status fields.

    The `finished_at` falls back to "now" only if the report metadata
    didn't carry one — which would be unusual (the report builder always
    sets it) but keeps the baseline file forwardable even from a hand-
    edited report.
    """
    metadata = report.get("metadata") or {}
    run_id = (
        metadata.get("run_id")
        or run_dir.name  # the directory name IS the run id by convention
    )
    finished_at = metadata.get("finished_at") or datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    tests: dict[str, dict] = {}
    for row in _iter_result_rows(run_dir):
        test_id = row["test_id"]
        # Last-write-wins on duplicate test_id — same semantics as
        # `coverage.builder._place_result`. In practice no layer emits
        # duplicates; documenting the rule here so it's not a surprise.
        tests[test_id] = {
            "status": row.get("status"),
            "severity": row.get("severity"),
            "target_id": row.get("target_id"),
            "target_kind": row.get("target_kind"),
            "layer": row.get("layer"),
        }

    return {
        "schema_version": BASELINE_SCHEMA_VERSION,
        "run_id": run_id,
        "finished_at": finished_at,
        "tests": tests,
    }, len(tests)


def _write_baseline(reports_dir: Path, baseline: dict) -> Path:
    """Write `<reports_dir>/.baseline/last-green.json` atomically.

    Atomicity: write to a sibling `.last-green.json.tmp` then rename. A
    crash mid-write would otherwise leave the diff loader with a partial
    JSON file and trigger CorruptBaselineError on the next run.
    """
    baseline_dir = reports_dir / ".baseline"
    baseline_dir.mkdir(parents=True, exist_ok=True)
    target = baseline_dir / "last-green.json"
    tmp = baseline_dir / ".last-green.json.tmp"
    tmp.write_text(
        json.dumps(baseline, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    tmp.replace(target)
    return target


# ─────────────────────────────── entry point ───────────────────────────────


def main(
    run_dir: Path | None = None,
    reports_dir: Path | None = None,
) -> int:
    """Promote a run to the baseline. Returns an exit code.

    Parameters
    ----------
    run_dir : Path | None
        Specific run directory to promote. If None, the most recent run
        under `reports_dir` is used.
    reports_dir : Path | None
        Root of the test-reports tree. Defaults to
        `<harness_root>/test-reports/`.

    Returns
    -------
    int
        0 — promoted successfully.
        1 — error (missing run / report.json / IO failure).
        2 — refused (run was not green).
    """
    reports_dir = reports_dir or _DEFAULT_REPORTS_DIR
    if not reports_dir.is_dir():
        print(
            f"error: reports directory does not exist: {reports_dir}",
            file=sys.stderr,
        )
        return 1

    if run_dir is None:
        run_dir = _find_most_recent_run(reports_dir)
        if run_dir is None:
            print(
                f"error: no run directory found under {reports_dir}",
                file=sys.stderr,
            )
            return 1

    if not run_dir.is_dir():
        print(
            f"error: run directory does not exist: {run_dir}",
            file=sys.stderr,
        )
        return 1

    try:
        report = _load_report_json(run_dir)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(
            f"error: report.json under {run_dir} is not valid JSON: {exc}",
            file=sys.stderr,
        )
        return 1

    if not _is_green(report):
        # Exit 2 (NOT 1) so the orchestrator can distinguish this case.
        summary = report.get("summary") or {}
        failed = summary.get("failed", summary.get("failures", "?"))
        print(
            f"refused: cannot promote a non-green run (failed={failed}) — "
            f"{run_dir.name}",
            file=sys.stderr,
        )
        return 2

    baseline, test_count = _build_baseline_dict(run_dir, report)
    target = _write_baseline(reports_dir, baseline)

    # Final confirmation to stdout — the prompt explicitly wants this
    # message, both for human operators and for any wrapper script that
    # greps the output.
    print(
        f"promoted run {baseline['run_id']} to baseline "
        f"({test_count} tests pinned) → {target}"
    )
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Promote a green harness run to .baseline/last-green.json "
            "for next-run diff."
        ),
    )
    parser.add_argument(
        "run_dir",
        nargs="?",
        type=Path,
        default=None,
        help="Run directory to promote (default: most recent under reports-dir).",
    )
    parser.add_argument(
        "--reports-dir",
        type=Path,
        default=None,
        help=f"test-reports root (default: {_DEFAULT_REPORTS_DIR}).",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":  # pragma: no cover — exercised via subprocess
    args = _parse_args()
    sys.exit(main(run_dir=args.run_dir, reports_dir=args.reports_dir))
