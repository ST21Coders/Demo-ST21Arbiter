"""Fetch Cognito identities for all 4 demo users and write Playwright storageState JSON files.

Called from `e2e/global-setup.js` before any Playwright test runs. One JSON
file is emitted per persona (e.g. ``ciso.json``) into the output directory
passed on the command line. The Playwright config's per-persona projects then
load each file via ``use.storageState``.

Storage shape notes (verified against ``ui/src/hooks/useAuth.js``)
------------------------------------------------------------------
The ARBITER SPA does NOT use the Amazon Cognito JS SDK's standard
``CognitoIdentityServiceProvider.<clientId>.<username>.idToken`` keys in
``localStorage``. Instead it stores a single JSON blob in **sessionStorage**
under the key ``arbiter.tokens``:

    sessionStorage.setItem('arbiter.tokens', JSON.stringify({
        id_token,
        access_token,
        refresh_token,
        expires_at,   // epoch-ms; useAuth.js subtracts 60s from expires_in
    }))

``useAuth.js::load()`` reads that single key, ``getIdToken()`` returns
``.id_token``, ``isAuthenticated()`` checks ``Date.now() < expires_at``, and
``decodeIdTokenPayload()`` derives ``cognito:groups`` from the IdToken JWT
payload. So injecting that one key with valid tokens is sufficient to let the
SPA skip the Cognito Hosted UI redirect entirely.

Playwright's storageState JSON shape supports both ``localStorage`` and
``sessionStorage`` per origin, so we put the blob in the ``sessionStorage``
array (see https://playwright.dev/docs/api/class-browsercontext#browser-context-storage-state).

Exit codes:
    0  All four storage-state files were written.
    1  ``DEMO_PASSWORD`` unset, Cognito auth failed, or any other error.

CLI:
    python3.13 -m scripts.emit_storage_states <output_dir>

Example:
    python3.13 -m scripts.emit_storage_states e2e/storage-states/
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

from src.identity.cognito_auth import Identity, Persona, fetch_all

# Default deployed dev CloudFront. Override via TARGET_BASE_URL env var. Kept
# in sync with the Playwright config and the spec §8 env var table.
_DEFAULT_BASE_URL = "https://d5u0vv1zl3eqd.cloudfront.net/"

# JWT exp claim is in seconds since epoch (RFC 7519 §4.1.4); useAuth.js stores
# expires_at in milliseconds since epoch (Date.now()). We convert here so the
# SPA's ``isAuthenticated()`` (which compares against Date.now()) accepts the
# token. If the JWT has no exp, default to one hour from now in ms.
_FALLBACK_EXPIRY_MS = 60 * 60 * 1000


def _origin_from_url(base_url: str) -> str:
    """Return ``scheme://host[:port]`` with no path, query, or trailing slash.

    Playwright requires the ``origins[].origin`` field to be exactly the origin
    string the browser uses internally — i.e. no path component, no trailing
    slash. A mismatch results in the storage entries being silently ignored
    when the page loads.
    """
    parsed = urlparse(base_url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(
            f"TARGET_BASE_URL must be a full URL with scheme and host; got {base_url!r}"
        )
    return f"{parsed.scheme}://{parsed.netloc}"


def _decode_exp_ms(id_token: str) -> int:
    """Pull the ``exp`` claim from a JWT and return it in epoch-ms.

    Mirrors the no-signature-verify decode pattern from cognito_auth.py
    (we are a test client, not a server). Returns ``Date.now() +
    _FALLBACK_EXPIRY_MS`` if exp is missing or malformed — better to overshoot
    than to fail closed when the SPA's expiry check would reject otherwise.
    """
    import base64
    import time

    try:
        parts = id_token.split(".")
        if len(parts) < 2:
            return int(time.time() * 1000) + _FALLBACK_EXPIRY_MS
        payload_b64 = parts[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = payload.get("exp")
        if isinstance(exp, (int, float)) and exp > 0:
            return int(exp * 1000)
    except (ValueError, json.JSONDecodeError, base64.binascii.Error):
        pass
    return int(time.time() * 1000) + _FALLBACK_EXPIRY_MS


def _build_arbiter_tokens_blob(identity: Identity) -> str:
    """Build the exact JSON string the SPA stores under ``arbiter.tokens``.

    Shape verified against ``ui/src/hooks/useAuth.js`` (lines 150-156, 22).
    Note: ``refresh_token`` is the empty string — the SPA's ``refresh()``
    path only fires when load().refresh_token is truthy; we want the SPA to
    treat the injected session as valid-but-not-refreshable until the IdToken
    naturally expires, at which point a re-run of the harness fetches fresh
    tokens. This avoids the SPA hammering Cognito's /oauth2/token endpoint
    with a refresh_token boto3 didn't issue.
    """
    blob = {
        "id_token": identity.id_token,
        "access_token": identity.access_token,
        "refresh_token": "",
        "expires_at": _decode_exp_ms(identity.id_token),
    }
    return json.dumps(blob, separators=(",", ":"))


def build_storage_state(identity: Identity, base_url: str) -> dict:
    """Compose the Playwright ``storageState`` dict for one persona.

    The dict shape matches https://playwright.dev/docs/api/class-browsercontext#browser-context-storage-state-option-storage-state
    Returned as a plain dict so the caller can ``json.dump`` it directly.
    """
    origin = _origin_from_url(base_url)
    return {
        "cookies": [],
        "origins": [
            {
                "origin": origin,
                "localStorage": [],
                "sessionStorage": [
                    {
                        "name": "arbiter.tokens",
                        "value": _build_arbiter_tokens_blob(identity),
                    }
                ],
            }
        ],
    }


def emit(
    out_dir: Path,
    base_url: str,
    identities: Iterable[tuple[Persona, Identity]] | None = None,
) -> list[Path]:
    """Write one ``<persona>.json`` storage-state file per identity.

    Returns the list of written paths in iteration order so callers (and
    tests) can assert exactly what was produced. If ``identities`` is ``None``
    the production path of ``fetch_all()`` is used; tests inject their own.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    if identities is None:
        identities = list(fetch_all().items())

    written: list[Path] = []
    for persona, identity in identities:
        state = build_storage_state(identity, base_url)
        target = out_dir / f"{persona.value}.json"
        with target.open("w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
            fh.write("\n")
        written.append(target)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch Cognito IdTokens for the 4 demo personas and write Playwright "
            "storage-state JSON files. Called from e2e/global-setup.js."
        )
    )
    parser.add_argument(
        "out_dir",
        type=Path,
        help="Output directory. One <persona>.json file is written per persona.",
    )
    args = parser.parse_args(argv)

    base_url = os.environ.get("TARGET_BASE_URL", "").strip() or _DEFAULT_BASE_URL
    written = emit(args.out_dir, base_url)
    # Print one line per written file so global-setup can verify in stdout.
    for path in written:
        print(path)
    return 0


if (
    __name__ == "__main__"
):  # pragma: no cover - exercised via subprocess in global-setup
    sys.exit(main())
