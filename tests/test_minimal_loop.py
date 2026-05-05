"""Phase 2.1.5 — minimal loop tests.

Covers ``run_agent.AIAgent.run_conversation`` with the real
``tools.registry`` singleton and the auto-loaded ``echo_tool``.
OpenAI is mocked via the ``stub_openai`` fixture from conftest.
"""

from __future__ import annotations

import json

from run_agent import AIAgent, IterationBudget
from tests.conftest import make_text_response, make_tool_response


# ── IterationBudget ───────────────────────────────────────────────────


class TestIterationBudget:
    def test_consume_decrements_remaining(self):
        b = IterationBudget(5)
        assert b.consume() is True
        assert b.used == 1
        assert b.remaining == 4

    def test_consume_returns_false_when_exhausted(self):
        b = IterationBudget(2)
        assert b.consume() is True
        assert b.consume() is True
        assert b.consume() is False
        assert b.remaining == 0

    def test_refund_returns_a_slot(self):
        b = IterationBudget(3)
        b.consume()
        b.consume()
        b.refund()
        assert b.used == 1
        assert b.remaining == 2

    def test_refund_below_zero_is_clamped(self):
        b = IterationBudget(1)
        b.refund()
        b.refund()
        assert b.used == 0


# ── AIAgent construction ──────────────────────────────────────────────


class TestAIAgentInit:
    def test_defaults_set(self):
        a = AIAgent(model="gpt-test")
        assert a.model == "gpt-test"
        assert a.max_iterations == 90
        assert a.iteration_budget.max_total == 90
        assert a.session_id  # non-empty uuid
        a.close()

    def test_base_url_property_setter(self):
        a = AIAgent(model="m", base_url="https://x/v1")
        assert a.base_url == "https://x/v1"
        a.base_url = "https://y/v2"
        assert a.base_url == "https://y/v2"
        a.close()

    def test_resolve_tool_schemas_includes_echo(self):
        """echo_tool auto-loads via tools/__init__.py — must show up."""
        a = AIAgent(model="m")
        schemas = a._resolve_tool_schemas()
        names = [s["function"]["name"] for s in schemas]
        assert "echo" in names
        a.close()


# ── run_conversation: no-tool / direct-answer path ────────────────────


def test_loop_exits_after_one_turn_when_no_tool_calls(stub_openai):
    stub_openai([make_text_response("Hello there.")])
    agent = AIAgent(model="gpt-test", api_key="sk-x", base_url="https://x/v1")
    try:
        result = agent.run_conversation("hi")
    finally:
        agent.close()

    assert result["api_calls"] == 1
    assert result["stop_reason"] == "completed"
    assert result["final_response"] == "Hello there."
    assert [m["role"] for m in result["messages"]] == ["system", "user", "assistant"]


# ── run_conversation: full tool-call closure ──────────────────────────


def test_loop_dispatches_real_echo_tool(stub_openai):
    stub = stub_openai([
        make_tool_response([("call_1", "echo", '{"text":"hi","uppercase":true}')]),
        make_text_response("Echoed: HI"),
    ])
    agent = AIAgent(model="gpt-test", api_key="sk-x", base_url="https://x/v1",
                    tool_delay=0)
    try:
        result = agent.run_conversation("echo hi")
    finally:
        agent.close()

    # Two API calls: tool_call → tool_result → final.
    assert result["api_calls"] == 2
    assert result["stop_reason"] == "completed"
    assert result["final_response"] == "Echoed: HI"
    roles = [m["role"] for m in result["messages"]]
    assert roles == ["system", "user", "assistant", "tool", "assistant"]

    # The tool message holds the real echo handler's JSON output.
    tool_payload = json.loads(result["messages"][3]["content"])
    assert tool_payload["text"] == "HI"
    assert tool_payload["call_count"] == 1

    # The first API call should have surfaced the echo tool to the model.
    first_call = stub.calls[0]
    tool_names = [t["function"]["name"] for t in first_call["tools"]]
    assert "echo" in tool_names


def test_unknown_tool_returns_error_marker(stub_openai):
    stub_openai([
        make_tool_response([("call_1", "doesnotexist", "{}")]),
        make_text_response("Sorry, can't."),
    ])
    agent = AIAgent(model="gpt-test", api_key="sk-x", base_url="https://x/v1",
                    tool_delay=0)
    try:
        result = agent.run_conversation("...")
    finally:
        agent.close()

    # Loop survives — registry's dispatch returns an error JSON instead of raising.
    assert result["stop_reason"] == "completed"
    tool_msg = result["messages"][3]
    assert tool_msg["role"] == "tool"
    assert "Unknown tool" in tool_msg["content"]


# ── run_conversation: budget enforcement ──────────────────────────────


def test_max_iterations_short_circuits(stub_openai):
    """If the model never returns a final answer, max_iterations stops the loop."""
    # Queue more tool-call responses than max_iterations allows.
    stub_openai([
        make_tool_response([("call_a", "echo", '{"text":"a"}')]),
        make_tool_response([("call_b", "echo", '{"text":"b"}')]),
        make_tool_response([("call_c", "echo", '{"text":"c"}')]),
    ])
    agent = AIAgent(model="gpt-test", api_key="sk-x", base_url="https://x/v1",
                    max_iterations=2, tool_delay=0)
    try:
        result = agent.run_conversation("loop")
    finally:
        agent.close()

    assert result["api_calls"] == 2
    # Loop exited via budget exhaustion or max_iterations gate, both acceptable.
    assert result["stop_reason"] in {"max_iterations", "budget_exhausted", "completed"}
    assert result["iterations_used"] <= 2


# ── helpers ───────────────────────────────────────────────────────────


class TestSerializeToolCalls:
    def test_passes_through_dict_form(self):
        raw = [{"id": "abc", "function": {"name": "echo", "arguments": "{}"}}]
        out = AIAgent._serialize_tool_calls(raw)
        assert out[0]["id"] == "abc"
        assert out[0]["function"]["name"] == "echo"

    def test_synthesises_id_when_missing(self):
        from tests.conftest import FakeToolCall
        tc = FakeToolCall(id=None, name="echo", arguments="{}")
        out = AIAgent._serialize_tool_calls([tc])
        assert out[0]["id"].startswith("call_")


class TestParseToolArguments:
    def test_dict_passthrough(self):
        assert AIAgent._parse_tool_arguments({"a": 1}) == {"a": 1}

    def test_json_string(self):
        assert AIAgent._parse_tool_arguments('{"a": 1}') == {"a": 1}

    def test_invalid_returns_empty(self):
        assert AIAgent._parse_tool_arguments("not json") == {}

    def test_non_object_json_returns_empty(self):
        # Loop must defend against ``arguments: "[1,2]"`` — not a dict.
        assert AIAgent._parse_tool_arguments("[1, 2]") == {}

    def test_empty_returns_empty(self):
        assert AIAgent._parse_tool_arguments("") == {}
        assert AIAgent._parse_tool_arguments(None) == {}
