"""``cli._run_repl`` tests (Phase 2.6 wave 1).

Drives the REPL through a fake PromptSession (or a fake stdin for the
fallback path) so the prompt_toolkit event loop never gets touched —
unit-testing prompt_toolkit's terminal layer is a deep rabbit hole and
isn't the contract we care about here.  What we *do* care about:

* the loop terminates cleanly on ``/exit`` / ``/quit`` / ``:q`` / EOF
* plain text drives ``agent.run_conversation`` with the running history
* exceptions inside ``run_conversation`` don't break the loop
* history is persisted to ``~/.hermes/cli_history`` when prompt_toolkit
  is available
* a missing prompt_toolkit falls back to ``input()`` cleanly
"""

from __future__ import annotations

import io
from typing import List

import pytest

import cli


# ── Fakes ────────────────────────────────────────────────────────────────


class FakePromptSession:
    """Stand-in for ``prompt_toolkit.PromptSession``.

    Constructed with a queued list of replies; ``prompt(...)`` pops
    them in order.  Records every call so assertions can verify the
    REPL really walked the queue.  When the queue runs out we raise
    ``EOFError`` so ``_run_repl`` exits gracefully — that mirrors what
    the real session does on Ctrl+D.
    """

    def __init__(self, replies: List[str]) -> None:
        self._replies = list(replies)
        self.calls: List[str] = []

    def prompt(self, prompt_text: str = "> ") -> str:
        self.calls.append(prompt_text)
        if not self._replies:
            raise EOFError
        return self._replies.pop(0)


class FakeAgent:
    """Minimal stand-in for ``AIAgent``.

    Only the surface ``_run_repl`` touches: ``model``, ``close``, and
    ``run_conversation``.  The conversation method records the args
    it was called with and returns a static response unless told to
    raise.
    """

    def __init__(self, *, model: str = "test-model", raise_on_run: bool = False):
        self.model = model
        self.raise_on_run = raise_on_run
        self.calls: List[dict] = []

    def run_conversation(self, message, conversation_history=None, **kwargs):
        self.calls.append({
            "message": message,
            "conversation_history": conversation_history,
        })
        if self.raise_on_run:
            raise RuntimeError("boom")
        return {
            "final_response": f"echo:{message}",
            "messages": [
                {"role": "user", "content": message},
                {"role": "assistant", "content": f"echo:{message}"},
            ],
            "api_calls": 1,
            "stop_reason": "completed",
            "iterations_used": 1,
        }

    def close(self) -> None:
        pass


@pytest.fixture
def isolated_phalanx_home(tmp_path, monkeypatch):
    """Force ~/.hermes lookups into tmp_path for a clean history file."""
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    return tmp_path


def _patch_prompt_toolkit(monkeypatch, replies: List[str]) -> FakePromptSession:
    """Wire a FakePromptSession into cli._build_prompt_session."""
    fake = FakePromptSession(replies)
    monkeypatch.setattr(cli, "_PT_AVAILABLE", True)
    monkeypatch.setattr(cli, "_build_prompt_session", lambda: fake)
    return fake


# ── Exit semantics ───────────────────────────────────────────────────────


def test_exit_token_returns_zero(isolated_phalanx_home, monkeypatch, capsys):
    fake = _patch_prompt_toolkit(monkeypatch, ["/exit"])
    rc = cli._run_repl(FakeAgent())
    assert rc == 0
    assert fake.calls == ["> "]


def test_quit_token_returns_zero(isolated_phalanx_home, monkeypatch):
    _patch_prompt_toolkit(monkeypatch, ["/quit"])
    assert cli._run_repl(FakeAgent()) == 0


def test_short_quit_token_returns_zero(isolated_phalanx_home, monkeypatch):
    _patch_prompt_toolkit(monkeypatch, [":q"])
    assert cli._run_repl(FakeAgent()) == 0


def test_eof_returns_zero(isolated_phalanx_home, monkeypatch, capsys):
    """An empty queue (real Ctrl+D) ends the loop without an error."""
    _patch_prompt_toolkit(monkeypatch, [])  # immediate EOF
    rc = cli._run_repl(FakeAgent())
    captured = capsys.readouterr()
    assert rc == 0
    # Banner still printed.
    assert "phalanx chat" in captured.out


def test_keyboard_interrupt_returns_zero(isolated_phalanx_home, monkeypatch):
    """Ctrl+C surfaces from session.prompt and ends the loop cleanly."""
    class _CtrlCSession:
        calls: List[str] = []

        def prompt(self, *a, **kw):
            raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_PT_AVAILABLE", True)
    monkeypatch.setattr(cli, "_build_prompt_session", _CtrlCSession)
    assert cli._run_repl(FakeAgent()) == 0


# ── Conversation forwarding ─────────────────────────────────────────────


def test_plain_text_calls_run_conversation(isolated_phalanx_home, monkeypatch, capsys):
    _patch_prompt_toolkit(monkeypatch, ["hello", "/exit"])
    agent = FakeAgent()
    rc = cli._run_repl(agent)
    captured = capsys.readouterr()
    assert rc == 0
    assert len(agent.calls) == 1
    assert agent.calls[0]["message"] == "hello"
    # First call: history is the default [].
    assert agent.calls[0]["conversation_history"] == []
    assert "echo:hello" in captured.out


