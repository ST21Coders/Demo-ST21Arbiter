"""Unit tests for the features (positive end-to-end) layer's classifiers + cost.

These tests exercise the pure functions in `features/classifiers.py` with
synthetic inputs. No network calls. The contract:

  * Every classifier returns ``(status, severity, reason)``.
  * ``status`` is exactly ``"pass"`` or ``"fail"``.
  * ``severity`` is one of ``"low" | "medium" | "high"`` on FAIL and
    ``None`` on PASS.
  * ``reason`` is a non-empty human-readable string.

Coverage:

  * `classify_chat_roundtrip`: PASS / FAIL HIGH / FAIL MEDIUM branches.
  * `classify_specialist_routing`: PASS / FAIL MEDIUM / FAIL HIGH branches.
  * `classify_conversation_persistence`: PASS / FAIL MEDIUM / FAIL HIGH.
  * `classify_token_usage_recorded`: PASS / FAIL HIGH (403) / FAIL MEDIUM.
  * `classify_kb_retrieval`: PASS / FAIL MEDIUM / FAIL HIGH branches.
  * `classify_chat_cost`: PASS / FAIL MEDIUM / FAIL HIGH / NaN / negative.
  * `compute_chat_cost_usd`: known token × price = expected USD.

Also pins `builder.py::_LAYERS` accepts ``"features"`` so layer wiring is
captured by a failing test if the constant is ever reverted.
"""

from __future__ import annotations

import pytest

from features.classifiers import (
    CHAT_LATENCY_BUDGET_SECONDS,
    COST_FAIL_HIGH_USD,
    COST_PASS_USD,
    MIN_REPLY_CHARS,
    SEVERITY_HIGH,
    SEVERITY_MEDIUM,
    classify_chat_cost,
    classify_chat_roundtrip,
    classify_conversation_persistence,
    classify_kb_retrieval,
    classify_specialist_routing,
    classify_token_usage_recorded,
    compute_chat_cost_usd,
)
from src.coverage.builder import CellStatus, TestResult, build_matrix


# ─────────────────────── classify_chat_roundtrip ────────────────────────────


def test_chat_roundtrip_pass_when_reply_meets_thresholds():
    """200 + long-enough reply + under-budget latency = PASS."""
    status, severity, reason = classify_chat_roundtrip(
        http_status=200,
        reply_text="The CISO persona can see the Dashboard and Findings pages.",
        latency_seconds=4.2,
    )
    assert status == "pass"
    assert severity is None
    assert "4.2" in reason


def test_chat_roundtrip_fail_high_on_5xx():
    """A 500 is the canonical "feature broken" signal."""
    status, severity, _ = classify_chat_roundtrip(500, "ignored", 1.0)
    assert status == "fail"
    assert severity == SEVERITY_HIGH


def test_chat_roundtrip_fail_high_on_transport_drop():
    """Zero status = connection drop / timeout — FAIL HIGH."""
    status, severity, reason = classify_chat_roundtrip(0, None, 30.5)
    assert status == "fail"
    assert severity == SEVERITY_HIGH
    assert "transport" in reason.lower()


def test_chat_roundtrip_fail_high_on_missing_reply():
    """200 but no reply field = high-severity break."""
    status, severity, _ = classify_chat_roundtrip(200, None, 1.0)
    assert status == "fail"
    assert severity == SEVERITY_HIGH


def test_chat_roundtrip_fail_medium_on_short_reply():
    """200 with a too-short reply is degraded, not broken — FAIL MEDIUM."""
    short = "ok."
    assert len(short) < MIN_REPLY_CHARS
    status, severity, reason = classify_chat_roundtrip(200, short, 2.0)
    assert status == "fail"
    assert severity == SEVERITY_MEDIUM
    assert "too short" in reason or "stub" in reason


def test_chat_roundtrip_fail_medium_on_slow_latency():
    """Reply is fine but latency over budget — FAIL MEDIUM."""
    long_reply = "x" * (MIN_REPLY_CHARS + 5)
    status, severity, reason = classify_chat_roundtrip(
        200, long_reply, CHAT_LATENCY_BUDGET_SECONDS + 5.0
    )
    assert status == "fail"
    assert severity == SEVERITY_MEDIUM
    assert "latency" in reason.lower() or "budget" in reason.lower()


