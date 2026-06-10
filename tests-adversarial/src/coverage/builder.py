"""src/coverage/builder.py — turns the static manifest + per-layer test results
into a coverage matrix the HTML/Markdown renderer (tasks 22/23) can consume.

This module is the heart of acceptance criteria AC6/7/8 (the visible "every
damn thing was tested" matrix in `report.html`) and AC20 (every failure has an
on-disk evidence pointer).

────────────────────────────────────────────────────────────────────────────
Surface
────────────────────────────────────────────────────────────────────────────

  CellStatus            — enum for cell colors in the rendered matrix.
  TestResult            — dataclass shaped like a row from a layer's
                          `results.json` (see plan §5.2).
  CoverageMatrix        — dataclass with three indexed views: pages,
                          api_routes, agent_tools — plus a summary dict.
  build_matrix(...)     — pure: manifest + results → CoverageMatrix.
  matrix_to_json(...)   — serializes CoverageMatrix to a stable JSON dict.
                          Iteration order follows the manifest so the
                          rendered tables are deterministic across runs.
  load_results(run_dir) — reads <run_dir>/<layer>/results.json for each of
                          the four layers (e2e, fuzz, auth, llm). Missing
                          file = layer didn't run (not an error).

  UnknownTargetError    — TestResult.target_id is not in the manifest.
  MissingPersonaError   — TestResult for a page target without persona.
  MissingEvidenceError  — TestResult.status == FAIL but evidence_path is None.

────────────────────────────────────────────────────────────────────────────
Initial cell state (before any result is placed)
────────────────────────────────────────────────────────────────────────────

  pages       : every (page_id, persona_id) cell is NOT_RUN. For a persona
                listed in pages[].blocked_for, the "positive" test will
                naturally not run — but the negative-gating test in task 10
                (`e2e.page.<page>.<persona>` for blocked personas) fills the
                same cell. PASS means "behavior matched manifest expectation":
                allowed persona saw the page, OR blocked persona was correctly
                denied. FAIL means the opposite. We do not distinguish
                positive vs negative tests with a separate flag — both write
                the same cell, and the test id makes the distinction visible
                in the per-layer drill-down. (Decision documented to keep the
                renderer simple; spec §4.1 calls all 60 cells "required
                coverage" regardless of polarity.)

  api_routes  : every route_id maps to an empty list of cells. Each entry
                a layer reports for that route appends one cell, tagged with
                the layer + status, so the renderer can show check / dash
                per layer column (spec §6.2 (b)).

  agent_tools : every tool_id is NOT_REACHED. A result transitions it to
                TOOL_INVOKED (best — telemetry confirmed the tool fired),
                PROMPT_ONLY (eliciting prompt landed but tool call was not
                observable), PASS (smoke-only e.g. jira_specialist black-box),
                FAIL, SKIPPED, or DOCUMENTED_UNSAFE per spec §4.3.

────────────────────────────────────────────────────────────────────────────
Determinism
────────────────────────────────────────────────────────────────────────────

`matrix_to_json` walks `manifest["pages"]`, `manifest["api_routes"]`, and
`manifest["agent_tools"]` in their declared order, and emits ordinary `dict`
literals (insertion order preserved in Python 3.7+). The test suite includes a
determinism check that re-builds the same matrix twice from identical inputs
and asserts byte-identical JSON via `json.dumps(..., sort_keys=False)`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

# ──────────────────────────── public exceptions ────────────────────────────


class CoverageError(RuntimeError):
    """Base for all errors raised from this module."""


class UnknownTargetError(CoverageError):
    """A TestResult references a target_id that isn't in the manifest."""


class MissingPersonaError(CoverageError):
    """A page-targeted TestResult was missing its `persona` field."""


class MissingEvidenceError(CoverageError):
    """A FAIL TestResult was missing its `evidence_path` (violates AC20)."""


# ───────────────────────────── data model ──────────────────────────────────


class CellStatus(str, Enum):
    """Status values for cells in the coverage matrix.

    Inherits from `str` so each member compares equal to its underlying value
    (handy for JSON round-trips and for ad-hoc comparisons in the renderer).
    """

    NOT_RUN = "not_run"  # gray — no result reported yet
    PASS = "pass"  # green
    FAIL = "fail"  # red
    SKIPPED = "skipped"  # yellow with reason
    TOOL_INVOKED = "tool_invoked"  # tool actually fired via chat
    PROMPT_ONLY = "prompt_only"  # prompt elicited, tool unconfirmed
    NOT_REACHED = "not_reached"  # never attempted
    DOCUMENTED_UNSAFE = "documented_unsafe"  # AC11 — present but expected


