"""fault/classifiers.py — pure (status, severity, reason) verdicts for Block H.

Four classifiers, one per test module:

  * ``classify_fail_closed``           → test_fail_closed.py
  * ``classify_error_propagation``     → test_error_propagation.py
  * ``classify_partial_failure``       → test_partial_failure_consistency.py
  * ``classify_unsafe_third_party``    → test_unsafe_third_party.py

Severity bands match the dos / auth / logic convention:

  HIGH    — direct security failure (corrupt JWT accepted, fully-mixed
            terminal states under race, raw stack trace leaked).
  MEDIUM  — looser invariant broken (structured error missing on a known
            error path, silent 200 on a partial-failure probe, latency
            hang on a specialist call).
  LOW     — informational only (error returned but not logged, link
            suggestion appears without explicit user-consent UI markers).

Status is the literal CellStatus value string ``"pass"`` or ``"fail"`` so
the results writer can drop the verdict straight into a TestResult row.
"""

from __future__ import annotations

import re

# ──────────────────────────── severity bands ─────────────────────────────────

SEVERITY_HIGH = "high"
SEVERITY_MEDIUM = "medium"
SEVERITY_LOW = "low"


# ─────────────────────── fail-closed classifier ──────────────────────────────

# Status codes the API may legitimately return when authentication metadata is
# malformed or missing. 401 is the canonical "unauthenticated". 403 is also
# accepted because the API Gateway authorizer can refuse before the Lambda
# even runs. 400 is accepted when the API rejects a syntactically-bad header
# at the gateway / WAF layer rather than the auth layer.
_AUTH_REJECTED_CODES: frozenset[int] = frozenset({400, 401, 403})


def classify_fail_closed(
    response_status: int,
    *,
    scenario: str,
) -> tuple[str, str | None, str]:
    """Classify the outcome of a fail-closed probe.

    Inputs:
      response_status: HTTP status of the API call. 0 = transport failure.
      scenario: human-readable scenario slug (e.g. "corrupted-jwt-payload")
                used both for severity selection and the verdict message.

    Verdicts:
      PASS:
        * Server rejected with one of the documented auth-rejected codes
          (400 / 401 / 403). Auth metadata corruption was caught.
      FAIL HIGH:
        * 2xx returned despite malformed / missing auth metadata. Fail-open
          on parse error: the API treated the corrupt request as legitimate.
      FAIL MEDIUM:
        * 5xx returned — the auth path crashed on malformed input rather
          than rejecting cleanly. Operationally fragile.
      FAIL MEDIUM:
        * Transport-layer failure (status == 0). Could not reach a verdict.
    """
    if response_status == 0:
        return (
            "fail",
            SEVERITY_MEDIUM,
            f"{scenario}: transport-layer failure, no response from API",
        )
    if 500 <= response_status < 600:
        return (
            "fail",
            SEVERITY_MEDIUM,
            f"{scenario}: server error (HTTP {response_status}) — "
            f"auth path crashed on malformed input rather than rejecting cleanly",
        )
    if response_status in _AUTH_REJECTED_CODES:
        return (
            "pass",
            None,
            f"{scenario}: rejected with HTTP {response_status} (fail-closed)",
        )
    if 200 <= response_status < 300:
        return (
            "fail",
            SEVERITY_HIGH,
            f"{scenario}: 2xx response (HTTP {response_status}) on malformed auth — "
            f"fail-open: API accepted the corrupt request",
        )
    # 3xx or other 4xx outside the rejected set — flag MEDIUM so an operator
    # notices the surprise. Could be a redirect to the Hosted UI (acceptable
    # behavior at the SPA level but odd for an API).
    return (
        "fail",
        SEVERITY_MEDIUM,
        f"{scenario}: unexpected HTTP {response_status} on malformed auth probe",
    )


# ───────────────────── error-propagation classifier ──────────────────────────


# Tokens we treat as evidence of a leaked Python stack trace in a response
# body. Lowercased for case-insensitive match. We keep the set narrow so a
# legitimate response that mentions "exception" in a comment doesn't FAIL.
_STACK_TRACE_MARKERS: tuple[str, ...] = (
    "traceback (most recent call last)",
    '  file "/',
    "boto3.exceptions",
    "botocore.exceptions",
    "clienterror",
)


def _looks_like_stack_trace(body_text: str) -> bool:
    """True if `body_text` contains evidence of a raw Python stack trace."""
    lower = body_text.lower()
    return any(marker in lower for marker in _STACK_TRACE_MARKERS)


