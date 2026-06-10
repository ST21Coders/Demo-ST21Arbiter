"""Golden + smoke tests for src/coverage/builder.py.

Covers (task 7 acceptance):
  1. Empty results against the real manifest — every cell is uncovered.
  2. Synthesized full-sweep result list — every cell is PASS, summary tallies
     match the manifest dimensions exactly.
  3. One FAIL result — failures: 1 + cell reflects FAIL.
  4. AC20: a FAIL result without evidence_path raises MissingEvidenceError.
  5. UnknownTargetError on a result targeting a manifest-absent id.
  6. DOCUMENTED_UNSAFE is counted separately from failures (AC11).
  7. Determinism — same inputs produce byte-identical JSON twice.
  8. load_results happy path — 4 fake layer files round-trip.
  9. load_results with one layer missing — no error, only present layer
     returns rows.

Plus a few targeted coverage tests for the validation invariants
(MissingPersonaError, the manifest-absent persona case, the malformed-file
error messages from load_results) so a regression in those paths trips a
test instead of silently making the renderer behave oddly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.coverage.builder import (
    CellStatus,
    CoverageMatrix,
    MissingEvidenceError,
    MissingPersonaError,
    TestResult,
    UnknownTargetError,
    build_matrix,
    load_results,
    matrix_to_json,
)


_HARNESS_ROOT = Path(__file__).resolve().parent.parent
_MANIFEST_PATH = _HARNESS_ROOT / "src" / "coverage" / "manifest.json"


# ──────────────────────────────── fixtures ─────────────────────────────────


@pytest.fixture
def manifest() -> dict:
    """The real, committed manifest. Tests depend on its current shape (15
    pages, 25 routes, 11 tools, 4 personas — see task 5 acceptance)."""
    return json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def manifest_dimensions(manifest: dict) -> dict:
    """Counts derived from the real manifest, so the goldens below survive
    additions to the manifest as long as task 5's counts hold."""
    return {
        "pages": len(manifest["pages"]),
        "personas": len(manifest["personas"]),
        "routes": len(manifest["api_routes"]),
        "tools": len(manifest["agent_tools"]),
    }


def _synthesize_full_sweep(manifest: dict) -> list[TestResult]:
    """Build a plausible result list that hits every cell with PASS.

    For pages: one positive test per (page, persona) cell — uses test_id
    template `e2e.page.<page_id>.<persona_id>`. For api_routes: one e2e and
    one fuzz curated row per route, both PASS. For agent_tools: one TOOL_INVOKED
    row per tool.
    """
    results: list[TestResult] = []

    personas = [p["id"] for p in manifest["personas"]]
    for page in manifest["pages"]:
        for persona in personas:
            results.append(
                TestResult(
                    test_id=f"e2e.page.{page['id']}.{persona}",
                    status=CellStatus.PASS,
                    layer="e2e",
                    target_kind="page",
                    target_id=page["id"],
                    persona=persona,
                    duration_seconds=0.42,
                )
            )

    for route in manifest["api_routes"]:
        results.append(
            TestResult(
                test_id=f"e2e.route.{route['id']}",
                status=CellStatus.PASS,
                layer="e2e",
                target_kind="api_route",
                target_id=route["id"],
                duration_seconds=0.12,
            )
        )
        results.append(
            TestResult(
                test_id=f"fuzz.route.{route['id']}.curated",
                status=CellStatus.PASS,
                layer="fuzz",
                target_kind="api_route",
                target_id=route["id"],
                duration_seconds=0.31,
            )
        )

    for tool in manifest["agent_tools"]:
        results.append(
            TestResult(
                test_id=f"llm.tool.{tool['id']}",
                status=CellStatus.TOOL_INVOKED,
                layer="llm",
                target_kind="agent_tool",
                target_id=tool["id"],
                duration_seconds=2.5,
            )
        )

    return results


# ───────────────────────── 1. empty-results golden ─────────────────────────


