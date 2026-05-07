"""§2.8.c wave 2 tests — critic / planner roles + --critic-model + /critic.

Layered coverage:

* Role-specific system prompts — critic / planner inject the
  forced-output template via ``ephemeral_system_prompt``; executor
  passes None.
* ``subject_artifact`` placement — critic puts it in the system
  slot (so the model sees "you are reviewing X" framing); executor
  appends to the user message; planner ignores.
* ``model_override`` — sub-agent uses the override model; absent →
  inherits parent's model.
* :func:`tools.delegate_tool.extract_verdict` — pulls the verdict
  word from a critic response, returns None when absent.
* CLI ``phalanx oneshot --critic-model X`` — runs critic post-pass,
  prints a delimited block, doesn't change the main exit code.
* REPL ``/critic`` slash — reviews the last assistant message,
  acceptable when no history yet, surfaces critic errors.

Sub-agent factory is monkeypatched in every test so no real LLM
fires.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from run_agent import AIAgent, IterationBudget
from tools import delegate_tool
from tools.delegate_tool import (
    _ROLE_SYSTEM_PROMPTS,
    delegate_task,
    extract_verdict,
)


# ── Reuse FakeSubAgent + factory-patch helper from wave 1 ────────────


class _FakeSubAgent:
    def __init__(
        self,
        parent: AIAgent,
        *,
        result: Optional[Dict[str, Any]] = None,
        ephemeral_system_prompt: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        self.parent = parent
        self.session_id = "sub-fake-session"
        self.delegation_depth = parent.delegation_depth + 1
        self._result = result
        self.ephemeral_system_prompt = ephemeral_system_prompt
        self.model = model or parent.model
        self.run_calls: List[str] = []
        self.closed = False

    def run_conversation(self, user_message: str) -> Dict[str, Any]:
        self.run_calls.append(user_message)
        return self._result or {
            "final_response": "ok",
            "messages": [],
            "api_calls": 1,
            "stop_reason": "completed",
            "iterations_used": 1,
            "usage_totals": {"input_tokens": 1, "output_tokens": 1},
        }

    def close(self) -> None:
        self.closed = True


def _patch_factory(monkeypatch, *, result=None):
    """Capture every kwarg passed into _build_subagent and return the
    captured dict so tests can assert on what the handler tried to
    construct."""
    captured: Dict[str, Any] = {}

    def _factory(parent, **overrides):
        captured["parent"] = parent
        captured["overrides"] = overrides
        sub = _FakeSubAgent(
            parent,
            result=result,
            ephemeral_system_prompt=overrides.get("ephemeral_system_prompt"),
            model=overrides.get("model"),
        )
        captured["sub"] = sub
        return sub

    monkeypatch.setattr(delegate_tool, "_build_subagent", _factory)
    return captured


# ── Role system-prompt routing ───────────────────────────────────────


def test_executor_role_passes_none_ephemeral(monkeypatch):
    """Default executor must not inject any role prompt — the sub-agent
    should see the standard build_system_prompt output unchanged."""
    captured = _patch_factory(monkeypatch)
    parent = AIAgent(model="dummy", base_url="", api_key="")
    delegate_task({"task_description": "x"}, caller_agent=parent)
    assert captured["overrides"]["ephemeral_system_prompt"] is None


def test_critic_role_injects_critic_prompt(monkeypatch):
    captured = _patch_factory(monkeypatch)
    parent = AIAgent(model="dummy", base_url="", api_key="")
    delegate_task(
        {"task_description": "review this", "role": "critic"},
        caller_agent=parent,
    )
    eph = captured["overrides"]["ephemeral_system_prompt"] or ""
    assert eph, "critic role should set ephemeral_system_prompt"
    # Critic prompt contract
    assert "VERDICT:" in eph
    assert "ACCEPT" in eph and "REJECT" in eph and "REVISE" in eph
    # Should be the canonical template (or an extension of it)
    assert _ROLE_SYSTEM_PROMPTS["critic"] in eph


def test_planner_role_injects_planner_prompt(monkeypatch):
    captured = _patch_factory(monkeypatch)
    parent = AIAgent(model="dummy", base_url="", api_key="")
    delegate_task(
        {"task_description": "plan a refactor", "role": "planner"},
        caller_agent=parent,
    )
    eph = captured["overrides"]["ephemeral_system_prompt"] or ""
    assert eph, "planner role should set ephemeral_system_prompt"
    assert "ESTIMATE:" in eph
    assert "numbered" in eph.lower() or "decompose" in eph.lower()


# ── subject_artifact placement (role-dependent) ──────────────────────


def test_critic_artifact_in_system_prompt(monkeypatch):
    captured = _patch_factory(monkeypatch)
    parent = AIAgent(model="dummy", base_url="", api_key="")
    delegate_task(
        {
            "task_description": "review patch",
            "role": "critic",
            "subject_artifact": "diff --git a/foo b/foo",
        },
        caller_agent=parent,
    )
    eph = captured["overrides"]["ephemeral_system_prompt"]
    user_msg = captured["sub"].run_calls[0]
    assert "<artifact>" in eph
    assert "diff --git" in eph
    # User message stays as the task description — not the artifact.
    assert "<artifact>" not in user_msg
    assert user_msg == "review patch"


def test_executor_artifact_in_user_message(monkeypatch):
    captured = _patch_factory(monkeypatch)
    parent = AIAgent(model="dummy", base_url="", api_key="")
    delegate_task(
        {
            "task_description": "execute this",
            "subject_artifact": "some content",
        },
        caller_agent=parent,
    )
    eph = captured["overrides"]["ephemeral_system_prompt"]
    user_msg = captured["sub"].run_calls[0]
    assert eph is None  # executor has no role prompt
    assert "<artifact>" in user_msg
    assert "some content" in user_msg


def test_planner_ignores_artifact(monkeypatch):
    """Planner role does not act on subject_artifact in wave 2.  The
    artifact must NOT leak into the system prompt or user message."""
    captured = _patch_factory(monkeypatch)
    parent = AIAgent(model="dummy", base_url="", api_key="")
    delegate_task(
        {
            "task_description": "plan something",
            "role": "planner",
            "subject_artifact": "irrelevant content",
        },
        caller_agent=parent,
    )
    eph = captured["overrides"]["ephemeral_system_prompt"] or ""
    user_msg = captured["sub"].run_calls[0]
    assert "irrelevant content" not in eph
    assert "irrelevant content" not in user_msg


# ── model_override ───────────────────────────────────────────────────


def test_model_override_passes_through(monkeypatch):
    captured = _patch_factory(monkeypatch)
    parent = AIAgent(
        model="parent-big", base_url="https://x/v1", api_key="sk-x",
    )
    delegate_task(
        {
            "task_description": "review",
            "role": "critic",
            "model_override": "critic-mini",
        },
        caller_agent=parent,
    )
    assert captured["overrides"]["model"] == "critic-mini"


def test_no_model_override_inherits_parent(monkeypatch):
    captured = _patch_factory(monkeypatch)
    parent = AIAgent(
        model="parent-big", base_url="https://x/v1", api_key="sk-x",
    )
    delegate_task({"task_description": "review"}, caller_agent=parent)
    # Override key absent (or None) — _build_subagent's _pick falls
    # back to parent.model.  Verify by exercising the real factory:
    assert captured["overrides"].get("model") in (None, "")


def test_real_factory_model_override_wins(monkeypatch, tmp_path):
    """End-to-end: a model_override actually lands on the sub-AIAgent
    instance's .model attribute, not just the kwargs blob."""
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    parent = AIAgent(
        model="parent-big", base_url="https://x/v1", api_key="sk-x",
        iteration_budget=IterationBudget(50),
    )
    sub = delegate_tool._build_subagent(
        parent, max_iterations=10, model="critic-mini",
    )
    try:
        assert sub.model == "critic-mini"
    finally:
        sub.close()


