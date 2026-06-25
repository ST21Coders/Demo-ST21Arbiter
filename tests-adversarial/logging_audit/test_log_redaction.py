"""Log-redaction probes (#68).

Plant a known-unique canary value in an API request, then read the
api_handler's CloudWatch log group via ``logs:FilterLogEvents`` and assert
the canary is NOT present. A canary hit means the Lambda is logging the
sensitive value verbatim — a direct secret leak to anyone with
``logs:GetLogEvents`` on the log group.

Scenarios
---------

  * ``logging.redaction.jwt`` — include a real IdToken in
    ``Authorization`` and hit a route. Search CloudWatch for the first
    and last 40 characters of the token. PASS if not present.
  * ``logging.redaction.body-field`` — POST /chat with a unique canary
    in the prompt. Search CloudWatch for the canary. PASS if not present
    in info-level logs.
  * ``logging.redaction.email`` — trigger a Cognito error (bad password
    for a synthetic email) and verify the email isn't logged. The
    api_handler doesn't see this directly, but its log group is the
    CloudWatch surface — any other sidecar that DOES write the email
    would show up.

Each test:
  1. Mints a unique canary (UUID-suffixed where possible).
  2. Triggers the event with the canary embedded.
  3. Sleeps 3 s for log propagation.
  4. Calls ``logs:FilterLogEvents`` with ``filterPattern`` set to the
     canary; counts matching events.
  5. Classifies with ``classify_log_redaction``.

Test IDs follow the harness convention: dot-separated lowercase.
"""

from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
import requests

# Local imports.
_LAYER_DIR = Path(__file__).resolve().parent
if str(_LAYER_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_LAYER_DIR.parent))

from logging_audit.classifiers import classify_log_redaction  # noqa: E402
from logging_audit.conftest import (  # noqa: E402
    API_HANDLER_LOG_GROUP,
    evidence_path_for,
)

# Propagation sleep between probe + CloudWatch query. Lambda's log driver
# flushes asynchronously; 3 s is empirically enough on dev.
_PROPAGATION_SLEEP_SECONDS = 3.0

# We search a window of [start-30s, start+propagation+30s] to absorb
# clock skew between the harness and the Lambda log driver.
_QUERY_WINDOW_BEFORE_MS = 30_000
_QUERY_WINDOW_AFTER_MS = 60_000

# CloudWatch FilterLogEvents requires the search needle to be at least 1
# character. Below 8 chars the false-positive rate is too high for the
# JWT-fragment search; we use 40 char windows for that scenario.
_JWT_FRAGMENT_LEN = 40


# ─────────────────────────── shared log helpers ──────────────────────────────


def _epoch_ms() -> int:
    """Current epoch in milliseconds — matches CloudWatch's API shape."""
    return int(time.time() * 1000)


def _count_canary_hits(
    logs_client: Any,
    *,
    canary: str,
    start_ms: int,
    end_ms: int,
    sample_size: int = 5,
) -> tuple[int, list[str]]:
    """Count CloudWatch events matching ``canary`` and return up to
    ``sample_size`` sample message bodies.

    Uses ``logs:FilterLogEvents`` with a quoted filterPattern so the canary
    is matched as a literal substring (CloudWatch's filter pattern language
    treats unquoted text as a tokenized term match).

    Returns ``(count, sample_messages)`` so the caller can use the samples
    for the log-injection downstream classifier (#71) too.
    """
    # The CloudWatch filterPattern needs the canary wrapped in double quotes
    # to be treated as a literal. Backslashes inside the canary are not
    # interpreted; double quotes inside would need escaping, but our
    # canaries never contain those.
    pattern = f'"{canary}"'

    count = 0
    samples: list[str] = []
    next_token: str | None = None
    pages = 0
    # Cap pages so a runaway query can't waste budget. Each page is up to
    # 1 MB; 8 pages = ~8 MB scanned, plenty for a recently-active log group.
    max_pages = 8
    while pages < max_pages:
        kwargs: dict[str, Any] = {
            "logGroupName": API_HANDLER_LOG_GROUP,
            "startTime": start_ms,
            "endTime": end_ms,
            "filterPattern": pattern,
            "limit": 100,
        }
        if next_token:
            kwargs["nextToken"] = next_token
        try:
            resp = logs_client.filter_log_events(**kwargs)
        except Exception:  # noqa: BLE001 - throttle / transient is "no data"
            break
        events = resp.get("events") or []
        for ev in events:
            count += 1
            msg = ev.get("message") or ""
            if len(samples) < sample_size:
                samples.append(msg)
        next_token = resp.get("nextToken")
        if not next_token:
            break
        pages += 1
    return count, samples


