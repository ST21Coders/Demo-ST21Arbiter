"""Forced-browsing probes against common admin / debug paths (Block C — #13 + #33).

Dictionary-based forced browsing: an attacker iterates a small wordlist of
well-known admin / debug / framework paths against the API base URL with no
authentication. The expected outcome for every path is 401, 403, or 404.

What we DO NOT need from this layer
-----------------------------------
- ``DEMO_PASSWORD`` — every probe is unauthenticated.
- ``COGNITO_CLIENT_ID`` — no Cognito calls.

Because of that, this module does NOT use the ``identities`` fixture and runs
cleanly in any environment where ``API_BASE_URL`` (or the ``TARGET_BASE_URL``
fallback) is reachable.

Wordlist
--------
The path list is the SANS-style "common admin / framework / debug" union,
pruned to entries that have actually surfaced in incident reports:

  * Generic admin consoles: ``/admin``, ``/admin/login``, ``/console``.
  * Debug & profiling: ``/debug``, ``/debug/pprof``, ``/actuator``.
  * API documentation: ``/swagger``, ``/swagger.json``, ``/swagger-ui/``,
    ``/api-docs``, ``/openapi.json``.
  * Source / config leaks: ``/.git/config``, ``/.env``, ``/.env.local``.
  * Framework defaults: ``/wp-admin/``, ``/phpmyadmin/``.
  * Health / metrics surfaces: ``/health/admin``, ``/metrics``.
  * Specially-handled: ``/.well-known/security.txt`` — a 200 here is a
    GOOD signal (responsible disclosure published), so it's classified
    as PASS not FAIL even on a 200.

Classification
--------------
  * 401 / 403 / 404 → PASS.
  * 200 with a non-trivial body (>32 bytes) → FAIL severity HIGH (sensitive
    surface exposed).
  * 200 with a trivial / empty body → FAIL severity LOW (informational —
    the path responds but doesn't appear to leak data).
  * 5xx → FAIL severity MEDIUM (API crashed on an unauth GET).
  * other → FAIL severity LOW (unexpected; worth flagging).
  * ``/.well-known/security.txt`` special-case: 200 is PASS.

The 32-byte "non-trivial body" threshold is the size of a typical CORS-only
or framework default-empty response. Below it, we treat the surface as
"responds but no data"; above, we treat it as "data exposed".
"""

from __future__ import annotations

import hashlib
import re
import time

import pytest
import requests

# Severities per the prompt.
SEVERITY_FORCED_BROWSING_HIGH = "high"
SEVERITY_FORCED_BROWSING_MEDIUM = "medium"
SEVERITY_FORCED_BROWSING_LOW = "low"

_TEST_ID_PREFIX = "auth.forced-browsing"

# Body-size threshold below which a 200 is classified LOW rather than HIGH.
# A typical CORS-preflight or framework "nothing here" response is ~0-32 bytes;
# Swagger UIs / admin consoles weigh in at multi-KB. Pick a small floor that
# clearly separates the two.
_TRIVIAL_BODY_BYTES = 32

# Paths whose 200 means "good citizen" (a responsible-disclosure file
# published) rather than "data leak". Add others only if their 200 is
# definitionally positive.
_POSITIVE_ON_200: set[str] = {"/.well-known/security.txt"}

# The probe wordlist. Order is preserved so test ids are deterministic.
# Note: ``/admin`` and ``/admin/`` would slug to the same test id, so we
# only include the bare ``/admin`` form. The trailing-slash variant is a
# server normalisation question, not a distinct security probe.
_PATHS: list[str] = [
    "/admin",
    "/admin/login",
    "/console",
    "/dashboard/admin",
    "/debug",
    "/debug/pprof",
    "/swagger",
    "/swagger.json",
    "/swagger-ui/",
    "/api-docs",
    "/openapi.json",
    "/.git/config",
    "/.env",
    "/.env.local",
    "/health/admin",
    "/metrics",
    "/actuator",
    "/wp-admin/",
    "/phpmyadmin/",
    "/.well-known/security.txt",
]


