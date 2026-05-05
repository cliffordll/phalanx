"""Slash command handler tests (Phase 2.6 wave 3).

Direct unit tests on each ``_cmd_*`` function — no PromptSession in
the loop.  Each handler takes ``(args, state)`` and operates on
``state["agent"]`` (an ``AIAgent`` stand-in) and ``state["history"]``
(the in-process message list).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

import pytest

import cli


# ── Fakes ────────────────────────────────────────────────────────────────


class FakeRegistry:
    """Stand-in for ``tools.registry.registry``."""

    def __init__(self):
        self._tools = {}

    def register(self, name: str, toolset: str, schema: dict) -> None:
        self._tools[name] = (toolset, schema)

    def get_all_tool_names(self):
        return sorted(self._tools)

    def get_toolset_for_tool(self, name: str):
        return self._tools[name][0] if name in self._tools else None

    def get_schema(self, name: str):
        return self._tools[name][1] if name in self._tools else None


class FakeSessionDB:
    """Stand-in for ``hermes_state.SessionDB``."""

    def __init__(self):
        self.resolved = None
        self.resumed = None
        self.reopened = None
        self.next_id_lookup = None
        self.history = []

    def resolve_session_id(self, target):
        self.resolved = target
        return self.next_id_lookup

    def resolve_resume_session_id(self, sid):
        return sid

    def get_messages_as_conversation(self, sid):
        return list(self.history)

    def reopen_session(self, sid):
        self.reopened = sid


class FakeAgent:
    def __init__(self, **kw):
        self.model = kw.get("model", "test-model")
        self.session_id = kw.get("session_id", "old-session-id")
        self.verbose_logging = kw.get("verbose_logging", False)
        self.disabled_toolsets = []
        self._tool_registry = kw.get("registry")
        self._tool_schemas_cache = ["fake-schema"]
        self._session_db = kw.get("session_db")
        self._session_db_created = True
        self._last_flushed_db_idx = 0


def _state(**kw) -> Dict[str, Any]:
    return {
        "agent": kw.get("agent") or FakeAgent(),
        "history": kw.get("history", []),
    }


# ── /new ────────────────────────────────────────────────────────────────


def test_cmd_new_resets_history_and_session(capsys):
    state = _state(history=[{"role": "user", "content": "x"}])
    old_id = state["agent"].session_id
    cli._cmd_new("", state)
    out = capsys.readouterr().out
    assert state["history"] == []
    assert state["agent"].session_id != old_id
    assert state["agent"]._session_db_created is False
    assert state["agent"]._last_flushed_db_idx == 0
    assert "started new session" in out
    # Banner shows first 8 chars of the new id.
    assert state["agent"].session_id[:8] in out


# ── /clear ──────────────────────────────────────────────────────────────


def test_cmd_clear_runs_new_after_screen_wipe(capsys, monkeypatch):
    """``/clear`` should fire os.system + apply ``/new``'s state changes."""
    calls: List[str] = []
    monkeypatch.setattr("os.system", lambda cmd: calls.append(cmd) or 0)
    state = _state(history=[{"role": "user", "content": "x"}])
    cli._cmd_clear("", state)
    assert state["history"] == []
    assert calls and calls[0] in ("cls", "clear")


# ── /history ────────────────────────────────────────────────────────────


def test_cmd_history_empty_state(capsys):
    cli._cmd_history("", _state())
    assert "no messages yet" in capsys.readouterr().out


def test_cmd_history_renders_each_role(capsys):
    state = _state(history=[
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "tool", "content": "tool-result"},
    ])
    cli._cmd_history("", state)
    out = capsys.readouterr().out
    assert "[0] user: hello" in out
    assert "[1] assistant: hi" in out
    assert "[2] tool: tool-result" in out


def test_cmd_history_truncates_long_content(capsys):
    state = _state(history=[
        {"role": "user", "content": "x" * 200},
    ])
    cli._cmd_history("", state)
    out = capsys.readouterr().out
    # Long lines get truncated to 80 chars + "..."
    assert "..." in out
    # Each printed line must fit within 80 + a few decoration chars.
    for line in out.splitlines():
        assert len(line) <= 100


def test_cmd_history_handles_multimodal_content_marker(capsys):
    state = _state(history=[
        {"role": "user", "content": [
            {"type": "text", "text": "look"},
            {"type": "image_url", "image_url": {"url": "http://x"}},
        ]},
    ])
    cli._cmd_history("", state)
    out = capsys.readouterr().out
    assert "<2 content parts>" in out


