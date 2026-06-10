"""Workflow-bypass probes against the action lifecycle state machine (#61).

The ARBITER action handler at `Infra/functions/api_handler/api_handler.py`
exposes four transitions on each change request:

  * POST /actions/{id}/approve
  * POST /actions/{id}/reject
  * POST /actions/{id}/execute
  * POST /actions/{id}/escalate

The handler's `_handle_action_transition` enforces (see lines 1584-1664):

  * `execute` only when prior_status ∈ {APPROVED, AUTO_APPROVED} — anything
    else returns 409.
  * `approve` requires the caller to match an approver in the chain — non
    approvers get 403.

We probe four invalid transitions. Each probe:

  1. Picks an existing action via `GET /actions` (the harness can't create
     actions from outside — `POST /actions` requires fields the handler
     populates from a finding; we treat that as a deliberate limitation
     and document it on skip).
  2. Drives the action into the state needed to test the bypass (if any).
  3. Fires the invalid transition.
  4. Records a verdict via `classify_state_transition`.
  5. Best-effort restores the action by rejecting it.

Test ids:
  * logic.workflow.skip-approve
  * logic.workflow.double-approve
  * logic.workflow.reject-after-execute
  * logic.workflow.escalate-from-terminal
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import requests

from logic.classifiers import classify_state_transition
from logic.conftest import evidence_path_for

# Terminal statuses per the handler — once an action lands in one of these,
# any further transition should be rejected. APPROVED is treated as
# "approvable terminal" (the next legitimate step is `execute`).
_TERMINAL_STATUSES: frozenset[str] = frozenset({"COMPLETED", "REJECTED", "ESCALATED"})

# Statuses that indicate the action is mid-flight (can be approved, rejected,
# escalated). We avoid picking actions stuck in PENDING_APPROVAL because their
# approver chain is unpredictable from outside.
_RESETTABLE_STATUSES: frozenset[str] = frozenset(
    {"PENDING_APPROVAL", "APPROVED", "AUTO_APPROVED", "ESCALATED"}
)


def _fetch_actions(
    api_base_url: str, auth_header: dict, session: requests.Session
) -> list[dict]:
    """`GET /actions` and return the change_requests list. Empty list on
    any error — callers skip on empty.
    """
    try:
        resp = session.get(
            f"{api_base_url}/actions",
            headers=auth_header,
            params={"limit": 5},
        )
    except requests.RequestException:
        return []
    if resp.status_code != 200:
        return []
    try:
        body = resp.json()
    except (ValueError, json.JSONDecodeError):
        return []
    return list(body.get("change_requests") or [])


def _pick_action(
    actions: list[dict], *, status_in: frozenset[str] | None = None
) -> dict | None:
    """First action whose `status` is in `status_in`. None on no match.

    `status_in=None` returns the first action regardless of status. We use
    that for skip-approve, which only needs *some* action to exist (the
    probe is "execute without approve" — the prior status doesn't matter
    if the handler is correct, it should reject anyway).
    """
    for action in actions:
        if not isinstance(action, dict):
            continue
        if status_in is None:
            return action
        status = (action.get("status") or "").upper()
        if status in status_in:
            return action
    return None


def _transition(
    *,
    api_base_url: str,
    cr_id: str,
    action_kind: str,
    auth_header: dict,
    session: requests.Session,
    body: dict[str, Any] | None = None,
) -> int:
    """Fire one transition. Returns HTTP status (0 on transport error).

    `body` carries `approver_email` / `approver_role` so the handler can
    match the caller to a chain row; defaults to a CISO-shaped body which
    triggers the override branch.
    """
    payload = body or {}
    try:
        resp = session.post(
            f"{api_base_url}/actions/{cr_id}/{action_kind}",
            headers={**auth_header, "Content-Type": "application/json"},
            json=payload,
        )
    except requests.RequestException:
        return 0
    return resp.status_code


def _best_effort_reset(
    *,
    api_base_url: str,
    cr_id: str,
    auth_header: dict,
    session: requests.Session,
) -> None:
    """Try to put the action back into a terminal-rejected state so the
    next harness run doesn't see leftover side effects.

    Sends `reject` with a `harness-reset` reason. Ignores all errors —
    cleanup is best-effort, the probe verdict is already recorded.
    """
    _transition(
        api_base_url=api_base_url,
        cr_id=cr_id,
        action_kind="reject",
        auth_header=auth_header,
        session=session,
        body={"reason": "harness-reset (logic layer cleanup)"},
    )


# ────────────────────────── skip-approve probe ───────────────────────────────


def test_skip_approve(
    api_base_url: str,
    ciso_auth_header: dict,
    http_session: requests.Session,
    results_writer,
) -> None:
    """`POST /actions/{id}/execute` without first approving.

    Expected: 409 (the handler at line 1638-1639 rejects with that
    status code when prior_status is not APPROVED / AUTO_APPROVED).
    FAIL HIGH if 200 — workflow bypass: actions execute without approval.
    """
    test_id = "logic.workflow.skip-approve"
    actions = _fetch_actions(api_base_url, ciso_auth_header, http_session)
    candidate = _pick_action(actions, status_in=_RESETTABLE_STATUSES)
    if candidate is None:
        results_writer.record(
            {
                "test_id": test_id,
                "status": "skipped",
                "layer": "logic",
                "target_kind": "api_route",
                "target_id": "post-action-execute",
                "skipped_reason": "no actions returned from GET /actions",
            }
        )
        pytest.skip("no actions available to probe")

    cr_id = candidate.get("cr_id") or candidate.get("id") or ""
    if not cr_id:
        results_writer.record(
            {
                "test_id": test_id,
                "status": "skipped",
                "layer": "logic",
                "target_kind": "api_route",
                "target_id": "post-action-execute",
                "skipped_reason": "selected action missing cr_id",
            }
        )
        pytest.skip("action missing cr_id")

    status = _transition(
        api_base_url=api_base_url,
        cr_id=cr_id,
        action_kind="execute",
        auth_header=ciso_auth_header,
        session=http_session,
        body={"actor_email": "harness@logic-layer"},
    )

    verdict, severity, reason = classify_state_transition(
        status, transition_kind="skip-approve"
    )

    row: dict = {
        "test_id": test_id,
        "status": verdict,
        "layer": "logic",
        "target_kind": "api_route",
        "target_id": "post-action-execute",
    }
    if severity:
        row["severity"] = severity
    if verdict == "fail":
        row["evidence_path"] = evidence_path_for(test_id)
    results_writer.record(row)

    if verdict == "fail":
        pytest.fail(f"{test_id}: {reason} (cr_id={cr_id}, http_status={status})")


# ────────────────────────── double-approve probe ─────────────────────────────


def test_double_approve(
    api_base_url: str,
    ciso_auth_header: dict,
    http_session: requests.Session,
    results_writer,
) -> None:
    """Approve an action, then approve the same action again.

    Expected: 409 / 400 (already approved). FAIL MEDIUM if 200 — idempotency
    is missing; could indicate the chain index marches forward on duplicate
    approvals or the audit-log gets duplicate entries.
    """
    test_id = "logic.workflow.double-approve"
    actions = _fetch_actions(api_base_url, ciso_auth_header, http_session)
    candidate = _pick_action(actions, status_in=_RESETTABLE_STATUSES)
    if candidate is None:
        results_writer.record(
            {
                "test_id": test_id,
                "status": "skipped",
                "layer": "logic",
                "target_kind": "api_route",
                "target_id": "post-action-approve",
                "skipped_reason": "no actions returned from GET /actions",
            }
        )
        pytest.skip("no actions available")

    cr_id = candidate.get("cr_id") or candidate.get("id") or ""
    if not cr_id:
        pytest.skip("action missing cr_id")

    # First approve — drives the action to APPROVED state. We don't fail
    # the test if this returns non-200; it just means the precondition for
    # the bypass probe isn't reachable from outside (handler's approver-
    # match logic rejected our CISO-override body). The probe then becomes
    # informational: a second approve on the same state should still error.
    _transition(
        api_base_url=api_base_url,
        cr_id=cr_id,
        action_kind="approve",
        auth_header=ciso_auth_header,
        session=http_session,
        body={
            "approver_email": "ciso@harness",
            "approver_role": "ciso",
            "comment": "harness first-approve",
        },
    )

    # Second approve — the bypass probe. Expected to be rejected.
    status = _transition(
        api_base_url=api_base_url,
        cr_id=cr_id,
        action_kind="approve",
        auth_header=ciso_auth_header,
        session=http_session,
        body={
            "approver_email": "ciso@harness",
            "approver_role": "ciso",
            "comment": "harness second-approve",
        },
    )

    verdict, severity, reason = classify_state_transition(
        status, transition_kind="double-approve"
    )

    row: dict = {
        "test_id": test_id,
        "status": verdict,
        "layer": "logic",
        "target_kind": "api_route",
        "target_id": "post-action-approve",
    }
    if severity:
        row["severity"] = severity
    if verdict == "fail":
        row["evidence_path"] = evidence_path_for(test_id)
    results_writer.record(row)

    # Best-effort reset regardless of verdict.
    _best_effort_reset(
        api_base_url=api_base_url,
        cr_id=cr_id,
        auth_header=ciso_auth_header,
        session=http_session,
    )

    if verdict == "fail":
        pytest.fail(f"{test_id}: {reason} (cr_id={cr_id}, http_status={status})")


# ─────────────────────── reject-after-execute probe ──────────────────────────


def test_reject_after_execute(
    api_base_url: str,
    ciso_auth_header: dict,
    http_session: requests.Session,
    results_writer,
) -> None:
    """Once an action has been executed, reject should be refused.

    We can't always force an execute (the precondition is APPROVED state
    + approver-chain match), so this probe accepts actions that are
    already in a terminal status (COMPLETED). If none are found we
    document-and-skip rather than synthesizing state we can't actually
    drive.
    """
    test_id = "logic.workflow.reject-after-execute"
    actions = _fetch_actions(api_base_url, ciso_auth_header, http_session)
    # Prefer a completed action (executed). Fall back to any action in a
    # terminal status — reject on REJECTED / ESCALATED is also an invalid
    # transition.
    candidate = _pick_action(actions, status_in=frozenset({"COMPLETED"})) or (
        _pick_action(actions, status_in=_TERMINAL_STATUSES)
    )
    if candidate is None:
        results_writer.record(
            {
                "test_id": test_id,
                "status": "skipped",
                "layer": "logic",
                "target_kind": "api_route",
                "target_id": "post-action-reject",
                "skipped_reason": "no terminal-state action available",
            }
        )
        pytest.skip("no terminal action available")

    cr_id = candidate.get("cr_id") or candidate.get("id") or ""
    if not cr_id:
        pytest.skip("action missing cr_id")

    status = _transition(
        api_base_url=api_base_url,
        cr_id=cr_id,
        action_kind="reject",
        auth_header=ciso_auth_header,
        session=http_session,
        body={"reason": "harness reject-after-terminal probe"},
    )

    verdict, severity, reason = classify_state_transition(
        status, transition_kind="reject-after-execute"
    )

    row: dict = {
        "test_id": test_id,
        "status": verdict,
        "layer": "logic",
        "target_kind": "api_route",
        "target_id": "post-action-reject",
    }
    if severity:
        row["severity"] = severity
    if verdict == "fail":
        row["evidence_path"] = evidence_path_for(test_id)
    results_writer.record(row)

    if verdict == "fail":
        pytest.fail(f"{test_id}: {reason} (cr_id={cr_id}, http_status={status})")


# ─────────────────────── escalate-from-terminal probe ────────────────────────


def test_escalate_from_terminal(
    api_base_url: str,
    ciso_auth_header: dict,
    http_session: requests.Session,
    results_writer,
) -> None:
    """Escalating an action already in a terminal status should be refused.

    Same selection strategy as reject-after-execute: pick an action in
    COMPLETED / REJECTED / ESCALATED, fire `escalate`, expect 4xx.
    """
    test_id = "logic.workflow.escalate-from-terminal"
    actions = _fetch_actions(api_base_url, ciso_auth_header, http_session)
    candidate = _pick_action(actions, status_in=_TERMINAL_STATUSES)
    if candidate is None:
        results_writer.record(
            {
                "test_id": test_id,
                "status": "skipped",
                "layer": "logic",
                "target_kind": "api_route",
                "target_id": "post-action-escalate",
                "skipped_reason": "no terminal-state action available",
            }
        )
        pytest.skip("no terminal action available")

    cr_id = candidate.get("cr_id") or candidate.get("id") or ""
    if not cr_id:
        pytest.skip("action missing cr_id")

    status = _transition(
        api_base_url=api_base_url,
        cr_id=cr_id,
        action_kind="escalate",
        auth_header=ciso_auth_header,
        session=http_session,
        body={"reason": "harness escalate-from-terminal probe"},
    )

    verdict, severity, reason = classify_state_transition(
        status, transition_kind="escalate-from-terminal"
    )

    row: dict = {
        "test_id": test_id,
        "status": verdict,
        "layer": "logic",
        "target_kind": "api_route",
        "target_id": "post-action-escalate",
    }
    if severity:
        row["severity"] = severity
    if verdict == "fail":
        row["evidence_path"] = evidence_path_for(test_id)
    results_writer.record(row)

    if verdict == "fail":
        pytest.fail(f"{test_id}: {reason} (cr_id={cr_id}, http_status={status})")