def test_empty_results_yields_all_uncovered_cells(
    manifest: dict, manifest_dimensions: dict
):
    """No results → every page-cell NOT_RUN, every tool NOT_REACHED, every
    route's cell-list empty. Summary covers nothing."""
    matrix = build_matrix(manifest, results=[])

    # Pages: one entry per manifest page, each carrying every persona key as
    # NOT_RUN.
    assert len(matrix.pages) == manifest_dimensions["pages"]
    for page_id, cells in matrix.pages.items():
        assert len(cells) == manifest_dimensions["personas"]
        for status in cells.values():
            assert status == CellStatus.NOT_RUN

    # Api routes: one key per manifest route, empty list of cells.
    assert len(matrix.api_routes) == manifest_dimensions["routes"]
    for cells in matrix.api_routes.values():
        assert cells == []

    # Tools: one key per manifest tool, NOT_REACHED.
    assert len(matrix.agent_tools) == manifest_dimensions["tools"]
    for status in matrix.agent_tools.values():
        assert status == CellStatus.NOT_REACHED

    # Summary: zero covered, zero failures.
    s = matrix.summary
    assert s["pages_covered"] == 0
    assert s["routes_covered"] == 0
    assert s["tools_covered"] == 0
    assert s["failures"] == 0
    assert s["documented_unsafe"] == 0
    assert s["skipped"] == 0
    # The label strings render "0/<total>" so the report makes the uncovered
    # state obvious in the footer.
    assert (
        s["pages_covered_label"]
        == f"0/{manifest_dimensions['pages'] * manifest_dimensions['personas']}"
    )
    assert s["routes_covered_label"] == f"0/{manifest_dimensions['routes']}"
    assert s["tools_covered_label"] == f"0/{manifest_dimensions['tools']}"


def test_empty_matrix_json_keys_match_manifest_order(manifest: dict):
    """matrix_to_json must walk the manifest's declared order so the rendered
    table rows are stable across runs."""
    matrix = build_matrix(manifest, results=[])
    blob = matrix_to_json(matrix)

    expected_page_order = [p["id"] for p in manifest["pages"]]
    expected_route_order = [r["id"] for r in manifest["api_routes"]]
    expected_tool_order = [t["id"] for t in manifest["agent_tools"]]
    expected_persona_order = [p["id"] for p in manifest["personas"]]

    assert list(blob["pages"].keys()) == expected_page_order
    assert list(blob["api_routes"].keys()) == expected_route_order
    assert list(blob["agent_tools"].keys()) == expected_tool_order
    # Every page's inner dict carries personas in manifest order.
    for inner in blob["pages"].values():
        assert list(inner.keys()) == expected_persona_order


# ─────────────────────── 2. full-sweep all-PASS golden ─────────────────────


def test_full_sweep_yields_all_pass_and_correct_summary(
    manifest: dict, manifest_dimensions: dict
):
    """Synthesize a result for every cell; the matrix is solid green and the
    summary reports 100% coverage with zero failures."""
    results = _synthesize_full_sweep(manifest)
    matrix = build_matrix(manifest, results)

    # Pages: every cell PASS.
    for cells in matrix.pages.values():
        for status in cells.values():
            assert status == CellStatus.PASS

    # Routes: each route has the two cells we added (e2e + fuzz).
    for route_cells in matrix.api_routes.values():
        assert len(route_cells) == 2
        layers = sorted(c["layer"] for c in route_cells)
        assert layers == ["e2e", "fuzz"]
        for c in route_cells:
            assert c["status"] == CellStatus.PASS.value

    # Tools: all TOOL_INVOKED.
    for status in matrix.agent_tools.values():
        assert status == CellStatus.TOOL_INVOKED

    # Summary mirrors the task-prompt example exactly.
    s = matrix.summary
    total_page_cells = manifest_dimensions["pages"] * manifest_dimensions["personas"]
    assert s == {
        "pages_total": total_page_cells,
        "pages_covered": total_page_cells,
        "pages_covered_label": f"{total_page_cells}/{total_page_cells}",
        "routes_total": manifest_dimensions["routes"],
        "routes_covered": manifest_dimensions["routes"],
        "routes_covered_label": (
            f"{manifest_dimensions['routes']}/{manifest_dimensions['routes']}"
        ),
        "tools_total": manifest_dimensions["tools"],
        "tools_covered": manifest_dimensions["tools"],
        "tools_covered_label": (
            f"{manifest_dimensions['tools']}/{manifest_dimensions['tools']}"
        ),
        "failures": 0,
        "documented_unsafe": 0,
        "skipped": 0,
    }


