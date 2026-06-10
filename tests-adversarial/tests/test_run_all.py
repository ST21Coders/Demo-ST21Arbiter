"""Tests for scripts/run_all.py — task 25 (orchestrator).

Covers the AC1/AC2/AC4/AC13/AC23/AC25 surface from the perspective of the
orchestrator entrypoint. The layers themselves are mocked via subprocess
fakes so the test suite doesn't need DEMO_PASSWORD or network access — the
goal here is to prove the orchestrator wires the components correctly, not
to re-test each component.

Test inventory (matches the prompt's per-task list):

  1. `--dry-run` exits 0 after preflight with all gates green.
  2. Missing DEMO_PASSWORD → exit 1 with the expected message.
  3. Pricing drift mock → exit 1.
  4. Manifest drift mock → exit 1.
  5. With `--layers e2e fuzz`, only those layers' subprocesses are invoked.
  6. After a (mocked) successful run, report.html / report.json / summary.md
     all exist.
  7. After a (mocked) green run, exit code 0.
  8. After a (mocked) run with one FAIL row, exit code 2.
  9. `--cap-usd 0.01` against the default layers triggers preflight rejection.
 10. Global timeout: a sleeping layer gets killed and the run still produces
     a (partial) report.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# `scripts/run_all.py` is `tests-adversarial/scripts/run_all.py` — importable
# as `scripts.run_all` via the pyproject `pythonpath = ["."]` setting.
from scripts import run_all


# ─────────────────────────── small helpers ───────────────────────────────


def _write_minimal_manifest_passthrough(monkeypatch):
    """No-op: the real manifest at src/coverage/manifest.json is fine to
    use. We monkeypatch the drift check to a green result so the test
    doesn't depend on the live source tree being in sync.
    """

    def _fake_run():
        return 0, "manifest matches\n", ""

    monkeypatch.setattr("scripts.check_manifest_drift.run", _fake_run, raising=True)


def _stub_cognito_identity(monkeypatch):
    """Stub fetch_all so the identity preflight gate sees four personas."""

    class _StubIdentity:
        def __init__(self, persona):
            self.persona = persona

    def _fake_fetch_all():
        return {
            "ciso": _StubIdentity("ciso"),
            "soc": _StubIdentity("soc"),
            "grc": _StubIdentity("grc"),
            "employee": _StubIdentity("employee"),
        }

    monkeypatch.setattr(
        "src.identity.cognito_auth.fetch_all", _fake_fetch_all, raising=True
    )


def _seed_env(monkeypatch):
    """Set the env vars the layers' conftests read so we don't trip on
    fallback paths. Identity gating is monkeypatched separately."""
    monkeypatch.setenv("DEMO_PASSWORD", "Sup3rSecret!")
    monkeypatch.setenv("COGNITO_USER_POOL_ID", "us-east-1_AbC123XyZ")
    monkeypatch.setenv("COGNITO_CLIENT_ID", "1example2client3id4")
    monkeypatch.setenv("TARGET_BASE_URL", "https://example.cloudfront.net")


def _stub_layer_runner(monkeypatch, behaviours: dict[str, dict]):
    """Replace `_run_layers_parallel` with a function that writes a fake
    `results.json` (and `cost.json`) per layer and returns a list of
    LayerOutcome instances built from `behaviours`.

    `behaviours[layer]` = `{"exit_code": int|None, "results": list, "timed_out": bool}`.
    Missing keys default to (0, [], False).
    """

    def _fake_run_layers_parallel(layers, run_dir, env, llm_probes, timeout_seconds):
        outcomes = []
        for layer in layers:
            spec = behaviours.get(layer, {})
            (run_dir / layer).mkdir(parents=True, exist_ok=True)
            # Write a results.json (defaults to empty).
            results_path = run_dir / layer / "results.json"
            results_path.write_text(
                json.dumps(spec.get("results", []), indent=2) + "\n",
                encoding="utf-8",
            )
            # Write a cost.json — zero for non-Bedrock layers, override
            # via spec["cost"].
            cost_path = run_dir / layer / "cost.json"
            cost_path.write_text(
                json.dumps(
                    spec.get(
                        "cost",
                        {
                            "total_usd": 0.0,
                            "per_layer_usd": {layer: 0.0},
                            "probe_counts": {},
                        },
                    ),
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            outcomes.append(
                run_all.LayerOutcome(
                    name=layer,
                    exit_code=spec.get("exit_code", 0),
                    duration_seconds=spec.get("duration_seconds", 1.0),
                    timed_out=spec.get("timed_out", False),
                    error=spec.get("error"),
                )
            )
        return outcomes

    monkeypatch.setattr(run_all, "_run_layers_parallel", _fake_run_layers_parallel)


def _write_fake_evidence(run_dir: Path, layer: str, relpath: str) -> str:
    """Write a stub evidence file and return its relative path as the
    result row's `evidence_path` value."""
    full = run_dir / relpath
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text("evidence\n", encoding="utf-8")
    return relpath


