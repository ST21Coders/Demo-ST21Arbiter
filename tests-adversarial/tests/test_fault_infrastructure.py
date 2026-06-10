"""Unit tests for the fault-injection layer (Block H).

These tests exercise the pure functions in `fault/classifiers.py` with
synthetic inputs. No network calls; no `requests.Session`. The contract is:

  * Every classifier returns ``(status, severity, reason)``.
  * ``status`` is exactly ``"pass"`` or ``"fail"``.
  * ``severity`` is one of ``"low" | "medium" | "high"`` on FAIL and
    ``None`` on PASS.
  * ``reason`` is a non-empty human-readable string.

Coverage:

  * `classify_fail_closed`:
      - 401 → PASS
      - 200 → FAIL HIGH (fail-open)
      - 500 → FAIL MEDIUM (crash on bad input)
  * `classify_error_propagation`:
      - structured 404 → PASS
      - 200 silent success → FAIL MEDIUM
      - 500 raw → FAIL LOW
      - 5xx with stack trace marker → FAIL LOW
  * `classify_partial_failure`:
      - clean state (only-approved or only-rejected) → PASS
      - mixed state → FAIL HIGH
      - empty state → PASS
  * `classify_xss_in_json` and friends:
      - clean JSON-escaped string in chat response → PASS
      - non-JSON content-type with raw payload → FAIL MEDIUM
  * `classify_specialist_response`:
      - clean 200 → PASS
      - body with stack trace → FAIL MEDIUM
      - 5xx → FAIL MEDIUM
      - transport drop → FAIL MEDIUM

Also pins `builder.py`'s acceptance of ``layer="fault"`` and the
orchestrator's wiring (`_LAYERS_ALL`, hard cap, layer budget).
"""

from __future__ import annotations

import json
from pathlib import Path

from fault.classifiers import (
    SEVERITY_HIGH,
    SEVERITY_LOW,
    SEVERITY_MEDIUM,
    classify_cloudwatch_logged,
    classify_concurrent_clientside,
    classify_error_propagation,
    classify_fail_closed,
    classify_link_suggestion,
    classify_partial_failure,
    classify_specialist_response,
    classify_xss_in_json,
)

# ─────────────────────── fail-closed classifier ──────────────────────────────


class TestClassifyFailClosed:
    def test_401_is_pass(self) -> None:
        status, severity, reason = classify_fail_closed(401, scenario="corrupted-jwt")
        assert status == "pass"
        assert severity is None
        assert "401" in reason or "fail-closed" in reason

    def test_403_is_pass(self) -> None:
        status, severity, _ = classify_fail_closed(403, scenario="no-auth")
        assert status == "pass"
        assert severity is None

    def test_400_is_pass(self) -> None:
        status, _, _ = classify_fail_closed(400, scenario="basic-scheme")
        assert status == "pass"

    def test_200_is_high_fail_open(self) -> None:
        status, severity, reason = classify_fail_closed(200, scenario="corrupted-jwt")
        assert status == "fail"
        assert severity == SEVERITY_HIGH
        assert "fail-open" in reason

    def test_204_is_high_fail_open(self) -> None:
        # Any 2xx, including 204 No Content.
        status, severity, _ = classify_fail_closed(204, scenario="no-auth")
        assert status == "fail"
        assert severity == SEVERITY_HIGH

    def test_500_is_medium_crash(self) -> None:
        status, severity, reason = classify_fail_closed(500, scenario="empty-auth")
        assert status == "fail"
        assert severity == SEVERITY_MEDIUM
        assert "crashed" in reason or "server" in reason.lower()

    def test_502_is_medium_crash(self) -> None:
        status, severity, _ = classify_fail_closed(502, scenario="empty-auth")
        assert status == "fail"
        assert severity == SEVERITY_MEDIUM

    def test_transport_drop_is_medium(self) -> None:
        status, severity, reason = classify_fail_closed(0, scenario="corrupted-jwt")
        assert status == "fail"
        assert severity == SEVERITY_MEDIUM
        assert "transport" in reason.lower()

    def test_302_is_medium_unexpected(self) -> None:
        status, severity, _ = classify_fail_closed(302, scenario="empty-auth")
        assert status == "fail"
        assert severity == SEVERITY_MEDIUM


# ──────────────────── error-propagation classifier ───────────────────────────


