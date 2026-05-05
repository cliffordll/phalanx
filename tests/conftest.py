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


@pytest.fixture(autouse=True)
def reset_echo_call_count():
    """Reset the global call counter on echo_tool between tests."""
    import tools.echo_tool as echo_tool
    echo_tool._call_count = 0
    yield
    echo_tool._call_count = 0