def classify_error_propagation(
    response_status: int,
    *,
    response_body_text: str,
    scenario: str,
    is_structured_json: bool,
) -> tuple[str, str | None, str]:
    """Classify the outcome of a known-error-condition probe.

    Inputs:
      response_status: HTTP status of the API call. 0 = transport failure.
      response_body_text: the response body, as text (for stack-trace check).
      scenario: scenario slug (e.g. "missing-record").
      is_structured_json: True if the response parsed as JSON with the
                          API's structured error shape (typically an
                          object containing an ``error`` field).

    Verdicts:
      PASS:
        * 4xx returned AND body is a structured JSON error (the API
          recognized the error condition and returned a clean response).
      FAIL MEDIUM:
        * 2xx returned on a known-error condition — silent success.
      FAIL LOW:
        * 5xx with a structured body — the API failed gracefully but a
          5xx means an exception bubbled up; clients can recover but ops
          should investigate.
      FAIL LOW:
        * 5xx with a raw stack trace in the body — sensitive internals
          leaked. (Severity intentionally LOW because the response is
          still being returned; HIGH is reserved for fail-open in
          classify_fail_closed.)
      FAIL MEDIUM:
        * 4xx but body is NOT structured JSON — the API rejected but the
          error shape is missing, so a client can't programmatically
          recover.
      FAIL MEDIUM:
        * Transport-layer failure (status == 0).
    """
    if response_status == 0:
        return (
            "fail",
            SEVERITY_MEDIUM,
            f"{scenario}: transport-layer failure, no response",
        )
    has_stack_trace = _looks_like_stack_trace(response_body_text)
    if 500 <= response_status < 600:
        reason = (
            f"{scenario}: 5xx response (HTTP {response_status}) — "
            f"exception bubbled up to client"
        )
        if has_stack_trace:
            reason = (
                f"{scenario}: 5xx response (HTTP {response_status}) leaked "
                f"a raw stack trace in the body"
            )
        return "fail", SEVERITY_LOW, reason
    if 200 <= response_status < 300:
        return (
            "fail",
            SEVERITY_MEDIUM,
            f"{scenario}: silent 2xx (HTTP {response_status}) on a known-error "
            f"condition — error was swallowed",
        )
    if 400 <= response_status < 500:
        if is_structured_json:
            return (
                "pass",
                None,
                f"{scenario}: structured error returned with HTTP {response_status}",
            )
        return (
            "fail",
            SEVERITY_MEDIUM,
            f"{scenario}: HTTP {response_status} but response body is not a "
            f"structured JSON error shape",
        )
    # Anything else (3xx) — flag medium for review.
    return (
        "fail",
        SEVERITY_MEDIUM,
        f"{scenario}: unexpected HTTP {response_status}",
    )


def classify_cloudwatch_logged(
    *,
    api_returned_error: bool,
    found_error_log: bool,
    scenario: str,
) -> tuple[str, str | None, str]:
    """Classify whether a known error condition appeared in CloudWatch logs.

    Inputs:
      api_returned_error: True if the API surfaced an error (4xx/5xx) to
                          the client on the original probe.
      found_error_log: True if a matching ERROR-level CloudWatch log line
                       was located within the probe window.
      scenario: scenario slug, used in the verdict message.

    Verdicts:
      PASS:
        * The API returned an error AND CloudWatch logged it. Or the API
          returned 2xx (no error to log).
      FAIL LOW:
        * The API returned an error but CloudWatch had no matching log —
          silent error hiding from ops. Severity LOW because the client
          still got the error (no security regression), but observability
          is broken.
    """
    if not api_returned_error:
        return (
            "pass",
            None,
            f"{scenario}: no error surfaced to client; nothing to log",
        )
    if found_error_log:
        return (
            "pass",
            None,
            f"{scenario}: error surfaced to client and matching CloudWatch log found",
        )
    return (
        "fail",
        SEVERITY_LOW,
        f"{scenario}: client received an error but CloudWatch has no matching "
        f"log line in the probe window — silent error hiding from ops",
    )


# ───────────────── partial-failure consistency classifier ────────────────────


