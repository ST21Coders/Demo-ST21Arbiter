"""Helpers for the curated-corpus fuzz layer.

Pulled out of `test_api_routes.py` so the harness-of-the-harness tests
(`tests/test_fuzz_infrastructure.py`) can exercise the response-classification
logic without spinning up a network stack.

Surface
-------
    load_corpus(corpus_dir) -> dict[family_id, dict]
        Reads every `*.json` file under `corpus_dir` and returns a
        `{family: corpus_dict}` map. The family id is the corpus file's
        `family` field, not its filename. Every file must declare both a
        `family` and a `payloads` list of dicts, each with an `id`.

    build_request_for_route(route, payload, family, valid_auth_header) -> Request
        Returns a `Request` namedtuple describing what to send for one
        (route, payload, family) tuple. Routes with path params get the
        payload spliced into the path on a path_traversal/oversized:path
        family; routes that accept a body get the payload as the value of a
        single body field; GET routes that don't have a path param get the
        payload as a query-string value.

    classify_response(response_status, response_body, payload_value,
                      expected_blocked) -> ClassifiedResponse
        Inspects an HTTP response and decides PASS / FAIL according to the
        contract in the task-12 prompt:
          - 500 from the server is FAIL (high).
          - A known stack-trace marker in the body is FAIL (high).
          - An XSS-style payload echoed back unencoded is FAIL (high).
          - Otherwise PASS.

    make_test_id(route_id, family, payload_id) -> str
        Stable, lowercase, dot-separated id matching the spec §7.3 convention:
        `fuzz.<route-id>.<family>.<payload-id>`. Deterministic — same inputs
        always produce the same id, so the `report.diff` block can match
        across runs.

    is_destructive(method) -> bool
        Returns True for POST / PUT / PATCH / DELETE. Test selection in
        `test_api_routes.py` uses this together with the `--include-destructive`
        CLI flag to decide whether to run the test or skip it.

Stack-trace markers
-------------------
The list is intentionally small and high-signal. It targets the strings that
Lambda (Python runtime) and API Gateway emit when an unhandled exception
escapes. Adding more markers means more false positives; adding fewer means a
real leak might slip past. The current list was chosen by reading
`Infra/functions/api_handler/api_handler.py` and noting that:

  - `logger.exception(...)` lines wrap every DDB / Bedrock call. If one
    surfaces in the body, the marker `Traceback` will be present.
  - Lambda's `aws_request_id` is in every cold-start error response.
  - `errorMessage` + `errorType` are the SDK shape for an unhandled exception.
  - `lambda function` is in the human-readable error string the runtime emits.

Each marker is matched case-insensitive on the response body.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

# ────────────────────────── known leak markers ───────────────────────────────

# Strings that should never appear in a well-formed API response body. Matched
# case-insensitive. Tune sparingly — every entry is a potential false-positive
# trap.
STACK_TRACE_MARKERS: tuple[str, ...] = (
    "Traceback",
    "at <module>",
    "aws_request_id",
    "lambda function",
    "errorType",
    "errorMessage",
    'File "/var/task/',
    "botocore.exceptions",
    "ClientError",
    "boto3",
)


# ────────────────────────── payload classification ───────────────────────────


@dataclass(frozen=True)
class ClassifiedResponse:
    """Classification verdict for one fuzz response.

    Fields
    ------
    status_code   : int — HTTP status code returned (0 if the request did not
                    reach the server at all, e.g. requests library blocked it
                    client-side for a CRLF in a header value).
    verdict       : "pass" | "fail" — does the response satisfy the
                    "API did not crash or leak" contract.
    severity      : "low" | "medium" | "high" — only set when verdict is fail.
                    "high" for stack-trace leaks, 500s, and reflected XSS;
                    "medium" for accepted-when-blocked-was-expected.
    reasons       : tuple of one-line strings describing why a verdict was
                    reached. PASS verdicts get a single explanatory reason
                    too so the transcript is readable.
    """

    status_code: int
    verdict: str
    severity: str | None
    reasons: tuple[str, ...]


def _body_contains_stack_trace(body: str) -> tuple[bool, str]:
    """Return (True, marker) if any known marker is in the response body."""
    lowered = body.lower()
    for marker in STACK_TRACE_MARKERS:
        if marker.lower() in lowered:
            return True, marker
    return False, ""


def _body_reflects_payload(body: str, payload_value: str) -> bool:
    """True if the raw payload appears verbatim in the response body.

    "Verbatim" means an unescaped substring match. Cases that don't count as a
    reflection:
      - the payload is too short to be distinctive (< 6 chars) — too many
        false positives on words like 'OR' or '<' that legitimately appear in
        JSON error messages;
      - the payload's leading character is a quote — JSON-escaped quotes
        appear in every error body, so we look for the bracketed content
        instead.
    """
    if not payload_value or not body:
        return False
    needle = payload_value
    # Strip leading/trailing whitespace and quotes; treat the inner content as
    # the canonical reflection signature.
    needle = needle.strip().strip('"').strip("'")
    if len(needle) < 6:
        return False
    return needle in body


def classify_response(
    status_code: int,
    body: str,
    payload_value: str | None,
    expected_blocked: bool,
    client_blocked: bool = False,
) -> ClassifiedResponse:
    """Inspect a response (or a client-side block) and return a verdict.

    Parameters
    ----------
    status_code : int
        HTTP status the server returned. 0 means the request never landed
        (client-side block, connection refused, etc).
    body : str
        Response body decoded as text. Empty string for status_code==0.
    payload_value : str | None
        The raw payload that was injected — used for the reflection check.
        None when the payload is structural (oversized, malformed-JWT) and
        a reflection check doesn't make sense.
    expected_blocked : bool
        Did the corpus author expect the API to refuse this payload?
    client_blocked : bool
        True if the HTTP client (requests) refused to send the request before
        it hit the wire (e.g. CRLF in a header). Always classified as PASS
        — the attack didn't reach the server.

    Returns
    -------
    ClassifiedResponse
    """
    reasons: list[str] = []

    if client_blocked:
        return ClassifiedResponse(
            status_code=0,
            verdict="pass",
            severity=None,
            reasons=("requests library refused to send the request (client-blocked)",),
        )

    # 500 from the server is always a fail. Even an explicit "500 Internal
    # Server Error" with no body is a fail per the task-12 contract.
    if status_code == 500:
        reasons.append("server returned HTTP 500")
        return ClassifiedResponse(
            status_code=status_code,
            verdict="fail",
            severity="high",
            reasons=tuple(reasons),
        )

    has_trace, marker = _body_contains_stack_trace(body)
    if has_trace:
        reasons.append(f"response body contains stack-trace marker {marker!r}")
        return ClassifiedResponse(
            status_code=status_code,
            verdict="fail",
            severity="high",
            reasons=tuple(reasons),
        )

    # Reflection check only when we have a payload value to look for.
    if payload_value and expected_blocked and 200 <= status_code < 300:
        if _body_reflects_payload(body, payload_value):
            reasons.append(
                "expected_blocked=true but response body reflects the payload verbatim"
            )
            return ClassifiedResponse(
                status_code=status_code,
                verdict="fail",
                severity="high",
                reasons=tuple(reasons),
            )

    # Anything else (including legitimate 200, expected 400/401/403/404/422)
    # is a pass. The corpus author's expected_blocked annotation only fails
    # the run via the reflection check above — a 200 that doesn't echo the
    # payload is still a pass, because the server may legitimately accept
    # the value (DDB string), store it, and return a sanitized echo.
    if 400 <= status_code < 500:
        reasons.append(f"server rejected with HTTP {status_code} (sane error)")
    elif 200 <= status_code < 300:
        reasons.append(f"server accepted with HTTP {status_code} (no reflection)")
    elif status_code == 0:
        reasons.append("request did not reach the server (network/dns/timeout)")
    else:
        reasons.append(f"server returned HTTP {status_code}")

    return ClassifiedResponse(
        status_code=status_code,
        verdict="pass",
        severity=None,
        reasons=tuple(reasons),
    )


# ────────────────────────────── test ids ─────────────────────────────────────


def make_test_id(route_id: str, family: str, payload_id: str) -> str:
    """Build a stable test id matching spec §7.3.

    Convention: `fuzz.<route-id>.<family>.<payload-id>`. All segments are
    lowercased, with non-`[a-z0-9-]` characters replaced by `-`. Two different
    inputs that normalize to the same id is an error the caller can detect by
    comparing call counts.
    """

    def _norm(s: str) -> str:
        out = []
        for ch in s.lower():
            if ch.isalnum() or ch == "-":
                out.append(ch)
            else:
                out.append("-")
        # Collapse runs of -.
        result = "".join(out)
        while "--" in result:
            result = result.replace("--", "-")
        return result.strip("-")

    return f"fuzz.{_norm(route_id)}.{_norm(family)}.{_norm(payload_id)}"


# ─────────────────────────── route dispatch ──────────────────────────────────


_DESTRUCTIVE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def is_destructive(method: str) -> bool:
    """True if the HTTP method might mutate state."""
    return (method or "").upper() in _DESTRUCTIVE_METHODS


def route_has_path_param(route_path: str) -> bool:
    """`/findings/{id}` style routes have a `{name}` placeholder."""
    return "{" in (route_path or "") and "}" in (route_path or "")


# ─────────────────────────── corpus loading ──────────────────────────────────


def load_corpus(corpus_dir: Path | str) -> dict[str, dict]:
    """Read every `*.json` file under `corpus_dir` and return a family map.

    Each file must declare:
        family   : str — short id (e.g. "xss", "sqli")
        payloads : list[dict] — each entry has at least an `id`

    Raises:
        FileNotFoundError if the directory does not exist.
        ValueError on a malformed file (missing required keys, duplicate
            family id, duplicate payload id within a family).
    """
    corpus_dir = Path(corpus_dir)
    if not corpus_dir.is_dir():
        raise FileNotFoundError(
            f"corpus directory {corpus_dir} does not exist or is not a directory"
        )

    families: dict[str, dict] = {}
    for path in sorted(corpus_dir.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}: invalid JSON ({exc})") from exc
        if not isinstance(raw, dict):
            raise ValueError(f"{path}: top-level JSON value must be an object")
        family = raw.get("family")
        payloads = raw.get("payloads")
        if not isinstance(family, str) or not family.strip():
            raise ValueError(f"{path}: missing or empty 'family' field")
        if not isinstance(payloads, list):
            raise ValueError(f"{path}: 'payloads' must be a list")
        if family in families:
            raise ValueError(
                f"{path}: duplicate family id {family!r} "
                f"(also declared in {families[family]['_source']!r})"
            )
        # Per-payload sanity.
        seen_ids: set[str] = set()
        for idx, entry in enumerate(payloads):
            if not isinstance(entry, dict):
                raise ValueError(f"{path}: payloads[{idx}] is not an object")
            pid = entry.get("id")
            if not isinstance(pid, str) or not pid.strip():
                raise ValueError(f"{path}: payloads[{idx}] missing 'id'")
            if pid in seen_ids:
                raise ValueError(
                    f"{path}: duplicate payload id {pid!r} within family {family!r}"
                )
            seen_ids.add(pid)
        families[family] = {**raw, "_source": str(path)}
    return families


# ─────────────────────────── results writer ──────────────────────────────────


def fuzz_results_path(run_dir: Path | str | None) -> Path:
    """Where the fuzz layer writes its `results.json`.

    Mirrors the convention from the E2E reporter: when `RUN_DIR` is set
    (orchestrator runs), write under `${RUN_DIR}/fuzz/results.json`;
    otherwise fall back to `test-reports/_local/fuzz/results.json` so a
    standalone `pytest fuzz/` run still produces an artifact.
    """
    if run_dir:
        return Path(run_dir) / "fuzz" / "results.json"
    # Locate the harness root (tests-adversarial/) by walking up from this
    # file. _payloads.py lives at tests-adversarial/fuzz/_payloads.py so
    # parents[1] is the harness root.
    harness_root = Path(__file__).resolve().parents[1]
    return harness_root / "test-reports" / "_local" / "fuzz" / "results.json"


__all__ = [
    "STACK_TRACE_MARKERS",
    "ClassifiedResponse",
    "classify_response",
    "make_test_id",
    "is_destructive",
    "route_has_path_param",
    "load_corpus",
    "fuzz_results_path",
]
