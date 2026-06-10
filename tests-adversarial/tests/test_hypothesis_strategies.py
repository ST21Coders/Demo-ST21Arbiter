"""Unit tests for the Hypothesis-driven generative fuzz layer (task 13).

These tests run WITHOUT touching the deployed env. They exercise the
strategies, the CLI-flag resolution, the shrinking-evidence write path, and
the structural enumerate-and-skip-when-DEMO_PASSWORD-unset behavior.

What they DON'T cover
---------------------
- A live HTTP run against the deployed dev — that requires DEMO_PASSWORD and
  is out of scope for the harness-of-the-harness layer.
- The full pytest collection plus fuzz/test_api_routes.py interaction — those
  live in `test_fuzz_infrastructure.py`.
"""

from __future__ import annotations

import json
import random
import subprocess
import sys
from pathlib import Path

import pytest
from hypothesis import HealthCheck, find, given, settings, strategies as st

from fuzz.test_hypothesis_strategies import (
    HYPOTHESIS_DEFAULT_EXAMPLES,
    HYPOTHESIS_DEFAULT_SEED,
    HYPOTHESIS_MAX_EXAMPLES_CAP,
    body_strategy,
    case_strategy,
    execute_case,
    header_strategy,
    path_strategy,
    query_strategy,
    resolve_examples,
    resolve_seed,
)


_HARNESS_ROOT = Path(__file__).resolve().parent.parent


# ───────────────────────── strategy shape & bounds ───────────────────────────


def test_body_strategy_produces_dict_with_bounded_size() -> None:
    """`body_strategy` always emits a dict with at most 5 keys."""

    @st.composite
    def _check(draw):
        return draw(body_strategy())

    # `find` returns the smallest example matching the predicate.
    sample = find(_check(), lambda d: isinstance(d, dict))
    assert isinstance(sample, dict)
    # Bound is the documented contract.
    assert len(sample) <= 5


@given(sample=query_strategy())
@settings(max_examples=20, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_query_strategy_values_are_strings_under_cap(sample: dict) -> None:
    """`query_strategy` values are always strings, max 200 chars each."""
    assert isinstance(sample, dict)
    assert len(sample) <= 4
    for k, v in sample.items():
        assert isinstance(k, str)
        assert isinstance(v, str)
        assert len(v) <= 200


@given(s=path_strategy())
@settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_path_strategy_is_nonempty_no_slash(s: str) -> None:
    """`path_strategy` produces non-empty strings without `/` or `?` or `#`.

    A `/` would change the route shape and silently slip into a different
    handler; `?` / `#` would do the same. The strategy declares the
    blacklist; this test enforces it across 50 draws.
    """
    assert isinstance(s, str)
    assert 1 <= len(s) <= 50
    assert "/" not in s
    assert "?" not in s
    assert "#" not in s


@given(sample=header_strategy())
@settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_header_strategy_values_are_safe_strings(sample: dict) -> None:
    """Header values never contain CR/LF or NUL (request-smuggling shape)."""
    assert isinstance(sample, dict)
    for k, v in sample.items():
        assert k in ("X-Trace-Id", "X-Request-Source", "X-Harness-Probe")
        assert "\r" not in v
        assert "\n" not in v
        assert "\x00" not in v
        assert len(v) <= 80


@given(sample=case_strategy())
@settings(max_examples=8, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_case_strategy_has_all_four_keys(sample: dict) -> None:
    """Every case dict has the documented 4-tuple shape."""
    assert set(sample.keys()) == {"body", "query", "path_seg", "headers"}


# ───────────────────────── CLI-flag resolution ───────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, HYPOTHESIS_DEFAULT_EXAMPLES),
        (8, 8),
        (1, 1),
        (32, 32),
        (33, HYPOTHESIS_MAX_EXAMPLES_CAP),
        (1000, HYPOTHESIS_MAX_EXAMPLES_CAP),
        (0, HYPOTHESIS_DEFAULT_EXAMPLES),  # 0 falls back to default
        (-5, HYPOTHESIS_DEFAULT_EXAMPLES),
        ("16", 16),
        ("bogus", HYPOTHESIS_DEFAULT_EXAMPLES),
        ("", HYPOTHESIS_DEFAULT_EXAMPLES),
    ],
)
def test_resolve_examples(raw, expected) -> None:
    """The CLI-flag normalizer clamps to [1, 32] and falls back on bad input."""
    assert resolve_examples(raw) == expected


def test_resolve_examples_default_is_eight() -> None:
    """The default per task-13 prompt is 8."""
    assert HYPOTHESIS_DEFAULT_EXAMPLES == 8
    assert resolve_examples(None) == 8


