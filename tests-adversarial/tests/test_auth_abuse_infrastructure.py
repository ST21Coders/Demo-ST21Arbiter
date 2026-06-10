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


# ───────────────── Block A: auth.default-creds infrastructure ────────────────
#
# The test_default_creds module short-circuits at import time with
# ``pytest.skip(..., allow_module_level=True)`` when DEMO_PASSWORD is unset.
# These unit tests need the symbols regardless of the env, so we set a
# dummy DEMO_PASSWORD before importing the module. This is harmless — none
# of the assertions below actually call Cognito; they only exercise the
# pure-logic helpers (curated wordlist length, classification rules).
import os as _os  # noqa: E402

_os.environ.setdefault("DEMO_PASSWORD", "unit-test-placeholder")

from auth.test_default_creds import (  # noqa: E402
    DEFAULT_CRED_PAIRS,
    classify_default_creds_response,
)


def test_default_creds_enumeration_count_matches_wordlist() -> None:
    """Block A spec: exactly the five curated pairs.

    If the wordlist grows or shrinks, this test will fail loudly so the
    matrix doc can be kept in sync. The five pairs are the SANS-top-default
    subset documented in the test module's header.
    """
    assert len(DEFAULT_CRED_PAIRS) == 5, (
        f"expected 5 curated default-cred pairs, got {len(DEFAULT_CRED_PAIRS)}: "
        f"{DEFAULT_CRED_PAIRS!r}"
    )


def test_default_creds_curated_set_is_the_documented_one() -> None:
    """The exact pairs match the Block A spec.

    The pairs are: admin/admin, admin/password, test/test, arbiter/arbiter,
    demo/demo123.
    """
    expected = {
        ("admin", "admin"),
        ("admin", "password"),
        ("test", "test"),
        ("arbiter", "arbiter"),
        ("demo", "demo123"),
    }
    actual = set(DEFAULT_CRED_PAIRS)
    assert actual == expected, f"curated default-cred pairs drifted: {actual!r}"


def test_default_creds_authentication_success_is_fail_high() -> None:
    """Faked successful auth (no exception) is recorded as FAIL severity HIGH.

    Block A spec: "FAIL = unexpectedly succeeds. Severity HIGH on fail."
    Classifier returns ``(fail, high)`` when error_code is None.
    """
    status, severity = classify_default_creds_response(None)
    assert status == "fail"
    assert severity == "high"


def test_default_creds_not_authorized_exception_is_pass() -> None:
    """Faked NotAuthorizedException is recorded as PASS.

    Block A spec: "PASS = Cognito rejects with NotAuthorizedException."
    """
    status, severity = classify_default_creds_response("NotAuthorizedException")
    assert status == "pass"
    assert severity is None


def test_default_creds_user_not_found_is_also_pass() -> None:
    """Some pools surface UserNotFoundException instead.

    Same outcome from a security PoV: the credential didn't authenticate.
    """
    status, severity = classify_default_creds_response("UserNotFoundException")
    assert status == "pass"
    assert severity is None


def test_default_creds_unexpected_error_code_is_fail_medium() -> None:
    """An unexpected Cognito error is a FAIL medium (operator should look).

    Anything other than the two known-rejection codes lands as MEDIUM —
    not a finding, but worth flagging so a config drift (e.g. the pool
    deleted, the app client revoked) doesn't silently pass.
    """
    status, severity = classify_default_creds_response("InvalidParameterException")
    assert status == "fail"
    assert severity == "medium"


# ───────────────── Block C: auth.idor infrastructure ────────────────
#
# Like test_default_creds, the Block C modules that gate on env vars at
# module-import time need the env var set BEFORE the import. The dummy
# DEMO_PASSWORD set above is sufficient for the idor module (it has no
# import-time env-var check — it gates inside fixtures). The Cognito-based
# modules (brute_force, password_reset, pool_config) gate on
# COGNITO_CLIENT_ID / COGNITO_USER_POOL_ID at import-time, so we
# pre-populate those with placeholders too.

_os.environ.setdefault("COGNITO_CLIENT_ID", "unit-test-client-id")
_os.environ.setdefault("COGNITO_USER_POOL_ID", "us-east-1_unitTestPool")