def test_chat_roundtrip_fail_high_on_unexpected_4xx():
    """Any non-200 status (e.g. 401, 404) is high — chat is the master workflow."""
    status, severity, _ = classify_chat_roundtrip(401, None, 0.5)
    assert status == "fail"
    assert severity == SEVERITY_HIGH


# ───────────────────── classify_specialist_routing ──────────────────────────


def test_routing_pass_when_keyword_matches_sharepoint():
    """sharepoint reply mentioning 'policy' (one of the keywords) = PASS."""
    reply = "Our SharePoint policy mandates a 7-year data retention window."
    status, severity, reason = classify_specialist_routing(
        "master.sharepoint_lookup", 200, reply
    )
    assert status == "pass"
    assert severity is None
    assert "policy" in reason or "sharepoint" in reason.lower()


def test_routing_pass_case_insensitive_match():
    """Keyword matching is case-insensitive."""
    status, _, _ = classify_specialist_routing(
        "master.awsconfig_lookup", 200, "AWS CONFIG findings: 3 noncompliant."
    )
    assert status == "pass"


def test_routing_fail_medium_when_no_keyword_in_reply():
    """200 with a reply that doesn't mention any keyword — FAIL MEDIUM."""
    status, severity, _ = classify_specialist_routing(
        "master.zscaler_lookup", 200, "I don't have that information."
    )
    assert status == "fail"
    assert severity == SEVERITY_MEDIUM


def test_routing_fail_high_on_unknown_tool_id():
    """Programmer error — unknown tool id = FAIL HIGH."""
    status, severity, reason = classify_specialist_routing(
        "master.nope_lookup", 200, "anything"
    )
    assert status == "fail"
    assert severity == SEVERITY_HIGH
    assert "unknown" in reason.lower()


def test_routing_fail_high_on_5xx():
    """5xx on the routing probe = FAIL HIGH."""
    status, severity, _ = classify_specialist_routing("master.jira_lookup", 502, None)
    assert status == "fail"
    assert severity == SEVERITY_HIGH


def test_routing_fail_high_on_empty_reply():
    """200 with empty reply — FAIL HIGH (no signal at all)."""
    status, severity, _ = classify_specialist_routing("master.paloalto_lookup", 200, "")
    assert status == "fail"
    assert severity == SEVERITY_HIGH


def test_routing_fail_high_on_transport_drop():
    """Zero status = transport drop — FAIL HIGH."""
    status, severity, _ = classify_specialist_routing(
        "master.sharepoint_lookup", 0, None
    )
    assert status == "fail"
    assert severity == SEVERITY_HIGH


# ─────────────────── classify_conversation_persistence ──────────────────────


def test_persistence_pass_when_session_id_in_list():
    """Session id present in returned list — PASS."""
    status, severity, reason = classify_conversation_persistence(
        http_status=200,
        session_ids_in_list=["sess-A", "sess-B", "expected-id"],
        expected_session_id="expected-id",
    )
    assert status == "pass"
    assert severity is None
    assert "expected-id" in reason


def test_persistence_fail_medium_when_session_id_missing():
    """200 but the session id never appeared — FAIL MEDIUM."""
    status, severity, reason = classify_conversation_persistence(
        200, ["sess-A", "sess-B"], "expected-id"
    )
    assert status == "fail"
    assert severity == SEVERITY_MEDIUM
    assert "missing" in reason.lower()


def test_persistence_fail_high_on_5xx():
    """5xx on the list endpoint — FAIL HIGH."""
    status, severity, _ = classify_conversation_persistence(503, [], "expected-id")
    assert status == "fail"
    assert severity == SEVERITY_HIGH


def test_persistence_fail_high_on_transport_drop():
    """Zero status — FAIL HIGH."""
    status, severity, _ = classify_conversation_persistence(0, [], "expected-id")
    assert status == "fail"
    assert severity == SEVERITY_HIGH


def test_persistence_fail_high_on_empty_expected_id():
    """Programmer error — no expected_session_id passed. FAIL HIGH."""
    status, severity, _ = classify_conversation_persistence(200, ["s1"], "")
    assert status == "fail"
    assert severity == SEVERITY_HIGH