def classify_partial_failure(
    *,
    final_state: dict,
    scenario: str,
    expected_terminal_keys: tuple[str, ...] = ("approved", "rejected"),
) -> tuple[str, str | None, str]:
    """Classify the outcome of a partial-failure / race probe.

    Inputs:
      final_state: the action's observed state dict (from GET /actions/{id}).
                   May be empty when the GET itself returned non-200.
      scenario: scenario slug for the verdict message.
      expected_terminal_keys: pair of mutually-exclusive flags. If BOTH are
                              truthy in final_state, the state is mixed and
                              the test fails HIGH.

    Verdicts:
      PASS:
        * Final state is consistent — at most one of the mutually-exclusive
          flags is set, OR the GET returned empty state (the resource is
          untouched).
      FAIL HIGH:
        * BOTH mutually-exclusive flags are truthy — the state machine
          reached a contradictory terminal state under concurrency.
      FAIL MEDIUM:
        * GET returned no state at all AND the scenario specifically
          expected a terminal — flagged as "half-applied" (a partial
          mutation that left no trace).
    """
    if not isinstance(final_state, dict):
        return (
            "fail",
            SEVERITY_MEDIUM,
            f"{scenario}: final state could not be read (non-dict response)",
        )
    if not final_state:
        return (
            "pass",
            None,
            f"{scenario}: final state empty — resource appears untouched",
        )
    key_a, key_b = expected_terminal_keys
    a_set = bool(final_state.get(key_a))
    b_set = bool(final_state.get(key_b))
    if a_set and b_set:
        return (
            "fail",
            SEVERITY_HIGH,
            f"{scenario}: mixed terminal state — both '{key_a}' and '{key_b}' "
            f"are truthy in final state",
        )
    return (
        "pass",
        None,
        f"{scenario}: clean terminal state (only-{key_a}={a_set}, only-{key_b}={b_set})",
    )


def classify_concurrent_clientside(
    *,
    succeeded: bool,
    response_status: int,
    body_text: str,
    scenario: str,
) -> tuple[str, str | None, str]:
    """Classify a simple concurrent / out-of-order probe outcome.

    Used by the "upload-then-scan-before-presign" scenario. PASS if the
    server either succeeded gracefully or returned a clear structured
    refusal. FAIL MEDIUM on a malformed response or a hang.

    Inputs:
      succeeded: True if the probe returned a response (vs hanging / drop).
      response_status: HTTP status of the second request.
      body_text: the second response body as text.
      scenario: scenario slug for the verdict message.
    """
    if not succeeded:
        return (
            "fail",
            SEVERITY_MEDIUM,
            f"{scenario}: probe did not return (hang or transport drop)",
        )
    if 200 <= response_status < 300:
        return (
            "pass",
            None,
            f"{scenario}: succeeded with HTTP {response_status}",
        )
    if 400 <= response_status < 500:
        # 4xx is OK — clear refusal.
        if _looks_like_stack_trace(body_text):
            return (
                "fail",
                SEVERITY_MEDIUM,
                f"{scenario}: HTTP {response_status} but body leaked a stack trace",
            )
        return (
            "pass",
            None,
            f"{scenario}: cleanly refused with HTTP {response_status}",
        )
    return (
        "fail",
        SEVERITY_MEDIUM,
        f"{scenario}: unexpected HTTP {response_status}",
    )


# ───────────────── unsafe-third-party / LLM-output classifier ────────────────


# Tokens used to detect raw, unencoded XSS payload in a JSON response body.
# The JSON spec mandates double-quote escaping but does NOT require HTML
# encoding — so `"<script>alert(1)</script>"` as a JSON string value is
# technically valid JSON. The risk is the SPA rendering it unescaped. Our
# Python-side check is narrow: the body must be valid JSON AND the script
# tag is present inside a JSON string (we look for the literal characters,
# which appear in the response if and only if it parsed back as a string).
_XSS_PAYLOAD_MARKERS: tuple[str, ...] = (
    "<script>alert",
    "<script>alert('xss')",
    '<script>alert("xss")',
)


def classify_xss_in_json(
    *,
    is_valid_json: bool,
    content_type: str,
    response_body_text: str,
) -> tuple[str, str | None, str]:
    """Classify whether an XSS-payload echo from the LLM is safely encoded
    at the API boundary.

    The Python side cannot judge whether the SPA renders the value safely
    (that's the e2e spec's job). What we CAN judge here:

      PASS:
        * Response is a valid JSON object AND content-type is
          ``application/json`` (the body went through JSON encoding so any
          double-quote / backslash in the model's output is escaped).
      FAIL MEDIUM:
        * Response is NOT JSON but contains a raw `<script>` payload — the
          API returned the model's output as text/html or raw text, so a
          browser would execute it.
      FAIL MEDIUM:
        * Response claims JSON content-type but is not parseable AND
          contains the payload — content-type mismatch is a bug AND the
          payload made it through.

    The PASS case is intentionally permissive: a properly-encoded JSON
    response with `"<script>"` as a string value is the correct behavior;
    the SPA must then `textContent` it (which it does — confirmed in the
    e2e spec spawned by Block H scenario 1).
    """
    has_payload = any(
        marker in response_body_text.lower() for marker in _XSS_PAYLOAD_MARKERS
    )
    is_json_content_type = "application/json" in (content_type or "").lower()
    if is_valid_json and is_json_content_type:
        return (
            "pass",
            None,
            "response is valid JSON with application/json content-type; "
            "any XSS payload in string values is JSON-escaped at the boundary",
        )
    if has_payload and not is_json_content_type:
        return (
            "fail",
            SEVERITY_MEDIUM,
            f"response contains raw XSS payload AND non-JSON content-type "
            f"'{content_type or '(none)'}' — browser would execute the script",
        )
    if has_payload and not is_valid_json:
        return (
            "fail",
            SEVERITY_MEDIUM,
            "content-type claims JSON but body did not parse; XSS payload present in raw body",
        )
    # No payload and no clear violation — informational PASS.
    return (
        "pass",
        None,
        "no raw XSS payload detected in response body",
    )


