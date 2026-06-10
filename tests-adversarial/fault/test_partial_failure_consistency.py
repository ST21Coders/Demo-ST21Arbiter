"""Partial-failure / inconsistent-state probes (#47).

ARBITER's action endpoints (`/actions/{id}/approve`, `/actions/{id}/reject`,
etc.) are a multi-step pipeline: approve → execute → audit-log + downstream
notification. If the pipeline is interrupted mid-flow, what's left in DDB?

A true mid-flow interrupt requires killing the Lambda from outside (AWS
Fault Injection Simulator). We can't do that from a black-box harness.
The pragmatic approach:

  1. ``client-abort``  — send a request, disconnect after a short delay,
                         then re-read the resource. PASS if state is
                         consistent (either fully transitioned or untouched).
  2. ``approve-vs-reject-race`` — fire approve and reject in parallel,
                                   then check the final state. PASS on a
                                   single decisive winner; FAIL HIGH on
                                   mixed state.
  3. ``concurrent-upload-then-scan`` — POST /uploads/presign then
                                        immediately POST /scan referencing
                                        the upload before it could be
                                        finalized. PASS on graceful
                                        success or clear refusal; FAIL
                                        MEDIUM on hang or malformed body.

Test IDs follow the harness convention: ``fault.partial-failure.<scenario>``.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
import requests

_LAYER_DIR = Path(__file__).resolve().parent
if str(_LAYER_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_LAYER_DIR.parent))

from fault.classifiers import (  # noqa: E402
    classify_concurrent_clientside,
    classify_partial_failure,
)
from fault.conftest import evidence_path_for  # noqa: E402

# Sleep duration after the abort or race fires before we re-read the
# action. 2 seconds is enough for DDB writes to propagate (eventual
# consistency on a Scan is sub-second on a quiet table; we add slack).
_SETTLE_SLEEP_SECONDS = 2.0


# ────────────────────────── action selection helper ──────────────────────────


def _fetch_actions(
    api_base_url: str, auth_header: dict, session: requests.Session
) -> list[dict]:
    """`GET /actions` and return the change_requests list. Empty list on
    any error — callers skip on empty.

    Mirrors the helper in `logic/test_action_state_machine.py` so the two
    layers select actions the same way.
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


def _get_action(
    api_base_url: str, auth_header: dict, session: requests.Session, *, cr_id: str
) -> dict:
    """GET /actions/{id} and return the action dict. Empty dict on error.

    The API exposes the action's terminal state via the response body's
    ``status`` field plus, in some responses, ``approved`` / ``rejected``
    boolean flags. We tolerate both shapes — the classifier only checks
    for mutually-exclusive truthiness.
    """
    try:
        resp = session.get(
            f"{api_base_url}/actions/{cr_id}",
            headers=auth_header,
        )
    except requests.RequestException:
        return {}
    if resp.status_code != 200:
        return {}
    try:
        body = resp.json()
    except (ValueError, json.JSONDecodeError):
        return {}
    return body if isinstance(body, dict) else {}


def _normalize_state(action: dict) -> dict:
    """Project the action's response into the {approved, rejected} shape
    the classifier consumes.

    The API may surface the terminal state either as boolean flags or as a
    ``status`` string. We normalize both into truthy ``approved`` /
    ``rejected`` keys so the classifier doesn't have to know about the
    underlying API shape.
    """
    status = (action.get("status") or "").upper()
    approved_status = status in {"APPROVED", "AUTO_APPROVED", "COMPLETED"}
    rejected_status = status in {"REJECTED"}
    return {
        "approved": bool(action.get("approved")) or approved_status,
        "rejected": bool(action.get("rejected")) or rejected_status,
        "raw_status": status,
    }


def _best_effort_reset(
    api_base_url: str,
    auth_header: dict,
    session: requests.Session,
    *,
    cr_id: str,
) -> None:
    """Try to put the action back into a terminal-rejected state for the
    next harness run. Cleanup is best-effort; we swallow errors.
    """
    try:
        session.post(
            f"{api_base_url}/actions/{cr_id}/reject",
            headers={**auth_header, "Content-Type": "application/json"},
            json={"reason": "harness-reset (fault layer cleanup)"},
        )
    except requests.RequestException:
        pass


# ─────────────────────────── client-abort probe ──────────────────────────────


