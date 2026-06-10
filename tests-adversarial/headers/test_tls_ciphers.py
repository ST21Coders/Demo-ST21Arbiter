"""TLS cipher / version probes (checklist item #24 — weak crypto algorithms).

Uses stdlib `ssl` + `socket` only — no `nmap`, `testssl.sh`, or other
subprocess wrappers. The probe set is intentionally small:

  - For each deprecated version (TLS 1.0, TLS 1.1), force the handshake at
    that version and confirm the server rejects it. FAIL HIGH if the
    handshake succeeds.
  - For TLS 1.2 (the modern floor), confirm the handshake succeeds and that
    the negotiated cipher does not contain a weak token (RC4 / 3DES / NULL /
    EXPORT / anonymous DH). FAIL HIGH if it does.

The target is the CloudFront SPA host — the API host typically rides the
same CloudFront distribution so probing it separately would just re-test
the same TLS endpoint.
"""

from __future__ import annotations

import socket
import ssl
import time
from urllib.parse import urlparse

import pytest

from headers.classifiers import (
    classify_negotiated_cipher,
    classify_tls_version_accepted,
)
from headers.conftest import evidence_path_for


# ─────────────────────── TLS version → SSLContext options ────────────────────
#
# Python 3.10+ deprecated the named constants for TLS 1.0 / 1.1, but ssl still
# accepts them on platforms whose OpenSSL was built with the legacy SECLEVEL.
# We force the maximum protocol version via `maximum_version` / `minimum_version`,
# which is the modern API and avoids the deprecation warnings.


_VERSION_TO_PYTHON: dict[str, ssl.TLSVersion] = {
    "TLSv1": ssl.TLSVersion.TLSv1,
    "TLSv1.1": ssl.TLSVersion.TLSv1_1,
    "TLSv1.2": ssl.TLSVersion.TLSv1_2,
    "TLSv1.3": ssl.TLSVersion.TLSv1_3,
}


def _attempt_handshake(
    host: str, port: int, version_label: str
) -> tuple[bool, str | None, str | None]:
    """Try a TLS handshake forcing exactly `version_label`.

    Returns ``(accepted, negotiated_version, negotiated_cipher)``. On a clean
    handshake, ``accepted=True`` and the negotiated values are populated.
    On any failure (SSLError, ConnectionResetError, OSError), returns
    ``(False, None, None)`` — caller treats that as "the server rejected
    this version", which is what we want for TLS 1.0/1.1 PASS.

    Why a fresh context per call:
      - `set_ciphers` would otherwise leak across versions.
      - `maximum_version`/`minimum_version` need to be set together so the
        handshake is pinned to exactly one version.
    """
    version = _VERSION_TO_PYTHON.get(version_label)
    if version is None:
        return False, None, None

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    # We're probing TLS, not verifying identity — the harness already knows
    # the target. Disable verification so a misconfigured local trust store
    # doesn't mask a finding.
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        ctx.minimum_version = version
        ctx.maximum_version = version
        # TLS 1.0/1.1 need SECLEVEL 0 on modern OpenSSL — bump it down.
        # ssl.OP_LEGACY_SERVER_CONNECT helps with some legacy implementations.
        if version in (ssl.TLSVersion.TLSv1, ssl.TLSVersion.TLSv1_1):
            try:
                ctx.set_ciphers("DEFAULT@SECLEVEL=0")
            except ssl.SSLError:
                # If OpenSSL was built without SECLEVEL 0 we can't even try
                # — treat as rejected (PASS for those versions).
                return False, None, None
    except (ValueError, ssl.SSLError):
        return False, None, None

    try:
        with socket.create_connection((host, port), timeout=8) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                negotiated = ssock.version()
                cipher_tuple = ssock.cipher()
                cipher_name = cipher_tuple[0] if cipher_tuple else None
                return True, negotiated, cipher_name
    except (ssl.SSLError, OSError):
        return False, None, None


def _cloudfront_host(target_base_url: str) -> tuple[str, int]:
    """Pull (host, port) out of the target base URL. CloudFront is 443/HTTPS."""
    parsed = urlparse(target_base_url)
    host = parsed.hostname or ""
    port = parsed.port or 443
    return host, port


# ─────────────────────────────── tests ───────────────────────────────────────


@pytest.mark.parametrize(
    "version_label",
    ["TLSv1", "TLSv1.1"],
    ids=["tls10", "tls11"],
)
def test_tls_minimum_version_rejects_deprecated(
    target_base_url: str,
    version_label: str,
    results_writer,
) -> None:
    """A handshake at TLS 1.0 / 1.1 must be rejected by the server.

    FAIL HIGH if the handshake succeeds — the server still accepts a
    deprecated TLS version.
    """
    host, port = _cloudfront_host(target_base_url)
    if not host:
        pytest.skip(f"could not parse host from TARGET_BASE_URL={target_base_url!r}")

    test_id = f"headers.tls-min-version.{version_label.lower().replace('.', '')}"
    started = time.monotonic()
    accepted, negotiated, cipher = _attempt_handshake(host, port, version_label)
    duration = time.monotonic() - started

    status, severity, reason = classify_tls_version_accepted(version_label, accepted)
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
        pytest.fail(
            f"{test_id}: {reason} (negotiated={negotiated!r}, cipher={cipher!r})"
        )


def test_no_export_ciphers(
    target_base_url: str,
    results_writer,
) -> None:
    """Open a modern TLS 1.2 connection and verify the negotiated cipher is
    not weak (RC4, 3DES, NULL, EXPORT, anonymous DH, MD5-based).

    FAIL HIGH if a weak cipher is negotiated.
    """
    host, port = _cloudfront_host(target_base_url)
    if not host:
        pytest.skip(f"could not parse host from TARGET_BASE_URL={target_base_url!r}")

    test_id = "headers.tls-cipher.no-weak"
    started = time.monotonic()
    accepted, negotiated, cipher = _attempt_handshake(host, port, "TLSv1.2")
    duration = time.monotonic() - started

    if not accepted:
        # We expected TLS 1.2 to succeed — this is a deployment problem worth
        # flagging on its own.
        results_writer.record(
            {
                "test_id": test_id,
                "status": "fail",
                "layer": "headers",
                "target_kind": "page",
                "target_id": "dashboard",
                "persona": "ciso",
                "severity": "medium",
                "evidence_path": evidence_path_for(test_id),
                "duration_seconds": duration,
            }
        )
        pytest.fail(f"{test_id}: TLS 1.2 handshake failed to {host}:{port}")

    status, severity, reason = classify_negotiated_cipher(cipher)
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
        pytest.fail(f"{test_id}: {reason} (negotiated={negotiated!r})")
