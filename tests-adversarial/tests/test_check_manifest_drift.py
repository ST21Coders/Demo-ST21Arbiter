"""Smoke tests for scripts/check_manifest_drift.py.

Covers:
  - Happy path: against the current source tree + current manifest, the script
    runs cleanly, exits 0, and prints the expected match summary on stdout.
  - Page drift: monkey-patch the page-glob function so it discovers an extra
    .jsx file that the manifest does not list — assert exit 1 and the fake
    filename appears in stderr.
  - Route drift: monkey-patch the api_handler reader to inject a fragment with
    a new `if path == "..."` route — assert exit 1 with the new route in
    stderr.
  - Tool drift: monkey-patch the agent reader to inject an extra
    `@tool`-decorated function — assert exit 1 with the new tool in stderr.
  - jira_specialist black-box invariant: confirm it is *not* flagged as drift
    by the unmodified tree (source_in_repo: false is the correct skip-signal).
  - Internal extractor unit-coverage: route patterns (path==, startswith inline,
    startswith block with nested subs), template normalization, and the
    @tool detector's robustness against decorator-as-substring in comments.
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts import check_manifest_drift as drift


# Where the harness lives, and where the manifest lives — same paths the
# script's defaults resolve to.
_HARNESS_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _HARNESS_ROOT.parent
_MANIFEST_PATH = _HARNESS_ROOT / "src" / "coverage" / "manifest.json"


# ───────────────────────────── happy path ─────────────────────────────────


def test_run_against_live_tree_exits_zero_and_prints_match_line():
    """Current source + current manifest agree — the canonical green run."""
    exit_code, stdout, stderr = drift.run()

    assert exit_code == 0, f"expected exit 0, got {exit_code}; stderr={stderr!r}"
    assert stderr == "", f"expected empty stderr on match, got {stderr!r}"
    assert stdout.startswith("manifest.json matches source tree"), (
        f"stdout did not start with expected match line: {stdout!r}"
    )
    # The summary tallies pages, routes, tools. We assert the exact shape the
    # task prompt mandates: 15 pages, 25 routes, 12 tools. The tool count is
    # 12 = 10 real in-repo tools + 1 jira black-box + 1 synthetic sentinel
    # (`master.chat_surface`, added for LLM red-team coverage rows).
    assert "(15 pages, 25 routes, 12 tools)" in stdout, (
        f"summary counts drifted from expected (15/25/12): {stdout!r}"
    )


def test_run_against_live_tree_does_not_flag_jira_specialist():
    """jira_specialist's source_in_repo: false is the correct skip-signal —
    it must not be flagged as drift on an unmodified tree."""
    exit_code, stdout, stderr = drift.run()
    assert exit_code == 0
    assert "jira_specialist" not in stderr.lower()
    assert "JIRA_SPECIALIST" not in stderr


# ───────────────────────────── pages drift ─────────────────────────────────


def test_pages_added_in_source_missing_in_manifest_flagged_as_drift(monkeypatch):
    """Inject a fake page file via the glob hook; assert drift reported on stderr."""
    real_glob = drift._glob_pages
    fake_file = "ui/src/pages/FakeNewPage.jsx"

    def fake_glob(repo_root: Path) -> set[str]:
        return real_glob(repo_root) | {fake_file}

    monkeypatch.setattr(drift, "_glob_pages", fake_glob)

    exit_code, stdout, stderr = drift.run()

    assert exit_code == 1
    assert stdout == ""
    assert "manifest drift detected" in stderr
    assert "PAGES added in source, missing in manifest" in stderr
    assert fake_file in stderr


def test_pages_removed_from_source_present_in_manifest_flagged_as_drift(monkeypatch):
    """Drop a real page from the glob; assert removed-from-source drift."""
    real_glob = drift._glob_pages

    def fake_glob(repo_root: Path) -> set[str]:
        # Drop Dashboard.jsx to simulate it being deleted from source.
        return real_glob(repo_root) - {"ui/src/pages/Dashboard.jsx"}

    monkeypatch.setattr(drift, "_glob_pages", fake_glob)

    exit_code, stdout, stderr = drift.run()

    assert exit_code == 1
    assert "PAGES removed from source, present in manifest" in stderr
    assert "ui/src/pages/Dashboard.jsx" in stderr


# ───────────────────────────── routes drift ────────────────────────────────


_REAL_HANDLER_INJECTED_ROUTE = """\
def handler(event, context):
    path = event.get("path") or ""
    method = (event.get("httpMethod") or "").upper()

    if path == "/health":
        return {}

    if path == "/chat" and method == "POST":
        return {}

    if path == "/findings" and method == "GET":
        return {}

    if path == "/actions" and method == "GET":
        return {}

    if path == "/audit" and method == "GET":
        return {}

    if path == "/token-usage" and method == "GET":
        return {}

    if path == "/token-usage/summary" and method == "GET":
        return {}

    if path == "/conversations" and method == "GET":
        return {}

    if path == "/uploads/presign" and method == "POST":
        return {}

    if path == "/uploads/list" and method == "GET":
        return {}

    if path.startswith("/conversations/"):
        tail = path[len("/conversations/"):].split("/", 1)
        sub = tail[1] if len(tail) > 1 else ""
        if sub == "messages" and method == "GET":
            return {}
        if not sub and method == "GET":
            return {}
        if not sub and method == "DELETE":
            return {}

    if path == "/dashboard" and method == "GET":
        return {}

    if path == "/mcp-health" and method == "GET":
        return {}

    if path == "/jira/tickets" and method == "POST":
        return {}

    if path == "/scan" and method == "POST":
        return {}

    if path == "/scan-runs" and method == "GET":
        return {}

    if path.startswith("/scan-runs/") and method == "GET":
        return {}

    if path.startswith("/findings/") and method == "GET":
        return {}

    if path == "/actions" and method == "POST":
        return {}

    if path.startswith("/actions/") and method == "POST":
        tail = path[len("/actions/"):].split("/", 1)
        sub = tail[1] if len(tail) > 1 else ""
        if sub == "approve":
            return {}
        if sub == "reject":
            return {}
        if sub == "execute":
            return {}
        if sub == "escalate":
            return {}

    if path == "/brand-new-route" and method == "GET":
        return {}

    return {}
