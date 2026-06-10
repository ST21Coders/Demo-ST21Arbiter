"""Error-propagation probes (#45 — swallowed errors).

Trigger known error conditions and verify both:

  (a) the API returns a structured error response (not silent success), AND
  (b) the error appears in CloudWatch (mirrors Block G's audit-log
      verification pattern).

Scenarios
---------

  1. ``missing-record``        — POST /actions/{nonexistent_id}/approve.
                                  Expected 404 with structured body.
  2. ``cross-pool-jwt``        — synthesize a token-shaped string with an
                                  ``aud`` claim from a different user pool.
                                  Expected 401.
  3. ``oversized-prompt``      — POST /chat with a 50 KB+ prompt that
                                  should exceed Bedrock's context window.
                                  Expected 4xx structured error OR a 200
                                  with a graceful refusal message.

For each scenario, a sibling test queries CloudWatch
(``logs:FilterLogEvents``) on the api_handler log group, looking for an
ERROR-level line near the request time. The CloudWatch check is recorded
as a separate row (test_id suffix ``.cloudwatch-logged``) so a missing log
contributes its own line in the report.

CloudWatch sub-checks are best-effort: when the AWS client isn't
available (no creds, no `logs:FilterLogEvents` permission), they emit a
SKIPPED row and the main HTTP probe still runs.

Test IDs
--------
  * ``fault.error-prop.<scenario>``
  * ``fault.error-prop.<scenario>.cloudwatch-logged``
"""

from __future__ import annotations

import base64
import json
import sys
import time
import uuid
from pathlib import Path

import pytest
import requests

_LAYER_DIR = Path(__file__).resolve().parent
if str(_LAYER_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_LAYER_DIR.parent))

from fault.classifiers import (  # noqa: E402
    classify_cloudwatch_logged,
    classify_error_propagation,
)
from fault.conftest import API_HANDLER_LOG_GROUP, evidence_path_for  # noqa: E402

# How long to wait after firing a probe before scanning CloudWatch. Lambda
# logs land within a few seconds; we give a little slack for clock skew.
_PROPAGATION_SLEEP_SECONDS = 4.0

# Window (in seconds, relative to request fire time) the CloudWatch scan
# looks at. 60 s is enough to cover propagation delay plus a few seconds
# of safety margin.
_CW_WINDOW_SECONDS = 60

# Maximum CloudWatch events to scan per probe. Keeps the probe bounded
# even if the log group is busy.
_CW_MAX_EVENTS = 500


# ─────────────────────────── helper utilities ────────────────────────────────


def _is_structured_json_error(resp: requests.Response) -> bool:
    """True if the response parses as JSON and has an `error` / `message` /
    `code` field — the shape ARBITER uses (see ``api_handler.py::_err``)."""
    try:
        body = resp.json()
    except (ValueError, json.JSONDecodeError):
        return False
    if not isinstance(body, dict):
        return False
    return any(k in body for k in ("error", "message", "code"))


def _scan_cloudwatch_for_error(
    *,
    aws_logs_client,
    start_epoch_ms: int,
    needles: tuple[str, ...],
) -> bool:
    """Scan the api_handler log group for an ERROR-level line containing any
    of the `needles` since `start_epoch_ms`.

    The CloudWatch FilterLogEvents API supports a server-side filter
    pattern. We use it to narrow to the time window only, then do the
    needle and ERROR-level match client-side (the filter pattern syntax
    doesn't support OR-over-arbitrary-strings cleanly).

    Returns True if at least one matching ERROR-level event was found.
    """
    if aws_logs_client is None:
        return False
    end_epoch_ms = start_epoch_ms + _CW_WINDOW_SECONDS * 1000
    try:
        response = aws_logs_client.filter_log_events(
            logGroupName=API_HANDLER_LOG_GROUP,
            startTime=start_epoch_ms,
            endTime=end_epoch_ms,
            limit=_CW_MAX_EVENTS,
        )
    except Exception:  # noqa: BLE001
        # boto3 raises ClientError on missing perms / throttle. We've
        # already module-failed gracefully via the aws_logs_client fixture
        # when perms are missing, so any error here is transient — treat
        # as "no log found" so the test can still record a verdict.
        return False
    needles_lower = tuple(n.lower() for n in needles if n)
    for event in response.get("events", []) or []:
        message = (event.get("message") or "").lower()
        # ARBITER logs ERROR via the stdlib `logging` module which prepends
        # the level token; some lines also use the AWS Lambda logger which
        # uses [ERROR]. We match both.
        if "error" not in message and "[error]" not in message:
            continue
        if not needles_lower:
            return True
        if any(needle in message for needle in needles_lower):
            return True
    return False


