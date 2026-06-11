"""Validates src/coverage/manifest.json against the task-5 contract.

These tests do not hit the network and do not import the harness modules
(beyond `json` + `pathlib`). They guard the hand-curated source-of-truth
against structural rot: missing keys, broken file refs, duplicate ids,
and persona union mismatches.

The drift-checker (`scripts/check_manifest_drift.py`, task 6) layers on
top of this — it catches added/removed surfaces in the source tree. These
tests catch self-consistency bugs in the manifest itself.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


# Locations.
_HARNESS_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _HARNESS_ROOT.parent
_MANIFEST_PATH = _HARNESS_ROOT / "src" / "coverage" / "manifest.json"

# The 4 personas the project pins. Sourced from CLAUDE.local.md "Cognito persona
# binding" and ui/src/contexts/PersonaContext.jsx. Anything else is a regression.
_EXPECTED_PERSONA_IDS = {"ciso", "soc", "grc", "employee"}

# jira_specialist source is not in this repo (CLAUDE.md off-limits list). Its
# agent_tools entry references a file path under agents/jira_specialist/agent.py
# that intentionally does not exist on disk; we skip the file-exists check for it.
_OFF_REPO_AGENTS = {"jira_specialist"}

# Synthetic agent_tools entries are harness-only sentinels (e.g.
# `master.chat_surface` for the LLM red-team layer). They carry `synthetic: true`
# and have `file: null` because they don't map to a real source tree symbol. The
# drift-checker (scripts/check_manifest_drift.py) ignores them; manifest
# self-consistency checks skip the file-existence assertion for them too.


@pytest.fixture(scope="module")
def manifest() -> dict:
    """Parse the manifest once per module run. JSON-parse errors fail loudly."""
    raw = _MANIFEST_PATH.read_text(encoding="utf-8")
    return json.loads(raw)


# ──────────────────────────── Top-level shape ─────────────────────────────


def test_manifest_file_exists():
    assert _MANIFEST_PATH.is_file(), (
        f"manifest missing at {_MANIFEST_PATH} — task 5 was supposed to create it"
    )


def test_manifest_is_valid_json(manifest):
    """Fixture would have already raised on bad JSON; this asserts a dict shape."""
    assert isinstance(manifest, dict), "top-level manifest must be a JSON object"


def test_schema_version_is_one_dot_zero_dot_zero(manifest):
    assert manifest.get("schema_version") == "1.0.0", (
        "schema_version must be '1.0.0' per task-5 prompt; "
        "bump only when the shape changes incompatibly"
    )


def test_generated_at_is_null(manifest):
    """Hand-curated file always has generated_at=null; only the auto-generated
    copy (if/when we add one) sets it to a timestamp."""
    assert manifest.get("generated_at") is None


def test_top_level_keys_present(manifest):
    for key in ("personas", "pages", "api_routes", "agent_tools"):
        assert key in manifest, f"manifest must have top-level '{key}' array"
        assert isinstance(manifest[key], list), f"'{key}' must be a list"


# ──────────────────────────── Personas ────────────────────────────────────


def test_personas_count_is_four(manifest):
    assert len(manifest["personas"]) == 4, (
        "exactly 4 personas required (ciso, soc, grc, employee) — see "
        "CLAUDE.local.md persona-binding section"
    )


def test_personas_ids_match_expected_set(manifest):
    ids = {p["id"] for p in manifest["personas"]}
    assert ids == _EXPECTED_PERSONA_IDS, (
        f"persona ids must be exactly {sorted(_EXPECTED_PERSONA_IDS)}; got {sorted(ids)}"
    )


def test_personas_have_required_fields(manifest):
    required = {"id", "username", "cognito_group", "label"}
    for p in manifest["personas"]:
        missing = required - set(p.keys())
        assert not missing, f"persona {p.get('id')} missing fields {missing}"
        for field in required:
            assert isinstance(p[field], str) and p[field], (
                f"persona {p.get('id')} field '{field}' must be a non-empty string"
            )


def test_persona_usernames_match_known_demo_users(manifest):
    """The deployed Cognito pool uses email as UsernameAttribute. Drift here
    means the demo accounts were renamed and the auth helper will fail at
    run-time. Note: CLAUDE.local.md says `ciso_daiana@` but deployed reality
    is `ciso_diana@` (verified 2026-06-09)."""
    expected_usernames = {
        "ciso": "ciso_diana@meridianinsurance.com",
        "soc": "soc_marcus@meridianinsurance.com",
        "grc": "grc_priya@meridianinsurance.com",
        "employee": "emp_sarah@meridianinsurance.com",
    }
    for p in manifest["personas"]:
        assert p["username"] == expected_usernames[p["id"]], (
            f"persona '{p['id']}' username should be '{expected_usernames[p['id']]}', "
            f"got '{p['username']}'"
        )


# ──────────────────────────── Pages ───────────────────────────────────────


def test_pages_have_required_fields(manifest):
    required = {"id", "file", "route", "label", "accessible_to", "blocked_for"}
    for page in manifest["pages"]:
        missing = required - set(page.keys())
        assert not missing, f"page {page.get('id')} missing fields {missing}"


def test_pages_access_union_covers_all_personas(manifest):
    """For every page, accessible_to + blocked_for must partition the 4 personas
    exactly — no missing, no duplicates across the two lists."""
    for page in manifest["pages"]:
        access = set(page["accessible_to"])
        blocked = set(page["blocked_for"])
        union = access | blocked
        overlap = access & blocked
        assert not overlap, (
            f"page {page['id']}: persona(s) {overlap} appear in BOTH accessible_to and blocked_for"
        )
        assert union == _EXPECTED_PERSONA_IDS, (
            f"page {page['id']}: accessible_to + blocked_for must equal "
            f"{sorted(_EXPECTED_PERSONA_IDS)}; got accessible={sorted(access)}, "
            f"blocked={sorted(blocked)}, union={sorted(union)}"
        )


def test_pages_file_paths_exist_on_disk(manifest):
    """Every non-synthetic page must reference a real file. Synthetic pages
    (``synthetic: true``, e.g. the ``spa-root`` sentinel for Block D bundle
    scans) carry ``file: null`` and are skipped — same pattern as the
    ``master.chat_surface`` synthetic entry in ``agent_tools``."""
    for page in manifest["pages"]:
        if page.get("synthetic") is True:
            assert page.get("file") is None, (
                f"synthetic page {page.get('id')} must have 'file': null "
                f"(got {page.get('file')!r})"
            )
            assert "note" in page and page["note"], (
                f"synthetic page {page.get('id')} must carry a 'note' "
                f"field explaining the sentinel"
            )
            continue
        path = _REPO_ROOT / page["file"]
        assert path.is_file(), (
            f"page {page['id']} references {page['file']} which is not a file "
            f"under repo root {_REPO_ROOT}"
        )


def test_synthetic_page_flag_is_bool_when_present(manifest):
    """Like ``synthetic`` on agent_tools, the optional ``synthetic`` flag on
    pages must be a JSON boolean when present (not the string "true"). Pinning
    the type keeps the drift checker's short-circuit unambiguous."""
    for page in manifest["pages"]:
        if "synthetic" in page:
            assert isinstance(page["synthetic"], bool), (
                f"page {page.get('id')} 'synthetic' must be a JSON boolean, "
                f"got {type(page['synthetic']).__name__}"
            )


