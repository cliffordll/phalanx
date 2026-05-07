"""§2.8.c wave 4 — delegate × memory × budget × depth integration.

Three regression tests that cover compositions wave 1-3 each
introduced piecewise but didn't verify together:

* Memory injection at sub-agent turn 0 — when ``share_memory=True``
  the sub-agent's first system prompt must carry the parent's
  long-term memories.  The compressor doesn't enter this picture
  until message counts grow, so the assertion is on the system
  slot of the sub-agent's first API call.
* Shared :class:`run_agent.IterationBudget` — sub-agent that
  *thinks* it has 20 max-iterations actually only burns from the
  parent's remaining budget (the smaller of its own cap and the
  shared remaining wins).
* Delegation depth capped at :data:`tools.delegate_tool._DELEGATION_DEPTH_MAX`
  — main → A (depth=1) → B (depth=2) is allowed; A's tool call to
  spawn C (would-be depth=3) is rejected by the depth gate
  *without* exhausting the budget.

All sub-agent runs use the real ``run_conversation`` flow with a
stub OpenAI client — the goal is to exercise the integration
points (memory hook + budget plumbing + depth gate), not the
network.
"""

from __future__ import annotations

from typing import Any, Dict, List

from hermes_state import SessionDB
from run_agent import AIAgent, IterationBudget
from tests.conftest import (
    StubClient,
    make_text_response,
)
from tools import delegate_tool
from tools.delegate_tool import _DELEGATION_DEPTH_MAX, delegate_task


# ── Test 1: memory inheritance via share_memory=True ─────────────────


def test_delegate_inherits_memory_through_share_memory(
    monkeypatch, tmp_path,
):
    """sub-agent's first API call carries the parent's memory block.

    The chain:
      1. parent agent has a SessionDB with one pinned memory
      2. parent calls delegate_task(role="executor", share_memory=True)
      3. sub-agent constructed with parent's session_db
      4. sub.run_conversation() runs — turn 0 should inject the
         memory into the sub's system prompt
      5. stub OpenAI captures the request; we assert the memory is
         in the system slot the model would have seen
    """
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    db = SessionDB(db_path=tmp_path / "state.db")
    db.store_memory(
        "preference", "user prefers terse pytest-style answers",
        scope="global", pinned=True,
    )

    parent = AIAgent(
        model="parent-model", api_key="sk-x",
        base_url="https://api.example.com/v1",
        session_db=db,
    )

    # Stub the OpenAI factory so the sub-agent's run hits a fake.
    sub_stub = StubClient([make_text_response("sub done")])
    monkeypatch.setattr("run_agent.OpenAI", lambda *a, **kw: sub_stub)

    try:
        raw = delegate_task(
            {"task_description": "what is X"},
            caller_agent=parent,
        )
    finally:
        parent.close()
        db.close()

    import json as _json
    out = _json.loads(raw)
    assert "error" not in out, raw
    assert out["final_response"] == "sub done"

    # Sub-agent's first API call payload must contain the memory.
    assert sub_stub.calls, "sub-agent never called the model"
    sys_msg = sub_stub.calls[0]["messages"][0]
    assert sys_msg["role"] == "system"
    assert "<memory-context>" in sys_msg["content"]
    assert "user prefers terse pytest-style answers" in sys_msg["content"]


def test_delegate_isolates_memory_with_share_memory_false(
    monkeypatch, tmp_path,
):
    """share_memory=False severs the SessionDB binding so the sub
    agent runs without the parent's memories — for sandboxed roles
    that shouldn't be polluted by user preferences (e.g. neutral
    fact-checker)."""
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    db = SessionDB(db_path=tmp_path / "state.db")
    db.store_memory(
        "preference", "highly distinctive memory token XYZ-isolation",
        scope="global", pinned=True,
    )

    parent = AIAgent(
        model="parent-model", api_key="sk-x",
        base_url="https://api.example.com/v1",
        session_db=db,
    )

    sub_stub = StubClient([make_text_response("sub done")])
    monkeypatch.setattr("run_agent.OpenAI", lambda *a, **kw: sub_stub)

    try:
        delegate_task(
            {"task_description": "isolate this", "share_memory": False},
            caller_agent=parent,
        )
    finally:
        parent.close()
        db.close()

    sys_content = sub_stub.calls[0]["messages"][0]["content"]
    # Memory must NOT have been injected.
    assert "XYZ-isolation" not in sys_content
    assert "<memory-context>" not in sys_content


# ── Test 2: shared IterationBudget ───────────────────────────────────