# ───────────────────────────── 3. one FAIL ────────────────────────────────


def test_one_fail_result_flips_cell_and_increments_failure_count(manifest: dict):
    """A single FAIL result with evidence — summary reports failures: 1 and
    the cell holds FAIL."""
    fail_result = TestResult(
        test_id="e2e.page.findings.ciso",
        status=CellStatus.FAIL,
        layer="e2e",
        target_kind="page",
        target_id="findings",
        persona="ciso",
        evidence_path="e2e/screenshots/ciso-findings.png",
    )
    matrix = build_matrix(manifest, results=[fail_result])

    assert matrix.pages["findings"]["ciso"] == CellStatus.FAIL
    assert matrix.summary["failures"] == 1
    assert matrix.summary["pages_covered"] == 1


# ─────────────────────────── 4. AC20 enforcement ──────────────────────────


def test_fail_without_evidence_raises_missing_evidence_error(manifest: dict):
    """AC20: every failure must point at an on-disk artifact. A FAIL with
    no evidence_path is rejected at build time."""
    bad = TestResult(
        test_id="e2e.page.findings.ciso",
        status=CellStatus.FAIL,
        layer="e2e",
        target_kind="page",
        target_id="findings",
        persona="ciso",
        # evidence_path intentionally omitted
    )
    with pytest.raises(MissingEvidenceError, match="AC20"):
        build_matrix(manifest, results=[bad])


def test_non_fail_status_does_not_require_evidence(manifest: dict):
    """PASS / SKIPPED / DOCUMENTED_UNSAFE / TOOL_INVOKED rows are allowed to
    omit evidence_path. Only FAIL is bound by the AC20 rule."""
    rows = [
        TestResult(
            test_id="e2e.page.findings.ciso",
            status=CellStatus.PASS,
            layer="e2e",
            target_kind="page",
            target_id="findings",
            persona="ciso",
        ),
        TestResult(
            test_id="auth.chat.no-signature",
            status=CellStatus.DOCUMENTED_UNSAFE,
            layer="auth",
            target_kind="api_route",
            target_id="post-chat",
        ),
        TestResult(
            test_id="llm.tool.master.sharepoint_lookup",
            status=CellStatus.TOOL_INVOKED,
            layer="llm",
            target_kind="agent_tool",
            target_id="master.sharepoint_lookup",
        ),
    ]
    # Must not raise.
    matrix = build_matrix(manifest, results=rows)
    assert matrix.summary["failures"] == 0


# ─────────────────────────── 5. unknown target ────────────────────────────


def test_unknown_page_target_raises(manifest: dict):
    """Result for a page id absent from manifest → UnknownTargetError."""
    bad = TestResult(
        test_id="e2e.page.nonexistent.ciso",
        status=CellStatus.PASS,
        layer="e2e",
        target_kind="page",
        target_id="nonexistent",
        persona="ciso",
    )
    with pytest.raises(UnknownTargetError, match="nonexistent"):
        build_matrix(manifest, results=[bad])


def test_unknown_route_target_raises(manifest: dict):
    bad = TestResult(
        test_id="fuzz.route.ghost",
        status=CellStatus.PASS,
        layer="fuzz",
        target_kind="api_route",
        target_id="ghost",
    )
    with pytest.raises(UnknownTargetError, match="ghost"):
        build_matrix(manifest, results=[bad])


def test_unknown_tool_target_raises(manifest: dict):
    bad = TestResult(
        test_id="llm.tool.missing",
        status=CellStatus.TOOL_INVOKED,
        layer="llm",
        target_kind="agent_tool",
        target_id="missing",
    )
    with pytest.raises(UnknownTargetError, match="missing"):
        build_matrix(manifest, results=[bad])


def test_unknown_target_kind_raises(manifest: dict):
    """A typo in target_kind (e.g. 'tools' instead of 'agent_tool') must not
    silently drop the result on the floor."""
    bad = TestResult(
        test_id="e2e.weird",
        status=CellStatus.PASS,
        layer="e2e",
        target_kind="not_a_real_kind",
        target_id="findings",
    )
    with pytest.raises(UnknownTargetError, match="target_kind"):
        build_matrix(manifest, results=[bad])