def test_spa_root_synthetic_page_entry_present(manifest):
    """The ``spa-root`` sentinel is required for Block D bundle-scan rows
    (hardcoded keys, source maps, sensitive comments, SRI, tabnabbing). It
    must exist with ``synthetic: true`` and ``file: null``, and be universally
    accessible (all 4 personas), since the probes hit static CloudFront assets."""
    by_id = {p["id"]: p for p in manifest["pages"]}
    assert "spa-root" in by_id, (
        "manifest.pages must contain a 'spa-root' synthetic sentinel for "
        "Block D bundle scans"
    )
    entry = by_id["spa-root"]
    assert entry.get("synthetic") is True, (
        "spa-root must be flagged 'synthetic': true so the drift checker skips it"
    )
    assert entry.get("file") is None
    assert entry.get("note"), "spa-root must carry a note explaining the sentinel"
    assert set(entry["accessible_to"]) == _EXPECTED_PERSONA_IDS, (
        "spa-root must be accessible to all 4 personas — static-asset probes "
        "are persona-agnostic"
    )
    assert entry["blocked_for"] == []


def test_pages_routes_start_with_slash(manifest):
    for page in manifest["pages"]:
        assert page["route"].startswith("/"), (
            f"page {page['id']} route '{page['route']}' must begin with '/'"
        )


def test_page_ids_are_unique(manifest):
    ids = [page["id"] for page in manifest["pages"]]
    duplicates = [i for i in set(ids) if ids.count(i) > 1]
    assert not duplicates, f"duplicate page ids: {duplicates}"


# ──────────────────────────── API routes ──────────────────────────────────


def test_api_routes_have_required_fields(manifest):
    required = {"id", "method", "path", "file", "auth_required"}
    for route in manifest["api_routes"]:
        missing = required - set(route.keys())
        assert not missing, f"api_route {route.get('id')} missing fields {missing}"


