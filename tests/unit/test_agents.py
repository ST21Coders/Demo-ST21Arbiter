"""Unit tests for the agent modules.

The agents import heavy dependencies (`strands`, `bedrock_agentcore`) at module
load. We stub those before the import so the tests can run without the real
agent runtime — these are pure logic tests, not integration tests.
"""
from __future__ import annotations

import importlib
import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent


def _install_stubs():
    """Stub strands + bedrock_agentcore so agent.py imports don't blow up."""

    if "strands" not in sys.modules:
        strands = types.ModuleType("strands")
        # The Agent class is instantiated in build_agent() but never called in unit tests.
        strands.Agent = MagicMock(return_value=MagicMock())
        sys.modules["strands"] = strands

        models_mod = types.ModuleType("strands.models")
        sys.modules["strands.models"] = models_mod

        bedrock_mod = types.ModuleType("strands.models.bedrock")
        bedrock_mod.BedrockModel = MagicMock()
        sys.modules["strands.models.bedrock"] = bedrock_mod

        tools_mod = types.ModuleType("strands.tools")
        # The @tool decorator just returns the function unchanged for our tests.
        tools_mod.tool = lambda fn: fn
        sys.modules["strands.tools"] = tools_mod

    if "bedrock_agentcore" not in sys.modules:
        bac = types.ModuleType("bedrock_agentcore")
        sys.modules["bedrock_agentcore"] = bac
        runtime = types.ModuleType("bedrock_agentcore.runtime")

        class FakeApp:
            def __init__(self): pass
            def entrypoint(self, fn): return fn
            def run(self): pass

        runtime.BedrockAgentCoreApp = FakeApp
        sys.modules["bedrock_agentcore.runtime"] = runtime


@pytest.fixture
def master_agent(monkeypatch):
    """Re-import master_orchestrator/agent.py under stubbed deps + env vars."""
    _install_stubs()
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("MEMORY_ID", "test-memory")
    monkeypatch.setenv("SESSIONS_TABLE", "test-sessions")
    monkeypatch.setenv("SHAREPOINT_RUNTIME_ARN",
                       "arn:aws:bedrock-agentcore:us-east-1:000000000000:runtime/sharepoint")
    monkeypatch.setenv("AWSCONFIG_RUNTIME_ARN",
                       "arn:aws:bedrock-agentcore:us-east-1:000000000000:runtime/awsconfig")
    monkeypatch.setenv("ZSCALER_RUNTIME_ARN",
                       "arn:aws:bedrock-agentcore:us-east-1:000000000000:runtime/zscaler")

    sys.path.insert(0, str(ROOT / "agents" / "master_orchestrator"))
    if "agent" in sys.modules:
        del sys.modules["agent"]
    return importlib.import_module("agent")


# ──────────────────────────── A-2: SYSTEM_PROMPT compliance ──────
def test_master_system_prompt_has_no_markdown_headers(master_agent):
    """The redeployed master orchestrator must not use ###/##/# markdown headers
    in its system prompt (so its responses won't either). See feedback memory."""
    prompt = master_agent.SYSTEM_PROMPT
    for line in prompt.splitlines():
        assert not line.lstrip().startswith("#"), \
            f"SYSTEM_PROMPT contains a markdown header line: {line!r}"


def test_master_system_prompt_forbids_filler_words(master_agent):
    """The prompt itself documents which AI-tell phrases are banned."""
    # Collapse whitespace so phrases broken across lines still match.
    prompt = " ".join(master_agent.SYSTEM_PROMPT.lower().split())
    for banned in ["certainly", "i'd be happy to", "i hope this helps", "as an ai"]:
        assert banned in prompt, f"Prompt should explicitly ban {banned!r}"


def test_master_system_prompt_specifies_section_template(master_agent):
    """The fixed Summary/Findings/Conflicts/Recommendation/Sources template
    must be present as the canonical output structure."""
    prompt = master_agent.SYSTEM_PROMPT
    for header in ["Summary", "Findings", "Conflicts", "Recommendation", "Sources"]:
        assert header in prompt


# ──────────────────────────── A-3, A-4: _invoke_runtime errors ───
def test_invoke_runtime_returns_placeholder_when_arn_empty(master_agent):
    """A specialist with no ARN configured must return the placeholder string,
    not crash."""
    result = master_agent._invoke_runtime("", "any prompt")
    assert "not configured" in result.lower()


def test_invoke_runtime_swallows_runtime_exception(master_agent, monkeypatch):
    """A failed specialist must return a graceful error string so the master
    can keep going with partial data."""
    mock_client = MagicMock()
    mock_client.invoke_agent_runtime.side_effect = RuntimeError("agentcore is down")
    monkeypatch.setattr(master_agent, "runtime_client", mock_client)

    result = master_agent._invoke_runtime(
        "arn:aws:bedrock-agentcore:us-east-1:000:runtime/x", "hi"
    )
    assert "specialist error" in result.lower()
    assert "RuntimeError" in result


