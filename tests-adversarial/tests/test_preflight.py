"""Smoke tests for src/cost/preflight.py — the AC4 mechanism.

Covers:
  - estimate math is correct for a single layer (per-million convention).
  - estimate sums across multiple layers and reports the largest.
  - preflight() raises BudgetExceededError when estimate > cap and names
    estimate / cap / largest layer.
  - preflight() passes silently when under cap.
  - AC4 alignment: cap=0.01 against a realistic default layout raises before
    any network call.
  - BudgetExceededError message contains the literal phrase 'budget exceeded'
    so a strict case-sensitive grep finds it.
"""
from __future__ import annotations

import pytest

from src.cost import preflight


# Synthetic pricing table used to monkeypatch pricing.load_pricing() so these
# tests don't depend on the real MODEL_PRICING values (which can shift if a
# new model is added or AWS reprices). Numbers chosen so the math is easy:
#   nova:   $1.00 per 1M input,  $4.00 per 1M output
#   claude: $3.00 per 1M input, $15.00 per 1M output
_FAKE_PRICING = {
    "us.amazon.nova-2-lite-v1:0": {"input": 1.0, "output": 4.0},
    "us.anthropic.claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
}


@pytest.fixture(autouse=True)
def _patch_pricing(monkeypatch):
    """Replace pricing.load_pricing() with the synthetic table for every test
    in this module. Keeps the math deterministic and decoupled from the live
    MODEL_PRICING values."""
    from src.cost import pricing as pricing_module

    monkeypatch.setattr(pricing_module, "load_pricing", lambda: dict(_FAKE_PRICING))


def test_estimate_single_layer_math():
    """1M input + 1M output on nova at $1 / $4 per million = $5.00 exactly."""
    budgets = {
        "llm": preflight.LayerBudget(
            name="llm",
            max_input_tokens=1_000_000,
            max_output_tokens=1_000_000,
            model_id="us.amazon.nova-2-lite-v1:0",
        ),
    }
    est = preflight.estimate_cost(budgets)
    assert est.total_usd == pytest.approx(5.0)
    assert est.per_layer == {"llm": pytest.approx(5.0)}
    assert est.largest_layer == "llm"


def test_estimate_per_million_convention_with_smaller_token_counts():
    """500k input + 250k output on nova = 0.5*$1 + 0.25*$4 = $1.50."""
    budgets = {
        "llm": preflight.LayerBudget(
            name="llm",
            max_input_tokens=500_000,
            max_output_tokens=250_000,
            model_id="us.amazon.nova-2-lite-v1:0",
        ),
    }
    est = preflight.estimate_cost(budgets)
    assert est.total_usd == pytest.approx(1.5)


def test_estimate_sums_multiple_layers_and_picks_largest():
    """Two layers contribute different amounts; total = sum; largest = the
    heavier one named correctly."""
    budgets = {
        "llm": preflight.LayerBudget(
            name="llm",
            max_input_tokens=1_000_000,
            max_output_tokens=1_000_000,
            model_id="us.anthropic.claude-sonnet-4-6",
        ),  # 3 + 15 = $18
        "e2e": preflight.LayerBudget(
            name="e2e",
            max_input_tokens=100_000,
            max_output_tokens=100_000,
            model_id="us.amazon.nova-2-lite-v1:0",
        ),  # 0.1 + 0.4 = $0.50
    }
    est = preflight.estimate_cost(budgets)
    assert est.per_layer["llm"] == pytest.approx(18.0)
    assert est.per_layer["e2e"] == pytest.approx(0.5)
    assert est.total_usd == pytest.approx(18.5)
    assert est.largest_layer == "llm"


def test_default_model_id_is_nova_lite():
    """Per CLAUDE.local.md, the project-wide default model_id for new LayerBudgets
    is Amazon Nova 2 Lite. Layers built without explicit model_id should pick it up."""
    budget = preflight.LayerBudget(
        name="auth",
        max_input_tokens=10,
        max_output_tokens=10,
    )
    assert budget.model_id == "us.amazon.nova-2-lite-v1:0"


def test_preflight_passes_silently_under_cap():
    """A cheap layer well under the cap must not raise."""
    budgets = {
        "auth": preflight.LayerBudget(
            name="auth",
            max_input_tokens=1000,
            max_output_tokens=1000,
            model_id="us.amazon.nova-2-lite-v1:0",
        ),  # very small cost
    }
    # Should return None without raising.
    assert preflight.preflight(cap_usd=1.0, layer_budgets=budgets) is None