# Statuses we count as a "failure" for the summary's failures tally. AC11's
# DOCUMENTED_UNSAFE is explicitly not in this set — those are counted on their
# own line in the summary.
_FAILURE_STATUSES: frozenset[CellStatus] = frozenset({CellStatus.FAIL})

# Statuses that count a page-cell or tool-cell as "covered" for the
# coverage-percent calculation in the summary. NOT_RUN / NOT_REACHED are the
# only "uncovered" states.
_COVERED_STATUSES: frozenset[CellStatus] = frozenset(
    {
        CellStatus.PASS,
        CellStatus.FAIL,
        CellStatus.SKIPPED,
        CellStatus.TOOL_INVOKED,
        CellStatus.PROMPT_ONLY,
        CellStatus.DOCUMENTED_UNSAFE,
    }
)


@dataclass
class TestResult:
    """One row from a layer's results.json. The harness's per-layer adapters
    (tasks 9, 12, 14, 17) parse their raw outputs into this shape before the
    builder consumes them.

    Fields:
      test_id        : stable, lowercase, dot-separated (spec §7.3).
                       Example: "e2e.page.findings.ciso".
      status         : CellStatus the cell should take on.
      layer          : "e2e" | "fuzz" | "auth" | "llm".
      target_kind    : "page" | "api_route" | "agent_tool".
      target_id      : matches the corresponding manifest entry's `id`.
                       For pages: the page id (e.g. "findings").
                       For api_routes: the route id (e.g. "get-findings").
                       For agent_tools: the tool id (e.g.
                       "master.sharepoint_lookup").
      persona        : required when target_kind == "page". Identifies which
                       (page, persona) cell to fill. Optional for routes/tools.
      skipped_reason : human-readable reason when status == SKIPPED.
      duration_seconds : per-test wall-clock (seconds, float). Optional.
      evidence_path  : path under the run dir to the supporting artifact
                       (transcript / screenshot / JSONL row). REQUIRED when
                       status == FAIL per AC20; the builder enforces this.
      severity       : optional human-ranked severity for findings — one of
                       "low" | "medium" | "high" | "critical" | "info" — set
                       by the spec (e.g. negative-gating sets "high" on a
                       leaked page, the auth layer sets "high" on a
                       cross-persona escalation). The builder does not
                       interpret it; it round-trips it through to the report
                       so the renderer (task 22) can rank findings.
    """

    # Tell pytest this is not a test class. The dataclass name starts with
    # "Test" — pytest's default collector would otherwise emit a warning and
    # try (and fail) to instantiate it.
    __test__ = False

    test_id: str
    status: CellStatus
    layer: str
    target_kind: str
    target_id: str
    persona: str | None = None
    skipped_reason: str | None = None
    duration_seconds: float | None = None
    evidence_path: str | None = None
    severity: str | None = None


@dataclass
class CoverageMatrix:
    """The built matrix. Three indexed views + a flat summary.

    pages       : page_id -> { persona_id -> CellStatus }. Always carries
                  every persona key for every page (NOT_RUN for cells no
                  result hit). Insertion order matches manifest order on
                  both the outer and inner dicts.
      api_routes  : route_id -> list[ dict ] of layer-tagged cells. The list
                  preserves insertion order (the order results arrived for
                  that route). One route may have many cells (e.g. an e2e
                  smoke + 8 fuzz curated + 8 hypothesis-generative).
      agent_tools : tool_id -> CellStatus.
      summary     : counts + percentages, see `_build_summary` for shape.
    """

    pages: dict[str, dict[str, CellStatus]] = field(default_factory=dict)
    api_routes: dict[str, list[dict]] = field(default_factory=dict)
    agent_tools: dict[str, CellStatus] = field(default_factory=dict)
    summary: dict = field(default_factory=dict)


# ─────────────────────── manifest indexing helpers ─────────────────────────


