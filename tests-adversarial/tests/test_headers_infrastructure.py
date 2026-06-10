"""Unit tests for the headers / TLS layer's classifiers (Block B).

These tests exercise the pure functions in `headers/classifiers.py` with
synthetic inputs. No network calls; no `requests.Session`. The contract is:

  - Every classifier returns ``(status, severity, reason)``.
  - ``status`` is exactly ``"pass"`` or ``"fail"`` (matches CellStatus values).
  - ``severity`` is one of ``"low" | "medium" | "high"`` on FAIL and ``None``
    on PASS.
  - ``reason`` is a non-empty human-readable string.

Coverage:

  * `classify_https_redirect`           — 302→https / 302→http / 200 / 404.
  * `classify_hsts_header`              — missing / short max-age / 1y / 2y.
  * `classify_tls_version_accepted`     — TLS 1.0/1.1 accepted+rejected, 1.2.
  * `classify_negotiated_cipher`        — weak cipher detection + edge cases.
  * `classify_csp_header`               — missing / present / unsafe-eval.
  * `classify_clickjacking_headers`     — XFO DENY / CSP frame-ancestors /
                                           both missing.
  * `classify_x_content_type_options`   — present / missing / wrong value.
  * `classify_referrer_policy`          — strict-origin / missing / unsafe-url.
  * `classify_cors_response`            — wildcard+credentials, echo, rejection.
  * `classify_csrf_cookie_only`         — 401 / 200 / 500.
  * `classify_security_headers_bundle`  — composite + presence of all 4 keys.

Also pins `builder.py`'s acceptance of ``layer="headers"`` so the rest of the
harness writes the layer's results without an exception.
"""

from __future__ import annotations

from headers.classifiers import (
    HSTS_MIN_MAX_AGE_SECONDS,
    SEVERITY_HIGH,
    SEVERITY_LOW,
    SEVERITY_MEDIUM,
    classify_clickjacking_headers,
    classify_cors_response,
    classify_csp_header,
    classify_csrf_cookie_only,
    classify_hsts_header,
    classify_https_redirect,
    classify_negotiated_cipher,
    classify_referrer_policy,
    classify_security_headers_bundle,
    classify_tls_version_accepted,
    classify_x_content_type_options,
)
from src.coverage.builder import CellStatus, TestResult, build_matrix


# ─────────────────────────── HTTPS redirect ──────────────────────────────────


def test_https_redirect_302_to_https_passes():
    status, severity, reason = classify_https_redirect(302, "https://example.com/")
    assert status == "pass"
    assert severity is None
    assert "https" in reason


def test_https_redirect_301_to_https_passes():
    status, severity, _ = classify_https_redirect(301, "https://example.com/")
    assert status == "pass"
    assert severity is None


def test_https_redirect_308_to_https_passes():
    status, severity, _ = classify_https_redirect(308, "https://example.com/")
    assert status == "pass"
    assert severity is None


def test_https_redirect_200_is_fail_high():
    status, severity, _ = classify_https_redirect(200, None)
    assert status == "fail"
    assert severity == SEVERITY_HIGH


def test_https_redirect_302_to_http_is_fail_high():
    status, severity, _ = classify_https_redirect(302, "http://example.com/")
    assert status == "fail"
    assert severity == SEVERITY_HIGH


def test_https_redirect_302_missing_location_is_fail_high():
    status, severity, _ = classify_https_redirect(302, None)
    assert status == "fail"
    assert severity == SEVERITY_HIGH


def test_https_redirect_404_is_fail_medium():
    status, severity, _ = classify_https_redirect(404, None)
    assert status == "fail"
    assert severity == SEVERITY_MEDIUM


# ────────────────────────────── HSTS ─────────────────────────────────────────


def test_hsts_missing_is_fail_medium():
    status, severity, _ = classify_hsts_header(None)
    assert status == "fail"
    assert severity == SEVERITY_MEDIUM


def test_hsts_empty_is_fail_medium():
    status, severity, _ = classify_hsts_header("")
    assert status == "fail"
    assert severity == SEVERITY_MEDIUM


def test_hsts_no_max_age_directive_is_fail_medium():
    status, severity, _ = classify_hsts_header("includeSubDomains")
    assert status == "fail"
    assert severity == SEVERITY_MEDIUM


def test_hsts_short_max_age_is_fail_low():
    status, severity, _ = classify_hsts_header("max-age=3600")
    assert status == "fail"
    assert severity == SEVERITY_LOW


