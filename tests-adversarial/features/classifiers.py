"""features/classifiers.py — pure (status, severity, reason) verdicts.

Six classifiers, one per test module:

  * ``classify_chat_roundtrip``         → test_chat_roundtrip.py
  * ``classify_specialist_routing``     → test_specialist_routing.py
  * ``classify_conversation_persistence`` → test_conversation_persistence.py
  * ``classify_token_usage_recorded``   → test_token_usage_recorded.py
  * ``classify_kb_retrieval``           → test_kb_retrieval.py
  * ``classify_chat_cost``              → test_chat_cost_bounds.py

Severity bands match the prompt's positive-feature convention:

  HIGH    — feature is completely broken (chat 5xx, no reply, page not found,
            cost exploded, conversation never persisted).
  MEDIUM  — feature works but degraded (latency over budget, response missing
            expected keyword, KB grounding absent, cost over expected band).
  LOW     — cosmetic / nice-to-have (not used by every classifier — reserved
            for future polish gradients).

Status is the literal CellStatus value string ``"pass"`` or ``"fail"`` so the
results writer can drop the verdict straight into a TestResult row. The few
classifiers that can SKIP also return the literal ``"skipped"``.
"""

from __future__ import annotations

from collections.abc import Sequence

# ───────────────────────────── severity bands ───────────────────────────────

SEVERITY_HIGH = "high"
SEVERITY_MEDIUM = "medium"
SEVERITY_LOW = "low"

# Tunable thresholds — kept at module scope so unit tests can import and
# reference them (matches dos/classifiers.py convention).

# A response shorter than this is treated as "empty / stub" — the master
# orchestrator's smallest legitimate reply during smoke includes at least the
# acknowledgement plus a one-sentence answer.
MIN_REPLY_CHARS = 20

# Wall-clock budget per /chat round-trip. Above this the verdict downgrades to
# FAIL MEDIUM. The master's typical reply lands in 8-15 s; 30 s is the cliff.
CHAT_LATENCY_BUDGET_SECONDS = 30.0

# Cost ceilings per chat turn. Nova 2 Lite at the master is well under a cent
# per turn at typical lengths; anything above $0.10 is broken.
COST_PASS_USD = 0.01
COST_FAIL_HIGH_USD = 0.10

# Per-tool keyword sets the specialist-routing classifier uses to confirm the
# reply mentions the tool's domain. The master may use any of the listed
# words; matching is substring + case-insensitive.
SPECIALIST_KEYWORDS: dict[str, tuple[str, ...]] = {
    "master.sharepoint_lookup": (
        "policy",
        "policies",
        "document",
        "sharepoint",
        "retention",
    ),
    "master.awsconfig_lookup": (
        "config",
        "finding",
        "findings",
        "compliance",
        "compliant",
        "s3",
    ),
    "master.zscaler_lookup": (
        "url",
        "category",
        "categories",
        "block",
        "blocked",
        "zscaler",
    ),
    "master.jira_lookup": ("ticket", "tickets", "issue", "issues", "jira"),
    "master.paloalto_lookup": ("firewall", "alert", "alerts", "ngfw", "palo"),
}

# KB grounding markers. The Knowledge Base citations show up as
# `[1]`-style citation markers, `Source:` blocks, or known document phrases.
# We're permissive — any one match passes.
KB_GROUNDING_MARKERS = (
    "[1]",
    "[2]",
    "source:",
    "according to",
    "the policy",
    "the document",
    "the guideline",
    "gdpr",
    "retention",
    "compliance documentation",
)


# ───────────────────────── chat-roundtrip classifier ─────────────────────────


