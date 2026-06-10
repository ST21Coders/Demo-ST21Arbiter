"""CORS misconfiguration probes (checklist item #35).

For each API route in the manifest, send three preflight (OPTIONS) requests
with unusual `Origin:` headers:

  - ``https://evil.com``  (off-domain attacker)
  - ``null``              (sandbox iframe / data URI origin)
  - ``file://``           (local file)

Then classify the response with `classifiers.classify_cors_response`.

The check is intentionally lenient: an unrecognised origin should either get
no `Access-Control-Allow-Origin` header at all, or get a fixed allowed
origin that does NOT equal the attacker origin. A wildcard ACAO without
credentials is also acceptable (read-only public surface).

FAIL HIGH:
  - ACAO == "*" AND Allow-Credentials == "true" (CSRF-via-CORS).
  - ACAO echoes the attacker origin back (origin reflection).

We only fan out across a representative subset of routes per origin to keep
the probe count small (12 cells for the layer total). The selected routes
cover GET and POST shapes, plus the IDOR-critical /conversations/{id} surface.
"""

from __future__ import annotations

import time

import pytest
import requests

from headers.classifiers import classify_cors_response
from headers.conftest import api_routes, evidence_path_for

_ATTACKER_ORIGINS: list[tuple[str, str]] = [
    ("evil", "https://evil.com"),
    ("null", "null"),
    ("file", "file://"),
]

# Subset of routes we probe — keeps the per-run cost down (the harness's
# 5 RPS throttle would otherwise eat ~30 s on a full sweep). Each represents
# a distinct response-shape class.
_PROBED_ROUTE_IDS: frozenset[str] = frozenset(
    {
        "get-health",
        "get-findings",
        "post-chat",
        "get-conversation-by-id",
        "post-actions",
    }
)


def _route_url(api_base_url: str, path_template: str) -> str:
    """Concrete a path template by replacing ``{param}`` placeholders with
    ``probe`` — the request will likely 404 but the CORS preflight response
    doesn't depend on the path-param value. Keeps the test deterministic.
    """
    import re

    return api_base_url + re.sub(r"\{[^}]+\}", "probe", path_template)


@pytest.mark.parametrize(
    "route",
    [r for r in api_routes() if r["id"] in _PROBED_ROUTE_IDS],
    ids=lambda r: r["id"],
)
@pytest.mark.parametrize(
    ("origin_id", "origin_header"),
    _ATTACKER_ORIGINS,
    ids=[o[0] for o in _ATTACKER_ORIGINS],
)
def test_cors_preflight_rejects_unrecognised_origins(
    route: dict,
    origin_id: str,
    origin_header: str,
    api_base_url: str,
    http_session: requests.Session,
    results_writer,
) -> None:
    """One row per (route × attacker_origin). FAIL HIGH on origin reflection
    or wildcard+credentials.
    """
    test_id = f"headers.cors.{route['id']}.{origin_id}"
    url = _route_url(api_base_url, route["path"])

    headers = {
        "Origin": origin_header,
        "Access-Control-Request-Method": route["method"],
        "Access-Control-Request-Headers": "authorization,content-type",
    }
    started = time.monotonic()
    try:
        resp = http_session.options(url, headers=headers, allow_redirects=False)
    except requests.RequestException as exc:
        results_writer.record(
            {
                "test_id": test_id,
                "status": "skipped",
                "layer": "headers",
                "target_kind": "api_route",
                "target_id": route["id"],
                "skipped_reason": f"transport error: {type(exc).__name__}",
                "duration_seconds": time.monotonic() - started,
            }
        )
        pytest.skip(f"{test_id}: {exc}")

    duration = time.monotonic() - started
    acao = resp.headers.get("Access-Control-Allow-Origin") or resp.headers.get(
        "access-control-allow-origin"
    )
    acac = resp.headers.get("Access-Control-Allow-Credentials") or resp.headers.get(
        "access-control-allow-credentials"
    )
    status, severity, reason = classify_cors_response(origin_header, acao, acac)

    row: dict = {
        "test_id": test_id,
        "status": status,
        "layer": "headers",
        "target_kind": "api_route",
        "target_id": route["id"],
        "duration_seconds": duration,
    }
    if severity:
        row["severity"] = severity
    if status == "fail":
        row["evidence_path"] = evidence_path_for(test_id)
    results_writer.record(row)

    if status == "fail":
        pytest.fail(f"{test_id}: {reason}")