def test_persistence_fail_high_on_unexpected_404():
    """Any non-200 (e.g. 404) — FAIL HIGH."""
    status, severity, _ = classify_conversation_persistence(404, [], "expected-id")
    assert status == "fail"
    assert severity == SEVERITY_HIGH


# ──────────────────── classify_token_usage_recorded ─────────────────────────


def test_token_usage_pass_with_new_records():
    """200 and at least one new record — PASS."""
    status, severity, reason = classify_token_usage_recorded(200, 1)
    assert status == "pass"
    assert severity is None
    assert "1" in reason


def test_token_usage_pass_with_many_new_records():
    """Multiple new records — still PASS."""
    status, severity, _ = classify_token_usage_recorded(200, 5)
    assert status == "pass"
    assert severity is None


def test_token_usage_fail_medium_when_no_new_records():
    """200 but no new rows — FAIL MEDIUM."""
    status, severity, _ = classify_token_usage_recorded(200, 0)
    assert status == "fail"
    assert severity == SEVERITY_MEDIUM


def test_token_usage_fail_high_on_403():
    """403 means the CISO IdToken was rejected — FAIL HIGH (route broken)."""
    status, severity, reason = classify_token_usage_recorded(403, 0)
    assert status == "fail"
    assert severity == SEVERITY_HIGH
    assert "403" in reason or "CISO" in reason


def test_token_usage_fail_high_on_5xx():
    """5xx — FAIL HIGH."""
    status, severity, _ = classify_token_usage_recorded(500, 0)
    assert status == "fail"
    assert severity == SEVERITY_HIGH


def test_token_usage_fail_high_on_transport_drop():
    """Zero — FAIL HIGH."""
    status, severity, _ = classify_token_usage_recorded(0, 0)
    assert status == "fail"
    assert severity == SEVERITY_HIGH


# ────────────────────────── classify_kb_retrieval ───────────────────────────


def test_kb_pass_with_citation_marker():
    """Reply contains a `[1]` citation marker — PASS."""
    status, severity, _ = classify_kb_retrieval(
        200, "Per policy [1], retention is 7 years."
    )
    assert status == "pass"
    assert severity is None


def test_kb_pass_with_source_marker():
    """Reply contains 'Source:' phrasing — PASS."""
    status, _, _ = classify_kb_retrieval(
        200, "Retention is 7 years. Source: MIG-POL-DR doc."
    )
    assert status == "pass"


def test_kb_pass_with_gdpr_keyword():
    """Reply mentions GDPR (a known KB-corpus topic) — PASS."""
    status, _, _ = classify_kb_retrieval(
        200, "GDPR Article 5 mandates data minimization."
    )
    assert status == "pass"


def test_kb_fail_medium_on_generic_reply():
    """Reply lacks any grounding marker — FAIL MEDIUM."""
    status, severity, _ = classify_kb_retrieval(200, "I'm not sure about that, sorry.")
    assert status == "fail"
    assert severity == SEVERITY_MEDIUM


def test_kb_fail_high_on_5xx():
    """5xx — FAIL HIGH."""
    status, severity, _ = classify_kb_retrieval(503, None)
    assert status == "fail"
    assert severity == SEVERITY_HIGH


def test_kb_fail_high_on_empty_reply():
    """200 + empty reply — FAIL HIGH."""
    status, severity, _ = classify_kb_retrieval(200, "")
    assert status == "fail"
    assert severity == SEVERITY_HIGH


def test_kb_fail_high_on_transport_drop():
    """Zero — FAIL HIGH."""
    status, severity, _ = classify_kb_retrieval(0, None)
    assert status == "fail"
    assert severity == SEVERITY_HIGH


# ─────────────────────────── classify_chat_cost ─────────────────────────────


def test_cost_pass_under_one_cent():
    """Cost under the PASS band — PASS."""
    status, severity, reason = classify_chat_cost(0.001)
    assert status == "pass"
    assert severity is None
    assert "0.00" in reason or "$" in reason


def test_cost_pass_zero_cost():
    """Zero is under any cliff — PASS."""
    status, severity, _ = classify_chat_cost(0.0)
    assert status == "pass"
    assert severity is None


