"""Unit tests for the auth-abuse layer's infrastructure (task 14).

These tests run WITHOUT touching the deployed env. They cover the static
contracts the layer relies on: cross-persona enumeration count + canonical
AC9 id, expired-JWT construction, classification of synthetic responses to
TestResult rows, results-writer formatting, and end-to-end round-trip into
the coverage builder.

They do NOT run any of the parametrised tests in `auth/test_*.py` — those
need DEMO_PASSWORD + a deployed target and live under the auth pytest
selection.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import pytest

from auth.conftest import (
    AuthResultsWriter,
    SEVERITY_API_CRASH_MEDIUM,
    SEVERITY_EXPIRED_ACCEPTED_HIGH,
    SEVERITY_PRIV_ESC_HIGH,
    api_routes,
    auth_results_path,
    classify_cross_persona_response,
    classify_expired_token_response,
    make_jwt,
    manifest,
    persona_ids,
)
from auth.test_chat_no_signature import (
    PROMPT_TEXT as CHAT_NO_SIG_PROMPT,
    TARGET_ID as CHAT_NO_SIG_TARGET_ID,
    TEST_ID as CHAT_NO_SIG_TEST_ID,
    classify_chat_no_signature_response,
    strip_jwt_signature,
)
from auth.test_cross_persona import (
    CROSS_PERSONA_PAIRS,
    CROSS_PERSONA_TEST_IDS,
    materialise_path,
)
from auth.test_expired_token import build_expired_jwt
from auth.test_forged_groups import (
    FORGED_GROUPS_LATERAL_PAIR,
    FORGED_GROUPS_PAIRS,
    FORGED_GROUPS_TEST_IDS,
    FORGED_GROUPS_UPWARD_PAIRS,
    SEVERITY_FORGED_API_CRASH_MEDIUM,
    SEVERITY_FORGED_PRIV_ESC_HIGH,
    classify_forged_groups_response,
    forge_cognito_groups,
)


# ────────────────────── manifest / enumeration count ─────────────────────────


def _expected_cross_persona_count() -> int:
    """Compute the expected cross-persona count directly from the manifest.

    For each route: `len(personas) - len(accessible_to)` if `accessible_to`
    is a strict subset of `personas`, otherwise 0.
    """
    personas = set(persona_ids())
    total = 0
    for route in api_routes():
        accessible = set(route.get("accessible_to", []))
        if not accessible or accessible == personas:
            continue
        total += len(personas) - len(accessible)
    return total


def test_cross_persona_enumeration_count_matches_manifest() -> None:
    """The enumerated test list must match the per-route arithmetic above."""
    expected = _expected_cross_persona_count()
    actual = len(CROSS_PERSONA_TEST_IDS)
    assert actual == expected, (
        f"cross-persona test count mismatch: enumerator emitted {actual} "
        f"but the manifest arithmetic predicts {expected}"
    )


def test_cross_persona_canonical_ac9_id_present() -> None:
    """AC9 mandates the canonical id `auth.token-usage.soc-forbidden`."""
    assert "auth.token-usage.soc-forbidden" in CROSS_PERSONA_TEST_IDS, (
        "AC9 canonical id `auth.token-usage.soc-forbidden` is missing from "
        f"the enumerated list: {CROSS_PERSONA_TEST_IDS}"
    )


def test_cross_persona_token_usage_soc_pair_points_at_get_token_usage() -> None:
    """The AC9 pair must target the `/token-usage` GET route with persona=soc."""
    by_test_id = {
        test_id: (route, persona) for route, persona, test_id in CROSS_PERSONA_PAIRS
    }
    route, persona = by_test_id["auth.token-usage.soc-forbidden"]
    assert route["id"] == "get-token-usage"
    assert route["method"] == "GET"
    assert route["path"] == "/token-usage"
    assert persona == "soc"


def test_cross_persona_ids_are_unique() -> None:
    """Two enumerated tests must not collide on test_id."""
    assert len(CROSS_PERSONA_TEST_IDS) == len(set(CROSS_PERSONA_TEST_IDS))


def test_cross_persona_count_is_at_least_three_for_token_usage_route() -> None:
    """The `get-token-usage` route is ciso-only, so it produces 3 pairs."""
    tu_pairs = [p for p in CROSS_PERSONA_PAIRS if p[0]["id"] == "get-token-usage"]
    assert len(tu_pairs) == 3
    # Personas covered: soc, grc, employee (all non-CISO).
    personas = {p[1] for p in tu_pairs}
    assert personas == {"soc", "grc", "employee"}


def test_manifest_loads_with_expected_top_level_keys() -> None:
    """Smoke check — the auth layer relies on these keys."""
    m = manifest()
    for key in ("personas", "api_routes"):
        assert key in m, f"manifest missing required key {key!r}"


# ────────────────────────── expired-JWT construction ─────────────────────────


def _decode_segment(seg: str) -> dict:
    """Decode a JWT segment as JSON (mirrors api_handler._caller_claims)."""
    seg = seg + "=" * (-len(seg) % 4)
    return json.loads(base64.urlsafe_b64decode(seg))


def test_expired_jwt_is_syntactically_valid_three_segments() -> None:
    """A JWT must have exactly three dot-separated segments."""
    token = build_expired_jwt()
    parts = token.split(".")
    assert len(parts) == 3, f"expected 3 segments, got {len(parts)}: {token!r}"


def test_expired_jwt_payload_has_past_exp() -> None:
    """`exp` must be in the past relative to now."""
    token = build_expired_jwt()
    payload = _decode_segment(token.split(".")[1])
    assert "exp" in payload, "expired JWT missing `exp` claim"
    assert payload["exp"] < int(time.time()), (
        f"expired JWT `exp` is not in the past: exp={payload['exp']} "
        f"now={int(time.time())}"
    )


def test_expired_jwt_payload_has_groups_and_sub() -> None:
    """The synthetic JWT must carry the claims api_handler reads."""
    token = build_expired_jwt()
    payload = _decode_segment(token.split(".")[1])
    assert "sub" in payload
    assert "cognito:groups" in payload
    assert "ciso" in payload["cognito:groups"]


def test_make_jwt_without_exp_omits_field() -> None:
    """If `exp` isn't passed, the payload must not include the key."""
    token = make_jwt()
    payload = _decode_segment(token.split(".")[1])
    assert "exp" not in payload


