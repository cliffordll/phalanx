"""Delegate-task tool — main agent spawns a sub-agent (§2.8.c wave 1).

The ``delegate_task`` tool lets a running ``AIAgent`` arrange for a
**fresh sub-AIAgent** to handle a self-contained subtask, then returns
the sub-agent's outcome as a structured tool result.

Wave 1 ships the minimum viable surface:

* IterationBudget *shared* with the parent — sub-agent consumes from
  the same counter.  This is what makes ``max_iterations=90`` mean
  "total turns across the whole fan-out tree", not "90 per agent".
* ``parent_session_id`` link — ``phalanx session show <parent>`` walks
  the chain and shows the sub-conversation.
* ``delegation_depth`` recursion guard — ``main → A → B`` allowed,
  ``main → A → B → C`` rejected at the third call (configurable via
  :data:`_DELEGATION_DEPTH_MAX`, default 2).  Independent of the
  shared budget; budget bounds *cost*, depth bounds *structural
  complexity*.
* ``share_memory`` (default True) — sub-agent uses the parent's
  SessionDB, so memories persist across the fan-out.  Set False for
  isolated experimentation that shouldn't pollute the user's
  long-term memory store.
* All sub-agent failure modes (API error / budget exhaustion /
  exception) wrap into the tool result; the **parent loop never
  sees an exception** from delegate.  Otherwise a sub-agent crash
  would short-circuit the parent's whole turn.

Wave 2 will add role-aware system prompts (executor / critic /
planner) — for wave 1, ``role`` is accepted and recorded but only
the default executor path runs.  The ``subject_artifact`` parameter
is similarly accepted for forward-compat.

Tool dispatch threads ``caller_agent`` through
:meth:`hermes_state.SessionDB`-style ``**kwargs`` channel; the handler
refuses to spawn anything when it can't see the parent.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from tools.registry import registry, tool_error, tool_result

logger = logging.getLogger(__name__)


# ─── Constants ──────────────────────────────────────────────────────────

_DELEGATION_DEPTH_MAX = 2

# Recognised role values.  Wave 1 only consumes "executor" semantics
# (no system-prompt customisation); the closed set lets the schema
# advertise the wave-2 surface without behavior drift.
_ROLES = ("executor", "critic", "planner")

# Sub-agent default for max_iterations when caller doesn't override.
# Bounded by parent.iteration_budget.remaining at runtime regardless,
# so this is a soft hint not a hard cap.
_DEFAULT_SUB_MAX_ITERATIONS = 20


# ─── Schema (advertised to the model) ──────────────────────────────────

DELEGATE_SCHEMA = {
    "name": "delegate_task",
    "description": (
        "Delegate a self-contained subtask to a fresh sub-agent.  "
        "Use this when the current task can be cleanly split — for "
        "example, asking a critic role to review work the main "
        "agent just produced, or asking a planner role to "
        "decompose a complex request into steps before execution.  "
        "The sub-agent shares the parent's iteration budget so the "
        "total cost stays bounded.  Returns a structured result "
        "with the sub-agent's final response, its tool calls, "
        "token usage, and stop reason.  Cannot be called more than "
        f"{_DELEGATION_DEPTH_MAX} levels deep."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_description": {
                "type": "string",
                "description": (
                    "The user-message prompt the sub-agent will see.  "
                    "Be self-contained; the sub-agent does not see "
                    "the parent's conversation history unless "
                    "share_memory is true and the parent has stored "
                    "relevant long-term memories."
                ),
            },
            "role": {
                "type": "string",
                "enum": list(_ROLES),
                "description": (
                    "Sub-agent role.  'executor' (default) runs as a "
                    "regular agent; 'critic' and 'planner' get "
                    "specialized system prompts (wave 2)."
                ),
                "default": "executor",
            },
            "max_iterations_subagent": {
                "type": "integer",
                "description": (
                    "Soft cap on sub-agent's own iteration count.  The "
                    "shared parent IterationBudget is the actual hard "
                    "cap — whichever is lower wins."
                ),
                "default": _DEFAULT_SUB_MAX_ITERATIONS,
            },
            "share_memory": {
                "type": "boolean",
                "description": (
                    "When true (default), sub-agent uses the same "
                    "SessionDB as the parent so any long-term "
                    "memories the parent stored are visible.  Set "
                    "false for experimentation that should not "
                    "pollute user memory."
                ),
                "default": True,
            },
            "subject_artifact": {
                "type": "string",
                "description": (
                    "Optional content for the sub-agent to operate "
                    "on (a patch to review, a doc to summarise, "
                    "etc.).  Wave 1 records but does not yet "
                    "specially format this; wave 2 wires it into "
                    "the critic system prompt."
                ),
            },
        },
        "required": ["task_description"],
    },
}


# ─── Sub-agent factory (overridable for tests) ─────────────────────────

# Wave 1 keeps a simple module-level factory; tests can monkeypatch
# this to inject a fake AIAgent.  When wave 3 lands the async surface
# we'll grow this into a registry-keyed lookup so different roles can
# point at different model bindings.

def _build_subagent(parent: Any, **overrides: Any) -> Any:
    """Construct a sub-AIAgent that inherits parent budget + session.

    ``overrides`` lets the caller patch specific kwargs (e.g.
    ``max_iterations`` for the ``max_iterations_subagent`` parameter).
    Anything not overridden inherits from *parent*.
    """
    from run_agent import AIAgent

    kwargs: Dict[str, Any] = dict(
        model=parent.model,
        base_url=parent._base_url,
        api_key=parent._api_key,
        provider=parent.provider,
        max_iterations=overrides.get(
            "max_iterations", _DEFAULT_SUB_MAX_ITERATIONS
        ),
        # Shared budget — same Python object reference, sub.consume()
        # decrements parent's counter.
        iteration_budget=parent.iteration_budget,
        session_db=overrides.get("session_db", parent._session_db),
        parent_session_id=parent.session_id,
        platform="delegate",
        verbose_logging=parent.verbose_logging,
        quiet_mode=True,  # don't echo sub-agent banner to user terminal
    )
    sub = AIAgent(**kwargs)
    sub.delegation_depth = parent.delegation_depth + 1
    return sub


# ─── Handler ───────────────────────────────────────────────────────────

def delegate_task(args: Dict[str, Any], **kwargs: Any) -> str:
    """Spawn a sub-agent, run it once, return structured outcome."""
    caller_agent = kwargs.get("caller_agent")
    if caller_agent is None:
        # Defensive — a tool runner that can't surface the calling
        # agent must not silently spawn an orphan.  The CLI dispatch
        # path always sets this; tests / direct calls that miss it
        # should fail loudly so we notice.
        return tool_error(
            "delegate_task: caller_agent is required (dispatch must "
            "pass caller_agent=<parent AIAgent>)"
        )

    task_description = (args.get("task_description") or "").strip()
    if not task_description:
        return tool_error(
            "delegate_task: task_description is required and must be "
            "non-empty"
        )

    role = args.get("role") or "executor"
    if role not in _ROLES:
        return tool_error(
            f"delegate_task: role must be one of {list(_ROLES)}, got "
            f"{role!r}"
        )

    # Recursion guard — independent of IterationBudget.  Caller's depth
    # is the depth at which this tool fires; blocked when *the next
    # level* would exceed _DELEGATION_DEPTH_MAX.  E.g. depth=0 in main
    # → spawn produces depth=1 (allowed); depth=2 would produce depth=3
    # (rejected when MAX=2).
    current_depth = getattr(caller_agent, "delegation_depth", 0)
    if current_depth >= _DELEGATION_DEPTH_MAX:
        return tool_error(
            f"delegate_task: max delegation depth "
            f"{_DELEGATION_DEPTH_MAX} reached "
            f"(caller is at depth {current_depth})"
        )

    raw_sub_max = args.get("max_iterations_subagent")
    if raw_sub_max is None:
        sub_max_iterations = _DEFAULT_SUB_MAX_ITERATIONS
    else:
        try:
            sub_max_iterations = int(raw_sub_max)
        except (TypeError, ValueError):
            return tool_error(
                "delegate_task: max_iterations_subagent must be an integer"
            )
    if sub_max_iterations <= 0:
        return tool_error(
            "delegate_task: max_iterations_subagent must be > 0"
        )

    share_memory = bool(args.get("share_memory", True))
    subject_artifact = args.get("subject_artifact")

    # Construct sub-agent.  Failures here (bad parent state, missing
    # AIAgent class, etc.) wrap as tool_error rather than propagate.
    try:
        sub_agent = _build_subagent(
            caller_agent,
            max_iterations=sub_max_iterations,
            session_db=(caller_agent._session_db if share_memory else None),
        )
    except Exception as exc:
        logger.exception("delegate_task: sub-agent construction failed")
        return tool_error(
            f"delegate_task: sub-agent construction failed: "
            f"{type(exc).__name__}: {exc}"
        )

    # Wave 2 will format ``role`` + ``subject_artifact`` into the
    # sub-agent's system prompt.  Wave 1 just appends artifact (when
    # present) to the user message verbatim so the surface is wired
    # end-to-end and tests can assert it shows up.
    user_message = task_description
    if subject_artifact:
        user_message = (
            f"{task_description}\n\n"
            f"<artifact>\n{subject_artifact}\n</artifact>"
        )

    # Run sub-agent, wrap every failure mode as a structured tool
    # result so the parent loop sees PASS/FAIL semantics, not
    # exceptions.
    try:
        result = sub_agent.run_conversation(user_message)
    except Exception as exc:
        logger.exception("delegate_task: sub-agent run_conversation crashed")
        # Best-effort cleanup.
        try:
            sub_agent.close()
        except Exception:
            pass
        return tool_error(
            f"delegate_task: sub-agent crashed: "
            f"{type(exc).__name__}: {exc}",
            sub_session_id=getattr(sub_agent, "session_id", None),
        )

    # Normal-shape tool result.  All keys are JSON-serialisable.
    out: Dict[str, Any] = {
        "final_response": result.get("final_response", ""),
        "tool_calls": _tool_calls_summary(result.get("messages") or []),
        "usage_totals": dict(result.get("usage_totals") or {}),
        "stop_reason": result.get("stop_reason") or "unknown",
        "iterations_used": int(result.get("iterations_used") or 0),
        "sub_session_id": getattr(sub_agent, "session_id", None),
        "role": role,
    }

    try:
        sub_agent.close()
    except Exception:
        pass

    return tool_result(out)


# ─── Helpers ───────────────────────────────────────────────────────────

def _tool_calls_summary(messages: list) -> list:
    """Extract a compact summary of tool calls from sub-agent messages.

    Format mirrors ``hermes_cli.eval._extract_tool_calls`` so the
    parent's downstream code (eval, debug printing) treats sub-agent
    tool calls the same way as top-level ones.  No arguments parsing —
    just name + call_id for the wave-1 surface.
    """
    summary: list = []
    for msg in messages or []:
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            try:
                fn = tc.get("function") or {}
                summary.append({
                    "name": fn.get("name") or tc.get("name"),
                    "id": tc.get("id"),
                })
            except Exception:
                # Malformed entry — keep going so one bad row doesn't
                # erase the whole list.
                continue
    return summary


def _check_delegate_requirements() -> bool:
    """Delegate is always available — no external deps."""
    return True


# ─── Registration ──────────────────────────────────────────────────────

registry.register(
    name="delegate_task",
    toolset="delegate",
    schema=DELEGATE_SCHEMA,
    handler=delegate_task,
    check_fn=_check_delegate_requirements,
    description=(
        "Delegate a sub-task to a fresh sub-AIAgent (shared "
        "IterationBudget, depth-capped at "
        f"{_DELEGATION_DEPTH_MAX})."
    ),
    emoji="🤝",
)
