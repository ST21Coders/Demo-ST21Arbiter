"""Live cost + probe-count tracker for the orchestrator.

Where `preflight.py` is the static estimator that runs once before any
network call, `CostTracker` is the live tally consulted during a run:

  - the LLM red-team layer calls `record(...)` after each probe to feed
    the cost footer (spec §6.2);
  - the same calls feed `probe_count(layer)`, which the AC16 probe-budget
    fixture in llm/conftest.py uses to enforce the per-run hard cap
    (default 30, override via --llm-probes);
  - the reporter's `report.json` builder calls `as_dict()` to serialize the
    final totals into the cost footer.

Thread-unsafe is fine — the orchestrator runs layers in series, not in
parallel (see plan §3, risk 4: Bedrock concurrency throttle).
"""

from __future__ import annotations

from typing import Mapping


class CostTracker:
    """Accumulates per-layer cost in USD and per-layer probe counts.

    Instantiated once at the start of a run with the reconciled pricing
    table (typically from `pricing.load_pricing()`), then `record(...)`'d
    after each Bedrock call. The token math mirrors
    agents/_shared/token_usage.py::compute_cost: per-million-token rates.
    """

    def __init__(self, pricing: Mapping[str, Mapping[str, float]]) -> None:
        # Defensive copy of the pricing table so a later mutation of the
        # caller's dict can't silently change the math on already-recorded
        # rows. Keys remain str -> {input: float, output: float}.
        self._pricing: dict[str, dict[str, float]] = {
            model_id: {"input": float(rates["input"]), "output": float(rates["output"])}
            for model_id, rates in pricing.items()
        }
        self._per_layer_usd: dict[str, float] = {}
        self._probe_counts: dict[str, int] = {}

    def record(
        self,
        layer: str,
        model_id: str,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        """Add one Bedrock-call worth of billed cost to the layer's total.

        Does NOT bump the probe counter — a probe is a SENT request, while
        `record()` is for OBSERVED billed tokens. A probe that errors before
        producing tokens (Bedrock throttle, runtime 5xx, request timeout)
        still has to count against the AC16 per-run cap, so probe accounting
        is split into `increment_probe()`. This split was added in task 17
        after the reviewer flagged that the previous "bump both" behavior
        let failed probes escape the cap.

        Unknown `model_id` raises with a clear message — silently dropping
        the row would mean the cost footer underreports and the cap
        enforcement upstream is meaningless. The caller is expected to use
        a model_id from the reconciled MODEL_PRICING table.
        """
        if model_id not in self._pricing:
            raise KeyError(
                f"CostTracker.record: model_id '{model_id}' not in pricing table "
                f"(known: {sorted(self._pricing.keys())}). Either add the model "
                f"to both MODEL_PRICING source files or use a known model_id."
            )
        rates = self._pricing[model_id]
        cost = (max(0, int(input_tokens)) / 1_000_000.0) * rates["input"] + (
            max(0, int(output_tokens)) / 1_000_000.0
        ) * rates["output"]
        self._per_layer_usd[layer] = self._per_layer_usd.get(layer, 0.0) + cost

    def increment_probe(self, layer: str = "llm") -> int:
        """Bump the per-layer probe counter and return the new count.

        Called by the LLM red-team probe-budget fixture BEFORE each `/chat`
        request so a probe that errors out (and thus never reaches `record`)
        still counts against the AC16 cap. Returns the post-increment
        count so callers can compare against their cap inline.

        `layer` defaults to "llm" because that's the only layer with a hard
        probe cap today; the parameter exists so other layers can opt in
        without an API change.
        """
        new_count = self._probe_counts.get(layer, 0) + 1
        self._probe_counts[layer] = new_count
        return new_count

    def total_usd(self) -> float:
        """Sum of all per-layer costs recorded so far."""
        return sum(self._per_layer_usd.values())

    def per_layer_usd(self) -> dict[str, float]:
        """Per-layer cost snapshot. Caller gets a fresh dict (mutation-safe)."""
        return dict(self._per_layer_usd)

    def probe_count(self, layer: str) -> int:
        """How many `record()` calls have landed in this layer.

        Used by the LLM red-team probe-budget fixture (AC16) to enforce the
        30-probe-per-run hard cap. Returns 0 for an unknown layer name —
        the orchestrator may query before any probe has been recorded.
        """
        return self._probe_counts.get(layer, 0)

    def as_dict(self) -> dict:
        """Serializable snapshot matching what report.json's cost footer wants.

        Shape:
            {
              "total_usd": float,
              "per_layer_usd": {layer_name: float, ...},
              "probe_counts": {layer_name: int, ...},
            }

        The reporter (task 21) merges this into the spec §6.3 `cost` block
        next to the pre-flight estimate.
        """
        return {
            "total_usd": self.total_usd(),
            "per_layer_usd": self.per_layer_usd(),
            "probe_counts": dict(self._probe_counts),
        }
