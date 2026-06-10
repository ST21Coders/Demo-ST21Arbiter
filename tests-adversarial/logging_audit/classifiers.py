"""logging_audit/classifiers.py — pure (status, severity, reason) verdicts for Block G.

Three classifiers, one per test module:

  * ``classify_security_event_logged`` → test_security_events_logged.py (#67)
  * ``classify_log_redaction``         → test_log_redaction.py            (#68)
  * ``classify_log_injection_downstream`` → test_log_injection_downstream.py (#71)

Severity bands match the dos / auth / headers / logic convention:

  HIGH    — direct security failure: a security event was NOT logged (audit
            silence on a forged-token attempt or cross-persona attempt), OR
            a known-sensitive value (JWT / canary secret) IS present in
            CloudWatch (log redaction failure → secret leak).
  MEDIUM  — log-injection downstream succeeded: the CR/LF in the payload
            caused CloudWatch to split a single log line into two events
            (log forgery vector). The API didn't reflect, but the log did.
  LOW     — informational only: ANSI escape sequences appear in the log
            line. Not a forgery vector by itself but worth flagging.

Status is the literal CellStatus value string ``"pass"`` or ``"fail"`` so
the results writer can drop the verdict straight into a TestResult row.
"""

from __future__ import annotations

from collections.abc import Sequence

# ──────────────────────────── severity bands ─────────────────────────────────

SEVERITY_HIGH = "high"
SEVERITY_MEDIUM = "medium"
SEVERITY_LOW = "low"


# ──────────────────────── security-event classifier (#67) ─────────────────────


def classify_security_event_logged(
    matching_audit_entries: int,
    *,
    scenario_id: str,
) -> tuple[str, str | None, str]:
    """Did the audit-log table receive an entry matching the probe scenario?

    Inputs:
      matching_audit_entries: count of audit-log rows whose timestamp falls
                              after the probe start and whose event-type /
                              actor matches the scenario.
      scenario_id: short id of the probe scenario, used only for the verdict
                   message ("forged-token" / "cross-persona" / etc).

    Verdicts:
      PASS:
        * `matching_audit_entries >= 1` — the security event was recorded.
      FAIL HIGH:
        * `matching_audit_entries == 0` — no audit entry for a security event.
          Silent failure: an attacker who tries the same probe is never
          flagged to operators.
    """
    if matching_audit_entries >= 1:
        return (
            "pass",
            None,
            f"{scenario_id}: audit-log entry found ({matching_audit_entries} match(es))",
        )
    return (
        "fail",
        SEVERITY_HIGH,
        f"{scenario_id}: no audit-log entry recorded — security event "
        f"would go undetected",
    )


# ──────────────────────── log-redaction classifier (#68) ─────────────────────


def classify_log_redaction(
    canary_match_count: int,
    *,
    canary_kind: str,
) -> tuple[str, str | None, str]:
    """Did a known-sensitive canary leak into CloudWatch?

    Inputs:
      canary_match_count: count of CloudWatch log events whose message
                          contains the canary string.
      canary_kind: human-readable kind of canary ("jwt" / "body-field" /
                   "email"), used only for the verdict message.

    Verdicts:
      PASS:
        * `canary_match_count == 0` — canary is not in CloudWatch. Either
          the Lambda redacted it or never logged it.
      FAIL HIGH:
        * `canary_match_count >= 1` — canary appears verbatim in
          CloudWatch. Direct secret leak; an operator (or anyone with
          `logs:GetLogEvents`) can read the sensitive value.
    """
    if canary_match_count == 0:
        return (
            "pass",
            None,
            f"{canary_kind}: canary not present in CloudWatch logs",
        )
    return (
        "fail",
        SEVERITY_HIGH,
        f"{canary_kind}: canary present in {canary_match_count} CloudWatch "
        f"log event(s) — sensitive value leaked verbatim",
    )


# ────────────── log-injection downstream classifier (#71) ───────────────────


# ANSI escape sequences we look for in the rendered log line. Each entry is
# tested as a substring against the message body. Keep narrow — we want the
# real ESC byte, not the word "ANSI".
_ANSI_MARKERS: tuple[str, ...] = (
    "\x1b[",  # CSI
    "\x1b]",  # OSC
)


def _contains_ansi(message: str) -> bool:
    """True if `message` contains an ANSI control sequence."""
    return any(marker in message for marker in _ANSI_MARKERS)


def classify_log_injection_downstream(
    matched_event_count: int,
    *,
    sample_messages: Sequence[str],
    payload_id: str,
) -> tuple[str, str | None, str]:
    """Did the log-injection payload break CloudWatch's log line boundaries?

    Inputs:
      matched_event_count: number of CloudWatch events (= log lines) that
                           contained the surrounding canary marker. A
                           healthy run records exactly one log line per
                           HTTP request.
      sample_messages: the message bodies of the matched events. Inspected
                       for ANSI escape sequences; we only need a small
                       sample (typically <= 5).
      payload_id: id of the corpus payload, used in the verdict message.

    Verdicts:
      PASS:
        * `matched_event_count == 1` and no ANSI escape in the sample —
          the payload was escaped or stripped, single log line preserved.
      FAIL MEDIUM:
        * `matched_event_count > 1` — the CR/LF split the log entry across
          multiple CloudWatch events. Attacker can forge a fake log entry.
      FAIL LOW:
        * `matched_event_count == 1` AND any ANSI escape present —
          payload landed on one line but ANSI is unsanitized. Less
          critical (no forgery) but ANSI-rendering tooling (`aws logs
          tail` on a coloured terminal) renders it.
      PASS:
        * `matched_event_count == 0` — the API didn't log the canary at
          all. Could be aggressive redaction or rate-limit refusal; either
          way the downstream log was not split. We treat this as PASS and
          let the security-event probes (#67) catch true logging gaps.
    """
    if matched_event_count == 0:
        return (
            "pass",
            None,
            f"{payload_id}: canary not present in CloudWatch (API did not log "
            f"the input — no downstream split)",
        )
    if matched_event_count > 1:
        return (
            "fail",
            SEVERITY_MEDIUM,
            f"{payload_id}: CRLF split log entry into {matched_event_count} "
            f"CloudWatch events — log forgery vector",
        )
    # Exactly one matching event. Check for ANSI residue.
    ansi_present = any(_contains_ansi(msg) for msg in sample_messages)
    if ansi_present:
        return (
            "fail",
            SEVERITY_LOW,
            f"{payload_id}: payload contained in a single log line but ANSI "
            f"escape sequence present — log-rendering tool exposure",
        )
    return (
        "pass",
        None,
        f"{payload_id}: payload escaped or stripped, single log line, no ANSI",
    )


__all__ = [
    "SEVERITY_HIGH",
    "SEVERITY_LOW",
    "SEVERITY_MEDIUM",
    "classify_log_injection_downstream",
    "classify_log_redaction",
    "classify_security_event_logged",
]
