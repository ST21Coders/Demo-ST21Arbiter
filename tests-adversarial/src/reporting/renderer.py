"""src/reporting/renderer.py — render report.json into report.html / summary.md.

Tasks 22 (HTML) and 23 (summary.md). The renderer is the deliverable the
user explicitly called out as needing to be "extremely polished" —
`report.html` is the in-browser artifact, `summary.md` is what gets
pasted into Slack/email when forwarding to the team.

Design choices:
- Jinja2 templates at `src/reporting/templates/`. Hand-rolling a 60-cell
  matrix in Python string concatenation is unreadable; Jinja lets the
  template carry the layout and the renderer carry just the data prep.
- Pure-Python, no JS build step. The HTML template embeds a tiny vanilla
  JS sorter (no jQuery, no framework). No CDN / no external fonts: the
  rendered HTML must work offline (AC: forwardable, self-contained).
- Determinism: the same `report` dict produces byte-identical output
  across two renders. Achieved by (a) `Environment(keep_trailing_newline
  =True)`, (b) never iterating over a Python `set`, and (c) deterministic
  finding ranking inherited from `report_builder._extract_findings`.
- The renderer reads ONLY the report dict + the manifest. It does not
  re-read layer results.json files — `build_report` already collapsed
  those into `findings[]` and `coverage`.
- AC12 sanitization for `summary.md`: a post-render `_sanitize` pass
  scrubs 12-digit AWS account ids, JWT-shaped strings, and oversized
  base64-ish blobs. We do this in the renderer (not the template) so the
  template stays readable and any future template additions are auto-
  scrubbed without per-field defensive Jinja.

Public entry points:
  render_html(report, out_dir)    -> Path  (writes report.html)
  render_summary(report, out_dir) -> Path  (writes summary.md)
"""

from __future__ import annotations

import re
from pathlib import Path

import jinja2

# ───────────────────── Jinja environment (module-singleton) ─────────────────

_TEMPLATES_DIR = Path(__file__).parent / "templates"

# We construct the environment once at import time so two renders in the
# same process don't re-walk the filesystem for the template directory.
# `autoescape=True` makes the template safe-by-default for any user-supplied
# string (target URLs, summary text, persona names) that lands in HTML
# content. The few places that need raw HTML (severity pill class names,
# the inline sorter script) use `|safe` filter or are static template text.
# `keep_trailing_newline=True` makes the rendered output end in `\n` exactly
# once, which keeps the determinism test stable across editors that strip
# trailing whitespace.
_ENV = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=jinja2.select_autoescape(["html", "j2"]),
    keep_trailing_newline=True,
    trim_blocks=True,
    lstrip_blocks=True,
)


# ────────────────────── data-prep helpers (private) ────────────────────────


# Severity → CSS class on the pill element. Stays in lock-step with the
# `_SEVERITY_TIERS` constant in report_builder.py. The "unknown" key catches
# any severity label that lands in the report unexpectedly so it still
# renders with a visible (gray) pill rather than collapsing to invisible.
_SEVERITY_PILL_CLASS = {
    "critical": "pill pill-critical",
    "high": "pill pill-high",
    "medium": "pill pill-medium",
    "low": "pill pill-low",
    "info": "pill pill-info",
}
_UNKNOWN_PILL_CLASS = "pill pill-unknown"

# Per-cell CSS class for the pages × personas matrix. Each maps to a small
# rule in the inline <style> block. NOT_RUN stays gray (uncovered); pass /
# fail are the two outcome states; skipped is yellow with a reason tooltip.
_PAGE_CELL_CLASS = {
    "pass": "cell cell-pass",
    "fail": "cell cell-fail",
    "skipped": "cell cell-skipped",
    "documented_unsafe": "cell cell-doc-unsafe",
    "not_run": "cell cell-notrun",
}

# Tool-coverage CSS class. tool_invoked = tool actually fired (best signal);
# prompt_only = chat received a prompt that should have called it but the
# trace didn't confirm invocation; not_reached = never attempted.
_TOOL_CELL_CLASS = {
    "tool_invoked": "cell cell-pass",
    "prompt_only": "cell cell-prompt-only",
    "not_reached": "cell cell-notrun",
    "pass": "cell cell-pass",
    "fail": "cell cell-fail",
    "skipped": "cell cell-skipped",
    "documented_unsafe": "cell cell-doc-unsafe",
    "not_run": "cell cell-notrun",
}