"""


def test_routes_added_in_source_missing_in_manifest_flagged_as_drift(monkeypatch):
    """Inject a brand-new route into the source via the reader hook; assert drift."""
    monkeypatch.setattr(
        drift,
        "_read_api_handler",
        lambda repo_root: _REAL_HANDLER_INJECTED_ROUTE,
    )

    exit_code, stdout, stderr = drift.run()

    assert exit_code == 1
    assert "API_ROUTES added in source, missing in manifest" in stderr
    assert "GET /brand-new-route" in stderr


def test_routes_removed_from_source_present_in_manifest_flagged_as_drift(monkeypatch):
    """Strip a route out of the source; assert removed-from-source drift."""
    monkeypatch.setattr(
        drift,
        "_read_api_handler",
        lambda repo_root: _REAL_HANDLER_INJECTED_ROUTE.replace(
            '    if path == "/health":\n        return {}\n\n',
            "",
        ),
    )

    exit_code, stdout, stderr = drift.run()

    assert exit_code == 1
    # /health was the only no-method-clause route; removing it should flag
    # `GET /health` as missing from source.
    assert "API_ROUTES removed from source, present in manifest" in stderr
    assert "GET /health" in stderr


# ───────────────────────────── tools drift ─────────────────────────────────


def test_tool_added_in_source_missing_in_manifest_flagged_as_drift(monkeypatch):
    """Append a brand-new @tool def to one agent's source via the reader hook."""
    real_read = drift._read_agent

    def fake_read(repo_root: Path, agent: str) -> str:
        text = real_read(repo_root, agent)
        if agent == "master_orchestrator":
            text += (
                "\n\n@tool\ndef brand_new_tool(query: str) -> str:\n    return 'x'\n"
            )
        return text

    monkeypatch.setattr(drift, "_read_agent", fake_read)

    exit_code, stdout, stderr = drift.run()

    assert exit_code == 1
    assert "AGENT_TOOLS added in source, missing in manifest" in stderr
    assert "master_orchestrator" in stderr
    assert "brand_new_tool" in stderr


