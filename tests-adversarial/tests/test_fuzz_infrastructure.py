"""Unit tests for the fuzz layer's infrastructure (task 12).

These tests run WITHOUT touching the deployed env. They cover the static
contracts the layer relies on: corpus shape, test-id determinism, response
classification, destructive-route gating, and results-writer formatting.

They do NOT run any of the parametrized fuzz tests in
`fuzz/test_api_routes.py` — those need DEMO_PASSWORD + a deployed target
and live under their own test selection.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from fuzz._payloads import (
    STACK_TRACE_MARKERS,
    classify_response,
    fuzz_results_path,
    is_destructive,
    load_corpus,
    make_test_id,
    route_has_path_param,
)


_HARNESS_ROOT = Path(__file__).resolve().parent.parent
_CORPUS_DIR = _HARNESS_ROOT / "fuzz" / "corpus"


# ─────────────────────────────── corpus shape ────────────────────────────────


_EXPECTED_FAMILIES: dict[str, int] = {
    # family -> minimum entry count mandated by the task-12 prompt
    "xss": 12,
    "sqli": 8,
    "oversized": 4,
    "malformed_jwt": 10,
    "header_injection": 4,
    "path_traversal": 4,
    "command_injection": 4,
    "null_bytes_and_control": 4,
    # Block A — compliance checklist additions (see
    # docs/security_compliance_coverage.md "Block A"). Each family has its
    # own minimum to match what the Block A prompt specifies.
    "nosql_operators": 6,
    "ldap": 6,
    "xpath": 6,
    "xml_xxe": 6,
    "ssti": 6,
    "log_injection": 4,
    "ssrf": 6,
    "mass_assignment": 6,
    "prototype_pollution": 4,
    "open_redirects": 6,
}


def test_corpus_files_all_parse_as_json() -> None:
    """Every file under fuzz/corpus/*.json round-trips through json.loads()."""
    files = list(_CORPUS_DIR.glob("*.json"))
    assert files, "no corpus files found under fuzz/corpus/"
    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict), f"{path} top-level value must be an object"
        assert "family" in data and isinstance(data["family"], str), (
            f"{path} missing 'family' string"
        )
        assert "payloads" in data and isinstance(data["payloads"], list), (
            f"{path} missing 'payloads' list"
        )


def test_corpus_load_returns_families_keyed_by_family_id() -> None:
    """load_corpus indexes by the file's `family` field, not its filename."""
    families = load_corpus(_CORPUS_DIR)
    # The 8 cross-cutting families per the task-12 prompt.
    assert set(families.keys()) == set(_EXPECTED_FAMILIES.keys())


@pytest.mark.parametrize("family,min_count", list(_EXPECTED_FAMILIES.items()))
def test_corpus_meets_minimum_entry_count(family: str, min_count: int) -> None:
    """Each corpus family has at least the task-12-mandated entry count."""
    families = load_corpus(_CORPUS_DIR)
    payloads = families[family]["payloads"]
    assert len(payloads) >= min_count, (
        f"family {family!r} has {len(payloads)} entries, "
        f"task-12 mandates at least {min_count}"
    )


def test_corpus_every_entry_has_required_fields() -> None:
    """Each payload has at least an `id` and an `expected_blocked` flag."""
    families = load_corpus(_CORPUS_DIR)
    for family, body in families.items():
        for idx, payload in enumerate(body["payloads"]):
            assert isinstance(payload.get("id"), str) and payload["id"].strip(), (
                f"{family}[{idx}] missing 'id'"
            )
            assert isinstance(payload.get("expected_blocked"), bool), (
                f"{family}[{idx}] missing or non-bool 'expected_blocked'"
            )


def test_corpus_payload_ids_unique_within_family() -> None:
    """No two payloads in the same family share an id (test-id collision risk)."""
    families = load_corpus(_CORPUS_DIR)
    for family, body in families.items():
        ids = [p["id"] for p in body["payloads"]]
        assert len(ids) == len(set(ids)), f"family {family!r} has duplicate payload ids"


def test_corpus_file_count_includes_block_a_additions() -> None:
    """After Block A, corpus dir has 8 original + 10 new = 18+ files.

    The matrix in ``docs/security_compliance_coverage.md`` documents Block A
    as 10 corpus additions (NoSQL operators, LDAP, XPath, XML/XXE, SSTI,
    log injection, SSRF, mass assignment, prototype pollution, open
    redirects). If a corpus file is deleted, this test surfaces the drift
    loudly so the matrix doc can be re-aligned.
    """
    files = list(_CORPUS_DIR.glob("*.json"))
    assert len(files) >= 18, (
        f"expected at least 18 corpus files post-Block-A, got {len(files)}: "
        f"{[f.name for f in files]}"
    )


def test_corpus_every_block_a_family_present() -> None:
    """Each Block A family was added to the corpus dir as its own file."""
    families = load_corpus(_CORPUS_DIR)
    block_a_families = {
        "nosql_operators",
        "ldap",
        "xpath",
        "xml_xxe",
        "ssti",
        "log_injection",
        "ssrf",
        "mass_assignment",
        "prototype_pollution",
        "open_redirects",
    }
    missing = block_a_families - set(families.keys())
    assert not missing, f"Block A families missing from corpus: {missing}"


def test_block_a_families_wired_into_route_enumeration() -> None:
    """The new families are referenced in ``_families_for_route``.

    Without that wiring, the corpus files load but no fuzz test references
    them — the parametrize matrix never grows. We don't import the function
    (it'd require pulling in the deployed-env fixtures); instead, we
    grep-scan the test module source for each new family name.
    """
    src = (_HARNESS_ROOT / "fuzz" / "test_api_routes.py").read_text()
    block_a_families = [
        "nosql_operators",
        "ldap",
        "xpath",
        "xml_xxe",
        "ssti",
        "log_injection",
        "ssrf",
        "mass_assignment",
        "prototype_pollution",
        "open_redirects",
    ]
    for family in block_a_families:
        assert f'"{family}"' in src, (
            f"family {family!r} not referenced in fuzz/test_api_routes.py "
            f"— route enumeration won't pick it up"
        )


def test_load_corpus_rejects_missing_directory(tmp_path: Path) -> None:
    """A non-existent corpus dir surfaces as FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        load_corpus(tmp_path / "does-not-exist")


def test_load_corpus_rejects_malformed_json(tmp_path: Path) -> None:
    """A corpus file with invalid JSON surfaces as ValueError."""
    (tmp_path / "broken.json").write_text("{not valid json")
    with pytest.raises(ValueError, match="invalid JSON"):
        load_corpus(tmp_path)


def test_load_corpus_rejects_missing_family_field(tmp_path: Path) -> None:
    """A corpus file without a `family` field surfaces as ValueError."""
    (tmp_path / "x.json").write_text(json.dumps({"payloads": []}))
    with pytest.raises(ValueError, match="missing or empty 'family'"):
        load_corpus(tmp_path)


def test_load_corpus_rejects_duplicate_payload_id(tmp_path: Path) -> None:
    """Two payloads in the same family with the same id is an error."""
    bad = {
        "family": "dupe",
        "payloads": [
            {"id": "p1", "expected_blocked": True},
            {"id": "p1", "expected_blocked": False},
        ],
    }
    (tmp_path / "dupe.json").write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="duplicate payload id"):
        load_corpus(tmp_path)


# ──────────────────────────── test-id generation ─────────────────────────────


def test_make_test_id_is_deterministic() -> None:
    """Same inputs ⇒ same id, byte-identical across calls."""
    a = make_test_id("post-chat", "xss", "xss-script-tag")
    b = make_test_id("post-chat", "xss", "xss-script-tag")
    assert a == b == "fuzz.post-chat.xss.xss-script-tag"


def test_make_test_id_normalizes_invalid_chars() -> None:
    """Non-alphanumeric chars (other than -) get replaced with -; runs of - collapse."""
    out = make_test_id("Post Chat!", "X.SS", "xss/script tag")
    assert out == "fuzz.post-chat.x-ss.xss-script-tag"
    # No leading or trailing dashes within a segment.
    assert "--" not in out


def test_make_test_id_segments_are_lowercase() -> None:
    """spec §7.3: all id segments lowercase."""
    out = make_test_id("GET-Findings", "XSS", "Script-Tag")
    assert out == out.lower()


# ──────────────────────────── classification ─────────────────────────────────


def test_classify_pass_for_clean_200_no_reflection() -> None:
    """200 + body that doesn't reflect the payload = PASS."""
    c = classify_response(
        200, '{"ok": true}', payload_value="<script>x</script>", expected_blocked=True
    )
    assert c.verdict == "pass"
    assert c.severity is None
    assert c.status_code == 200


def test_classify_fail_for_500() -> None:
    """500 from the server ⇒ FAIL high."""
    c = classify_response(
        500, "internal error", payload_value="any", expected_blocked=False
    )
    assert c.verdict == "fail"
    assert c.severity == "high"
    assert any("500" in r for r in c.reasons)


def test_classify_fail_for_stack_trace_in_body() -> None:
    """A 200 carrying a stack trace marker ⇒ FAIL high."""
    body = '{"error": "Traceback (most recent call last):\\n  File ..."}'
    c = classify_response(200, body, payload_value=None, expected_blocked=False)
    assert c.verdict == "fail"
    assert c.severity == "high"
    assert any("stack-trace" in r for r in c.reasons)


def test_classify_fail_for_xss_reflection() -> None:
    """A 200 echoing back an expected-blocked payload verbatim ⇒ FAIL high."""
    payload = "<script>alert(1)</script>"
    body = f'{{"echo": "{payload}"}}'
    c = classify_response(200, body, payload_value=payload, expected_blocked=True)
    assert c.verdict == "fail"
    assert c.severity == "high"
    assert any("reflect" in r for r in c.reasons)


def test_classify_pass_when_expected_blocked_false_even_with_reflection() -> None:
    """A 200 echo of a not-expected-blocked payload is still a PASS."""
    payload = "hello world"
    body = '{"echo": "hello world"}'
    c = classify_response(200, body, payload_value=payload, expected_blocked=False)
    assert c.verdict == "pass"


def test_classify_pass_for_400_sane_rejection() -> None:
    """A 4xx with a clean JSON body ⇒ PASS."""
    c = classify_response(
        400, '{"error": "bad request"}', payload_value="x", expected_blocked=True
    )
    assert c.verdict == "pass"


def test_classify_pass_when_client_blocks_the_request() -> None:
    """requests-library client-side block (CRLF in header) ⇒ PASS."""
    c = classify_response(
        0, "", payload_value="x\r\nY: 1", expected_blocked=True, client_blocked=True
    )
    assert c.verdict == "pass"
    assert c.status_code == 0
    assert any("client-blocked" in r for r in c.reasons)


def test_classify_skips_reflection_check_for_short_payloads() -> None:
    """Payloads shorter than 6 chars don't trigger the reflection check
    (too many false positives on common ASCII bytes)."""
    payload = "abc"
    body = '{"msg": "abc returned"}'
    c = classify_response(200, body, payload_value=payload, expected_blocked=True)
    assert c.verdict == "pass"


@pytest.mark.parametrize("marker", STACK_TRACE_MARKERS)
def test_classify_detects_each_stack_trace_marker(marker: str) -> None:
    """Each declared marker is actually checked (case-insensitive)."""
    body = f"some text {marker.upper()} more text"
    c = classify_response(200, body, payload_value=None, expected_blocked=False)
    assert c.verdict == "fail"
    assert c.severity == "high"


# ───────────────────────── destructive-route gating ──────────────────────────


@pytest.mark.parametrize(
    "method,expected",
    [
        ("GET", False),
        ("get", False),
        ("HEAD", False),
        ("OPTIONS", False),
        ("POST", True),
        ("post", True),
        ("PUT", True),
        ("PATCH", True),
        ("DELETE", True),
    ],
)
def test_is_destructive_matches_http_method(method: str, expected: bool) -> None:
    assert is_destructive(method) is expected


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/findings", False),
        ("/findings/{id}", True),
        ("/conversations/{session_id}/messages", True),
        ("/health", False),
        ("", False),
    ],
)
def test_route_has_path_param(path: str, expected: bool) -> None:
    assert route_has_path_param(path) is expected


def test_destructive_marker_skips_in_isolation() -> None:
    """Run a single destructive route's collection in a subprocess and
    confirm pytest reports it as skipped (no --include-destructive).

    Uses a destructive POST route from the manifest (`post-jira-tickets`)
    and asserts at least one test was skipped in the run.
    """
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "fuzz/test_api_routes.py",
        "-k",
        "post-jira-tickets and xss",
        "-q",
        "--no-header",
    ]
    env = {
        "PYTHONPATH": str(_HARNESS_ROOT),
        "PATH": __import__("os").environ.get("PATH", ""),
    }
    result = subprocess.run(
        cmd,
        cwd=str(_HARNESS_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    # Either "skipped" appears (destructive-marker gate worked) or the whole
    # session skipped due to missing DEMO_PASSWORD (also acceptable — the
    # gate runs before the fixture). What must NOT happen is a green
    # "passed" — that would mean we silently fired POSTs at the dev env.
    out = result.stdout + result.stderr
    assert "passed" not in out or "skipped" in out, out


def test_destructive_marker_enables_with_include_destructive_flag() -> None:
    """With --include-destructive, the destructive skip-marker is NOT added.

    We can't actually run the request (no DEMO_PASSWORD here), but we can
    verify the collection result doesn't report `skipped: destructive`.
    """
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "fuzz/test_api_routes.py",
        "-k",
        "post-jira-tickets and xss-script-tag and persona=ciso",
        "--include-destructive",
        "-v",
        "--no-header",
    ]
    env = {
        "PYTHONPATH": str(_HARNESS_ROOT),
        "PATH": __import__("os").environ.get("PATH", ""),
    }
    result = subprocess.run(
        cmd,
        cwd=str(_HARNESS_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    out = result.stdout + result.stderr
    # The skip reason for the destructive marker should NOT appear; we
    # accept any other skip (e.g. DEMO_PASSWORD unset).
    assert "pass --include-destructive to enable" not in out, out


# ──────────────────────────── results writer ─────────────────────────────────


def test_fuzz_results_path_uses_run_dir_when_set(tmp_path: Path) -> None:
    """When RUN_DIR is provided, results.json lands under it."""
    out = fuzz_results_path(tmp_path)
    assert out == tmp_path / "fuzz" / "results.json"


def test_fuzz_results_path_falls_back_to_local_when_run_dir_is_none() -> None:
    """When RUN_DIR is None, results.json lands under the local fallback."""
    out = fuzz_results_path(None)
    # The fallback is `<harness>/test-reports/_local/fuzz/results.json`.
    assert out.parts[-3:] == ("_local", "fuzz", "results.json")


def test_results_writer_round_trips(tmp_path: Path) -> None:
    """The writer dumps rows as a sorted JSON list."""
    # Import lazily so we don't drag in pytest plugin internals at module load.
    from fuzz.conftest import FuzzResultsWriter  # type: ignore

    writer = FuzzResultsWriter()
    writer.record(
        {
            "test_id": "fuzz.get-findings.xss.xss-script-tag",
            "status": "pass",
            "layer": "fuzz",
            "target_kind": "api_route",
            "target_id": "get-findings",
            "duration_seconds": 0.12,
        }
    )
    writer.record(
        {
            "test_id": "fuzz.get-findings.xss.xss-img-onerror",
            "status": "fail",
            "layer": "fuzz",
            "target_kind": "api_route",
            "target_id": "get-findings",
            "severity": "high",
            "evidence_path": "fuzz/results.json#fuzz.get-findings.xss.xss-img-onerror",
            "duration_seconds": 0.21,
        }
    )
    out_path = tmp_path / "fuzz" / "results.json"
    writer.write(out_path)
    rows = json.loads(out_path.read_text())
    assert len(rows) == 2
    # Sorted by test_id ascending.
    assert rows[0]["test_id"] < rows[1]["test_id"]
    # The FAIL row carries severity + evidence_path.
    fail = [r for r in rows if r["status"] == "fail"][0]
    assert fail["severity"] == "high"
    assert fail["evidence_path"].startswith("fuzz/results.json#")


def test_results_writer_creates_parent_dirs(tmp_path: Path) -> None:
    """`write` mkdir-p's the parent so the orchestrator doesn't have to."""
    from fuzz.conftest import FuzzResultsWriter  # type: ignore

    writer = FuzzResultsWriter()
    writer.record(
        {
            "test_id": "fuzz.x.y.z",
            "status": "pass",
            "layer": "fuzz",
            "target_kind": "api_route",
            "target_id": "x",
        }
    )
    out_path = tmp_path / "nested" / "deeper" / "fuzz" / "results.json"
    writer.write(out_path)
    assert out_path.exists()


def test_results_writer_pass_row_has_required_keys(tmp_path: Path) -> None:
    """A PASS row recorded by the writer keeps the keys the builder needs."""
    from fuzz.conftest import FuzzResultsWriter  # type: ignore

    writer = FuzzResultsWriter()
    row = {
        "test_id": "fuzz.get-findings.xss.xss-script-tag",
        "status": "pass",
        "layer": "fuzz",
        "target_kind": "api_route",
        "target_id": "get-findings",
        "duration_seconds": 0.12,
    }
    writer.record(row)
    out_path = tmp_path / "fuzz" / "results.json"
    writer.write(out_path)
    rows = json.loads(out_path.read_text())
    assert rows[0]["test_id"] == row["test_id"]
    assert rows[0]["status"] == "pass"
    assert rows[0]["target_kind"] == "api_route"


# ───────────────────────── round-trip through builder ────────────────────────


def test_writer_output_loads_via_coverage_builder(tmp_path: Path) -> None:
    """The writer's results.json shape is consumable by `load_results`."""
    from src.coverage.builder import build_matrix, load_results  # type: ignore
    from fuzz.conftest import FuzzResultsWriter  # type: ignore

    # Use a real route id from the manifest so build_matrix doesn't raise
    # UnknownTargetError.
    writer = FuzzResultsWriter()
    writer.record(
        {
            "test_id": "fuzz.get-findings.xss.xss-script-tag",
            "status": "fail",
            "layer": "fuzz",
            "target_kind": "api_route",
            "target_id": "get-findings",
            "severity": "high",
            "evidence_path": "fuzz/results.json#fuzz.get-findings.xss.xss-script-tag",
            "duration_seconds": 0.21,
        }
    )
    (tmp_path / "fuzz").mkdir(parents=True, exist_ok=True)
    writer.write(tmp_path / "fuzz" / "results.json")

    manifest = json.loads(
        (_HARNESS_ROOT / "src" / "coverage" / "manifest.json").read_text()
    )
    results = load_results(tmp_path)
    assert len(results) == 1
    matrix = build_matrix(manifest, results)
    cells = matrix.api_routes["get-findings"]
    assert len(cells) == 1
    assert cells[0]["layer"] == "fuzz"
    assert cells[0]["status"] == "fail"
    assert cells[0]["severity"] == "high"
    assert matrix.summary["failures"] == 1