def test_hsts_one_year_passes():
    status, severity, _ = classify_hsts_header(
        f"max-age={HSTS_MIN_MAX_AGE_SECONDS}; includeSubDomains"
    )
    assert status == "pass"
    assert severity is None


def test_hsts_two_year_passes():
    status, severity, _ = classify_hsts_header("max-age=63072000; preload")
    assert status == "pass"
    assert severity is None


def test_hsts_quoted_max_age_passes():
    status, _, _ = classify_hsts_header('max-age="31536000"')
    assert status == "pass"


# ────────────────────────────── TLS ──────────────────────────────────────────


def test_tls10_accepted_is_fail_high():
    status, severity, _ = classify_tls_version_accepted("TLSv1", accepted=True)
    assert status == "fail"
    assert severity == SEVERITY_HIGH


def test_tls11_accepted_is_fail_high():
    status, severity, _ = classify_tls_version_accepted("TLSv1.1", accepted=True)
    assert status == "fail"
    assert severity == SEVERITY_HIGH


def test_tls10_rejected_passes():
    status, severity, _ = classify_tls_version_accepted("TLSv1", accepted=False)
    assert status == "pass"
    assert severity is None


def test_tls11_rejected_passes():
    status, _, _ = classify_tls_version_accepted("TLSv1.1", accepted=False)
    assert status == "pass"


def test_tls12_accepted_passes():
    status, severity, _ = classify_tls_version_accepted("TLSv1.2", accepted=True)
    assert status == "pass"
    assert severity is None


def test_tls12_rejected_is_fail_medium():
    status, severity, _ = classify_tls_version_accepted("TLSv1.2", accepted=False)
    assert status == "fail"
    assert severity == SEVERITY_MEDIUM


def test_tls_unknown_version_is_fail_medium():
    status, severity, _ = classify_tls_version_accepted("TLSv99", accepted=True)
    assert status == "fail"
    assert severity == SEVERITY_MEDIUM


def test_negotiated_cipher_strong_passes():
    status, severity, _ = classify_negotiated_cipher("ECDHE-RSA-AES128-GCM-SHA256")
    assert status == "pass"
    assert severity is None


def test_negotiated_cipher_rc4_is_fail_high():
    status, severity, _ = classify_negotiated_cipher("RC4-SHA")
    assert status == "fail"
    assert severity == SEVERITY_HIGH


def test_negotiated_cipher_3des_is_fail_high():
    status, severity, _ = classify_negotiated_cipher("DES-CBC3-SHA")
    assert status == "fail"
    assert severity == SEVERITY_HIGH


def test_negotiated_cipher_null_is_fail_high():
    status, severity, _ = classify_negotiated_cipher("NULL-MD5")
    assert status == "fail"
    assert severity == SEVERITY_HIGH


def test_negotiated_cipher_export_is_fail_high():
    status, severity, _ = classify_negotiated_cipher("EXP-RC4-MD5")
    assert status == "fail"
    assert severity == SEVERITY_HIGH


def test_negotiated_cipher_missing_is_fail_medium():
    status, severity, _ = classify_negotiated_cipher(None)
    assert status == "fail"
    assert severity == SEVERITY_MEDIUM


# ───────────────────────────── CSP ───────────────────────────────────────────


def test_csp_missing_is_fail_medium():
    status, severity, _ = classify_csp_header(None)
    assert status == "fail"
    assert severity == SEVERITY_MEDIUM


def test_csp_present_safe_passes():
    status, severity, _ = classify_csp_header("default-src 'self'; script-src 'self'")
    assert status == "pass"
    assert severity is None


def test_csp_unsafe_eval_is_fail_low():
    status, severity, _ = classify_csp_header("script-src 'self' 'unsafe-eval'")
    assert status == "fail"
    assert severity == SEVERITY_LOW


def test_csp_unsafe_inline_is_fail_low():
    status, severity, _ = classify_csp_header(
        "default-src 'self'; script-src 'self' 'unsafe-inline'"
    )
    assert status == "fail"
    assert severity == SEVERITY_LOW


def test_csp_data_in_script_src_is_fail_low():
    status, severity, _ = classify_csp_header(
        "default-src 'self'; script-src 'self' data:"
    )
    assert status == "fail"
    assert severity == SEVERITY_LOW


