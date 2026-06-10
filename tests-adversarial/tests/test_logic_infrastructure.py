"""Unit tests for the logic / state layer (Block F).

These tests exercise the pure functions in `logic/classifiers.py` with
synthetic inputs. No network calls; no `requests.Session`. The contract is:

  * Every classifier returns ``(status, severity, reason)``.
  * ``status`` is exactly ``"pass"`` or ``"fail"``.
  * ``severity`` is one of ``"low" | "medium" | "high"`` on FAIL and
    ``None`` on PASS.
  * ``reason`` is a non-empty human-readable string.

Coverage:

  * `classify_state_transition`:
      - skip-approve + 200 → FAIL HIGH (workflow bypass)
      - skip-approve + 409 → PASS
      - double-approve + 200 → FAIL MEDIUM
      - reject-after-execute + 400 → PASS
      - escalate-from-terminal + 409 → PASS
      - any kind + 500 → FAIL HIGH
      - any kind + 0 → FAIL MEDIUM
  * `classify_concurrent_writes`:
      - 1×200 + 4×409 → PASS
      - 3×200 → FAIL HIGH (race)
      - 5×500 → FAIL MEDIUM
      - 0×2xx → FAIL MEDIUM (all rejected)
      - 1×200 + 1×0 + 3×409 → FAIL MEDIUM (transport drop with winner)
      - empty → FAIL MEDIUM
  * `classify_field_exposure` / `walk_json_for_leaks`:
      - response with `password` → FAIL HIGH
      - response with `password_hash` → FAIL HIGH
      - response with cross-persona `cognito:groups` → FAIL MEDIUM
      - response with cross-user `email` → FAIL MEDIUM
      - response with `_internal` → FAIL LOW
      - response with caller's own email → PASS
      - response with caller's own cognito:groups → PASS
      - clean response → PASS
      - nested dict + array + scalar mix → walker descends and finds leaks
      - depth-cap stops descent (deeply nested secret beyond cap is missed)
      - non-dict / non-list scalar inputs return clean

Also pins `builder.py`'s acceptance of ``layer="logic"`` so the rest of
the harness writes the layer's results without an exception.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from logic.classifiers import (
    JSON_WALK_DEPTH_CAP,
    SEVERITY_HIGH,
    SEVERITY_LOW,
    SEVERITY_MEDIUM,
    classify_concurrent_writes,
    classify_field_exposure,
    classify_state_transition,
    walk_json_for_leaks,
)

# ─────────────────────── state-transition classifier ─────────────────────────


class TestClassifyStateTransition:
    def test_skip_approve_2xx_is_high_workflow_bypass(self) -> None:
        status, severity, reason = classify_state_transition(
            200, transition_kind="skip-approve"
        )
        assert status == "fail"
        assert severity == SEVERITY_HIGH
        assert "workflow bypass" in reason

    def test_skip_approve_409_is_pass(self) -> None:
        status, severity, reason = classify_state_transition(
            409, transition_kind="skip-approve"
        )
        assert status == "pass"
        assert severity is None
        assert "409" in reason

    def test_skip_approve_400_is_pass(self) -> None:
        status, _, _ = classify_state_transition(400, transition_kind="skip-approve")
        assert status == "pass"

    def test_skip_approve_403_is_pass(self) -> None:
        status, _, _ = classify_state_transition(403, transition_kind="skip-approve")
        assert status == "pass"

    def test_double_approve_2xx_is_medium(self) -> None:
        status, severity, _ = classify_state_transition(
            200, transition_kind="double-approve"
        )
        assert status == "fail"
        assert severity == SEVERITY_MEDIUM

    def test_double_approve_409_is_pass(self) -> None:
        status, severity, _ = classify_state_transition(
            409, transition_kind="double-approve"
        )
        assert status == "pass"
        assert severity is None

    def test_reject_after_execute_400_is_pass(self) -> None:
        status, _, _ = classify_state_transition(
            400, transition_kind="reject-after-execute"
        )
        assert status == "pass"

    def test_reject_after_execute_2xx_is_medium(self) -> None:
        status, severity, _ = classify_state_transition(
            200, transition_kind="reject-after-execute"
        )
        assert status == "fail"
        assert severity == SEVERITY_MEDIUM

    def test_escalate_from_terminal_409_is_pass(self) -> None:
        status, _, _ = classify_state_transition(
            409, transition_kind="escalate-from-terminal"
        )
        assert status == "pass"

    def test_escalate_from_terminal_2xx_is_medium(self) -> None:
        status, severity, _ = classify_state_transition(
            201, transition_kind="escalate-from-terminal"
        )
        assert status == "fail"
        assert severity == SEVERITY_MEDIUM

    def test_5xx_any_kind_is_high(self) -> None:
        for kind in (
            "skip-approve",
            "double-approve",
            "reject-after-execute",
            "escalate-from-terminal",
        ):
            status, severity, _ = classify_state_transition(500, transition_kind=kind)
            assert status == "fail"
            assert severity == SEVERITY_HIGH

    def test_transport_drop_is_medium(self) -> None:
        status, severity, reason = classify_state_transition(
            0, transition_kind="skip-approve"
        )
        assert status == "fail"
        assert severity == SEVERITY_MEDIUM
        assert "transport" in reason.lower()

    def test_unexpected_3xx_is_medium_fail(self) -> None:
        status, severity, _ = classify_state_transition(
            302, transition_kind="double-approve"
        )
        assert status == "fail"
        assert severity == SEVERITY_MEDIUM


# ─────────────────── concurrent-writes classifier ────────────────────────────


class TestClassifyConcurrentWrites:
    def test_single_winner_with_409_rejections_is_pass(self) -> None:
        status, severity, _ = classify_concurrent_writes([200, 409, 409, 409, 409])
        assert status == "pass"
        assert severity is None

    def test_single_winner_with_404_rejections_is_pass(self) -> None:
        # delete-conversation race: 1 winner, 2 see "already gone".
        status, severity, _ = classify_concurrent_writes([200, 404, 404])
        assert status == "pass"
        assert severity is None

    def test_multiple_winners_is_high_race(self) -> None:
        status, severity, reason = classify_concurrent_writes([200, 200, 200, 409, 409])
        assert status == "fail"
        assert severity == SEVERITY_HIGH
        assert "race" in reason.lower()

    def test_two_winners_when_expected_one_is_high(self) -> None:
        status, severity, _ = classify_concurrent_writes(
            [200, 201, 409], expected_successes=1
        )
        assert status == "fail"
        assert severity == SEVERITY_HIGH

    def test_all_500_is_medium_crash(self) -> None:
        status, severity, reason = classify_concurrent_writes([500, 500, 500, 500, 500])
        assert status == "fail"
        assert severity == SEVERITY_MEDIUM
        assert "crash" in reason.lower() or "5xx" in reason

    def test_zero_winners_is_medium(self) -> None:
        status, severity, _ = classify_concurrent_writes([409, 409, 409])
        assert status == "fail"
        assert severity == SEVERITY_MEDIUM

    def test_winner_plus_transport_drop_is_medium(self) -> None:
        status, severity, reason = classify_concurrent_writes([200, 0, 409, 409, 409])
        assert status == "fail"
        assert severity == SEVERITY_MEDIUM
        assert "transport" in reason.lower() or "drop" in reason.lower()

    def test_empty_input_is_medium(self) -> None:
        status, severity, _ = classify_concurrent_writes([])
        assert status == "fail"
        assert severity == SEVERITY_MEDIUM

    def test_two_winners_when_expected_two_is_pass(self) -> None:
        # Configurable expected — used for batch-approve flows.
        status, _, _ = classify_concurrent_writes([200, 200, 409], expected_successes=2)
        assert status == "pass"


# ───────────────────── field-exposure classifier ─────────────────────────────


class TestClassifyFieldExposure:
    _CALLER_GROUPS = ("soc",)
    _CALLER_EMAIL = "soc_marcus@meridianinsurance.com"

    def test_password_field_is_high(self) -> None:
        body = {"user": "alice", "password": "hunter2"}
        status, severity, reason = classify_field_exposure(
            body, caller_groups=self._CALLER_GROUPS, caller_email=self._CALLER_EMAIL
        )
        assert status == "fail"
        assert severity == SEVERITY_HIGH
        assert "password" in reason

    def test_password_hash_field_is_high(self) -> None:
        body = {"user": "alice", "password_hash": "$2b$..."}
        _, severity, _ = classify_field_exposure(
            body, caller_groups=self._CALLER_GROUPS, caller_email=self._CALLER_EMAIL
        )
        assert severity == SEVERITY_HIGH

    def test_passwordHash_camelcase_is_high(self) -> None:
        body = {"passwordHash": "x"}
        _, severity, _ = classify_field_exposure(
            body, caller_groups=self._CALLER_GROUPS, caller_email=self._CALLER_EMAIL
        )
        assert severity == SEVERITY_HIGH

    def test_secret_field_is_high(self) -> None:
        body = {"secret": "abc"}
        _, severity, _ = classify_field_exposure(
            body, caller_groups=self._CALLER_GROUPS, caller_email=self._CALLER_EMAIL
        )
        assert severity == SEVERITY_HIGH

    def test_api_key_field_is_high(self) -> None:
        body = {"api_key": "abc"}
        _, severity, _ = classify_field_exposure(
            body, caller_groups=self._CALLER_GROUPS, caller_email=self._CALLER_EMAIL
        )
        assert severity == SEVERITY_HIGH

    def test_private_key_field_is_high(self) -> None:
        body = {"private_key": "abc"}
        _, severity, _ = classify_field_exposure(
            body, caller_groups=self._CALLER_GROUPS, caller_email=self._CALLER_EMAIL
        )
        assert severity == SEVERITY_HIGH

    def test_aws_access_key_field_is_high(self) -> None:
        body = {"aws_access_key": "AKIA..."}
        _, severity, _ = classify_field_exposure(
            body, caller_groups=self._CALLER_GROUPS, caller_email=self._CALLER_EMAIL
        )
        assert severity == SEVERITY_HIGH

    def test_cross_persona_cognito_groups_is_medium(self) -> None:
        body = {"user_id": "x", "cognito:groups": ["ciso"]}
        status, severity, reason = classify_field_exposure(
            body, caller_groups=self._CALLER_GROUPS, caller_email=self._CALLER_EMAIL
        )
        assert status == "fail"
        assert severity == SEVERITY_MEDIUM
        assert "cognito:groups" in reason or "ciso" in reason

    def test_caller_own_cognito_groups_is_pass(self) -> None:
        body = {"user_id": "x", "cognito:groups": ["soc"]}
        status, _, _ = classify_field_exposure(
            body, caller_groups=self._CALLER_GROUPS, caller_email=self._CALLER_EMAIL
        )
        assert status == "pass"

    def test_cross_user_email_is_medium(self) -> None:
        body = {"email": "ciso_diana@meridianinsurance.com"}
        status, severity, _ = classify_field_exposure(
            body, caller_groups=self._CALLER_GROUPS, caller_email=self._CALLER_EMAIL
        )
        assert status == "fail"
        assert severity == SEVERITY_MEDIUM

    def test_caller_own_email_is_pass(self) -> None:
        body = {"email": self._CALLER_EMAIL}
        status, _, _ = classify_field_exposure(
            body, caller_groups=self._CALLER_GROUPS, caller_email=self._CALLER_EMAIL
        )
        assert status == "pass"

    def test_caller_email_case_insensitive(self) -> None:
        body = {"email": self._CALLER_EMAIL.upper()}
        status, _, _ = classify_field_exposure(
            body, caller_groups=self._CALLER_GROUPS, caller_email=self._CALLER_EMAIL
        )
        assert status == "pass"

    def test_internal_field_is_low(self) -> None:
        body = {"name": "x", "_internal": "stuff"}
        status, severity, _ = classify_field_exposure(
            body, caller_groups=self._CALLER_GROUPS, caller_email=self._CALLER_EMAIL
        )
        assert status == "fail"
        assert severity == SEVERITY_LOW

    def test_mongo_id_is_low(self) -> None:
        body = {"_id": "abc", "name": "x"}
        _, severity, _ = classify_field_exposure(
            body, caller_groups=self._CALLER_GROUPS, caller_email=self._CALLER_EMAIL
        )
        assert severity == SEVERITY_LOW

    def test_mongo_version_key_is_low(self) -> None:
        body = {"__v": 1, "name": "x"}
        _, severity, _ = classify_field_exposure(
            body, caller_groups=self._CALLER_GROUPS, caller_email=self._CALLER_EMAIL
        )
        assert severity == SEVERITY_LOW

    def test_clean_response_is_pass(self) -> None:
        body = {
            "findings": [
                {"id": "F-1", "severity": "HIGH", "status": "OPEN"},
                {"id": "F-2", "severity": "LOW", "status": "RESOLVED"},
            ],
            "count": 2,
        }
        status, severity, _ = classify_field_exposure(
            body, caller_groups=self._CALLER_GROUPS, caller_email=self._CALLER_EMAIL
        )
        assert status == "pass"
        assert severity is None

    def test_high_wins_over_medium_and_low(self) -> None:
        # Multiple hits — classifier should report the worst (HIGH).
        body = {
            "password": "x",
            "cognito:groups": ["ciso"],
            "_internal": "y",
        }
        _, severity, reason = classify_field_exposure(
            body, caller_groups=self._CALLER_GROUPS, caller_email=self._CALLER_EMAIL
        )
        assert severity == SEVERITY_HIGH
        # Should hint there are more.
        assert "other field" in reason or "+" in reason

    def test_nested_password_is_found(self) -> None:
        body = {
            "users": [
                {"name": "alice"},
                {"name": "bob", "credentials": {"password": "x"}},
            ]
        }
        _, severity, _ = classify_field_exposure(
            body, caller_groups=self._CALLER_GROUPS, caller_email=self._CALLER_EMAIL
        )
        assert severity == SEVERITY_HIGH

    def test_walker_descends_arrays_and_dicts(self) -> None:
        body = [{"a": [{"b": {"secret": "leak"}}]}]
        hits = walk_json_for_leaks(
            body,
            caller_groups=self._CALLER_GROUPS,
            caller_email=self._CALLER_EMAIL,
        )
        assert any(sev == SEVERITY_HIGH for sev, _, _ in hits)

    def test_walker_handles_scalar_inputs(self) -> None:
        for scalar in ("just a string", 42, None, True, 1.5):
            assert (
                walk_json_for_leaks(
                    scalar,
                    caller_groups=self._CALLER_GROUPS,
                    caller_email=self._CALLER_EMAIL,
                )
                == []
            )

    def test_walker_depth_cap_stops_descent(self) -> None:
        # Build a deeply nested dict with `password` at the very bottom,
        # past JSON_WALK_DEPTH_CAP. The walker should not reach it.
        nested: object = {"password": "leak-deep"}
        for _ in range(JSON_WALK_DEPTH_CAP + 3):
            nested = {"next": nested}
        hits = walk_json_for_leaks(
            nested,
            caller_groups=self._CALLER_GROUPS,
            caller_email=self._CALLER_EMAIL,
        )
        assert hits == []

    def test_walker_depth_cap_finds_at_shallow(self) -> None:
        # Same shape but with the credential within the cap.
        nested: object = {"password": "leak-shallow"}
        for _ in range(JSON_WALK_DEPTH_CAP - 3):
            nested = {"next": nested}
        hits = walk_json_for_leaks(
            nested,
            caller_groups=self._CALLER_GROUPS,
            caller_email=self._CALLER_EMAIL,
        )
        assert any(sev == SEVERITY_HIGH for sev, _, _ in hits)

    def test_cognito_groups_string_form_is_split(self) -> None:
        # API Gateway sometimes flattens cognito:groups into a comma-joined
        # string; the walker should still detect the cross-persona group.
        body = {"cognito:groups": "soc,ciso,grc"}
        status, severity, _ = classify_field_exposure(
            body, caller_groups=self._CALLER_GROUPS, caller_email=self._CALLER_EMAIL
        )
        assert status == "fail"
        assert severity == SEVERITY_MEDIUM

    def test_empty_caller_email_skips_email_check(self) -> None:
        # When the harness can't resolve the caller's email, we shouldn't
        # flood with false positives on every email field.
        body = {"email": "anyone@example.com"}
        status, _, _ = classify_field_exposure(
            body, caller_groups=self._CALLER_GROUPS, caller_email=""
        )
        assert status == "pass"


# ────────────────────── builder layer acceptance ─────────────────────────────


def test_builder_accepts_logic_layer(tmp_path: Path) -> None:
    """`load_results` must walk a `<run_dir>/logic/results.json` file. This
    pins the layer name in `src/coverage/builder.py::_LAYERS` so a
    refactor doesn't silently drop the logic layer from aggregation."""
    from src.coverage.builder import _LAYERS, load_results

    assert "logic" in _LAYERS

    # Synthesize a minimal logic row and confirm it round-trips.
    logic_dir = tmp_path / "logic"
    logic_dir.mkdir()
    row = {
        "test_id": "logic.workflow.skip-approve",
        "status": "pass",
        "layer": "logic",
        "target_kind": "api_route",
        "target_id": "post-action-execute",
    }
    (logic_dir / "results.json").write_text(json.dumps([row]), encoding="utf-8")

    results = load_results(tmp_path)
    assert len(results) == 1
    assert results[0].test_id == "logic.workflow.skip-approve"
    assert results[0].layer == "logic"


