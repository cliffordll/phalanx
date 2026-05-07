"""§2.8.c wave 1 tests — delegate_task tool.

Layers under test:

* :func:`tools.delegate_tool.delegate_task` — argument validation,
  recursion-depth gate, sub-agent construction.
* Sub-agent failure-mode wrapping — every failure path must produce
  a structured tool result, never an exception escaping into the
  parent loop.
* Shared :class:`run_agent.IterationBudget` — sub-agent consumes
  from the same counter as the parent.
* :class:`run_agent.AIAgent.delegation_depth` plumbing — increments
  on each spawn level, blocks at MAX.
* Registry surface — schema sanity, dispatch threading.

All tests use a fake sub-agent factory; no real LLM is invoked.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from run_agent import AIAgent, IterationBudget
from tools import delegate_tool
from tools.delegate_tool import (
    _DELEGATION_DEPTH_MAX,
    _ROLES,
    DELEGATE_SCHEMA,
    delegate_task,
)


# ── Fake sub-agent factory ────────────────────────────────────────────


class _FakeSubAgent:
    """Stand-in returned by patched ``_build_subagent``.

    Mimics the run_conversation contract:
    ``{final_response, messages, api_calls, stop_reason, iterations_used,
       usage_totals}``.  Tests can pre-load *result* / *raises* on the
    instance to drive specific outcomes.
    """

    def __init__(
        self,
        parent: AIAgent,
        *,
        result: Optional[Dict[str, Any]] = None,
        raises: Optional[Exception] = None,
        consume_budget: int = 0,
    ) -> None:
        self.parent = parent
        self.session_id = "sub-session-id-12345"
        self.delegation_depth = parent.delegation_depth + 1
        self._result = result
        self._raises = raises
        self._consume_budget = consume_budget
        self.run_calls: List[str] = []
        self.closed = False

    def run_conversation(self, user_message: str) -> Dict[str, Any]:
        self.run_calls.append(user_message)
        # Walk the shared budget so tests can assert sub-agent consumed it.
        for _ in range(self._consume_budget):
            self.parent.iteration_budget.consume()
        if self._raises:
            raise self._raises
        return self._result or {
            "final_response": "OK",
            "messages": [],
            "api_calls": 1,
            "stop_reason": "completed",
            "iterations_used": self._consume_budget,
            "usage_totals": {"input_tokens": 5, "output_tokens": 3},
        }

    def close(self) -> None:
        self.closed = True


def _patch_factory(monkeypatch, **builder_overrides):
    """Replace ``_build_subagent`` with a lambda that returns a
    pre-configured FakeSubAgent.

    ``builder_overrides`` flows directly into ``_FakeSubAgent.__init__``
    so a single test can dial in the result / failure / budget
    consumption.
    """
    captured: Dict[str, Any] = {}

    def _factory(parent, **overrides):
        captured["parent"] = parent
        captured["overrides"] = overrides
        sub = _FakeSubAgent(parent, **builder_overrides)
        captured["sub"] = sub
        return sub

    monkeypatch.setattr(delegate_tool, "_build_subagent", _factory)
    return captured


# ── Schema / registry ────────────────────────────────────────────────


def test_schema_advertises_required_fields():
    assert DELEGATE_SCHEMA["name"] == "delegate_task"
    props = DELEGATE_SCHEMA["parameters"]["properties"]
    assert "task_description" in props
    assert DELEGATE_SCHEMA["parameters"]["required"] == ["task_description"]
    # Closed-set role enum must include the wave-2 roles too — we
    # advertise the surface even though only executor runs.
    assert set(props["role"]["enum"]) == set(_ROLES)


def test_registry_exposes_delegate_task():
    from tools.registry import registry
    entry = registry.get_entry("delegate_task")
    assert entry is not None
    assert entry.toolset == "delegate"
    assert entry.handler is delegate_task


# ── Argument validation ─────────────────────────────────────────────


def test_missing_caller_agent_returns_error():
    out = json.loads(delegate_task({"task_description": "anything"}))
    assert "error" in out
    assert "caller_agent" in out["error"]


def test_empty_task_description_returns_error(monkeypatch):
    _patch_factory(monkeypatch)
    parent = AIAgent(model="dummy", base_url="", api_key="")
    out = json.loads(delegate_task({"task_description": "   "}, caller_agent=parent))
    assert "error" in out
    assert "task_description" in out["error"]


def test_unknown_role_returns_error(monkeypatch):
    _patch_factory(monkeypatch)
    parent = AIAgent(model="dummy", base_url="", api_key="")
    out = json.loads(delegate_task(
        {"task_description": "do thing", "role": "wizard"},
        caller_agent=parent,
    ))
    assert "error" in out
    assert "role" in out["error"]


def test_zero_max_iterations_returns_error(monkeypatch):
    _patch_factory(monkeypatch)
    parent = AIAgent(model="dummy", base_url="", api_key="")
    out = json.loads(delegate_task(
        {"task_description": "do thing", "max_iterations_subagent": 0},
        caller_agent=parent,
    ))
    assert "error" in out


# ── Recursion-depth gate ────────────────────────────────────────────


def test_depth_zero_succeeds(monkeypatch):
    _patch_factory(monkeypatch)
    parent = AIAgent(model="dummy", base_url="", api_key="")
    parent.delegation_depth = 0
    out = json.loads(delegate_task({"task_description": "do thing"}, caller_agent=parent))
    assert "error" not in out
    assert out["final_response"] == "OK"


def test_depth_one_succeeds(monkeypatch):
    _patch_factory(monkeypatch)
    parent = AIAgent(model="dummy", base_url="", api_key="")
    parent.delegation_depth = 1
    out = json.loads(delegate_task({"task_description": "do thing"}, caller_agent=parent))
    assert "error" not in out


def test_depth_max_blocks(monkeypatch):
    """At depth = MAX, a further spawn would exceed — must reject."""
    _patch_factory(monkeypatch)
    parent = AIAgent(model="dummy", base_url="", api_key="")
    parent.delegation_depth = _DELEGATION_DEPTH_MAX
    out = json.loads(delegate_task({"task_description": "do thing"}, caller_agent=parent))
    assert "error" in out
    assert "depth" in out["error"]


def test_subagent_inherits_depth_plus_one(monkeypatch):
    captured = _patch_factory(monkeypatch)
    parent = AIAgent(model="dummy", base_url="", api_key="")
    parent.delegation_depth = 1
    delegate_task({"task_description": "x"}, caller_agent=parent)
    assert captured["sub"].delegation_depth == 2


# ── Shared IterationBudget ──────────────────────────────────────────


def test_subagent_shares_parent_budget(monkeypatch):
    """Sub-agent consuming the budget must decrement parent's counter."""
    _patch_factory(monkeypatch, consume_budget=3)
    parent = AIAgent(
        model="dummy", base_url="", api_key="",
        iteration_budget=IterationBudget(10),
    )
    assert parent.iteration_budget.remaining == 10
    delegate_task({"task_description": "x"}, caller_agent=parent)
    assert parent.iteration_budget.remaining == 7  # 10 - 3 consumed by sub