def test_unknown_persona_raises(manifest: dict):
    """A page result with a persona not in manifest.personas surfaces as
    UnknownTargetError — same drift signal as a missing page id."""
    bad = TestResult(
        test_id="e2e.page.findings.intern",
        status=CellStatus.PASS,
        layer="e2e",
        target_kind="page",
        target_id="findings",
        persona="intern",
    )
    with pytest.raises(UnknownTargetError, match="intern"):
        build_matrix(manifest, results=[bad])


def test_page_result_missing_persona_raises(manifest: dict):
    """Page targets without persona can't be placed into the (page, persona)
    matrix → MissingPersonaError."""
    bad = TestResult(
        test_id="e2e.page.findings.unspecified",
        status=CellStatus.PASS,
        layer="e2e",
        target_kind="page",
        target_id="findings",
        persona=None,
    )
    with pytest.raises(MissingPersonaError):
        build_matrix(manifest, results=[bad])


def test_validate_result_page_target_with_null_persona_raises_missing_persona():
    """Contract pin (C1): `_validate_result` must reject `target_kind='page'`
    + `persona=None` BEFORE the result reaches `_place_result`.

    The cognito-hosted-ui spec earlier pushed `harness-result` annotations with
    `target_kind: 'page'`, `target_id: 'signin'`, `persona: null`. Without this
    invariant, the harness's `load_results → build_matrix` step would crash on
    those rows and abort the entire reporting pipeline. The spec has since
    been fixed to drop those annotations (page-targeted rows must carry a
    persona, OR not emit a row at all), and the results-reporter.js now
    defensively skips such rows too — but this unit test pins the contract
    at the builder boundary so a future regression in either layer fails a
    test instead of corrupting the run.
    """
    from src.coverage.builder import _validate_result

    bad = TestResult(
        test_id="e2e.signin.hosted-ui-redirects-to-cognito",
        status=CellStatus.PASS,
        layer="e2e",
        target_kind="page",
        target_id="signin",
        persona=None,
    )
    with pytest.raises(MissingPersonaError, match="persona"):
        _validate_result(bad)


# ─────────────────────── 6. documented-unsafe counting ─────────────────────


def test_documented_unsafe_counted_separately_from_failures(manifest: dict):
    """AC11: DOCUMENTED_UNSAFE does not count as a failure in the summary.
    It has its own line."""
    rows = [
        TestResult(
            test_id="auth.chat.no-signature",
            status=CellStatus.DOCUMENTED_UNSAFE,
            layer="auth",
            target_kind="api_route",
            target_id="post-chat",
        ),
        TestResult(
            test_id="e2e.page.governance.soc",
            status=CellStatus.FAIL,
            layer="e2e",
            target_kind="page",
            target_id="governance",
            persona="soc",
            evidence_path="e2e/screenshots/soc-governance.png",
        ),
    ]
    matrix = build_matrix(manifest, rows)

    assert matrix.summary["failures"] == 1
    assert matrix.summary["documented_unsafe"] == 1


# ─────────────────────────── 7. determinism ────────────────────────────────


def test_matrix_to_json_is_deterministic_across_two_builds(manifest: dict):
    """Same manifest + same results twice — byte-identical JSON. Relies on
    insertion-order matching manifest order; do NOT use sort_keys."""
    results = _synthesize_full_sweep(manifest)

    blob_a = matrix_to_json(build_matrix(manifest, results))
    blob_b = matrix_to_json(build_matrix(manifest, results))

    rendered_a = json.dumps(blob_a, sort_keys=False)
    rendered_b = json.dumps(blob_b, sort_keys=False)

    assert rendered_a == rendered_b


def test_skipped_status_counted_in_summary(manifest: dict):
    """A SKIPPED result with a reason counts towards `skipped` and `covered`
    (we know it ran far enough to be skipped — gray, not white)."""
    row = TestResult(
        test_id="e2e.page.forgot-password.ciso",
        status=CellStatus.SKIPPED,
        layer="e2e",
        target_kind="page",
        target_id="signin",
        persona="ciso",
        skipped_reason="by-design: forgot-password emails the demo inbox",
    )
    matrix = build_matrix(manifest, results=[row])
    assert matrix.summary["skipped"] == 1
    assert matrix.summary["pages_covered"] == 1
    assert matrix.pages["signin"]["ciso"] == CellStatus.SKIPPED