def test_make_jwt_extra_fields_round_trip() -> None:
    """`extra=` dict must be merged into the payload."""
    token = make_jwt(extra={"iss": "test-issuer", "aud": "test-aud"})
    payload = _decode_segment(token.split(".")[1])
    assert payload["iss"] == "test-issuer"
    assert payload["aud"] == "test-aud"


# ────────────────────────── response classification ──────────────────────────


@pytest.mark.parametrize(
    ("status_code", "expected_status", "expected_severity"),
    [
        (200, "fail", SEVERITY_PRIV_ESC_HIGH),  # 2xx => privilege esc
        (201, "fail", SEVERITY_PRIV_ESC_HIGH),
        (299, "fail", SEVERITY_PRIV_ESC_HIGH),
        (401, "pass", None),  # rejected
        (403, "pass", None),  # rejected
        (400, "fail", SEVERITY_API_CRASH_MEDIUM),  # other 4xx unexpected
        (404, "fail", SEVERITY_API_CRASH_MEDIUM),
        (500, "fail", SEVERITY_API_CRASH_MEDIUM),
        (503, "fail", SEVERITY_API_CRASH_MEDIUM),
    ],
)
def test_classify_cross_persona_response(
    status_code: int, expected_status: str, expected_severity: str | None
) -> None:
    """Per the task-14 prompt's classification table."""
    status, severity = classify_cross_persona_response(status_code)
    assert status == expected_status
    assert severity == expected_severity


def test_faked_2xx_on_blocked_route_is_fail_high() -> None:
    """The task-14 prompt's explicit acceptance bullet — re-asserted here."""
    status, severity = classify_cross_persona_response(200)
    assert status == "fail"
    assert severity == "high"


def test_faked_403_on_blocked_route_is_pass() -> None:
    """The task-14 prompt's explicit acceptance bullet — re-asserted here."""
    status, severity = classify_cross_persona_response(403)
    assert status == "pass"
    assert severity is None