from auth.test_idor import (  # noqa: E402
    IDOR_DELETE_TEST_IDS,
    IDOR_READ_TEST_IDS,
    SEVERITY_IDOR_DELETE_CRITICAL,
    SEVERITY_IDOR_READ_HIGH,
    classify_idor_delete_response,
    classify_idor_read_response,
)


def test_idor_read_test_ids_are_canonical() -> None:
    """The three read ids cover SOC/GRC/EMPLOYEE as readers, CISO as owner."""
    assert IDOR_READ_TEST_IDS == [
        "auth.idor.conversation-read.ciso-as-soc",
        "auth.idor.conversation-read.ciso-as-grc",
        "auth.idor.conversation-read.ciso-as-employee",
    ]


def test_idor_delete_test_ids_are_canonical() -> None:
    """The three delete ids cover SOC/GRC/EMPLOYEE as deleters."""
    assert IDOR_DELETE_TEST_IDS == [
        "auth.idor.conversation-delete.ciso-as-soc",
        "auth.idor.conversation-delete.ciso-as-grc",
        "auth.idor.conversation-delete.ciso-as-employee",
    ]


@pytest.mark.parametrize(
    ("status_code", "expected_status", "expected_severity"),
    [
        (200, "fail", SEVERITY_IDOR_READ_HIGH),  # leak
        (201, "fail", SEVERITY_IDOR_READ_HIGH),
        (403, "pass", None),
        (404, "pass", None),
        (500, "fail", "medium"),
        (400, "fail", "medium"),
    ],
)
def test_classify_idor_read_response(
    status_code: int, expected_status: str, expected_severity: str | None
) -> None:
    status, severity = classify_idor_read_response(status_code)
    assert status == expected_status
    assert severity == expected_severity


@pytest.mark.parametrize(
    ("status_code", "expected_status", "expected_severity"),
    [
        (200, "fail", SEVERITY_IDOR_DELETE_CRITICAL),  # cross-user delete
        (204, "fail", SEVERITY_IDOR_DELETE_CRITICAL),
        (403, "pass", None),
        (404, "pass", None),
        (500, "fail", "medium"),
    ],
)
def test_classify_idor_delete_response(
    status_code: int, expected_status: str, expected_severity: str | None
) -> None:
    status, severity = classify_idor_delete_response(status_code)
    assert status == expected_status
    assert severity == expected_severity


def test_idor_faked_200_read_is_fail_high() -> None:
    """Explicit Block C bullet — non-owner 200 is HIGH (data disclosure)."""
    status, severity = classify_idor_read_response(200)
    assert status == "fail"
    assert severity == "high"


def test_idor_faked_200_delete_is_fail_critical() -> None:
    """Explicit Block C bullet — non-owner DELETE 200 is CRITICAL."""
    status, severity = classify_idor_delete_response(200)
    assert status == "fail"
    assert severity == "critical"


def test_idor_faked_404_is_pass() -> None:
    """Explicit Block C bullet — 404 (owner-mismatch) is PASS."""
    assert classify_idor_read_response(404) == ("pass", None)
    assert classify_idor_delete_response(404) == ("pass", None)


# ───────────────── Block C: auth.forced-browsing infrastructure ────────────

from auth.test_forced_browsing import (  # noqa: E402
    FORCED_BROWSING_TEST_IDS,
    SEVERITY_FORCED_BROWSING_HIGH,
    SEVERITY_FORCED_BROWSING_LOW,
    SEVERITY_FORCED_BROWSING_MEDIUM,
    classify_forced_browsing_response,
)


def test_forced_browsing_wordlist_covers_canonical_paths() -> None:
    """The Block C spec names these paths; they MUST be in the wordlist."""
    canonical = {
        "auth.forced-browsing.admin",
        "auth.forced-browsing.swagger",
        "auth.forced-browsing.env",
        "auth.forced-browsing.git-config",
        "auth.forced-browsing.metrics",
        "auth.forced-browsing.security-txt",
    }
    missing = canonical - set(FORCED_BROWSING_TEST_IDS)
    assert not missing, f"forced-browsing wordlist missing canonical ids: {missing}"