# ── /model ──────────────────────────────────────────────────────────────


def test_cmd_model_show_current(capsys):
    state = _state(agent=FakeAgent(model="qwen2.5:1.5b"))
    cli._cmd_model("", state)
    assert "qwen2.5:1.5b" in capsys.readouterr().out


def test_cmd_model_switch(capsys):
    agent = FakeAgent(model="old-model")
    state = _state(agent=agent)
    cli._cmd_model("new-model", state)
    out = capsys.readouterr().out
    assert agent.model == "new-model"
    assert "old-model" in out
    assert "new-model" in out


# ── /debug ──────────────────────────────────────────────────────────────


def test_cmd_debug_default_shows_status(capsys):
    state = _state(agent=FakeAgent(verbose_logging=False))
    cli._cmd_debug("", state)
    assert "debug: off" in capsys.readouterr().out


def test_cmd_debug_on(capsys):
    agent = FakeAgent(verbose_logging=False)
    cli._cmd_debug("on", _state(agent=agent))
    assert agent.verbose_logging is True
    assert logging.getLogger().level == logging.DEBUG


def test_cmd_debug_off(capsys):
    agent = FakeAgent(verbose_logging=True)
    cli._cmd_debug("off", _state(agent=agent))
    assert agent.verbose_logging is False
    assert logging.getLogger().level == logging.WARNING


def test_cmd_debug_unknown_subcommand(capsys):
    cli._cmd_debug("loud", _state())
    assert "unknown sub-action" in capsys.readouterr().out


# ── /tools ──────────────────────────────────────────────────────────────


def test_cmd_tools_list_no_registry(capsys):
    state = _state(agent=FakeAgent(registry=None))
    cli._cmd_tools("", state)
    assert "no tool registry" in capsys.readouterr().out


def test_cmd_tools_list_empty_registry(capsys):
    state = _state(agent=FakeAgent(registry=FakeRegistry()))
    cli._cmd_tools("list", state)
    assert "no tools registered" in capsys.readouterr().out


def test_cmd_tools_list_renders_each_tool(capsys):
    reg = FakeRegistry()
    reg.register("echo", "core", {"description": "Echoes input"})
    reg.register("read_file", "fs", {"description": "Reads a file"})
    state = _state(agent=FakeAgent(registry=reg))
    cli._cmd_tools("list", state)
    out = capsys.readouterr().out
    assert "echo" in out
    assert "[core]" in out
    assert "Echoes input" in out
    assert "read_file" in out
    assert "[fs]" in out


def test_cmd_tools_disable_adds_to_list(capsys):
    agent = FakeAgent(registry=FakeRegistry())
    cli._cmd_tools("disable web", _state(agent=agent))
    assert "web" in agent.disabled_toolsets
    assert agent._tool_schemas_cache is None
    assert "disabled: web" in capsys.readouterr().out


def test_cmd_tools_enable_removes_from_list(capsys):
    agent = FakeAgent(registry=FakeRegistry())
    agent.disabled_toolsets = ["web", "fs"]
    cli._cmd_tools("enable web", _state(agent=agent))
    assert "web" not in agent.disabled_toolsets
    assert "fs" in agent.disabled_toolsets
    assert "enabled: web" in capsys.readouterr().out


def test_cmd_tools_disable_without_target_warns(capsys):
    cli._cmd_tools("disable", _state())
    assert "missing tool" in capsys.readouterr().out


def test_cmd_tools_unknown_subcommand(capsys):
    cli._cmd_tools("yeet", _state())
    assert "unknown sub-action" in capsys.readouterr().out


def test_cmd_tools_list_marks_disabled(capsys):
    reg = FakeRegistry()
    reg.register("echo", "core", {"description": "Echoes"})
    agent = FakeAgent(registry=reg)
    agent.disabled_toolsets = ["core"]
    cli._cmd_tools("list", _state(agent=agent))
    assert "(disabled)" in capsys.readouterr().out


# ── /resume ─────────────────────────────────────────────────────────────


def test_cmd_resume_missing_arg(capsys):
    cli._cmd_resume("", _state(agent=FakeAgent(session_db=FakeSessionDB())))
    assert "usage" in capsys.readouterr().out