# ─────────────────────────── 8 + 9. load_results ──────────────────────────


def _write_layer_results(run_dir: Path, layer: str, rows: list[dict]) -> None:
    layer_dir = run_dir / layer
    layer_dir.mkdir(parents=True, exist_ok=True)
    (layer_dir / "results.json").write_text(json.dumps(rows), encoding="utf-8")


def test_load_results_reads_all_four_layers(tmp_path: Path):
    """Write a results.json for each of e2e / fuzz / auth / llm; load_results
    returns one TestResult per row, in layer order."""
    _write_layer_results(
        tmp_path,
        "e2e",
        [
            {
                "test_id": "e2e.page.findings.ciso",
                "status": "pass",
                "layer": "e2e",
                "target_kind": "page",
                "target_id": "findings",
                "persona": "ciso",
            },
        ],
    )
    _write_layer_results(
        tmp_path,
        "fuzz",
        [
            {
                "test_id": "fuzz.route.get-findings.oversized",
                "status": "pass",
                "layer": "fuzz",
                "target_kind": "api_route",
                "target_id": "get-findings",
            },
        ],
    )
    _write_layer_results(
        tmp_path,
        "auth",
        [
            {
                "test_id": "auth.token-usage.soc-forbidden",
                "status": "pass",
                "layer": "auth",
                "target_kind": "api_route",
                "target_id": "get-token-usage",
            },
        ],
    )
    _write_layer_results(
        tmp_path,
        "llm",
        [
            {
                "test_id": "llm.tool.master.sharepoint_lookup",
                "status": "tool_invoked",
                "layer": "llm",
                "target_kind": "agent_tool",
                "target_id": "master.sharepoint_lookup",
            },
        ],
    )

    results = load_results(tmp_path)
    assert len(results) == 4
    assert [r.layer for r in results] == ["e2e", "fuzz", "auth", "llm"]
    assert results[0].status == CellStatus.PASS
    assert results[3].status == CellStatus.TOOL_INVOKED


def test_load_results_missing_layer_is_not_an_error(tmp_path: Path):
    """Only e2e/results.json exists → load_results returns just that layer's
    rows, no error raised. The other three layers register as NOT_RUN cells
    in the matrix downstream."""
    _write_layer_results(
        tmp_path,
        "e2e",
        [
            {
                "test_id": "e2e.page.dashboard.ciso",
                "status": "pass",
                "layer": "e2e",
                "target_kind": "page",
                "target_id": "dashboard",
                "persona": "ciso",
            },
        ],
    )

    results = load_results(tmp_path)
    assert len(results) == 1
    assert results[0].layer == "e2e"


def test_load_results_returns_empty_when_no_layer_files_exist(tmp_path: Path):
    """A run dir with no layer subdirectories yet — load_results returns
    [] cleanly (caller will pass [] to build_matrix → all NOT_RUN)."""
    results = load_results(tmp_path)
    assert results == []