def test_forced_browsing_test_ids_are_unique() -> None:
    assert len(FORCED_BROWSING_TEST_IDS) == len(set(FORCED_BROWSING_TEST_IDS))


@pytest.mark.parametrize(
    ("path", "status_code", "body_len", "expected_status", "expected_severity"),
    [
        # Empty body 200 — informational LOW.
        ("/admin", 200, 0, "fail", SEVERITY_FORCED_BROWSING_LOW),
        # Non-trivial body 200 — HIGH (data leak).
        ("/admin", 200, 5000, "fail", SEVERITY_FORCED_BROWSING_HIGH),
        # 401/403/404 — PASS.
        ("/admin", 401, 0, "pass", None),
        ("/admin", 403, 0, "pass", None),
        ("/admin", 404, 0, "pass", None),
        # 5xx — MEDIUM.
        ("/admin", 500, 0, "fail", SEVERITY_FORCED_BROWSING_MEDIUM),
        # /.well-known/security.txt 200 — PASS (special case).
        ("/.well-known/security.txt", 200, 5000, "pass", None),
        # Same path 404 — also PASS.
        ("/.well-known/security.txt", 404, 0, "pass", None),
        # Unexpected status — LOW.
        ("/admin", 418, 0, "fail", SEVERITY_FORCED_BROWSING_LOW),
    ],
)
def test_classify_forced_browsing_response(
    path: str,
    status_code: int,
    body_len: int,
    expected_status: str,
    expected_severity: str | None,
) -> None:
    status, severity = classify_forced_browsing_response(path, status_code, body_len)
    assert status == expected_status
    assert severity == expected_severity


def test_forced_browsing_200_with_html_body_is_fail_high() -> None:
    """Explicit Block C bullet."""
    status, severity = classify_forced_browsing_response("/admin", 200, 4096)
    assert status == "fail"
    assert severity == "high"


def test_forced_browsing_security_txt_200_is_pass() -> None:
    """Explicit Block C special case."""
    status, severity = classify_forced_browsing_response(
        "/.well-known/security.txt", 200, 2048
    )
    assert status == "pass"
    assert severity is None


# ───────────────── Block C: auth.brute-force infrastructure ────────────

from auth.test_brute_force import (  # noqa: E402
    SEVERITY_BRUTE_FORCE_HIGH,
    classify_brute_force_attempts,
)


def test_brute_force_throttle_within_window_is_pass() -> None:
    """5 NotAuthorized then 1 LimitExceeded → PASS with K=6."""
    codes = ["NotAuthorizedException"] * 5 + ["LimitExceededException"]
    status, severity, k = classify_brute_force_attempts(codes)
    assert status == "pass"
    assert severity is None
    assert k == 6


def test_brute_force_no_throttle_is_fail_high() -> None:
    """10 NotAuthorized with no throttle → FAIL HIGH."""
    codes = ["NotAuthorizedException"] * 10
    status, severity, k = classify_brute_force_attempts(codes)
    assert status == "fail"
    assert severity == SEVERITY_BRUTE_FORCE_HIGH
    assert k is None


def test_brute_force_unexpected_auth_success_is_fail_high() -> None:
    """A None in the list means auth succeeded — HIGH."""
    codes = ["NotAuthorizedException", None]
    status, severity, k = classify_brute_force_attempts(codes)
    assert status == "fail"
    assert severity == SEVERITY_BRUTE_FORCE_HIGH


def test_brute_force_other_errors_are_fail_medium() -> None:
    """Non-throttle, non-rejected codes are MEDIUM (operator should look)."""
    codes = ["InvalidParameterException", "InvalidParameterException"]
    status, severity, k = classify_brute_force_attempts(codes)
    assert status == "fail"
    assert severity == "medium"


def test_brute_force_throttling_exception_also_counts_as_throttle() -> None:
    """`ThrottlingException` is a Cognito throttle variant."""
    codes = ["NotAuthorizedException", "ThrottlingException"]
    status, severity, k = classify_brute_force_attempts(codes)
    assert status == "pass"
    assert k == 2


