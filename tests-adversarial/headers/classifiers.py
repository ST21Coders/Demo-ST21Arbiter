"""headers/classifiers.py — pure response-to-(status, severity) classifiers.

Every classifier in this file is a pure function. The live pytest tests in
`headers/test_*.py` build an HTTP response (or a TLS handshake outcome) and
hand the result to one of these functions; the unit tests in
`tests/test_headers_infrastructure.py` exercise the same functions with
synthetic inputs.

Keeping the policy logic out of the pytest fixtures means:

  - Edge cases (missing-header, weak-cipher, unsafe-eval-in-CSP) are testable
    without a live deployment.
  - Severity is one source of truth — when the operator asks "why HIGH?",
    they read this file, not five test modules.
  - The harness can re-classify recorded raw evidence later without re-running
    the layer (helpful when the policy bar moves).

Severity bands (mirrors auth/conftest.py's convention):

  HIGH    — direct security regression (plaintext served, weak cipher accepted,
            CORS echoes attacker origin, CSRF-via-cookie works, etc.).
  MEDIUM  — missing defence-in-depth (no CSP, no XFO/frame-ancestors, short
            HSTS max-age).
  LOW     — informational hardening gap (missing X-Content-Type-Options,
            unsafe-eval in CSP).
"""

from __future__ import annotations

from collections.abc import Mapping

# ──────────────────────────── severity bands ─────────────────────────────────

SEVERITY_HIGH = "high"
SEVERITY_MEDIUM = "medium"
SEVERITY_LOW = "low"

# HSTS minimum max-age. RFC 6797 doesn't pick a number; the OWASP Secure
# Headers Project recommends >= 1 year (31536000 s). Anything shorter is a
# defence-in-depth gap, not a direct break.
HSTS_MIN_MAX_AGE_SECONDS = 31_536_000

# Substrings considered weak when they appear in a negotiated TLS cipher
# suite name (uppercase comparison). The list is intentionally conservative —
# every entry is a known-broken or known-deprecated family.
_WEAK_CIPHER_TOKENS: tuple[str, ...] = (
    "RC4",
    "3DES",
    "DES",
    "NULL",
    "EXPORT",
    "ANON",
    "MD5",
    "IDEA",
    "SEED",
    "PSK",
    "EXP-",
)

# CSP source-list tokens that weaken the policy when present in script-src.
_UNSAFE_CSP_TOKENS: tuple[str, ...] = (
    "'unsafe-eval'",
    "'unsafe-inline'",
)

# Acceptable Referrer-Policy values. `unsafe-url` is explicitly NOT here —
# it leaks the full URL across origins.
_ACCEPTABLE_REFERRER_POLICIES: frozenset[str] = frozenset(
    {
        "no-referrer",
        "no-referrer-when-downgrade",
        "same-origin",
        "strict-origin",
        "strict-origin-when-cross-origin",
        "origin",
        "origin-when-cross-origin",
    }
)


# ────────────────────── HTTPS-only / HSTS classifier ─────────────────────────


def classify_https_redirect(
    status_code: int, location: str | None
) -> tuple[str, str | None, str]:
    """Classify a GET on the http:// scheme.

    PASS:    3xx (301/302/307/308) with a Location pointing at https://.
    FAIL HIGH: 200 (plaintext page served) OR a redirect Location still
               on http:// (open downgrade).
    FAIL MEDIUM: any other status — the host should redirect, full stop.

    Returns ``(status, severity, reason)``. ``status`` is the
    builder.TestResult.status string ("pass" / "fail"). ``severity`` is None
    on PASS.
    """
    if status_code in (301, 302, 307, 308):
        if location and location.lower().startswith("https://"):
            return "pass", None, f"redirect to {location}"
        if location and location.lower().startswith("http://"):
            return (
                "fail",
                SEVERITY_HIGH,
                f"redirect target still on http:// ({location})",
            )
        return (
            "fail",
            SEVERITY_HIGH,
            f"redirect missing or relative Location header (got {location!r})",
        )
    if 200 <= status_code < 300:
        return (
            "fail",
            SEVERITY_HIGH,
            f"plaintext page served on http:// (status {status_code})",
        )
    return (
        "fail",
        SEVERITY_MEDIUM,
        f"unexpected status {status_code} on http:// — expected 301/302/307/308",
    )


