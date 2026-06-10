"""Clickjacking probe (checklist item #56).

Two tests:

  - ``headers.clickjacking.iframe-embed`` — fetch the SPA root and confirm
    that *either* ``X-Frame-Options: DENY|SAMEORIGIN`` *or* a CSP
    ``frame-ancestors`` directive restricts framing. Same logic as
    `test_security_headers::test_security_header[xfo]` but packaged as a
    single named test for the clickjacking row in the matrix.

The companion ``headers.clickjacking.iframe-render`` test is implemented as
a Playwright E2E spec at `e2e/tests/clickjacking.spec.js` — Playwright is
the only sane way to assert that a browser actually refused to render an
iframe. We don't duplicate the headers-side check here in Python.
"""

from __future__ import annotations

import time

import pytest
import requests

from headers.classifiers import classify_clickjacking_headers
from headers.conftest import evidence_path_for


def test_iframe_embed_blocked_via_headers(
    target_base_url: str,
    http_session: requests.Session,
    results_writer,
) -> None:
    """Fetch the SPA root and confirm headers DENY framing.

    FAIL MEDIUM if neither X-Frame-Options nor CSP frame-ancestors restricts
    iframe embedding — the SPA is exposed to clickjacking.
    """
    test_id = "headers.clickjacking.iframe-embed"
    started = time.monotonic()
    try:
        resp = http_session.get(target_base_url + "/", allow_redirects=True)
    except requests.RequestException as exc:
        results_writer.record(
            {
                "test_id": test_id,
                "status": "skipped",
                "layer": "headers",
                "target_kind": "page",
                "target_id": "dashboard",
                "persona": "ciso",
                "skipped_reason": f"transport error: {type(exc).__name__}",
                "duration_seconds": time.monotonic() - started,
            }
        )
        pytest.skip(f"{test_id}: {exc}")

    duration = time.monotonic() - started
    xfo = resp.headers.get("X-Frame-Options") or resp.headers.get("x-frame-options")
    csp = resp.headers.get("Content-Security-Policy") or resp.headers.get(
        "content-security-policy"
    )
    status, severity, reason = classify_clickjacking_headers(xfo, csp)

    row: dict = {
        "test_id": test_id,
        "status": status,
        "layer": "headers",
        "target_kind": "page",
        "target_id": "dashboard",
        "persona": "ciso",
        "duration_seconds": duration,
    }
    if severity:
        row["severity"] = severity
    if status == "fail":
        row["evidence_path"] = evidence_path_for(test_id)
    results_writer.record(row)

    if status == "fail":
        pytest.fail(f"{test_id}: {reason}")