# ───────────────── Block C: auth.session-swap infrastructure ────────────

from auth.test_session_swap import (  # noqa: E402
    FIXATION_TEST_ID,
    SEVERITY_SESSION_FIXATION_HIGH,
    STALE_TOKEN_TEST_ID,
    classify_session_fixation_response,
    classify_stale_token_response,
)


def test_session_swap_test_ids_are_canonical() -> None:
    assert FIXATION_TEST_ID == "auth.session-swap.cross-persona-fixation"
    assert STALE_TOKEN_TEST_ID == "auth.session-swap.stale-token-still-works"


@pytest.mark.parametrize(
    ("status_code", "echoed", "requested", "expected_status", "expected_severity"),
    [
        # SOC rejected — PASS.
        (403, None, "abc", "pass", None),
        (401, None, "abc", "pass", None),
        # SOC got a NEW session — PASS.
        (200, "different-id", "abc", "pass", None),
        # SOC's message landed in CISO's session — FAIL HIGH.
        (200, "abc", "abc", "fail", SEVERITY_SESSION_FIXATION_HIGH),
        (201, "abc", "abc", "fail", SEVERITY_SESSION_FIXATION_HIGH),
        # API crashed — MEDIUM.
        (500, None, "abc", "fail", "medium"),
    ],
)
def test_classify_session_fixation_response(
    status_code: int,
    echoed: str | None,
    requested: str,
    expected_status: str,
    expected_severity: str | None,
) -> None:
    status, severity = classify_session_fixation_response(
        status_code, echoed, requested
    )
    assert status == expected_status
    assert severity == expected_severity


@pytest.mark.parametrize(
    ("status_code", "expected_status", "expected_severity"),
    [
        # 200 with stale token — documented_unsafe per AC11.
        (200, "documented_unsafe", "info"),
        (201, "documented_unsafe", "info"),
        # 401/403 with stale token — regression direction, FAIL MEDIUM.
        (401, "fail", "medium"),
        (403, "fail", "medium"),
        # 5xx — FAIL MEDIUM.
        (500, "fail", "medium"),
    ],
)
def test_classify_stale_token_response(
    status_code: int, expected_status: str, expected_severity: str | None
) -> None:
    status, severity = classify_stale_token_response(status_code)
    assert status == expected_status
    assert severity == expected_severity


# ───────────────── Block C: auth.password-reset infrastructure ────────────

from auth.test_password_reset import (  # noqa: E402
    ENUMERATION_TEST_ID,
    RATE_LIMIT_TEST_ID,
    SEVERITY_PASSWORD_RESET_MEDIUM,
    classify_enumeration_responses,
    classify_rate_limit_attempts,
)


def test_password_reset_test_ids_are_canonical() -> None:
    assert ENUMERATION_TEST_ID == "auth.password-reset.enumeration"
    assert RATE_LIMIT_TEST_ID == "auth.password-reset.rate-limit"


def test_password_reset_enumeration_same_outcome_is_pass() -> None:
    """Same error for known + unknown → no enumeration possible."""
    assert classify_enumeration_responses(None, None) == ("pass", None)
    assert classify_enumeration_responses(
        "LimitExceededException", "LimitExceededException"
    ) == ("pass", None)


def test_password_reset_enumeration_different_outcomes_is_fail_medium() -> None:
    """Different error codes for known + unknown → MEDIUM."""
    status, severity = classify_enumeration_responses(None, "UserNotFoundException")
    assert status == "fail"
    assert severity == SEVERITY_PASSWORD_RESET_MEDIUM


def test_password_reset_rate_limit_hit_within_window_is_pass() -> None:
    """K within window → PASS with K recorded."""
    codes = [None, None, "LimitExceededException"]
    status, severity, k = classify_rate_limit_attempts(codes)
    assert status == "pass"
    assert k == 3


def test_password_reset_rate_limit_no_throttle_is_fail_medium() -> None:
    """No throttle → FAIL MEDIUM."""
    codes = [None] * 5
    status, severity, k = classify_rate_limit_attempts(codes)
    assert status == "fail"
    assert severity == SEVERITY_PASSWORD_RESET_MEDIUM
    assert k is None