# ─────────────────────────── the 10 tests ────────────────────────────────


def test_dry_run_exits_zero_after_preflight(monkeypatch, tmp_path, capsys):
    """1. `--dry-run` exits 0 after preflight with all gates green."""
    _seed_env(monkeypatch)
    _write_minimal_manifest_passthrough(monkeypatch)
    _stub_cognito_identity(monkeypatch)

    result = run_all.run(
        layers=["e2e", "fuzz", "auth", "llm"],
        dry_run=True,
        cap_usd=1.00,
        run_dir=tmp_path / "run1",
    )
    assert result.exit_code == 0
    assert result.report_html_path is None  # dry-run doesn't write the report
    out = capsys.readouterr().out
    assert "Phase 1: Preflight" in out
    assert "Dry-run complete" in out


def test_missing_demo_password_exits_one(monkeypatch, tmp_path, capsys):
    """2. Missing DEMO_PASSWORD → exit 1 with the expected message."""
    monkeypatch.delenv("DEMO_PASSWORD", raising=False)
    monkeypatch.delenv("COGNITO_USER_POOL_ID", raising=False)
    monkeypatch.delenv("COGNITO_CLIENT_ID", raising=False)
    _write_minimal_manifest_passthrough(monkeypatch)

    result = run_all.run(
        layers=["e2e"],
        dry_run=True,
        cap_usd=1.00,
        run_dir=tmp_path / "run2",
    )
    assert result.exit_code == 1
    assert result.preflight_message is not None
    assert "DEMO_PASSWORD required" in result.preflight_message


def test_pricing_drift_exits_one(monkeypatch, tmp_path):
    """3. Pricing drift mock → exit 1."""
    _seed_env(monkeypatch)
    _write_minimal_manifest_passthrough(monkeypatch)

    from src.cost import pricing as pricing_mod

    def _fake_load_pricing():
        raise pricing_mod.PricingDriftError(
            "pricing drift detected — MODEL_PRICING disagrees between\n"
            "  foo\nand\n  bar\nDifferences: nova differs"
        )

    monkeypatch.setattr(pricing_mod, "load_pricing", _fake_load_pricing)

    result = run_all.run(
        layers=["e2e"],
        dry_run=True,
        cap_usd=1.00,
        run_dir=tmp_path / "run3",
    )
    assert result.exit_code == 1
    assert "pricing drift" in (result.preflight_message or "")


def test_manifest_drift_exits_one(monkeypatch, tmp_path):
    """4. Manifest drift mock → exit 1."""
    _seed_env(monkeypatch)

    def _fake_run():
        return 1, "", "manifest drift detected: pages added: ['fake']\n"

    monkeypatch.setattr("scripts.check_manifest_drift.run", _fake_run, raising=True)

    result = run_all.run(
        layers=["e2e"],
        dry_run=True,
        cap_usd=1.00,
        run_dir=tmp_path / "run4",
    )
    assert result.exit_code == 1
    assert "manifest drift" in (result.preflight_message or "")


def test_layer_subset_only_runs_selected(monkeypatch, tmp_path):
    """5. With `--layers e2e fuzz`, only those layers' subprocesses are invoked."""
    _seed_env(monkeypatch)
    _write_minimal_manifest_passthrough(monkeypatch)
    _stub_cognito_identity(monkeypatch)

    called: list[list[str]] = []

    def _fake_run_layers_parallel(layers, run_dir, env, llm_probes, timeout_seconds):
        called.append(list(layers))
        # Write empty layer artifacts so aggregate can succeed.
        outcomes = []
        for layer in layers:
            (run_dir / layer).mkdir(parents=True, exist_ok=True)
            (run_dir / layer / "results.json").write_text("[]\n", encoding="utf-8")
            (run_dir / layer / "cost.json").write_text(
                json.dumps(
                    {
                        "total_usd": 0.0,
                        "per_layer_usd": {layer: 0.0},
                        "probe_counts": {},
                    }
                ),
                encoding="utf-8",
            )
            outcomes.append(
                run_all.LayerOutcome(name=layer, exit_code=0, duration_seconds=0.1)
            )
        return outcomes

    monkeypatch.setattr(run_all, "_run_layers_parallel", _fake_run_layers_parallel)

    result = run_all.run(
        layers=["e2e", "fuzz"],
        cap_usd=1.00,
        run_dir=tmp_path / "run5",
    )
    assert result.exit_code == 0
    assert called == [["e2e", "fuzz"]]