def classify_chat_roundtrip(
    http_status: int,
    reply_text: str | None,
    latency_seconds: float,
) -> tuple[str, str | None, str]:
    """Classify the outcome of a single /chat call.

    PASS:
      * HTTP 200, reply >= MIN_REPLY_CHARS, latency < CHAT_LATENCY_BUDGET.
    FAIL HIGH:
      * 5xx, or HTTP 0 (transport failure), or reply is None / missing.
    FAIL MEDIUM:
      * HTTP 200 but reply shorter than MIN_REPLY_CHARS.
      * HTTP 200 and reply long enough but latency over budget.

    Inputs:
      http_status:  HTTP status code returned by the chat endpoint. Use 0 for
                    transport errors so the classifier can flag them HIGH.
      reply_text:   The reply string from the response body, or None if no
                    `reply` field was found.
      latency_seconds: Wall-clock of the round-trip.
    """
    if http_status == 0:
        return (
            "fail",
            SEVERITY_HIGH,
            f"transport-layer failure on /chat (latency {latency_seconds:.1f}s)",
        )
    if 500 <= http_status < 600:
        return (
            "fail",
            SEVERITY_HIGH,
            f"chat returned HTTP {http_status} — feature broken",
        )
    if http_status != 200:
        # Any other non-200 (404 / 401 / 403) is still a high-severity break
        # since chat is the master workflow.
        return (
            "fail",
            SEVERITY_HIGH,
            f"chat returned unexpected HTTP {http_status}",
        )
    if reply_text is None:
        return (
            "fail",
            SEVERITY_HIGH,
            "chat returned 200 but response body had no 'reply' field",
        )
    if len(reply_text) < MIN_REPLY_CHARS:
        return (
            "fail",
            SEVERITY_MEDIUM,
            (
                f"chat reply is too short ({len(reply_text)} chars, "
                f"need >= {MIN_REPLY_CHARS}) — stub or truncated response"
            ),
        )
    if latency_seconds > CHAT_LATENCY_BUDGET_SECONDS:
        return (
            "fail",
            SEVERITY_MEDIUM,
            (
                f"chat reply ok ({len(reply_text)} chars) but latency "
                f"{latency_seconds:.1f}s exceeds {CHAT_LATENCY_BUDGET_SECONDS:.0f}s budget"
            ),
        )
    return (
        "pass",
        None,
        f"chat replied {len(reply_text)} chars in {latency_seconds:.1f}s",
    )


# ───────────────────────── specialist-routing classifier ──────────────────────


def classify_specialist_routing(
    tool_id: str,
    http_status: int,
    reply_text: str | None,
) -> tuple[str, str | None, str]:
    """Classify whether the reply mentions the expected tool's domain.

    PASS:
      * HTTP 200, reply non-empty, at least one of SPECIALIST_KEYWORDS[tool_id]
        appears (substring, case-insensitive).
    FAIL MEDIUM:
      * HTTP 200, reply non-empty, none of the keywords match — the master
        didn't route to the expected specialist (or the specialist returned
        unrelated content).
    FAIL HIGH:
      * 5xx, missing reply, or unknown tool_id.

    Inputs:
      tool_id:      A key in SPECIALIST_KEYWORDS. Unknown ids return FAIL HIGH
                    because the test plan should never call this with one.
      http_status:  HTTP status code; 0 means transport failure.
      reply_text:   The reply body string.
    """
    keywords = SPECIALIST_KEYWORDS.get(tool_id)
    if keywords is None:
        return (
            "fail",
            SEVERITY_HIGH,
            f"unknown tool_id '{tool_id}' — extend SPECIALIST_KEYWORDS",
        )
    if http_status == 0 or 500 <= http_status < 600:
        return (
            "fail",
            SEVERITY_HIGH,
            f"specialist routing probe got HTTP {http_status}",
        )
    if http_status != 200:
        return (
            "fail",
            SEVERITY_HIGH,
            f"specialist routing probe got unexpected HTTP {http_status}",
        )
    if not reply_text:
        return (
            "fail",
            SEVERITY_HIGH,
            "specialist routing probe returned empty reply",
        )
    lowered = reply_text.lower()
    matched = [k for k in keywords if k in lowered]
    if matched:
        return (
            "pass",
            None,
            f"reply mentions '{matched[0]}' — routing to {tool_id} confirmed",
        )
    return (
        "fail",
        SEVERITY_MEDIUM,
        (
            f"reply did not mention any domain keyword "
            f"({', '.join(keywords)}) — specialist may not have been invoked"
        ),
    )