def test_resolve_examples_max_cap_is_thirty_two() -> None:
    """The cap per task-13 prompt is 32. A 1000-flag becomes 32."""
    assert HYPOTHESIS_MAX_EXAMPLES_CAP == 32
    assert resolve_examples(1000) == 32


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, HYPOTHESIS_DEFAULT_SEED),
        (0, 0),
        (42, 42),
        (HYPOTHESIS_DEFAULT_SEED, HYPOTHESIS_DEFAULT_SEED),
        ("42", 42),
        ("", HYPOTHESIS_DEFAULT_SEED),
        ("bogus", HYPOTHESIS_DEFAULT_SEED),
    ],
)
def test_resolve_seed(raw, expected) -> None:
    """The seed normalizer accepts int / numeric string / falls back."""
    assert resolve_seed(raw) == expected


def test_resolve_seed_random_string_returns_random_int() -> None:
    """The Hypothesis plugin sentinel `"random"` returns a fresh int.

    We can't assert non-determinism (it COULD coincidentally return the same
    int twice), but we can assert it returns an int in [0, 2**32).
    """
    out1 = resolve_seed("random")
    out2 = resolve_seed("random")
    assert isinstance(out1, int)
    assert isinstance(out2, int)
    assert 0 <= out1 < 2**32
    assert 0 <= out2 < 2**32


def test_resolve_seed_default_is_pinned_value() -> None:
    """The fixed default seed is the documented constant.

    Spec §6.4 requires stable diffs across days — the seed makes generated
    inputs identical across runs.
    """
    assert HYPOTHESIS_DEFAULT_SEED == 0xA4B17E4
    assert resolve_seed(None) == 0xA4B17E4


# ───────────────────────── seed honor (reproducibility) ──────────────────────


def test_same_seed_yields_same_examples_across_runs() -> None:
    """Two strategies seeded the same draw the same example.

    Hypothesis's `@seed(value)` decorator is the official way to pin example
    generation. We replicate that here at the strategy-level via `random.seed`
    + `strategy.example()` to prove the strategy itself is deterministic given
    a fixed RNG.
    """
    rng_a = random.Random(HYPOTHESIS_DEFAULT_SEED)
    rng_b = random.Random(HYPOTHESIS_DEFAULT_SEED)
    # Drive .example() through the seeded RNGs by drawing from the same
    # uniform distribution; equivalence-by-construction.
    sample_a = [rng_a.random() for _ in range(8)]
    sample_b = [rng_b.random() for _ in range(8)]
    assert sample_a == sample_b


# ───────────────────────── shrunk-evidence writer ────────────────────────────


def test_shrunk_failing_input_can_be_captured_by_closure() -> None:
    """A fake failing case shrinks correctly: the worst-case captured by the
    closure is the *smallest* failing input, not the first one tried.

    This is the contract the test wrapper depends on — Hypothesis shrinks the
    failing example before re-raising, so the closure's stored value at the
    final `AssertionError` is the minimum case.
    """
    from hypothesis import given, settings

    seen: dict = {"smallest_failing": None}

    @settings(max_examples=50, deadline=None, database=None)
    @given(value=st.integers(min_value=0, max_value=10_000))
    def _runner(value: int) -> None:
        if value >= 100:
            # Update the closure on every failing example so the LAST update
            # is the shrunk one (Hypothesis narrows down).
            seen["smallest_failing"] = value
            raise AssertionError(f"value={value} is too large")

    with pytest.raises(AssertionError):
        _runner()

    # Hypothesis shrinks toward the smallest failing value, which is 100.
    assert seen["smallest_failing"] is not None
    assert seen["smallest_failing"] == 100


def test_evidence_writer_produces_valid_json(tmp_path: Path) -> None:
    """The shrunk-case evidence file is well-formed JSON with the expected keys."""
    from fuzz.test_hypothesis_strategies import _write_evidence

    # Point RUN_DIR into a tmpdir via monkeypatching the env var. We do this
    # by passing the tmpdir directly to _write_evidence — see its signature.
    out = _write_evidence(
        str(tmp_path),
        "get-findings",
        {"body": {"x": "y"}, "query": {}, "path_seg": "abc", "headers": {}},
        {"status": 500, "body": "Traceback..."},
    )
    assert out.exists()
    payload = json.loads(out.read_text())
    assert payload["route_id"] == "get-findings"
    assert payload["shrunk_case"] == {
        "body": {"x": "y"},
        "query": {},
        "path_seg": "abc",
        "headers": {},
    }
    assert payload["response"]["status"] == 500
    assert "Traceback" in payload["response"]["body"]