def _index_pages(manifest: dict) -> dict[str, dict]:
    """`{page_id: page_entry}` — insertion order matches manifest order."""
    return {p["id"]: p for p in manifest.get("pages", [])}


def _index_routes(manifest: dict) -> dict[str, dict]:
    return {r["id"]: r for r in manifest.get("api_routes", [])}


def _index_tools(manifest: dict) -> dict[str, dict]:
    return {t["id"]: t for t in manifest.get("agent_tools", [])}


def _persona_ids(manifest: dict) -> list[str]:
    """Ordered persona ids from the manifest's `personas` array. Falls back
    to the four canonical ids if a slimmed-down manifest omits the array
    (covers the goldens-with-minimal-manifest case in the tests)."""
    if manifest.get("personas"):
        return [p["id"] for p in manifest["personas"]]
    return ["ciso", "soc", "grc", "employee"]


# ────────────────────────── matrix construction ────────────────────────────


def _empty_pages_matrix(
    pages_index: dict[str, dict], personas: list[str]
) -> dict[str, dict[str, CellStatus]]:
    """Every (page, persona) cell starts as NOT_RUN. Order: pages first
    (manifest order), then personas inside each page (manifest order)."""
    return {
        page_id: {persona_id: CellStatus.NOT_RUN for persona_id in personas}
        for page_id in pages_index
    }


def _empty_routes_matrix(routes_index: dict[str, dict]) -> dict[str, list[dict]]:
    """Every route starts with an empty list — each result for that route
    appends a layer-tagged cell dict (see _route_cell)."""
    return {route_id: [] for route_id in routes_index}


def _empty_tools_matrix(tools_index: dict[str, dict]) -> dict[str, CellStatus]:
    """Every tool starts as NOT_REACHED."""
    return {tool_id: CellStatus.NOT_REACHED for tool_id in tools_index}


def _route_cell(result: TestResult) -> dict:
    """Serialize a route-targeted TestResult into the small dict shape the
    api_routes list stores. Stable keys so test-snapshot diffs are clean.

    `severity` is included so the renderer can rank route-level findings the
    same way it ranks page-level and tool-level ones; it's `None` for results
    that didn't set one.
    """
    return {
        "layer": result.layer,
        "status": result.status.value,
        "test_id": result.test_id,
        "duration": result.duration_seconds,
        "evidence": result.evidence_path,
        "severity": result.severity,
    }


def _validate_result(result: TestResult) -> None:
    """AC20 + page-persona-required invariants. Raised before the result is
    placed so the matrix never enters a partially-built state."""
    if result.status == CellStatus.FAIL and not result.evidence_path:
        raise MissingEvidenceError(
            f"FAIL result '{result.test_id}' has no evidence_path — "
            f"AC20 requires every failure to point at an on-disk artifact."
        )
    if result.target_kind == "page" and result.persona is None:
        raise MissingPersonaError(
            f"page-targeted result '{result.test_id}' has no persona — "
            f"cannot place it in the (page, persona) matrix."
        )


def _place_result(
    matrix: CoverageMatrix,
    result: TestResult,
    pages_index: dict[str, dict],
    routes_index: dict[str, dict],
    tools_index: dict[str, dict],
    personas: list[str],
) -> None:
    """Drop a single result into the matrix. Raises UnknownTargetError if the
    target_id isn't in the appropriate index — this catches drift between the
    manifest and the per-layer test ids."""
    if result.target_kind == "page":
        if result.target_id not in pages_index:
            raise UnknownTargetError(
                f"page result '{result.test_id}' targets unknown page id "
                f"'{result.target_id}' (not in manifest.pages)."
            )
        if result.persona not in personas:
            raise UnknownTargetError(
                f"page result '{result.test_id}' targets unknown persona "
                f"'{result.persona}' (not in manifest.personas)."
            )
        matrix.pages[result.target_id][result.persona] = result.status
    elif result.target_kind == "api_route":
        if result.target_id not in routes_index:
            raise UnknownTargetError(
                f"api_route result '{result.test_id}' targets unknown route "
                f"id '{result.target_id}' (not in manifest.api_routes)."
            )
        matrix.api_routes[result.target_id].append(_route_cell(result))
    elif result.target_kind == "agent_tool":
        if result.target_id not in tools_index:
            raise UnknownTargetError(
                f"agent_tool result '{result.test_id}' targets unknown tool "
                f"id '{result.target_id}' (not in manifest.agent_tools)."
            )
        matrix.agent_tools[result.target_id] = result.status
    else:
        raise UnknownTargetError(
            f"result '{result.test_id}' has unknown target_kind '{result.target_kind}'."
        )