def test_cost_fail_medium_between_pass_and_high():
    """Cost between $0.01 and $0.10 — FAIL MEDIUM."""
    mid = (COST_PASS_USD + COST_FAIL_HIGH_USD) / 2
    status, severity, _ = classify_chat_cost(mid)
    assert status == "fail"
    assert severity == SEVERITY_MEDIUM


def test_cost_fail_high_at_cliff():
    """Cost at or above $0.10 — FAIL HIGH."""
    status, severity, _ = classify_chat_cost(COST_FAIL_HIGH_USD)
    assert status == "fail"
    assert severity == SEVERITY_HIGH


def test_cost_fail_high_far_above_cliff():
    """Cost well above the cliff — FAIL HIGH."""
    status, severity, reason = classify_chat_cost(1.0)
    assert status == "fail"
    assert severity == SEVERITY_HIGH
    assert "cliff" in reason or "broken" in reason


def test_cost_fail_high_on_nan():
    """NaN cost — FAIL HIGH."""
    status, severity, reason = classify_chat_cost(float("nan"))
    assert status == "fail"
    assert severity == SEVERITY_HIGH
    assert "NaN" in reason


def test_cost_fail_high_on_negative():
    """Negative cost — programmer error, FAIL HIGH."""
    status, severity, _ = classify_chat_cost(-0.001)
    assert status == "fail"
    assert severity == SEVERITY_HIGH


# ───────────────────────── compute_chat_cost_usd ────────────────────────────


def test_compute_cost_nova_lite_typical_chat():
    """1000 input + 500 output @ Nova 2 Lite pricing ($0.06/M in, $0.24/M out).

    Expected: (1000 * 0.06 + 500 * 0.24) / 1e6 = (60 + 120) / 1e6 = 1.8e-4 USD.
    """
    pricing = {"input": 0.06, "output": 0.24}
    cost = compute_chat_cost_usd(1000, 500, pricing)
    assert cost == pytest.approx(0.00018, rel=1e-6)


def test_compute_cost_claude_sonnet():
    """1000 input + 500 output @ Sonnet pricing ($3/M in, $15/M out).

    Expected: (1000 * 3 + 500 * 15) / 1e6 = (3000 + 7500) / 1e6 = 0.0105 USD.
    """
    pricing = {"input": 3.00, "output": 15.00}
    cost = compute_chat_cost_usd(1000, 500, pricing)
    assert cost == pytest.approx(0.0105, rel=1e-6)


def test_compute_cost_zero_tokens_is_zero():
    """0 tokens × any rate = $0."""
    pricing = {"input": 100.0, "output": 200.0}
    assert compute_chat_cost_usd(0, 0, pricing) == 0.0


def test_compute_cost_negative_tokens_clamped_to_zero():
    """Defensive: a wrong-shape result with negative counts → cost 0, not negative."""
    pricing = {"input": 0.06, "output": 0.24}
    assert compute_chat_cost_usd(-100, -50, pricing) == 0.0


def test_compute_cost_missing_keys_use_zero():
    """Pricing dict without input/output keys yields zero, not KeyError."""
    cost = compute_chat_cost_usd(1000, 500, {})
    assert cost == 0.0


# ───────────────────── builder/layer wiring guard ──────────────────────────


def test_features_layer_is_registered_in_builder():
    """`_LAYERS` in builder.py must include `"features"` so load_results
    walks the layer's results.json. Without this the orchestrator's matrix
    silently misses the features results.
    """
    from src.coverage.builder import _LAYERS

    assert "features" in _LAYERS


def test_features_layer_accepted_by_build_matrix(tmp_path):
    """A TestResult with layer="features" round-trips through build_matrix
    without raising. Uses target_kind="api_route" so the layer's positive
    smoke rows have a place to land.
    """
    manifest = {
        "personas": [{"id": "ciso"}],
        "pages": [],
        "api_routes": [{"id": "post-chat"}],
        "agent_tools": [],
    }
    results = [
        TestResult(
            test_id="features.chat-roundtrip.ciso",
            status=CellStatus.PASS,
            layer="features",
            target_kind="api_route",
            target_id="post-chat",
            duration_seconds=4.5,
        )
    ]
    matrix = build_matrix(manifest, results)
    assert matrix.api_routes["post-chat"][0]["layer"] == "features"
    assert matrix.api_routes["post-chat"][0]["status"] == "pass"