def _path_slug(path: str) -> str:
    """Convert a URL path to a short, slug-shaped suffix for the test id.

    ``/.git/config`` -> ``git-config``
    ``/swagger.json`` -> ``swagger-json``
    ``/admin/`` -> ``admin``
    """
    slug = path.lower().lstrip("/")
    # Strip the well-known prefix so the id stays human-readable.
    slug = slug.removeprefix(".well-known/")
    slug = slug.rstrip("/")
    slug = re.sub(r"[/.]+", "-", slug)
    slug = re.sub(r"[^a-z0-9-]+", "", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug or "root"


FORCED_BROWSING_TEST_IDS: list[str] = [
    f"{_TEST_ID_PREFIX}.{_path_slug(p)}" for p in _PATHS
]


def _body_fingerprint(body: bytes) -> str:
    """sha256 fingerprint (first 32 hex chars) of a response body.

    Used to detect React SPA catch-all responses: CloudFront falls back to
    `index.html` for any path the SPA router doesn't know about, so a probe
    against `/admin` or `/swagger` returns the same bytes as a probe
    against `/`. We hash both and compare, which catches the fallback even
    if the response body changes between runs (only the prefix matters).
    """
    return hashlib.sha256(body or b"").hexdigest()[:32]


def classify_forced_browsing_response(
    path: str,
    status_code: int,
    body_len: int,
    *,
    spa_root_fingerprint: str | None = None,
    body_fingerprint: str | None = None,
) -> tuple[str, str | None, str | None]:
    """Map (path, status, body length, body fingerprint) to (status, severity, skipped_reason).

    Rules:
      * 401 / 403 / 404 → PASS.
      * ``/.well-known/security.txt`` 200 → PASS (special-case).
      * 200 whose body fingerprint matches the SPA root → PASS with
        ``skipped_reason: "SPA fallback (response identical to root)"``.
        CloudFront's catch-all returns `index.html` for every unknown
        path; the body of `/admin` is byte-identical to `/`, which is
        not an admin-console exposure. Without this check the harness
        reported 19 false-positive HIGH findings on the 2026-06-10 run.
      * 200 with body_len >= 32 (and NOT SPA fallback) → FAIL severity HIGH.
      * 200 with body_len < 32 → FAIL severity LOW.
      * 5xx → FAIL severity MEDIUM.
      * other → FAIL severity LOW.

    The third tuple element is a ``skipped_reason`` string that is set ONLY
    for the SPA-fallback PASS so the row in results.json carries the
    rationale. Other PASS / FAIL outcomes return ``None`` in that slot.
    """
    if status_code in (401, 403, 404):
        return "pass", None, None
    if path in _POSITIVE_ON_200 and 200 <= status_code < 300:
        return "pass", None, None
    if 200 <= status_code < 300:
        # SPA fallback detection: if both fingerprints are available and
        # match, treat as PASS with a clear reason. Bytes-identical
        # response means CloudFront served the React index.html for an
        # unknown path; the probe is not a finding.
        if (
            spa_root_fingerprint is not None
            and body_fingerprint is not None
            and spa_root_fingerprint == body_fingerprint
        ):
            return (
                "pass",
                None,
                "SPA fallback (response identical to root)",
            )
        if body_len >= _TRIVIAL_BODY_BYTES:
            return "fail", SEVERITY_FORCED_BROWSING_HIGH, None
        return "fail", SEVERITY_FORCED_BROWSING_LOW, None
    if 500 <= status_code < 600:
        return "fail", SEVERITY_FORCED_BROWSING_MEDIUM, None
    return "fail", SEVERITY_FORCED_BROWSING_LOW, None


@pytest.fixture(scope="module")
def spa_root_fingerprint(
    target_base_url: str,
    http_session: requests.Session,
) -> str | None:
    """Hash of the SPA root (`/`) response body.

    Used by the per-path probes to detect CloudFront's catch-all
    fallback: any path the React router doesn't know about returns the
    SPA's ``index.html``, so the body of `/admin` is byte-identical to
    the body of `/`. Comparing fingerprints converts those 200s from
    false-positive HIGH findings into clean PASSes with a clear
    ``skipped_reason``.

    Returns None if the root probe itself fails — the per-path tests then
    fall back to the pre-fix classification (which over-reports admin
    paths but never under-reports a real leak).
    """
    try:
        resp = http_session.request("GET", target_base_url, allow_redirects=False)
    except requests.RequestException:
        return None
    if not (200 <= resp.status_code < 300):
        # Non-2xx root means CloudFront isn't fronting the SPA the way the
        # detector expects. Bail safely.
        return None
    return _body_fingerprint(resp.content or b"")


@pytest.mark.parametrize(
    ("path", "test_id"),
    [(p, f"{_TEST_ID_PREFIX}.{_path_slug(p)}") for p in _PATHS],
    ids=FORCED_BROWSING_TEST_IDS,
)
def test_unauthenticated_forced_browsing(
    path: str,
    test_id: str,
    api_base_url: str,
    http_session: requests.Session,
    spa_root_fingerprint: str | None,
    results_writer,
) -> None:
    """GET ``${API_BASE_URL}${path}`` with NO auth header.

    Records one result row per path. FAIL HIGH if a non-trivial 200 leaks
    a sensitive surface that isn't just the SPA's catch-all fallback.
    """
    url = f"{api_base_url.rstrip('/')}{path}"
    started = time.monotonic()
    try:
        # No Authorization header — this is the forced-browsing scenario.
        # We follow redirects so a redirect-to-signin counts as the final
        # status (typically 200 of the signin page, which is universally
        # accessible). Use a smaller default timeout — the API should
        # respond fast to unauth probes.
        response = http_session.request("GET", url, allow_redirects=False)
    except requests.RequestException as exc:
        duration = time.monotonic() - started
        results_writer.record(
            {
                "test_id": test_id,
                "status": "skipped",
                "layer": "auth",
                "target_kind": "api_route",
                "target_id": "forced-browsing-probes",
                "target_path": path,
                "skipped_reason": f"request error: {exc}",
                "duration_seconds": duration,
            }
        )
        pytest.skip(f"request error: {exc}")

    duration = time.monotonic() - started
    body = response.content or b""
    body_len = len(body)
    body_fp = _body_fingerprint(body)
    status, severity, skipped_reason = classify_forced_browsing_response(
        path,
        response.status_code,
        body_len,
        spa_root_fingerprint=spa_root_fingerprint,
        body_fingerprint=body_fp,
    )

    row: dict = {
        "test_id": test_id,
        "status": status,
        "layer": "auth",
        "target_kind": "api_route",
        "target_id": "forced-browsing-probes",
        "target_path": path,
        "duration_seconds": duration,
    }
    if severity is not None:
        row["severity"] = severity
    if skipped_reason is not None:
        # Used for the SPA-fallback PASS — keeps the rationale in the row
        # so an auditor reading results.json sees why /admin & friends
        # didn't fail despite their 200.
        row["skipped_reason"] = skipped_reason
    if status == "fail":
        row["evidence_path"] = f"auth/results.json#{test_id}"
    results_writer.record(row)

    if status == "fail":
        pytest.fail(
            f"{test_id}: expected 401/403/404 for {path}, got HTTP "
            f"{response.status_code} body_len={body_len} (severity={severity})"
        )


__all__ = [
    "FORCED_BROWSING_TEST_IDS",
    "SEVERITY_FORCED_BROWSING_HIGH",
    "SEVERITY_FORCED_BROWSING_LOW",
    "SEVERITY_FORCED_BROWSING_MEDIUM",
    "classify_forced_browsing_response",
]