def test_faked_5xx_on_blocked_route_is_fail_medium() -> None:
    """The task-14 prompt's explicit acceptance bullet — re-asserted here."""
    status, severity = classify_cross_persona_response(500)
    assert status == "fail"
    assert severity == "medium"


@pytest.mark.parametrize(
    ("status_code", "expected_status", "expected_severity"),
    [
        (401, "pass", None),
        (403, "pass", None),
        (200, "fail", SEVERITY_EXPIRED_ACCEPTED_HIGH),
        (500, "fail", SEVERITY_API_CRASH_MEDIUM),
        (502, "fail", SEVERITY_API_CRASH_MEDIUM),
        (418, "fail", SEVERITY_API_CRASH_MEDIUM),
    ],
)
def test_classify_expired_token_response(
    status_code: int, expected_status: str, expected_severity: str | None
) -> None:
    status, severity = classify_expired_token_response(status_code)
    assert status == expected_status
    assert severity == expected_severity


# ──────────────────────────── path materialisation ───────────────────────────


@pytest.mark.parametrize(
    ("input_path", "expected"),
    [
        ("/findings", "/findings"),
        ("/findings/{conflict_id}", "/findings/harness-probe"),
        (
            "/conversations/{session_id}/messages",
            "/conversations/harness-probe/messages",
        ),
        (
            "/actions/{cr_id}/approve",
            "/actions/harness-probe/approve",
        ),
        ("/scan-runs/{scan_run_id}", "/scan-runs/harness-probe"),
    ],
)
def test_materialise_path(input_path: str, expected: str) -> None:
    assert materialise_path(input_path) == expected


# ────────────────────────────── results writer ───────────────────────────────


def test_auth_results_path_respects_run_dir(tmp_path: Path) -> None:
    """`RUN_DIR` should anchor the auth results.json under <run>/auth/."""
    p = auth_results_path(tmp_path)
    assert p == tmp_path / "auth" / "results.json"


def test_auth_results_path_falls_back_to_local() -> None:
    """Without RUN_DIR, results land under `test-reports/_local/auth/`."""
    p = auth_results_path(None)
    # We don't assert the absolute path (CI vs local diverge) but we DO
    # assert the trailing structure.
    assert p.name == "results.json"
    assert p.parent.name == "auth"
    assert p.parent.parent.name == "_local"
    assert p.parent.parent.parent.name == "test-reports"


def test_results_writer_round_trips(tmp_path: Path) -> None:
    """Writer must produce a valid JSON list, sorted by test_id."""
    writer = AuthResultsWriter()
    writer.record(
        {
            "test_id": "auth.zzz.last",
            "status": "pass",
            "layer": "auth",
            "target_kind": "api_route",
            "target_id": "get-findings",
            "persona": "ciso",
            "duration_seconds": 0.1,
        }
    )
    writer.record(
        {
            "test_id": "auth.token-usage.soc-forbidden",
            "status": "fail",
            "layer": "auth",
            "target_kind": "api_route",
            "target_id": "get-token-usage",
            "persona": "soc",
            "severity": "high",
            "evidence_path": "auth/results.json#auth.token-usage.soc-forbidden",
            "duration_seconds": 0.2,
        }
    )
    out_path = tmp_path / "auth" / "results.json"
    writer.write(out_path)

    assert out_path.exists()
    parsed = json.loads(out_path.read_text(encoding="utf-8"))
    assert isinstance(parsed, list)
    assert len(parsed) == 2
    # Sorted alphabetically by test_id.
    assert parsed[0]["test_id"] == "auth.token-usage.soc-forbidden"
    assert parsed[1]["test_id"] == "auth.zzz.last"
    assert parsed[0]["severity"] == "high"