def test_cmd_resume_no_session_db(capsys):
    cli._cmd_resume("abc", _state(agent=FakeAgent(session_db=None)))
    assert "session DB unavailable" in capsys.readouterr().out


def test_cmd_resume_unknown_id(capsys):
    db = FakeSessionDB()
    db.next_id_lookup = None
    state = _state(agent=FakeAgent(session_db=db))
    cli._cmd_resume("nope", state)
    assert "no matching session" in capsys.readouterr().out


def test_cmd_resume_loads_history(capsys):
    db = FakeSessionDB()
    db.next_id_lookup = "real-session-uuid-aaa"
    db.history = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
    ]
    agent = FakeAgent(session_db=db)
    state = _state(agent=agent)
    cli._cmd_resume("real", state)
    assert agent.session_id == "real-session-uuid-aaa"
    assert agent._session_db_created is True
    assert agent._last_flushed_db_idx == 2
    assert state["history"] == db.history
    assert db.reopened == "real-session-uuid-aaa"
    out = capsys.readouterr().out
    assert "resumed session" in out
    assert "2 messages restored" in out


# ── tips ────────────────────────────────────────────────────────────────


def test_pick_tip_returns_a_string():
    from hermes_cli.tips import TIPS, pick_tip
    tip = pick_tip()
    assert tip in TIPS


def test_pick_tip_handles_empty_corpus():
    from hermes_cli.tips import pick_tip
    assert pick_tip(tips=[]) is None


def test_pick_tip_uses_supplied_corpus():
    from hermes_cli.tips import pick_tip
    assert pick_tip(tips=["only one"]) == "only one"


def test_print_random_tip_skips_silently_on_failure(capsys, monkeypatch):
    """A broken tips module shouldn't crash the REPL banner."""
    def _boom(*a, **kw):
        raise RuntimeError("broken")

    monkeypatch.setattr("hermes_cli.tips.pick_tip", _boom)
    cli._print_random_tip()
    # No exception, no output.
    captured = capsys.readouterr()
    assert "tip:" not in captured.out


def test_print_random_tip_prints_tip(capsys, monkeypatch):
    monkeypatch.setattr("hermes_cli.tips.pick_tip", lambda: "use /help")
    cli._print_random_tip()
    assert "tip: use /help" in capsys.readouterr().out


# ── _run_turn streaming ─────────────────────────────────────────────────


def test_run_turn_streams_via_callback(capsys):
    """The stream_callback wins over print() when deltas are emitted."""

    class _StreamingAgent:
        def run_conversation(self, msg, *, conversation_history, stream_callback):
            stream_callback("Hello ")
            stream_callback("world")
            return {
                "final_response": "Hello world",
                "messages": [
                    {"role": "user", "content": msg},
                    {"role": "assistant", "content": "Hello world"},
                ],
            }

    cli._run_turn(_StreamingAgent(), "hi", [])
    out = capsys.readouterr().out
    assert "Hello world" in out
    # _run_turn must have closed the line itself; otherwise the prompt
    # and the stream collide.
    assert out.endswith("\n")


def test_run_turn_falls_back_when_no_streaming(capsys):
    """Tool-only turns get the final_response printed directly."""

    class _SilentAgent:
        def run_conversation(self, msg, *, conversation_history, stream_callback):
            # No callback fired → simulating a tool-only turn.
            return {
                "final_response": "tool call complete",
                "messages": [],
            }

    cli._run_turn(_SilentAgent(), "go", [])
    assert "tool call complete" in capsys.readouterr().out


def test_run_turn_threads_history(capsys):
    seen = {}

    class _Recording:
        def run_conversation(self, msg, *, conversation_history, stream_callback):
            seen["history"] = conversation_history
            return {"final_response": "ok", "messages": []}

    history = [{"role": "user", "content": "earlier"}]
    cli._run_turn(_Recording(), "next", history)
    assert seen["history"] == history


def test_open_stream_context_falls_back_when_pt_unavailable(monkeypatch):
    """When _PT_AVAILABLE is False the helper hands back a NullContext."""
    monkeypatch.setattr(cli, "_PT_AVAILABLE", False)
    ctx = cli._open_stream_context()
    assert isinstance(ctx, cli._NullContext)


def test_null_context_is_a_context_manager():
    with cli._NullContext() as ret:
        assert ret is not None