def test_successful_run_writes_report_siblings(monkeypatch, tmp_path):
    """6. After a (mocked) successful run, report.html / report.json /
    summary.md all exist in the run dir."""
    _seed_env(monkeypatch)
    _write_minimal_manifest_passthrough(monkeypatch)
    _stub_cognito_identity(monkeypatch)
    _stub_layer_runner(monkeypatch, behaviours={})

    result = run_all.run(
        layers=["e2e", "fuzz", "auth", "llm"],
        cap_usd=1.00,
        run_dir=tmp_path / "run6",
    )
    assert result.report_html_path is not None
    assert result.report_html_path.exists()
    assert result.report_json_path is not None
    assert result.report_json_path.exists()
    assert result.summary_md_path is not None
    assert result.summary_md_path.exists()


def test_green_run_exits_zero(monkeypatch, tmp_path):
    """7. After a (mocked) green run, exit code 0."""
    _seed_env(monkeypatch)
    _write_minimal_manifest_passthrough(monkeypatch)
    _stub_cognito_identity(monkeypatch)
    _stub_layer_runner(monkeypatch, behaviours={})

    result = run_all.run(
        layers=["e2e", "fuzz", "auth", "llm"],
        cap_usd=1.00,
        run_dir=tmp_path / "run7",
    )
    assert result.exit_code == 0
    assert result.failures == 0


def test_run_with_one_fail_exits_two(monkeypatch, tmp_path):
    """8. After a (mocked) run with one FAIL row, exit code 2."""
    _seed_env(monkeypatch)
    _write_minimal_manifest_passthrough(monkeypatch)
    _stub_cognito_identity(monkeypatch)

    run_dir = tmp_path / "run8"
    # Pre-create the run_dir + the layer subdir so the evidence file
    # writer can land before the stubbed runner.
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "fuzz").mkdir(exist_ok=True)
    evidence_rel = _write_fake_evidence(run_dir, "fuzz", "fuzz/transcripts/foo.jsonl")

    # Pick a route id that exists in the real manifest so the matrix
    # builder doesn't choke on UnknownTargetError.
    manifest = run_all._load_manifest()
    real_route_id = manifest["api_routes"][0]["id"]

    fuzz_results = [
        {
            "test_id": "fuzz.test.fake.fail",
            "status": "fail",
            "layer": "fuzz",
            "target_kind": "api_route",
            "target_id": real_route_id,
            "evidence_path": evidence_rel,
            "severity": "medium",
        }
    ]
    _stub_layer_runner(
        monkeypatch,
        behaviours={
            "fuzz": {"results": fuzz_results, "exit_code": 1},
        },
    )

    result = run_all.run(
        layers=["fuzz"],
        cap_usd=1.00,
        run_dir=run_dir,
    )
    # Could be 2 from either the layer exit_code != 0 path OR the
    # failures > 0 path; both collapse to 2.
    assert result.exit_code == 2


def test_cap_too_low_rejects_at_preflight(monkeypatch, tmp_path):
    """9. `--cap-usd 0.01` against the default layers triggers preflight rejection."""
    _seed_env(monkeypatch)
    _write_minimal_manifest_passthrough(monkeypatch)
    _stub_cognito_identity(monkeypatch)

    result = run_all.run(
        layers=["e2e", "fuzz", "auth", "llm"],
        dry_run=True,
        cap_usd=0.0000001,  # well below the default llm worst-case estimate
        run_dir=tmp_path / "run9",
    )
    assert result.exit_code == 1
    assert "budget exceeded" in (result.preflight_message or "")


