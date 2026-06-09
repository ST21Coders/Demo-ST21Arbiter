"""Smoke tests for src/cost/tracker.py — the live cost + probe-count tally.

Covers:
  - records accumulate into per-layer USD totals (per-million convention).
  - probe count per layer is accurate.
  - unknown model_id raises with a clear message (no silent zero rows).
  - as_dict() shape matches what the reporter consumes.
  - the same probe across two layers does not bleed into the wrong count.
"""
from __future__ import annotations

import pytest

from src.cost.tracker import CostTracker


# Same synthetic table as test_preflight.py — easy math:
#   nova:   $1.00 / 1M input,  $4.00 / 1M output
#   claude: $3.00 / 1M input, $15.00 / 1M output
_FAKE_PRICING = {
    "us.amazon.nova-2-lite-v1:0": {"input": 1.0, "output": 4.0},
    "us.anthropic.claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
}


def test_single_record_accumulates_cost():
    """1M input + 1M output on nova should sum to $5.00."""
    tracker = CostTracker(pricing=_FAKE_PRICING)
    tracker.record(
        layer="llm",
        model_id="us.amazon.nova-2-lite-v1:0",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    )
    assert tracker.total_usd() == pytest.approx(5.0)
    assert tracker.per_layer_usd() == {"llm": pytest.approx(5.0)}


def test_multiple_records_same_layer_accumulate():
    """Two billed-response records in the same layer sum their costs.

    Note: `record()` no longer bumps the probe counter (post-task-17 split).
    Probe count is bumped only by `increment_probe()`, which the LLM probe
    budget fixture calls before each request. The two-record case here
    therefore expects probe_count == 0 unless `increment_probe` is also
    called — see `test_increment_probe_*` below.
    """
    tracker = CostTracker(pricing=_FAKE_PRICING)
    tracker.record("llm", "us.amazon.nova-2-lite-v1:0", 500_000, 500_000)  # $2.50
    tracker.record("llm", "us.amazon.nova-2-lite-v1:0", 500_000, 500_000)  # $2.50
    assert tracker.total_usd() == pytest.approx(5.0)
    # Regression guard: `record()` must NOT bump the probe counter.
    assert tracker.probe_count("llm") == 0


def test_record_does_not_bump_probe_count():
    """Regression guard for the task-17 split.

    Before task 17, `record()` did `probe_counts[layer] += 1` as a side
    effect. The carryover from task 3's reviewer pushed that bump into
    `increment_probe()` so a probe that errors before producing a billed
    response still counts against the AC16 cap. This test pins the new
    contract: `record()` is cost-only, never probe-count.
    """
    tracker = CostTracker(pricing=_FAKE_PRICING)
    tracker.record("llm", "us.amazon.nova-2-lite-v1:0", 1_000, 1_000)
    tracker.record("llm", "us.amazon.nova-2-lite-v1:0", 1_000, 1_000)
    tracker.record("llm", "us.amazon.nova-2-lite-v1:0", 1_000, 1_000)
    assert tracker.probe_count("llm") == 0


def test_increment_probe_bumps_count_and_returns_new_value():
    """`increment_probe()` returns the post-increment count for inline cap checks."""
    tracker = CostTracker(pricing=_FAKE_PRICING)
    assert tracker.increment_probe("llm") == 1
    assert tracker.increment_probe("llm") == 2
    assert tracker.increment_probe("llm") == 3
    assert tracker.probe_count("llm") == 3


def test_increment_probe_independent_of_record():
    """`increment_probe()` bumps the counter without touching cost totals."""
    tracker = CostTracker(pricing=_FAKE_PRICING)
    tracker.increment_probe("llm")
    tracker.increment_probe("llm")
    # No `record()` calls — cost must be zero.
    assert tracker.total_usd() == pytest.approx(0.0)
    assert tracker.probe_count("llm") == 2


def test_increment_probe_default_layer_is_llm():
    """The `layer` arg defaults to 'llm' since that's the only capped layer today."""
    tracker = CostTracker(pricing=_FAKE_PRICING)
    tracker.increment_probe()  # no layer arg
    tracker.increment_probe()
    assert tracker.probe_count("llm") == 2


def test_increment_probe_per_layer_isolation():
    """Bumping one layer must not bleed into another."""
    tracker = CostTracker(pricing=_FAKE_PRICING)
    tracker.increment_probe("llm")
    tracker.increment_probe("llm")
    tracker.increment_probe("auth")
    assert tracker.probe_count("llm") == 2
    assert tracker.probe_count("auth") == 1


def test_record_and_increment_probe_compose():
    """The intended usage pattern: increment_probe BEFORE the request, then
    record AFTER if billed tokens come back. Both totals accumulate."""
    tracker = CostTracker(pricing=_FAKE_PRICING)
    # Probe 1: bumped, billed.
    tracker.increment_probe("llm")
    tracker.record("llm", "us.amazon.nova-2-lite-v1:0", 1_000_000, 1_000_000)  # $5
    # Probe 2: bumped, errored before producing tokens (no record() call).
    tracker.increment_probe("llm")
    # Probe 3: bumped, billed.
    tracker.increment_probe("llm")
    tracker.record("llm", "us.amazon.nova-2-lite-v1:0", 100_000, 100_000)  # $0.50
    assert tracker.probe_count("llm") == 3
    assert tracker.total_usd() == pytest.approx(5.5)