def test_results_writer_round_trips_through_builder(tmp_path: Path) -> None:
    """End-to-end: writer output must be readable by `load_results`/`build_matrix`.

    Picks the AC9 pair so we exercise the exact row the spec mandates.
    """
    from src.coverage.builder import build_matrix, load_results

    writer = AuthResultsWriter()
    writer.record(
        {
            "test_id": "auth.token-usage.soc-forbidden",
            "status": "fail",
            "layer": "auth",
            "target_kind": "api_route",
            "target_id": "get-token-usage",
            "persona": "soc",
            "severity": "high",
            "evidence_path": "auth/results.json#auth.token-usage.soc-forbidden",
            "duration_seconds": 0.42,
        }
    )
    writer.write(tmp_path / "auth" / "results.json")

    results = load_results(tmp_path)
    assert len(results) == 1
    assert results[0].test_id == "auth.token-usage.soc-forbidden"
    assert results[0].severity == "high"

    matrix = build_matrix(manifest(), results)
    # One auth row landed under the route in matrix.api_routes.
    route_cells = matrix.api_routes["get-token-usage"]
    assert len(route_cells) == 1
    assert route_cells[0]["status"] == "fail"
    assert route_cells[0]["severity"] == "high"
    assert matrix.summary["failures"] == 1


# ───────────────────── AC11: auth.chat.no-signature probe ────────────────────


def test_chat_no_signature_canonical_test_id() -> None:
    """AC11 spells the literal id; it must not drift."""
    assert CHAT_NO_SIG_TEST_ID == "auth.chat.no-signature"


def test_chat_no_signature_target_id_is_post_chat() -> None:
    """The result row must point at the manifest's `post-chat` route id."""
    assert CHAT_NO_SIG_TARGET_ID == "post-chat"
    # Sanity-check that the manifest still carries `post-chat`.
    route_ids = {r["id"] for r in api_routes()}
    assert "post-chat" in route_ids


def test_chat_no_signature_prompt_has_harness_tag() -> None:
    """Spec §11 requires `[harness]` prefix on chat probes for audit-log triage."""
    assert CHAT_NO_SIG_PROMPT.startswith("[harness]")


def test_strip_jwt_signature_produces_header_payload_empty_third_segment() -> None:
    """`header.payload.` — the third segment must be the empty string after stripping."""
    token = make_jwt(sub="probe", groups=["ciso"])
    stripped = strip_jwt_signature(token)
    parts = stripped.split(".")
    assert len(parts) == 3, f"expected 3 segments, got {len(parts)}: {stripped!r}"
    # The header and payload survive verbatim.
    original_parts = token.split(".")
    assert parts[0] == original_parts[0]
    assert parts[1] == original_parts[1]
    # The signature is the empty string.
    assert parts[2] == ""
    # The token must still end in a trailing dot (the AC11 stripped shape).
    assert stripped.endswith(".")


def test_strip_jwt_signature_rejects_malformed_jwt() -> None:
    """Anything other than a 3-segment dot-string is a ValueError."""
    with pytest.raises(ValueError, match="3-segment"):
        strip_jwt_signature("not-a-jwt")
    with pytest.raises(ValueError, match="3-segment"):
        strip_jwt_signature("only.two")
    with pytest.raises(ValueError, match="3-segment"):
        strip_jwt_signature("four.segments.are.too.many")


@pytest.mark.parametrize(
    ("status_code", "expected_status", "expected_severity"),
    [
        # 2xx — current behaviour, regression detector idle.
        (200, "documented_unsafe", "info"),
        (201, "documented_unsafe", "info"),
        (299, "documented_unsafe", "info"),
        # 401/403 — regression direction (the platform tightened).
        (401, "fail", "medium"),
        (403, "fail", "medium"),
        # 5xx — API crash on malformed JWT.
        (500, "fail", "medium"),
        (502, "fail", "medium"),
        (503, "fail", "medium"),
        # Other 4xx — unexpected, also a fail.
        (400, "fail", "medium"),
        (404, "fail", "medium"),
    ],
)
def test_classify_chat_no_signature_response(
    status_code: int, expected_status: str, expected_severity: str | None
) -> None:
    """Per AC11's status → outcome table."""
    status, severity = classify_chat_no_signature_response(status_code)
    assert status == expected_status
    assert severity == expected_severity


def test_faked_200_chat_no_signature_is_documented_unsafe_info() -> None:
    """Explicit AC11 bullet — 200 is the current behaviour, not a finding."""
    status, severity = classify_chat_no_signature_response(200)
    assert status == "documented_unsafe"
    assert severity == "info"


