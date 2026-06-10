"""Unit tests for the logging / audit layer (Block G).

These tests exercise the pure functions in `logging_audit/classifiers.py`
plus the wiring around the layer:

  * Every classifier returns ``(status, severity, reason)``.
  * ``status`` is exactly ``"pass"`` or ``"fail"``.
  * ``severity`` is one of ``"low" | "medium" | "high"`` on FAIL and
    ``None`` on PASS.
  * ``reason`` is a non-empty human-readable string.

Coverage:

  * ``classify_security_event_logged``:
      - >= 1 matching audit entry → PASS.
      - 0 matching audit entries → FAIL HIGH.
      - reason references the scenario id.
  * ``classify_log_redaction``:
      - canary not present (count == 0) → PASS.
      - canary present (count >= 1) → FAIL HIGH.
      - reason references the canary kind.
  * ``classify_log_injection_downstream``:
      - 1 matching event, no ANSI in samples → PASS.
      - 0 matching events → PASS (API didn't log; not our finding).
      - >1 matching events → FAIL MEDIUM (CRLF split → log forgery).
      - 1 matching event with ANSI ESC bytes in sample → FAIL LOW.

Also pins:

  * ``builder.py::_LAYERS`` contains ``"logging_audit"``.
  * The ``TestResult`` shape round-trips through the builder with
    ``layer="logging_audit"``.
"""

from __future__ import annotations


from logging_audit.classifiers import (
    SEVERITY_HIGH,
    SEVERITY_LOW,
    SEVERITY_MEDIUM,
    classify_log_injection_downstream,
    classify_log_redaction,
    classify_security_event_logged,
)
from src.coverage.builder import CellStatus, TestResult, build_matrix


# ───────────────────── classify_security_event_logged ────────────────────────


class TestClassifySecurityEventLogged:
    def test_one_match_is_pass(self) -> None:
        status, severity, reason = classify_security_event_logged(
            1, scenario_id="forged-token"
        )
        assert status == "pass"
        assert severity is None
        assert "forged-token" in reason
        assert "1" in reason

    def test_many_matches_is_pass(self) -> None:
        status, severity, _ = classify_security_event_logged(
            17, scenario_id="cross-persona"
        )
        assert status == "pass"
        assert severity is None

    def test_zero_matches_is_fail_high(self) -> None:
        status, severity, reason = classify_security_event_logged(
            0, scenario_id="brute-force"
        )
        assert status == "fail"
        assert severity == SEVERITY_HIGH
        assert "brute-force" in reason
        assert "undetected" in reason.lower() or "no audit" in reason.lower()

    def test_reason_is_non_empty(self) -> None:
        _, _, reason = classify_security_event_logged(0, scenario_id="x")
        assert reason
        assert isinstance(reason, str)


# ─────────────────────── classify_log_redaction ──────────────────────────────


class TestClassifyLogRedaction:
    def test_zero_canary_hits_is_pass(self) -> None:
        status, severity, reason = classify_log_redaction(0, canary_kind="jwt")
        assert status == "pass"
        assert severity is None
        assert "jwt" in reason
        assert "not present" in reason.lower()

    def test_one_canary_hit_is_fail_high(self) -> None:
        status, severity, reason = classify_log_redaction(1, canary_kind="body-field")
        assert status == "fail"
        assert severity == SEVERITY_HIGH
        assert "body-field" in reason
        assert "leaked" in reason.lower() or "verbatim" in reason.lower()

    def test_many_canary_hits_is_fail_high(self) -> None:
        status, severity, _ = classify_log_redaction(42, canary_kind="email")
        assert status == "fail"
        assert severity == SEVERITY_HIGH

    def test_reason_includes_canary_count(self) -> None:
        _, _, reason = classify_log_redaction(7, canary_kind="jwt")
        assert "7" in reason


# ─────────────────── classify_log_injection_downstream ───────────────────────