def test_csp_data_in_img_src_passes():
    """data: in img-src is fine and common (data-URL inline images)."""
    status, _, _ = classify_csp_header(
        "default-src 'self'; img-src 'self' data:; script-src 'self'"
    )
    assert status == "pass"


# ────────────────────── X-Frame-Options / clickjacking ──────────────────────


def test_clickjacking_xfo_deny_passes():
    status, severity, _ = classify_clickjacking_headers("DENY", None)
    assert status == "pass"
    assert severity is None


def test_clickjacking_xfo_sameorigin_passes():
    status, _, _ = classify_clickjacking_headers("SAMEORIGIN", None)
    assert status == "pass"


def test_clickjacking_csp_frame_ancestors_none_passes():
    status, _, _ = classify_clickjacking_headers(
        None, "default-src 'self'; frame-ancestors 'none'"
    )
    assert status == "pass"


def test_clickjacking_csp_frame_ancestors_self_passes():
    status, _, _ = classify_clickjacking_headers(
        None, "default-src 'self'; frame-ancestors 'self'"
    )
    assert status == "pass"


def test_clickjacking_both_missing_is_fail_medium():
    status, severity, _ = classify_clickjacking_headers(None, None)
    assert status == "fail"
    assert severity == SEVERITY_MEDIUM


def test_clickjacking_csp_no_frame_ancestors_is_fail_medium():
    status, severity, _ = classify_clickjacking_headers(
        None, "default-src 'self'; script-src 'self'"
    )
    assert status == "fail"
    assert severity == SEVERITY_MEDIUM


def test_clickjacking_frame_ancestors_wildcard_is_fail_low():
    status, severity, _ = classify_clickjacking_headers(None, "frame-ancestors *")
    assert status == "fail"
    assert severity == SEVERITY_LOW


# ────────────────────── X-Content-Type-Options ──────────────────────────────


def test_xcto_nosniff_passes():
    status, _, _ = classify_x_content_type_options("nosniff")
    assert status == "pass"


def test_xcto_nosniff_case_insensitive_passes():
    status, _, _ = classify_x_content_type_options("NoSniff")
    assert status == "pass"


def test_xcto_missing_is_fail_low():
    status, severity, _ = classify_x_content_type_options(None)
    assert status == "fail"
    assert severity == SEVERITY_LOW


def test_xcto_wrong_value_is_fail_low():
    status, severity, _ = classify_x_content_type_options("sniff")
    assert status == "fail"
    assert severity == SEVERITY_LOW


# ─────────────────────────── Referrer-Policy ─────────────────────────────────


def test_referrer_strict_origin_passes():
    status, _, _ = classify_referrer_policy("strict-origin-when-cross-origin")
    assert status == "pass"


def test_referrer_no_referrer_passes():
    status, _, _ = classify_referrer_policy("no-referrer")
    assert status == "pass"


def test_referrer_missing_is_fail_low():
    status, severity, _ = classify_referrer_policy(None)
    assert status == "fail"
    assert severity == SEVERITY_LOW


def test_referrer_unsafe_url_is_fail_low():
    status, severity, _ = classify_referrer_policy("unsafe-url")
    assert status == "fail"
    assert severity == SEVERITY_LOW


def test_referrer_takes_first_token():
    """Referrer-Policy allows a comma-separated fallback list. The first
    token is the operative one — that's what we should grade."""
    status, _, _ = classify_referrer_policy("strict-origin, unsafe-url")
    assert status == "pass"


# ───────────────────────────────── CORS ──────────────────────────────────────


def test_cors_wildcard_with_credentials_is_fail_high():
    status, severity, _ = classify_cors_response("https://evil.com", "*", "true")
    assert status == "fail"
    assert severity == SEVERITY_HIGH


def test_cors_echo_attacker_origin_is_fail_high():
    status, severity, _ = classify_cors_response(
        "https://evil.com", "https://evil.com", None
    )
    assert status == "fail"
    assert severity == SEVERITY_HIGH


def test_cors_echo_attacker_origin_case_insensitive_is_fail_high():
    """ACAO comparison must be case-insensitive on the host portion."""
    status, severity, _ = classify_cors_response(
        "https://EVIL.com", "https://evil.com", None
    )
    assert status == "fail"
    assert severity == SEVERITY_HIGH


def test_cors_no_acao_passes():
    """No ACAO header at all = browser won't expose response = PASS."""
    status, severity, _ = classify_cors_response("https://evil.com", None, None)
    assert status == "pass"
    assert severity is None