def test_faked_401_chat_no_signature_is_fail_medium() -> None:
    """Explicit AC11 bullet — 401 is the regression direction."""
    status, severity = classify_chat_no_signature_response(401)
    assert status == "fail"
    assert severity == "medium"


def test_faked_500_chat_no_signature_is_fail_medium() -> None:
    """Explicit AC11 bullet — 5xx is an API crash, still a fail."""
    status, severity = classify_chat_no_signature_response(500)
    assert status == "fail"
    assert severity == "medium"


def test_documented_unsafe_status_round_trips_through_builder(tmp_path: Path) -> None:
    """Emit a faked documented_unsafe row; build_matrix must accept it.

    Asserts that:
      * the row survives the writer -> load_results -> build_matrix pipeline
        without a validation error (DOCUMENTED_UNSAFE is a valid CellStatus,
        and a doc-unsafe row has no evidence_path requirement);
      * the summary tallies it on its own line (NOT in `failures`).
    """
    from src.coverage.builder import CellStatus, build_matrix, load_results

    writer = AuthResultsWriter()
    writer.record(
        {
            "test_id": CHAT_NO_SIG_TEST_ID,
            "status": "documented_unsafe",
            "layer": "auth",
            "target_kind": "api_route",
            "target_id": CHAT_NO_SIG_TARGET_ID,
            "persona": "ciso",
            "severity": "info",
            "evidence_path": f"auth/results.json#{CHAT_NO_SIG_TEST_ID}",
            "duration_seconds": 0.13,
        }
    )
    writer.write(tmp_path / "auth" / "results.json")

    results = load_results(tmp_path)
    assert len(results) == 1
    assert results[0].test_id == CHAT_NO_SIG_TEST_ID
    # Round-trips back into the CellStatus enum (str-based so equality holds).
    assert results[0].status == CellStatus.DOCUMENTED_UNSAFE
    assert results[0].status == "documented_unsafe"

    matrix = build_matrix(manifest(), results)
    route_cells = matrix.api_routes[CHAT_NO_SIG_TARGET_ID]
    assert len(route_cells) == 1
    assert route_cells[0]["status"] == "documented_unsafe"

    # AC11: documented_unsafe MUST be counted on its own line, NOT folded
    # into failures.
    assert matrix.summary["failures"] == 0
    assert matrix.summary["documented_unsafe"] == 1


def test_cellstatus_documented_unsafe_member_exists() -> None:
    """Defensive — task 7 added this enum member; confirm it's still there."""
    from src.coverage.builder import CellStatus

    assert hasattr(CellStatus, "DOCUMENTED_UNSAFE")
    assert CellStatus.DOCUMENTED_UNSAFE.value == "documented_unsafe"


def test_chat_no_signature_uses_make_jwt_compatible_token() -> None:
    """The stripped-signature path must accept a token built by `make_jwt`.

    Round-trip: build → strip → assert decode-able middle segment. This
    matches what the lambda's `_caller_claims` does to the token in
    production — the middle segment must still be valid base64url JSON
    after the signature is dropped.
    """
    token = make_jwt(sub="ciso", groups=["ciso"], extra={"iss": "test"})
    stripped = strip_jwt_signature(token)
    payload_seg = stripped.split(".")[1]
    # Pad and decode like api_handler._caller_claims does.
    payload_seg = payload_seg + "=" * (-len(payload_seg) % 4)
    payload = json.loads(base64.urlsafe_b64decode(payload_seg))
    assert payload["sub"] == "ciso"
    assert payload["cognito:groups"] == ["ciso"]
    assert payload["iss"] == "test"


# ───────────────── task 16: forged cognito:groups probes ─────────────────────


def test_forge_cognito_groups_round_trips() -> None:
    """Forged token decodes back to the new groups list."""
    original = make_jwt(sub="soc-user", groups=["soc"])
    forged = forge_cognito_groups(original, ["ciso"])

    parts = forged.split(".")
    assert len(parts) == 3
    payload = _decode_segment(parts[1])
    assert payload["cognito:groups"] == ["ciso"]
    # Other claims survive verbatim.
    assert payload["sub"] == "soc-user"
    assert payload["cognito:username"] == "soc-user"