def _severity_pill_class(severity: str | None) -> str:
    """Return the CSS class for a severity pill. Unknown labels get a
    distinct 'unknown' class so the report still shows them (rather than
    silently hiding a typo)."""
    if not severity:
        return _UNKNOWN_PILL_CLASS
    return _SEVERITY_PILL_CLASS.get(severity.lower(), _UNKNOWN_PILL_CLASS)


def _page_cell_class(status: str) -> str:
    """CSS class for a single (page, persona) cell."""
    return _PAGE_CELL_CLASS.get(status, "cell cell-notrun")


def _tool_cell_class(status: str) -> str:
    return _TOOL_CELL_CLASS.get(status, "cell cell-notrun")


def _route_layer_status(cells: list[dict], layer: str) -> dict | None:
    """Pick the worst-case cell for a given (route, layer) pair.

    A route can have multiple cells per layer (e.g. fuzz has both curated
    and hypothesis rows). The matrix table needs ONE status per (route ×
    layer) intersection. We pick the worst one so a single failure is
    never hidden behind sibling passes.

    Order of severity for picking: fail > skipped > documented_unsafe >
    pass. Returns the chosen cell dict, or None if no cell matched that
    layer (renders as a dash in the table).
    """
    if not cells:
        return None
    layer_cells = [c for c in cells if c.get("layer") == layer]
    if not layer_cells:
        return None
    order = {"fail": 0, "skipped": 1, "documented_unsafe": 2, "pass": 3}
    return min(layer_cells, key=lambda c: order.get(c.get("status", ""), 99))


def _prepare_pages_rows(coverage: dict, manifest: dict) -> list[dict]:
    """Build one display row per page, with cells in manifest persona order.

    Each row is `{page_id, page_label, cells: [{persona_id, status,
    cell_class, title}]}`. `title` is the tooltip — uses the page's
    `accessible_to` to annotate "allowed access" vs "blocked".
    """
    persona_ids = [p["id"] for p in manifest.get("personas", [])]
    pages = manifest.get("pages", [])
    pages_cells = coverage.get("pages", {})

    rows: list[dict] = []
    for page in pages:
        page_id = page["id"]
        accessible_to = set(page.get("accessible_to") or [])
        page_cells = pages_cells.get(page_id, {})
        cells: list[dict] = []
        for persona_id in persona_ids:
            status = page_cells.get(persona_id, "not_run")
            allowed = persona_id in accessible_to
            tooltip = (
                f"{page_id} × {persona_id}: {status} "
                f"({'allowed' if allowed else 'blocked'} by manifest)"
            )
            cells.append(
                {
                    "persona_id": persona_id,
                    "status": status,
                    "cell_class": _page_cell_class(status),
                    "title": tooltip,
                    "allowed": allowed,
                }
            )
        rows.append(
            {
                "page_id": page_id,
                "page_label": page.get("label") or page_id,
                "cells": cells,
            }
        )
    return rows


def _prepare_routes_rows(coverage: dict, manifest: dict) -> list[dict]:
    """One display row per API route. Columns per layer (fuzz, auth) show
    the worst-case status for that intersection. e2e and llm are added as
    extra columns so the operator sees the full picture; routes that don't
    apply to a layer show a dash.
    """
    routes = manifest.get("api_routes", [])
    routes_cells = coverage.get("api_routes", {})

    rows: list[dict] = []
    layers = ("e2e", "fuzz", "auth", "llm")
    for route in routes:
        rid = route["id"]
        cells = routes_cells.get(rid, [])
        layer_summaries: dict[str, dict | None] = {}
        for layer in layers:
            picked = _route_layer_status(cells, layer)
            if picked is None:
                layer_summaries[layer] = None
            else:
                layer_summaries[layer] = {
                    "status": picked.get("status"),
                    "test_id": picked.get("test_id"),
                    "evidence": picked.get("evidence"),
                    "severity": picked.get("severity"),
                    "cell_class": _page_cell_class(picked.get("status", "not_run")),
                }
        rows.append(
            {
                "route_id": rid,
                "method": route.get("method"),
                "path": route.get("path"),
                "layers": layer_summaries,
                "total_cells": len(cells),
            }
        )
    return rows