def test_cors_fixed_allowed_origin_passes():
    """The deployment has a fixed allowed origin (e.g. the SPA host) — it
    should NOT echo unknown origins. Passing in a different attacker origin
    + a fixed ACAO is PASS."""
    status, _, _ = classify_cors_response(
        "https://evil.com", "https://d5u0vv1zl3eqd.cloudfront.net", None
    )
    assert status == "pass"


def test_cors_wildcard_without_credentials_passes():
    """`Access-Control-Allow-Origin: *` without credentials is fine for
    public APIs — the browser just can't send cookies/auth."""
    status, severity, _ = classify_cors_response("https://evil.com", "*", None)
    assert status == "pass"
    assert severity is None


# ────────────────────────────── CSRF ─────────────────────────────────────────


def test_csrf_401_passes():
    status, severity, _ = classify_csrf_cookie_only(401)
    assert status == "pass"
    assert severity is None


def test_csrf_403_passes():
    status, severity, _ = classify_csrf_cookie_only(403)
    assert status == "pass"
    assert severity is None


def test_csrf_200_is_fail_high():
    status, severity, _ = classify_csrf_cookie_only(200)
    assert status == "fail"
    assert severity == SEVERITY_HIGH


def test_csrf_500_is_fail_medium():
    status, severity, _ = classify_csrf_cookie_only(500)
    assert status == "fail"
    assert severity == SEVERITY_MEDIUM


# ─────────────────────────── Bundle composite ────────────────────────────────


def test_bundle_returns_all_four_keys():
    out = classify_security_headers_bundle({})
    assert set(out.keys()) == {"csp", "xfo", "xcto", "referrer"}


def test_bundle_empty_headers_all_fail():
    out = classify_security_headers_bundle({})
    for status, _, _ in out.values():
        assert status == "fail"


def test_bundle_all_present_all_pass():
    out = classify_security_headers_bundle(
        {
            "Content-Security-Policy": "default-src 'self'",
            "X-Frame-Options": "DENY",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "strict-origin",
        }
    )
    for status, _, _ in out.values():
        assert status == "pass", f"expected pass, got {status}"


def test_bundle_lowercase_header_names_match():
    """requests' CaseInsensitiveDict and plain dicts both should work."""
    out = classify_security_headers_bundle(
        {
            "content-security-policy": "default-src 'self'",
            "x-frame-options": "DENY",
            "x-content-type-options": "nosniff",
            "referrer-policy": "strict-origin",
        }
    )
    for status, _, _ in out.values():
        assert status == "pass"


# ────────────── builder.py accepts layer="headers" ──────────────────────────


def test_builder_accepts_headers_layer():
    """Pin that `layer="headers"` is allowed by the validation. The builder
    treats `layer` as an opaque string, so this should round-trip without
    complaint — but if someone ever adds an allow-list, this test will
    catch the regression."""
    manifest = {
        "personas": [
            {"id": "ciso", "username": "x@y.z", "cognito_group": "ciso", "label": "x"},
            {"id": "soc", "username": "x@y.z", "cognito_group": "soc", "label": "x"},
            {"id": "grc", "username": "x@y.z", "cognito_group": "grc", "label": "x"},
            {"id": "employee", "username": "x@y.z", "cognito_group": "e", "label": "x"},
        ],
        "pages": [
            {
                "id": "dashboard",
                "file": "ui/src/pages/Dashboard.jsx",
                "route": "/",
                "label": "Dashboard",
                "accessible_to": ["ciso", "soc", "grc"],
                "blocked_for": ["employee"],
            }
        ],
        "api_routes": [
            {
                "id": "get-health",
                "method": "GET",
                "path": "/health",
                "file": "x",
                "auth_required": False,
            }
        ],
        "agent_tools": [],
    }
    results = [
        TestResult(
            test_id="headers.csp.dashboard",
            status=CellStatus.PASS,
            layer="headers",
            target_kind="page",
            target_id="dashboard",
            persona="ciso",
        ),
        TestResult(
            test_id="headers.cors.get-health.evil",
            status=CellStatus.PASS,
            layer="headers",
            target_kind="api_route",
            target_id="get-health",
        ),
    ]
    matrix = build_matrix(manifest, results)
    assert matrix.pages["dashboard"]["ciso"] == CellStatus.PASS
    assert len(matrix.api_routes["get-health"]) == 1
    assert matrix.api_routes["get-health"][0]["layer"] == "headers"