class TestClassifyLogInjectionDownstream:
    def test_single_line_clean_is_pass(self) -> None:
        status, severity, reason = classify_log_injection_downstream(
            1,
            sample_messages=["marker--foo bar--marker"],
            payload_id="logi-crlf-fake-entry",
        )
        assert status == "pass"
        assert severity is None
        assert "logi-crlf-fake-entry" in reason

    def test_zero_events_is_pass(self) -> None:
        """API didn't log the input at all — not our finding to flag.

        Other probes (#67 / #68) catch true logging gaps; this classifier
        only fires when CR/LF actually split a log event.
        """
        status, severity, reason = classify_log_injection_downstream(
            0,
            sample_messages=[],
            payload_id="logi-url-encoded-crlf",
        )
        assert status == "pass"
        assert severity is None
        assert "logi-url-encoded-crlf" in reason

    def test_two_events_is_fail_medium(self) -> None:
        """CR/LF split the line — attacker can forge a fake log entry."""
        status, severity, reason = classify_log_injection_downstream(
            2,
            sample_messages=["marker--foo", "FAKE LOG ENTRY: admin--marker"],
            payload_id="logi-crlf-fake-entry",
        )
        assert status == "fail"
        assert severity == SEVERITY_MEDIUM
        assert "logi-crlf-fake-entry" in reason
        assert "split" in reason.lower() or "forgery" in reason.lower()

    def test_many_events_is_fail_medium(self) -> None:
        status, severity, _ = classify_log_injection_downstream(
            5,
            sample_messages=["a", "b", "c", "d", "e"],
            payload_id="logi-crlf-fake-entry",
        )
        assert status == "fail"
        assert severity == SEVERITY_MEDIUM

    def test_one_event_with_csi_ansi_is_fail_low(self) -> None:
        """ANSI control sequence preserved in the log line — low severity."""
        sample_with_csi = "marker--\x1b[2K[ATTACKER] hidden--marker"
        status, severity, reason = classify_log_injection_downstream(
            1,
            sample_messages=[sample_with_csi],
            payload_id="logi-ansi-erase-line",
        )
        assert status == "fail"
        assert severity == SEVERITY_LOW
        assert "logi-ansi-erase-line" in reason
        assert "ansi" in reason.lower()

    def test_one_event_with_color_ansi_is_fail_low(self) -> None:
        sample_with_color = "marker--\x1b[31m[WARN]\x1b[0m system--marker"
        status, severity, _ = classify_log_injection_downstream(
            1,
            sample_messages=[sample_with_color],
            payload_id="logi-ansi-color-fake-warning",
        )
        assert status == "fail"
        assert severity == SEVERITY_LOW

    def test_one_event_with_osc_ansi_is_fail_low(self) -> None:
        """OSC (Operating System Command) escape — also flagged."""
        sample_with_osc = "marker--\x1b]0;hijacked title\x07--marker"
        status, severity, _ = classify_log_injection_downstream(
            1,
            sample_messages=[sample_with_osc],
            payload_id="logi-test-osc",
        )
        assert status == "fail"
        assert severity == SEVERITY_LOW

    def test_one_event_with_only_word_ansi_is_pass(self) -> None:
        """The literal word "ANSI" in a log message is NOT a finding —
        the classifier only flags real control bytes."""
        sample = "marker--user typed: 'ANSI escape codes are fun'--marker"
        status, severity, _ = classify_log_injection_downstream(
            1, sample_messages=[sample], payload_id="logi-no-real-escape"
        )
        assert status == "pass"
        assert severity is None

    def test_empty_sample_with_one_event_is_pass(self) -> None:
        """Defensive: if the caller passes 1 event but no sample, classify
        as PASS — we have no evidence of ANSI residue."""
        status, severity, _ = classify_log_injection_downstream(
            1, sample_messages=[], payload_id="logi-empty"
        )
        assert status == "pass"
        assert severity is None


# ────────────── builder.py accepts layer="logging_audit" ────────────────────


def test_builder_accepts_logging_audit_layer() -> None:
    """Pin that ``layer="logging_audit"`` round-trips through the builder.

    The builder treats ``layer`` as an opaque string today; this test
    pins that contract so the rest of the harness can write the layer's
    results without an exception.
    """
    minimal_manifest = {
        "personas": [
            {"id": "ciso"},
            {"id": "soc"},
            {"id": "grc"},
            {"id": "employee"},
        ],
        "pages": [],
        "api_routes": [
            {"id": "get-token-usage", "path": "/token-usage", "method": "GET"},
        ],
        "agent_tools": [],
    }
    results = [
        TestResult(
            test_id="logging.security-event.forged-token",
            status=CellStatus.PASS,
            layer="logging_audit",
            target_kind="api_route",
            target_id="get-token-usage",
        ),
    ]
    matrix = build_matrix(minimal_manifest, results)
    assert "get-token-usage" in matrix.api_routes
    assert len(matrix.api_routes["get-token-usage"]) == 1
    assert matrix.api_routes["get-token-usage"][0]["layer"] == "logging_audit"


def test_builder_logging_audit_in_layer_constant() -> None:
    """Pin that ``_LAYERS`` in builder.py includes ``"logging_audit"``.

    The ``load_results`` loader walks this tuple to find layer subdirs
    under the run dir; missing it would silently drop the layer's
    results.json.
    """
    from src.coverage.builder import _LAYERS

    assert "logging_audit" in _LAYERS


# ────────────── run_all wiring: layer + budget + cap ───────────────────────


def test_run_all_layers_all_contains_logging_audit() -> None:
    """Pin that ``scripts/run_all.py::_LAYERS_ALL`` includes the new layer.

    Without this entry the orchestrator would never spawn the layer's
    pytest subprocess.
    """
    from scripts.run_all import _LAYERS_ALL

    assert "logging_audit" in _LAYERS_ALL


def test_run_all_logging_audit_has_hard_cap() -> None:
    """Pin the 600 s hard cap on the layer's wall-clock.

    CloudWatch FilterLogEvents queries can legitimately take 10+ s each;
    a too-low cap would force premature kills. A too-high cap would let
    a wedged run keep hammering the dev env. 600 s is the documented value.
    """
    from scripts.run_all import _LAYER_HARD_CAPS_SECONDS

    assert "logging_audit" in _LAYER_HARD_CAPS_SECONDS
    assert _LAYER_HARD_CAPS_SECONDS["logging_audit"] == 600.0


def test_run_all_logging_audit_budget_is_zero() -> None:
    """Pin the LayerBudget shape: 0 input / 0 output tokens.

    The layer makes no Bedrock calls; any non-zero budget would inflate
    the cost-preflight estimate.
    """
    from scripts.run_all import _build_layer_budgets

    budgets = _build_layer_budgets(["logging_audit"])
    assert "logging_audit" in budgets
    budget = budgets["logging_audit"]
    assert budget.max_input_tokens == 0
    assert budget.max_output_tokens == 0


# ────────────── npm script wiring: package.json ───────────────────────────


def test_package_json_has_test_logging_script() -> None:
    """Pin that ``tests-adversarial/package.json`` has a ``test:logging`` script.

    The orchestrator runs Python layers via ``python -m pytest``, but the
    docs + standalone ``npm run test:logging`` path is the canonical entry
    point for the layer — a missing script would surprise operators.
    """
    import json
    from pathlib import Path

    harness_root = Path(__file__).resolve().parent.parent
    pkg = json.loads((harness_root / "package.json").read_text(encoding="utf-8"))
    scripts = pkg.get("scripts") or {}
    assert "test:logging" in scripts
    assert "logging_audit" in scripts["test:logging"]