def test_approve_abort_client(
    api_base_url: str,
    ciso_auth_header: dict,
    http_session: requests.Session,
    results_writer,
) -> None:
    """POST /approve with a deliberately short read timeout, then re-read.

    We can't actually kill the Lambda — but we CAN abort our own read of
    the response after the bytes are sent. From the API's perspective the
    work is already in flight. We then re-read the action's state to see
    if it's consistent.

    PASS if the final state has at most one of `approved` / `rejected`
    set. FAIL HIGH if both are set (mixed terminal state).
    """
    test_id = "fault.partial-failure.approve-abort-client"
    actions = _fetch_actions(api_base_url, ciso_auth_header, http_session)
    if not actions:
        results_writer.record(
            {
                "test_id": test_id,
                "status": "skipped",
                "layer": "fault",
                "target_kind": "api_route",
                "target_id": "post-action-approve",
                "skipped_reason": "no actions returned from GET /actions",
            }
        )
        pytest.skip("no actions available")

    candidate = next((a for a in actions if isinstance(a, dict)), None)
    if candidate is None:
        pytest.skip("no usable action dict")
    cr_id = candidate.get("cr_id") or candidate.get("id") or ""
    if not cr_id:
        pytest.skip("action missing cr_id")

    # Use a fresh Session so the short timeout doesn't bleed into other
    # tests. requests' default timeout on a Session can't be overridden
    # per-request to be SHORTER than what we'd want here, so we use a new
    # one with a 0.1 second total timeout.
    abort_session = requests.Session()
    try:
        abort_session.post(
            f"{api_base_url}/actions/{cr_id}/approve",
            headers={**ciso_auth_header, "Content-Type": "application/json"},
            json={
                "approver_email": "ciso@harness",
                "approver_role": "ciso",
                "comment": "fault layer abort probe",
            },
            timeout=(0.5, 0.1),  # connect=0.5s, read=0.1s
        )
    except requests.RequestException:
        # Expected — read timed out before the body landed. The server may
        # or may not have completed its writes.
        pass
    finally:
        abort_session.close()

    # Let any in-flight writes settle.
    time.sleep(_SETTLE_SLEEP_SECONDS)

    final_action = _get_action(
        api_base_url, ciso_auth_header, http_session, cr_id=cr_id
    )
    final_state = _normalize_state(final_action) if final_action else {}

    verdict, severity, reason = classify_partial_failure(
        final_state=final_state,
        scenario="approve-abort-client",
    )

    row: dict = {
        "test_id": test_id,
        "status": verdict,
        "layer": "fault",
        "target_kind": "api_route",
        "target_id": "post-action-approve",
    }
    if severity:
        row["severity"] = severity
    if verdict == "fail":
        row["evidence_path"] = evidence_path_for(test_id)
    results_writer.record(row)

    _best_effort_reset(api_base_url, ciso_auth_header, http_session, cr_id=cr_id)

    if verdict == "fail":
        pytest.fail(f"{test_id}: {reason} (cr_id={cr_id}, final_state={final_state})")


# ─────────────────────────── approve-vs-reject probe ─────────────────────────


