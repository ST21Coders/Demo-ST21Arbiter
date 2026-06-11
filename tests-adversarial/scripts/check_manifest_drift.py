"""scripts/check_manifest_drift.py — fail the run if manifest.json is out of sync with source.

Run: python -m scripts.check_manifest_drift

Exit codes:
  0 — manifest matches source tree (match summary printed to stdout).
  1 — drift detected (drift summary printed to stderr).
  2 — manifest file not found at the resolved path, or its contents are not
      valid JSON (error message printed to stderr, no source-tree scan attempted).

Purpose: catch the case where the real ARBITER source tree has gained or lost a
page, API route, or agent tool that isn't reflected in
src/coverage/manifest.json. The harness must fail loudly if the manifest is
stale, so the coverage matrix in the report (AC6/7/8) cannot silently
undercount.

Checks (all stdlib-only — re, json, pathlib, sys, argparse):

  1. Pages.    Glob ui/src/pages/*.jsx vs manifest pages[].file.
  2. Routes.   Parse Infra/functions/api_handler/api_handler.py inside _route()
               (the literal `if path == "..."` and nested `path.startswith("...")`
               blocks between roughly lines 130-216) vs manifest
               api_routes[].(method, path).
  3. Tools.    For each in-repo agent (master_orchestrator, sharepoint_specialist,
               awsconfig_specialist, zscaler_specialist), find every `@tool`-
               decorated function name in agents/<name>/agent.py vs manifest
               agent_tools[] filtered by agent.
  4. jira.     The jira_specialist runtime has source_in_repo: false per
               CLAUDE.md (off-limits list). We only confirm that flag is intact;
               we do not try to read its non-existent agent.py.

False-positive defenses:

  - Route regex anchors to `if`/`elif` statements (whitespace-preceded), not
    to literals appearing inside comments or docstrings.
  - The `@tool` decorator match requires the decorator to be the immediately
    preceding non-blank line of a `def <name>(` line.

Both manifest path-template params (`{id}`, `{cr_id}`, etc.) and source-side
`path.startswith("/foo/")` shapes are normalized to `/foo/{}` placeholders so
the two sides compare cleanly.

Public surface:
  run(repo_root=None, manifest_path=None) -> tuple[int, str, str]
    Returns (exit_code, stdout_text, stderr_text). Pure function — no
    sys.exit / printing — so tests can capture both streams directly.

  main(argv=None) -> int
    CLI entrypoint. Prints stdout to sys.stdout, stderr to sys.stderr, returns
    the exit code for sys.exit().
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# ──────────────────────────── repo layout ──────────────────────────────────
# This file lives at .../tests-adversarial/scripts/check_manifest_drift.py
#   parents[0] = scripts
#   parents[1] = tests-adversarial
#   parents[2] = <repo root>
_DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_MANIFEST = (
    Path(__file__).resolve().parents[1] / "src" / "coverage" / "manifest.json"
)

# The four in-repo agents. jira_specialist is intentionally excluded
# (source_in_repo: false per CLAUDE.md off-limits list).
_IN_REPO_AGENTS = (
    "master_orchestrator",
    "sharepoint_specialist",
    "awsconfig_specialist",
    "zscaler_specialist",
)


# ─────────────────────────── source extractors ─────────────────────────────


def _glob_pages(repo_root: Path) -> set[str]:
    """Return the set of repo-relative page file paths under ui/src/pages/.

    Result entries look like ``ui/src/pages/Dashboard.jsx``. Sorted-comparable
    via plain string ordering.
    """
    pages_dir = repo_root / "ui" / "src" / "pages"
    found: set[str] = set()
    for jsx in pages_dir.glob("*.jsx"):
        rel = jsx.relative_to(repo_root)
        # Force forward-slash separators so the comparison matches manifest
        # entries (which are POSIX-style regardless of OS).
        found.add(rel.as_posix())
    return found


def _read_api_handler(repo_root: Path) -> str:
    """Read api_handler.py as text. Wrapped for test monkey-patching."""
    return (
        repo_root / "Infra" / "functions" / "api_handler" / "api_handler.py"
    ).read_text(encoding="utf-8")


def _read_agent(repo_root: Path, agent: str) -> str:
    """Read agents/<agent>/agent.py as text. Wrapped for test monkey-patching."""
    return (repo_root / "agents" / agent / "agent.py").read_text(encoding="utf-8")


# ─────────────────────── route extraction (api_handler.py) ─────────────────
#
# Two source patterns the parser handles:
#
#   (A) Single-line `if`/`elif` with optional method clause:
#         if path == "/foo":                             -> (GET, /foo) by convention
#         if path == "/foo" and method == "POST":        -> (POST, /foo)
#         elif method == "GET" and path == "/foo":       -> (GET,  /foo)
#         if path.startswith("/foo/") and method == "X": -> (X,    /foo/{})
#
#   (B) `path.startswith("/foo/"):` block — possibly without an inline method
#       clause — that contains nested `if sub == "bar" and method == "X":`
#       (or `if not sub and method == "X":` for the bare /foo/<id> shape).
#       We walk forward from the startswith line to the next top-level `if `
#       statement at the same indent (or end of `_route()`), and collect
#       (method, path) pairs from the nested checks.

_RE_PATH_EQ = re.compile(
    r"""
    ^\s*(?:if|elif)\s+              # if/elif at any indent
    (?:                              # method-first or path-first
        path\s*==\s*"(?P<path1>[^"]+)"\s*
        (?:and\s+method\s*==\s*"(?P<method1>[A-Z]+)")?
        |
        method\s*==\s*"(?P<method2>[A-Z]+)"\s*
        and\s+path\s*==\s*"(?P<path2>[^"]+)"
    )
    \s*:\s*$
    """,
    re.VERBOSE,
)

_RE_PATH_STARTSWITH_INLINE = re.compile(
    r"""
    ^\s*(?:if|elif)\s+
    path\.startswith\(\s*"(?P<path>[^"]+)"\s*\)
    (?:\s+and\s+method\s*==\s*"(?P<method>[A-Z]+)")?
    \s*:\s*$
    """,
    re.VERBOSE,
)

_RE_NESTED_SUB_AND_METHOD = re.compile(
    r"""
    ^\s*(?:if|elif)\s+
    (?:
        sub\s*==\s*"(?P<sub1>[^"]+)"\s*
        (?:and\s+method\s*==\s*"(?P<method1>[A-Z]+)")?
        |
        not\s+sub\s+and\s+method\s*==\s*"(?P<method2>[A-Z]+)"
        |
        sub\s*==\s*"(?P<sub3>[^"]+)"\s*$
    )
    \s*:?\s*$
    """,
    re.VERBOSE,
)


def _normalize_template(path: str) -> str:
    """Replace any ``{param}`` segment with the placeholder ``{}``.

    >>> _normalize_template("/findings/{conflict_id}")
    '/findings/{}'
    >>> _normalize_template("/actions/{cr_id}/approve")
    '/actions/{}/approve'
    >>> _normalize_template("/health")
    '/health'
    """
    return re.sub(r"\{[^}]+\}", "{}", path)


def _slice_route_body(source: str) -> list[str]:
    """Return the lines of api_handler.py from the start of ``def handler(`` to
    the next top-level ``def`` (exclusive). This is the only region we scan,
    which makes the route extraction immune to literal-route strings that
    happen to appear in unrelated helper functions or module-level constants.
    """
    lines = source.splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.match(r"^def\s+handler\s*\(", line):
            start = i
            break
    if start is None:
        return []
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if re.match(r"^def\s+\w+\s*\(", lines[j]):
            end = j
            break
    return lines[start:end]


def _extract_routes_from_source(source: str) -> set[tuple[str, str]]:
    """Parse api_handler.py text and return ``{(method, normalized_path), ...}``.

    Handles three source shapes (see module docstring "Two source patterns"
    comment): literal ``path == "/X"`` (optionally with method), inline
    ``path.startswith("/X/")`` (with method), and ``path.startswith("/X/"):``
    blocks whose nested ``sub == "Y"`` checks expand into
    ``(method, /X/{}/Y)`` tuples.

    Default method for a bare ``path == "/foo":`` (no method clause) is
    ``GET`` — matches the project's only such case, ``/health``.
    """
    routes: set[tuple[str, str]] = set()
    body = _slice_route_body(source)
    if not body:
        return routes

    i = 0
    while i < len(body):
        line = body[i]

        m = _RE_PATH_EQ.match(line)
        if m:
            path = m.group("path1") or m.group("path2")
            method = m.group("method1") or m.group("method2") or "GET"
            routes.add((method, _normalize_template(path)))
            i += 1
            continue

        m = _RE_PATH_STARTSWITH_INLINE.match(line)
        if m:
            prefix = m.group("path").rstrip("/") + "/{}"
            inline_method = m.group("method")
            # Walk forward at the inner indent and collect any nested
            # `sub == "Y"` (with or without `and method == "M"`) patterns.
            # If we find nested subs, they are what the route really is — the
            # inline method on the outer guard is the *block-level* method
            # constraint (e.g. /actions/{} block guarded by POST, then
            # sub == "approve" / "reject" etc. inside). If we find none, the
            # outer line itself is the route — but only if it had an inline
            # method (a bare `path.startswith("/X/"):` block with no inner
            # method clauses is not a complete handler shape we recognize).
            base_indent = len(line) - len(line.lstrip())
            nested_routes: set[tuple[str, str]] = set()
            j = i + 1
            while j < len(body):
                inner = body[j]
                stripped = inner.strip()
                if not stripped or stripped.startswith("#"):
                    j += 1
                    continue
                indent = len(inner) - len(inner.lstrip())
                if indent <= base_indent and re.match(
                    r"(if|elif|def|return)\b", stripped
                ):
                    # Left the startswith block.
                    break
                nm = _RE_NESTED_SUB_AND_METHOD.match(inner)
                if nm:
                    nested_method = (
                        nm.group("method1") or nm.group("method2") or inline_method
                    )
                    if not nested_method:
                        j += 1
                        continue
                    sub = nm.group("sub1") or nm.group("sub3")
                    if sub:
                        nested_routes.add((nested_method, f"{prefix}/{sub}"))
                    else:  # `not sub and method == ...`
                        nested_routes.add((nested_method, prefix))
                j += 1
            if nested_routes:
                routes |= nested_routes
            elif inline_method:
                # No nested subs — single-line handler form:
                # `if path.startswith("/X/") and method == "M":`
                routes.add((inline_method, prefix))
            i = j
            continue

        i += 1

    return routes


# ─────────────────────── tool extraction (agent.py) ────────────────────────


def _extract_tools_from_agent_source(source: str) -> set[str]:
    """Return the set of ``@tool``-decorated function names from an agent.py.

    Strands' ``@tool`` decorator is typically applied bare (no arguments) on
    the line immediately before ``def funcname(...)``. We require:

      - A line that is exactly ``@tool`` (after stripping whitespace), OR
        ``@tool(...)`` with any arg list — both forms count.
      - The next non-blank, non-comment line begins ``def <name>(``.

    Lines inside string literals are not specially handled, because in
    practice ``@tool`` doesn't appear in any docstring in this codebase. The
    module docstring comment ``# @tool wrappers...`` in master_orchestrator
    is correctly skipped because the parser anchors on a *line* equal to
    ``@tool`` (not a substring match).
    """
    names: set[str] = set()
    lines = source.splitlines()

    decorator_re = re.compile(r"^@tool(?:\s*\(.*\))?\s*$")
    def_re = re.compile(r"^def\s+(?P<name>[A-Za-z_][A-Za-z_0-9]*)\s*\(")

    for idx, raw in enumerate(lines):
        if not decorator_re.match(raw.strip()):
            continue
        # Walk forward to the next non-blank, non-comment, non-decorator line.
        j = idx + 1
        while j < len(lines):
            nxt = lines[j].strip()
            if not nxt or nxt.startswith("#"):
                j += 1
                continue
            if nxt.startswith("@"):
                # Stacked decorators above the def — keep walking.
                j += 1
                continue
            break
        if j >= len(lines):
            continue
        m = def_re.match(lines[j].lstrip())
        if m:
            names.add(m.group("name"))

    return names


# ─────────────────────── manifest-side extractors ──────────────────────────


def _manifest_pages(manifest: dict) -> set[str]:
    """Return the set of `pages[].file` entries from the manifest.

    Synthetic page entries (carrying ``synthetic: true``) are harness-only
    sentinels — e.g. ``spa-root`` for Block D bundle scans — that don't map
    to a ``ui/src/pages/*.jsx`` file. They're skipped here so the drift
    checker doesn't report them as "manifest entries missing in source".
    """
    return {
        p["file"]
        for p in manifest.get("pages", [])
        if p.get("file") and p.get("synthetic") is not True
    }


def _manifest_routes(manifest: dict) -> set[tuple[str, str]]:
    """Return the set of `(method, normalized_path)` from manifest api_routes.

    Synthetic route entries (carrying ``synthetic: true``) are harness-only
    sentinels — e.g. ``cognito-initiate-auth`` for the brute-force test's
    target_id binding — that don't map to any line in ``api_handler.py``.
    They're skipped here so the drift checker doesn't report them as
    "manifest routes missing in source".
    """
    out: set[tuple[str, str]] = set()
    for r in manifest.get("api_routes", []):
        if r.get("synthetic") is True:
            continue
        method = (r.get("method") or "").upper()
        path = r.get("path") or ""
        if not method or not path:
            continue
        out.add((method, _normalize_template(path)))
    return out


def _manifest_tools(manifest: dict) -> dict[str, set[str]]:
    """Return ``{agent_name: {tool_name, ...}}`` for in-repo agents only.

    The jira_specialist agent is skipped here because its source is not in the
    repo (CLAUDE.md off-limits list); its black-box manifest entry is
    validated separately in ``_check_jira_blackbox_intact``.

    Synthetic entries (``synthetic: true``) are also skipped: they are
    harness-only sentinels (e.g. ``master.chat_surface`` for LLM red-team
    coverage rows) that intentionally have no source-tree counterpart, so
    they would otherwise show up as ``tools_removed`` drift on every run.
    """
    out: dict[str, set[str]] = {a: set() for a in _IN_REPO_AGENTS}
    for t in manifest.get("agent_tools", []):
        if t.get("synthetic") is True:
            continue
        agent = t.get("agent")
        name = t.get("tool_name")
        if agent in out and name:
            out[agent].add(name)
    return out


def _check_jira_blackbox_intact(manifest: dict) -> str | None:
    """Confirm the manifest still treats jira_specialist as black-box.

    Returns ``None`` if intact, or an error string if the runtime's
    ``source_in_repo`` flag has been flipped to ``true`` (which would mean
    someone added jira_specialist source — at which point this drift checker
    needs to be updated to actually scan it). Also returns an error if the
    runtime is missing entirely.
    """
    for r in manifest.get("runtimes", []):
        if r.get("id") == "jira_specialist":
            if r.get("source_in_repo") is False:
                return None
            return (
                "jira_specialist runtime's source_in_repo flag is no longer "
                "false — if source was added to this repo, update "
                "check_manifest_drift.py to scan it."
            )
    return "manifest is missing the jira_specialist runtime entry (expected source_in_repo: false)."


# ───────────────────────── drift reporting ────────────────────────────────


def _fmt_routes(routes: set[tuple[str, str]]) -> list[str]:
    """Sort routes for stable diff output: by path then method."""
    return [f"{m} {p}" for p, m in sorted((p, m) for (m, p) in routes)]


def _format_drift(
    pages_added: set[str],
    pages_removed: set[str],
    routes_added: set[tuple[str, str]],
    routes_removed: set[tuple[str, str]],
    tools_added: dict[str, set[str]],
    tools_removed: dict[str, set[str]],
    jira_error: str | None,
) -> str:
    """Render a human-readable drift summary for stderr. Empty string if no drift."""
    lines: list[str] = []

    if pages_added or pages_removed:
        if pages_added:
            lines.append("  PAGES added in source, missing in manifest:")
            for f in sorted(pages_added):
                lines.append(f"    {f}")
        if pages_removed:
            lines.append("  PAGES removed from source, present in manifest:")
            for f in sorted(pages_removed):
                lines.append(f"    {f}")

    if routes_added or routes_removed:
        if routes_added:
            lines.append("  API_ROUTES added in source, missing in manifest:")
            for r in _fmt_routes(routes_added):
                lines.append(f"    {r}")
        if routes_removed:
            lines.append("  API_ROUTES removed from source, present in manifest:")
            for r in _fmt_routes(routes_removed):
                lines.append(f"    {r}")

    if any(tools_added.values()) or any(tools_removed.values()):
        for agent in _IN_REPO_AGENTS:
            added = sorted(tools_added.get(agent, set()))
            removed = sorted(tools_removed.get(agent, set()))
            if added:
                lines.append(
                    f"  AGENT_TOOLS added in source, missing in manifest ({agent}):"
                )
                for name in added:
                    lines.append(f"    {agent}.{name}")
            if removed:
                lines.append(
                    f"  AGENT_TOOLS removed from source, present in manifest ({agent}):"
                )
                for name in removed:
                    lines.append(f"    {agent}.{name}")

    if jira_error:
        lines.append(f"  JIRA_SPECIALIST: {jira_error}")

    if not lines:
        return ""
    return "manifest drift detected:\n" + "\n".join(lines) + "\n"


# ───────────────────────────── entry points ───────────────────────────────


def run(
    repo_root: Path | None = None,
    manifest_path: Path | None = None,
) -> tuple[int, str, str]:
    """Run the drift check.

    Returns (exit_code, stdout, stderr). The function is pure (no print, no
    sys.exit) so callers — including tests — can capture both streams.
    """
    repo_root = repo_root or _DEFAULT_REPO_ROOT
    manifest_path = manifest_path or _DEFAULT_MANIFEST

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return 2, "", f"manifest not found at {manifest_path}\n"
    except json.JSONDecodeError as exc:
        return 2, "", f"manifest at {manifest_path} is not valid JSON: {exc}\n"

    # ── Pages ─────────────────────────────────────────────────────────────
    src_pages = _glob_pages(repo_root)
    manifest_pages = _manifest_pages(manifest)
    pages_added = src_pages - manifest_pages
    pages_removed = manifest_pages - src_pages

    # ── Routes ────────────────────────────────────────────────────────────
    api_handler_text = _read_api_handler(repo_root)
    src_routes = _extract_routes_from_source(api_handler_text)
    manifest_routes = _manifest_routes(manifest)
    routes_added = src_routes - manifest_routes
    routes_removed = manifest_routes - src_routes

    # ── Tools ─────────────────────────────────────────────────────────────
    src_tools: dict[str, set[str]] = {}
    for agent in _IN_REPO_AGENTS:
        src_tools[agent] = _extract_tools_from_agent_source(
            _read_agent(repo_root, agent)
        )
    manifest_tools = _manifest_tools(manifest)
    tools_added = {a: src_tools[a] - manifest_tools[a] for a in _IN_REPO_AGENTS}
    tools_removed = {a: manifest_tools[a] - src_tools[a] for a in _IN_REPO_AGENTS}

    # ── jira_specialist black-box invariant ───────────────────────────────
    jira_error = _check_jira_blackbox_intact(manifest)

    drift_text = _format_drift(
        pages_added=pages_added,
        pages_removed=pages_removed,
        routes_added=routes_added,
        routes_removed=routes_removed,
        tools_added=tools_added,
        tools_removed=tools_removed,
        jira_error=jira_error,
    )

    if drift_text:
        return 1, "", drift_text

    # Tool count is the manifest's agent_tools length (includes jira_specialist
    # black-box entry + synthetic sentinels) — this is the count the report's
    # coverage matrix uses. Page count is computed the same way so synthetic
    # sentinels (e.g. `spa-root` for Block D bundle scans) are counted.
    total_tools = len(manifest.get("agent_tools", []))
    total_pages = len(manifest.get("pages", []))
    summary = (
        f"manifest.json matches source tree "
        f"({total_pages} pages, {len(src_routes)} routes, {total_tools} tools)\n"
    )
    return 0, summary, ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_manifest_drift",
        description=(
            "Fail if src/coverage/manifest.json drifts from source tree. "
            "Exit codes: 0 = match, 1 = drift detected, "
            "2 = manifest not found or invalid JSON."
        ),
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Path to manifest.json (defaults to src/coverage/manifest.json).",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Repo root (defaults to two parents up from this script).",
    )
    args = parser.parse_args(argv)

    exit_code, stdout_text, stderr_text = run(
        repo_root=args.repo_root,
        manifest_path=args.manifest,
    )
    if stdout_text:
        sys.stdout.write(stdout_text)
    if stderr_text:
        sys.stderr.write(stderr_text)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
