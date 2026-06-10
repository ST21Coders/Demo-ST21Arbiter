"""Unit tests for the DoS / rate-limit layer's classifiers + CLI clamps (Block E).

These tests exercise the pure functions in `dos/classifiers.py` with
synthetic inputs. No network calls; no `requests.Session`. The contract is:

  * Every classifier returns ``(status, severity, reason)``.
  * ``status`` is exactly ``"pass"`` or ``"fail"``.
  * ``severity`` is one of ``"low" | "medium" | "high"`` on FAIL and
    ``None`` on PASS.
  * ``reason`` is a non-empty human-readable string.

Coverage:

  * `classify_rate_limit_burst`:
      - sequence with at least one 429 → PASS
      - all 200, monotonic latency growth → FAIL HIGH
      - all 200, flat latency → FAIL MEDIUM
      - any 500 → FAIL HIGH
      - any zero-status (transport drop) → FAIL HIGH
      - empty inputs → FAIL MEDIUM
      - length mismatch → ValueError
  * `classify_oversize_payload`:
      - 10 MB returns 413 → PASS
      - 10 MB latency 2x baseline + 200 status → PASS
      - 10 MB latency 6x baseline + 200 → FAIL MEDIUM
      - 10 MB latency 10x baseline + 200 → FAIL HIGH
      - `timed_out=True` → FAIL HIGH
      - 5xx oversize status → FAIL HIGH
      - zero-baseline edge case → falls through cleanly
  * `classify_concurrent_burst`:
      - 10/10 return 200 → PASS
      - 10/10 mix of 200/429/503 → PASS
      - 1/10 returns 500 → FAIL HIGH
      - 1/10 returns malformed JSON → FAIL MEDIUM
      - 1/10 returns 400 (not 429) → FAIL MEDIUM
      - empty inputs → FAIL MEDIUM
  * Clamps:
      - `--dos-rps` hard ceiling at 100
      - `--dos-duration-seconds` hard ceiling at 30
      - Negative / zero values clamped to 1

Also pins `builder.py`'s acceptance of ``layer="dos"`` so the rest of the
harness writes the layer's results without an exception.
"""

from __future__ import annotations

import pytest

from dos.classifiers import (
    DOS_DURATION_HARD_CEILING_SECONDS,
    DOS_RPS_HARD_CEILING,
    SEVERITY_HIGH,
    SEVERITY_MEDIUM,
    clamp_duration_seconds,
    clamp_rps,
    classify_concurrent_burst,
    classify_oversize_payload,
    classify_rate_limit_burst,
)
from src.coverage.builder import CellStatus, TestResult, build_matrix


# ────────────────────── classify_rate_limit_burst ────────────────────────────


def test_rate_limit_pass_when_any_429_observed():
    """At least one 429 inside an otherwise-healthy sequence is the PASS
    signal — the API has a rate limiter and it engaged."""
    status_codes = [200] * 50 + [429] * 5 + [200] * 45
    latencies_ms = [50.0] * 100
    status, severity, reason = classify_rate_limit_burst(status_codes, latencies_ms)
    assert status == "pass"
    assert severity is None
    assert "429" in reason


def test_rate_limit_fail_high_on_monotonic_latency_growth():
    """All 200s with the last quartile of latencies > 2× the first quartile
    is the buckling-without-throttle signal."""
    # First quartile mean ~50ms, last quartile mean ~500ms — 10× ratio.
    status_codes = [200] * 100
    latencies_ms = [50.0] * 75 + [500.0] * 25
    status, severity, _ = classify_rate_limit_burst(status_codes, latencies_ms)
    assert status == "fail"
    assert severity == SEVERITY_HIGH


def test_rate_limit_fail_medium_on_flat_latency_no_throttle():
    """All 200s with flat latency = no rate limiting at all (no protection,
    but the API also isn't buckling). FAIL MEDIUM by convention."""
    status_codes = [200] * 100
    latencies_ms = [50.0] * 100
    status, severity, _ = classify_rate_limit_burst(status_codes, latencies_ms)
    assert status == "fail"
    assert severity == SEVERITY_MEDIUM


