"""Open-redirect probes against the Cognito Hosted UI (Block A — checklist #57).

ARBITER's own API doesn't expose any redirect routes (verified by reading
``Infra/functions/api_handler/api_handler.py`` — there is no ``/auth/callback``
or ``?redirect=`` endpoint on the API). The exposure surface for the
"open redirect" item on the compliance checklist is the **Cognito Hosted UI**
``/login?redirect_uri=`` flow: Cognito enforces an allowlist on the app
client's ``CallbackURLs``, and the SPA's only valid callback is
``http://localhost:5173/callback`` (dev) plus the deployed CloudFront URL.

This module probes the Hosted UI directly with each open-redirect corpus
entry as the ``redirect_uri`` value and inspects the response:

  - PASS: Cognito returns 400 / 4xx (rejection) OR a 3xx whose ``Location``
    header is the Cognito-hosted error page (i.e. on the cognito-idp domain).
  - FAIL HIGH: Cognito returns a 3xx whose ``Location`` header points
    off-domain (any host that is neither ``*.amazoncognito.com`` nor the
    allowlisted SPA host). That would mean the allowlist isn't enforced.

If ``COGNITO_HOSTED_UI_URL`` (or the fallback env vars) are unset, the test
records a ``skipped`` row and bails. The Hosted UI URL is set as part of the
03-identity stack and shipped to ``.env.development`` by ``deploy.sh``; the
harness operator needs to copy it into ``tests-adversarial/.env``.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse

import pytest
import requests

_HARNESS_ROOT = Path(__file__).resolve().parent.parent
_CORPUS_PATH = _HARNESS_ROOT / "fuzz" / "corpus" / "open_redirects.json"


def _load_open_redirect_payloads() -> list[dict]:
    """Read the open_redirects corpus at collection time."""
    raw = json.loads(_CORPUS_PATH.read_text(encoding="utf-8"))
    return list(raw.get("payloads") or [])


_PAYLOADS = _load_open_redirect_payloads()
_TEST_IDS = [f"fuzz.open-redirects.{p['id']}" for p in _PAYLOADS]


def _allowed_redirect_host(host: str) -> bool:
    """Return True if ``host`` is one of the Cognito- or app-allowlisted hosts.

    Two trusted host families:
      * Cognito's own Hosted UI domain (``*.auth.us-east-1.amazoncognito.com``
        or any ``*.amazoncognito.com`` subdomain). A 302 back to a Cognito
        error page is the expected rejection shape.
      * The SPA's deployed origin — we read from ``TARGET_BASE_URL`` and
        treat its hostname as allowlisted. A 302 to the SPA's
        ``/callback`` (the configured app-client callback URL) is also a
        normal rejection shape on a valid redirect_uri.

    Any host outside these two families is treated as ``off-domain`` and
    fails the probe.
    """
    if not host:
        return True  # relative redirect — never off-domain
    host = host.lower()
    if host.endswith(".amazoncognito.com") or host == "amazoncognito.com":
        return True
    target = os.environ.get("TARGET_BASE_URL", "").strip()
    if target:
        target_host = urlparse(target).hostname
        if target_host and host == target_host.lower():
            return True
    return False


def classify_open_redirect_response(
    status_code: int, location: str | None
) -> tuple[str, str | None]:
    """Map a response to (status, severity).

    Rules:
      * 4xx                           → ``pass`` (Cognito rejected).
      * 3xx with Location on an allowlisted host (Cognito error page or the
        SPA's deployed origin) or relative → ``pass``.
      * 3xx with Location pointing off-domain → ``fail`` severity ``high``.
      * 2xx (no redirect)             → ``pass`` (Hosted UI rendered its
        own login form, didn't follow the attacker's redirect_uri).
      * 5xx                           → ``fail`` severity ``medium``
        (Hosted UI crashed on a malformed redirect_uri).
    """
    if 500 <= status_code < 600:
        return "fail", "medium"
    if 300 <= status_code < 400:
        if location is None:
            return "pass", None
        parsed = urlparse(location)
        if _allowed_redirect_host(parsed.hostname or ""):
            return "pass", None
        return "fail", "high"
    # 2xx / 4xx — Cognito either rendered the login page or rejected. PASS.
    return "pass", None


@pytest.fixture(scope="module")
def hosted_ui_url() -> str | None:
    """Cognito Hosted UI base URL. May be None — skip if unset.

    Read order:
      * ``COGNITO_HOSTED_UI_URL`` (preferred — set by the operator).
      * Fallback: build from ``COGNITO_DOMAIN_PREFIX`` if set.

    Returns ``None`` when no Hosted UI URL is configured; tests then record
    a ``skipped`` row.
    """
    explicit = os.environ.get("COGNITO_HOSTED_UI_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    domain_prefix = os.environ.get("COGNITO_DOMAIN_PREFIX", "").strip()
    if domain_prefix:
        return f"https://{domain_prefix}.auth.us-east-1.amazoncognito.com"
    return None


@pytest.mark.parametrize(
    ("payload", "test_id"),
    list(zip(_PAYLOADS, _TEST_IDS)),
    ids=_TEST_IDS,
)
def test_open_redirect_rejected_by_hosted_ui(
    payload: dict,
    test_id: str,
    hosted_ui_url: str | None,
    http_session: requests.Session,
    results_writer,
) -> None:
    """Probe the Cognito ``/login`` endpoint with an attacker-controlled
    ``redirect_uri`` and verify the response does not redirect off-domain."""
    if hosted_ui_url is None:
        results_writer.record(
            {
                "test_id": test_id,
                "status": "skipped",
                "layer": "fuzz",
                "target_kind": "api_route",
                "target_id": "cognito-hosted-ui-login",
                "skipped_reason": (
                    "COGNITO_HOSTED_UI_URL / COGNITO_DOMAIN_PREFIX not set"
                ),
            }
        )
        pytest.skip("COGNITO_HOSTED_UI_URL / COGNITO_DOMAIN_PREFIX not set")

    client_id = os.environ.get("COGNITO_CLIENT_ID", "").strip()
    if not client_id:
        results_writer.record(
            {
                "test_id": test_id,
                "status": "skipped",
                "layer": "fuzz",
                "target_kind": "api_route",
                "target_id": "cognito-hosted-ui-login",
                "skipped_reason": "COGNITO_CLIENT_ID not set",
            }
        )
        pytest.skip("COGNITO_CLIENT_ID not set")

    redirect_value = str(payload.get("payload") or "")
    qs = urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "scope": "openid",
            "redirect_uri": redirect_value,
        }
    )
    url = f"{hosted_ui_url}/login?{qs}"

    started = time.monotonic()
    try:
        # allow_redirects=False so we can inspect the Location header.
        response = http_session.request("GET", url, allow_redirects=False)
        status_code = response.status_code
        location = response.headers.get("Location")
    except requests.RequestException as exc:
        duration = time.monotonic() - started
        results_writer.record(
            {
                "test_id": test_id,
                "status": "skipped",
                "layer": "fuzz",
                "target_kind": "api_route",
                "target_id": "cognito-hosted-ui-login",
                "skipped_reason": f"request error: {type(exc).__name__}",
                "duration_seconds": duration,
            }
        )
        pytest.skip(f"request error: {exc}")

    duration = time.monotonic() - started
    status, severity = classify_open_redirect_response(status_code, location)
    row: dict = {
        "test_id": test_id,
        "status": status,
        "layer": "fuzz",
        "target_kind": "api_route",
        "target_id": "cognito-hosted-ui-login",
        "duration_seconds": duration,
    }
    if severity is not None:
        row["severity"] = severity
    if status == "fail":
        row["evidence_path"] = f"fuzz/results.json#{test_id}"
    results_writer.record(row)

    if status == "fail":
        pytest.fail(
            f"{test_id}: status={status_code} location={location!r} — "
            f"Cognito Hosted UI may be following an off-domain redirect_uri "
            f"(severity={severity})"
        )