def test_factory_passes_parent_budget_object(monkeypatch):
    captured = _patch_factory(monkeypatch)
    parent_budget = IterationBudget(20)
    parent = AIAgent(
        model="dummy", base_url="", api_key="",
        iteration_budget=parent_budget,
    )
    delegate_task({"task_description": "x"}, caller_agent=parent)
    # Factory got the SAME budget object (not a copy).
    assert captured["sub"].parent.iteration_budget is parent_budget


# ── Failure-mode wrapping ───────────────────────────────────────────


def test_subagent_crash_returns_error_not_raise(monkeypatch):
    _patch_factory(monkeypatch, raises=RuntimeError("kaboom"))
    parent = AIAgent(model="dummy", base_url="", api_key="")
    # Must not raise — the parent loop never sees an exception from
    # delegate.
    out = json.loads(delegate_task({"task_description": "x"}, caller_agent=parent))
    assert "error" in out
    assert "kaboom" in out["error"]
    assert out.get("sub_session_id") == "sub-session-id-12345"


def test_subagent_budget_exhausted_returns_structured_result(monkeypatch):
    _patch_factory(monkeypatch, result={
        "final_response": "",
        "messages": [],
        "api_calls": 0,
        "stop_reason": "budget_exhausted",
        "iterations_used": 0,
        "usage_totals": {},
    })
    parent = AIAgent(model="dummy", base_url="", api_key="")
    out = json.loads(delegate_task({"task_description": "x"}, caller_agent=parent))
    assert "error" not in out
    assert out["stop_reason"] == "budget_exhausted"
    assert out["final_response"] == ""