def _parse_hsts_max_age(header_value: str) -> int | None:
    """Pull ``max-age=N`` (integer seconds) out of a Strict-Transport-Security
    header value. Returns None if the directive is missing or malformed.

    Whitespace and case are tolerated to match RFC 6797 §6.1.
    """
    for raw in header_value.split(";"):
        part = raw.strip().lower()
        if not part.startswith("max-age"):
            continue
        _, _, val = part.partition("=")
        val = val.strip().strip('"')
        try:
            return int(val)
        except ValueError:
            return None
    return None


def classify_hsts_header(
    header_value: str | None,
) -> tuple[str, str | None, str]:
    """Classify a Strict-Transport-Security response header.

    PASS:        present and max-age >= 1 year.
    FAIL MEDIUM: header missing.
    FAIL LOW:    present but max-age < 1 year (operator should bump).
    """
    if not header_value:
        return "fail", SEVERITY_MEDIUM, "Strict-Transport-Security header missing"
    max_age = _parse_hsts_max_age(header_value)
    if max_age is None:
        return (
            "fail",
            SEVERITY_MEDIUM,
            f"Strict-Transport-Security has no parseable max-age directive ({header_value!r})",
        )
    if max_age < HSTS_MIN_MAX_AGE_SECONDS:
        return (
            "fail",
            SEVERITY_LOW,
            f"HSTS max-age {max_age}s < {HSTS_MIN_MAX_AGE_SECONDS}s (1 year)",
        )
    return "pass", None, f"HSTS max-age {max_age}s"


# ──────────────────────────── TLS classifiers ────────────────────────────────


def classify_tls_version_accepted(
    version: str, accepted: bool
) -> tuple[str, str | None, str]:
    """Classify whether a deprecated TLS version was accepted by the server.

    Inputs:
      version  — short label, e.g. "TLSv1", "TLSv1.1", "TLSv1.2".
      accepted — True if the handshake succeeded, False if it was rejected.

    Rules:
      - TLSv1.0 / TLSv1.1 accepted  → FAIL HIGH (deprecated, weak).
      - TLSv1.0 / TLSv1.1 rejected  → PASS.
      - TLSv1.2 / TLSv1.3 accepted  → PASS.
      - TLSv1.2 / TLSv1.3 rejected  → FAIL MEDIUM (server should support).
      - Unknown label               → FAIL MEDIUM (caller is asking about
                                         something we don't have policy for).
    """
    label = version.strip()
    if label in {"TLSv1", "TLSv1.0", "TLSv1.1"}:
        if accepted:
            return (
                "fail",
                SEVERITY_HIGH,
                f"deprecated {label} handshake succeeded — server still accepts it",
            )
        return "pass", None, f"deprecated {label} correctly rejected"
    if label in {"TLSv1.2", "TLSv1.3"}:
        if accepted:
            return "pass", None, f"{label} accepted"
        return (
            "fail",
            SEVERITY_MEDIUM,
            f"{label} handshake failed — server may not support modern TLS",
        )
    return (
        "fail",
        SEVERITY_MEDIUM,
        f"unrecognised TLS version label {label!r}",
    )


def classify_negotiated_cipher(cipher_name: str | None) -> tuple[str, str | None, str]:
    """Classify a negotiated TLS cipher suite name.

    PASS if the cipher contains no weak token (RC4, 3DES, NULL, EXPORT, etc.).
    FAIL HIGH otherwise. A missing / empty cipher name is treated as
    FAIL MEDIUM — the harness couldn't confirm strength.
    """
    if not cipher_name:
        return (
            "fail",
            SEVERITY_MEDIUM,
            "no cipher reported — handshake may have failed before negotiation",
        )
    upper = cipher_name.upper()
    for token in _WEAK_CIPHER_TOKENS:
        if token in upper:
            return (
                "fail",
                SEVERITY_HIGH,
                f"negotiated cipher {cipher_name!r} contains weak token {token!r}",
            )
    return "pass", None, f"cipher {cipher_name!r}"


# ────────────────────── security headers classifiers ─────────────────────────


def _norm_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    """Lower-case the header names so callers can do `h["content-security-policy"]`
    regardless of the wire casing. requests' `Response.headers` is a
    CaseInsensitiveDict, but we accept any Mapping for test ergonomics.
    """
    if not headers:
        return {}
    return {str(k).lower(): str(v) for k, v in headers.items()}


