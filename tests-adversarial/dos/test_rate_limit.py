"""Per-route 429 verification (Block E, closes checklist item #51).

For each of five representative API routes, send a sustained burst of
requests at `--dos-rps` for `--dos-duration-seconds` seconds, then run the
collected (status, latency) sequence through
`dos.classifiers.classify_rate_limit_burst`.

Route choice
------------
We deliberately pick 5 routes (not all 26) so wall-clock stays inside the
layer's 5-minute orchestrator cap even at the hard ceilings:

  1. ``get-findings``       — most-trafficked read in the SPA.
  2. ``get-conversations``  — paginated read, common burst target.
  3. ``get-dashboard``      — aggregated read with fanout to DDB Query.
  4. ``get-agent-status``   — read that hits the AgentCore control plane.
  5. ``post-chat``          — destructive (POST); skipped without
                              ``--include-destructive``. Most expensive in
                              the steady state.

Each route emits one TestResult row, id ``dos.rate-limit.<route-id>``.
"""

from __future__ import annotations

import time

import pytest
import requests

from dos.classifiers import classify_rate_limit_burst
from dos.conftest import evidence_path_for

# ───────────────────────────── probe targets ─────────────────────────────────

# (route_id, http_method, path_under_api_base, destructive_marker,
#  request_kwargs_factory). The kwargs factory receives `auth_header` and
# `api_base_url` and returns a dict ready to splat into `http_session.request`.
_PROBE_ROUTES: list[tuple[str, str, str, bool]] = [
    ("get-findings", "GET", "/findings", False),
    ("get-conversations", "GET", "/conversations", False),
    ("get-dashboard", "GET", "/dashboard", False),
    ("get-agent-status", "GET", "/agent-status", False),
    ("post-chat", "POST", "/chat", True),
]


def _post_chat_body() -> dict:
    """Smallest plausible /chat body. Keeps the burst cheap upstream — we
    only care about throttle behaviour, not the model's response.
    """
    return {"prompt": "ping", "session_id": "dos-rate-limit-burst"}


@pytest.mark.parametrize(
    ("route_id", "method", "path", "is_destructive"),
    _PROBE_ROUTES,
    ids=[t[0] for t in _PROBE_ROUTES],
)
def test_rate_limit_burst(
    route_id: str,
    method: str,
    path: str,
    is_destructive: bool,
    api_base_url: str,
    chat_function_url: str | None,
    http_session: requests.Session,
    ciso_auth_header: dict,
    dos_rps: int,
    dos_duration_seconds: int,
    include_destructive: bool,
    results_writer,
) -> None:
    """One burst per route. PASS if at least one 429 lands.

    For ``post-chat`` the burst goes through the Lambda Function URL (not
    API Gateway) since that's where the real route lives; if
    ``CHAT_FUNCTION_URL`` is unset we record a SKIP row so the route still
    shows up in the coverage matrix.
    """
    test_id = f"dos.rate-limit.{route_id}"

    if is_destructive and not include_destructive:
        results_writer.record(
            {
                "test_id": test_id,
                "status": "skipped",
                "layer": "dos",
                "target_kind": "api_route",
                "target_id": route_id,
                "skipped_reason": "destructive route; pass --include-destructive to enable",
            }
        )
        pytest.skip("destructive; --include-destructive not passed")

    # Resolve the actual URL. /chat is Function-URL-served in production.
    if route_id == "post-chat":
        if not chat_function_url:
            results_writer.record(
                {
                    "test_id": test_id,
                    "status": "skipped",
                    "layer": "dos",
                    "target_kind": "api_route",
                    "target_id": route_id,
                    "skipped_reason": "CHAT_FUNCTION_URL unset; /chat lives behind the Function URL",
                }
            )
            pytest.skip("CHAT_FUNCTION_URL unset")
        url = f"{chat_function_url}/chat"
    else:
        url = f"{api_base_url}{path}"

    # Per-request kwargs. POST routes need a JSON body; GETs need only auth.
    request_kwargs: dict = {"headers": dict(ciso_auth_header)}
    if method == "POST":
        request_kwargs["json"] = _post_chat_body()

    # The burst itself. We pace ourselves at the requested RPS by sleeping
    # the residual interval after each request, so an unexpectedly slow
    # response naturally cuts the rate (no extra parallelism). This matches
    # what a single attacker host would actually generate.
    target_interval_s = 1.0 / max(1, dos_rps)
    deadline = time.monotonic() + dos_duration_seconds

    status_codes: list[int] = []
    latencies_ms: list[float] = []

    while time.monotonic() < deadline:
        req_started = time.monotonic()
        try:
            resp = http_session.request(method, url, **request_kwargs)
            status_codes.append(resp.status_code)
        except requests.RequestException:
            # Transport failure (connection drop / timeout) — record a
            # zero-status so the classifier flags it HIGH.
            status_codes.append(0)
        elapsed_s = time.monotonic() - req_started
        latencies_ms.append(elapsed_s * 1000.0)

        residual = target_interval_s - elapsed_s
        if residual > 0:
            time.sleep(residual)

    status, severity, reason = classify_rate_limit_burst(status_codes, latencies_ms)

    row: dict = {
        "test_id": test_id,
        "status": status,
        "layer": "dos",
        "target_kind": "api_route",
        "target_id": route_id,
        "duration_seconds": sum(latencies_ms) / 1000.0,
    }
    if severity:
        row["severity"] = severity
    if status == "fail":
        row["evidence_path"] = evidence_path_for(test_id)
    results_writer.record(row)

    if status == "fail":
        pytest.fail(
            f"{test_id}: {reason} "
            f"(n={len(status_codes)}, rps={dos_rps}, duration={dos_duration_seconds}s)"
        )