def test_real_factory_falsy_model_falls_back(monkeypatch, tmp_path):
    """``model=None`` (the CLI's no-flag default) must NOT clobber
    parent.model — _pick treats falsy override as 'not specified'."""
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    parent = AIAgent(
        model="parent-big", base_url="https://x/v1", api_key="sk-x",
        iteration_budget=IterationBudget(50),
    )
    sub = delegate_tool._build_subagent(
        parent, max_iterations=10, model=None,
    )
    try:
        assert sub.model == "parent-big"
    finally:
        sub.close()


# ── extract_verdict helper ───────────────────────────────────────────


def test_extract_verdict_accept():
    text = "1. minor issue\n\nVERDICT: ACCEPT\n"
    assert extract_verdict(text) == "ACCEPT"


def test_extract_verdict_reject_in_middle_of_response():
    text = (
        "1. blocker: foo\n"
        "2. nitpick: bar\n"
        "VERDICT: REJECT\n"
        "p.s. trailing comment after verdict\n"
    )
    assert extract_verdict(text) == "REJECT"


def test_extract_verdict_revise_case_insensitive():
    text = "verdict: revise"
    assert extract_verdict(text) == "REVISE"


def test_extract_verdict_missing_returns_none():
    text = "I think it's fine, no issues found."
    assert extract_verdict(text) is None


def test_extract_verdict_empty_returns_none():
    assert extract_verdict("") is None
    assert extract_verdict(None) is None


def test_extract_verdict_invalid_word_returns_none():
    """A 'VERDICT: MAYBE' line is not a valid contract — must return
    None so callers know the critic violated the protocol."""
    text = "VERDICT: MAYBE"
    assert extract_verdict(text) is None


# ── --critic-model CLI integration ───────────────────────────────────