def classify_csp_header(
    csp_value: str | None,
) -> tuple[str, str | None, str]:
    """Classify a Content-Security-Policy response header.

    PASS:        present, no `unsafe-eval`, no `data:` in script-src.
    FAIL MEDIUM: header missing entirely (defence-in-depth gap).
    FAIL LOW:    present but contains `unsafe-eval` / `unsafe-inline` /
                 `data:` in a script-src directive.
    """
    if not csp_value:
        return (
            "fail",
            SEVERITY_MEDIUM,
            "Content-Security-Policy header missing",
        )
    lower = csp_value.lower()
    for unsafe_token in _UNSAFE_CSP_TOKENS:
        if unsafe_token in lower:
            return (
                "fail",
                SEVERITY_LOW,
                f"CSP contains {unsafe_token} — weakens script-src",
            )
    # Crude script-src/data: detection. We only flag when "data:" appears in a
    # script-src or default-src list; appearing in img-src is fine and common.
    for directive in lower.split(";"):
        directive = directive.strip()
        if directive.startswith("script-src") or directive.startswith("default-src"):
            if "data:" in directive:
                return (
                    "fail",
                    SEVERITY_LOW,
                    f"CSP {directive.split()[0]} allows data: scheme",
                )
    return "pass", None, f"CSP present ({csp_value[:80]!r})"


def classify_clickjacking_headers(
    xfo_value: str | None,
    csp_value: str | None,
) -> tuple[str, str | None, str]:
    """Classify X-Frame-Options + CSP frame-ancestors together.

    At least one must DENY framing for the route. Both missing = FAIL MEDIUM
    (clickjacking exposure).

    PASS: X-Frame-Options = DENY | SAMEORIGIN, OR CSP includes
          frame-ancestors with 'none' / 'self'.
    """
    xfo_norm = (xfo_value or "").strip().upper()
    csp_lower = (csp_value or "").lower()

    has_xfo_deny = xfo_norm in {"DENY", "SAMEORIGIN"}
    has_frame_ancestors = False
    has_frame_ancestors_safe = False
    for directive in csp_lower.split(";"):
        directive = directive.strip()
        if directive.startswith("frame-ancestors"):
            has_frame_ancestors = True
            # Accept 'none' or only 'self'.
            tokens = directive.split()[1:]
            if "'none'" in tokens:
                has_frame_ancestors_safe = True
            elif tokens and all(tok in {"'self'"} for tok in tokens):
                has_frame_ancestors_safe = True

    if has_xfo_deny:
        return (
            "pass",
            None,
            f"X-Frame-Options = {xfo_norm}",
        )
    if has_frame_ancestors_safe:
        return (
            "pass",
            None,
            "CSP frame-ancestors restricts framing",
        )
    if has_frame_ancestors:
        return (
            "fail",
            SEVERITY_LOW,
            "CSP frame-ancestors present but does not DENY framing",
        )
    return (
        "fail",
        SEVERITY_MEDIUM,
        "neither X-Frame-Options nor CSP frame-ancestors restricts framing — clickjacking exposure",
    )


def classify_x_content_type_options(
    value: str | None,
) -> tuple[str, str | None, str]:
    """Classify the X-Content-Type-Options header.

    PASS:     present and equals 'nosniff' (case-insensitive).
    FAIL LOW: missing or set to any other value.
    """
    if value and value.strip().lower() == "nosniff":
        return "pass", None, "X-Content-Type-Options = nosniff"
    if not value:
        return "fail", SEVERITY_LOW, "X-Content-Type-Options header missing"
    return (
        "fail",
        SEVERITY_LOW,
        f"X-Content-Type-Options has unexpected value {value!r}",
    )


def classify_referrer_policy(value: str | None) -> tuple[str, str | None, str]:
    """Classify the Referrer-Policy header.

    PASS:     present and in the acceptable set (excludes 'unsafe-url').
    FAIL LOW: missing OR set to 'unsafe-url' / unknown.
    """
    if not value:
        return "fail", SEVERITY_LOW, "Referrer-Policy header missing"
    norm = value.strip().lower()
    # The header allows comma-separated fallback lists; take the first token.
    first_token = norm.split(",")[0].strip()
    if first_token in _ACCEPTABLE_REFERRER_POLICIES:
        return "pass", None, f"Referrer-Policy = {first_token}"
    if first_token == "unsafe-url":
        return "fail", SEVERITY_LOW, "Referrer-Policy = unsafe-url leaks full URL"
    return (
        "fail",
        SEVERITY_LOW,
        f"Referrer-Policy has unrecognised value {first_token!r}",
    )