def test_approve_vs_reject_race(
    api_base_url: str,
    ciso_auth_header: dict,
    http_session: requests.Session,
    results_writer,
) -> None:
    """Fire approve and reject in parallel; check the final state.

    Expected: one wins decisively. PASS if final state has ``approved``
    XOR ``rejected`` set. FAIL HIGH if both are set (state machine
    converged to a contradiction).
    """
    test_id = "fault.partial-failure.approve-vs-reject-race"
    actions = _fetch_actions(api_base_url, ciso_auth_header, http_session)
    if not actions:
        results_writer.record(
            {
                "test_id": test_id,
                "status": "skipped",
                "layer": "fault",
                "target_kind": "api_route",
                "target_id": "post-action-approve",
                "skipped_reason": "no actions returned from GET /actions",
            }
        )
        pytest.skip("no actions available")

    candidate = next((a for a in actions if isinstance(a, dict)), None)
    if candidate is None:
        pytest.skip("no usable action dict")
    cr_id = candidate.get("cr_id") or candidate.get("id") or ""
    if not cr_id:
        pytest.skip("action missing cr_id")

    # Use fresh Sessions per thread so the connection pool doesn't
    # serialize them.
    approve_session = requests.Session()
    reject_session = requests.Session()
    results: dict[str, int] = {}
    barrier = threading.Barrier(2)

    def _do_approve() -> None:
        barrier.wait()
        try:
            resp = approve_session.post(
                f"{api_base_url}/actions/{cr_id}/approve",
                headers={**ciso_auth_header, "Content-Type": "application/json"},
                json={
                    "approver_email": "ciso@harness",
                    "approver_role": "ciso",
                    "comment": "race-approve",
                },
                timeout=10,
            )
            results["approve"] = resp.status_code
        except requests.RequestException:
            results["approve"] = 0

    def _do_reject() -> None:
        barrier.wait()
        try:
            resp = reject_session.post(
                f"{api_base_url}/actions/{cr_id}/reject",
                headers={**ciso_auth_header, "Content-Type": "application/json"},
                json={"reason": "race-reject"},
                timeout=10,
            )
            results["reject"] = resp.status_code
        except requests.RequestException:
            results["reject"] = 0

    threads = [
        threading.Thread(target=_do_approve, name="approve"),
        threading.Thread(target=_do_reject, name="reject"),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    approve_session.close()
    reject_session.close()

    time.sleep(_SETTLE_SLEEP_SECONDS)
    final_action = _get_action(
        api_base_url, ciso_auth_header, http_session, cr_id=cr_id
    )
    final_state = _normalize_state(final_action) if final_action else {}

    verdict, severity, reason = classify_partial_failure(
        final_state=final_state,
        scenario="approve-vs-reject-race",
    )

    row: dict = {
        "test_id": test_id,
        "status": verdict,
        "layer": "fault",
        "target_kind": "api_route",
        "target_id": "post-action-approve",
    }
    if severity:
        row["severity"] = severity
    if verdict == "fail":
        row["evidence_path"] = evidence_path_for(test_id)
    results_writer.record(row)

    _best_effort_reset(api_base_url, ciso_auth_header, http_session, cr_id=cr_id)

    if verdict == "fail":
        pytest.fail(
            f"{test_id}: {reason} (cr_id={cr_id}, "
            f"approve={results.get('approve')}, reject={results.get('reject')})"
        )


# ────────────────── concurrent-upload-then-scan probe ────────────────────────


def test_concurrent_upload_then_scan(
    api_base_url: str,
    ciso_auth_header: dict,
    http_session: requests.Session,
    results_writer,
) -> None:
    """POST /uploads/presign then immediately POST /scan referencing the
    upload before it could be finalized.

    The realistic shape: an attacker calls /scan with a freshly-minted
    object key that points at an S3 object the API hasn't seen yet.
    The server should either accept (idempotent retry) or refuse cleanly.

    FAIL MEDIUM on hang or malformed body.
    """
    test_id = "fault.partial-failure.concurrent-upload-then-scan"
    presign_status = 0
    presign_body: Any = None
    try:
        presign_resp = http_session.post(
            f"{api_base_url}/uploads/presign",
            headers={**ciso_auth_header, "Content-Type": "application/json"},
            json={"filename": f"harness-fault-{uuid.uuid4().hex}.txt"},
        )
        presign_status = presign_resp.status_code
        try:
            presign_body = presign_resp.json()
        except (ValueError, json.JSONDecodeError):
            presign_body = None
    except requests.RequestException:
        pass

    if presign_status >= 400 or presign_status == 0:
        # The presign call itself failed — we can't probe the race. Skip
        # rather than synthesize a fake object key.
        results_writer.record(
            {
                "test_id": test_id,
                "status": "skipped",
                "layer": "fault",
                "target_kind": "api_route",
                "target_id": "post-scan",
                "skipped_reason": (
                    f"presign returned HTTP {presign_status}; cannot probe "
                    f"upload-then-scan race"
                ),
            }
        )
        pytest.skip("presign unavailable")

    # The presign response shape includes either an `object_key` or `key`
    # field plus the presigned URL. We use whatever's present; if neither
    # is, fall back to a synthetic key (the API should refuse, which is
    # the PASS path).
    object_key = ""
    if isinstance(presign_body, dict):
        object_key = (
            presign_body.get("object_key")
            or presign_body.get("key")
            or presign_body.get("objectKey")
            or ""
        )
    if not object_key:
        object_key = f"harness-fault-no-presign-{uuid.uuid4().hex}.txt"

    scan_status = 0
    scan_body_text = ""
    succeeded = False
    try:
        scan_resp = http_session.post(
            f"{api_base_url}/scan",
            headers={**ciso_auth_header, "Content-Type": "application/json"},
            json={"object_key": object_key},
        )
        scan_status = scan_resp.status_code
        scan_body_text = scan_resp.text or ""
        succeeded = True
    except requests.RequestException:
        succeeded = False

    verdict, severity, reason = classify_concurrent_clientside(
        succeeded=succeeded,
        response_status=scan_status,
        body_text=scan_body_text,
        scenario="concurrent-upload-then-scan",
    )

    row: dict = {
        "test_id": test_id,
        "status": verdict,
        "layer": "fault",
        "target_kind": "api_route",
        "target_id": "post-scan",
    }
    if severity:
        row["severity"] = severity
    if verdict == "fail":
        row["evidence_path"] = evidence_path_for(test_id)
    results_writer.record(row)

    if verdict == "fail":
        pytest.fail(f"{test_id}: {reason} (scan_status={scan_status})")
