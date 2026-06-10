"""scripts/run_all.py — orchestrator. Runs all 4 adversarial layers + builds the polished report.

Usage:
  python -m scripts.run_all                       # full sweep, default budget
  python -m scripts.run_all --layers e2e fuzz     # subset
  python -m scripts.run_all --llm-probes 32       # AC17 + AC19 included
  python -m scripts.run_all --dry-run             # preflight only, no network calls

This is task 25 of the full-app adversarial testing plan. The orchestrator's
job is to assemble all the components built in tasks 1-24 into the single
user-facing entrypoint declared by spec §5.5:

    1. Phase 0 — Setup: load .env, resolve run_id, create run_dir.
    2. Phase 1 — Preflight: manifest drift, pricing drift, cost cap, identity.
       Refuses to start (exit 1) on any failure. AC4 + AC23 + AC25.
    3. Phase 2 — Run layers in parallel (subprocess-bound, threads are fine).
       10-minute global timeout. Kills outstanding subprocesses on overrun.
       AC1 + AC13 + AC24.
    4. Phase 3 — Aggregate: load each layer's results.json + cost.json,
       build the coverage matrix, load the baseline.
    5. Phase 4 — Render: report.json + report.html + summary.md siblings. AC2.
    6. Phase 5 — Exit: 0 green / 1 setup error / 2 fail / 3 timeout.

Exit codes are intentional so a wrapper script can distinguish "the harness
broke" from "the harness ran fine but found a regression."
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ─────────────────────────── harness layout ─────────────────────────────────

_HARNESS_ROOT = Path(__file__).resolve().parent.parent
# Make `src.*` importable when invoked via `python -m scripts.run_all` from
# anywhere — the harness root is two parents up from this file.
if str(_HARNESS_ROOT) not in sys.path:
    sys.path.insert(0, str(_HARNESS_ROOT))

_LAYERS_ALL: tuple[str, ...] = ("e2e", "fuzz", "auth", "llm")

# Default to a CloudFront URL matching CLAUDE.local.md. Operators override via
# env or CLI flag.
_DEFAULT_TARGET_URL = "https://d5u0vv1zl3eqd.cloudfront.net"

# Default global timeout per spec §5.5: 10 minutes wall-clock for the full
# sweep. Includes Phase 2 only — preflight + aggregation are bounded by
# their own (much shorter) operations.
_DEFAULT_TIMEOUT_SECONDS = 600

# Default Bedrock cost cap (USD) per AC3. The .env can override via
# BEDROCK_COST_CAP_USD; the CLI's --cap-usd flag wins over both.
_DEFAULT_COST_CAP_USD = 1.00

# Default LLM probe cap matches the conftest's _DEFAULT_PROBE_CAP. We
# duplicate the value here (rather than import) so an orchestrator run with
# --dry-run doesn't trigger pytest plugin loading.
_DEFAULT_LLM_PROBES = 30

# Harness version. Read from package.json at import time so a single bump in
# the JSON propagates everywhere. Fallback constant for the case where the
# JSON file is unreadable (e.g. test fixtures that monkey-patch the layout).
_PACKAGE_JSON = _HARNESS_ROOT / "package.json"


def _read_harness_version() -> str:
    """Read `version` from tests-adversarial/package.json.

    Falls back to "0.0.0" if the file is missing or malformed — the version
    is informational (it ends up in `metadata.harness_version`) and a missing
    one should not block a run.
    """
    try:
        return json.loads(_PACKAGE_JSON.read_text(encoding="utf-8")).get(
            "version", "0.0.0"
        )
    except (OSError, json.JSONDecodeError):
        return "0.0.0"


_HARNESS_VERSION = _read_harness_version()


# ─────────────────────────── data containers ────────────────────────────────


@dataclass
class LayerOutcome:
    """One layer's subprocess run summary. Populated by `_run_layer`."""

    name: str
    exit_code: int | None  # None on global timeout (subprocess killed)
    duration_seconds: float
    timed_out: bool = False
    error: str | None = None  # set when the subprocess failed to start