def classify_security_headers_bundle(
    headers: Mapping[str, str] | None,
) -> dict[str, tuple[str, str | None, str]]:
    """Run every per-header classifier and return the keyed results.

    Returns ``{key: (status, severity, reason)}`` where ``key`` is one of:
    ``csp``, ``xfo``, ``xcto``, ``referrer``. The clickjacking key (``xfo``
    here) folds X-Frame-Options + CSP frame-ancestors per
    `classify_clickjacking_headers`.
    """
    h = _norm_headers(headers)
    return {
        "csp": classify_csp_header(h.get("content-security-policy")),
        "xfo": classify_clickjacking_headers(
            h.get("x-frame-options"), h.get("content-security-policy")
        ),
        "xcto": classify_x_content_type_options(h.get("x-content-type-options")),
        "referrer": classify_referrer_policy(h.get("referrer-policy")),
    }


# ────────────────────────────── CORS classifier ──────────────────────────────


def classify_cors_response(
    sent_origin: str,
    acao_value: str | None,
    acac_value: str | None,
) -> tuple[str, str | None, str]:
    """Classify a CORS preflight or actual-request response.

    Inputs:
      sent_origin — the Origin header the harness sent (e.g. "https://evil.com").
      acao_value  — Access-Control-Allow-Origin response header (or None).
      acac_value  — Access-Control-Allow-Credentials response header (or None).

    Rules:
      - ACAO == "*" AND ACAC == "true" → FAIL HIGH (CSRF-via-CORS).
      - ACAO echoes sent_origin (case-insensitive) → FAIL HIGH (origin
        reflection — attacker-controlled origin is whitelisted).
      - ACAO absent OR set to a fixed allowed origin that doesn't equal
        sent_origin → PASS (browser blocks the cross-origin read).
      - ACAO == "*" without credentials → PASS (still readable but no
        cookies/auth flow — public surface).
    """
    acao_norm = (acao_value or "").strip()
    acac_norm = (acac_value or "").strip().lower()

    if acao_norm == "*" and acac_norm == "true":
        return (
            "fail",
            SEVERITY_HIGH,
            "Access-Control-Allow-Origin: * combined with Allow-Credentials: true (CSRF-via-CORS)",
        )
    if acao_norm and sent_origin and acao_norm.lower() == sent_origin.lower():
        return (
            "fail",
            SEVERITY_HIGH,
            f"Access-Control-Allow-Origin echoes attacker origin {sent_origin!r}",
        )
    return (
        "pass",
        None,
        f"CORS rejected — ACAO={acao_norm!r}, ACAC={acac_norm!r}",
    )


# ─────────────────────────────── CSRF classifier ─────────────────────────────


def classify_csrf_cookie_only(status_code: int) -> tuple[str, str | None, str]:
    """Classify a request sent with a cookie BUT no Authorization header.

    PASS:     401 / 403 — the API requires an explicit Authorization header
              and does not honour cookies (so classical CSRF is moot).
    FAIL HIGH: 2xx — the API has a cookie-based fallback that authenticates
              the request without an Authorization header. Any attacker-
              controlled cross-site form post could ride the cookie.
      FAIL MEDIUM: any other status (the API crashed or did something weird).
    """
    if status_code in (401, 403):
        return "pass", None, f"cookie-only auth rejected with {status_code}"
    if 200 <= status_code < 300:
        return (
            "fail",
            SEVERITY_HIGH,
            f"cookie-only request authenticated (status {status_code}) — CSRF-exposed",
        )
    return (
        "fail",
        SEVERITY_MEDIUM,
        f"unexpected status {status_code} for cookie-only request",
    )


__all__ = [
    "HSTS_MIN_MAX_AGE_SECONDS",
    "SEVERITY_HIGH",
    "SEVERITY_LOW",
    "SEVERITY_MEDIUM",
    "classify_clickjacking_headers",
    "classify_cors_response",
    "classify_csp_header",
    "classify_csrf_cookie_only",
    "classify_hsts_header",
    "classify_https_redirect",
    "classify_negotiated_cipher",
    "classify_referrer_policy",
    "classify_security_headers_bundle",
    "classify_tls_version_accepted",
    "classify_x_content_type_options",
]
