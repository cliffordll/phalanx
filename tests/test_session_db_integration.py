"""SessionDB ↔ run_conversation integration tests (Phase 2.5 wave 2)."""

from __future__ import annotations

import pytest

import run_agent
from hermes_state import SessionDB
from tests.conftest import make_text_response, make_tool_response


@pytest.fixture
def stub_session_db(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    yield db
    db.close()


def _make_agent(stub_session_db, **overrides):
    """Build an AIAgent wired to a fresh SessionDB.  Tools off by default."""
    kwargs = {
        "model": "test-model",
        "base_url": "http://stub",
        "api_key": "stub",
        "max_iterations": 5,
        "tool_delay": 0.0,
        "session_db": stub_session_db,
        "platform": "test",
    }
    kwargs.update(overrides)
    agent = run_agent.AIAgent(**kwargs)
    # Strip auto-loaded tools so we can drive deterministic plain-text
    # turns; individual tests opt into tools as needed.
    agent._tool_registry = None
    agent._tool_schemas_cache = None
    return agent


def test_run_conversation_persists_user_and_assistant(stub_session_db, stub_openai):
    """A plain text exchange writes both messages and ends the session."""
    stub_openai([make_text_response("hi back")])
    agent = _make_agent(stub_session_db)

    result = agent.run_conversation("hello")

    assert result["final_response"] == "hi back"

    # Session row exists with the right source + ended state.
    sess = stub_session_db.get_session(agent.session_id)
    assert sess is not None
    assert sess["source"] == "test"
    assert sess["model"] == "test-model"
    assert sess["ended_at"] is not None
    assert sess["end_reason"] == "completed"
    assert sess["system_prompt"]  # populated via update_system_prompt

    # Messages: system + user + assistant; ordering preserved.
    msgs = stub_session_db.get_messages(agent.session_id)
    roles = [m["role"] for m in msgs]
    assert roles == ["system", "user", "assistant"]
    assert msgs[1]["content"] == "hello"
    assert msgs[2]["content"] == "hi back"


def test_run_conversation_persists_tool_call_round_trip(
    stub_session_db, stub_openai, monkeypatch
):
    """A tool-call turn records the assistant's tool_calls + the tool result."""
    # First response: assistant wants to call ``echo``; second: final text.
    stub_openai([
        make_tool_response([("call_1", "echo", '{"text": "hi"}')]),
        make_text_response("done"),
    ])
    agent = _make_agent(stub_session_db)

    # Stub the tool dispatch so we don't need a real tool registry.
    def fake_execute(self, assistant_msg, messages, *, effective_task_id, api_call_count):
        messages.append({
            "role": "tool",
            "tool_call_id": "call_1",
            "tool_name": "echo",
            "content": "tool-result-payload",
        })

    monkeypatch.setattr(run_agent.AIAgent, "_execute_tool_calls", fake_execute)

    result = agent.run_conversation("please call echo")
    assert result["final_response"] == "done"

    msgs = stub_session_db.get_messages(agent.session_id)
    roles = [m["role"] for m in msgs]
    assert roles == ["system", "user", "assistant", "tool", "assistant"]

    assistant_with_tool = msgs[2]
    assert assistant_with_tool["tool_calls"] == [{
        "id": "call_1",
        "type": "function",
        "function": {"name": "echo", "arguments": '{"text": "hi"}'},
    }]

    tool_msg = msgs[3]
    assert tool_msg["tool_call_id"] == "call_1"
    assert tool_msg["tool_name"] == "echo"
    assert tool_msg["content"] == "tool-result-payload"

    assert stub_session_db.get_session(agent.session_id)["tool_call_count"] == 1


def test_db_failure_does_not_break_conversation(stub_session_db, stub_openai):
    """append_message raising mid-flush must warn but let the loop continue."""
    stub_openai([make_text_response("ok")])
    agent = _make_agent(stub_session_db)

    # Force every append_message to raise — covers the inner try/except in
    # _persist_messages_to_db.
    def boom(*args, **kwargs):
        raise RuntimeError("simulated DB outage")

    stub_session_db.append_message = boom  # type: ignore[assignment]

    # The conversation must still return the model's reply.
    result = agent.run_conversation("hello")
    assert result["final_response"] == "ok"
    assert result["stop_reason"] == "completed"


def test_no_session_db_means_no_persistence(stub_openai):
    """Default (session_db=None) path bypasses persistence entirely."""
    stub_openai([make_text_response("hi")])
    agent = run_agent.AIAgent(
        model="test-model", base_url="http://stub",
        api_key="stub", max_iterations=3,
    )
    agent._tool_registry = None
    agent._tool_schemas_cache = None

    result = agent.run_conversation("hello")
    assert result["final_response"] == "hi"
    # Sanity: agent doesn't even pretend a session was created.
    assert agent._session_db_created is False


def test_resume_skips_preloaded_history(stub_session_db, stub_openai):
    """conversation_history rows must not be re-flushed on resume.

    Pre-existing rows for the resumed session_id stay untouched; only
    the new user prompt + assistant reply land on top.
    """
    # Seed the DB by running one turn.
    stub_openai([make_text_response("first reply")])
    first = _make_agent(stub_session_db)
    first.run_conversation("first prompt")
    first_sid = first.session_id

    # Seeded message list as the resume caller would supply (system +
    # user + assistant from the original turn).
    history = stub_session_db.get_messages(first_sid)
    history_for_resume = [
        {"role": m["role"], "content": m["content"]}
        for m in history
    ]
    rows_before = len(history)

    # Simulate /resume — fresh agent, same session_id, history seeded.
    stub_openai([make_text_response("second reply")])
    second = _make_agent(stub_session_db, session_id=first_sid)
    # Reopen the closed session so end_session-on-completion is a no-op
    # against the original "completed" record (matches /resume flow).
    stub_session_db.reopen_session(first_sid)

    second.run_conversation(
        "second prompt", conversation_history=history_for_resume,
    )

    final_rows = stub_session_db.get_messages(first_sid)
    # Original rows preserved (no double-write), plus user + assistant
    # from this turn.
    assert len(final_rows) == rows_before + 2
    assert final_rows[-2]["role"] == "user"
    assert final_rows[-2]["content"] == "second prompt"
    assert final_rows[-1]["role"] == "assistant"
    assert final_rows[-1]["content"] == "second reply"