def test_subagent_close_called_on_success(monkeypatch):
    captured = _patch_factory(monkeypatch)
    parent = AIAgent(model="dummy", base_url="", api_key="")
    delegate_task({"task_description": "x"}, caller_agent=parent)
    assert captured["sub"].closed is True


def test_subagent_close_called_on_crash(monkeypatch):
    captured = _patch_factory(monkeypatch, raises=ValueError("oops"))
    parent = AIAgent(model="dummy", base_url="", api_key="")
    delegate_task({"task_description": "x"}, caller_agent=parent)
    assert captured["sub"].closed is True


def test_factory_construction_failure_wrapped(monkeypatch):
    def _bad_factory(parent, **overrides):
        raise RuntimeError("cannot construct")

    monkeypatch.setattr(delegate_tool, "_build_subagent", _bad_factory)
    parent = AIAgent(model="dummy", base_url="", api_key="")
    out = json.loads(delegate_task({"task_description": "x"}, caller_agent=parent))
    assert "error" in out
    assert "construction failed" in out["error"]


# ── Result shape ────────────────────────────────────────────────────


def test_result_carries_expected_keys(monkeypatch):
    _patch_factory(monkeypatch, result={
        "final_response": "the answer",
        "messages": [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": "calling tool",
                "tool_calls": [
                    {"id": "c1", "function": {"name": "echo", "arguments": "{}"}},
                ],
            },
            {"role": "tool", "content": "{}", "tool_name": "echo"},
            {"role": "assistant", "content": "the answer"},
        ],
        "api_calls": 2,
        "stop_reason": "completed",
        "iterations_used": 2,
        "usage_totals": {"input_tokens": 100, "output_tokens": 20},
    })
    parent = AIAgent(model="dummy", base_url="", api_key="")
    out = json.loads(delegate_task({"task_description": "x"}, caller_agent=parent))
    assert out["final_response"] == "the answer"
    assert out["stop_reason"] == "completed"
    assert out["iterations_used"] == 2
    assert out["usage_totals"]["input_tokens"] == 100
    assert out["sub_session_id"] == "sub-session-id-12345"
    assert out["role"] == "executor"
    # tool_calls summarised — name only, no arg replay.
    assert out["tool_calls"] == [{"name": "echo", "id": "c1"}]


def test_role_default_is_executor(monkeypatch):
    _patch_factory(monkeypatch)
    parent = AIAgent(model="dummy", base_url="", api_key="")
    out = json.loads(delegate_task({"task_description": "x"}, caller_agent=parent))
    assert out["role"] == "executor"


def test_role_critic_recorded(monkeypatch):
    _patch_factory(monkeypatch)
    parent = AIAgent(model="dummy", base_url="", api_key="")
    out = json.loads(delegate_task(
        {"task_description": "review this", "role": "critic"},
        caller_agent=parent,
    ))
    # Wave 1 doesn't customise the prompt yet, but the role must be
    # echoed back so wave-2 callers can assert what they got.
    assert out["role"] == "critic"


# ── subject_artifact plumbing ───────────────────────────────────────