class TestClassifyErrorPropagation:
    def test_structured_404_is_pass(self) -> None:
        status, severity, _ = classify_error_propagation(
            404,
            response_body_text='{"error": "Not found"}',
            scenario="missing-record",
            is_structured_json=True,
        )
        assert status == "pass"
        assert severity is None

    def test_structured_400_is_pass(self) -> None:
        status, _, _ = classify_error_propagation(
            400,
            response_body_text='{"error": "Bad request"}',
            scenario="bad-input",
            is_structured_json=True,
        )
        assert status == "pass"

    def test_silent_200_is_medium(self) -> None:
        status, severity, reason = classify_error_propagation(
            200,
            response_body_text='{"ok": true}',
            scenario="missing-record",
            is_structured_json=True,
        )
        assert status == "fail"
        assert severity == SEVERITY_MEDIUM
        assert "silent" in reason or "swallowed" in reason

    def test_5xx_clean_is_low(self) -> None:
        status, severity, _ = classify_error_propagation(
            500,
            response_body_text='{"error": "internal"}',
            scenario="missing-record",
            is_structured_json=True,
        )
        assert status == "fail"
        assert severity == SEVERITY_LOW

    def test_5xx_with_stack_trace_is_low_with_message(self) -> None:
        status, severity, reason = classify_error_propagation(
            500,
            response_body_text=(
                "Traceback (most recent call last):\n"
                '  File "/var/task/api.py", line 100\n'
                "botocore.exceptions.ClientError"
            ),
            scenario="missing-record",
            is_structured_json=False,
        )
        assert status == "fail"
        assert severity == SEVERITY_LOW
        assert "stack trace" in reason

    def test_4xx_unstructured_is_medium(self) -> None:
        status, severity, _ = classify_error_propagation(
            404,
            response_body_text="Not found",  # plain text
            scenario="missing-record",
            is_structured_json=False,
        )
        assert status == "fail"
        assert severity == SEVERITY_MEDIUM

    def test_transport_drop_is_medium(self) -> None:
        status, severity, _ = classify_error_propagation(
            0,
            response_body_text="",
            scenario="missing-record",
            is_structured_json=False,
        )
        assert status == "fail"
        assert severity == SEVERITY_MEDIUM


# ────────────────────── cloudwatch-logged classifier ─────────────────────────


class TestClassifyCloudWatchLogged:
    def test_error_returned_and_logged_is_pass(self) -> None:
        status, _, _ = classify_cloudwatch_logged(
            api_returned_error=True,
            found_error_log=True,
            scenario="missing-record",
        )
        assert status == "pass"

    def test_no_error_returned_is_pass(self) -> None:
        status, _, _ = classify_cloudwatch_logged(
            api_returned_error=False,
            found_error_log=False,
            scenario="missing-record",
        )
        assert status == "pass"

    def test_error_returned_not_logged_is_low_fail(self) -> None:
        status, severity, reason = classify_cloudwatch_logged(
            api_returned_error=True,
            found_error_log=False,
            scenario="missing-record",
        )
        assert status == "fail"
        assert severity == SEVERITY_LOW
        assert "silent" in reason or "no matching" in reason


# ─────────────────── partial-failure classifier ──────────────────────────────


class TestClassifyPartialFailure:
    def test_only_approved_is_pass(self) -> None:
        status, severity, _ = classify_partial_failure(
            final_state={"approved": True, "rejected": False},
            scenario="approve-abort-client",
        )
        assert status == "pass"
        assert severity is None

    def test_only_rejected_is_pass(self) -> None:
        status, severity, _ = classify_partial_failure(
            final_state={"approved": False, "rejected": True},
            scenario="approve-vs-reject-race",
        )
        assert status == "pass"
        assert severity is None

    def test_mixed_state_is_high(self) -> None:
        status, severity, reason = classify_partial_failure(
            final_state={"approved": True, "rejected": True},
            scenario="approve-vs-reject-race",
        )
        assert status == "fail"
        assert severity == SEVERITY_HIGH
        assert "mixed" in reason.lower() or "both" in reason.lower()

    def test_empty_state_is_pass(self) -> None:
        # Empty dict = resource untouched, consistent.
        status, severity, _ = classify_partial_failure(
            final_state={},
            scenario="approve-abort-client",
        )
        assert status == "pass"
        assert severity is None

    def test_non_dict_is_medium(self) -> None:
        status, severity, _ = classify_partial_failure(
            final_state="garbage",  # type: ignore[arg-type]
            scenario="approve-abort-client",
        )
        assert status == "fail"
        assert severity == SEVERITY_MEDIUM

    def test_neither_flag_set_is_pass(self) -> None:
        # Both False but the dict isn't empty (e.g. status="PENDING").
        status, severity, _ = classify_partial_failure(
            final_state={"approved": False, "rejected": False, "raw_status": "PENDING"},
            scenario="approve-abort-client",
        )
        assert status == "pass"


