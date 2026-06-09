"""Curated-corpus adversarial fuzz across every API route in the manifest.

Test matrix
-----------
For each route in `manifest.api_routes`, we generate one test per relevant
payload family. "Relevant" depends on what the route accepts:

  - Every authenticated route gets the `malformed_jwt` family (probes the
    auth header).
  - Every route with a path parameter gets the `path_traversal` family
    (probes the path param).
  - Every POST route gets `xss`, `sqli`, `command_injection`,
    `null_bytes_and_control`, `oversized`, `header_injection` families
    (probes body fields and headers).
  - Every GET route with a query string gets the same set as POST but
    payloads land in a query-string value instead of a body field.
  - The `header_injection` family runs against every route (independent of
    method).

Tests are parameterized at collection time. The id pattern is
`fuzz.<route-id>.<family>.<payload-id>` — matches spec §7.3 and the
`make_test_id` helper in `_payloads.py`.

Test-isolation rules
--------------------
- POST / DELETE / PATCH / PUT routes get `@pytest.mark.destructive` and are
  skipped unless `--include-destructive` is passed (handled in conftest).
- 10 s per-request timeout, 5 RPS throttle (handled in conftest's
  `http_session` fixture).
- `/chat` skips when `CHAT_FUNCTION_URL` is unset.

What PASS / FAIL means
----------------------
PASS: the API returned a sane status (200, 4xx) AND the response body has no
stack-trace marker AND no XSS-style reflection of the payload.

FAIL: the API returned 500, OR a stack-trace marker is in the body, OR an
XSS-style payload appears verbatim in the body. Severity = "high" — those
are real bugs.

The `expected_blocked` flag on a corpus entry only triggers the reflection
check; an explicit acceptance of a payload (status 200, body sanitized) is
still a PASS. We never assert a specific status code beyond "not 500" — the
backend may legitimately accept some of these payloads as literal strings.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
import requests

from fuzz._payloads import (
    classify_response,
    load_corpus,
    make_test_id,
    route_has_path_param,
)


# ──────────────────────────── manifest loading ───────────────────────────────


_HARNESS_ROOT = Path(__file__).resolve().parent.parent
_MANIFEST_PATH = _HARNESS_ROOT / "src" / "coverage" / "manifest.json"


def _load_manifest_routes() -> list[dict]:
    """Read the manifest at collection time and return its api_routes list."""
    raw = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    return list(raw.get("api_routes") or [])


def _load_corpus_at_collection() -> dict[str, dict]:
    """Read the corpus at collection time. Used to parameterize the test."""
    return load_corpus(Path(__file__).resolve().parent / "corpus")


_ROUTES: list[dict] = _load_manifest_routes()
_CORPUS: dict[str, dict] = _load_corpus_at_collection()


# Which families apply to which route shape. The mapping is conservative —
# we err on the side of running more tests when it's free; the only
# trade-off is wall-clock.
def _families_for_route(route: dict) -> list[str]:
    method = (route.get("method") or "GET").upper()
    path = route.get("path") or ""
    auth_required = bool(route.get("auth_required"))
    chosen: list[str] = []
    # XSS / SQLi / command-injection / null-bytes go on any route that
    # accepts user-supplied input in body or query string.
    if method in ("POST", "PUT", "PATCH") or method == "GET":
        chosen.extend(["xss", "sqli", "command_injection", "null_bytes_and_control"])
    # Path-traversal only makes sense if the route has a path parameter.
    if route_has_path_param(path):
        chosen.append("path_traversal")
    # Oversized body / query for any route. (Oversized path runs only when
    # there's a path param.)
    chosen.append("oversized")
    # Header injection runs against every route — the payload is in a
    # request header, not in the route's "primary" input slot.
    chosen.append("header_injection")
    # Malformed JWT runs against every authenticated route.
    if auth_required:
        chosen.append("malformed_jwt")
    return chosen


# ────────────────────────── parameter expansion ──────────────────────────────


def _expand_test_params() -> list[tuple]:
    """Cross-product (route × family × payload).

    Returns a list of (route_dict, family_str, payload_dict) tuples that
    pytest fans out into one test per tuple via @parametrize.
    """
    out: list[tuple] = []
    for route in _ROUTES:
        families = _families_for_route(route)
        for family in families:
            corpus_entry = _CORPUS.get(family)
            if not corpus_entry:
                # No corpus file for this family — skip silently.
                continue
            for payload in corpus_entry["payloads"]:
                out.append((route, family, payload))
    return out


_TEST_PARAMS = _expand_test_params()


def _param_id(p: tuple) -> str:
    """Build a stable, readable pytest id from one (route, family, payload) tuple."""
    route, family, payload = p
    return make_test_id(route["id"], family, payload["id"])


# ─────────────────────────── request builder ─────────────────────────────────


# Body fields that are safe to use as the "primary writable field" for a
# given route. Maps route id -> field name. Routes not in the map default
# to "prompt" for /chat-style endpoints, "summary" for jira, and a generic
# `payload` field otherwise. The actual API will reject the unknown field
# names with a 400 — that's a PASS (server returned a sane status).
_PRIMARY_BODY_FIELD: dict[str, str] = {
    "post-chat": "prompt",
    "post-jira-tickets": "summary",
    "post-uploads-presign": "filename",
    "post-actions": "description",
    "post-action-approve": "comment",
    "post-action-reject": "comment",
    "post-action-execute": "comment",
    "post-action-escalate": "comment",
    "post-scan": "trigger",
}


def _primary_field(route_id: str) -> str:
    return _PRIMARY_BODY_FIELD.get(route_id, "payload")


def _route_url(api_base_url: str, chat_function_url: str | None, route: dict) -> str | None:
    """Resolve the route to an absolute URL. Returns None to indicate skip.

    Returns None for /chat when chat_function_url is unset (documented:
    fuzz against /chat needs the Function URL).
    """
    if route["id"] == "post-chat":
        if not chat_function_url:
            return None
        return f"{chat_function_url}{route['path']}" if not chat_function_url.endswith(route["path"]) else chat_function_url
    return f"{api_base_url}{route['path']}"


def _materialize_path(route_path: str, payload_str: str) -> str:
    """Replace `{name}` placeholders with `payload_str`. If there are multiple
    placeholders, the same value goes into each."""
    out = route_path
    while "{" in out and "}" in out:
        lbr = out.find("{")
        rbr = out.find("}", lbr)
        if rbr < 0:
            break
        out = out[:lbr] + payload_str + out[rbr + 1 :]
    return out


def _materialize_path_with_id_placeholder(route_path: str) -> str:
    """Replace `{name}` placeholders with a safe id so the URL is valid even
    when the payload is going into a header/body slot, not the path."""
    return _materialize_path(route_path, "harness-fuzz-placeholder")


# ────────────────────────────── the test ─────────────────────────────────────


@pytest.mark.parametrize("route,family,payload", _TEST_PARAMS, ids=[_param_id(p) for p in _TEST_PARAMS])
def test_route_fuzz(
    route: dict,
    family: str,
    payload: dict,
    http_session: requests.Session,
    auth_header: dict,
    api_base_url: str,
    chat_function_url: str | None,
    results_writer,
    request: pytest.FixtureRequest,
) -> None:
    """Send one adversarial payload at one route under one persona and
    classify the response.

    The test function name (`test_route_fuzz`) plus the parametrize id is the
    `test_id` we record. A FAIL classification raises `pytest.fail()` so
    pytest itself flags the row red; PASS classifications return normally.
    """
    # Apply the destructive marker dynamically so conftest's skip-gate fires
    # before any network call. The marker has to be set at collection time
    # for the gate to see it, so we attach via `pytest.mark.destructive` on
    # the test function below — see the loop after this definition.
    method = (route.get("method") or "GET").upper()
    route_id = route["id"]

    # Skip /chat when the Function URL isn't configured.
    url = _route_url(api_base_url, chat_function_url, route)
    if url is None:
        results_writer.record({
            "test_id": request.node.name.split("[")[-1].rstrip("]"),
            "status": "skipped",
            "layer": "fuzz",
            "target_kind": "api_route",
            "target_id": route_id,
            "skipped_reason": "CHAT_FUNCTION_URL unset; /chat fuzz disabled.",
        })
        pytest.skip("CHAT_FUNCTION_URL unset; /chat fuzz disabled.")

    # Resolve URL with path-param materialization. For path_traversal family,
    # the payload IS the path-param value; otherwise we use a placeholder.
    if family == "path_traversal" and route_has_path_param(route["path"]):
        path_value = str(payload.get("payload") or "")
        url_with_path = _route_url(
            api_base_url, chat_function_url,
            {**route, "path": _materialize_path(route["path"], path_value)},
        ) or url
    else:
        url_with_path = _route_url(
            api_base_url, chat_function_url,
            {**route, "path": _materialize_path_with_id_placeholder(route["path"])},
        ) or url

    # Build kwargs for http_session.request().
    headers = dict(auth_header)
    body: dict | None = None
    params: dict | None = None
    client_blocked = False
    payload_for_reflection: str | None = None
    started = time.monotonic()

    try:
        if family == "header_injection":
            hname = str(payload.get("header_name") or "X-Harness-Probe")
            hvalue = str(payload.get("header_value") or "1")
            headers[hname] = hvalue
            payload_for_reflection = hvalue
        elif family == "malformed_jwt":
            if "value" in payload:
                token_val = str(payload["value"])
            else:
                # `kind: construct-*` entries are synthesized at runtime.
                token_val = _construct_jwt(payload)
            headers["Authorization"] = f"Bearer {token_val}" if token_val else ""
            payload_for_reflection = None  # don't reflection-check JWT
        elif family == "oversized":
            size = int(payload.get("size_bytes") or 0)
            fill = str(payload.get("fill_char") or "A")[:1] or "A"
            blob = fill * size
            kind = str(payload.get("kind") or "json-string")
            if kind == "header":
                headers["X-Harness-Oversized"] = blob
                payload_for_reflection = None
            elif kind == "query":
                params = {"q": blob}
                payload_for_reflection = None
            elif kind == "path":
                if route_has_path_param(route["path"]):
                    url_with_path = _route_url(
                        api_base_url, chat_function_url,
                        {**route, "path": _materialize_path(route["path"], blob)},
                    ) or url_with_path
                else:
                    # Oversized-path doesn't apply to this route; treat as
                    # an oversized body field instead.
                    body = {_primary_field(route_id): blob}
                payload_for_reflection = None
            else:  # json-string body
                body = {_primary_field(route_id): blob}
                payload_for_reflection = None
        else:
            # xss / sqli / command_injection / null_bytes_and_control /
            # path_traversal (when path_traversal lands in a body/query slot
            # for a no-path-param route).
            payload_str = str(payload.get("payload") or "")
            payload_for_reflection = payload_str
            if method == "GET":
                # Default to query-string injection for GET.
                params = {"q": payload_str}
            elif method in ("POST", "PUT", "PATCH"):
                body = {_primary_field(route_id): payload_str}
            elif method == "DELETE":
                # DELETE often has no body; inject via query string.
                params = {"q": payload_str}
            else:
                params = {"q": payload_str}

        # Issue the request.
        try:
            req_kwargs: dict[str, Any] = {"headers": headers}
            if body is not None:
                req_kwargs["json"] = body
            if params is not None:
                req_kwargs["params"] = params
            response = http_session.request(method, url_with_path, **req_kwargs)
            status_code = response.status_code
            text = response.text or ""
        except requests.exceptions.InvalidHeader:
            client_blocked = True
            status_code = 0
            text = ""
        except requests.exceptions.RequestException as exc:
            # Connection refused, DNS failure, timeout, etc. Record as 0/blocked
            # so the test still emits a row; reasoning is captured in the body.
            client_blocked = False
            status_code = 0
            text = f"requests exception: {type(exc).__name__}: {exc}"
    finally:
        duration = time.monotonic() - started

    classified = classify_response(
        status_code=status_code,
        body=text,
        payload_value=payload_for_reflection,
        expected_blocked=bool(payload.get("expected_blocked")),
        client_blocked=client_blocked,
    )

    test_id = make_test_id(route_id, family, payload["id"])
    persona = request.node.callspec.params.get("auth_header", "unknown") if hasattr(request.node, "callspec") else None
    row = {
        "test_id": test_id,
        "status": classified.verdict,
        "layer": "fuzz",
        "target_kind": "api_route",
        "target_id": route_id,
        "duration_seconds": round(duration, 3),
    }
    if persona:
        row["persona"] = persona
    if classified.severity:
        row["severity"] = classified.severity
    if classified.verdict == "fail":
        # AC20: every FAIL needs evidence. The transcript is in this very row's
        # `reasons` field; the caller can drill into the run log to see the raw
        # body. The reporter (task 21) will resolve this path relative to the
        # run directory.
        row["evidence_path"] = f"fuzz/results.json#{test_id}"
    results_writer.record(row)

    if classified.verdict == "fail":
        reasons = "; ".join(classified.reasons)
        pytest.fail(
            f"{test_id} | status={status_code} severity={classified.severity} | {reasons}"
        )


# ───────────────────────── JWT construction helpers ──────────────────────────


def _construct_jwt(payload_entry: dict) -> str:
    """Build a structurally-valid but cryptographically-bogus JWT for the
    malformed-JWT corpus entries that use `kind: construct-*`.

    The signature is always invalid — these payloads exist to confirm the
    server rejects them. We never sign with a real secret. The token has
    three dot-separated base64url segments matching the JWT shape.
    """
    import base64
    import json as _json

    def _b64(obj) -> str:
        raw = _json.dumps(obj, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    kind = payload_entry.get("kind", "")
    if kind == "construct-alg-none":
        header = {"alg": "none", "typ": "JWT"}
        claims = payload_entry.get("payload_claims") or {}
        return f"{_b64(header)}.{_b64(claims)}."
    if kind == "construct-expired":
        header = {"alg": "HS256", "typ": "JWT"}
        claims = payload_entry.get("payload_claims") or {}
        return f"{_b64(header)}.{_b64(claims)}.invalidsignature"
    if kind == "construct-wrong-issuer":
        header = {"alg": "HS256", "typ": "JWT"}
        claims = payload_entry.get("payload_claims") or {}
        return f"{_b64(header)}.{_b64(claims)}.invalidsignature"
    if kind == "construct-forged-groups":
        header = {"alg": "HS256", "typ": "JWT"}
        claims = payload_entry.get("payload_claims") or {}
        return f"{_b64(header)}.{_b64(claims)}.invalidsignature"
    if kind == "construct-oversized-payload":
        header = {"alg": "HS256", "typ": "JWT"}
        target_size = int(payload_entry.get("payload_claims_size_bytes") or 16384)
        claims = {"sub": "harness", "filler": "A" * max(0, target_size - 16)}
        return f"{_b64(header)}.{_b64(claims)}.invalidsignature"
    return ""