def test_forge_cognito_groups_preserves_signature_segment() -> None:
    """The signature segment must be byte-identical after forgery."""
    original = make_jwt(sub="grc-user", groups=["grc"])
    original_sig = original.split(".")[2]

    forged = forge_cognito_groups(original, ["ciso"])
    forged_sig = forged.split(".")[2]

    assert forged_sig == original_sig
    # The header is also untouched.
    assert forged.split(".")[0] == original.split(".")[0]


def test_forge_cognito_groups_preserves_other_payload_fields() -> None:
    """Forgery must NOT clobber other claims (sub, iss, aud, exp, ...)."""
    original = make_jwt(
        sub="employee-user",
        groups=["employee"],
        exp=1234567890,
        extra={"iss": "real-issuer", "aud": "real-aud", "token_use": "id"},
    )
    forged = forge_cognito_groups(original, ["ciso"])
    payload = _decode_segment(forged.split(".")[1])

    assert payload["sub"] == "employee-user"
    assert payload["iss"] == "real-issuer"
    assert payload["aud"] == "real-aud"
    assert payload["token_use"] == "id"
    assert payload["exp"] == 1234567890
    assert payload["cognito:groups"] == ["ciso"]


def test_forge_cognito_groups_accepts_multi_value_lists() -> None:
    """Forgery supports lateral escalation (adding a group, not replacing)."""
    original = make_jwt(sub="employee-user", groups=["employee"])
    forged = forge_cognito_groups(original, ["employee", "soc"])
    payload = _decode_segment(forged.split(".")[1])
    assert payload["cognito:groups"] == ["employee", "soc"]


def test_forge_cognito_groups_rejects_malformed_jwt() -> None:
    """Anything other than a 3-segment dot-string is a ValueError."""
    with pytest.raises(ValueError, match="3-segment"):
        forge_cognito_groups("not-a-jwt", ["ciso"])
    with pytest.raises(ValueError, match="3-segment"):
        forge_cognito_groups("only.two", ["ciso"])
    with pytest.raises(ValueError, match="3-segment"):
        forge_cognito_groups("four.segments.are.too.many", ["ciso"])


def test_forged_groups_enumeration_count_is_ten() -> None:
    """9 upward escalation (3 routes x 3 personas) + 1 lateral = 10."""
    assert len(FORGED_GROUPS_UPWARD_PAIRS) == 9
    assert len(FORGED_GROUPS_PAIRS) == 10
    assert len(FORGED_GROUPS_TEST_IDS) == 10
    # Ids are unique.
    assert len(set(FORGED_GROUPS_TEST_IDS)) == 10


def test_forged_groups_canonical_token_usage_soc_id_present() -> None:
    """Task 16 prompt explicitly names this id."""
    assert "auth.token-usage.forged-soc-to-ciso" in FORGED_GROUPS_TEST_IDS


def test_forged_groups_covers_all_three_ciso_only_routes() -> None:
    """One pair set per CISO-only route, in the order spelled by the prompt."""
    route_ids_by_pair = {pair[0]["id"] for pair in FORGED_GROUPS_UPWARD_PAIRS}
    assert route_ids_by_pair == {
        "get-token-usage",
        "get-token-usage-summary",
        "post-action-approve",
    }


def test_forged_groups_covers_all_three_non_ciso_personas_per_route() -> None:
    """Each CISO-only route is probed from soc, grc, and employee."""
    from collections import defaultdict

    by_route: dict[str, set[str]] = defaultdict(set)
    for (
        route,
        original_persona,
        _forged_groups,
        _forged_persona,
        _test_id,
    ) in FORGED_GROUPS_UPWARD_PAIRS:
        by_route[route["id"]].add(original_persona)
    for route_id, originals in by_route.items():
        assert originals == {"soc", "grc", "employee"}, (
            f"route {route_id!r} missing non-CISO personas: {originals}"
        )