def test_runner_layers_includes_logic() -> None:
    """`scripts.run_all._LAYERS_ALL` should include `'logic'` so the
    orchestrator runs the layer by default.
    """
    from scripts.run_all import _LAYERS_ALL

    assert "logic" in _LAYERS_ALL


def test_runner_hard_cap_for_logic() -> None:
    """The logic layer should be pinned to a 5-minute wall-clock cap so a
    runaway state-machine probe can't keep hammering the dev environment.
    """
    from scripts.run_all import _LAYER_HARD_CAPS_SECONDS

    assert _LAYER_HARD_CAPS_SECONDS.get("logic") == 300.0


def test_runner_logic_budget_is_zero_bedrock() -> None:
    """`_build_layer_budgets` must register a zero-token LayerBudget for
    logic so the cost-preflight gate treats the layer as non-Bedrock.
    """
    from scripts.run_all import _build_layer_budgets

    budgets = _build_layer_budgets(["logic"])
    assert "logic" in budgets
    assert budgets["logic"].max_input_tokens == 0
    assert budgets["logic"].max_output_tokens == 0


# ────────────────────── parametrize sanity (no network) ──────────────────────


def test_field_exposure_parametrize_enumerates_personas() -> None:
    """The parametrize generator should produce at least one case per
    persona, so the layer's coverage actually fans out by identity.
    """
    from logic import test_field_exposure as mod

    cases = mod._persona_route_cases()
    personas = {p for p, _ in cases}
    assert personas == {"ciso", "soc", "grc", "employee"}


