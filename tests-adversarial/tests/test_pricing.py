"""Smoke tests for src/cost/pricing.py — the AC23 mechanism.

Covers:
  - Happy path: both source files currently agree; load_pricing() returns the
    expected dict shape with at least the model ids that exist today.
  - Drift on rate: mock one source to return a different price for one model,
    assert PricingDriftError names the model + the differing field.
  - Drift on missing key: mock one source to drop a model the other has,
    assert PricingDriftError names the missing model.
"""

from __future__ import annotations

import pytest

from src.cost import pricing


# Model ids known to be in both sources today. If the production constants
# add models, this list grows — the test is a floor, not a fence.
_REQUIRED_MODEL_IDS = (
    "us.amazon.nova-2-lite-v1:0",
    "us.anthropic.claude-sonnet-4-6",
    "anthropic.claude-sonnet-4-6-20251006-v1:0",
)


def test_load_pricing_happy_path_returns_reconciled_dict():
    """Both source files agree as committed; load_pricing returns the merged dict."""
    result = pricing.load_pricing()

    assert isinstance(result, dict), "load_pricing must return a dict"
    assert result, "load_pricing must not return an empty dict"

    for model_id in _REQUIRED_MODEL_IDS:
        assert model_id in result, (
            f"expected model '{model_id}' to be present in reconciled pricing "
            f"(got keys: {sorted(result.keys())})"
        )
        rates = result[model_id]
        assert set(rates.keys()) == {"input", "output"}, (
            f"model '{model_id}' must have exactly input + output keys, got {sorted(rates.keys())}"
        )
        assert isinstance(rates["input"], float)
        assert isinstance(rates["output"], float)
        assert rates["input"] >= 0
        assert rates["output"] >= 0


def test_load_pricing_returned_dict_is_independent_copy():
    """Mutating the returned dict must not affect a subsequent call."""
    first = pricing.load_pricing()
    first["us.amazon.nova-2-lite-v1:0"]["input"] = 999.99
    second = pricing.load_pricing()
    assert second["us.amazon.nova-2-lite-v1:0"]["input"] != 999.99


def test_drift_on_input_rate_raises_named_error(monkeypatch):
    """If the Python source reports a different input rate than JS for one
    model, PricingDriftError must name the model and the 'input' field."""

    def fake_python_pricing() -> dict[str, dict[str, float]]:
        # Same models as the JS file today, but a different input price on Nova.
        return {
            "us.amazon.nova-2-lite-v1:0": {"input": 0.99, "output": 0.24},
            "us.anthropic.claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
            "anthropic.claude-sonnet-4-6-20251006-v1:0": {
                "input": 3.00,
                "output": 15.00,
            },
        }

    monkeypatch.setattr(pricing, "_load_python_pricing", fake_python_pricing)

    with pytest.raises(pricing.PricingDriftError) as exc_info:
        pricing.load_pricing()

    message = str(exc_info.value)
    assert "us.amazon.nova-2-lite-v1:0" in message, (
        f"drift error must name the differing model id; got: {message!r}"
    )
    assert "input" in message, (
        f"drift error must name the differing 'input' field; got: {message!r}"
    )
    # The error must also point both operators at both source files.
    assert "token_usage.py" in message
    assert "mockData.js" in message


def test_drift_on_output_rate_raises_named_error(monkeypatch):
    """Same shape as above but on the output rate, to ensure both fields are checked."""

    def fake_python_pricing() -> dict[str, dict[str, float]]:
        return {
            "us.amazon.nova-2-lite-v1:0": {"input": 0.06, "output": 0.24},
            "us.anthropic.claude-sonnet-4-6": {"input": 3.00, "output": 99.99},
            "anthropic.claude-sonnet-4-6-20251006-v1:0": {
                "input": 3.00,
                "output": 15.00,
            },
        }

    monkeypatch.setattr(pricing, "_load_python_pricing", fake_python_pricing)

    with pytest.raises(pricing.PricingDriftError) as exc_info:
        pricing.load_pricing()

    message = str(exc_info.value)
    assert "us.anthropic.claude-sonnet-4-6" in message
    assert "output" in message


def test_drift_on_missing_key_names_missing_model(monkeypatch):
    """If the Python source has a model the JS source doesn't, the error must
    name the missing model and say which file it's missing from."""

    def fake_python_pricing() -> dict[str, dict[str, float]]:
        # Add a model that does not exist in the JS file.
        base = {
            "us.amazon.nova-2-lite-v1:0": {"input": 0.06, "output": 0.24},
            "us.anthropic.claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
            "anthropic.claude-sonnet-4-6-20251006-v1:0": {
                "input": 3.00,
                "output": 15.00,
            },
            "us.amazon.nova-3-pro-v1:0": {"input": 1.00, "output": 4.00},
        }
        return base

    monkeypatch.setattr(pricing, "_load_python_pricing", fake_python_pricing)

    with pytest.raises(pricing.PricingDriftError) as exc_info:
        pricing.load_pricing()

    message = str(exc_info.value)
    assert "us.amazon.nova-3-pro-v1:0" in message, (
        f"drift error must name the model missing from one side; got: {message!r}"
    )
    assert "mockData.js" in message, (
        f"error must say which file the model is missing from; got: {message!r}"
    )


def test_drift_on_missing_key_from_python_side(monkeypatch):
    """Symmetric case: JS has a model the Python source doesn't."""

    def fake_python_pricing() -> dict[str, dict[str, float]]:
        # Drop the Sonnet entries; JS still has them.
        return {
            "us.amazon.nova-2-lite-v1:0": {"input": 0.06, "output": 0.24},
        }

    monkeypatch.setattr(pricing, "_load_python_pricing", fake_python_pricing)

    with pytest.raises(pricing.PricingDriftError) as exc_info:
        pricing.load_pricing()

    message = str(exc_info.value)
    assert "us.anthropic.claude-sonnet-4-6" in message
    assert "token_usage.py" in message


def test_js_parser_handles_quoted_and_computed_keys():
    """Direct unit test on the JS parser to lock in the regex's behavior for
    both key forms used in mockData.js today."""

    source = """
    export const NOVA_LITE_MODEL_ID = 'us.amazon.nova-2-lite-v1:0'

    export const MODEL_PRICING = {
      [NOVA_LITE_MODEL_ID]:                          { input: 0.06, output: 0.24 },
      'us.anthropic.claude-sonnet-4-6':              { input: 3.00, output: 15.00 },
      'anthropic.claude-sonnet-4-6-20251006-v1:0':   { input: 3.00, output: 15.00 },
    }
    """

    parsed = pricing._load_js_pricing(source=source)
    assert parsed == {
        "us.amazon.nova-2-lite-v1:0": {"input": 0.06, "output": 0.24},
        "us.anthropic.claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
        "anthropic.claude-sonnet-4-6-20251006-v1:0": {"input": 3.00, "output": 15.00},
    }


def test_js_parser_raises_on_missing_block():
    """If the MODEL_PRICING block can't be found, fail loudly — silent fallback
    is the exact failure mode AC23 forbids."""

    with pytest.raises(pricing.PricingDriftError) as exc_info:
        pricing._load_js_pricing(source="// no MODEL_PRICING here")

    assert "MODEL_PRICING" in str(exc_info.value)
