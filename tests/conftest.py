"""Shared pytest fixtures.

The fakes here let tests drive ``run_conversation`` deterministically
without touching the network — patch ``run_agent.OpenAI`` to return
a ``StubClient`` whose ``chat.completions.create`` reads from a
queued response list.
"""

from __future__ import annotations

from typing import Any, Iterable, List, Optional

import pytest


# ── Fake OpenAI response objects ──────────────────────────────────────


class FakeFunction:
    """Stand-in for ``openai.types.chat.chat_completion_message_tool_call.Function``."""

    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class FakeToolCall:
    """Stand-in for an SDK tool_call object (just .id + .function)."""

    def __init__(self, id: str, name: str, arguments: str) -> None:
        self.id = id
        self.function = FakeFunction(name, arguments)


class FakeMessage:
    def __init__(self, content: Optional[str], tool_calls: Optional[List[FakeToolCall]] = None) -> None:
        self.content = content
        self.tool_calls = tool_calls


class FakeChoice:
    def __init__(self, message: FakeMessage) -> None:
        self.message = message


class FakeResponse:
    def __init__(self, choices: List[FakeChoice]) -> None:
        self.choices = choices


def make_text_response(text: str) -> FakeResponse:
    """Build a single-choice response containing only assistant text."""
    return FakeResponse([FakeChoice(FakeMessage(text, None))])


def make_tool_response(tool_calls: Iterable[tuple]) -> FakeResponse:
    """Build a single-choice response that returns tool_calls.

    Each tuple is ``(id, name, arguments_json_str)``.
    """
    calls = [FakeToolCall(*tc) for tc in tool_calls]
    return FakeResponse([FakeChoice(FakeMessage(None, calls))])


# ── StubClient: queues responses, emits them in order ─────────────────