def test_api_route_methods_are_uppercase_known_verbs(manifest):
    known = {"GET", "POST", "PUT", "DELETE", "PATCH"}
    for route in manifest["api_routes"]:
        assert route["method"] in known, (
            f"api_route {route['id']} method '{route['method']}' is not in {known}"
        )


def test_api_route_paths_start_with_slash(manifest):
    """Real api_routes are API Gateway paths and must start with `/`.

    Synthetic entries (``synthetic: true``, e.g. the ``cognito-initiate-auth``
    sentinel for the brute-force test's target_id binding) may carry a full
    URL (https://…) because they point at non-API-Gateway endpoints. They're
    skipped here for that reason.
    """
    for route in manifest["api_routes"]:
        if route.get("synthetic") is True:
            continue
        assert route["path"].startswith("/"), (
            f"api_route {route['id']} path '{route['path']}' must begin with '/'"
        )


def test_api_route_auth_required_is_bool(manifest):
    for route in manifest["api_routes"]:
        assert isinstance(route["auth_required"], bool), (
            f"api_route {route['id']} auth_required must be JSON boolean, "
            f"got {type(route['auth_required']).__name__}"
        )


def test_synthetic_api_route_flag_is_bool_when_present(manifest):
    """``synthetic`` on api_routes mirrors the pages/agent_tools convention:
    optional boolean. When present it must be a JSON boolean so the drift
    checker's ``synthetic is True`` short-circuit stays unambiguous.
    """
    for route in manifest["api_routes"]:
        if "synthetic" in route:
            assert isinstance(route["synthetic"], bool), (
                f"api_route {route.get('id')} 'synthetic' must be a JSON "
                f"boolean, got {type(route['synthetic']).__name__}"
            )


def test_synthetic_api_route_has_null_file_and_note(manifest):
    """Synthetic api_routes must carry ``file: null`` and a ``note`` so a
    future maintainer can tell why the entry exists without a source-tree
    counterpart. Mirror of ``test_agent_tool_file_paths_exist_on_disk``'s
    synthetic-handling contract.
    """
    for route in manifest["api_routes"]:
        if route.get("synthetic") is not True:
            continue
        assert route.get("file") is None, (
            f"synthetic api_route {route.get('id')} must have 'file': null "
            f"(got {route.get('file')!r})"
        )
        assert "note" in route and route["note"], (
            f"synthetic api_route {route.get('id')} must carry a 'note' "
            f"field explaining the sentinel"
        )


def test_api_route_file_paths_exist_on_disk(manifest):
    """Every non-synthetic api_route must reference a real source file.

    Synthetic sentinel entries (``synthetic: true``, ``file: null``) are
    skipped — they exist only to give harness layers a coverage row to
    land on (e.g. ``cognito-initiate-auth`` for the brute-force test).
    """
    for route in manifest["api_routes"]:
        if route.get("synthetic") is True:
            continue
        path = _REPO_ROOT / route["file"]
        assert path.is_file(), (
            f"api_route {route['id']} references {route['file']} which is not a file"
        )


def test_api_route_ids_are_unique(manifest):
    ids = [route["id"] for route in manifest["api_routes"]]
    duplicates = [i for i in set(ids) if ids.count(i) > 1]
    assert not duplicates, f"duplicate api_route ids: {duplicates}"


def test_api_route_method_path_pairs_are_unique(manifest):
    """Two routes can share a path (GET vs DELETE on /conversations/{id}) but a
    (method, path) pair must be unique."""
    seen = set()
    duplicates = []
    for route in manifest["api_routes"]:
        key = (route["method"], route["path"])
        if key in seen:
            duplicates.append(key)
        seen.add(key)
    assert not duplicates, f"duplicate (method, path) pairs: {duplicates}"


# ──────────────────────────── Agent tools ─────────────────────────────────


def test_agent_tools_have_required_fields(manifest):
    """Required keys are `agent`, `tool_name`, and `file` — but `file` may be
    `null` for synthetic sentinel entries (e.g. `master.chat_surface`), which
    carry `synthetic: true` and don't map to a real source-tree symbol."""
    required = {"agent", "tool_name", "file"}
    for tool in manifest["agent_tools"]:
        missing = required - set(tool.keys())
        assert not missing, f"agent_tool {tool.get('id')} missing fields {missing}"


def test_synthetic_flag_is_bool_when_present(manifest):
    """`synthetic` is an optional boolean on agent_tools entries. When present,
    it must be a JSON boolean (not the string "true"); when absent, the entry
    is implicitly non-synthetic. Pinning the type keeps the drift-checker's
    `synthetic is True` short-circuit unambiguous."""
    for tool in manifest["agent_tools"]:
        if "synthetic" in tool:
            assert isinstance(tool["synthetic"], bool), (
                f"agent_tool {tool.get('id')} 'synthetic' must be a JSON "
                f"boolean, got {type(tool['synthetic']).__name__}"
            )