# ───────────────────── conversation-persistence classifier ─────────────────────


def classify_conversation_persistence(
    http_status: int,
    session_ids_in_list: Sequence[str],
    expected_session_id: str,
) -> tuple[str, str | None, str]:
    """Classify whether a freshly-created session_id appears in GET /conversations.

    PASS:
      * HTTP 200, expected_session_id appears in session_ids_in_list.
    FAIL HIGH:
      * 5xx, transport drop, missing session id.
    FAIL MEDIUM:
      * HTTP 200 but the session id is absent — write didn't propagate within
        the polling window.
    """
    if http_status == 0 or 500 <= http_status < 600:
        return (
            "fail",
            SEVERITY_HIGH,
            f"GET /conversations returned HTTP {http_status}",
        )
    if http_status != 200:
        return (
            "fail",
            SEVERITY_HIGH,
            f"GET /conversations returned unexpected HTTP {http_status}",
        )
    if not expected_session_id:
        return (
            "fail",
            SEVERITY_HIGH,
            "no expected_session_id provided to classifier — programmer error",
        )
    if expected_session_id in session_ids_in_list:
        return (
            "pass",
            None,
            f"session '{expected_session_id}' appears in conversation list",
        )
    return (
        "fail",
        SEVERITY_MEDIUM,
        (
            f"session '{expected_session_id}' missing from conversation list "
            f"(saw {len(session_ids_in_list)} other sessions)"
        ),
    )


# ───────────────────── token-usage-recorded classifier ─────────────────────


def classify_token_usage_recorded(
    http_status: int,
    new_record_count: int,
) -> tuple[str, str | None, str]:
    """Classify whether a new token-usage row landed after a chat.

    PASS:
      * HTTP 200, at least one new record with timestamp > pre-chat time.
    FAIL HIGH:
      * 5xx / transport error.
    FAIL MEDIUM:
      * HTTP 200, no new records appeared within the polling window.

    Inputs:
      http_status:     HTTP status of GET /token-usage.
      new_record_count: count of records with timestamp > the marker.
    """
    if http_status == 0 or 500 <= http_status < 600:
        return (
            "fail",
            SEVERITY_HIGH,
            f"GET /token-usage returned HTTP {http_status}",
        )
    if http_status == 403:
        # CISO-only endpoint; if the test ran as CISO and got 403 that's a
        # real break of the route, not a permission issue.
        return (
            "fail",
            SEVERITY_HIGH,
            "GET /token-usage returned 403 — CISO IdToken was rejected",
        )
    if http_status != 200:
        return (
            "fail",
            SEVERITY_HIGH,
            f"GET /token-usage returned unexpected HTTP {http_status}",
        )
    if new_record_count > 0:
        return (
            "pass",
            None,
            f"{new_record_count} new token-usage row(s) recorded after chat",
        )
    return (
        "fail",
        SEVERITY_MEDIUM,
        "no new token-usage rows appeared within the polling window",
    )


# ─────────────────────────── kb-retrieval classifier ──────────────────────────