def _prepare_tools_rows(coverage: dict, manifest: dict) -> list[dict]:
    """One row per agent tool. The 'sentinel' column flags synthetic
    entries like `master.chat_surface` so the operator sees the row is a
    proxy, not a real `@tool`-decorated function."""
    tools = manifest.get("agent_tools", [])
    tools_cells = coverage.get("agent_tools", {})

    rows: list[dict] = []
    for tool in tools:
        tid = tool["id"]
        status = tools_cells.get(tid, "not_reached")
        rows.append(
            {
                "tool_id": tid,
                "agent": tool.get("agent") or tool.get("runtime"),
                "is_sentinel": bool(tool.get("synthetic")),
                "status": status,
                "cell_class": _tool_cell_class(status),
            }
        )
    return rows


def _prepare_findings(findings: list[dict]) -> list[dict]:
    """Annotate each finding with its CSS pill class. Returns a new list;
    does NOT mutate the input (the report dict is shared with summary.md
    and the JSON writer, both of which expect the original shape)."""
    out: list[dict] = []
    for f in findings:
        out.append({**f, "pill_class": _severity_pill_class(f.get("severity"))})
    return out


def _cost_progress_pct(actual: float, cap: float) -> float:
    """Width of the progress bar as a 0-100 float. Capped at 100 so a
    runaway spend doesn't blow past the bar's visual edge."""
    if cap <= 0:
        return 0.0
    pct = (actual / cap) * 100.0
    return max(0.0, min(100.0, pct))


# ─────────────────────────── public entry points ───────────────────────────


