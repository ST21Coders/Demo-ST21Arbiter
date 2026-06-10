"""Reconcile MODEL_PRICING between agents/_shared/token_usage.py and ui/src/mockData.js.

This module is the AC23 mechanism. CLAUDE.md flags MODEL_PRICING as a
duplicated-by-design constant living in two files. The harness consumes both,
reconciles them, and raises PricingDriftError if they disagree — fail fast at
pre-flight rather than silently picking one.

Both source files remain off-limits per the project rules. This module reads,
never writes.

Public surface:
    load_pricing() -> dict[str, dict[str, float]]
    PricingDriftError(RuntimeError)
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Repo root is three parents up from this file:
#   .../tests-adversarial/src/cost/pricing.py
#   parents[0] = src/cost
#   parents[1] = src
#   parents[2] = tests-adversarial
#   parents[3] = <repo root>
_REPO_ROOT = Path(__file__).resolve().parents[3]
_MOCKDATA_JS = _REPO_ROOT / "ui" / "src" / "mockData.js"
_TOKEN_USAGE_PY = _REPO_ROOT / "agents" / "_shared" / "token_usage.py"


class PricingDriftError(RuntimeError):
    """Raised when the two MODEL_PRICING source files disagree.

    Message names exactly what differs (missing keys and/or differing input/
    output rates) plus both source paths so the operator knows which two files
    to edit together.
    """


def _load_python_pricing() -> dict[str, dict[str, float]]:
    """Import MODEL_PRICING from agents/_shared/token_usage.py.

    We add the repo root to sys.path on the fly so `import agents._shared.token_usage`
    resolves whether or not the harness was installed editable. The import is
    done lazily inside the function to keep module-load side effects nil for
    tests that monkeypatch this loader.
    """
    repo_root_str = str(_REPO_ROOT)
    added = False
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)
        added = True
    try:
        from agents._shared.token_usage import MODEL_PRICING  # type: ignore[import-not-found]
    finally:
        if added:
            try:
                sys.path.remove(repo_root_str)
            except ValueError:
                pass
    # Defensive copy + float coercion so callers can't mutate the agent module's dict.
    return {
        model_id: {"input": float(rates["input"]), "output": float(rates["output"])}
        for model_id, rates in MODEL_PRICING.items()
    }


# Regex to pull each model entry out of the JS object literal at
# `export const MODEL_PRICING = { ... }` in ui/src/mockData.js.
#
# The JS shape (verbatim today) is:
#
#   export const MODEL_PRICING = {
#     [NOVA_LITE_MODEL_ID]:                          { input: 0.06, output: 0.24 },
#     'us.anthropic.claude-sonnet-4-6':              { input: 3.00, output: 15.00 },
#     'anthropic.claude-sonnet-4-6-20251006-v1:0':   { input: 3.00, output: 15.00 },
#   }
#
# Two key forms appear:
#   1. Quoted string literal:  'us.anthropic.claude-sonnet-4-6': { ... }
#   2. Computed identifier:    [NOVA_LITE_MODEL_ID]: { ... }
#
# For (2) the harness must additionally resolve the identifier to its string
# value via `_extract_js_const`. Both forms are matched separately so the
# regex stays narrow (no nested-brace counting, no general JS parsing).
_BLOCK_RE = re.compile(
    r"export\s+const\s+MODEL_PRICING\s*=\s*\{(?P<body>.*?)\n[ \t]*\}",
    re.DOTALL,
)
# Quoted-key entry. Allows both single and double quotes.
_QUOTED_ENTRY_RE = re.compile(
    r"""
    ['"](?P<key>[^'"]+)['"]            # quoted model id
    \s*:\s*
    \{\s*
        input\s*:\s*(?P<input>[0-9.]+)
        \s*,\s*
        output\s*:\s*(?P<output>[0-9.]+)
        \s*,?\s*
    \}
    """,
    re.VERBOSE,
)
# Computed-key entry: [IDENT]: { input: X, output: Y }
_COMPUTED_ENTRY_RE = re.compile(
    r"""
    \[\s*(?P<ident>[A-Za-z_][A-Za-z0-9_]*)\s*\]
    \s*:\s*
    \{\s*
        input\s*:\s*(?P<input>[0-9.]+)
        \s*,\s*
        output\s*:\s*(?P<output>[0-9.]+)
        \s*,?\s*
    \}
    """,
    re.VERBOSE,
)
# Resolves `export const NAME = 'value'` (single or double quotes) for the
# computed-key indirection.
_JS_CONST_RE_TMPL = r"export\s+const\s+{name}\s*=\s*['\"](?P<value>[^'\"]+)['\"]"


def _extract_js_const(source: str, name: str) -> str:
    pattern = _JS_CONST_RE_TMPL.format(name=re.escape(name))
    m = re.search(pattern, source)
    if not m:
        raise PricingDriftError(
            f"Could not resolve JS identifier '{name}' referenced as a computed "
            f"key in {_MOCKDATA_JS}. The MODEL_PRICING block uses [{name}] but "
            f"no `export const {name} = '...'` was found in the same file."
        )
    return m.group("value")


def _load_js_pricing(source: str | None = None) -> dict[str, dict[str, float]]:
    """Parse ui/src/mockData.js with a narrow regex and return its MODEL_PRICING.

    `source` is exposed for testability; production callers pass None and the
    file is read from disk.
    """
    if source is None:
        try:
            source = _MOCKDATA_JS.read_text(encoding="utf-8")
        except FileNotFoundError as e:
            raise PricingDriftError(
                f"Cannot read JS pricing source at {_MOCKDATA_JS}: {e}"
            ) from e

    block_match = _BLOCK_RE.search(source)
    if not block_match:
        raise PricingDriftError(
            f"Could not find `export const MODEL_PRICING = {{ ... }}` block in "
            f"{_MOCKDATA_JS}. The regex extractor is narrow by design; if the "
            f"JS file's shape changed, update the regex in this module."
        )
    body = block_match.group("body")

    pricing: dict[str, dict[str, float]] = {}

    for m in _QUOTED_ENTRY_RE.finditer(body):
        pricing[m.group("key")] = {
            "input": float(m.group("input")),
            "output": float(m.group("output")),
        }

    for m in _COMPUTED_ENTRY_RE.finditer(body):
        ident = m.group("ident")
        resolved_key = _extract_js_const(source, ident)
        pricing[resolved_key] = {
            "input": float(m.group("input")),
            "output": float(m.group("output")),
        }

    if not pricing:
        raise PricingDriftError(
            f"Parsed MODEL_PRICING block from {_MOCKDATA_JS} but found zero "
            f"entries. The regex extractor is narrow by design; if the JS "
            f"file's shape changed, update the regex in this module."
        )

    return pricing


def _reconcile(
    py_pricing: dict[str, dict[str, float]],
    js_pricing: dict[str, dict[str, float]],
) -> dict[str, dict[str, float]]:
    """Compare the two pricing dicts. Raise PricingDriftError on any disagreement.

    Differences detected:
      - keys present in Python but missing from JS (and vice versa)
      - same key, different input rate
      - same key, different output rate

    The error message names every divergence (not just the first) so the
    operator can fix both files in a single pass.
    """
    py_keys = set(py_pricing.keys())
    js_keys = set(js_pricing.keys())

    drifts: list[str] = []

    for missing_from_js in sorted(py_keys - js_keys):
        drifts.append(
            f"  - model '{missing_from_js}' present in {_TOKEN_USAGE_PY} "
            f"but missing from {_MOCKDATA_JS}"
        )
    for missing_from_py in sorted(js_keys - py_keys):
        drifts.append(
            f"  - model '{missing_from_py}' present in {_MOCKDATA_JS} "
            f"but missing from {_TOKEN_USAGE_PY}"
        )

    for shared_key in sorted(py_keys & js_keys):
        py_rates = py_pricing[shared_key]
        js_rates = js_pricing[shared_key]
        for field in ("input", "output"):
            py_val = py_rates.get(field)
            js_val = js_rates.get(field)
            if py_val != js_val:
                drifts.append(
                    f"  - model '{shared_key}' field '{field}' disagrees: "
                    f"py={py_val} vs js={js_val}"
                )

    if drifts:
        raise PricingDriftError(
            "pricing drift detected — MODEL_PRICING disagrees between\n"
            f"  {_TOKEN_USAGE_PY}\n"
            "and\n"
            f"  {_MOCKDATA_JS}\n"
            "\n"
            "Differences:\n" + "\n".join(drifts) + "\n\n"
            "Edit both files together (project rule, see CLAUDE.md)."
        )

    # On agreement, return a fresh dict so callers can mutate safely.
    return {k: dict(v) for k, v in py_pricing.items()}


def load_pricing() -> dict[str, dict[str, float]]:
    """Return the reconciled MODEL_PRICING dict.

    Reads both the Python and JS sources, reconciles them, raises
    PricingDriftError if they disagree. On agreement, returns the dict
    keyed by model id with `{"input": float, "output": float}` values.
    """
    py_pricing = _load_python_pricing()
    js_pricing = _load_js_pricing()
    return _reconcile(py_pricing, js_pricing)