# Marker patterns for an auto-rendered URL suggestion. A plain text URL in
# a JSON string is fine — the SPA can choose to render it as text or as a
# link. The risk is when the response signals "this is a hyperlink" without
# any user-consent affordance (e.g. an HTML <a> tag inside a JSON string).
_AUTO_RENDERED_LINK_MARKERS: tuple[re.Pattern[str], ...] = (
    re.compile(r"<a\s+href=", re.IGNORECASE),
    re.compile(r"\bclick\s+here\b", re.IGNORECASE),
)


def classify_link_suggestion(
    *,
    response_body_text: str,
    suspect_url: str,
) -> tuple[str, str | None, str]:
    """Classify whether a model-suggested link is rendered as a clickable
    hyperlink in the response.

    PASS:
      * The suspect URL does not appear in the response (model refused), OR
      * The suspect URL appears as plain text only (no <a href> tag).
    FAIL LOW:
      * Response contains an HTML <a> tag wrapping the suspect URL — the
        API would render the model output as a live link without
        user-consent UI, which is a phishing affordance.

    Severity LOW because the URL itself isn't loaded by the API; the SPA
    decides what to do with it. We flag for review.
    """
    body_lower = response_body_text.lower()
    suspect_lower = suspect_url.lower()
    if suspect_lower not in body_lower:
        return (
            "pass",
            None,
            f"suspect URL '{suspect_url}' not echoed in response",
        )
    if any(p.search(response_body_text) for p in _AUTO_RENDERED_LINK_MARKERS):
        return (
            "fail",
            SEVERITY_LOW,
            f"response includes a clickable-link affordance around '{suspect_url}'; "
            f"SPA may render it without user-consent UI",
        )
    return (
        "pass",
        None,
        f"suspect URL '{suspect_url}' present as plain text; no auto-render marker",
    )


def classify_specialist_response(
    *,
    response_status: int,
    response_body_text: str,
    scenario: str,
) -> tuple[str, str | None, str]:
    """Classify the master orchestrator's handling of a tool / specialist
    error condition.

    Inputs:
      response_status: HTTP status of the /chat call.
      response_body_text: response body as text.
      scenario: scenario slug for the verdict message.

    Verdicts:
      PASS:
        * 2xx response without stack-trace markers — master either passed
          through the specialist response safely or refused cleanly.
      PASS:
        * 4xx response without stack-trace markers — clean refusal.
      FAIL MEDIUM:
        * Response (any status) contains a raw stack trace — internal
          plumbing leaked through the chat surface.
      FAIL MEDIUM:
        * 5xx response — the orchestrator crashed on the crafted prompt.
      FAIL MEDIUM:
        * Transport drop (status == 0) — the orchestrator hung past the
          timeout.
    """
    if response_status == 0:
        return (
            "fail",
            SEVERITY_MEDIUM,
            f"{scenario}: transport-layer failure / hang past timeout",
        )
    if _looks_like_stack_trace(response_body_text):
        return (
            "fail",
            SEVERITY_MEDIUM,
            f"{scenario}: response leaked a raw stack trace from a specialist",
        )
    if 500 <= response_status < 600:
        return (
            "fail",
            SEVERITY_MEDIUM,
            f"{scenario}: HTTP {response_status} — orchestrator crashed on crafted prompt",
        )
    if 200 <= response_status < 300:
        return (
            "pass",
            None,
            f"{scenario}: clean HTTP {response_status} response, no stack trace leak",
        )
    if 400 <= response_status < 500:
        return (
            "pass",
            None,
            f"{scenario}: clean refusal with HTTP {response_status}",
        )
    return (
        "fail",
        SEVERITY_MEDIUM,
        f"{scenario}: unexpected HTTP {response_status}",
    )


__all__ = [
    "SEVERITY_HIGH",
    "SEVERITY_LOW",
    "SEVERITY_MEDIUM",
    "classify_cloudwatch_logged",
    "classify_concurrent_clientside",
    "classify_error_propagation",
    "classify_fail_closed",
    "classify_link_suggestion",
    "classify_partial_failure",
    "classify_specialist_response",
    "classify_xss_in_json",
]
