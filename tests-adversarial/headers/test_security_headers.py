"""Security-header probes (checklist item #31).

For a representative set of routes (1 SPA root + 3 API routes), fetch the
response and run the classifiers in `classifiers.classify_security_headers_bundle`.

Each individual header check is its own test row in the harness output:

  - ``headers.csp.<route>``       — Content-Security-Policy
  - ``headers.xfo.<route>``       — X-Frame-Options OR CSP frame-ancestors
  - ``headers.xcto.<route>``      — X-Content-Type-Options: nosniff
  - ``headers.referrer.<route>``  — Referrer-Policy

`Permissions-Policy` is informational only and is not asserted — its absence
is noted in the row but does not FAIL.

HSTS is covered by `test_https_only.py::test_hsts_header`; we cross-link
rather than re-test it here.
"""

from __future__ import annotations

import time

import pytest
import requests

from headers.classifiers import classify_security_headers_bundle
from headers.conftest import evidence_path_for


# Representative routes per Block-B spec (1 SPA root + 3 API routes covering
# GET/POST shapes). The SPA root cell is reported against the dashboard page;
# the API routes are reported against their manifest ids.
_PROBE_TARGETS: list[tuple[str, str, str, str]] = [
    # (route_id, target_kind, target_id, path_under_host)
    ("spa-root", "page", "dashboard", "/"),
    ("get-health", "api_route", "get-health", "/health"),
    ("get-findings", "api_route", "get-findings", "/findings"),
    ("get-dashboard", "api_route", "get-dashboard", "/dashboard"),
]


# Header keys exposed by the bundle classifier — used to fan out tests.
_HEADER_TEST_KEYS: tuple[str, ...] = ("csp", "xfo", "xcto", "referrer")


@pytest.fixture
def _fetched_headers(
    target_base_url: str,
    api_base_url: str,
    http_session: requests.Session,
    identities: dict,
) -> dict[str, tuple[int, requests.structures.CaseInsensitiveDict, float]]:
    """One HTTP fetch per probe target, shared across the per-header tests.

    Returns ``{route_id: (status_code, headers, duration_seconds)}``. Auth
    headers are attached for API routes so the response reflects real,
    authenticated traffic. The SPA root needs no auth.
    """
    from src.identity.cognito_auth import Persona

    ciso = identities[Persona.CISO]
    auth_header = {"Authorization": f"Bearer {ciso.id_token}"}

    out: dict[str, tuple[int, requests.structures.CaseInsensitiveDict, float]] = {}
    for route_id, target_kind, _, path in _PROBE_TARGETS:
        if target_kind == "page":
            url = f"{target_base_url}{path}"
            headers = {}
        else:
            url = f"{api_base_url}{path}"
            headers = dict(auth_header)
        started = time.monotonic()
        try:
            resp = http_session.get(url, headers=headers, allow_redirects=True)
        except requests.RequestException:
            # Record an empty header dict so the per-key tests still fire
            # (and FAIL with reason="header missing"); also stash the
            # transport error on the row so the operator sees what happened.
            out[route_id] = (
                0,
                requests.structures.CaseInsensitiveDict(),
                time.monotonic() - started,
            )
            continue
        out[route_id] = (resp.status_code, resp.headers, time.monotonic() - started)
    return out


@pytest.mark.parametrize(
    ("route_id", "target_kind", "target_id", "_path"),
    _PROBE_TARGETS,
    ids=[t[0] for t in _PROBE_TARGETS],
)
@pytest.mark.parametrize(
    "header_key",
    _HEADER_TEST_KEYS,
    ids=list(_HEADER_TEST_KEYS),
)
def test_security_header(
    route_id: str,
    target_kind: str,
    target_id: str,
    _path: str,
    header_key: str,
    _fetched_headers,
    results_writer,
) -> None:
    """One row per (probe target × header key). Routes the classifier output
    through to the harness writer.
    """
    status_code, headers, duration = _fetched_headers[route_id]
    if status_code == 0:
        results_writer.record(
            {
                "test_id": f"headers.{header_key}.{route_id}",
                "status": "skipped",
                "layer": "headers",
                "target_kind": target_kind,
                "target_id": target_id,
                "persona": "ciso" if target_kind == "page" else None,
                "skipped_reason": "fetch failed at transport layer",
                "duration_seconds": duration,
            }
        )
        pytest.skip(f"{route_id}: fetch failed")

    bundle = classify_security_headers_bundle(headers)
    status, severity, reason = bundle[header_key]
    test_id = f"headers.{header_key}.{route_id}"
    row: dict = {
        "test_id": test_id,
        "status": status,
        "layer": "headers",
        "target_kind": target_kind,
        "target_id": target_id,
        "duration_seconds": duration,
    }
    if target_kind == "page":
        row["persona"] = "ciso"
    if severity:
        row["severity"] = severity
    if status == "fail":
        row["evidence_path"] = evidence_path_for(test_id)
    results_writer.record(row)

    if status == "fail":
        pytest.fail(f"{test_id}: {reason}")
