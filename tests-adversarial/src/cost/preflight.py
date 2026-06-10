"""Pre-flight Bedrock cost estimator and cap enforcer.

This module is the AC4 mechanism. Before any network call to the deployed
runtime, the orchestrator declares each layer's worst-case token budget,
multiplies by the reconciled MODEL_PRICING rates, and refuses to start if
the total exceeds the configured cap. Bedrock pricing is per-million-tokens
(see agents/_shared/token_usage.py::compute_cost) so the math here uses the
same convention: `(tokens / 1_000_000) * rate_per_million`.

If MODEL_PRICING disagrees between its two source files, the underlying
PricingDriftError from src.cost.pricing bubbles up unchanged — the harness
must never start a paid run on disagreeing prices.

Public surface:
    @dataclass LayerBudget
    @dataclass Estimate
    BudgetExceededError(RuntimeError)
    estimate_cost(layer_budgets: dict[str, LayerBudget]) -> Estimate
    preflight(cap_usd: float, layer_budgets: dict[str, LayerBudget]) -> None
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from src.cost import pricing

# Default model_id for a LayerBudget — Amazon Nova 2 Lite is the project-wide
# default per CLAUDE.local.md ("Foundation model default is Amazon Nova 2 Lite
# (us.amazon.nova-2-lite-v1:0) on all 4 runtimes"). Each layer can override.
_DEFAULT_MODEL_ID = "us.amazon.nova-2-lite-v1:0"


class BudgetExceededError(RuntimeError):
    """Raised by `preflight()` when the estimated cost exceeds the cap.

    The message must contain the literal phrase 'budget exceeded' so a strict
    case-sensitive grep over the harness's error surface finds it. It also
    names the estimate, the cap, and the largest-contributing layer so the
    operator knows where the budget went.
    """


@dataclass(frozen=True)
class LayerBudget:
    """Worst-case token budget declared by a single layer for pre-flight.

    `name` is a short layer label (e.g. "llm", "e2e", "fuzz", "auth") used as
    the key in per-layer breakdowns and in the error message's
    largest-contributor field.

    `model_id` selects the row from MODEL_PRICING. Defaults to Nova 2 Lite,
    the project-wide default. Override per-layer if a runtime is known to be
    on a more expensive model (e.g. master_orchestrator on Claude Sonnet 4.6).

    `max_input_tokens` / `max_output_tokens` are upper-bound counts for the
    whole layer (not per-probe). The estimator multiplies these directly.
    """

    name: str
    max_input_tokens: int
    max_output_tokens: int
    model_id: str = _DEFAULT_MODEL_ID


@dataclass(frozen=True)
class Estimate:
    """Output of `estimate_cost`. Sums and per-layer breakdown in USD.

    `largest_layer` is the layer name with the highest contribution; empty
    string if there were no layers (defensive — preflight() would never call
    with an empty dict in practice).
    """

    total_usd: float
    per_layer: dict[str, float] = field(default_factory=dict)
    largest_layer: str = ""


def _cost_for_layer(
    budget: LayerBudget,
    rates: Mapping[str, Mapping[str, float]],
) -> float:
    """Compute the USD cost of one layer given the reconciled pricing table.

    Mirrors the per-million convention from agents/_shared/token_usage.py::
    compute_cost so the harness's estimate is shape-compatible with the
    agent-side actual write. Unknown model_id is a programmer error here
    (the harness controls its own budgets), so we raise rather than silently
    fall through to zero — that would defeat the cap.
    """
    model_rates = rates.get(budget.model_id)
    if model_rates is None:
        raise KeyError(
            f"layer '{budget.name}' uses model_id '{budget.model_id}' which is "
            f"not in the reconciled MODEL_PRICING table (known models: "
            f"{sorted(rates.keys())}). Either update the layer's model_id or "
            f"add the model to both MODEL_PRICING source files."
        )
    input_cost = (max(0, budget.max_input_tokens) / 1_000_000.0) * float(
        model_rates["input"]
    )
    output_cost = (max(0, budget.max_output_tokens) / 1_000_000.0) * float(
        model_rates["output"]
    )
    return input_cost + output_cost


def estimate_cost(layer_budgets: Mapping[str, LayerBudget]) -> Estimate:
    """Sum the per-layer worst-case cost from MODEL_PRICING rates.

    Reads pricing via `pricing.load_pricing()` so any drift between the two
    MODEL_PRICING source files raises PricingDriftError here (before
    preflight enforces its cap). Returns an Estimate with the layer-name
    that contributed the most so the cap error can point a finger.
    """
    rates = pricing.load_pricing()

    per_layer: dict[str, float] = {}
    for layer_name, budget in layer_budgets.items():
        per_layer[layer_name] = _cost_for_layer(budget, rates)

    total = sum(per_layer.values())
    largest = max(per_layer, key=per_layer.get) if per_layer else ""

    return Estimate(total_usd=total, per_layer=per_layer, largest_layer=largest)


def preflight(cap_usd: float, layer_budgets: Mapping[str, LayerBudget]) -> None:
    """Estimate total cost; raise BudgetExceededError if it exceeds `cap_usd`.

    This is the gate the orchestrator calls before any deployed-env network
    call. On pass, returns silently. On fail, raises with a message that
    names the estimate, the cap, and the largest-contributing layer so the
    operator can see at a glance which layer to trim (or whether to raise
    the cap deliberately — which is a human decision, not the harness's).
    """
    est = estimate_cost(layer_budgets)
    if est.total_usd > cap_usd:
        largest_usd = est.per_layer.get(est.largest_layer, 0.0)
        raise BudgetExceededError(
            f"budget exceeded — estimated ${est.total_usd:.4f} exceeds cap "
            f"${cap_usd:.4f} (largest contributor: layer '{est.largest_layer}' "
            f"at ${largest_usd:.4f})"
        )