def test_rate_limit_fail_high_on_any_500():
    """A single 500 inside the burst is the crash signal — FAIL HIGH
    regardless of throttle behaviour."""
    status_codes = [200] * 50 + [500] + [200] * 49
    latencies_ms = [50.0] * 100
    status, severity, reason = classify_rate_limit_burst(status_codes, latencies_ms)
    assert status == "fail"
    assert severity == SEVERITY_HIGH
    assert "500" in reason or "server" in reason


def test_rate_limit_fail_high_on_transport_drop():
    """A zero-status (connection drop / timeout) is treated the same as a
    server error: FAIL HIGH."""
    status_codes = [200] * 50 + [0] + [200] * 49
    latencies_ms = [50.0] * 100
    status, severity, _ = classify_rate_limit_burst(status_codes, latencies_ms)
    assert status == "fail"
    assert severity == SEVERITY_HIGH


def test_rate_limit_fail_medium_on_empty_inputs():
    """No responses recorded = something went wrong before the first
    request. Classifier returns FAIL MEDIUM with a clear reason."""
    status, severity, reason = classify_rate_limit_burst([], [])
    assert status == "fail"
    assert severity == SEVERITY_MEDIUM
    assert "no responses" in reason.lower()


def test_rate_limit_raises_on_length_mismatch():
    """Misaligned status/latency arrays are a programmer error, not a runtime
    finding — raise ValueError."""
    with pytest.raises(ValueError, match="same length"):
        classify_rate_limit_burst([200, 200], [50.0])


def test_rate_limit_short_sequence_falls_back_to_first_last():
    """For n<4 the quartile heuristic gives no signal; the classifier falls
    back to comparing last to first directly."""
    # 2-sample sequence with the last sample 5× the first — flagged HIGH.
    status_codes = [200, 200]
    latencies_ms = [10.0, 100.0]
    status, severity, _ = classify_rate_limit_burst(status_codes, latencies_ms)
    assert status == "fail"
    assert severity == SEVERITY_HIGH


def test_rate_limit_single_sample_no_buckle_signal():
    """A single 200 can't be a buckle signal — defaults to FAIL MEDIUM
    (no throttle observed)."""
    status, severity, _ = classify_rate_limit_burst([200], [50.0])
    assert status == "fail"
    assert severity == SEVERITY_MEDIUM


# ────────────────────── classify_oversize_payload ────────────────────────────


def test_oversize_pass_on_413_refusal():
    """Server rejected the oversized body with a 413 — that's the
    protective behaviour we want."""
    status, severity, reason = classify_oversize_payload(
        baseline_status=200,
        baseline_ms=50.0,
        oversize_status=413,
        oversize_ms=20.0,
    )
    assert status == "pass"
    assert severity is None
    assert "413" in reason


def test_oversize_pass_on_400_refusal():
    """Any 4xx rejection counts as PASS — the API said "no" cleanly."""
    status, severity, _ = classify_oversize_payload(
        baseline_status=200,
        baseline_ms=50.0,
        oversize_status=400,
        oversize_ms=20.0,
    )
    assert status == "pass"
    assert severity is None


def test_oversize_pass_when_latency_under_medium_threshold():
    """200 status + latency only 2× baseline (< 3× medium threshold) is
    PASS — the API absorbed the oversize body without degrading."""
    status, severity, _ = classify_oversize_payload(
        baseline_status=200,
        baseline_ms=100.0,
        oversize_status=200,
        oversize_ms=200.0,
    )
    assert status == "pass"
    assert severity is None


def test_oversize_fail_medium_when_latency_between_medium_and_high():
    """200 status + latency 4× baseline (between 3× MEDIUM and 5× HIGH
    thresholds) = degraded but not refused. FAIL MEDIUM."""
    status, severity, _ = classify_oversize_payload(
        baseline_status=200,
        baseline_ms=100.0,
        oversize_status=200,
        oversize_ms=400.0,
    )
    assert status == "fail"
    assert severity == SEVERITY_MEDIUM