def test_records_across_layers_keep_separate_totals():
    """Costs for different layers must not bleed into each other."""
    tracker = CostTracker(pricing=_FAKE_PRICING)
    tracker.record("llm", "us.anthropic.claude-sonnet-4-6", 1_000_000, 1_000_000)  # $18
    tracker.record("e2e", "us.amazon.nova-2-lite-v1:0", 100_000, 100_000)  # $0.50

    per_layer = tracker.per_layer_usd()
    assert per_layer["llm"] == pytest.approx(18.0)
    assert per_layer["e2e"] == pytest.approx(0.5)
    assert tracker.total_usd() == pytest.approx(18.5)


def test_probe_count_is_per_layer_not_global():
    """AC16 enforcement uses per-layer probe counts. Two probes in 'llm' and
    one in 'auth' must yield counts {llm: 2, auth: 1} — never blended."""
    tracker = CostTracker(pricing=_FAKE_PRICING)
    tracker.increment_probe("llm")
    tracker.increment_probe("llm")
    tracker.increment_probe("auth")

    assert tracker.probe_count("llm") == 2
    assert tracker.probe_count("auth") == 1


def test_probe_count_unknown_layer_returns_zero():
    """Querying a layer that has had no records must return 0 (not KeyError),
    since the orchestrator may check before the first probe."""
    tracker = CostTracker(pricing=_FAKE_PRICING)
    assert tracker.probe_count("llm") == 0
    assert tracker.probe_count("never-recorded") == 0


def test_unknown_model_id_raises_clear_message():
    """Recording with a model_id not in the pricing table must raise — silent
    zero would mean cost footer underreports and the cap is meaningless."""
    tracker = CostTracker(pricing=_FAKE_PRICING)
    with pytest.raises(KeyError) as exc_info:
        tracker.record(
            layer="llm",
            model_id="bogus.model-id-not-priced",
            input_tokens=1000,
            output_tokens=1000,
        )
    message = str(exc_info.value)
    assert "bogus.model-id-not-priced" in message


def test_negative_token_counts_clamped_to_zero():
    """Defensive: a negative token count (malformed telemetry) should not
    subtract from running totals — clamp at zero.

    After the task-17 split, `record()` no longer touches the probe counter;
    so a clamped row leaves probe_count untouched. Probe accounting is the
    fixture's job via `increment_probe()`.
    """
    tracker = CostTracker(pricing=_FAKE_PRICING)
    tracker.record("llm", "us.amazon.nova-2-lite-v1:0", -100, -100)
    # No cost added.
    assert tracker.total_usd() == pytest.approx(0.0)
    # Probe count untouched — `record()` is cost-only.
    assert tracker.probe_count("llm") == 0


def test_as_dict_shape_matches_reporter_contract():
    """as_dict() must produce exactly the keys the report builder consumes:
    total_usd (float), per_layer_usd (dict), probe_counts (dict).

    Probe counts come from `increment_probe()`, billed cost from `record()`;
    the snapshot composes both.
    """
    tracker = CostTracker(pricing=_FAKE_PRICING)
    tracker.increment_probe("llm")
    tracker.record("llm", "us.amazon.nova-2-lite-v1:0", 1_000_000, 1_000_000)  # $5
    tracker.increment_probe("e2e")
    tracker.record("e2e", "us.amazon.nova-2-lite-v1:0", 100_000, 100_000)  # $0.50

    snapshot = tracker.as_dict()
    assert set(snapshot.keys()) == {"total_usd", "per_layer_usd", "probe_counts"}
    assert snapshot["total_usd"] == pytest.approx(5.5)
    assert snapshot["per_layer_usd"] == {
        "llm": pytest.approx(5.0),
        "e2e": pytest.approx(0.5),
    }
    assert snapshot["probe_counts"] == {"llm": 1, "e2e": 1}


def test_per_layer_usd_returns_independent_copy():
    """Caller mutating the returned dict must not affect later snapshots —
    the report builder relies on getting a clean view."""
    tracker = CostTracker(pricing=_FAKE_PRICING)
    tracker.record("llm", "us.amazon.nova-2-lite-v1:0", 1_000_000, 1_000_000)

    snapshot = tracker.per_layer_usd()
    snapshot["llm"] = 999.99
    assert tracker.per_layer_usd()["llm"] == pytest.approx(5.0)


def test_pricing_table_defensive_copy_at_init():
    """If the caller mutates the pricing dict after instantiation, already-
    constructed trackers must still use the original rates."""
    mutable_pricing = {
        "us.amazon.nova-2-lite-v1:0": {"input": 1.0, "output": 4.0},
    }
    tracker = CostTracker(pricing=mutable_pricing)
    # Caller mutates after construction.
    mutable_pricing["us.amazon.nova-2-lite-v1:0"]["input"] = 999.0
    tracker.record("llm", "us.amazon.nova-2-lite-v1:0", 1_000_000, 0)
    # Cost reflects the original $1, not the mutated $999.
    assert tracker.total_usd() == pytest.approx(1.0)