@dataclass
class OrchestratorResult:
    """Final result of an orchestrator invocation. Returned from `run(...)`.

    The CLI wrapper translates this into an exit code via `_resolve_exit_code`.
    Exposed as a dataclass (not just an int) so the test suite can assert on
    the report path + per-layer outcomes without re-parsing stdout.
    """

    exit_code: int
    run_dir: Path
    report_html_path: Path | None
    report_json_path: Path | None
    summary_md_path: Path | None
    layer_outcomes: list[LayerOutcome]
    failures: int = 0
    preflight_message: str | None = None


# ─────────────────────────────── helpers ────────────────────────────────────


def _utc_run_id() -> str:
    """Filesystem-safe UTC ISO timestamp. Replaces ':' with '-' so the run
    id can be used as a directory name on every OS the harness might run on
    (including Windows-mounted volumes the operator might forward the
    report from).
    """
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H-%M-%SZ")


def _print_banner(text: str) -> None:
    """Print a section banner to stdout.

    The orchestrator's stdout is the FIRST thing the user sees — these
    banners are the polish surface called out in the task prompt. Plain
    text only (no emoji per CLAUDE.md), 2-space indent for sub-lines.
    """
    print(f"[ARBITER harness] {text}", flush=True)


def _print_check(text: str, ok: bool = True) -> None:
    """Print a checked/unchecked sub-line under the most recent banner.

    Uses Unicode check / cross marks so a polished terminal renders them
    in green/red via the operator's color scheme. ASCII fallback is
    implicit (the marks render as one cell each in every modern terminal).
    """
    mark = "✓" if ok else "✗"  # ✓ / ✗
    print(f"  {mark} {text}", flush=True)


def _print_arrow(text: str) -> None:
    """Print a queued-action line under a banner."""
    print(f"  → {text}", flush=True)  # →


def _load_dotenv() -> None:
    """Load tests-adversarial/.env if present. Silent no-op if missing.

    Uses python-dotenv when available (declared in pyproject.toml). Falls
    back to a stdlib-only parser so a fresh venv without the dep still
    loads at least the simple `KEY=VALUE` lines — the harness should not
    refuse to start because a transitive dep didn't install.
    """
    env_path = _HARNESS_ROOT / ".env"
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv  # type: ignore[import-not-found]

        load_dotenv(env_path, override=False)
        return
    except ImportError:
        pass
    # Fallback: parse simple `KEY=VALUE` lines, ignore comments + blanks.
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


# ─────────────────── preflight: drift, pricing, identity ────────────────────


class PreflightError(RuntimeError):
    """Raised when any preflight gate fails. Carries a single-line message
    that gets printed to stderr and propagated into the orchestrator result.
    """


def _preflight_manifest_drift() -> None:
    """Run scripts.check_manifest_drift.run() and refuse to start on drift.

    This is the AC25 mechanism (no infra writes — the drift check is the
    only way the harness could ever interact with the source tree, and
    even then it's read-only).
    """
    from scripts import check_manifest_drift

    exit_code, stdout, stderr = check_manifest_drift.run()
    if exit_code == 0:
        # Reformat the summary into our standard sub-line shape so it lines
        # up with the rest of the preflight output.
        summary = (stdout or "").strip()
        if summary:
            _print_check(summary)
        return
    raise PreflightError(f"manifest drift detected:\n{(stderr or '').strip()}")


def _preflight_pricing() -> dict[str, dict[str, float]]:
    """Reconcile MODEL_PRICING between the two source files. Raises on drift.

    Returns the reconciled pricing table so the caller can hand it to
    `estimate_cost` without re-loading.
    """
    from src.cost import pricing

    try:
        table = pricing.load_pricing()
    except pricing.PricingDriftError as exc:
        # Truncate the multi-line drift message to a single line for the
        # banner; the operator can re-run with --dry-run to see the full
        # detail via stderr.
        first_line = str(exc).splitlines()[0]
        raise PreflightError(f"pricing drift: {first_line}") from exc
    _print_check(f"Pricing reconciled, {len(table)} models, no drift")
    return table