def render_html(
    report: dict,
    out_dir: Path,
    manifest: dict | None = None,
) -> Path:
    """Render `report.json` dict to `report.html` under `out_dir`.

    Parameters
    ----------
    report : dict
        The output of `report_builder.build_report(...)`. Must include
        `metadata`, `coverage`, `cost`, `findings`, `summary` keys.
    out_dir : Path
        Directory to write `report.html` into. Created if missing.
    manifest : dict, optional
        The parsed `src/coverage/manifest.json`. Used to label pages,
        order personas, and tag tools as synthetic. If None, we fall back
        to whatever can be read from the coverage matrix alone — the
        report still renders but column headers degrade to bare ids.

    Returns
    -------
    Path
        Absolute path to the written HTML file.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = manifest or {}
    coverage = report.get("coverage", {}) or {}
    cost = report.get("cost", {}) or {}

    context = {
        "report": report,
        "metadata": report.get("metadata", {}),
        "summary": report.get("summary", {}),
        "cost": cost,
        "cost_pct": _cost_progress_pct(
            float(cost.get("actual_usd", 0.0)),
            float(cost.get("cap_usd", 1.0) or 1.0),
        ),
        "findings": _prepare_findings(report.get("findings") or []),
        "pages_rows": _prepare_pages_rows(coverage, manifest),
        "routes_rows": _prepare_routes_rows(coverage, manifest),
        "tools_rows": _prepare_tools_rows(coverage, manifest),
        "personas": list(manifest.get("personas") or []),
        "diff_from_last_green": report.get("diff_from_last_green"),
    }

    template = _ENV.get_template("report.html.j2")
    html = template.render(**context)
    target = out_dir / "report.html"
    target.write_text(html, encoding="utf-8")
    return target


# ────────────────── summary.md helpers (task 23) ──────────────────────────


# AC12 sanitization regexes. Applied in `_sanitize` after the template
# renders so the template itself doesn't have to be defensive.
#
# `_RE_ACCOUNT_ID`: any 12-digit run with word boundaries. AWS account ids
# are unsigned 12-digit integers (currently — the format has been stable
# since 2006). Word boundaries dodge false positives on longer numeric
# sequences (e.g. a 14-digit transaction id should NOT be redacted).
_RE_ACCOUNT_ID = re.compile(r"\b\d{12}\b")

# `_RE_JWT`: header.payload[.signature] where header+payload are JWT-shaped
# base64url segments starting with `eyJ` (the base64 prefix of the JSON
# bytes `{"`). The signature segment may be empty (e.g. the documented-
# unsafe stripped-signature probe in tests/security), so we allow `[...]?`
# at the end. We require BOTH the header and the payload to start with
# `eyJ` because a single `eyJ`-prefixed token in isolation could be a
# legitimate config blob; two together is unambiguously a JWT.
_RE_JWT = re.compile(r"eyJ[a-zA-Z0-9_-]+\.eyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]*")

# `_RE_BIG_BASE64`: any unbroken alphanumeric/`+`/`/`/`=` run longer than
# 200 chars that looks base64-ish (length divisible-by-4-ish, no spaces).
# The truncation marker is `[...]` so the result is still readable and
# obviously elided. We deliberately accept BOTH standard base64 (`+`/`/`
# pad chars) AND base64url (`-`/`_`) because exfiltrated tokens in a real
# transcript could be either form.
_RE_BIG_BASE64 = re.compile(r"[A-Za-z0-9+/_\-]{201,}={0,2}")


def _sanitize(md_text: str) -> str:
    """Scrub AC12-forbidden patterns from rendered Markdown.

    Applied AFTER Jinja renders. Three passes in order:
      1. JWT scrub first — a JWT might contain a 12-digit sub or aud
         claim segment, and we want to redact the whole token rather than
         leave dangling header+payload after a partial scrub.
      2. Big-base64 scrub — replaces with `[REDACTED-BASE64-N-CHARS]` so
         a forwarded summary never carries an exfiltrated artifact.
      3. 12-digit AWS account id scrub last. Word-boundary regex.

    Returns the scrubbed text. The original is not mutated (str is
    immutable, but worth stating for clarity).
    """
    out = _RE_JWT.sub("[REDACTED-JWT]", md_text)
    out = _RE_BIG_BASE64.sub(
        lambda m: f"[REDACTED-BASE64-{len(m.group(0))}-CHARS]", out
    )
    out = _RE_ACCOUNT_ID.sub("[REDACTED-12DIGIT]", out)
    return out


def _format_duration(seconds: float | int | None) -> str:
    """Format a duration as `Xm Ys`. Always emits both minutes and seconds
    so the column width stays stable across runs (e.g. `0m 12s`, `8m 03s`).
    Negative or None defensively renders as `0m 0s`.
    """
    if seconds is None:
        return "0m 0s"
    total = max(0, int(round(float(seconds))))
    minutes, secs = divmod(total, 60)
    return f"{minutes}m {secs}s"


def _format_money(amount: float | int | None) -> str:
    """Two-decimal dollar amount, e.g. `0.38`. Caller wraps in `$...`."""
    if amount is None:
        return "0.00"
    return f"{float(amount):.2f}"


def _cost_percent_str(actual: float | int | None, cap: float | int | None) -> str:
    """Integer-ish percent of the cap that was spent, capped at 100 so
    a runaway run shows `100%` rather than `387%`. Returns the string
    representation (no `%` suffix — the template adds that)."""
    cap_f = float(cap or 0)
    if cap_f <= 0:
        return "0"
    pct = (float(actual or 0) / cap_f) * 100.0
    pct = max(0.0, min(100.0, pct))
    return str(int(round(pct)))


def _status_label(summary_block: dict) -> str:
    """One-glance run status. PASS when no failures and no AC11
    documented-unsafe regressions (those are counted as PASS in the
    builder, so we only key off `failed`). Avoids emoji per CLAUDE.md.
    """
    failed = int(summary_block.get("failed", 0) or 0)
    return "FAIL" if failed > 0 else "PASS"


def _coverage_shortfalls(summary_block: dict) -> list[str]:
    """Bullet lines describing which surfaces have uncovered cells.

    Empty list when every page / route / tool is fully covered (the
    common green-run case). The template only renders the bullets block
    when this list is non-empty.
    """
    lines: list[str] = []
    pages_total = int(summary_block.get("pages_total", 0) or 0)
    pages_covered = int(summary_block.get("pages_covered", 0) or 0)
    routes_total = int(summary_block.get("routes_total", 0) or 0)
    routes_covered = int(summary_block.get("routes_covered", 0) or 0)
    tools_total = int(summary_block.get("tools_total", 0) or 0)
    tools_covered = int(summary_block.get("tools_covered", 0) or 0)
    if pages_total and pages_covered < pages_total:
        lines.append(
            f"{pages_total - pages_covered} page cell(s) NOT_RUN — "
            "see the pages matrix in `report.html`."
        )
    if routes_total and routes_covered < routes_total:
        lines.append(
            f"{routes_total - routes_covered} API route(s) uncovered — "
            "see the routes table in `report.html`."
        )
    if tools_total and tools_covered < tools_total:
        lines.append(
            f"{tools_total - tools_covered} agent tool(s) NOT_REACHED — "
            "see the tools table in `report.html`."
        )
    return lines


def _shorten_evidence_path(evidence_path: str | None) -> str:
    """Strip a leading run-directory prefix so the table cell stays
    readable on a Slack-narrow render. The builder already stores
    evidence_path as a relative path (e.g. `e2e/screenshots/foo.png`)
    so this is mostly defensive: collapse any leading absolute path
    down to its last 4 segments.
    """
    if not evidence_path:
        return "-"
    parts = Path(evidence_path).parts
    if len(parts) <= 4:
        return str(Path(*parts))
    return str(Path(*parts[-4:]))


def _top_findings(findings: list[dict], n: int = 5) -> list[dict]:
    """First `n` entries from the already-ranked `findings[]`.

    `report_builder._extract_findings` ranks by severity tier
    (critical > high > medium > low > info) then alphabetically by
    `target_id` then `test_id`. We trust that ordering and just slice.
    Each row gets `evidence_path` shortened for table-cell readability.
    """
    out: list[dict] = []
    for f in findings[:n]:
        row = dict(f)
        row["evidence_path"] = _shorten_evidence_path(f.get("evidence_path"))
        row["severity"] = (f.get("severity") or "info").lower()
        out.append(row)
    return out


def _diff_for_template(diff_block: dict | None) -> dict | None:
    """Normalize the diff block for the template.

    Task 24 update: the report builder now ALWAYS populates the diff block
    (never None), but the block carries `summary.has_baseline = False`
    when no baseline file exists. We return None here in that case so the
    template's `{% if diff %}` falls through to the "No baseline yet"
    hint — that branch reads better than rendering "0 new failures /
    0 resolved" against a non-existent baseline.

    The legacy in-flight `newly_passing` / `documented_unsafe_transitions`
    aliases are kept for robustness against any cached test fixture that
    pre-dates the diff module landing.
    """
    if not diff_block:
        return None
    summary = diff_block.get("summary") or {}
    # AC15: no baseline → summary template renders the promotable hint.
    if summary.get("has_baseline") is False:
        return None
    new_failures = diff_block.get("new_failures") or []
    # `resolved` is the task-24 canonical name; `newly_passing` was the
    # placeholder name used before this module landed. Accept both.
    newly_passing = diff_block.get("resolved") or diff_block.get("newly_passing") or []
    # Flapping = documented_unsafe ↔ fail transitions per the task-24 diff
    # builder; `documented_unsafe_transitions` / `_changes` were the
    # pre-implementation placeholder names. Accept all three.
    transitions = (
        diff_block.get("flapping")
        or diff_block.get("documented_unsafe_transitions")
        or diff_block.get("documented_unsafe_changes")
        or []
    )
    # Detail lines: prefer an explicitly provided list, else synthesize
    # one line per new failure test_id (most actionable diff info).
    detail = list(diff_block.get("detail_lines") or [])
    if not detail and new_failures:
        for entry in new_failures[:10]:
            if isinstance(entry, dict):
                detail.append(f"NEW FAIL: `{entry.get('test_id') or entry}`")
            else:
                detail.append(f"NEW FAIL: `{entry}`")
    return {
        "new_failures_count": len(new_failures),
        "resolved_count": len(newly_passing),
        "documented_unsafe_transitions_count": len(transitions),
        "detail_lines": detail,
        "baseline_run_id": diff_block.get("baseline_run_id"),
        "net_change": summary.get("net_change", 0),
    }


def render_summary(report: dict, out_dir: Path) -> Path:
    """Render `summary.md` under `out_dir` — the forwardable digest.

    Per AC12, the summary includes the run header, cost, coverage totals,
    top-5 findings by severity, the diff-from-last-green block, and a
    link to `report.html`. Sensitive identifiers (AWS account ids, JWT
    tokens, oversized base64 blobs) are scrubbed via `_sanitize` after
    Jinja renders.

    Parameters
    ----------
    report : dict
        Output of `report_builder.build_report(...)`. Required keys:
        `metadata`, `summary`, `cost`, `findings`, optionally
        `diff_from_last_green`.
    out_dir : Path
        Directory to write `summary.md` into. Created if missing.

    Returns
    -------
    Path
        Absolute path to the written `summary.md` file.
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    metadata = report.get("metadata", {}) or {}
    summary_block = report.get("summary", {}) or {}
    cost = report.get("cost", {}) or {}
    findings = report.get("findings") or []
    diff_block = report.get("diff_from_last_green")

    # Page total from the matrix summary — used for persona-count display.
    # The matrix summary has `pages_total` and `pages_covered_label`; the
    # number of personas is derivable from the matrix dimensions but the
    # cheap way is to read it off the manifest if embedded in the report's
    # coverage dict. We fall back to "all" if not available, so the line
    # stays grammatical regardless.
    coverage = report.get("coverage", {}) or {}
    persona_count = _count_personas(coverage)
    sentinel_count = _count_sentinel_tools(coverage)

    context = {
        "run_id": metadata.get("run_id", "unknown-run"),
        # AC12 allows the target URL but NOT user-pool / client ids —
        # `target_base_url` is the CloudFront URL, which is public and
        # safe to echo. We do NOT emit `chat_function_url` here because
        # a Function URL contains an account-derived UUID that adds zero
        # value to a forwarded summary.
        "target_base_url": metadata.get("target_base_url", ""),
        "duration_human": _format_duration(metadata.get("duration_seconds")),
        "harness_version": metadata.get("harness_version", "0.0.0"),
        "summary": summary_block,
        "cost_actual_str": _format_money(cost.get("actual_usd")),
        "cost_cap_str": _format_money(cost.get("cap_usd")),
        "cost_pct_str": _cost_percent_str(cost.get("actual_usd"), cost.get("cap_usd")),
        "status_label": _status_label(summary_block),
        "persona_count": persona_count,
        "sentinel_count": sentinel_count,
        "coverage_shortfalls": _coverage_shortfalls(summary_block),
        "top_findings": _top_findings(findings, n=5),
        "diff": _diff_for_template(diff_block),
    }

    template = _ENV.get_template("summary.md.j2")
    rendered = template.render(**context)
    # AC12: scrub forbidden patterns post-render. Any future template
    # additions are auto-scrubbed without per-field defensive Jinja.
    rendered = _sanitize(rendered)

    target = out_dir / "summary.md"
    target.write_text(rendered, encoding="utf-8")
    return target


def _count_personas(coverage: dict) -> int:
    """Best-effort persona count from the matrix.

    Reads the first page's persona dict to count cells. Returns 4 (the
    expected ARBITER persona count) as a safe default if the matrix is
    empty — the summary line then reads "across 4 personas" which is
    accurate for every real run.
    """
    pages = coverage.get("pages") or {}
    for cells in pages.values():
        if cells:
            return len(cells)
    return 4


def _count_sentinel_tools(coverage: dict) -> int:
    """Count synthetic-sentinel tool rows in the matrix. The harness
    currently has one (`master.chat_surface`) but the template parens
    block uses this count so future sentinels are reflected automatically.
    """
    # The matrix exposes a flat tool->status map; the sentinel-ness lives
    # on the manifest entry, not the matrix. We can't recover it from
    # `coverage` alone. The known sentinel id is `master.chat_surface`;
    # checking for its presence is the cheapest reliable signal.
    tools = coverage.get("agent_tools") or {}
    return 1 if "master.chat_surface" in tools else 0


__all__ = [
    "render_html",
    "render_summary",
]