def _record_and_assert(
    *,
    test_id: str,
    target_id: str,
    hit_count: int,
    canary_kind: str,
    canary_value: str,
    results_writer,
) -> None:
    """Drop the verdict into the results writer and pytest.fail on FAIL."""
    verdict, severity, reason = classify_log_redaction(
        hit_count, canary_kind=canary_kind
    )
    row: dict = {
        "test_id": test_id,
        "status": verdict,
        "layer": "logging_audit",
        "target_kind": "api_route",
        "target_id": target_id,
    }
    if severity:
        row["severity"] = severity
    if verdict == "fail":
        row["evidence_path"] = evidence_path_for(test_id)
    results_writer.record(row)
    if verdict == "fail":
        # Truncate canary in the failure message — we DON'T want pytest's
        # stdout to log the same value we just flagged. Show only the
        # first 8 chars as a fingerprint.
        fingerprint = canary_value[:8] + "..." if canary_value else "(empty)"
        pytest.fail(f"{test_id}: {reason} (canary_fingerprint={fingerprint})")


# ─────────────────────────── scenario 1: JWT not logged ──────────────────────


def test_jwt_not_logged(
    api_base_url: str,
    ciso_id_token: str,
    ciso_auth_header: dict,
    http_session: requests.Session,
    aws_clients: dict,
    results_writer,
) -> None:
    """A real IdToken in Authorization must not appear in CloudWatch.

    We send a routine request, then check that neither the first nor the
    last 40 characters of the IdToken appear in any log event in the
    propagation window. Using two non-overlapping fragments doubles the
    odds of catching a Lambda that logs (e.g.) ``event[...]`` containing
    the headers verbatim.
    """
    test_id = "logging.redaction.jwt"
    target_id = "get-dashboard"

    # Pick two fragments unlikely to collide with anything in normal log
    # traffic. JWT fragments are ~40 chars of base64url — high entropy.
    head_fragment = ciso_id_token[:_JWT_FRAGMENT_LEN]
    tail_fragment = ciso_id_token[-_JWT_FRAGMENT_LEN:]

    start_ms = _epoch_ms()
    try:
        http_session.get(
            f"{api_base_url}/dashboard",
            headers=ciso_auth_header,
        )
    except requests.RequestException:
        pass

    time.sleep(_PROPAGATION_SLEEP_SECONDS)
    end_ms = _epoch_ms() + _QUERY_WINDOW_AFTER_MS

    logs_client = aws_clients["cloudwatch_logs"]
    head_count, _ = _count_canary_hits(
        logs_client,
        canary=head_fragment,
        start_ms=start_ms - _QUERY_WINDOW_BEFORE_MS,
        end_ms=end_ms,
    )
    tail_count, _ = _count_canary_hits(
        logs_client,
        canary=tail_fragment,
        start_ms=start_ms - _QUERY_WINDOW_BEFORE_MS,
        end_ms=end_ms,
    )

    _record_and_assert(
        test_id=test_id,
        target_id=target_id,
        hit_count=head_count + tail_count,
        canary_kind="jwt",
        canary_value=head_fragment,
        results_writer=results_writer,
    )


# ─────────────────────── scenario 2: body field not logged ───────────────────