# ───────────────── Block C: auth.pool-config infrastructure ────────────

from auth.test_pool_config import (  # noqa: E402
    ADMIN_CREATE_TEST_ID,
    ALL_POOL_CONFIG_TEST_IDS,
    MIN_LENGTH_TEST_ID,
    RECOVERY_TEST_ID,
    REQ_LOWER_TEST_ID,
    REQ_NUMBERS_TEST_ID,
    REQ_SYMBOLS_TEST_ID,
    REQ_UPPER_TEST_ID,
    SEVERITY_POOL_CONFIG_LOW,
    SEVERITY_POOL_CONFIG_MEDIUM,
    TEMP_PASSWORD_TEST_ID,
    classify_account_recovery,
    classify_admin_create_only,
    classify_minimum_length,
    classify_require_flag,
    classify_temp_password_validity,
)


def test_pool_config_all_test_ids_present() -> None:
    """The 8 canonical pool-config test ids must all be enumerated."""
    expected = {
        MIN_LENGTH_TEST_ID,
        REQ_UPPER_TEST_ID,
        REQ_LOWER_TEST_ID,
        REQ_NUMBERS_TEST_ID,
        REQ_SYMBOLS_TEST_ID,
        TEMP_PASSWORD_TEST_ID,
        RECOVERY_TEST_ID,
        ADMIN_CREATE_TEST_ID,
    }
    assert set(ALL_POOL_CONFIG_TEST_IDS) == expected


def test_pool_minimum_length_at_12_is_pass() -> None:
    """Explicit Block C bullet — 12 is the NIST floor."""
    assert classify_minimum_length(12) == ("pass", None)
    assert classify_minimum_length(20) == ("pass", None)


def test_pool_minimum_length_below_12_is_fail_medium() -> None:
    """Explicit Block C bullet — 8 is below the NIST floor."""
    status, severity = classify_minimum_length(8)
    assert status == "fail"
    assert severity == SEVERITY_POOL_CONFIG_MEDIUM


def test_pool_minimum_length_missing_is_fail_medium() -> None:
    """A missing MinimumLength is treated as if it's below threshold."""
    status, severity = classify_minimum_length(None)
    assert status == "fail"
    assert severity == SEVERITY_POOL_CONFIG_MEDIUM


def test_pool_require_flag_true_is_pass() -> None:
    assert classify_require_flag(True) == ("pass", None)


@pytest.mark.parametrize("value", [False, None])
def test_pool_require_flag_falsey_is_fail_low(value: bool | None) -> None:
    status, severity = classify_require_flag(value)
    assert status == "fail"
    assert severity == SEVERITY_POOL_CONFIG_LOW


def test_pool_temp_password_at_7_is_pass() -> None:
    assert classify_temp_password_validity(7) == ("pass", None)
    assert classify_temp_password_validity(1) == ("pass", None)


def test_pool_temp_password_above_7_is_fail_low() -> None:
    status, severity = classify_temp_password_validity(30)
    assert status == "fail"
    assert severity == SEVERITY_POOL_CONFIG_LOW


def test_pool_account_recovery_set_is_pass() -> None:
    """A non-empty dict means recovery is configured."""
    status, severity = classify_account_recovery(
        {"RecoveryMechanisms": [{"Name": "verified_email", "Priority": 1}]}
    )
    assert status == "pass"
    assert severity is None


@pytest.mark.parametrize("value", [None, {}, "not-a-dict"])
def test_pool_account_recovery_missing_is_fail_medium(value: object) -> None:
    status, severity = classify_account_recovery(value)
    assert status == "fail"
    assert severity == SEVERITY_POOL_CONFIG_MEDIUM


def test_pool_admin_create_only_true_is_pass() -> None:
    assert classify_admin_create_only(True) == ("pass", None)


@pytest.mark.parametrize("value", [False, None])
def test_pool_admin_create_only_falsey_is_fail_low(value: bool | None) -> None:
    status, severity = classify_admin_create_only(value)
    assert status == "fail"
    assert severity == SEVERITY_POOL_CONFIG_LOW