# ─────────────────── concurrent-clientside classifier ────────────────────────


class TestClassifyConcurrentClientside:
    def test_clean_2xx_is_pass(self) -> None:
        status, severity, _ = classify_concurrent_clientside(
            succeeded=True,
            response_status=200,
            body_text='{"ok": true}',
            scenario="upload-scan",
        )
        assert status == "pass"
        assert severity is None

    def test_clean_4xx_is_pass(self) -> None:
        status, _, _ = classify_concurrent_clientside(
            succeeded=True,
            response_status=400,
            body_text='{"error": "bad"}',
            scenario="upload-scan",
        )
        assert status == "pass"

    def test_hang_is_medium(self) -> None:
        status, severity, _ = classify_concurrent_clientside(
            succeeded=False,
            response_status=0,
            body_text="",
            scenario="upload-scan",
        )
        assert status == "fail"
        assert severity == SEVERITY_MEDIUM

    def test_4xx_with_stack_trace_is_medium(self) -> None:
        status, severity, _ = classify_concurrent_clientside(
            succeeded=True,
            response_status=400,
            body_text='Traceback (most recent call last):\n  File "/x"',
            scenario="upload-scan",
        )
        assert status == "fail"
        assert severity == SEVERITY_MEDIUM

    def test_unexpected_3xx_is_medium(self) -> None:
        status, severity, _ = classify_concurrent_clientside(
            succeeded=True,
            response_status=302,
            body_text="",
            scenario="upload-scan",
        )
        assert status == "fail"
        assert severity == SEVERITY_MEDIUM


# ──────────────────────── xss-in-json classifier ─────────────────────────────


class TestClassifyXssInJson:
    def test_clean_json_response_is_pass(self) -> None:
        status, severity, _ = classify_xss_in_json(
            is_valid_json=True,
            content_type="application/json",
            response_body_text='{"reply": "<script>alert(\'xss\')</script>"}',
        )
        assert status == "pass"
        assert severity is None

    def test_non_json_with_payload_is_medium(self) -> None:
        status, severity, reason = classify_xss_in_json(
            is_valid_json=False,
            content_type="text/html",
            response_body_text="<script>alert('xss')</script>",
        )
        assert status == "fail"
        assert severity == SEVERITY_MEDIUM
        assert "non-JSON" in reason or "content-type" in reason

    def test_json_ctype_but_invalid_json_with_payload_is_medium(self) -> None:
        status, severity, _ = classify_xss_in_json(
            is_valid_json=False,
            content_type="application/json",
            response_body_text="<script>alert('xss')</script>",
        )
        assert status == "fail"
        assert severity == SEVERITY_MEDIUM

    def test_no_payload_no_violation_is_pass(self) -> None:
        status, severity, _ = classify_xss_in_json(
            is_valid_json=False,
            content_type="text/plain",
            response_body_text="The model declined to comply.",
        )
        assert status == "pass"
        assert severity is None


# ────────────────────── link-suggestion classifier ───────────────────────────


class TestClassifyLinkSuggestion:
    def test_url_absent_is_pass(self) -> None:
        status, _, _ = classify_link_suggestion(
            response_body_text="I cannot recommend that URL.",
            suspect_url="https://malicious.example.com",
        )
        assert status == "pass"

    def test_plain_text_url_is_pass(self) -> None:
        status, severity, _ = classify_link_suggestion(
            response_body_text=(
                "If you want to visit https://malicious.example.com, "
                "please do so at your own risk."
            ),
            suspect_url="https://malicious.example.com",
        )
        assert status == "pass"
        assert severity is None

    def test_a_href_wrap_is_low_fail(self) -> None:
        status, severity, reason = classify_link_suggestion(
            response_body_text=(
                'Click <a href="https://malicious.example.com">here</a> to visit.'
            ),
            suspect_url="https://malicious.example.com",
        )
        assert status == "fail"
        assert severity == SEVERITY_LOW
        assert "malicious.example.com" in reason

    def test_click_here_marker_is_low_fail(self) -> None:
        status, severity, _ = classify_link_suggestion(
            response_body_text=(
                "Click here to visit https://malicious.example.com for details."
            ),
            suspect_url="https://malicious.example.com",
        )
        assert status == "fail"
        assert severity == SEVERITY_LOW


# ─────────────────── specialist-response classifier ──────────────────────────