class StubClient:
    """Minimal stand-in for ``openai.OpenAI(...).chat.completions``.

    Construct with a list of ``FakeResponse`` objects.  Each call to
    ``chat.completions.create()`` pops the next response off the queue
    and records the kwargs the agent supplied.
    """

    def __init__(self, responses: List[FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls: List[dict] = []
        self.chat = _StubChat(self)

    def close(self) -> None:  # for AIAgent.close()
        pass


class _StubChat:
    def __init__(self, client: StubClient) -> None:
        self.completions = _StubCompletions(client)


class _StubCompletions:
    def __init__(self, client: StubClient) -> None:
        self._client = client

    def create(self, **kwargs: Any) -> FakeResponse:
        self._client.calls.append(kwargs)
        if not self._client._responses:
            raise AssertionError("StubClient ran out of queued responses")
        return self._client._responses.pop(0)


# ── pytest fixtures ───────────────────────────────────────────────────


@pytest.fixture
def stub_openai(monkeypatch):
    """Factory: ``stub_openai([resp1, resp2, ...])`` patches ``run_agent.OpenAI``.

    Returns the underlying ``StubClient`` so tests can inspect
    ``stub.calls`` after a run.
    """
    import run_agent

    def factory(responses: List[FakeResponse]) -> StubClient:
        client = StubClient(responses)
        monkeypatch.setattr(run_agent, "OpenAI", lambda *a, **kw: client)
        return client

    return factory


# ── Anthropic stubs (§2.4 wave 3) ─────────────────────────────────────


class FakeAnthropicTextBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class FakeAnthropicToolUseBlock:
    type = "tool_use"

    def __init__(self, id: str, name: str, input: dict) -> None:
        self.id = id
        self.name = name
        self.input = input


class FakeAnthropicResponse:
    def __init__(self, content: list, stop_reason: str = "end_turn") -> None:
        self.content = content
        self.stop_reason = stop_reason


def make_anthropic_text_response(text: str, stop_reason: str = "end_turn") -> FakeAnthropicResponse:
    """One text-only Anthropic content block."""
    return FakeAnthropicResponse([FakeAnthropicTextBlock(text)], stop_reason)


def make_anthropic_tool_response(text: str, tool_calls: Iterable[tuple]) -> FakeAnthropicResponse:
    """Mixed text + tool_use blocks; stop_reason='tool_use'."""
    blocks: list = []
    if text:
        blocks.append(FakeAnthropicTextBlock(text))
    for call_id, name, input_dict in tool_calls:
        blocks.append(FakeAnthropicToolUseBlock(call_id, name, input_dict))
    return FakeAnthropicResponse(blocks, "tool_use")


class FakeAnthropicTextDelta:
    """A `text_delta` content_block_delta delta payload."""
    type = "text_delta"

    def __init__(self, text: str) -> None:
        self.text = text


class FakeAnthropicStreamEvent:
    """One iteration item from a fake `messages.stream(...)` context manager."""

    def __init__(self, type: str, **kwargs: Any) -> None:
        self.type = type
        for k, v in kwargs.items():
            setattr(self, k, v)


def make_anthropic_text_delta_event(text: str) -> FakeAnthropicStreamEvent:
    """Sugar: build a `content_block_delta` event carrying a single text_delta."""
    return FakeAnthropicStreamEvent(
        "content_block_delta",
        delta=FakeAnthropicTextDelta(text),
    )


class FakeAnthropicStream:
    """Stand-in for the context manager returned by `messages.stream(**kw)`.

    Yields the queued events on iteration, then `get_final_message()`
    returns the prepared `FakeAnthropicResponse` so the SDK-shape
    converter can run downstream.
    """

    def __init__(self, events: List[FakeAnthropicStreamEvent], final: FakeAnthropicResponse) -> None:
        self._events = list(events)
        self._final = final

    def __enter__(self) -> "FakeAnthropicStream":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def __iter__(self):
        return iter(self._events)

    def get_final_message(self) -> FakeAnthropicResponse:
        return self._final


class StubAnthropicClient:
    """Stand-in for ``anthropic.Anthropic(...)``.

    Each entry of ``responses`` is consumed by either ``messages.create``
    (non-streaming) or ``messages.stream`` (streaming).  Streaming entries
    must be a ``(events_list, final_response)`` tuple; non-streaming
    entries are bare ``FakeAnthropicResponse`` objects.
    """

    def __init__(self, responses: List[Any]) -> None:
        self._responses = list(responses)
        self.calls: List[dict] = []
        self.stream_calls: List[dict] = []
        self.messages = _StubAnthropicMessages(self)

    def close(self) -> None:
        pass


class _StubAnthropicMessages:
    def __init__(self, client: StubAnthropicClient) -> None:
        self._client = client

    def create(self, **kwargs: Any) -> FakeAnthropicResponse:
        self._client.calls.append(kwargs)
        if not self._client._responses:
            raise AssertionError("StubAnthropicClient ran out of queued responses")
        nxt = self._client._responses.pop(0)
        if not isinstance(nxt, FakeAnthropicResponse):
            raise AssertionError(
                "StubAnthropicClient.messages.create called but next queued "
                "response is a streaming tuple (events, final). Use messages.stream "
                "or queue a non-streaming response instead."
            )
        return nxt

    def stream(self, **kwargs: Any) -> FakeAnthropicStream:
        self._client.stream_calls.append(kwargs)
        if not self._client._responses:
            raise AssertionError("StubAnthropicClient ran out of queued responses")
        nxt = self._client._responses.pop(0)
        if not (isinstance(nxt, tuple) and len(nxt) == 2):
            raise AssertionError(
                "StubAnthropicClient.messages.stream called but next queued "
                "response is not a (events, final) tuple."
            )
        events, final = nxt
        return FakeAnthropicStream(events, final)


@pytest.fixture
def stub_anthropic(monkeypatch):
    """Factory: ``stub_anthropic([r1, r2, ...])`` patches ``build_anthropic_client``.

    Patches the symbol ``agent.anthropic_adapter.build_anthropic_client`` so
    the AIAgent's lazy ``_get_anthropic_client`` returns the stub.  Returns
    the ``StubAnthropicClient`` so tests can inspect ``stub.calls`` after
    a run.
    """
    from agent import anthropic_adapter

    def factory(responses: List[FakeAnthropicResponse]) -> StubAnthropicClient:
        client = StubAnthropicClient(responses)
        monkeypatch.setattr(
            anthropic_adapter, "build_anthropic_client",
            lambda *a, **kw: client,
        )
        return client

    return factory


@pytest.fixture(autouse=True)
def reset_echo_call_count():
    """Reset the global call counter on echo_tool between tests."""
    import tools.echo_tool as echo_tool
    echo_tool._call_count = 0
    yield
    echo_tool._call_count = 0