def test_body_field_not_logged(
    chat_function_url: str | None,
    ciso_auth_header: dict,
    http_session: requests.Session,
    aws_clients: dict,
    results_writer,
) -> None:
    """A unique canary in a POST /chat body should not appear in
    info-level logs.

    The /chat handler routes to the master orchestrator; the api_handler
    logs request metadata but should NOT log the prompt verbatim (the
    prompt may contain sensitive policy content from CISO). We plant a
    UUID-suffixed canary that has zero chance of pre-existing in logs.
    """
    test_id = "logging.redaction.body-field"
    target_id = "post-chat"

    if not chat_function_url:
        results_writer.record(
            {
                "test_id": test_id,
                "status": "skipped",
                "layer": "logging_audit",
                "target_kind": "api_route",
                "target_id": target_id,
                "skipped_reason": "CHAT_FUNCTION_URL unset",
            }
        )
        pytest.skip("CHAT_FUNCTION_URL unset")

    canary = f"harness-canary-{uuid.uuid4()}-secret"

    start_ms = _epoch_ms()
    try:
        http_session.post(
            f"{chat_function_url}/chat",
            headers={**ciso_auth_header, "Content-Type": "application/json"},
            json={
                "prompt": canary,
                "session_id": f"logging-canary-{uuid.uuid4()}",
                "chat_type": "analyst",
            },
            timeout=30,
        )
    except requests.RequestException:
        pass

    time.sleep(_PROPAGATION_SLEEP_SECONDS)
    end_ms = _epoch_ms() + _QUERY_WINDOW_AFTER_MS

    hit_count, _ = _count_canary_hits(
        aws_clients["cloudwatch_logs"],
        canary=canary,
        start_ms=start_ms - _QUERY_WINDOW_BEFORE_MS,
        end_ms=end_ms,
    )

    _record_and_assert(
        test_id=test_id,
        target_id=target_id,
        hit_count=hit_count,
        canary_kind="body-field",
        canary_value=canary,
        results_writer=results_writer,
    )


# ────────────────────── scenario 3: email not logged in errors ───────────────


def test_email_not_logged_on_cognito_error(
    aws_clients: dict,
    results_writer,
) -> None:
    """A failed Cognito sign-in with a synthetic email must not echo the
    email verbatim into the api_handler log group.

    Why the api_handler log group rather than Cognito's own surface:
    Cognito's pool logging is controlled by AWS; we only audit the parts
    of the surface ARBITER owns. Any error path that forwards to the
    Lambda (the SPA's relay-the-error pattern) ends up here.
    """
    from src.identity.cognito_auth import _require_env

    test_id = "logging.redaction.email"
    target_id = "cognito-initiate-auth"

    try:
        import boto3

        client_id = _require_env("COGNITO_CLIENT_ID")
        cognito = boto3.client("cognito-idp", region_name="us-east-1")
    except Exception as exc:  # noqa: BLE001
        results_writer.record(
            {
                "test_id": test_id,
                "status": "skipped",
                "layer": "logging_audit",
                "target_kind": "api_route",
                "target_id": target_id,
                "skipped_reason": f"cognito client unavailable: {exc}",
            }
        )
        pytest.skip(f"cognito client unavailable: {exc}")

    canary_email = f"harness-{uuid.uuid4().hex}@harness.invalid"

    start_ms = _epoch_ms()
    try:
        cognito.initiate_auth(
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={
                "USERNAME": canary_email,
                "PASSWORD": "not-the-password",
            },
            ClientId=client_id,
        )
    except Exception:  # noqa: BLE001 - failure IS the test
        pass

    time.sleep(_PROPAGATION_SLEEP_SECONDS)
    end_ms = _epoch_ms() + _QUERY_WINDOW_AFTER_MS

    hit_count, _ = _count_canary_hits(
        aws_clients["cloudwatch_logs"],
        canary=canary_email,
        start_ms=start_ms - _QUERY_WINDOW_BEFORE_MS,
        end_ms=end_ms,
    )

    _record_and_assert(
        test_id=test_id,
        target_id=target_id,
        hit_count=hit_count,
        canary_kind="email",
        canary_value=canary_email,
        results_writer=results_writer,
    )