def test_delegate_shares_iteration_budget_end_to_end(monkeypatch, tmp_path):
    """Parent budget=10, parent has already used 6; sub asks for max
    20 but the shared budget caps it at 4 remaining.

    Setup:
      * Parent IterationBudget(10), pre-consume 6 to simulate prior
        turns
      * Sub max_iterations_subagent=20 (its own cap)
      * Sub gets a chain of stub responses that would let it run 20
        turns if budget allowed
      * Assert: sub.iterations_used <= 4 and budget.remaining == 0
    """
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    parent_budget = IterationBudget(10)
    for _ in range(6):
        parent_budget.consume()
    assert parent_budget.remaining == 4
    assert parent_budget.used == 6

    parent = AIAgent(
        model="parent-model", api_key="sk-x",
        base_url="https://api.example.com/v1",
        iteration_budget=parent_budget,
    )

    # Build a long chain: sub-agent will try to call echo over and over,
    # but budget should run out long before 20 turns.
    from tests.conftest import make_tool_response

    responses: List[Any] = []
    for i in range(20):
        responses.append(
            make_tool_response([(f"call-{i}", "echo", '{"text":"x"}')])
        )
    responses.append(make_text_response("done"))

    sub_stub = StubClient(responses)
    monkeypatch.setattr("run_agent.OpenAI", lambda *a, **kw: sub_stub)

    try:
        raw = delegate_task(
            {
                "task_description": "loop a bunch",
                "max_iterations_subagent": 20,
            },
            caller_agent=parent,
        )
    finally:
        parent.close()

    import json as _json
    out = _json.loads(raw)
    assert "error" not in out, raw

    # Parent's shared budget must now be at 0 — sub consumed it down.
    assert parent_budget.remaining == 0
    # Sub burned exactly the parent's remaining budget (4 of 4).  Note
    # ``iterations_used`` in the result dict reflects the SHARED
    # budget's cumulative ``used`` (parent's 6 + sub's burn) because
    # the sub looks at the same IterationBudget instance — that's the
    # whole point of sharing.
    assert out["iterations_used"] == 10
    assert out["iterations_used"] - 6 == 4  # sub's actual delta
    # Stop reason: either ``budget_exhausted`` (consume() failed
    # mid-turn) or ``completed`` (loop exited cleanly because
    # remaining==0 at the next while-check).  Both prove the budget
    # was actually exhausted by the sub — the distinction is just
    # about which side of consume() the cap fired on.
    assert out["stop_reason"] in ("budget_exhausted", "completed")


# ── Test 3: delegation depth capped ──────────────────────────────────


def test_delegate_depth_capped_blocks_third_level(monkeypatch, tmp_path):
    """Build a parent at depth 0 → spawn delegate (sub at depth 1)
    → from inside the sub, attempt another delegate.  Sub is at
    depth 1, MAX is 2, so spawning would yield depth 2 which is the
    boundary — that's allowed.  The truly deep path fires when the
    sub's *delegation_depth* reaches MAX and tries to spawn one
    more level.  We test by seeding a synthetic agent at depth=MAX
    and confirming its delegate_task call is rejected.
    """
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))

    # Synthetic agent whose depth equals MAX — equivalent to "we're
    # already at the deepest allowed sub-agent".
    parent = AIAgent(model="dummy", api_key="", base_url="")
    parent.delegation_depth = _DELEGATION_DEPTH_MAX

    # No need to monkeypatch _build_subagent: the depth gate fires
    # before construction.
    import json as _json
    raw = delegate_task(
        {"task_description": "should be rejected"},
        caller_agent=parent,
    )
    out = _json.loads(raw)
    assert "error" in out
    assert "depth" in out["error"].lower()


def test_delegate_depth_one_succeeds(monkeypatch, tmp_path):
    """Sanity inverse: the boundary itself (sub at depth 1 calling
    delegate to depth 2) IS allowed when MAX=2."""
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))

    captured: Dict[str, Any] = {}

    def _fake_factory(parent, **overrides):
        from run_agent import AIAgent

        # Build a real AIAgent that wraps a stub OpenAI to produce
        # one response.  We monkeypatch the OpenAI() call.
        captured["parent_depth"] = parent.delegation_depth
        sub = AIAgent(
            model=overrides.get("model") or parent.model,
            api_key=parent._api_key, base_url=parent._base_url,
            iteration_budget=parent.iteration_budget,
            max_iterations=overrides.get("max_iterations", 20),
            ephemeral_system_prompt=overrides.get("ephemeral_system_prompt"),
            quiet_mode=True,
        )
        sub.delegation_depth = parent.delegation_depth + 1
        captured["sub_depth"] = sub.delegation_depth
        return sub

    monkeypatch.setattr(delegate_tool, "_build_subagent", _fake_factory)

    parent = AIAgent(model="dummy", api_key="sk-x", base_url="https://x/v1")
    parent.delegation_depth = 1  # one below MAX

    sub_stub = StubClient([make_text_response("ok")])
    monkeypatch.setattr("run_agent.OpenAI", lambda *a, **kw: sub_stub)

    try:
        import json as _json
        raw = delegate_task(
            {"task_description": "inner"},
            caller_agent=parent,
        )
    finally:
        parent.close()

    out = _json.loads(raw)
    assert "error" not in out, raw
    assert captured["parent_depth"] == 1
    assert captured["sub_depth"] == 2  # MAX boundary, allowed


# ── Test 4: parent_session_id chain ──────────────────────────────────


def test_delegate_links_sub_session_to_parent(monkeypatch, tmp_path):
    """phalanx session show <parent> should be able to walk the
    chain — wave 1 set parent_session_id, this test pins the linkage
    end to end against a real SessionDB."""
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    db = SessionDB(db_path=tmp_path / "state.db")
    parent = AIAgent(
        model="parent-model", api_key="sk-x",
        base_url="https://x/v1",
        session_db=db,
    )

    sub_stub = StubClient([make_text_response("hi")])
    monkeypatch.setattr("run_agent.OpenAI", lambda *a, **kw: sub_stub)

    try:
        # Force parent's session row to exist (run_conversation
        # normally does this, but we want to call delegate before
        # any parent turn lands).
        db.create_session(parent.session_id, source="test")

        import json as _json
        raw = delegate_task(
            {"task_description": "child task"},
            caller_agent=parent,
        )
    finally:
        parent.close()

    out = _json.loads(raw)
    sub_id = out["sub_session_id"]
    assert sub_id

    # Re-open db (close/reopen pattern) and walk the parent_session_id
    # of the sub row.
    db.close()
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        sub_row = db.get_session(sub_id)
        assert sub_row is not None
        assert sub_row["parent_session_id"] == parent.session_id
    finally:
        db.close()