def classify_kb_retrieval(
    http_status: int,
    reply_text: str | None,
) -> tuple[str, str | None, str]:
    """Classify whether a compliance prompt triggered Knowledge Base grounding.

    PASS:
      * HTTP 200, reply contains at least one KB_GROUNDING_MARKER substring.
    FAIL MEDIUM:
      * HTTP 200, reply present but no grounding markers — generic response.
    FAIL HIGH:
      * 5xx / missing reply / transport drop.
    """
    if http_status == 0 or 500 <= http_status < 600:
        return (
            "fail",
            SEVERITY_HIGH,
            f"KB-retrieval probe got HTTP {http_status}",
        )
    if http_status != 200:
        return (
            "fail",
            SEVERITY_HIGH,
            f"KB-retrieval probe got unexpected HTTP {http_status}",
        )
    if not reply_text:
        return (
            "fail",
            SEVERITY_HIGH,
            "KB-retrieval probe returned empty reply",
        )
    lowered = reply_text.lower()
    matched = [m for m in KB_GROUNDING_MARKERS if m in lowered]
    if matched:
        return (
            "pass",
            None,
            f"reply contains KB-grounding marker '{matched[0]}'",
        )
    return (
        "fail",
        SEVERITY_MEDIUM,
        (
            "reply did not contain any KB-grounding marker "
            f"(checked {len(KB_GROUNDING_MARKERS)} markers) — generic response"
        ),
    )


# ─────────────────────────── chat-cost classifier ────────────────────────────


def classify_chat_cost(cost_usd: float) -> tuple[str, str | None, str]:
    """Classify a single chat-turn cost against the expected bounds.

    PASS:
      * cost < COST_PASS_USD ($0.01).
    FAIL MEDIUM:
      * COST_PASS_USD <= cost < COST_FAIL_HIGH_USD ($0.01 – $0.10).
    FAIL HIGH:
      * cost >= COST_FAIL_HIGH_USD ($0.10) — broken model selection or runaway
        loop.
      * cost is negative or NaN.
    """
    if cost_usd != cost_usd:  # NaN check (NaN != NaN)
        return (
            "fail",
            SEVERITY_HIGH,
            "computed cost is NaN — something broke upstream",
        )
    if cost_usd < 0:
        return (
            "fail",
            SEVERITY_HIGH,
            f"computed cost is negative (${cost_usd:.4f}) — programmer error",
        )
    if cost_usd >= COST_FAIL_HIGH_USD:
        return (
            "fail",
            SEVERITY_HIGH,
            (
                f"chat-turn cost ${cost_usd:.4f} >= ${COST_FAIL_HIGH_USD:.2f} "
                "cliff — model selection or token loop broken"
            ),
        )
    if cost_usd >= COST_PASS_USD:
        return (
            "fail",
            SEVERITY_MEDIUM,
            (
                f"chat-turn cost ${cost_usd:.4f} >= ${COST_PASS_USD:.2f} "
                "PASS band — more expensive than expected"
            ),
        )
    return (
        "pass",
        None,
        f"chat-turn cost ${cost_usd:.6f} under ${COST_PASS_USD:.2f} bound",
    )


# ─────────────────────────── cost compute helper ─────────────────────────────


def compute_chat_cost_usd(
    input_tokens: int,
    output_tokens: int,
    pricing: dict[str, float],
) -> float:
    """Compute one chat turn's cost from token counts + a pricing dict.

    The pricing dict shape matches MODEL_PRICING entries:
      {"input": rate_per_million_usd, "output": rate_per_million_usd}

    Negative inputs are clamped to zero (defensive — a wrong-shape result
    from the agent should never produce a negative-cost finding).
    """
    in_tok = max(0, int(input_tokens))
    out_tok = max(0, int(output_tokens))
    in_rate = float(pricing.get("input", 0.0))
    out_rate = float(pricing.get("output", 0.0))
    return (in_tok * in_rate + out_tok * out_rate) / 1_000_000.0


__all__ = [
    "CHAT_LATENCY_BUDGET_SECONDS",
    "COST_FAIL_HIGH_USD",
    "COST_PASS_USD",
    "KB_GROUNDING_MARKERS",
    "MIN_REPLY_CHARS",
    "SEVERITY_HIGH",
    "SEVERITY_LOW",
    "SEVERITY_MEDIUM",
    "SPECIALIST_KEYWORDS",
    "classify_chat_cost",
    "classify_chat_roundtrip",
    "classify_conversation_persistence",
    "classify_kb_retrieval",
    "classify_specialist_routing",
    "classify_token_usage_recorded",
    "compute_chat_cost_usd",
]