def _record_probe(
    *,
    test_id: str,
    scenario: str,
    response_status: int,
    response_body_text: str,
    is_structured_json: bool,
    results_writer,
    target_id: str,
) -> str:
    """Classify + record the main probe row. Returns the verdict string."""
    verdict, severity, reason = classify_error_propagation(
        response_status,
        response_body_text=response_body_text,
        scenario=scenario,
        is_structured_json=is_structured_json,
    )
    row: dict = {
        "test_id": test_id,
        "status": verdict,
        "layer": "fault",
        "target_kind": "api_route",
        "target_id": target_id,
    }
    if severity:
        row["severity"] = severity
    if verdict == "fail":
        row["evidence_path"] = evidence_path_for(test_id)
    results_writer.record(row)
    return verdict, reason


def _record_cloudwatch_sub(
    *,
    parent_test_id: str,
    scenario: str,
    target_id: str,
    api_returned_error: bool,
    aws_logs_client,
    start_epoch_ms: int,
    needles: tuple[str, ...],
    results_writer,
) -> None:
    """CloudWatch sub-check + row. Records SKIPPED if the AWS client is
    unavailable; otherwise records a PASS / FAIL row.
    """
    sub_test_id = f"{parent_test_id}.cloudwatch-logged"
    if aws_logs_client is None:
        results_writer.record(
            {
                "test_id": sub_test_id,
                "status": "skipped",
                "layer": "fault",
                "target_kind": "api_route",
                "target_id": target_id,
                "skipped_reason": "AWS logs client unavailable (creds or perms missing)",
            }
        )
        return

    # Give CloudWatch a moment to receive the log.
    time.sleep(_PROPAGATION_SLEEP_SECONDS)
    found = _scan_cloudwatch_for_error(
        aws_logs_client=aws_logs_client,
        start_epoch_ms=start_epoch_ms,
        needles=needles,
    )
    verdict, severity, reason = classify_cloudwatch_logged(
        api_returned_error=api_returned_error,
        found_error_log=found,
        scenario=scenario,
    )
    row: dict = {
        "test_id": sub_test_id,
        "status": verdict,
        "layer": "fault",
        "target_kind": "api_route",
        "target_id": target_id,
    }
    if severity:
        row["severity"] = severity
    if verdict == "fail":
        row["evidence_path"] = evidence_path_for(sub_test_id)
    results_writer.record(row)
    # CloudWatch missing isn't a hard pytest failure — we surface it as a
    # FAIL row but the harness exit code is driven by the report builder,
    # not by raising here. This matches the logging_audit layer.


# ─────────────────────────── missing-record probe ────────────────────────────


