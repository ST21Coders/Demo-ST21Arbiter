"""Hypothesis-based generative fuzz on top of the task-12 curated layer.

Why this layer
--------------
The task-12 corpus catches known-bad shapes (XSS, SQLi, oversize, malformed JWT,
etc). Hypothesis explores the input space at random within sane bounds and,
critically, *shrinks* failing inputs to a minimal counterexample. That makes it
the right tool to find shape mismatches the curated corpus missed (e.g. a field
that accepts strings but crashes on a particular Unicode normalization, or a
query-string handler that 500s on an empty value).

How it composes with task 12
----------------------------
- Same `results_writer` fixture — every hypothesis run for a given route emits
  ONE row to `fuzz/results.json` (`fuzz.<route-id>.hypothesis`), regardless of
  how many examples Hypothesis tried. The shrunk failing input (if any) is
  written to a per-route evidence JSON under `fuzz/hypothesis-evidence/`.
- Same `http_session` fixture — so the same 10s timeout and 5 RPS throttle
  apply. With the default 8 examples × 25 routes, that's ~40 seconds at the
  throttle floor.
- Same destructive-marker gate — POST/PUT/PATCH/DELETE routes are skipped
  unless `--include-destructive` is passed. We don't want stray writes.
- Same identity source — uses the CISO identity (broadest surface). Per-persona
  fan-out would multiply the wall-clock by 4 with little extra coverage signal.

Test-id convention
------------------
`fuzz.<route-id>.hypothesis` — one row per route. Per spec §7.3 the diff block
matches on stable ids, so each run for the same route writes to the same row.
The shrunk input is the evidence; it does not affect the test id.

Cost / time control
-------------------
- `max_examples = 8` (default; CLI override `--hypothesis-examples N`, capped
  at 32).
- `deadline = 10000ms` per example — pairs with the 10s HTTP timeout, so a
  single slow example doesn't deadlock the layer.
- `suppress_health_check = [HealthCheck.too_slow]` — the 5 RPS throttle is
  inherent to live HTTP fuzz; Hypothesis would otherwise abort with
  `FailedHealthCheck`.
- Fixed seed via `derandomize=True` + a `phases=[explicit, reuse, generate]`
  exclusion of `shrink` is NOT what we want — we want shrinking on failure.
  Instead, we set `random.seed()` on the local Random instance via
  `register_random` and pass `--hypothesis-seed N` to make runs deterministic
  across days (important for diff-from-last-green stability).

Shrinking
---------
On failure, Hypothesis automatically minimizes the failing input to the
smallest example that still fails. We capture that minimized input and write
it to `fuzz/hypothesis-evidence/<route-id>.json`, then point the row's
`evidence_path` at that file.
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest
import requests
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from hypothesis import seed as hyp_seed

from fuzz._payloads import (
    classify_response,
    fuzz_results_path,
    route_has_path_param,
)


# ──────────────────────────── tunables ───────────────────────────────────────


# Maximum value the operator can pass via `--hypothesis-examples`. Hard cap so
# a slipped-in `--hypothesis-examples 10000` doesn't blow the wall-clock or
# the API Gateway throttle. See task-13 prompt §3.
HYPOTHESIS_MAX_EXAMPLES_CAP = 32

# Default seed for derandomized runs. Picked once and pinned so runs across
# days produce the same set of generated inputs; this is what makes the
# diff-from-last-green block stable. Override per-run via `--hypothesis-seed N`.
HYPOTHESIS_DEFAULT_SEED = 0xA4B17E4

# Default per-route example count. Matches the task-13 prompt; the orchestrator
# can raise this for a deeper sweep at the cost of wall-clock.
HYPOTHESIS_DEFAULT_EXAMPLES = 8


# ───────────────────────── manifest loading ──────────────────────────────────


_HARNESS_ROOT = Path(__file__).resolve().parent.parent
_MANIFEST_PATH = _HARNESS_ROOT / "src" / "coverage" / "manifest.json"
_EVIDENCE_DIR_NAME = "hypothesis-evidence"


def _load_routes() -> list[dict]:
    raw = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    return list(raw.get("api_routes") or [])


_ROUTES: list[dict] = _load_routes()


# ───────────────────────── strategy definitions ──────────────────────────────


# Strings bounded to avoid blowing the 1MB body cap the fuzz layer enforces.
# 200-char strings are plenty large to find shape mismatches while staying
# comfortably under the per-field budget Lambda will accept.
_TEXT = st.text(max_size=200)
_QUERY_TEXT = st.text(max_size=200)
_PATH_SEG = st.text(
    alphabet=st.characters(
        whitelist_categories=("L", "N", "P", "S"),
        blacklist_characters="/?#",  # would change the route shape
    ),
    min_size=1,
    max_size=50,
)
_HEADER_VALUE = st.text(
    # Disallow CR/LF in header values — the requests library would refuse to
    # send them client-side anyway, and we want each example to actually hit
    # the wire so the response is meaningful.
    alphabet=st.characters(
        blacklist_characters="\r\n\x00",
        whitelist_categories=("L", "N", "P", "S", "Zs"),
    ),
    max_size=80,
)


def body_strategy() -> st.SearchStrategy[dict]:
    """Strategy for a JSON body dict with up to 5 keys of mixed value types.

    Field-value types are drawn from a small union: text, integer, list of
    text, dict of text->text. Bounded so the encoded JSON stays under 4KB.
    """
    return st.dictionaries(
        keys=st.text(min_size=1, max_size=40),
        values=st.one_of(
            _TEXT,
            st.integers(min_value=-(2**31), max_value=2**31 - 1),
            st.lists(_TEXT, max_size=8),
            st.dictionaries(st.text(min_size=1, max_size=20), _TEXT, max_size=8),
            st.booleans(),
            st.none(),
        ),
        max_size=5,
    )


def query_strategy() -> st.SearchStrategy[dict]:
    """Strategy for query-string params: up to 4 keys, all string values."""
    return st.dictionaries(
        keys=st.text(min_size=1, max_size=40),
        values=_QUERY_TEXT,
        max_size=4,
    )


def path_strategy() -> st.SearchStrategy[str]:
    """Strategy for a single path-param segment.

    Non-empty, no slashes or other route-shape-mutating characters.
    """
    return _PATH_SEG


def header_strategy() -> st.SearchStrategy[dict]:
    """Strategy for a small handful of non-standard headers."""
    return st.dictionaries(
        keys=st.sampled_from(("X-Trace-Id", "X-Request-Source", "X-Harness-Probe")),
        values=_HEADER_VALUE,
        max_size=3,
    )


# Composite strategy: one "generated case" per route is a 4-tuple
# (body, query, path_seg, headers). Some routes only use a subset, but we
# always draw all four so the test wrapper can decide.
def case_strategy() -> st.SearchStrategy[dict]:
    return st.fixed_dictionaries(
        {
            "body": body_strategy(),
            "query": query_strategy(),
            "path_seg": path_strategy(),
            "headers": header_strategy(),
        }
    )


# ────────────────────────── request execution ───────────────────────────────


_PRIMARY_BODY_FIELD: dict[str, str] = {
    "post-chat": "prompt",
    "post-jira-tickets": "summary",
    "post-uploads-presign": "filename",
    "post-actions": "description",
    "post-action-approve": "comment",
    "post-action-reject": "comment",
    "post-action-execute": "comment",
    "post-action-escalate": "comment",
    "post-scan": "trigger",
}


def _materialize_path(route_path: str, path_seg: str) -> str:
    """Replace every `{name}` placeholder in `route_path` with `path_seg`."""
    out = route_path
    while "{" in out and "}" in out:
        lbr = out.find("{")
        rbr = out.find("}", lbr)
        if rbr < 0:
            break
        out = out[:lbr] + path_seg + out[rbr + 1 :]
    return out


def execute_case(
    route: dict,
    case: dict,
    *,
    api_base_url: str,
    chat_function_url: str | None,
    auth_header: dict,
    http_session: requests.Session,
) -> tuple[int, str, bool]:
    """Send one generated case at one route. Returns (status, body, client_blocked).

    Exposed module-level (not nested inside the test) so the harness-of-the-harness
    tests can verify the dispatch without standing up Hypothesis.
    """
    method = (route.get("method") or "GET").upper()
    path = route.get("path") or ""

    # Materialize URL.
    if route_has_path_param(path):
        path = _materialize_path(path, case["path_seg"])

    if route["id"] == "post-chat":
        if not chat_function_url:
            return (0, "CHAT_FUNCTION_URL unset", True)
        url = (
            f"{chat_function_url}{path}"
            if not chat_function_url.endswith(path)
            else chat_function_url
        )
    else:
        url = f"{api_base_url}{path}"

    headers = dict(auth_header)
    headers.update(case["headers"])

    body: dict | None = None
    params: dict | None = None

    if method in ("POST", "PUT", "PATCH"):
        # Project the generated body into the route's primary field plus the
        # other generated keys as extras (the API will reject unknown fields
        # with a 400 — that's a PASS in our classifier).
        primary = _PRIMARY_BODY_FIELD.get(route["id"], "payload")
        body = dict(case["body"])
        body.setdefault(primary, "")
    else:
        params = dict(case["query"])

    try:
        kwargs: dict[str, Any] = {"headers": headers}
        if body is not None:
            kwargs["json"] = body
        if params is not None:
            kwargs["params"] = params
        response = http_session.request(method, url, **kwargs)
        return (response.status_code, response.text or "", False)
    except requests.exceptions.InvalidHeader:
        return (0, "", True)
    except requests.exceptions.RequestException as exc:
        return (0, f"requests exception: {type(exc).__name__}: {exc}", False)


# ──────────────────────────── routes to run ──────────────────────────────────


def _eligible_routes() -> list[dict]:
    """Return the routes the hypothesis layer will exercise.

    We intentionally include destructive routes here too — the conftest's
    `--include-destructive` gate is enforced at the test-level marker, not at
    this enumeration. Skipping at the marker layer keeps the inventory full so
    the report shows the route as covered even when the run skipped it.
    """
    return list(_ROUTES)


_ROUTE_PARAMS = _eligible_routes()


def _route_param_id(r: dict) -> str:
    return r["id"]


# ──────────────────────── per-run knob resolution ────────────────────────────


def resolve_examples(raw: Any) -> int:
    """Normalize the `--hypothesis-examples` CLI value into [1, CAP].

    None / empty / non-int values fall back to the default. Values above the
    cap are silently capped so a slipped-in `--hypothesis-examples 10000`
    doesn't blow the wall-clock.
    """
    if raw is None:
        return HYPOTHESIS_DEFAULT_EXAMPLES
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return HYPOTHESIS_DEFAULT_EXAMPLES
    if value <= 0:
        return HYPOTHESIS_DEFAULT_EXAMPLES
    return min(value, HYPOTHESIS_MAX_EXAMPLES_CAP)


def resolve_seed(raw: Any) -> int:
    """Normalize the `--hypothesis-seed` CLI value into an int seed.

    The Hypothesis pytest plugin owns `--hypothesis-seed`; its parsed value can
    be:
      - None (flag not passed) — fall back to HYPOTHESIS_DEFAULT_SEED.
      - "random" — the plugin's sentinel for "actually be random"; we honor it
        by returning a fresh random.randrange so each example is independent.
      - an int / a base-10 numeric string — use as-is.
    """
    if raw is None:
        return HYPOTHESIS_DEFAULT_SEED
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return HYPOTHESIS_DEFAULT_SEED
        if s.lower() == "random":
            return random.randrange(0, 2**32)
        try:
            return int(s)
        except ValueError:
            return HYPOTHESIS_DEFAULT_SEED
    try:
        return int(raw)
    except (TypeError, ValueError):
        return HYPOTHESIS_DEFAULT_SEED


# ───────────────────────────── the test ──────────────────────────────────────


def _evidence_dir(run_dir: str | None) -> Path:
    """Where shrunk failing inputs are dumped."""
    return fuzz_results_path(run_dir).parent / _EVIDENCE_DIR_NAME


def _write_evidence(
    run_dir: str | None, route_id: str, shrunk_case: dict, response: dict
) -> Path:
    """Write the shrunk failing input + response snapshot to disk."""
    evidence_dir = _evidence_dir(run_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    out_path = evidence_dir / f"{route_id}.json"
    payload = {
        "route_id": route_id,
        "shrunk_case": shrunk_case,
        "response": response,
    }
    out_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )
    return out_path


@pytest.fixture(scope="session")
def ciso_auth_header(identities: dict) -> dict:
    """Single-persona auth header for the hypothesis layer.

    The task-13 prompt budgets 8 examples × 25 routes = 200 generative tests,
    NOT × 4 personas. Per-persona fanout would quadruple the wall-clock at
    the 5 RPS throttle with little extra coverage signal — task 12 already
    covers per-persona surface. CISO is chosen because it has the broadest
    access map (every route reachable).
    """
    from src.identity.cognito_auth import Persona

    identity = identities[Persona.CISO]
    return {"Authorization": f"Bearer {identity.id_token}"}


@pytest.mark.parametrize(
    "route", _ROUTE_PARAMS, ids=[_route_param_id(r) for r in _ROUTE_PARAMS]
)
def test_hypothesis_route_fuzz(
    route: dict,
    http_session: requests.Session,
    ciso_auth_header: dict,
    api_base_url: str,
    chat_function_url: str | None,
    results_writer,
    request: pytest.FixtureRequest,
    pytestconfig: pytest.Config,
) -> None:
    """Run a hypothesis-driven generative fuzz at one route.

    PASS = every generated case returned a sane response.
    FAIL = at least one case triggered the failure invariant (500, stack-trace
           marker, or payload reflection).

    On FAIL, Hypothesis's shrinker has already minimized the input; we capture
    the shrunk case via a closure and write it as the evidence file.
    """
    # Gate /chat behind CHAT_FUNCTION_URL.
    if route["id"] == "post-chat" and not chat_function_url:
        results_writer.record(
            {
                "test_id": f"fuzz.{route['id']}.hypothesis",
                "status": "skipped",
                "layer": "fuzz",
                "target_kind": "api_route",
                "target_id": route["id"],
                "skipped_reason": "CHAT_FUNCTION_URL unset; /chat hypothesis fuzz disabled.",
            }
        )
        pytest.skip("CHAT_FUNCTION_URL unset; /chat hypothesis fuzz disabled.")

    # Destructive routes are gated by `pytest_collection_modifyitems` in
    # conftest.py — that hook walks every parametrized item and attaches the
    # destructive marker before the run starts, so destructive hypothesis cases
    # are skipped at the marker layer when `--include-destructive` is off.
    # No inline check needed here.

    # Resolve per-run knobs.
    examples = resolve_examples(
        pytestconfig.getoption(
            "--hypothesis-examples", default=HYPOTHESIS_DEFAULT_EXAMPLES
        )
    )
    seed_value = resolve_seed(pytestconfig.getoption("--hypothesis-seed", default=None))

    # Capture the worst case the runner sees, so we can emit it as evidence on
    # FAIL. Mutated by the inner function via closure.
    worst: dict = {
        "verdict": "pass",
        "severity": None,
        "reasons": (),
        "shrunk_case": None,
        "status": None,
        "body": None,
    }
    started = time.monotonic()

    @settings(
        max_examples=examples,
        deadline=10_000,  # 10 s per example
        suppress_health_check=[
            HealthCheck.too_slow,
            HealthCheck.function_scoped_fixture,
            HealthCheck.filter_too_much,
        ],
        derandomize=True,  # paired with hyp_seed() decorator below
    )
    @hyp_seed(seed_value)
    @given(case=case_strategy())
    def _runner(case: dict) -> None:
        status, body, client_blocked = execute_case(
            route,
            case,
            api_base_url=api_base_url,
            chat_function_url=chat_function_url,
            auth_header=ciso_auth_header,
            http_session=http_session,
        )
        # Use the generated body's first field value as the reflection probe
        # (best-effort). Header values are bounded; not used for reflection
        # because the API never echoes headers in responses.
        ref: str | None = None
        if case.get("body"):
            for v in case["body"].values():
                if isinstance(v, str) and len(v) >= 6:
                    ref = v
                    break
        verdict = classify_response(
            status_code=status,
            body=body,
            payload_value=ref,
            expected_blocked=True,  # generated inputs are always treated as adversarial
            client_blocked=client_blocked,
        )
        if verdict.verdict == "fail":
            # Snapshot the worst case BEFORE raising, so the shrinker keeps
            # narrowing toward the minimal input that fails.
            worst["verdict"] = "fail"
            worst["severity"] = verdict.severity
            worst["reasons"] = verdict.reasons
            worst["shrunk_case"] = case
            worst["status"] = status
            worst["body"] = body[:2000]  # truncate for evidence file
            # AssertionError is the standard signal Hypothesis uses to drive
            # shrinking. The closure above captures the *current* failing case;
            # when shrinking yields a smaller failing case, the closure
            # overwrites it. Final value of `worst` is the shrunk case.
            raise AssertionError(
                f"{route['id']} hypothesis case failed: status={status} "
                f"severity={verdict.severity} reasons={'; '.join(verdict.reasons)}"
            )

    duration = 0.0
    fail_exc: BaseException | None = None
    try:
        _runner()
    except AssertionError as exc:
        fail_exc = exc
    finally:
        duration = round(time.monotonic() - started, 3)

    test_id = f"fuzz.{route['id']}.hypothesis"
    row: dict = {
        "test_id": test_id,
        "status": "fail" if fail_exc else "pass",
        "layer": "fuzz",
        "target_kind": "api_route",
        "target_id": route["id"],
        "duration_seconds": duration,
    }

    # Hypothesis layer always runs as CISO (see `ciso_auth_header` fixture).
    row["persona"] = "ciso"

    if fail_exc:
        row["severity"] = worst["severity"] or "high"
        evidence_path = _write_evidence(
            __import__("os").environ.get("RUN_DIR"),
            route["id"],
            worst["shrunk_case"] or {},
            {"status": worst["status"], "body": worst["body"]},
        )
        # Store as a path relative to the fuzz/results.json file so the
        # reporter can resolve it without knowing the run dir.
        row["evidence_path"] = f"fuzz/{_EVIDENCE_DIR_NAME}/{route['id']}.json"
        results_writer.record(row)
        pytest.fail(
            f"{test_id} | shrunk evidence at {evidence_path} | "
            f"{'; '.join(worst['reasons'])}"
        )
    else:
        results_writer.record(row)


# ───────────────────────── public exports ────────────────────────────────────


__all__ = [
    "HYPOTHESIS_DEFAULT_EXAMPLES",
    "HYPOTHESIS_DEFAULT_SEED",
    "HYPOTHESIS_MAX_EXAMPLES_CAP",
    "body_strategy",
    "case_strategy",
    "execute_case",
    "header_strategy",
    "path_strategy",
    "query_strategy",
    "resolve_examples",
    "resolve_seed",
]


def _all_route_ids() -> Iterable[str]:
    """Helper exposed for tests/discovery."""
    return (r["id"] for r in _ROUTES)