def test_load_results_rejects_top_level_object(tmp_path: Path):
    """Layer files must be a JSON list of result dicts; an object is wrong."""
    layer_dir = tmp_path / "e2e"
    layer_dir.mkdir()
    (layer_dir / "results.json").write_text(json.dumps({"tests": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="must contain a JSON list"):
        load_results(tmp_path)


def test_load_results_rejects_malformed_json(tmp_path: Path):
    layer_dir = tmp_path / "fuzz"
    layer_dir.mkdir()
    (layer_dir / "results.json").write_text("not json{", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        load_results(tmp_path)


def test_load_results_rejects_row_with_unknown_status(tmp_path: Path):
    """Status field must coerce to a known CellStatus member. Anything else
    surfaces with a useful row-index pointer."""
    _write_layer_results(
        tmp_path,
        "auth",
        [
            {
                "test_id": "auth.weird",
                "status": "broken-status",
                "layer": "auth",
                "target_kind": "api_route",
                "target_id": "get-token-usage",
            },
        ],
    )
    with pytest.raises(ValueError, match=r"\[0\]"):
        load_results(tmp_path)


def test_load_results_rejects_row_missing_required_field(tmp_path: Path):
    """A row missing target_id should not silently become a half-built
    TestResult — load_results raises ValueError with the row index."""
    _write_layer_results(
        tmp_path,
        "auth",
        [
            {
                "test_id": "auth.weird",
                "status": "pass",
                "layer": "auth",
                "target_kind": "api_route",
                # target_id intentionally missing
            },
        ],
    )
    with pytest.raises(ValueError, match=r"\[0\]"):
        load_results(tmp_path)


# ───────────────────────── round-trip via load_results ─────────────────────


def test_round_trip_load_then_build(tmp_path: Path, manifest: dict):
    """End-to-end: write 4 results.json files, load them, build matrix, walk
    the cells. Catches any contract mismatch between the loader and builder."""
    _write_layer_results(
        tmp_path,
        "e2e",
        [
            {
                "test_id": "e2e.page.findings.ciso",
                "status": "pass",
                "layer": "e2e",
                "target_kind": "page",
                "target_id": "findings",
                "persona": "ciso",
                "duration_seconds": 0.7,
            },
        ],
    )
    _write_layer_results(
        tmp_path,
        "llm",
        [
            {
                "test_id": "llm.tool.master.sharepoint_lookup",
                "status": "tool_invoked",
                "layer": "llm",
                "target_kind": "agent_tool",
                "target_id": "master.sharepoint_lookup",
                "duration_seconds": 2.1,
            },
        ],
    )

    results = load_results(tmp_path)
    matrix = build_matrix(manifest, results)

    assert matrix.pages["findings"]["ciso"] == CellStatus.PASS
    assert matrix.agent_tools["master.sharepoint_lookup"] == CellStatus.TOOL_INVOKED
    assert matrix.summary["failures"] == 0


def test_severity_round_trips_through_load_and_matrix_to_json(
    tmp_path: Path, manifest: dict
):
    """Severity is set by the spec (e.g. negative-gating sets 'high' on a
    leaked page), parsed by load_results, carried on TestResult.severity, and
    serialized via matrix_to_json into the route cells' `severity` key (and
    available on the underlying TestResult for the renderer's findings list).
    This guard catches a regression in any of those three steps."""
    # Write a high-severity fuzz row for a known route id.
    _write_layer_results(
        tmp_path,
        "fuzz",
        [
            {
                "test_id": "fuzz.findings-id.path-traversal",
                "status": "fail",
                "layer": "fuzz",
                "target_kind": "api_route",
                "target_id": "get-finding-by-id",
                "evidence_path": "fuzz/transcripts/findings.jsonl",
                "severity": "high",
            },
        ],
    )

    results = load_results(tmp_path)
    assert len(results) == 1
    assert results[0].severity == "high"

    matrix = build_matrix(manifest, results)
    blob = matrix_to_json(matrix)

    # The severity round-trips into the route cell dict.
    route_cells = blob["api_routes"]["get-finding-by-id"]
    assert len(route_cells) == 1
    assert route_cells[0]["severity"] == "high"
    assert route_cells[0]["status"] == "fail"

    # And a results.json row WITHOUT a severity field round-trips as None
    # (defaults are tolerant — not every layer tags severity).
    _write_layer_results(
        tmp_path,
        "e2e",
        [
            {
                "test_id": "e2e.page.findings.ciso",
                "status": "pass",
                "layer": "e2e",
                "target_kind": "page",
                "target_id": "findings",
                "persona": "ciso",
            },
        ],
    )
    results = load_results(tmp_path)
    e2e_result = next(r for r in results if r.layer == "e2e")
    assert e2e_result.severity is None


def test_coverage_matrix_default_is_empty():
    """Direct construction without args produces a valid empty CoverageMatrix
    — guards against the dataclass picking up a mutable default (the
    default_factory was deliberately chosen to avoid that)."""
    m = CoverageMatrix()
    assert m.pages == {}
    assert m.api_routes == {}
    assert m.agent_tools == {}
    assert m.summary == {}