def test_tool_removed_from_source_present_in_manifest_flagged_as_drift(monkeypatch):
    """Strip a known @tool out of the source; assert removed-from-source drift."""
    real_read = drift._read_agent

    def fake_read(repo_root: Path, agent: str) -> str:
        text = real_read(repo_root, agent)
        if agent == "master_orchestrator":
            # Delete the sharepoint_lookup tool definition.
            text = text.replace(
                "@tool\ndef sharepoint_lookup",
                "def _removed_sharepoint_lookup",
            )
        return text

    monkeypatch.setattr(drift, "_read_agent", fake_read)

    exit_code, stdout, stderr = drift.run()

    assert exit_code == 1
    assert "AGENT_TOOLS removed from source, present in manifest" in stderr
    assert "sharepoint_lookup" in stderr


# ─────────────────────── jira_specialist black-box ─────────────────────────


def test_jira_specialist_flagged_if_source_in_repo_flipped_to_true(
    monkeypatch, tmp_path
):
    """If someone flips source_in_repo on jira_specialist to true, the drift
    check must surface that as a flagged invariant — the harness would
    otherwise silently keep treating it as black-box."""
    # Copy the real manifest, mutate the jira_specialist runtime flag, write
    # to tmp, then point the script at the tmp copy.
    real = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    for r in real.get("runtimes", []):
        if r.get("id") == "jira_specialist":
            r["source_in_repo"] = True
    tmp_manifest = tmp_path / "manifest.json"
    tmp_manifest.write_text(json.dumps(real), encoding="utf-8")

    exit_code, stdout, stderr = drift.run(manifest_path=tmp_manifest)

    assert exit_code == 1
    assert "JIRA_SPECIALIST" in stderr
    assert "source_in_repo" in stderr


def test_jira_specialist_runtime_missing_is_flagged(monkeypatch, tmp_path):
    """If the runtimes block loses the jira_specialist entry entirely, drift
    fires too — black-box coverage is a deliberate invariant, not an
    accidental omission."""
    real = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    real["runtimes"] = [
        r for r in real.get("runtimes", []) if r.get("id") != "jira_specialist"
    ]
    tmp_manifest = tmp_path / "manifest.json"
    tmp_manifest.write_text(json.dumps(real), encoding="utf-8")

    exit_code, stdout, stderr = drift.run(manifest_path=tmp_manifest)

    assert exit_code == 1
    assert "JIRA_SPECIALIST" in stderr
    assert "missing" in stderr.lower()


# ─────────────────── internal extractor unit coverage ─────────────────────


def test_normalize_template_replaces_path_params():
    assert drift._normalize_template("/findings/{conflict_id}") == "/findings/{}"
    assert (
        drift._normalize_template("/actions/{cr_id}/approve") == "/actions/{}/approve"
    )
    assert drift._normalize_template("/health") == "/health"
    assert drift._normalize_template("/a/{x}/b/{y}/c") == "/a/{}/b/{}/c"


def test_extract_routes_from_source_handles_path_eq_with_method():
    src = """\
def handler(event, context):
    path = ""
    method = ""
    if path == "/foo" and method == "POST":
        return {}
    if path == "/bar":
        return {}
"""
    routes = drift._extract_routes_from_source(src)
    assert ("POST", "/foo") in routes
    # Bare path== with no method defaults to GET.
    assert ("GET", "/bar") in routes


def test_extract_routes_from_source_handles_startswith_with_inline_method():
    src = """\
def handler(event, context):
    path = ""
    method = ""
    if path.startswith("/scan-runs/") and method == "GET":
        return {}
"""
    routes = drift._extract_routes_from_source(src)
    assert ("GET", "/scan-runs/{}") in routes


def test_extract_routes_from_source_handles_startswith_block_with_nested_subs():
    """The /actions/{} block in api_handler.py guards by POST inline but the
    nested `sub == ...` lines are what the real handler dispatches on.
    The parser must emit one tuple per nested sub, not just the bare prefix.
    """
    src = """\
def handler(event, context):
    path = ""
    method = ""
    if path.startswith("/actions/") and method == "POST":
        tail = path[len("/actions/"):].split("/", 1)
        sub = tail[1] if len(tail) > 1 else ""
        if sub == "approve":
            return {}
        if sub == "reject":
            return {}
"""
    routes = drift._extract_routes_from_source(src)
    assert ("POST", "/actions/{}/approve") in routes
    assert ("POST", "/actions/{}/reject") in routes
    # When nested subs are present, the bare prefix is NOT a route on its own.
    assert ("POST", "/actions/{}") not in routes