def test_force_ddb_missing_record(
    api_base_url: str,
    ciso_auth_header: dict,
    http_session: requests.Session,
    aws_logs_client,
    results_writer,
) -> None:
    """POST /actions/{nonexistent_id}/approve.

    Expected: 404 with a structured error body. The handler's
    ConditionalCheckFailedException path should surface as 404.
    FAIL MEDIUM if 2xx — silent success on a missing record.
    FAIL LOW if 5xx — raw exception leaked.
    """
    parent_test_id = "fault.error-prop.missing-record"
    scenario = "missing-record"
    nonexistent_id = f"harness-no-such-cr-{uuid.uuid4().hex}"
    start_epoch_ms = int(time.time() * 1000)
    try:
        resp = http_session.post(
            f"{api_base_url}/actions/{nonexistent_id}/approve",
            headers={**ciso_auth_header, "Content-Type": "application/json"},
            json={"reason": "harness missing-record probe"},
        )
        response_status = resp.status_code
        response_text = resp.text or ""
        is_structured = _is_structured_json_error(resp)
    except requests.RequestException:
        response_status = 0
        response_text = ""
        is_structured = False

    verdict, _ = _record_probe(
        test_id=parent_test_id,
        scenario=scenario,
        response_status=response_status,
        response_body_text=response_text,
        is_structured_json=is_structured,
        results_writer=results_writer,
        target_id="post-action-approve",
    )

    api_returned_error = response_status >= 400 or response_status == 0
    _record_cloudwatch_sub(
        parent_test_id=parent_test_id,
        scenario=scenario,
        target_id="post-action-approve",
        api_returned_error=api_returned_error,
        aws_logs_client=aws_logs_client,
        start_epoch_ms=start_epoch_ms,
        needles=(nonexistent_id,),
        results_writer=results_writer,
    )

    if verdict == "fail":
        # Don't pytest.fail here — the row is recorded; the orchestrator's
        # report builder picks up the FAIL. We do raise on the MEDIUM
        # silent-success path so an operator running pytest standalone
        # gets a visible failure.
        if response_status >= 200 and response_status < 300:
            pytest.fail(
                f"{parent_test_id}: silent 2xx (HTTP {response_status}) on "
                f"a known-missing CR id"
            )


# ────────────────────────── cross-pool JWT probe ─────────────────────────────


def _craft_cross_pool_token() -> str:
    """Synthesize a JWT-shaped string with an ``aud`` claim from a different
    user pool. The signature is bogus — the API should reject before the
    signature check matters, but our goal here is the ``aud`` mismatch.

    Returns a `header.payload.signature` triple. The header announces RS256
    so the handler attempts the signature verify and (because of the bogus
    sig) rejects. If the handler instead trusts the `aud` claim without
    verifying the signature, that's the cross-pool acceptance we're hunting.
    """
    header = {"alg": "RS256", "typ": "JWT", "kid": "harness-fake-key"}
    payload = {
        "sub": "harness-cross-pool-attacker",
        "aud": "cross-pool-client-id-aaaaaaaa",
        "iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_OTHERPOOL",
        "token_use": "id",
        "cognito:groups": ["ciso"],
        "exp": int(time.time()) + 3600,
        "iat": int(time.time()),
    }
    h_b64 = base64.urlsafe_b64encode(json.dumps(header).encode()).rstrip(b"=").decode()
    p_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    sig_b64 = (
        base64.urlsafe_b64encode(b"harness-fake-signature-bytes").rstrip(b"=").decode()
    )
    return f"{h_b64}.{p_b64}.{sig_b64}"


def test_force_cognito_cross_pool(
    api_base_url: str,
    http_session: requests.Session,
    aws_logs_client,
    results_writer,
) -> None:
    """A JWT with an `aud` claim from a different user pool.

    Expected: 401. FAIL MEDIUM if 2xx — cross-pool claim accepted.
    """
    parent_test_id = "fault.error-prop.cross-pool-jwt"
    scenario = "cross-pool-jwt"
    fake_token = _craft_cross_pool_token()
    start_epoch_ms = int(time.time() * 1000)
    try:
        resp = http_session.get(
            f"{api_base_url}/token-usage",
            headers={"Authorization": f"Bearer {fake_token}"},
        )
        response_status = resp.status_code
        response_text = resp.text or ""
        is_structured = _is_structured_json_error(resp)
    except requests.RequestException:
        response_status = 0
        response_text = ""
        is_structured = False

    verdict, _ = _record_probe(
        test_id=parent_test_id,
        scenario=scenario,
        response_status=response_status,
        response_body_text=response_text,
        is_structured_json=is_structured,
        results_writer=results_writer,
        target_id="get-token-usage",
    )

    api_returned_error = response_status >= 400 or response_status == 0
    _record_cloudwatch_sub(
        parent_test_id=parent_test_id,
        scenario=scenario,
        target_id="get-token-usage",
        api_returned_error=api_returned_error,
        aws_logs_client=aws_logs_client,
        start_epoch_ms=start_epoch_ms,
        needles=("OTHERPOOL", "cross-pool"),
        results_writer=results_writer,
    )

    if verdict == "fail":
        if 200 <= response_status < 300:
            pytest.fail(
                f"{parent_test_id}: silent 2xx (HTTP {response_status}) on "
                f"a cross-pool aud claim"
            )