def test_oversize_fail_high_when_latency_over_high_threshold():
    """200 status + latency 10× baseline = the API buckled. FAIL HIGH."""
    status, severity, _ = classify_oversize_payload(
        baseline_status=200,
        baseline_ms=100.0,
        oversize_status=200,
        oversize_ms=1000.0,
    )
    assert status == "fail"
    assert severity == SEVERITY_HIGH


def test_oversize_fail_high_on_timeout():
    """`timed_out=True` is always FAIL HIGH regardless of other fields."""
    status, severity, reason = classify_oversize_payload(
        baseline_status=200,
        baseline_ms=100.0,
        oversize_status=0,
        oversize_ms=30_000.0,
        timed_out=True,
    )
    assert status == "fail"
    assert severity == SEVERITY_HIGH
    assert "timed out" in reason.lower() or "timeout" in reason.lower()


def test_oversize_fail_high_on_zero_status():
    """oversize_status=0 (transport error) is treated like a timeout —
    FAIL HIGH."""
    status, severity, _ = classify_oversize_payload(
        baseline_status=200,
        baseline_ms=100.0,
        oversize_status=0,
        oversize_ms=5_000.0,
    )
    assert status == "fail"
    assert severity == SEVERITY_HIGH


def test_oversize_fail_high_on_5xx():
    """Server error on the oversize call = the API crashed on a body it
    couldn't reject. FAIL HIGH."""
    status, severity, _ = classify_oversize_payload(
        baseline_status=200,
        baseline_ms=100.0,
        oversize_status=502,
        oversize_ms=500.0,
    )
    assert status == "fail"
    assert severity == SEVERITY_HIGH


def test_oversize_zero_baseline_does_not_crash():
    """Baseline=0 (transport error on the warm-up call) shouldn't divide-by-
    zero; the classifier falls back to an absolute threshold."""
    status, severity, _ = classify_oversize_payload(
        baseline_status=0,
        baseline_ms=0.0,
        oversize_status=200,
        oversize_ms=500.0,
    )
    # No usable baseline → ratio is infinity → FAIL HIGH.
    assert status == "fail"
    assert severity == SEVERITY_HIGH


# ──────────────────── classify_concurrent_burst ──────────────────────────────


def test_concurrent_pass_when_all_200():
    """10/10 clean 200s = PASS."""
    status, severity, _ = classify_concurrent_burst([200] * 10)
    assert status == "pass"
    assert severity is None


def test_concurrent_pass_with_mix_of_200_429_503():
    """A mix of 200, 429, 503 is still PASS — those are all structured
    responses the API knows how to emit under load."""
    status, severity, _ = classify_concurrent_burst(
        [200, 200, 429, 503, 200, 429, 200, 200, 503, 200]
    )
    assert status == "pass"
    assert severity is None


def test_concurrent_fail_high_on_any_500():
    """A single 500 in the fan-out is FAIL HIGH — server crash."""
    status_codes = [200] * 9 + [500]
    status, severity, reason = classify_concurrent_burst(status_codes)
    assert status == "fail"
    assert severity == SEVERITY_HIGH
    assert "500" in reason or "server" in reason.lower()


def test_concurrent_fail_high_on_transport_drop():
    """A zero-status (dropped connection) is FAIL HIGH."""
    status_codes = [200] * 9 + [0]
    status, severity, _ = classify_concurrent_burst(status_codes)
    assert status == "fail"
    assert severity == SEVERITY_HIGH


def test_concurrent_fail_medium_on_malformed_response():
    """1/10 returned a malformed body (truncated JSON / wrong content-type)
    while the status code was clean — FAIL MEDIUM."""
    status, severity, reason = classify_concurrent_burst(
        [200] * 10,
        malformed_response_count=1,
    )
    assert status == "fail"
    assert severity == SEVERITY_MEDIUM
    assert "malformed" in reason.lower()