def _build_layer_budgets(layers: list[str]) -> dict[str, Any]:
    """Construct LayerBudget objects per requested layer.

    Worst-case token budgets are estimated from spec §5:
      - e2e: 4 chat turns × 1500 tokens each (positive /analyst persona).
      - fuzz: zero Bedrock spend (no /chat).
      - auth: 2 chat turns × 1500 tokens (forged-token happy-path edge).
      - llm: 30 probes × 4000 input + 2000 output (curated + generative + cost-DoS).

    The estimator multiplies these directly so an over-estimate just leaves
    cap headroom — there's no downside to padding.
    """
    from src.cost.preflight import LayerBudget

    budgets: dict[str, Any] = {}
    if "e2e" in layers:
        budgets["e2e"] = LayerBudget(
            name="e2e",
            max_input_tokens=4 * 1500,
            max_output_tokens=4 * 1500,
        )
    if "fuzz" in layers:
        budgets["fuzz"] = LayerBudget(
            name="fuzz",
            max_input_tokens=0,
            max_output_tokens=0,
        )
    if "auth" in layers:
        budgets["auth"] = LayerBudget(
            name="auth",
            max_input_tokens=2 * 1500,
            max_output_tokens=2 * 1500,
        )
    if "llm" in layers:
        budgets["llm"] = LayerBudget(
            name="llm",
            max_input_tokens=30 * 4000,
            max_output_tokens=30 * 2000,
        )
    return budgets


def _preflight_cost(
    layers: list[str], cap_usd: float
) -> tuple[float, dict[str, float]]:
    """Enforce the Bedrock cost cap (AC4).

    Returns `(estimated_total_usd, per_layer_usd)` so the caller can echo
    the breakdown into the banner.
    """
    from src.cost.preflight import BudgetExceededError, estimate_cost, preflight

    budgets = _build_layer_budgets(layers)
    if not budgets:
        # No layers selected = zero cost; still echo a check so the operator
        # sees the gate ran.
        _print_check(
            f"Cost preflight: 0 layers selected, estimated $0.0000 ≤ cap ${cap_usd:.4f}"
        )
        return 0.0, {}
    try:
        estimate = estimate_cost(budgets)
        preflight(cap_usd, budgets)
    except BudgetExceededError as exc:
        raise PreflightError(str(exc)) from exc
    _print_check(
        f"Cost preflight: estimated ${estimate.total_usd:.4f} ≤ cap ${cap_usd:.4f} "
        f"(largest: {estimate.largest_layer})"
    )
    return estimate.total_usd, estimate.per_layer


def _preflight_identity() -> None:
    """Fetch all four Cognito identities and confirm DEMO_PASSWORD is set.

    Skipped at the boundary: if COGNITO_USER_POOL_ID or COGNITO_CLIENT_ID is
    unset (which is true on a fresh checkout before .env is filled in), we
    refuse to start with a clear message. AC5.
    """
    from src.identity.cognito_auth import (
        CognitoAuthError,
        MissingPasswordError,
        fetch_all,
    )

    try:
        identities = fetch_all()
    except MissingPasswordError as exc:
        raise PreflightError(f"DEMO_PASSWORD required: {exc}") from exc
    except CognitoAuthError as exc:
        raise PreflightError(f"Cognito auth failed: {exc}") from exc
    _print_check(f"Cognito identities fetched ({len(identities)} personas)")


def _preflight(
    layers: list[str],
    cap_usd: float,
    skip_identity: bool = False,
) -> dict[str, Any]:
    """Run all preflight gates in order. Raises PreflightError on any failure.

    `skip_identity` lets tests run the rest of preflight without hitting
    Cognito. Production callers (the CLI) never set this.

    Returns a dict carrying the gates' useful outputs (estimated cost,
    per-layer breakdown) so the orchestrator can write them into the cost
    block of the final report.
    """
    _print_banner("Phase 1: Preflight")
    _preflight_manifest_drift()
    pricing_table = _preflight_pricing()
    estimated, per_layer = _preflight_cost(layers, cap_usd)
    if not skip_identity:
        _preflight_identity()
    return {
        "pricing_table": pricing_table,
        "estimated_usd": estimated,
        "per_layer_estimated_usd": per_layer,
    }