# ────────────────────────── oversized-prompt probe ───────────────────────────


def test_force_agentcore_oversized_prompt(
    chat_function_url: str | None,
    ciso_auth_header: dict,
    http_session: requests.Session,
    aws_logs_client,
    results_writer,
) -> None:
    """POST /chat with a 50 KB+ prompt.

    Bedrock's Nova Lite model has a context window in the 100K-token range
    but the API may impose its own request-body cap (API Gateway tops out
    around 10 MB; Function URLs are higher). Either way, a 50 KB prompt is
    expected to either:

      * Succeed with a graceful "I couldn't process that" reply (200), OR
      * Be rejected with a structured 4xx error.

    FAIL MEDIUM on 500 / empty response — silent failure.

    Skipped if `CHAT_FUNCTION_URL` is unset (the /chat surface lives on a
    Function URL, not API Gateway).
    """
    parent_test_id = "fault.error-prop.oversized-prompt"
    scenario = "oversized-prompt"
    if not chat_function_url:
        results_writer.record(
            {
                "test_id": parent_test_id,
                "status": "skipped",
                "layer": "fault",
                "target_kind": "api_route",
                "target_id": "post-chat",
                "skipped_reason": "CHAT_FUNCTION_URL unset; /chat surface unreachable",
            }
        )
        pytest.skip("CHAT_FUNCTION_URL unset")

    # Build a 50 KB prompt. Repeating "abc " is enough to push past the
    # API's small-body fast-path. We use a deterministic shape (not random
    # bytes) so a future reproducer can re-fire the exact body.
    oversized_prompt = "abc " * (50_000 // 4)
    canary = uuid.uuid4().hex[:12]
    payload = {
        "session_id": f"harness-oversized-{canary}",
        "message": f"{oversized_prompt} canary={canary}",
    }
    start_epoch_ms = int(time.time() * 1000)
    try:
        resp = http_session.post(
            chat_function_url,
            headers={**ciso_auth_header, "Content-Type": "application/json"},
            json=payload,
        )
        response_status = resp.status_code
        response_text = resp.text or ""
        is_structured = _is_structured_json_error(resp)
    except requests.RequestException:
        response_status = 0
        response_text = ""
        is_structured = False

    # The "graceful 200" path should ALSO PASS — the model gave us a
    # refusal message, that's fine. The classifier doesn't know about
    # that nuance, so override the verdict here: 2xx with a non-empty
    # body is treated as PASS.
    if 200 <= response_status < 300 and response_text.strip():
        verdict = "pass"
        row: dict = {
            "test_id": parent_test_id,
            "status": verdict,
            "layer": "fault",
            "target_kind": "api_route",
            "target_id": "post-chat",
        }
        results_writer.record(row)
    else:
        verdict, _ = _record_probe(
            test_id=parent_test_id,
            scenario=scenario,
            response_status=response_status,
            response_body_text=response_text,
            is_structured_json=is_structured,
            results_writer=results_writer,
            target_id="post-chat",
        )

    api_returned_error = response_status >= 400 or response_status == 0
    _record_cloudwatch_sub(
        parent_test_id=parent_test_id,
        scenario=scenario,
        target_id="post-chat",
        api_returned_error=api_returned_error,
        aws_logs_client=aws_logs_client,
        start_epoch_ms=start_epoch_ms,
        needles=(canary,),
        results_writer=results_writer,
    )

    if verdict == "fail" and (
        response_status == 0 or 500 <= response_status < 600 or response_status == 200
    ):
        pytest.fail(
            f"{parent_test_id}: silent failure (HTTP {response_status}, body_len={len(response_text)})"
        )