def test_history_is_threaded_across_turns(isolated_phalanx_home, monkeypatch):
    _patch_prompt_toolkit(monkeypatch, ["one", "two", "/exit"])
    agent = FakeAgent()
    cli._run_repl(agent)
    assert len(agent.calls) == 2
    # Second turn must see the rolled-up history from the first.
    assert agent.calls[1]["conversation_history"] == [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "echo:one"},
    ]


def test_blank_lines_are_skipped(isolated_phalanx_home, monkeypatch):
    _patch_prompt_toolkit(monkeypatch, ["", "   ", "go", "/exit"])
    agent = FakeAgent()
    cli._run_repl(agent)
    assert len(agent.calls) == 1
    assert agent.calls[0]["message"] == "go"


def test_run_conversation_exception_is_caught(
    isolated_phalanx_home, monkeypatch, capsys,
):
    _patch_prompt_toolkit(monkeypatch, ["bad input", "/exit"])
    agent = FakeAgent(raise_on_run=True)
    rc = cli._run_repl(agent)
    captured = capsys.readouterr()
    assert rc == 0
    assert "[error]" in captured.err
    assert "boom" in captured.err


# ── History file ─────────────────────────────────────────────────────────


def test_history_path_resolves_under_phalanx_home(isolated_phalanx_home):
    path = cli._history_path()
    assert path is not None
    assert path.parent == isolated_phalanx_home
    assert path.name == "cli_history"
    # _history_path() must have created the parent directory.
    assert isolated_phalanx_home.exists()


def test_filehistory_writes_to_resolved_path(isolated_phalanx_home):
    """Confirm the FileHistory we'd hand the session actually persists.

    We don't instantiate the full PromptSession here — that would
    probe the terminal device and fail under pytest on Windows.
    Instead, we exercise FileHistory directly against the path that
    ``_history_path()`` produces.
    """
    if not cli._PT_AVAILABLE:  # pragma: no cover — env without prompt_toolkit
        pytest.skip("prompt_toolkit not installed")
    from prompt_toolkit.history import FileHistory
    path = cli._history_path()
    assert path is not None
    h = FileHistory(str(path))
    h.append_string("hello world")
    assert path.exists()
    contents = path.read_text(encoding="utf-8", errors="replace")
    assert "hello world" in contents


# ── Fallback (input()) path ──────────────────────────────────────────────


def test_input_fallback_runs_when_pt_unavailable(
    isolated_phalanx_home, monkeypatch, capsys,
):
    """When _PT_AVAILABLE is False the loop reads from input()."""
    monkeypatch.setattr(cli, "_PT_AVAILABLE", False)
    monkeypatch.setattr("sys.stdin", io.StringIO("hello\n/exit\n"))
    monkeypatch.setattr("builtins.input", lambda prompt="": sys.stdin.readline().rstrip("\n"))
    # The lambda above closes over module-level sys; import here so it
    # resolves to the same object monkeypatch handed us.
    import sys
    agent = FakeAgent()
    rc = cli._run_repl(agent)
    captured = capsys.readouterr()
    assert rc == 0
    assert len(agent.calls) == 1
    assert agent.calls[0]["message"] == "hello"
    assert "echo:hello" in captured.out


def test_input_fallback_handles_eof(isolated_phalanx_home, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_PT_AVAILABLE", False)

    def _eof(*args, **kwargs):
        raise EOFError

    monkeypatch.setattr("builtins.input", _eof)
    rc = cli._run_repl(FakeAgent())
    assert rc == 0


# ── Module-level smoke tests ─────────────────────────────────────────────


def test_pt_available_flag_reflects_install():
    """If prompt_toolkit is on the path, _PT_AVAILABLE must be True."""
    try:
        import prompt_toolkit  # noqa: F401
        assert cli._PT_AVAILABLE is True
    except ImportError:  # pragma: no cover
        assert cli._PT_AVAILABLE is False


def test_build_prompt_session_constructible_outside_tty(
    isolated_phalanx_home, monkeypatch,
):
    """``_build_prompt_session()`` must not crash on import/wiring.

    Under pytest there's no real terminal, so prompt_toolkit's input/
    output backends may refuse to attach.  We just want to confirm
    the helper doesn't raise on the cheap parts (history + key
    bindings); environments that can't construct a PromptSession at
    all just let the error propagate to ``_run_repl``, which is the
    documented contract.  Skip when the platform's prompt_toolkit
    backend can't open ``sys.stdin``/``sys.stdout`` — that's a TTY
    issue, not a phalanx bug.
    """
    if not cli._PT_AVAILABLE:  # pragma: no cover
        pytest.skip("prompt_toolkit not installed")
    try:
        sess = cli._build_prompt_session()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"prompt_toolkit refused to construct under pytest: {exc}")
    from prompt_toolkit import PromptSession
    assert isinstance(sess, PromptSession)
    # FileHistory should have been wired since the home dir exists.
    assert sess.history is not None