# ───────────────────────────── layer runner ─────────────────────────────────


def _layer_command(layer: str, llm_probes: int | None) -> list[str]:
    """Subprocess command for a single layer.

    The e2e layer runs the Playwright JS suite via the npm script the prompt
    wired (`test:e2e`). The Python layers run via `python3.13 -m pytest`
    against the layer directory. Using the absolute pytest path (via
    `sys.executable -m pytest`) is safer than relying on the shell PATH —
    a venv with a custom interpreter still works.
    """
    if layer == "e2e":
        # npm script targets Playwright; the package.json wires it up.
        return ["npm", "run", "test:e2e"]
    cmd = [sys.executable, "-m", "pytest", layer + "/"]
    if layer == "llm" and llm_probes is not None:
        cmd += ["--llm-probes", str(llm_probes)]
    return cmd


def _run_layer(
    layer: str,
    run_dir: Path,
    env: dict[str, str],
    llm_probes: int | None,
    layer_timeout_seconds: float,
) -> LayerOutcome:
    """Spawn one layer's subprocess. Captures stdout/stderr to a log file.

    The log goes to `<run_dir>/<layer>/orchestrator.log` (NOT the layer's
    own results.json — that's the layer's own write target). This separation
    means an operator debugging "why did fuzz fail" can read the verbatim
    pytest output without it overlapping with the structured results.

    Returns a LayerOutcome with the exit code + duration. On global timeout
    we kill the subprocess group and mark `timed_out=True`; the caller
    interprets that as "include partial results, exit 3."
    """
    layer_dir = run_dir / layer
    layer_dir.mkdir(parents=True, exist_ok=True)
    log_path = layer_dir / "orchestrator.log"

    cmd = _layer_command(layer, llm_probes)
    started = time.monotonic()
    try:
        with log_path.open("w", encoding="utf-8") as logf:
            logf.write(f"# command: {' '.join(cmd)}\n")
            logf.write(f"# cwd: {_HARNESS_ROOT}\n")
            logf.write(f"# timeout: {layer_timeout_seconds:.1f}s\n\n")
            logf.flush()
            process = subprocess.Popen(
                cmd,
                cwd=str(_HARNESS_ROOT),
                env=env,
                stdout=logf,
                stderr=subprocess.STDOUT,
            )
            try:
                exit_code = process.wait(timeout=layer_timeout_seconds)
            except subprocess.TimeoutExpired:
                # Kill the subprocess so it doesn't keep running after the
                # orchestrator returns. terminate() first (clean SIGTERM)
                # then kill() if it didn't budge.
                process.terminate()
                try:
                    process.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                duration = time.monotonic() - started
                return LayerOutcome(
                    name=layer,
                    exit_code=None,
                    duration_seconds=duration,
                    timed_out=True,
                    error=f"layer '{layer}' exceeded timeout of {layer_timeout_seconds:.1f}s",
                )
        duration = time.monotonic() - started
        return LayerOutcome(
            name=layer,
            exit_code=exit_code,
            duration_seconds=duration,
        )
    except FileNotFoundError as exc:
        duration = time.monotonic() - started
        return LayerOutcome(
            name=layer,
            exit_code=None,
            duration_seconds=duration,
            error=f"could not start subprocess for layer '{layer}': {exc}",
        )


