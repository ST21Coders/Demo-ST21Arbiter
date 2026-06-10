"""HTTPS-only probes (checklist item #23 — plaintext transmission).

For each public host (the CloudFront SPA URL, the API URL if separate, and
the Lambda Function URL if separate), confirm:

  - A GET on `http://` redirects to `https://` (no plaintext page served).
  - The corresponding `https://` GET sets a Strict-Transport-Security header
    with `max-age >= 1 year`.

PASS = behaviour matches modern best practice. FAIL severity follows
`classifiers.classify_https_redirect` / `classify_hsts_header`.

Why two tests per host
----------------------
The redirect probe and the HSTS probe target different controls. The redirect
catches an actively-served plaintext page (HIGH severity); the HSTS probe
catches a missing defence-in-depth header (MEDIUM). Splitting them keeps
the per-test signal clean.
"""

from __future__ import annotations

import time
from urllib.parse import urlparse

import pytest
import requests

from headers.classifiers import classify_hsts_header, classify_https_redirect
from headers.conftest import evidence_path_for


# ─────────────────────────── host enumeration ────────────────────────────────


def _https_hosts(
    target_base_url: str,
    api_base_url: str,
    chat_function_url: str | None,
) -> list[tuple[str, str, str]]:
    """Return ``[(host_id, https_url, target_id), ...]`` for every public host.

    - ``cloudfront`` → SPA root (target the dashboard page as a sentinel — the
      SPA host serves every page so the page id is just for the matrix cell).
    - ``api`` → API Gateway base (target ``get-health`` which exists on every
      stage and is unauthenticated).
    - ``chat-function-url`` → Lambda Function URL for /chat (target
      ``post-chat``). Skipped when CHAT_FUNCTION_URL is unset.

    De-duplicate by URL — if api_base_url == ``${target_base_url}/api`` and
    api fronting is on CloudFront, we emit the CloudFront entry once.
    """
    hosts: list[tuple[str, str, str]] = []
    seen_urls: set[str] = set()

    def _add(host_id: str, url: str, target_id: str) -> None:
        # Normalise to the scheme://host portion only — we only care about
        # the TLS endpoint, not the path.
        parsed = urlparse(url)
        normalised = f"{parsed.scheme}://{parsed.netloc}"
        if normalised in seen_urls:
            return
        seen_urls.add(normalised)
        hosts.append((host_id, normalised, target_id))

    _add("cloudfront", target_base_url, "dashboard")
    _add("api", api_base_url, "get-health")
    if chat_function_url:
        _add("chat-function-url", chat_function_url, "post-chat")
    return hosts


# ─────────────────────────────── HTTP → HTTPS ────────────────────────────────


def _http_url_for(https_url: str) -> str:
    """Rewrite a https:// URL to http:// for the redirect probe."""
    parsed = urlparse(https_url)
    return f"http://{parsed.netloc}{parsed.path or '/'}"


def test_http_redirects_to_https(
    target_base_url: str,
    api_base_url: str,
    chat_function_url: str | None,
    http_session: requests.Session,
    results_writer,
) -> None:
    """For every public host, GET the http:// scheme and confirm a redirect
    to https://. FAIL HIGH if a 200 is returned (plaintext page) or if the
    redirect target is still on http://.
    """
    hosts = _https_hosts(target_base_url, api_base_url, chat_function_url)
    if not hosts:
        pytest.skip("no public hosts to probe")

    failures: list[str] = []
    for host_id, https_url, target_id in hosts:
        test_id = f"headers.https-only.{host_id}"
        http_url = _http_url_for(https_url)
        started = time.monotonic()
        try:
            resp = http_session.get(http_url, allow_redirects=False)
        except requests.RequestException as exc:
            # Network refusal on port 80 is effectively the same as "no
            # plaintext served" — record as PASS with a note.
            results_writer.record(
                {
                    "test_id": test_id,
                    "status": "pass",
                    "layer": "headers",
                    "target_kind": "api_route" if target_id != "dashboard" else "page",
                    "target_id": target_id,
                    "persona": "ciso" if target_id == "dashboard" else None,
                    "duration_seconds": time.monotonic() - started,
                    "skipped_reason": f"http:// refused at transport ({type(exc).__name__})",
                }
            )
            continue

        duration = time.monotonic() - started
        location = resp.headers.get("Location") or resp.headers.get("location")
        status, severity, reason = classify_https_redirect(resp.status_code, location)
        row: dict = {
            "test_id": test_id,
            "status": status,
            "layer": "headers",
            "target_kind": "api_route" if target_id != "dashboard" else "page",
            "target_id": target_id,
            "duration_seconds": duration,
        }
        if target_id == "dashboard":
            row["persona"] = "ciso"
        if severity:
            row["severity"] = severity
        if status == "fail":
            row["evidence_path"] = evidence_path_for(test_id)
            failures.append(f"{test_id}: {reason}")
        results_writer.record(row)

    if failures:
        pytest.fail("; ".join(failures))


def test_hsts_header(
    target_base_url: str,
    api_base_url: str,
    chat_function_url: str | None,
    http_session: requests.Session,
    results_writer,
) -> None:
    """For every public host, GET on https:// and confirm Strict-Transport-Security
    is set with ``max-age >= 1 year``. Missing header = FAIL MEDIUM; short
    max-age = FAIL LOW.
    """
    hosts = _https_hosts(target_base_url, api_base_url, chat_function_url)
    if not hosts:
        pytest.skip("no public hosts to probe")

    failures: list[str] = []
    for host_id, https_url, target_id in hosts:
        test_id = f"headers.hsts.{host_id}"
        started = time.monotonic()
        try:
            resp = http_session.get(https_url, allow_redirects=False)
        except requests.RequestException as exc:
            results_writer.record(
                {
                    "test_id": test_id,
                    "status": "skipped",
                    "layer": "headers",
                    "target_kind": "api_route" if target_id != "dashboard" else "page",
                    "target_id": target_id,
                    "persona": "ciso" if target_id == "dashboard" else None,
                    "duration_seconds": time.monotonic() - started,
                    "skipped_reason": f"https:// request failed: {type(exc).__name__}",
                }
            )
            continue

        duration = time.monotonic() - started
        hsts_value = resp.headers.get("Strict-Transport-Security") or resp.headers.get(
            "strict-transport-security"
        )
        status, severity, reason = classify_hsts_header(hsts_value)
        row: dict = {
            "test_id": test_id,
            "status": status,
            "layer": "headers",
            "target_kind": "api_route" if target_id != "dashboard" else "page",
            "target_id": target_id,
            "duration_seconds": duration,
        }
        if target_id == "dashboard":
            row["persona"] = "ciso"
        if severity:
            row["severity"] = severity
        if status == "fail":
            row["evidence_path"] = evidence_path_for(test_id)
            failures.append(f"{test_id}: {reason}")
        results_writer.record(row)

    if failures:
        pytest.fail("; ".join(failures))