def test_subject_artifact_appended_to_user_message(monkeypatch):
    captured = _patch_factory(monkeypatch)
    parent = AIAgent(model="dummy", base_url="", api_key="")
    delegate_task(
        {
            "task_description": "review the patch",
            "subject_artifact": "diff --git a/foo b/foo\n+++ etc",
        },
        caller_agent=parent,
    )
    user_msg = captured["sub"].run_calls[0]
    assert "review the patch" in user_msg
    assert "<artifact>" in user_msg
    assert "diff --git" in user_msg
    assert "</artifact>" in user_msg


def test_no_artifact_means_plain_user_message(monkeypatch):
    captured = _patch_factory(monkeypatch)
    parent = AIAgent(model="dummy", base_url="", api_key="")
    delegate_task({"task_description": "do thing"}, caller_agent=parent)
    user_msg = captured["sub"].run_calls[0]
    assert user_msg == "do thing"
    assert "<artifact>" not in user_msg


# ── share_memory toggle ─────────────────────────────────────────────


def test_share_memory_default_uses_parent_db(monkeypatch, tmp_path):
    """Default share_memory=True passes parent's session_db to factory."""
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    captured = _patch_factory(monkeypatch)
    parent = AIAgent(model="dummy", base_url="", api_key="", session_db=db)
    try:
        delegate_task({"task_description": "x"}, caller_agent=parent)
    finally:
        db.close()
    assert captured["overrides"]["session_db"] is db


def test_share_memory_false_passes_none(monkeypatch, tmp_path):
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    captured = _patch_factory(monkeypatch)
    parent = AIAgent(model="dummy", base_url="", api_key="", session_db=db)
    try:
        delegate_task(
            {"task_description": "x", "share_memory": False},
            caller_agent=parent,
        )
    finally:
        db.close()
    assert captured["overrides"]["session_db"] is None


# ── Dispatch wiring (via registry) ──────────────────────────────────


def test_dispatch_threads_caller_agent(monkeypatch):
    """The registry.dispatch path must thread caller_agent into the
    handler — verifies the wave-1 plumbing in run_agent._dispatch_tool_call.
    """
    from tools.registry import registry

    _patch_factory(monkeypatch)
    parent = AIAgent(model="dummy", base_url="", api_key="")
    raw = registry.dispatch(
        "delegate_task",
        {"task_description": "via dispatch"},
        caller_agent=parent,
    )
    out = json.loads(raw)
    assert "error" not in out, raw


def test_dispatch_without_caller_agent_returns_error():
    """Direct dispatch without caller_agent (no AIAgent in the loop)
    must surface the error rather than crash."""
    from tools.registry import registry
    raw = registry.dispatch("delegate_task", {"task_description": "orphan"})
    out = json.loads(raw)
    assert "error" in out
    assert "caller_agent" in out["error"]


# ── Sub-agent factory smoke (real construction, no run) ─────────────


def test_real_factory_inherits_parent_state(monkeypatch, tmp_path):
    """Without monkeypatching ``_build_subagent``, exercise the real
    construction path enough to confirm inheritance is wired (we don't
    actually call run_conversation, just inspect the built sub-agent).
    """
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    parent_budget = IterationBudget(50)
    parent = AIAgent(
        model="parent-model",
        base_url="https://api.example.com/v1",
        api_key="sk-parent",
        iteration_budget=parent_budget,
        session_db=db,
    )
    parent.delegation_depth = 1

    try:
        sub = delegate_tool._build_subagent(
            parent, max_iterations=15, session_db=db,
        )
        assert sub.model == "parent-model"
        assert sub._base_url == "https://api.example.com/v1"
        assert sub.iteration_budget is parent_budget
        assert sub._parent_session_id == parent.session_id
        assert sub.delegation_depth == 2
        assert sub.platform == "delegate"
        # Hard cap honoured (not stomped by AIAgent default 90).
        assert sub.max_iterations == 15
        sub.close()
    finally:
        db.close()
