"""logic/classifiers.py — pure (status, severity, reason) verdicts for Block F.

Three classifiers, one per test module:

  * ``classify_state_transition``  → test_action_state_machine.py
  * ``classify_concurrent_writes`` → test_race_conditions.py
  * ``classify_field_exposure``    → test_field_exposure.py

Plus a small recursive JSON walker used by the field-exposure probe.

Severity bands match the dos / auth / headers convention:

  HIGH    — direct security failure (workflow bypass, race-window double
            write, credential-shaped field in a customer response).
  MEDIUM  — looser invariant broken (idempotency missing, cross-persona
            field leak, server crash under concurrency).
  LOW     — informational only (internal id / mongo version key leak).

Status is the literal CellStatus value string ``"pass"`` or ``"fail"`` so
the results writer can drop the verdict straight into a TestResult row.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

# ──────────────────────────── severity bands ─────────────────────────────────

SEVERITY_HIGH = "high"
SEVERITY_MEDIUM = "medium"
SEVERITY_LOW = "low"

# Maximum recursion depth the JSON walker descends. Six levels is enough for
# the deepest legitimate ARBITER response shape (dashboard → trend → entries →
# nested totals) without runaway recursion on a hostile response.
JSON_WALK_DEPTH_CAP = 6

# Throttle for the logic layer. Workflow probes are sequential by nature so a
# low rate is fine; we deliberately keep it well below the DoS layer's ceiling
# so a misconfigured run can't accidentally rate-limit-test the API.
LOGIC_RPS = 5

# ──────────────────────── state-transition classifier ────────────────────────

# HTTP codes that legitimately reject an invalid state transition. The API
# handler uses 409 for "wrong state" (the comment at line 1639 in
# api_handler.py: "CR in status X cannot execute"). 400 is the generic
# "bad request" the SPA also tolerates. 403 / 404 are accepted because the
# handler's caller-not-an-approver branch returns 403 and a missing CR
# returns 404 — both legitimate refusals that prevent a workflow bypass.
_REJECTED_STATUS_CODES: frozenset[int] = frozenset({400, 403, 404, 409, 422})


def classify_state_transition(
    response_status: int,
    *,
    transition_kind: str,
) -> tuple[str, str | None, str]:
    """Classify the outcome of an invalid state-transition probe.

    Inputs:
      response_status: HTTP status of the API call. 0 = transport failure.
      transition_kind: one of "skip-approve" | "double-approve"
                       | "reject-after-execute" | "escalate-from-terminal".
                       Used both for severity selection and the verdict
                       message.

    Verdicts:
      PASS:
        * Server refused with one of the documented status codes.
      FAIL HIGH:
        * `transition_kind == "skip-approve"` and the server returned 2xx —
          actions could be executed without approval. Direct security
          failure.
      FAIL MEDIUM:
        * Any other 2xx (double-approve / reject-after-execute /
          escalate-from-terminal). Idempotency / lifecycle is broken but
          the security impact is bounded by the prior state.
      FAIL HIGH:
        * Server returned 5xx (the workflow engine crashed under the probe).
      FAIL MEDIUM:
        * Transport-layer failure (status == 0) — the probe couldn't
          reach a verdict.
    """
    if response_status == 0:
        return (
            "fail",
            SEVERITY_MEDIUM,
            f"{transition_kind}: transport-layer failure, no response",
        )
    if 500 <= response_status < 600:
        return (
            "fail",
            SEVERITY_HIGH,
            f"{transition_kind}: server error (HTTP {response_status}) — "
            f"workflow engine crashed on invalid transition",
        )
    if response_status in _REJECTED_STATUS_CODES:
        return (
            "pass",
            None,
            f"{transition_kind}: rejected with HTTP {response_status}",
        )
    if 200 <= response_status < 300:
        if transition_kind == "skip-approve":
            return (
                "fail",
                SEVERITY_HIGH,
                "skip-approve: execute without prior approval succeeded — workflow bypass",
            )
        return (
            "fail",
            SEVERITY_MEDIUM,
            f"{transition_kind}: invalid transition accepted (HTTP {response_status}) — "
            f"workflow state machine bypassed",
        )
    # Anything else (3xx, exotic 4xx outside the rejected set) is
    # informationally a fail but doesn't fit either severity cleanly. Pick
    # MEDIUM so the operator notices.
    return (
        "fail",
        SEVERITY_MEDIUM,
        f"{transition_kind}: unexpected HTTP {response_status}",
    )


# ───────────────────── concurrent-writes classifier ──────────────────────────


def classify_concurrent_writes(
    status_codes: Sequence[int],
    *,
    expected_successes: int = 1,
) -> tuple[str, str | None, str]:
    """Classify the outcome of a concurrent same-resource probe.

    Inputs:
      status_codes: per-request HTTP status from the fan-out (0 = transport
                    error). Length is the concurrency level.
      expected_successes: how many 2xx the server *should* return. Default
                          1 — for approve/reject/delete-once the first
                          caller wins and the rest must be rejected with
                          4xx.

    Verdicts:
      PASS:
        * Exactly `expected_successes` 2xx; the remainder are 4xx
          (typically 409 Conflict or 404 after a successful delete races).
      FAIL HIGH:
        * More than `expected_successes` 2xx — race condition: the same
          resource was approved / deleted multiple times.
      FAIL MEDIUM:
        * All responses are 5xx (server crashed under concurrency).
      FAIL MEDIUM:
        * Zero 2xx (the resource ended in a stuck state — the first call
          should have succeeded). This catches the case where every
          concurrent request raced and lost; a healthy system serializes.
      FAIL MEDIUM:
        * Any zero-status (connection drop) without a clear single-winner.
    """
    if not status_codes:
        return "fail", SEVERITY_MEDIUM, "no concurrent responses recorded"

    successes = sum(1 for c in status_codes if 200 <= c < 300)
    server_errors = sum(1 for c in status_codes if 500 <= c < 600)
    transport_errors = sum(1 for c in status_codes if c == 0)
    total = len(status_codes)

    if successes > expected_successes:
        return (
            "fail",
            SEVERITY_HIGH,
            f"race condition: {successes} successful responses (expected at most "
            f"{expected_successes}) across {total} concurrent requests",
        )

    if server_errors == total:
        return (
            "fail",
            SEVERITY_MEDIUM,
            f"all {total} concurrent requests returned 5xx — server crashed under load",
        )

    if successes == 0:
        return (
            "fail",
            SEVERITY_MEDIUM,
            f"no successful response across {total} concurrent requests "
            f"(statuses={list(status_codes)})",
        )

    if transport_errors and successes == expected_successes:
        # One winner, but other callers were dropped at the transport layer.
        # The single-winner invariant held; flag MEDIUM so the operator
        # notices the dropped connections separately.
        return (
            "fail",
            SEVERITY_MEDIUM,
            f"{successes} winner but {transport_errors} transport drop(s) — "
            f"single-winner held but connections leaked",
        )

    return (
        "pass",
        None,
        f"single winner with {expected_successes} success and "
        f"{total - successes} rejection(s) (statuses={list(status_codes)})",
    )


# ───────────────────── field-exposure classifier ─────────────────────────────

# Field-name patterns that should NEVER appear in a customer-visible response.
# Each pattern is compiled case-insensitive and matched against the lowercased
# field-key. The patterns are intentionally narrow: too broad and the harness
# floods with false positives ("emailed" is not "email"), too narrow and the
# classifier misses a `passwordHash` because it only matched `password`.
_HIGH_SEVERITY_KEY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^password(_?hash)?$", re.IGNORECASE),
    re.compile(r"^password_?hash$", re.IGNORECASE),
    re.compile(r"^secret(_?key)?$", re.IGNORECASE),
    re.compile(r"^api[_-]?key$", re.IGNORECASE),
    re.compile(r"^apikey$", re.IGNORECASE),
    re.compile(r"^private[_-]?key$", re.IGNORECASE),
    re.compile(r"^aws[_-]?access[_-]?key(_?id)?$", re.IGNORECASE),
    re.compile(r"^aws[_-]?secret[_-]?(access[_-]?)?key$", re.IGNORECASE),
)

# LOW-severity internal-id leak patterns. Mongo version keys, raw internal IDs.
_LOW_SEVERITY_KEY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^_internal$", re.IGNORECASE),
    re.compile(r"^internal[_-]?id$", re.IGNORECASE),
    re.compile(r"^__v$"),  # Mongo version key — case sensitive
    re.compile(r"^_id$"),  # Raw Mongo id — case sensitive
)

# Cognito groups key — present in JWT claims but should never echo back in
# customer responses except for the caller's own groups. The classifier needs
# context (caller groups) to decide whether it's a leak.
_COGNITO_GROUPS_KEY = "cognito:groups"

# Email key — a response may legitimately include the caller's own email. Any
# OTHER email is a cross-user leak.
_EMAIL_KEY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^email$", re.IGNORECASE),
    re.compile(r"^user[_-]?email$", re.IGNORECASE),
)


def _matches_any(key: str, patterns: tuple[re.Pattern[str], ...]) -> bool:
    """True if `key` matches any pattern in the tuple."""
    return any(p.match(key) for p in patterns)


def walk_json_for_leaks(
    obj: object,
    *,
    caller_groups: Sequence[str],
    caller_email: str,
    depth_cap: int = JSON_WALK_DEPTH_CAP,
) -> list[tuple[str, str, str]]:
    """Walk a JSON-shaped object and collect sensitive-field hits.

    Returns a list of `(severity, key, reason)` tuples. Empty list means
    the response is clean.

    Inputs:
      obj: the parsed JSON (dict / list / scalar — any shape).
      caller_groups: the caller's own Cognito groups. A `cognito:groups`
                     field whose value matches the caller's groups is NOT
                     a leak.
      caller_email: the caller's own email. An `email` field whose value
                    matches the caller is NOT a leak.
      depth_cap: maximum recursion depth. Default JSON_WALK_DEPTH_CAP (6).
                 Hitting the cap silently stops descent (the partial walk
                 still surfaces leaks at shallow depth).

    Behavior:
      * Dict keys are matched against the credential-shape, internal-id,
        and email patterns above.
      * `cognito:groups` is compared against `caller_groups`; any group
        not in the caller's set is flagged MEDIUM.
      * Email fields are compared against `caller_email`; any other value
        is flagged MEDIUM.
      * List elements are walked recursively.
      * Scalars are ignored (the classifier inspects field keys, not
        free-form string contents — a string containing the word "secret"
        in a legitimate field like `comment` is not a leak).
    """
    hits: list[tuple[str, str, str]] = []
    caller_groups_set = {g.lower() for g in caller_groups if g}
    caller_email_lower = (caller_email or "").lower().strip()
    _walk_inner(
        obj,
        depth=0,
        depth_cap=depth_cap,
        hits=hits,
        caller_groups_set=caller_groups_set,
        caller_email_lower=caller_email_lower,
    )
    return hits


def _walk_inner(
    obj: object,
    *,
    depth: int,
    depth_cap: int,
    hits: list[tuple[str, str, str]],
    caller_groups_set: set[str],
    caller_email_lower: str,
) -> None:
    """Recursive helper. Mutates `hits` in place."""
    if depth >= depth_cap:
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            if not isinstance(key, str):
                continue
            # HIGH-severity credential-shape keys: presence alone is the
            # leak, regardless of value.
            if _matches_any(key, _HIGH_SEVERITY_KEY_PATTERNS):
                hits.append(
                    (
                        SEVERITY_HIGH,
                        key,
                        f"credential-shaped field '{key}' present in response",
                    )
                )
            # LOW-severity internal-id keys: presence alone.
            elif _matches_any(key, _LOW_SEVERITY_KEY_PATTERNS):
                hits.append(
                    (
                        SEVERITY_LOW,
                        key,
                        f"internal-id field '{key}' leaked in response",
                    )
                )
            # cognito:groups — compare value to caller's groups.
            elif key == _COGNITO_GROUPS_KEY:
                leaked = _cognito_group_leak(value, caller_groups_set)
                if leaked:
                    hits.append(
                        (
                            SEVERITY_MEDIUM,
                            key,
                            f"cognito:groups field contains other-persona group(s): {leaked}",
                        )
                    )
            # email fields — compare value to caller's email.
            elif _matches_any(key, _EMAIL_KEY_PATTERNS):
                if _email_is_other(value, caller_email_lower):
                    hits.append(
                        (
                            SEVERITY_MEDIUM,
                            key,
                            f"email field '{key}' contains a value other than the caller's email",
                        )
                    )
            # Always descend into the value regardless of key match — a
            # leak might be nested under a benign key.
            _walk_inner(
                value,
                depth=depth + 1,
                depth_cap=depth_cap,
                hits=hits,
                caller_groups_set=caller_groups_set,
                caller_email_lower=caller_email_lower,
            )
    elif isinstance(obj, list):
        for item in obj:
            _walk_inner(
                item,
                depth=depth + 1,
                depth_cap=depth_cap,
                hits=hits,
                caller_groups_set=caller_groups_set,
                caller_email_lower=caller_email_lower,
            )
    # Scalars: nothing to do.


def _cognito_group_leak(value: object, caller_groups_set: set[str]) -> list[str]:
    """Return groups in `value` that aren't in the caller's set. Empty list
    means no leak.

    Tolerates both shapes — a list of group strings (Cognito's standard) or a
    comma-separated string (API Gateway's flattened form).
    """
    if isinstance(value, str):
        groups = [g.strip().lower() for g in value.split(",") if g.strip()]
    elif isinstance(value, list):
        groups = [str(g).strip().lower() for g in value if g]
    else:
        return []
    return [g for g in groups if g not in caller_groups_set]


def _email_is_other(value: object, caller_email_lower: str) -> bool:
    """True if `value` is a non-empty string that isn't the caller's email.

    Empty / None / non-string values are not leaks (the field may be
    optional). An empty caller_email_lower disables the check (we cannot
    tell whose email it is, so we don't flag).
    """
    if not isinstance(value, str):
        return False
    cleaned = value.strip().lower()
    if not cleaned:
        return False
    if not caller_email_lower:
        return False
    return cleaned != caller_email_lower


def classify_field_exposure(
    response_body: object,
    *,
    caller_groups: Sequence[str],
    caller_email: str,
) -> tuple[str, str | None, str]:
    """Classify one (response, caller) for sensitive-field leakage.

    Walks the response body with `walk_json_for_leaks`, then picks the
    highest-severity hit (HIGH > MEDIUM > LOW). PASS if zero hits.

    Returns ``(status, severity, reason)`` shaped the same as every other
    classifier in the harness.
    """
    hits = walk_json_for_leaks(
        response_body,
        caller_groups=caller_groups,
        caller_email=caller_email,
    )
    if not hits:
        return "pass", None, "no sensitive-field patterns detected in response"

    # Severity precedence: HIGH > MEDIUM > LOW.
    severities = {sev for sev, _, _ in hits}
    if SEVERITY_HIGH in severities:
        worst = SEVERITY_HIGH
    elif SEVERITY_MEDIUM in severities:
        worst = SEVERITY_MEDIUM
    else:
        worst = SEVERITY_LOW

    # Pick the first hit at the worst severity so the reason names a
    # specific field (the operator can grep the response for it).
    matching = [h for h in hits if h[0] == worst]
    _, key, reason = matching[0]
    if len(hits) > 1:
        reason = f"{reason} (+{len(hits) - 1} other field(s))"
    return "fail", worst, reason


__all__ = [
    "JSON_WALK_DEPTH_CAP",
    "LOGIC_RPS",
    "SEVERITY_HIGH",
    "SEVERITY_LOW",
    "SEVERITY_MEDIUM",
    "classify_concurrent_writes",
    "classify_field_exposure",
    "classify_state_transition",
    "walk_json_for_leaks",
]