def test_forged_groups_upward_pairs_target_ciso_impersonation() -> None:
    """Every upward pair must forge groups to ``["ciso"]`` exactly."""
    for (
        route,
        _original,
        forged_groups,
        forged_persona,
        _test_id,
    ) in FORGED_GROUPS_UPWARD_PAIRS:
        assert forged_groups == ["ciso"], (
            f"upward pair for {route['id']!r} has unexpected forged_groups={forged_groups!r}"
        )
        assert forged_persona == "ciso"


def test_forged_groups_lateral_pair_targets_token_usage_and_soc() -> None:
    """The lateral sanity test forges employee->soc against /token-usage.

    C1 fix: the original wiring pointed at /findings, which is universally
    accessible (all 4 personas in accessible_to). A 2xx from /findings is
    the expected contract behaviour, so that test always reported a
    false-positive HIGH-severity escalation. Re-pointed at /token-usage
    (CISO-only via _require_ciso) so a 2xx is a real escalation signal.

    The forged groups deliberately exclude `ciso` so this test probes a
    different gate behaviour than the upward escalation ids — does
    _require_ciso check for `ciso` specifically, or accept any non-default
    group as privileged?
    """
    route, original_persona, forged_groups, forged_persona, test_id = (
        FORGED_GROUPS_LATERAL_PAIR
    )
    assert route["id"] == "get-token-usage"
    # Sanity-check the manifest property the test relies on: the route must
    # gate employee out, or the lateral test isn't load-bearing.
    assert "employee" not in route.get("accessible_to", [])
    assert original_persona == "employee"
    assert forged_groups == ["employee", "soc"]
    # The forge MUST NOT include ciso — the whole point of the lateral test
    # is to probe "does the gate accept any non-default group?" not "does the
    # gate accept a forged ciso claim?" (which is the upward path's job).
    assert "ciso" not in forged_groups
    assert forged_persona == "soc"
    assert test_id == "auth.token-usage.forged-employee-add-soc-claim"


@pytest.mark.parametrize(
    ("status_code", "expected_status", "expected_severity"),
    [
        (200, "fail", SEVERITY_FORGED_PRIV_ESC_HIGH),  # 2xx => priv esc
        (201, "fail", SEVERITY_FORGED_PRIV_ESC_HIGH),
        (299, "fail", SEVERITY_FORGED_PRIV_ESC_HIGH),
        (401, "pass", None),  # rejected
        (403, "pass", None),  # rejected
        (400, "fail", SEVERITY_FORGED_API_CRASH_MEDIUM),  # other 4xx
        (404, "fail", SEVERITY_FORGED_API_CRASH_MEDIUM),
        (500, "fail", SEVERITY_FORGED_API_CRASH_MEDIUM),
        (503, "fail", SEVERITY_FORGED_API_CRASH_MEDIUM),
    ],
)
def test_classify_forged_groups_response(
    status_code: int, expected_status: str, expected_severity: str | None
) -> None:
    """Per the task-16 prompt's classification table."""
    status, severity = classify_forged_groups_response(status_code)
    assert status == expected_status
    assert severity == expected_severity


def test_faked_200_on_forged_token_is_fail_high() -> None:
    """Explicit task-16 acceptance bullet: 200 = privilege escalation."""
    status, severity = classify_forged_groups_response(200)
    assert status == "fail"
    assert severity == "high"


def test_faked_403_on_forged_token_is_pass() -> None:
    """Explicit task-16 acceptance bullet: 403 = API correctly rejected."""
    status, severity = classify_forged_groups_response(403)
    assert status == "pass"
    assert severity is None


def test_faked_500_on_forged_token_is_fail_medium() -> None:
    """Explicit task-16 acceptance bullet: 5xx = API crash on forged token."""
    status, severity = classify_forged_groups_response(500)
    assert status == "fail"
    assert severity == "medium"


def test_forged_groups_unknown_persona_in_pair_is_a_real_persona() -> None:
    """All original personas in the pair list must be valid Persona ids."""
    valid = set(persona_ids())
    for _route, original, _groups, _forged, _test_id in FORGED_GROUPS_PAIRS:
        assert original in valid, (
            f"forged-groups pair references unknown persona {original!r}"
        )
