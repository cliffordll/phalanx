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


# ── Codex Responses API stubs (§2.4 wave 5) ───────────────────────────


class FakeCodexOutputItem:
    """Stand-in for one item in a Responses API ``response.output[]`` array.

    Just an attribute bag — ``_normalize_codex_response`` reads via getattr.
    """

    def __init__(self, type: str, **kwargs: Any) -> None:
        self.type = type
        self.role = kwargs.pop("role", None)
        self.status = kwargs.pop("status", "completed")
        for k, v in kwargs.items():
            setattr(self, k, v)


class FakeCodexContentPart:
    """One content part inside a `message` output item (output_text)."""

    def __init__(self, text: str, type: str = "output_text") -> None:
        self.type = type
        self.text = text


class FakeCodexResponse:
    """Stand-in for ``responses.create(...)`` return value."""

    def __init__(self, output: List[FakeCodexOutputItem], status: str = "completed",
                 output_text: Optional[str] = None) -> None:
        self.output = output
        self.status = status
        if output_text is not None:
            self.output_text = output_text


def make_codex_text_response(text: str) -> FakeCodexResponse:
    """One-message response with a single output_text content part."""
    return FakeCodexResponse(
        [FakeCodexOutputItem(
            "message", role="assistant", status="completed",
            content=[FakeCodexContentPart(text)],
        )],
    )


def make_codex_tool_response(tool_calls: Iterable[tuple]) -> FakeCodexResponse:
    """Function-call output items; each tuple is (call_id, name, arguments_json)."""
    items = []
    for call_id, name, arguments in tool_calls:
        items.append(FakeCodexOutputItem(
            "function_call",
            call_id=call_id,
            name=name,
            arguments=arguments,
            id=None,
        ))
    return FakeCodexResponse(items)


class FakeCodexStreamEvent:
    """One iteration item from a fake `responses.stream(...)` context manager."""

    def __init__(self, type: str, **kwargs: Any) -> None:
        self.type = type
        for k, v in kwargs.items():
            setattr(self, k, v)


def make_codex_text_delta_event(text: str) -> FakeCodexStreamEvent:
    """Sugar: build a `response.output_text.delta` event."""
    return FakeCodexStreamEvent("response.output_text.delta", delta=text)


class FakeCodexStream:
    """Stand-in for the context manager returned by `responses.stream(**kw)`."""

    def __init__(self, events: List[FakeCodexStreamEvent], final: FakeCodexResponse) -> None:
        self._events = list(events)
        self._final = final

    def __enter__(self) -> "FakeCodexStream":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def __iter__(self):
        return iter(self._events)

    def get_final_response(self) -> FakeCodexResponse:
        return self._final


class StubCodexResponses:
    """Stand-in for ``client.responses``.

    Each entry of the queue is either a bare ``FakeCodexResponse`` (consumed
    by ``create``) or a ``(events_list, final_response)`` tuple (consumed by
    ``stream``).  Asserting on the shape catches mismatched setups.
    """

    def __init__(self, client: "StubCodexClient") -> None:
        self._client = client

    def create(self, **kwargs: Any) -> FakeCodexResponse:
        self._client.calls.append(kwargs)
        if not self._client._responses:
            raise AssertionError("StubCodexClient ran out of queued responses")
        nxt = self._client._responses.pop(0)
        if not isinstance(nxt, FakeCodexResponse):
            raise AssertionError(
                "StubCodexClient.responses.create called but next queued "
                "response is a streaming tuple (events, final). Use "
                "responses.stream or queue a non-streaming response instead."
            )
        return nxt

    def stream(self, **kwargs: Any) -> FakeCodexStream:
        self._client.stream_calls.append(kwargs)
        if not self._client._responses:
            raise AssertionError("StubCodexClient ran out of queued responses")
        nxt = self._client._responses.pop(0)
        if not (isinstance(nxt, tuple) and len(nxt) == 2):
            raise AssertionError(
                "StubCodexClient.responses.stream called but next queued "
                "response is not a (events, final) tuple."
            )
        events, final = nxt
        return FakeCodexStream(events, final)


class StubCodexClient:
    """Stand-in for ``openai.OpenAI(...)`` with .responses wired."""

    def __init__(self, responses: List[Any]) -> None:
        self._responses = list(responses)
        self.calls: List[dict] = []
        self.stream_calls: List[dict] = []
        self.responses = StubCodexResponses(self)
        # Defensive: include `chat` so any leak into the openai-compatible
        # path raises a clear error rather than AttributeError.
        self.chat = None

    def close(self) -> None:
        pass


@pytest.fixture
def stub_codex(monkeypatch):
    """Factory: ``stub_codex([resp1, ...])`` patches ``run_agent.OpenAI``.

    Mirrors stub_openai but the returned client exposes ``.responses.create``
    instead of ``.chat.completions.create`` so codex routing (which hits
    ``client.responses.create``) drives the stub.
    """
    import run_agent

    def factory(responses: List[FakeCodexResponse]) -> StubCodexClient:
        client = StubCodexClient(responses)
        monkeypatch.setattr(run_agent, "OpenAI", lambda *a, **kw: client)
        return client

    return factory


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