def test_preflight_raises_when_over_cap_with_named_details():
    """Estimate > cap raises BudgetExceededError; message names estimate, cap,
    and the largest-contributing layer."""
    budgets = {
        "llm": preflight.LayerBudget(
            name="llm",
            max_input_tokens=1_000_000,
            max_output_tokens=1_000_000,
            model_id="us.anthropic.claude-sonnet-4-6",
        ),  # $18
        "e2e": preflight.LayerBudget(
            name="e2e",
            max_input_tokens=1000,
            max_output_tokens=1000,
            model_id="us.amazon.nova-2-lite-v1:0",
        ),  # ~$0.005
    }
    with pytest.raises(preflight.BudgetExceededError) as exc_info:
        preflight.preflight(cap_usd=1.0, layer_budgets=budgets)

    message = str(exc_info.value)
    # AC: name the largest layer
    assert "llm" in message
    # AC: name the estimate (some form of the dollar amount)
    assert "18" in message
    # AC: name the cap
    assert "1.0" in message or "1.00" in message


def test_budget_exceeded_message_contains_literal_phrase():
    """A strict case-sensitive grep for 'budget exceeded' must find the message —
    this is how operators discover the error class in shell output."""
    budgets = {
        "llm": preflight.LayerBudget(
            name="llm",
            max_input_tokens=1_000_000,
            max_output_tokens=1_000_000,
            model_id="us.amazon.nova-2-lite-v1:0",
        ),  # $5
    }
    with pytest.raises(preflight.BudgetExceededError) as exc_info:
        preflight.preflight(cap_usd=0.01, layer_budgets=budgets)

    assert "budget exceeded" in str(exc_info.value)


def test_ac4_default_layout_raises_at_one_cent_cap():
    """AC4 alignment: setting cap to $0.01 against a default layer layout
    representative of the harness's actual run must raise before any network
    call. The numbers below model the spec §5 budgets:
      - e2e:   4 chat turns on Nova
      - fuzz:  no Bedrock calls (token counts ~0)
      - auth:  ~2 worst-case calls on Nova
      - llm:   30 probes on Nova
    Total comes out > $0.01 trivially."""
    budgets = {
        "e2e": preflight.LayerBudget(
            name="e2e",
            max_input_tokens=4 * 2_000,
            max_output_tokens=4 * 500,
            model_id="us.amazon.nova-2-lite-v1:0",
        ),
        "fuzz": preflight.LayerBudget(
            name="fuzz",
            max_input_tokens=0,
            max_output_tokens=0,
            model_id="us.amazon.nova-2-lite-v1:0",
        ),
        "auth": preflight.LayerBudget(
            name="auth",
            max_input_tokens=2 * 2_000,
            max_output_tokens=2 * 500,
            model_id="us.amazon.nova-2-lite-v1:0",
        ),
        "llm": preflight.LayerBudget(
            name="llm",
            max_input_tokens=30 * 2_000,
            max_output_tokens=30 * 500,
            model_id="us.amazon.nova-2-lite-v1:0",
        ),
    }

    with pytest.raises(preflight.BudgetExceededError):
        preflight.preflight(cap_usd=0.01, layer_budgets=budgets)


def test_estimate_with_empty_layer_dict_is_zero():
    """Defensive: passing no layers yields zero cost and empty largest_layer.
    preflight() with $0 estimate under any non-negative cap should pass."""
    est = preflight.estimate_cost({})
    assert est.total_usd == 0.0
    assert est.per_layer == {}
    assert est.largest_layer == ""
    # preflight passes silently with zero estimate.
    assert preflight.preflight(cap_usd=0.0, layer_budgets={}) is None


def test_unknown_model_id_in_layer_budget_raises_keyerror():
    """If a LayerBudget uses a model_id absent from MODEL_PRICING, estimate
    must raise rather than silently fall through to zero — that would defeat
    the cap."""
    budgets = {
        "llm": preflight.LayerBudget(
            name="llm",
            max_input_tokens=1000,
            max_output_tokens=1000,
            model_id="bogus.model-id-not-priced",
        ),
    }
    with pytest.raises(KeyError) as exc_info:
        preflight.estimate_cost(budgets)
    assert "bogus.model-id-not-priced" in str(exc_info.value)


def test_pricing_drift_bubbles_up_from_preflight(monkeypatch):
    """If MODEL_PRICING disagrees between sources, PricingDriftError must
    bubble up from preflight() unchanged (no swallowing into BudgetExceededError)."""
    from src.cost import pricing as pricing_module

    def raise_drift():
        raise pricing_module.PricingDriftError("synthetic drift for test")

    monkeypatch.setattr(pricing_module, "load_pricing", raise_drift)

    budgets = {
        "llm": preflight.LayerBudget(
            name="llm",
            max_input_tokens=1000,
            max_output_tokens=1000,
        ),
    }
    with pytest.raises(pricing_module.PricingDriftError):
        preflight.preflight(cap_usd=10.0, layer_budgets=budgets)