# ──────────────────────────── summary builder ──────────────────────────────


def _build_summary(
    matrix: CoverageMatrix,
    results: list[TestResult],
) -> dict:
    """Counts + percentages displayed in summary.md (task 23) and the report
    HTML footer (task 22).

    Shape:
      {
        "pages_total": 60,
        "pages_covered": 58,
        "pages_covered_label": "58/60",
        "routes_total": 25,
        "routes_covered": 24,
        "routes_covered_label": "24/25",
        "tools_total": 11,
        "tools_covered": 10,
        "tools_covered_label": "10/11",
        "failures": 1,
        "documented_unsafe": 1,
        "skipped": 2,
      }
    """
    # Pages: total cells = pages × personas (manifest dimensions). Covered =
    # any status except NOT_RUN.
    pages_total = sum(len(cells) for cells in matrix.pages.values())
    pages_covered = sum(
        1
        for cells in matrix.pages.values()
        for status in cells.values()
        if status in _COVERED_STATUSES
    )

    # Routes: total = manifest route count. Covered = route has ≥1 cell.
    routes_total = len(matrix.api_routes)
    routes_covered = sum(1 for cells in matrix.api_routes.values() if cells)

    # Tools: total = manifest tool count. Covered = any status except
    # NOT_REACHED.
    tools_total = len(matrix.agent_tools)
    tools_covered = sum(
        1 for status in matrix.agent_tools.values() if status in _COVERED_STATUSES
    )

    # Failure / documented-unsafe / skipped counts span every result the
    # builder saw, regardless of target_kind. AC11: documented-unsafe is
    # counted on its own line, NOT folded into failures.
    failures = sum(1 for r in results if r.status in _FAILURE_STATUSES)
    documented_unsafe = sum(
        1 for r in results if r.status == CellStatus.DOCUMENTED_UNSAFE
    )
    skipped = sum(1 for r in results if r.status == CellStatus.SKIPPED)

    return {
        "pages_total": pages_total,
        "pages_covered": pages_covered,
        "pages_covered_label": f"{pages_covered}/{pages_total}",
        "routes_total": routes_total,
        "routes_covered": routes_covered,
        "routes_covered_label": f"{routes_covered}/{routes_total}",
        "tools_total": tools_total,
        "tools_covered": tools_covered,
        "tools_covered_label": f"{tools_covered}/{tools_total}",
        "failures": failures,
        "documented_unsafe": documented_unsafe,
        "skipped": skipped,
    }


# ─────────────────────────── public entry points ───────────────────────────


def build_matrix(manifest: dict, results: list[TestResult]) -> CoverageMatrix:
    """Build the coverage matrix from a manifest + a flat list of results.

    Order of operations:
      1. Index manifest entries (preserves manifest declaration order).
      2. Initialize every cell to its empty/uncovered state.
      3. Validate each result (AC20 + page-persona invariant) before placing.
      4. Place each result; UnknownTargetError surfaces manifest/layer drift.
      5. Compute the summary tally.

    Raises:
      MissingEvidenceError — a FAIL result has no `evidence_path` (AC20).
      MissingPersonaError  — a page-targeted result has no `persona`.
      UnknownTargetError   — a result's `target_id` is not in the manifest.
    """
    pages_index = _index_pages(manifest)
    routes_index = _index_routes(manifest)
    tools_index = _index_tools(manifest)
    personas = _persona_ids(manifest)

    matrix = CoverageMatrix(
        pages=_empty_pages_matrix(pages_index, personas),
        api_routes=_empty_routes_matrix(routes_index),
        agent_tools=_empty_tools_matrix(tools_index),
    )

    # First pass: validate everything BEFORE any state change. This keeps
    # build_matrix atomic — either the whole list is valid and the matrix
    # reflects it, or an exception is raised and the caller can correct the
    # input without holding a half-built matrix.
    for result in results:
        _validate_result(result)

    # Second pass: place. Order matters — later results for the same cell
    # overwrite earlier ones for pages/tools, while routes accumulate as a
    # list. In practice each layer emits one result per (target, scenario)
    # so collisions on pages/tools are not expected; documenting the
    # last-write-wins semantics here so it isn't a surprise later.
    for result in results:
        _place_result(matrix, result, pages_index, routes_index, tools_index, personas)

    matrix.summary = _build_summary(matrix, results)
    return matrix