def _run_layers_parallel(
    layers: list[str],
    run_dir: Path,
    env: dict[str, str],
    llm_probes: int | None,
    timeout_seconds: float,
) -> list[LayerOutcome]:
    """Run each requested layer's subprocess in parallel.

    Layers are subprocess-bound, so a ThreadPoolExecutor (one thread per
    layer) is the right shape — no GIL contention because the threads are
    waiting on OS pipes the whole time. Global timeout = wall-clock budget
    for the entire phase 2; per-layer timeout is the same (a single layer
    that hangs the whole budget should still be killed).
    """
    _print_banner(
        f"Phase 2: Running layers in parallel (timeout {timeout_seconds:.0f}s)"
    )
    for layer in layers:
        _print_arrow(f"{layer} → {_layer_command(layer, llm_probes)[0]} ...")

    outcomes: dict[str, LayerOutcome] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(layers)) as pool:
        futures = {
            pool.submit(
                _run_layer,
                layer,
                run_dir,
                env,
                llm_probes,
                timeout_seconds,
            ): layer
            for layer in layers
        }
        # Use wait() with the global timeout. Any future still running at
        # the deadline gets cancelled; its subprocess was already killed by
        # _run_layer's per-layer timeout.
        done, not_done = concurrent.futures.wait(
            futures.keys(), timeout=timeout_seconds + 30.0
        )
        for fut in done:
            layer = futures[fut]
            try:
                outcomes[layer] = fut.result()
            except Exception as exc:  # noqa: BLE001
                outcomes[layer] = LayerOutcome(
                    name=layer,
                    exit_code=None,
                    duration_seconds=0.0,
                    error=f"layer thread raised: {exc}",
                )
        for fut in not_done:
            layer = futures[fut]
            fut.cancel()
            outcomes[layer] = LayerOutcome(
                name=layer,
                exit_code=None,
                duration_seconds=timeout_seconds,
                timed_out=True,
                error="global timeout exceeded",
            )

    # Preserve the caller's layer order in the returned list for stable
    # stdout summaries.
    return [outcomes[layer] for layer in layers]


# ─────────────────────────── aggregation phase ──────────────────────────────


def _load_layer_cost(run_dir: Path, layer: str) -> dict[str, Any]:
    """Read `<run_dir>/<layer>/cost.json` if the layer wrote one.

    Each layer's conftest writes this at session end via the cost-stash
    fixture below. Missing file = layer didn't run (or wasn't on Bedrock)
    — return a zero-shape so the aggregator doesn't need to special-case
    the absence.
    """
    path = run_dir / layer / "cost.json"
    if not path.exists():
        return {"total_usd": 0.0, "per_layer_usd": {}, "probe_counts": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"total_usd": 0.0, "per_layer_usd": {}, "probe_counts": {}}


def _aggregate_cost(run_dir: Path, layers: list[str]) -> dict[str, Any]:
    """Sum every layer's cost.json into a single CostTracker-shaped dict.

    The shape matches `CostTracker.as_dict()` so the report builder
    consumes both sources identically — operators reading the report
    don't see a difference between "cost from a live run" and "cost
    aggregated from per-layer artifacts."
    """
    total_usd = 0.0
    per_layer_usd: dict[str, float] = {}
    probe_counts: dict[str, int] = {}
    for layer in layers:
        layer_cost = _load_layer_cost(run_dir, layer)
        layer_total = float(layer_cost.get("total_usd", 0.0) or 0.0)
        total_usd += layer_total
        per_layer_usd[layer] = layer_total
        for pl_layer, count in (layer_cost.get("probe_counts") or {}).items():
            probe_counts[pl_layer] = probe_counts.get(pl_layer, 0) + int(count or 0)
    return {
        "total_usd": total_usd,
        "per_layer_usd": per_layer_usd,
        "probe_counts": probe_counts,
    }