def test_agent_tool_file_paths_exist_on_disk(manifest):
    """All in-repo agents must have their file present. jira_specialist is
    skipped because CLAUDE.md flags its source as not in this repo. Synthetic
    sentinel entries (`synthetic: true`, `file: null`) are also skipped —
    they exist only to give LLM-layer probes a coverage row to land on."""
    for tool in manifest["agent_tools"]:
        if tool.get("synthetic") is True:
            # Synthetic entries must carry a note explaining why they exist.
            assert tool.get("file") is None, (
                f"synthetic agent_tool {tool.get('id')} must have 'file': null "
                f"(got {tool.get('file')!r})"
            )
            assert "note" in tool and tool["note"], (
                f"synthetic agent_tool {tool.get('id')} must carry a 'note' "
                f"field explaining the sentinel"
            )
            continue
        if tool["agent"] in _OFF_REPO_AGENTS:
            # Sanity: the tool entry MUST note this so a future maintainer doesn't
            # think the missing file is a typo.
            assert "note" in tool and tool["note"], (
                f"off-repo agent_tool {tool.get('id')} must carry a 'note' field "
                f"explaining the missing source"
            )
            continue
        path = _REPO_ROOT / tool["file"]
        assert path.is_file(), (
            f"agent_tool {tool.get('id')} references {tool['file']} which is not a file"
        )


def test_master_chat_surface_synthetic_entry_present(manifest):
    """The `master.chat_surface` sentinel is required for the LLM red-team
    coverage rows (curated jailbreaks, generative probes, cost-DoS). It must
    exist with `synthetic: true` and `file: null`, and target the
    `master_orchestrator` runtime."""
    by_id = {t["id"]: t for t in manifest["agent_tools"]}
    assert "master.chat_surface" in by_id, (
        "agent_tools must contain a 'master.chat_surface' sentinel for LLM "
        "red-team coverage"
    )
    entry = by_id["master.chat_surface"]
    assert entry.get("synthetic") is True, (
        "master.chat_surface must be flagged 'synthetic': true so the drift "
        "checker skips it"
    )
    assert entry["agent"] == "master_orchestrator"
    assert entry["tool_name"] == "chat_surface"
    assert entry["file"] is None
    assert entry.get("note"), (
        "master.chat_surface must carry a note explaining the sentinel"
    )


def test_agent_tool_ids_are_unique(manifest):
    ids = [tool["id"] for tool in manifest["agent_tools"]]
    duplicates = [i for i in set(ids) if ids.count(i) > 1]
    assert not duplicates, f"duplicate agent_tool ids: {duplicates}"


def test_agent_tool_agent_names_known(manifest):
    """The agent field must match a runtime id. Catches typos like
    'master_orch' or 'sharepoint'."""
    known_agents = {
        "master_orchestrator",
        "sharepoint_specialist",
        "awsconfig_specialist",
        "zscaler_specialist",
        "jira_specialist",
    }
    for tool in manifest["agent_tools"]:
        assert tool["agent"] in known_agents, (
            f"agent_tool {tool['id']} agent '{tool['agent']}' not in {known_agents}"
        )


# ──────────────────────────── Cross-cutting ───────────────────────────────


def test_no_duplicate_ids_across_pages_routes_tools(manifest):
    """A single id collision across categories would corrupt the diff section."""
    all_ids = (
        [p["id"] for p in manifest["pages"]]
        + [r["id"] for r in manifest["api_routes"]]
        + [t["id"] for t in manifest["agent_tools"]]
    )
    duplicates = [i for i in set(all_ids) if all_ids.count(i) > 1]
    assert not duplicates, f"id collision across pages/routes/tools: {duplicates}"


def test_all_persona_refs_in_pages_are_known(manifest):
    """Every persona id mentioned in any page's accessible_to / blocked_for must
    be one of the 4 known personas. Guards against typos like 'cisso'."""
    for page in manifest["pages"]:
        for persona_id in [*page["accessible_to"], *page["blocked_for"]]:
            assert persona_id in _EXPECTED_PERSONA_IDS, (
                f"page {page['id']}: unknown persona id '{persona_id}' "
                f"(expected one of {sorted(_EXPECTED_PERSONA_IDS)})"
            )


def test_all_persona_refs_in_routes_are_known(manifest):
    for route in manifest["api_routes"]:
        for persona_id in route.get("accessible_to", []):
            assert persona_id in _EXPECTED_PERSONA_IDS, (
                f"api_route {route['id']}: unknown persona id '{persona_id}'"
            )