def test_invoke_runtime_extracts_result_field(master_agent, monkeypatch):
    mock_client = MagicMock()
    mock_client.invoke_agent_runtime.return_value = {
        "response": MagicMock(read=MagicMock(
            return_value=json.dumps({"result": "specialist answer"}).encode()
        ))
    }
    monkeypatch.setattr(master_agent, "runtime_client", mock_client)
    result = master_agent._invoke_runtime(
        "arn:aws:bedrock-agentcore:us-east-1:000:runtime/x", "hi"
    )
    assert result == "specialist answer"


def test_invoke_runtime_falls_back_to_full_body_when_no_result(master_agent, monkeypatch):
    """If the specialist returns JSON without a 'result' key, the raw body is
    returned — preserves whatever the specialist sent so the master sees it."""
    mock_client = MagicMock()
    raw = json.dumps({"reply": "no result key"})
    mock_client.invoke_agent_runtime.return_value = {
        "response": MagicMock(read=MagicMock(return_value=raw.encode()))
    }
    monkeypatch.setattr(master_agent, "runtime_client", mock_client)
    result = master_agent._invoke_runtime(
        "arn:aws:bedrock-agentcore:us-east-1:000:runtime/x", "hi"
    )
    # parsed.get('result', body) returns the body string when 'result' is missing.
    assert "no result key" in result


# ──────────────────────────── A-5, A-7: memory disabled ──────────
def test_retrieve_history_returns_empty_when_memory_id_unset(master_agent, monkeypatch):
    monkeypatch.setattr(master_agent, "MEMORY_ID", "")
    assert master_agent._retrieve_history("actor", "session") == ""


def test_save_turn_is_noop_when_memory_id_unset(master_agent, monkeypatch):
    monkeypatch.setattr(master_agent, "MEMORY_ID", "")
    mock_client = MagicMock()
    monkeypatch.setattr(master_agent, "runtime_client", mock_client)
    master_agent._save_turn("actor", "session", "user msg", "assistant msg")
    # create_event must not have been called.
    mock_client.create_event.assert_not_called()


def test_save_turn_swallows_create_event_exception(master_agent, monkeypatch):
    """A failed memory write must not crash the invocation — memory is best-effort."""
    mock_client = MagicMock()
    mock_client.create_event.side_effect = RuntimeError("memory unavailable")
    monkeypatch.setattr(master_agent, "runtime_client", mock_client)
    # Should not raise.
    master_agent._save_turn("actor", "session", "u", "a")


# ──────────────────────────── A-6: list_events pagination + reversal ─
def test_retrieve_history_reverses_events_to_chronological(master_agent, monkeypatch):
    mock_client = MagicMock()
    # AgentCore returns newest first; master must reverse them.
    mock_client.list_events.return_value = {"events": [
        {"payload": [{"conversational": {"role": "ASSISTANT",
                                          "content": {"text": "newer"}}}]},
        {"payload": [{"conversational": {"role": "USER",
                                          "content": {"text": "older"}}}]},
    ]}
    mock_client.retrieve_memory_records.return_value = {}
    monkeypatch.setattr(master_agent, "runtime_client", mock_client)

    history = master_agent._retrieve_history("actor", "session", max_turns=5)
    older_idx = history.find("older")
    newer_idx = history.find("newer")
    assert 0 <= older_idx < newer_idx


# ──────────────────────────── A-10: invoke entrypoint ────────────
def test_invoke_entrypoint_missing_prompt_returns_error(master_agent):
    resp = master_agent.invoke({"actor_id": "a", "session_id": "s"})
    assert "error" in resp
    assert "prompt" in resp["error"].lower()


def test_invoke_entrypoint_accepts_input_as_prompt_alias(master_agent, monkeypatch):
    """Backward-compat: both 'prompt' and 'input' are accepted."""
    monkeypatch.setattr(master_agent, "_conversation_exists", lambda *a, **kw: True)
    monkeypatch.setattr(master_agent, "_retrieve_history", lambda *a, **kw: "")
    monkeypatch.setattr(master_agent, "_save_turn", lambda *a, **kw: None)
    monkeypatch.setattr(master_agent, "_bump_conversation", lambda *a, **kw: None)
    fake_agent = MagicMock(return_value="fake reply")
    monkeypatch.setattr(master_agent, "build_agent", lambda: fake_agent)

    resp = master_agent.invoke({"input": "use the alias", "session_id": "adhoc"})
    assert "result" in resp


# ──────────────────────────── conversation index ────────────────
def test_conversation_exists_returns_true_when_memory_disabled(master_agent, monkeypatch):
    """With memory disabled, we have no way to check — default to 'exists'
    so we don't write spurious index rows."""
    monkeypatch.setattr(master_agent, "MEMORY_ID", "")
    assert master_agent._conversation_exists("a", "s") is True


def test_index_new_conversation_swallows_conditional_check_failure(master_agent, monkeypatch):
    """If two concurrent invocations race to index the same session, the second
    one's ConditionalCheckFailedException must be silent (already-indexed
    is the correct state)."""
    fake_exception = type("ConditionalCheckFailedException", (Exception,), {})
    mock_ddb = MagicMock()
    mock_ddb.put_item.side_effect = fake_exception("already exists")
    mock_ddb.exceptions = MagicMock(ConditionalCheckFailedException=fake_exception)
    monkeypatch.setattr(master_agent, "ddb_client", mock_ddb)

    # Should not raise.
    master_agent._index_new_conversation("a", "s", "title", "analyst")