def _run_cli(monkeypatch, capsys, argv, **fake_run_overrides):
    """Drive ``hermes_cli.main.main`` in-process and return
    ``(rc, stdout, stderr)``.

    Patches ``run_agent.AIAgent.run_conversation`` to return a
    canned result dict, so no real LLM runs.  ``fake_run_overrides``
    is merged into the canned response.
    """
    canned = {
        "final_response": "main agent reply",
        "messages": [],
        "api_calls": 1,
        "stop_reason": "completed",
        "iterations_used": 1,
        "usage_totals": {},
    }
    canned.update(fake_run_overrides)

    def _fake_run_conversation(self, *a, **kw):
        return canned

    monkeypatch.setattr(
        "run_agent.AIAgent.run_conversation", _fake_run_conversation,
    )
    monkeypatch.setattr("sys.argv", ["phalanx", *argv])

    from hermes_cli.main import main
    rc = main()
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def test_critic_model_flag_runs_critic(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://x/v1")
    # Patch the sub-agent factory globally so the critic spawn produces
    # a deterministic response.
    _patch_factory(monkeypatch, result={
        "final_response": "1. file foo.py: typo bar\n\nVERDICT: REVISE",
        "messages": [],
        "api_calls": 1,
        "stop_reason": "completed",
        "iterations_used": 1,
        "usage_totals": {},
    })
    rc, out, err = _run_cli(
        monkeypatch, capsys,
        ["--model", "main-big", "oneshot",
         "--critic-model", "critic-mini",
         "Refactor src/foo.py"],
    )
    assert rc == 0, err
    assert "main agent reply" in out
    # Critic block delimiter + content
    assert "──────── critic" in out
    assert "VERDICT: REVISE" in out
    assert "critic-mini" in out


def test_no_critic_model_skips_critic_block(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://x/v1")
    rc, out, err = _run_cli(
        monkeypatch, capsys,
        ["--model", "main-big", "oneshot", "Refactor"],
    )
    assert rc == 0, err
    assert "main agent reply" in out
    assert "──────── critic" not in out


def test_critic_block_warns_when_verdict_missing(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://x/v1")
    _patch_factory(monkeypatch, result={
        "final_response": "Looks fine to me, no concerns.",
        "messages": [],
        "api_calls": 1,
        "stop_reason": "completed",
        "iterations_used": 1,
        "usage_totals": {},
    })
    rc, out, err = _run_cli(
        monkeypatch, capsys,
        ["--model", "main-big", "oneshot",
         "--critic-model", "critic-mini", "do thing"],
    )
    assert rc == 0
    assert "no VERDICT line" in out


def test_critic_skipped_when_main_response_empty(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://x/v1")
    rc, out, err = _run_cli(
        monkeypatch, capsys,
        ["--model", "main-big", "oneshot",
         "--critic-model", "critic-mini", "do thing"],
        final_response="   ",
    )
    assert rc == 0
    assert "──────── critic" not in out
    assert "main response was empty" in err


# ── /critic REPL slash ───────────────────────────────────────────────


def test_repl_critic_help(capsys):
    from cli import _cmd_critic
    _cmd_critic("help", {"agent": None, "history": []})
    out = capsys.readouterr().out
    assert "/critic" in out
    assert "VERDICT" in out or "review" in out.lower()


def test_repl_critic_no_history_message(capsys):
    from cli import _cmd_critic
    _cmd_critic("", {"agent": None, "history": []})
    out = capsys.readouterr().out
    assert "no assistant reply" in out


def test_repl_critic_runs_on_last_assistant(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    _patch_factory(monkeypatch, result={
        "final_response": "1. small typo\n\nVERDICT: ACCEPT",
        "messages": [],
        "api_calls": 1,
        "stop_reason": "completed",
        "iterations_used": 1,
        "usage_totals": {},
    })
    parent = AIAgent(model="dummy", base_url="", api_key="")
    history = [
        {"role": "user", "content": "what is 2+2?"},
        {"role": "assistant", "content": "4."},
    ]
    from cli import _cmd_critic
    _cmd_critic("", {"agent": parent, "history": history})
    out = capsys.readouterr().out
    assert "──────── critic ────────" in out
    assert "VERDICT: ACCEPT" in out


def test_repl_critic_surfaces_subagent_error(monkeypatch, capsys, tmp_path):
    """Empty critic response → still print the block with 'no VERDICT'
    advisory, not crash."""
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    _patch_factory(monkeypatch, result={
        "final_response": "",
        "messages": [],
        "api_calls": 0,
        "stop_reason": "empty",
        "iterations_used": 0,
        "usage_totals": {},
    })
    parent = AIAgent(model="dummy", base_url="", api_key="")
    history = [
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "a"},
    ]
    from cli import _cmd_critic
    _cmd_critic("", {"agent": parent, "history": history})
    out = capsys.readouterr().out
    assert "no VERDICT" in out