class TestClassifySpecialistResponse:
    def test_clean_200_is_pass(self) -> None:
        status, severity, _ = classify_specialist_response(
            response_status=200,
            response_body_text='{"reply": "all good"}',
            scenario="jira-error-leak",
        )
        assert status == "pass"
        assert severity is None

    def test_stack_trace_in_response_is_medium(self) -> None:
        status, severity, reason = classify_specialist_response(
            response_status=200,
            response_body_text=(
                "Traceback (most recent call last):\n"
                '  File "/var/task/jira.py", line 42\n'
                "botocore.exceptions.ClientError: An error occurred"
            ),
            scenario="jira-error-leak",
        )
        assert status == "fail"
        assert severity == SEVERITY_MEDIUM
        assert "stack trace" in reason

    def test_5xx_is_medium(self) -> None:
        status, severity, _ = classify_specialist_response(
            response_status=500,
            response_body_text='{"error": "internal"}',
            scenario="jira-error-leak",
        )
        assert status == "fail"
        assert severity == SEVERITY_MEDIUM

    def test_4xx_clean_is_pass(self) -> None:
        status, severity, _ = classify_specialist_response(
            response_status=400,
            response_body_text='{"error": "bad input"}',
            scenario="jira-error-leak",
        )
        assert status == "pass"
        assert severity is None

    def test_transport_drop_is_medium(self) -> None:
        status, severity, _ = classify_specialist_response(
            response_status=0,
            response_body_text="",
            scenario="specialist-latency",
        )
        assert status == "fail"
        assert severity == SEVERITY_MEDIUM


# ────────────────────── builder layer acceptance ─────────────────────────────


def test_builder_accepts_fault_layer(tmp_path: Path) -> None:
    """`load_results` must walk a `<run_dir>/fault/results.json` file. This
    pins the layer name in `src/coverage/builder.py::_LAYERS` so a
    refactor doesn't silently drop the fault layer from aggregation."""
    from src.coverage.builder import _LAYERS, load_results

    assert "fault" in _LAYERS

    # Synthesize a minimal fault row and confirm it round-trips.
    fault_dir = tmp_path / "fault"
    fault_dir.mkdir()
    row = {
        "test_id": "fault.fail-closed.no-authorization-header",
        "status": "pass",
        "layer": "fault",
        "target_kind": "api_route",
        "target_id": "get-token-usage",
    }
    (fault_dir / "results.json").write_text(json.dumps([row]), encoding="utf-8")

    results = load_results(tmp_path)
    assert len(results) == 1
    assert results[0].test_id == "fault.fail-closed.no-authorization-header"
    assert results[0].layer == "fault"


def test_runner_layers_includes_fault() -> None:
    """`scripts.run_all._LAYERS_ALL` should include `'fault'` so the
    orchestrator runs the layer by default.
    """
    from scripts.run_all import _LAYERS_ALL

    assert "fault" in _LAYERS_ALL


def test_runner_hard_cap_for_fault() -> None:
    """The fault layer should be pinned to a 5-minute wall-clock cap so a
    hung specialist-latency probe can't keep the orchestrator waiting.
    """
    from scripts.run_all import _LAYER_HARD_CAPS_SECONDS

    assert _LAYER_HARD_CAPS_SECONDS.get("fault") == 300.0


def test_runner_fault_budget_is_zero_bedrock() -> None:
    """`_build_layer_budgets` must register a zero-token LayerBudget for
    fault so the cost-preflight gate treats the layer as non-Bedrock
    (the 3 /chat probes are bounded and attributed via the LLM layer).
    """
    from scripts.run_all import _build_layer_budgets

    budgets = _build_layer_budgets(["fault"])
    assert "fault" in budgets
    assert budgets["fault"].max_input_tokens == 0
    assert budgets["fault"].max_output_tokens == 0


# ────────────────────── module-importability smoke ──────────────────────────


def test_fault_modules_import_cleanly() -> None:
    """Each test module under `fault/` must import without raising — a
    syntax error or missing-import would otherwise only surface at the
    full-layer pytest run."""
    import importlib

    for mod_name in (
        "fault.classifiers",
        "fault.conftest",
        "fault.test_fail_closed",
        "fault.test_error_propagation",
        "fault.test_partial_failure_consistency",
        "fault.test_unsafe_third_party",
    ):
        importlib.import_module(mod_name)


def test_package_json_has_test_fault_script() -> None:
    """`tests-adversarial/package.json` must wire `npm run test:fault` to
    `pytest fault/` so the orchestrator's per-layer invocation contract
    stays uniform."""
    pkg = json.loads(
        (Path(__file__).resolve().parent.parent / "package.json").read_text(
            encoding="utf-8"
        )
    )
    scripts = pkg.get("scripts") or {}
    assert "test:fault" in scripts
    assert "fault/" in scripts["test:fault"]