def _load_manifest() -> dict:
    """Read the manifest from src/coverage/manifest.json."""
    return json.loads(
        (_HARNESS_ROOT / "src" / "coverage" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )


# ───────────────────────────── report building ──────────────────────────────


def _build_and_write_report(
    run_dir: Path,
    manifest: dict,
    cost: dict[str, Any],
    metadata,
    cap_usd: float,
) -> tuple[Path, Path, Path, int]:
    """Build the coverage matrix, then write report.json/html + summary.md.

    Returns `(report_json, report_html, summary_md, failures_count)`.

    The `failures_count` propagates to the orchestrator's exit-code logic
    so a fail-on-any-failure run can flip to exit 2.
    """
    from src.coverage.builder import build_matrix, load_results, matrix_to_json
    from src.reporting.diff import load_baseline
    from src.reporting.renderer import render_html, render_summary
    from src.reporting.report_builder import build_report, serialize_report

    results = load_results(run_dir)
    matrix = build_matrix(manifest, results)
    matrix_json = matrix_to_json(matrix)

    baseline_dir = _HARNESS_ROOT / "test-reports" / ".baseline"
    try:
        baseline = load_baseline(baseline_dir)
    except Exception:  # noqa: BLE001
        # CorruptBaselineError is the canonical failure shape; we tolerate
        # it here because a corrupt baseline shouldn't block the report —
        # the operator will see the issue in the diff section.
        baseline = None

    report = build_report(
        run_dir=run_dir,
        manifest=manifest,
        matrix=matrix_json,
        cost=cost,
        metadata=metadata,
        results=results,
        cap_usd=cap_usd,
        baseline=baseline,
    )

    report_json_path = run_dir / "report.json"
    report_json_path.write_text(serialize_report(report), encoding="utf-8")

    report_html_path = render_html(report, run_dir, manifest=manifest)
    summary_md_path = render_summary(report, run_dir)

    failures = int(report.get("summary", {}).get("failed", 0) or 0)
    return report_json_path, report_html_path, summary_md_path, failures


# ───────────────────────────── exit-code logic ──────────────────────────────


def _resolve_exit_code(
    preflight_error: PreflightError | None,
    layer_outcomes: list[LayerOutcome],
    failures: int,
) -> int:
    """Map the run state to one of the four documented exit codes.

    0 = green (every layer ran and no FAILs landed in the report).
    1 = preflight or setup error (refused to start, no layer ran).
    2 = at least one FAIL row in the report.
    3 = global timeout (at least one layer killed mid-run).

    Precedence: setup error (1) > timeout (3) > fail (2) > green (0). The
    rationale is "tell the operator the most actionable cause first" — a
    timeout means the layer never finished, so the count of FAILs is
    unreliable and should not mask it.
    """
    if preflight_error is not None:
        return 1
    if any(outcome.timed_out for outcome in layer_outcomes):
        return 3
    # Layer exit code != 0 AND no fail recorded in results = the layer
    # itself broke (not a test fail). Treat as failure (exit 2) so the
    # operator notices.
    if any(
        outcome.exit_code not in (None, 0) and outcome.error is None
        for outcome in layer_outcomes
    ):
        # Pytest exits 1 when tests fail; that's not a setup error, it's
        # the AC1 "exits non-zero on fail" case. Bucket it as exit 2.
        return 2
    if failures > 0:
        return 2
    return 0


# ───────────────────────────── public entry point ───────────────────────────


def run(
    layers: list[str] | None = None,
    llm_probes: int = _DEFAULT_LLM_PROBES,
    cap_usd: float = _DEFAULT_COST_CAP_USD,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    target_base_url: str | None = None,
    dry_run: bool = False,
    skip_identity: bool = False,
    run_dir: Path | None = None,
) -> OrchestratorResult:
    """Run the full orchestrator.

    Parameters
    ----------
    layers : list[str] | None
        Subset of e2e/fuzz/auth/llm. Default: all.
    llm_probes : int
        Override for the LLM probe budget (AC16). Passed through to the
        pytest subprocess as `--llm-probes N`.
    cap_usd : float
        Bedrock cost cap (AC4 + AC3). Overrides the .env default.
    timeout_seconds : float
        Global wall-clock budget for Phase 2 (AC1: 10 minutes).
    target_base_url : str | None
        Override for TARGET_BASE_URL. The fuzz/auth/llm conftests read this
        env var; setting it here propagates to every layer's subprocess env.
    dry_run : bool
        Preflight only; exit 0 after the gates pass. AC4 verification mode.
    skip_identity : bool
        Skip the Cognito identity gate. Used by tests that can't reach
        Cognito but want to exercise the rest of preflight.
    run_dir : Path | None
        Override the run directory. Used by tests so the run lands in
        their tmpdir. Production callers leave this None and the
        orchestrator generates a UTC-ISO timestamp directory under
        `test-reports/`.

    Returns
    -------
    OrchestratorResult
        Carries the exit code, the run_dir, the three report sibling
        paths, and the per-layer outcomes.
    """
    _load_dotenv()

    layers = list(layers) if layers else list(_LAYERS_ALL)
    target_base_url = (
        target_base_url
        or os.environ.get("TARGET_BASE_URL", "").strip()
        or _DEFAULT_TARGET_URL
    )

    # Resolve run_id + run_dir. Run_dir is created up-front so each layer's
    # subprocess can find it (the env var is set below) and so a preflight
    # failure still leaves a forwardable directory.
    if run_dir is None:
        run_id = _utc_run_id()
        run_dir = _HARNESS_ROOT / "test-reports" / run_id
    else:
        run_id = run_dir.name
    run_dir.mkdir(parents=True, exist_ok=True)
    for layer in _LAYERS_ALL:
        (run_dir / layer).mkdir(exist_ok=True)
    (run_dir / "artifacts").mkdir(exist_ok=True)

    started_at = datetime.now(timezone.utc)
    start_monotonic = time.monotonic()

    _print_banner(
        f"Run {run_id} → {run_dir} (target={target_base_url}, "
        f"cap=${cap_usd:.2f}, layers={','.join(layers)})"
    )

    # Phase 1: Preflight.
    preflight_error: PreflightError | None = None
    try:
        preflight_payload = _preflight(layers, cap_usd, skip_identity=skip_identity)
    except PreflightError as exc:
        preflight_error = exc
        _print_check(str(exc), ok=False)
        # On preflight failure we still write a minimal report shell so the
        # operator's `ls test-reports/<run_id>/` shows the run happened.
        return OrchestratorResult(
            exit_code=1,
            run_dir=run_dir,
            report_html_path=None,
            report_json_path=None,
            summary_md_path=None,
            layer_outcomes=[],
            preflight_message=str(exc),
        )

    if dry_run:
        _print_banner(
            f"Dry-run complete. Estimated cost ${preflight_payload['estimated_usd']:.4f} "
            f"≤ cap ${cap_usd:.4f}. No layers were run."
        )
        return OrchestratorResult(
            exit_code=0,
            run_dir=run_dir,
            report_html_path=None,
            report_json_path=None,
            summary_md_path=None,
            layer_outcomes=[],
        )

    # Phase 2: Run layers.
    child_env = os.environ.copy()
    child_env["RUN_DIR"] = str(run_dir)
    child_env["TARGET_BASE_URL"] = target_base_url
    # Pass through the cost cap so layers that share preflight (e.g. the
    # llm conftest) see the same value.
    child_env["BEDROCK_COST_CAP_USD"] = str(cap_usd)
    layer_outcomes = _run_layers_parallel(
        layers, run_dir, child_env, llm_probes, timeout_seconds
    )

    for outcome in layer_outcomes:
        if outcome.timed_out:
            _print_check(
                f"{outcome.name}: TIMED OUT after {outcome.duration_seconds:.1f}s",
                ok=False,
            )
        elif outcome.error:
            _print_check(f"{outcome.name}: {outcome.error}", ok=False)
        else:
            ok = outcome.exit_code == 0
            _print_check(
                f"{outcome.name}: exit {outcome.exit_code} in {outcome.duration_seconds:.1f}s",
                ok=ok,
            )

    # Phase 3: Aggregate.
    _print_banner("Phase 3: Aggregating results")
    cost = _aggregate_cost(run_dir, layers)
    manifest = _load_manifest()

    finished_at = datetime.now(timezone.utc)
    duration_seconds = time.monotonic() - start_monotonic

    from src.reporting.report_builder import RunMetadata

    metadata = RunMetadata(
        run_id=run_id,
        target_base_url=target_base_url,
        chat_function_url=os.environ.get("CHAT_FUNCTION_URL"),
        started_at=started_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        finished_at=finished_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        duration_seconds=duration_seconds,
        harness_version=_HARNESS_VERSION,
    )

    # Phase 4: Build the report.
    _print_banner("Phase 4: Building report")
    try:
        report_json, report_html, summary_md, failures = _build_and_write_report(
            run_dir=run_dir,
            manifest=manifest,
            cost=cost,
            metadata=metadata,
            cap_usd=cap_usd,
        )
        _print_check(f"report.html  → {report_html}")
        _print_check(f"report.json  → {report_json}")
        _print_check(f"summary.md   → {summary_md}")
    except Exception as exc:  # noqa: BLE001 - we want to surface ANY report error
        _print_check(f"report build failed: {exc}", ok=False)
        return OrchestratorResult(
            exit_code=1,
            run_dir=run_dir,
            report_html_path=None,
            report_json_path=None,
            summary_md_path=None,
            layer_outcomes=layer_outcomes,
            preflight_message=f"report build failed: {exc}",
        )

    # Phase 5: Exit code.
    exit_code = _resolve_exit_code(preflight_error, layer_outcomes, failures)
    status_label = {0: "PASS", 1: "ERROR", 2: "FAIL", 3: "TIMEOUT"}.get(exit_code, "?")
    _print_banner(
        f"Done. status={status_label} (exit {exit_code}), "
        f"actual_cost=${cost['total_usd']:.4f}, "
        f"duration={duration_seconds:.1f}s, "
        f"failures={failures}"
    )

    return OrchestratorResult(
        exit_code=exit_code,
        run_dir=run_dir,
        report_html_path=report_html,
        report_json_path=report_json,
        summary_md_path=summary_md,
        layer_outcomes=layer_outcomes,
        failures=failures,
    )


# ───────────────────────────────── CLI ──────────────────────────────────────


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_all",
        description=(
            "Orchestrator for the ARBITER full-app adversarial testing harness. "
            "Runs all 4 layers (e2e, fuzz, auth, llm) against the deployed dev "
            "CloudFront and assembles a polished report at "
            "test-reports/<UTC-ISO-timestamp>/."
        ),
    )
    parser.add_argument(
        "--layers",
        nargs="+",
        choices=list(_LAYERS_ALL),
        default=None,
        help="Subset of layers to run (default: all).",
    )
    parser.add_argument(
        "--llm-probes",
        type=int,
        default=_DEFAULT_LLM_PROBES,
        help=f"LLM per-run probe cap (default: {_DEFAULT_LLM_PROBES}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preflight only — no network calls, no layers run, exit 0 on pass.",
    )
    parser.add_argument(
        "--cap-usd",
        type=float,
        default=None,
        help="Override BEDROCK_COST_CAP_USD for this run.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=_DEFAULT_TIMEOUT_SECONDS,
        help=f"Global wall-clock cap for Phase 2 (default: {_DEFAULT_TIMEOUT_SECONDS}s = 10 min).",
    )
    parser.add_argument(
        "--target-url",
        type=str,
        default=None,
        help="Override TARGET_BASE_URL for this run.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _load_dotenv()

    # Cap resolution precedence: CLI flag > env var > default.
    if args.cap_usd is not None:
        cap_usd = args.cap_usd
    else:
        try:
            cap_usd = float(
                os.environ.get("BEDROCK_COST_CAP_USD", _DEFAULT_COST_CAP_USD)
            )
        except ValueError:
            cap_usd = _DEFAULT_COST_CAP_USD

    result = run(
        layers=args.layers,
        llm_probes=args.llm_probes,
        cap_usd=cap_usd,
        timeout_seconds=args.timeout_seconds,
        target_base_url=args.target_url,
        dry_run=args.dry_run,
    )
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