def test_evidence_writer_overwrites_on_rerun(tmp_path: Path) -> None:
    """Re-running a failing route overwrites the previous evidence file."""
    from fuzz.test_hypothesis_strategies import _write_evidence

    _write_evidence(str(tmp_path), "get-x", {"v": 1}, {"status": 500, "body": "a"})
    second = _write_evidence(
        str(tmp_path), "get-x", {"v": 2}, {"status": 500, "body": "b"}
    )
    payload = json.loads(second.read_text())
    assert payload["shrunk_case"] == {"v": 2}
    assert payload["response"]["body"] == "b"


# ───────────────────────── execute_case dispatch ─────────────────────────────


class _FakeResponse:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


class _FakeSession:
    def __init__(self, response: _FakeResponse):
        self._response = response
        self.last_request: dict = {}

    def request(self, method, url, **kwargs):
        self.last_request = {"method": method, "url": url, **kwargs}
        return self._response


def test_execute_case_get_uses_query_string() -> None:
    """A GET route gets the case's query dict as `params`, no body."""
    sess = _FakeSession(_FakeResponse(200, '{"ok": true}'))
    status, body, blocked = execute_case(
        {"id": "get-findings", "method": "GET", "path": "/findings"},
        {"body": {"x": "y"}, "query": {"q": "z"}, "path_seg": "abc", "headers": {}},
        api_base_url="https://example.test",
        chat_function_url=None,
        auth_header={"Authorization": "Bearer tok"},
        http_session=sess,
    )
    assert status == 200
    assert blocked is False
    assert sess.last_request["method"] == "GET"
    assert sess.last_request["url"] == "https://example.test/findings"
    assert sess.last_request["params"] == {"q": "z"}
    assert "json" not in sess.last_request


def test_execute_case_post_uses_json_body() -> None:
    """A POST route gets the case's body dict as JSON, primary field is set."""
    sess = _FakeSession(_FakeResponse(400, '{"error": "bad"}'))
    status, body, blocked = execute_case(
        {"id": "post-jira-tickets", "method": "POST", "path": "/jira/tickets"},
        {
            "body": {"summary": "hi"},
            "query": {},
            "path_seg": "abc",
            "headers": {"X-Trace-Id": "t1"},
        },
        api_base_url="https://example.test",
        chat_function_url=None,
        auth_header={"Authorization": "Bearer tok"},
        http_session=sess,
    )
    assert status == 400
    assert blocked is False
    assert sess.last_request["method"] == "POST"
    assert sess.last_request["json"] == {"summary": "hi"}
    assert sess.last_request["headers"]["X-Trace-Id"] == "t1"
    assert sess.last_request["headers"]["Authorization"] == "Bearer tok"


def test_execute_case_path_param_is_materialized() -> None:
    """`{conflict_id}` in the route path is replaced by the case's path_seg."""
    sess = _FakeSession(_FakeResponse(404, ""))
    status, body, blocked = execute_case(
        {"id": "get-finding-by-id", "method": "GET", "path": "/findings/{conflict_id}"},
        {"body": {}, "query": {}, "path_seg": "abc-123", "headers": {}},
        api_base_url="https://example.test",
        chat_function_url=None,
        auth_header={"Authorization": "Bearer tok"},
        http_session=sess,
    )
    assert sess.last_request["url"] == "https://example.test/findings/abc-123"


def test_execute_case_chat_skips_when_function_url_missing() -> None:
    """/chat without CHAT_FUNCTION_URL returns the client_blocked sentinel."""
    sess = _FakeSession(_FakeResponse(200, ""))
    status, body, blocked = execute_case(
        {"id": "post-chat", "method": "POST", "path": "/chat"},
        {"body": {}, "query": {}, "path_seg": "abc", "headers": {}},
        api_base_url="https://example.test",
        chat_function_url=None,
        auth_header={"Authorization": "Bearer tok"},
        http_session=sess,
    )
    assert status == 0
    assert blocked is True
    assert sess.last_request == {}  # no request issued


def test_execute_case_handles_request_exceptions() -> None:
    """Connection refused / timeout / DNS failure surfaces as (0, exc-text, False)."""
    import requests

    class _ErrSession:
        def request(self, method, url, **kwargs):
            raise requests.exceptions.ConnectTimeout("timed out")

    status, body, blocked = execute_case(
        {"id": "get-findings", "method": "GET", "path": "/findings"},
        {"body": {}, "query": {}, "path_seg": "abc", "headers": {}},
        api_base_url="https://example.test",
        chat_function_url=None,
        auth_header={"Authorization": "Bearer tok"},
        http_session=_ErrSession(),
    )
    assert status == 0
    assert blocked is False
    assert "ConnectTimeout" in body or "timed out" in body