def matrix_to_json(matrix: CoverageMatrix) -> dict:
    """Serialize a CoverageMatrix into a JSON-ready dict.

    Cells:
      pages       -> { page_id: { persona_id: "status" } }
      api_routes  -> { route_id: [ {layer, status, test_id, duration, evidence} ] }
      agent_tools -> { tool_id: "status" }
      summary     -> dict (already JSON-ready)

    Stable iteration: matches the order the matrix was built in (which itself
    matches manifest order). Cells use the str-valued enum's `.value` so
    `json.dumps(..., sort_keys=False)` is deterministic.
    """
    return {
        "pages": {
            page_id: {persona_id: status.value for persona_id, status in cells.items()}
            for page_id, cells in matrix.pages.items()
        },
        "api_routes": {
            route_id: list(cells) for route_id, cells in matrix.api_routes.items()
        },
        "agent_tools": {
            tool_id: status.value for tool_id, status in matrix.agent_tools.items()
        },
        "summary": dict(matrix.summary),
    }


# ───────────────────────── results.json loader ─────────────────────────────


# Layer names the orchestrator runs, in execution order. `load_results` walks
# this list (not a glob) so an unexpected directory under `run_dir` doesn't
# get loaded as a phantom layer.
_LAYERS = ("e2e", "fuzz", "auth", "llm")


def _result_from_dict(raw: dict, default_layer: str) -> TestResult:
    """Parse one results.json row into a TestResult.

    Raises ValueError on malformed input — callers (`load_results`) wrap this
    in a helpful filename + index context. Status is coerced via CellStatus
    so an unknown value surfaces immediately.
    """
    try:
        status = CellStatus(raw["status"])
    except (KeyError, ValueError) as exc:
        raise ValueError(f"invalid or missing 'status' field: {exc}") from exc
    try:
        return TestResult(
            test_id=raw["test_id"],
            status=status,
            layer=raw.get("layer", default_layer),
            target_kind=raw["target_kind"],
            target_id=raw["target_id"],
            persona=raw.get("persona"),
            skipped_reason=raw.get("skipped_reason"),
            duration_seconds=raw.get("duration_seconds"),
            evidence_path=raw.get("evidence_path"),
            severity=raw.get("severity"),
        )
    except KeyError as exc:
        raise ValueError(f"missing required field {exc}") from exc


def load_results(run_dir: Path) -> list[TestResult]:
    """Read every layer's results.json under `run_dir` and return a flat
    list of TestResult, in (layer-order, then file-order).

    Layout expected (matches plan §5.2 + §6.1):
      <run_dir>/e2e/results.json    — list of TestResult dicts
      <run_dir>/fuzz/results.json
      <run_dir>/auth/results.json
      <run_dir>/llm/results.json

    Missing layer file = that layer didn't run. This is a valid state, NOT
    an error: NOT_RUN cells in the final matrix tell the reader the layer
    was skipped. A malformed JSON file or a malformed result row raises
    ValueError with the offending file + row index in the message.
    """
    out: list[TestResult] = []
    for layer in _LAYERS:
        path = run_dir / layer / "results.json"
        if not path.exists():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path} is not valid JSON: {exc}") from exc
        if not isinstance(raw, list):
            raise ValueError(
                f"{path} must contain a JSON list of result dicts, "
                f"got {type(raw).__name__}."
            )
        for idx, row in enumerate(raw):
            if not isinstance(row, dict):
                raise ValueError(
                    f"{path}[{idx}] is not a JSON object (got {type(row).__name__})."
                )
            try:
                out.append(_result_from_dict(row, default_layer=layer))
            except ValueError as exc:
                raise ValueError(f"{path}[{idx}]: {exc}") from exc
    return out


__all__ = [
    "CellStatus",
    "TestResult",
    "CoverageMatrix",
    "CoverageError",
    "UnknownTargetError",
    "MissingPersonaError",
    "MissingEvidenceError",
    "build_matrix",
    "matrix_to_json",
    "load_results",
]