def test_extract_routes_from_source_handles_startswith_block_with_not_sub():
    """The /conversations/{} block: bare `not sub` paths map to /conversations/{}."""
    src = """\
def handler(event, context):
    path = ""
    method = ""
    if path.startswith("/conversations/"):
        tail = path[len("/conversations/"):].split("/", 1)
        sub = tail[1] if len(tail) > 1 else ""
        if sub == "messages" and method == "GET":
            return {}
        if not sub and method == "GET":
            return {}
        if not sub and method == "DELETE":
            return {}
"""
    routes = drift._extract_routes_from_source(src)
    assert ("GET", "/conversations/{}/messages") in routes
    assert ("GET", "/conversations/{}") in routes
    assert ("DELETE", "/conversations/{}") in routes


def test_extract_routes_from_source_ignores_strings_outside_handler():
    """A route literal inside an unrelated helper or comment must not be picked
    up — the parser only scans the handler() function body."""
    src = """\
# if path == "/comment-only" should not be picked up
ROUTES = ["/string-literal"]

def _other_helper(event):
    # Even inside a function, if it isn't `handler`, it must be skipped.
    if path == "/helper-route":
        return {}

def handler(event, context):
    if path == "/real-route" and method == "GET":
        return {}
"""
    routes = drift._extract_routes_from_source(src)
    assert ("GET", "/real-route") in routes
    assert ("GET", "/comment-only") not in routes
    assert ("GET", "/helper-route") not in routes


def test_extract_tools_from_agent_handles_bare_decorator():
    src = """\
@tool
def my_tool(query: str) -> str:
    return ""

@tool
def another_tool(query: str, max: int = 5) -> str:
    return ""
"""
    assert drift._extract_tools_from_agent_source(src) == {"my_tool", "another_tool"}


def test_extract_tools_from_agent_ignores_comment_mention_of_tool():
    """The master_orchestrator's source contains `# @tool wrappers...` in a
    comment. That must not be misread as a decorator on the next def."""
    src = """\
# @tool wrappers (which only receive query from the LLM) can forward via env vars
def not_a_tool(query: str) -> str:
    return ""

@tool
def real_tool(query: str) -> str:
    return ""
"""
    assert drift._extract_tools_from_agent_source(src) == {"real_tool"}


def test_extract_tools_from_agent_handles_tool_with_args():
    """If the decorator is ever invoked with arguments, the parser still
    extracts the function name on the next def."""
    src = """\
@tool(name="custom")
def my_tool(query: str) -> str:
    return ""
"""
    assert drift._extract_tools_from_agent_source(src) == {"my_tool"}


# ───────────────────────────── CLI entry point ─────────────────────────────


def test_main_prints_stdout_on_success_and_returns_zero(capsys):
    """`main()` is the script entry point used by `python -m scripts...`."""
    code = drift.main([])
    captured = capsys.readouterr()

    assert code == 0
    assert captured.out.startswith("manifest.json matches source tree")
    assert captured.err == ""


def test_main_prints_stderr_on_drift_and_returns_one(capsys, tmp_path):
    """Use a manifest copy with the jira flag flipped to force drift."""
    real = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    for r in real.get("runtimes", []):
        if r.get("id") == "jira_specialist":
            r["source_in_repo"] = True
    tmp_manifest = tmp_path / "manifest.json"
    tmp_manifest.write_text(json.dumps(real), encoding="utf-8")

    code = drift.main(["--manifest", str(tmp_manifest)])
    captured = capsys.readouterr()

    assert code == 1
    assert captured.out == ""
    assert "manifest drift detected" in captured.err


def test_main_returns_two_on_missing_manifest(capsys, tmp_path):
    """Non-existent manifest path is a pre-flight error (exit 2), not a drift
    (exit 1) — keeps the contract that exit 1 means 'manifest is stale' and
    exit 2 means 'we couldn't even check'."""
    missing = tmp_path / "does-not-exist.json"
    code = drift.main(["--manifest", str(missing)])
    captured = capsys.readouterr()

    assert code == 2
    assert "manifest not found" in captured.err