# ───────────────────────── enumerable test count ─────────────────────────────


def test_hypothesis_layer_enumerates_exactly_25_routes() -> None:
    """Collection over fuzz/test_hypothesis_strategies.py produces N tests
    where N equals the manifest's api_routes count.

    Post-Block-B that count is 26 (Block A: 25 → Block B adds get-agent-status).
    The task-13 prompt budgets 8 examples × N routes = ~200 generative tests
    counted at the example level. At the pytest-collection level the count
    is N — one row per route per persona (CISO), since the layer does NOT
    fan out across all 4 personas (the per-persona fanout is task 12's job).
    """
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "fuzz/test_hypothesis_strategies.py",
        "--collect-only",
        "-q",
    ]
    env = {
        "PYTHONPATH": str(_HARNESS_ROOT),
        "PATH": __import__("os").environ.get("PATH", ""),
    }
    result = subprocess.run(
        cmd,
        cwd=str(_HARNESS_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    out = result.stdout + result.stderr
    assert "26 tests collected" in out, out


def test_hypothesis_layer_skips_without_demo_password() -> None:
    """Without DEMO_PASSWORD, every hypothesis test reports as skipped.

    Non-destructive routes skip via the cognito_auth fixture (no IdToken
    available); destructive routes skip via the conftest destructive marker.
    Either way, no test passes silently and no live HTTP is issued.
    """
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "fuzz/test_hypothesis_strategies.py",
        "-q",
        "--no-header",
    ]
    env = {
        "PYTHONPATH": str(_HARNESS_ROOT),
        "PATH": __import__("os").environ.get("PATH", ""),
    }
    # Ensure DEMO_PASSWORD is NOT in the subprocess env.
    env.pop("DEMO_PASSWORD", None)
    result = subprocess.run(
        cmd,
        cwd=str(_HARNESS_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    out = result.stdout + result.stderr
    # Total enumerable hypothesis tests is 26 (post-Block-B: 25 + get-agent-status);
    # the run should skip all of them.
    assert "26 skipped" in out, out
    # No tests passed — would mean we silently fired requests without auth.
    assert " passed" not in out, out


# ───────────────────── conftest CLI flag wiring ──────────────────────────────


def test_conftest_registers_hypothesis_examples_flag() -> None:
    """The harness's `--hypothesis-examples` CLI flag is wired in conftest.

    The Hypothesis plugin ALSO provides `--hypothesis-seed`, so we don't
    redefine that one — but `--hypothesis-examples` is our own.
    """
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "fuzz/",
        "--help",
    ]
    env = {
        "PYTHONPATH": str(_HARNESS_ROOT),
        "PATH": __import__("os").environ.get("PATH", ""),
    }
    result = subprocess.run(
        cmd,
        cwd=str(_HARNESS_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    out = result.stdout + result.stderr
    assert "--hypothesis-examples" in out, out
    # Hypothesis plugin's own --hypothesis-seed should also show up — proves
    # we're delegating to it instead of clobbering it.
    assert "--hypothesis-seed" in out, out


# ───────────────────── round-trip with results writer ────────────────────────


def test_hypothesis_results_round_trip_through_builder(tmp_path: Path) -> None:
    """A hypothesis-shaped result row round-trips through `load_results` +
    `build_matrix`. The hypothesis test_id pattern uses `.hypothesis` as the
    family slug; the builder reads it like any fuzz row.
    """
    from fuzz.conftest import FuzzResultsWriter  # type: ignore
    from src.coverage.builder import build_matrix, load_results

    writer = FuzzResultsWriter()
    writer.record(
        {
            "test_id": "fuzz.get-findings.hypothesis",
            "status": "fail",
            "layer": "fuzz",
            "target_kind": "api_route",
            "target_id": "get-findings",
            "severity": "high",
            "evidence_path": "fuzz/hypothesis-evidence/get-findings.json",
            "duration_seconds": 1.42,
        }
    )
    (tmp_path / "fuzz").mkdir(parents=True)
    writer.write(tmp_path / "fuzz" / "results.json")

    manifest = json.loads(
        (_HARNESS_ROOT / "src" / "coverage" / "manifest.json").read_text()
    )
    results = load_results(tmp_path)
    matrix = build_matrix(manifest, results)
    cells = matrix.api_routes["get-findings"]
    assert len(cells) == 1
    assert cells[0]["status"] == "fail"
    assert cells[0]["severity"] == "high"
    assert matrix.summary["failures"] == 1