def test_concurrent_fail_medium_on_unexpected_4xx():
    """A 400 (well-formed request, server says it's bad) under concurrency
    is FAIL MEDIUM — the request was identical to the others, so the 400
    is improper."""
    status, severity, _ = classify_concurrent_burst([200] * 9 + [400])
    assert status == "fail"
    assert severity == SEVERITY_MEDIUM


def test_concurrent_fail_medium_on_empty_inputs():
    """No status codes recorded = setup failed. FAIL MEDIUM with a clear
    reason."""
    status, severity, reason = classify_concurrent_burst([])
    assert status == "fail"
    assert severity == SEVERITY_MEDIUM
    assert "no concurrent responses" in reason.lower()


# ─────────────────────────── clamp helpers ───────────────────────────────────


def test_clamp_rps_hard_ceiling_at_100():
    """Values above 100 silently cap to 100 — the safety floor."""
    assert clamp_rps(500) == DOS_RPS_HARD_CEILING == 100
    assert clamp_rps(101) == 100
    assert clamp_rps(1_000_000) == 100


def test_clamp_rps_passthrough_below_ceiling():
    assert clamp_rps(20) == 20
    assert clamp_rps(99) == 99
    assert clamp_rps(100) == 100


def test_clamp_rps_floor_at_1():
    """Negative or zero RPS clamps to 1 — pytest wouldn't make a useful
    burst out of zero requests/sec."""
    assert clamp_rps(0) == 1
    assert clamp_rps(-5) == 1


def test_clamp_duration_hard_ceiling_at_30():
    """Values above 30 silently cap to 30 — the safety floor."""
    assert clamp_duration_seconds(60) == DOS_DURATION_HARD_CEILING_SECONDS == 30
    assert clamp_duration_seconds(31) == 30
    assert clamp_duration_seconds(3600) == 30


def test_clamp_duration_passthrough_below_ceiling():
    assert clamp_duration_seconds(5) == 5
    assert clamp_duration_seconds(30) == 30


def test_clamp_duration_floor_at_1():
    """Zero or negative duration clamps to 1 second — anything shorter is
    a no-op."""
    assert clamp_duration_seconds(0) == 1
    assert clamp_duration_seconds(-10) == 1


# ────────────── builder.py accepts layer="dos" ──────────────────────────────


def test_builder_accepts_dos_layer():
    """Pin that `layer="dos"` is allowed by the validation. The builder
    treats `layer` as an opaque string today, so this should round-trip
    without complaint — but if someone ever adds an allow-list, this test
    will catch the regression.
    """
    manifest = {
        "personas": [
            {"id": "ciso", "username": "x@y.z", "cognito_group": "ciso", "label": "x"},
            {"id": "soc", "username": "x@y.z", "cognito_group": "soc", "label": "x"},
            {"id": "grc", "username": "x@y.z", "cognito_group": "grc", "label": "x"},
            {"id": "employee", "username": "x@y.z", "cognito_group": "e", "label": "x"},
        ],
        "pages": [],
        "api_routes": [
            {
                "id": "get-findings",
                "method": "GET",
                "path": "/findings",
                "file": "x",
                "auth_required": True,
            }
        ],
        "agent_tools": [],
    }
    results = [
        TestResult(
            test_id="dos.rate-limit.get-findings",
            status=CellStatus.PASS,
            layer="dos",
            target_kind="api_route",
            target_id="get-findings",
        ),
    ]
    matrix = build_matrix(manifest, results)
    assert len(matrix.api_routes["get-findings"]) == 1
    assert matrix.api_routes["get-findings"][0]["layer"] == "dos"


def test_builder_dos_layer_in_layer_constant():
    """Pin that `_LAYERS` in builder.py includes "dos". The `load_results`
    loader walks this tuple to find layer subdirectories under the run dir.
    """
    from src.coverage.builder import _LAYERS

    assert "dos" in _LAYERS