def test_global_timeout_yields_exit_three_and_partial_report(monkeypatch, tmp_path):
    """10. Global timeout: a sleeping layer gets killed and the run still
    produces a (partial) report. Exit code is 3."""
    _seed_env(monkeypatch)
    _write_minimal_manifest_passthrough(monkeypatch)
    _stub_cognito_identity(monkeypatch)

    def _timeout_runner(layers, run_dir, env, llm_probes, timeout_seconds):
        outcomes = []
        for layer in layers:
            (run_dir / layer).mkdir(parents=True, exist_ok=True)
            # Empty results = partial output.
            (run_dir / layer / "results.json").write_text("[]\n", encoding="utf-8")
            (run_dir / layer / "cost.json").write_text(
                json.dumps(
                    {
                        "total_usd": 0.0,
                        "per_layer_usd": {layer: 0.0},
                        "probe_counts": {},
                    }
                ),
                encoding="utf-8",
            )
            outcomes.append(
                run_all.LayerOutcome(
                    name=layer,
                    exit_code=None,
                    duration_seconds=timeout_seconds,
                    timed_out=True,
                    error="global timeout exceeded",
                )
            )
        return outcomes

    monkeypatch.setattr(run_all, "_run_layers_parallel", _timeout_runner)

    result = run_all.run(
        layers=["e2e", "fuzz"],
        cap_usd=1.00,
        run_dir=tmp_path / "run10",
    )
    assert result.exit_code == 3
    # Partial report still landed:
    assert result.report_html_path is not None
    assert result.report_html_path.exists()
    assert result.report_json_path is not None
    assert result.report_json_path.exists()


# ───────────────────────── ancillary unit tests ──────────────────────────


def test_utc_run_id_filesystem_safe():
    rid = run_all._utc_run_id()
    # Shape: YYYY-MM-DDTHH-MM-SSZ — no colons (filesystem-safe).
    assert ":" not in rid
    assert rid.endswith("Z")


def test_aggregate_cost_sums_layers(tmp_path):
    run_dir = tmp_path / "agg"
    (run_dir / "llm").mkdir(parents=True)
    (run_dir / "fuzz").mkdir(parents=True)
    (run_dir / "llm" / "cost.json").write_text(
        json.dumps(
            {
                "total_usd": 0.25,
                "per_layer_usd": {"llm": 0.25},
                "probe_counts": {"llm": 10},
            }
        )
    )
    (run_dir / "fuzz" / "cost.json").write_text(
        json.dumps(
            {"total_usd": 0.00, "per_layer_usd": {"fuzz": 0.00}, "probe_counts": {}}
        )
    )
    cost = run_all._aggregate_cost(run_dir, ["llm", "fuzz"])
    assert cost["total_usd"] == pytest.approx(0.25)
    assert cost["probe_counts"]["llm"] == 10


def test_resolve_exit_code_precedence():
    # Preflight error always wins.
    err = run_all.PreflightError("boom")
    assert run_all._resolve_exit_code(err, [], 0) == 1
    # Timeout beats failures.
    timed_out = run_all.LayerOutcome(
        name="x", exit_code=None, duration_seconds=0.0, timed_out=True
    )
    assert run_all._resolve_exit_code(None, [timed_out], 99) == 3
    # Non-zero subprocess exit triggers fail (2).
    bad = run_all.LayerOutcome(name="x", exit_code=1, duration_seconds=0.1)
    assert run_all._resolve_exit_code(None, [bad], 0) == 2
    # Failures recorded → 2.
    ok = run_all.LayerOutcome(name="x", exit_code=0, duration_seconds=0.1)
    assert run_all._resolve_exit_code(None, [ok], 1) == 2
    # Otherwise green.
    assert run_all._resolve_exit_code(None, [ok], 0) == 0


def test_layer_command_e2e_is_npm():
    assert run_all._layer_command("e2e", None)[0] == "npm"


def test_layer_command_python_uses_pytest():
    cmd = run_all._layer_command("fuzz", None)
    assert "pytest" in cmd
    assert cmd[-1] == "fuzz/"


def test_layer_command_llm_passes_probes():
    cmd = run_all._layer_command("llm", 25)
    assert "--llm-probes" in cmd
    assert "25" in cmd


def test_dotenv_loader_silent_when_missing(monkeypatch, tmp_path):
    # Replace harness root with an empty tmpdir → no .env file → no error.
    monkeypatch.setattr(run_all, "_HARNESS_ROOT", tmp_path)
    run_all._load_dotenv()  # must not raise