def test_field_exposure_skips_path_param_routes() -> None:
    """Routes with `{path-param}` in the path are filtered out — the
    harness can't synthesize an id."""
    from logic import test_field_exposure as mod

    eligible = mod._eligible_get_routes()
    for route in eligible:
        assert "{" not in (route.get("path") or "")
        assert "}" not in (route.get("path") or "")


def test_field_exposure_skips_health() -> None:
    """`/health` is unauthenticated and has no sensitive fields — skip."""
    from logic import test_field_exposure as mod

    eligible_ids = {r.get("id") for r in mod._eligible_get_routes()}
    assert "get-health" not in eligible_ids


# Module-level safety: pytest should not collect the layer's test_* modules
# as part of this unit-test run by accident. We don't enforce that here
# (the user runs `pytest tests/`), but a smoke check on the imports keeps
# the layer importable from a clean checkout.


def test_logic_modules_import_cleanly() -> None:
    """Each test module under `logic/` must import without raising — a
    syntax error or missing-import would otherwise only surface at the
    full-layer pytest run."""
    import importlib

    for mod_name in (
        "logic.classifiers",
        "logic.conftest",
        "logic.test_action_state_machine",
        "logic.test_race_conditions",
        "logic.test_field_exposure",
    ):
        importlib.import_module(mod_name)


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
