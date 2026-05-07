"""§2.8.c wave 3 tests — async auxiliary_client surface.

Tests the three new async entry points that replace the previous
stubs:

* ``get_async_text_auxiliary_client(task, *, main_runtime)`` — sync
  resolution of an :class:`openai.AsyncOpenAI` client + model id.
* ``async_call_llm(*, task, model, messages, ...)`` — async one-shot
  chat-completion call.  Raises :class:`RuntimeError` when no
  auxiliary client/model can be resolved (web_tools catches this
  to fall back to truncated raw content).
* ``async_summarize_messages(client, model, messages, ...)`` — async
  mirror of :func:`agent.auxiliary_client.summarize_messages`.

All tests mock the AsyncOpenAI client so no real network calls
happen.  ``pytest-asyncio`` is configured strict-mode in
``pyproject.toml`` so each async test carries the
``@pytest.mark.asyncio`` decorator explicitly.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from agent.auxiliary_client import (
    _DEFAULT_AUXILIARY_TIMEOUT_S,
    _resolve_auxiliary_timeout,
    async_call_llm,
    async_summarize_messages,
    extract_content_or_reasoning,
    get_async_text_auxiliary_client,
)


# ── Stub AsyncOpenAI ──────────────────────────────────────────────────


class _FakeAsyncCompletions:
    def __init__(self, *, response: Any = None, raises: Optional[Exception] = None) -> None:
        self._response = response
        self._raises = raises
        self.calls: List[Dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return self._response


class _FakeAsyncChat:
    def __init__(self, completions: _FakeAsyncCompletions) -> None:
        self.completions = completions


class _FakeAsyncOpenAI:
    """Stand-in for openai.AsyncOpenAI."""

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = "",
        response: Any = None,
        raises: Optional[Exception] = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.completions = _FakeAsyncCompletions(response=response, raises=raises)
        self.chat = _FakeAsyncChat(self.completions)


def _make_chat_response(text: str) -> Any:
    """Build the SDK shape extract_content_or_reasoning expects."""
    class _Msg:
        content = text
        reasoning_content = None
        reasoning = None

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]

    return _Resp()


# ── _resolve_auxiliary_timeout ───────────────────────────────────────


def test_resolve_timeout_default_when_no_config(tmp_path, monkeypatch):
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    from hermes_cli import config as cfg_mod
    cfg_mod._RAW_CONFIG_CACHE.clear()
    assert _resolve_auxiliary_timeout("web_extract") == _DEFAULT_AUXILIARY_TIMEOUT_S


def test_resolve_timeout_reads_per_task(tmp_path, monkeypatch):
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "auxiliary:\n  web_extract:\n    timeout: 90\n",
        encoding="utf-8",
    )
    from hermes_cli import config as cfg_mod
    cfg_mod._RAW_CONFIG_CACHE.clear()
    assert _resolve_auxiliary_timeout("web_extract") == 90.0


def test_resolve_timeout_falls_back_to_default_section(tmp_path, monkeypatch):
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "auxiliary:\n  default:\n    timeout: 45\n",
        encoding="utf-8",
    )
    from hermes_cli import config as cfg_mod
    cfg_mod._RAW_CONFIG_CACHE.clear()
    assert _resolve_auxiliary_timeout("any_task") == 45.0


# ── get_async_text_auxiliary_client ──────────────────────────────────


def test_async_client_returns_none_when_no_model(tmp_path, monkeypatch):
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("PHALANX_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    from hermes_cli import config as cfg_mod
    cfg_mod._RAW_CONFIG_CACHE.clear()

    client, model = get_async_text_auxiliary_client("summary")
    assert client is None and model is None


def test_async_client_uses_main_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    from hermes_cli import config as cfg_mod
    cfg_mod._RAW_CONFIG_CACHE.clear()

    client, model = get_async_text_auxiliary_client(
        "summary",
        main_runtime={
            "model": "gpt-x", "base_url": "http://localhost:1234/v1",
            "api_key": "sk-test",
        },
    )
    assert model == "gpt-x"
    assert client is not None
    # Real AsyncOpenAI instance — has a chat.completions attribute.
    assert hasattr(client, "chat")


def test_async_client_uses_per_task_config(tmp_path, monkeypatch):
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    (tmp_path / "config.yaml").write_text(
        "auxiliary:\n"
        "  web_extract:\n"
        "    model: web-aux\n"
        "    api_key: sk-task\n"
        "    base_url: http://aux:1234/v1\n",
        encoding="utf-8",
    )
    from hermes_cli import config as cfg_mod
    cfg_mod._RAW_CONFIG_CACHE.clear()
    client, model = get_async_text_auxiliary_client("web_extract")
    assert model == "web-aux"
    assert client is not None


def test_async_client_handles_missing_sdk(monkeypatch):
    """If openai.AsyncOpenAI import fails, return (None, None) cleanly."""
    import builtins

    real_import = builtins.__import__

    def _bad_import(name, *args, **kwargs):
        if name == "openai":
            raise ImportError("not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _bad_import)
    client, model = get_async_text_auxiliary_client(
        "summary",
        main_runtime={"model": "x", "base_url": "y", "api_key": "z"},
    )
    assert client is None and model is None


# ── async_call_llm ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_async_call_llm_requires_messages():
    with pytest.raises(RuntimeError, match="messages is required"):
        await async_call_llm(task="x", model="y", messages=[])


@pytest.mark.asyncio
async def test_async_call_llm_raises_when_no_client(monkeypatch, tmp_path):
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("PHALANX_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    from hermes_cli import config as cfg_mod
    cfg_mod._RAW_CONFIG_CACHE.clear()

    with pytest.raises(RuntimeError, match="no auxiliary"):
        await async_call_llm(
            task="missing",
            messages=[{"role": "user", "content": "hi"}],
        )


@pytest.mark.asyncio
async def test_async_call_llm_uses_supplied_client():
    """When *client* is passed directly, no resolution path runs."""
    fake = _FakeAsyncOpenAI(response=_make_chat_response("hi"))
    out = await async_call_llm(
        client=fake,
        model="m",
        messages=[{"role": "user", "content": "hello"}],
    )
    assert out is fake.completions._response
    assert len(fake.completions.calls) == 1
    call = fake.completions.calls[0]
    assert call["model"] == "m"
    assert call["messages"][0]["content"] == "hello"


@pytest.mark.asyncio
async def test_async_call_llm_passes_extra_body_and_timeout():
    fake = _FakeAsyncOpenAI(response=_make_chat_response("ok"))
    await async_call_llm(
        client=fake,
        model="m",
        messages=[{"role": "user", "content": "x"}],
        max_tokens=512,
        temperature=0.5,
        extra_body={"tags": ["foo"]},
        timeout=120,
    )
    call = fake.completions.calls[0]
    assert call["max_tokens"] == 512
    assert call["temperature"] == 0.5
    assert call["extra_body"] == {"tags": ["foo"]}
    assert call["timeout"] == 120


@pytest.mark.asyncio
async def test_async_call_llm_resolves_timeout_from_config(
    tmp_path, monkeypatch,
):
    """When *timeout* is None, fall back to per-task config + default."""
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "auxiliary:\n  web_extract:\n    timeout: 12\n",
        encoding="utf-8",
    )
    from hermes_cli import config as cfg_mod
    cfg_mod._RAW_CONFIG_CACHE.clear()
    fake = _FakeAsyncOpenAI(response=_make_chat_response("ok"))
    await async_call_llm(
        client=fake,
        model="m",
        messages=[{"role": "user", "content": "x"}],
        task="web_extract",
    )
    assert fake.completions.calls[0]["timeout"] == 12.0


@pytest.mark.asyncio
async def test_async_call_llm_propagates_api_errors():
    """API errors are NOT swallowed — caller gets them to retry / log."""
    fake = _FakeAsyncOpenAI(raises=ValueError("rate-limit"))
    with pytest.raises(ValueError, match="rate-limit"):
        await async_call_llm(
            client=fake,
            model="m",
            messages=[{"role": "user", "content": "x"}],
        )


@pytest.mark.asyncio
async def test_async_call_llm_resolves_via_main_runtime(monkeypatch, tmp_path):
    """No explicit client + main_runtime hints → resolve from main_runtime."""
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    from hermes_cli import config as cfg_mod
    cfg_mod._RAW_CONFIG_CACHE.clear()

    # Replace AsyncOpenAI in the module under test with a factory that
    # returns our fake.  agent.auxiliary_client imports it inside the
    # function, so monkeypatch the module-level name it imports from.
    fake = _FakeAsyncOpenAI(response=_make_chat_response("hi"))

    import openai
    monkeypatch.setattr(openai, "AsyncOpenAI", lambda **kw: fake)

    out = await async_call_llm(
        task="critic",
        messages=[{"role": "user", "content": "hello"}],
        main_runtime={
            "model": "critic-m",
            "base_url": "http://localhost:1234/v1",
            "api_key": "sk-test",
        },
    )
    assert out is fake.completions._response
    assert fake.completions.calls[0]["model"] == "critic-m"


# ── async_summarize_messages ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_async_summarize_returns_text_on_success():
    fake = _FakeAsyncOpenAI(response=_make_chat_response("the summary"))
    out = await async_summarize_messages(
        fake, "m",
        [{"role": "user", "content": "hello"},
         {"role": "assistant", "content": "world"}],
    )
    assert out == "the summary"
    assert len(fake.completions.calls) == 1
    call = fake.completions.calls[0]
    # System slot is the summarisation prompt; user slot has transcript.
    assert call["messages"][0]["role"] == "system"
    assert call["messages"][1]["role"] == "user"
    assert "hello" in call["messages"][1]["content"]


@pytest.mark.asyncio
async def test_async_summarize_returns_none_on_empty_response():
    fake = _FakeAsyncOpenAI(response=_make_chat_response(""))
    out = await async_summarize_messages(
        fake, "m", [{"role": "user", "content": "hi"}],
    )
    assert out is None


@pytest.mark.asyncio
async def test_async_summarize_returns_none_on_api_error():
    fake = _FakeAsyncOpenAI(raises=RuntimeError("boom"))
    out = await async_summarize_messages(
        fake, "m", [{"role": "user", "content": "hi"}],
    )
    assert out is None


@pytest.mark.asyncio
async def test_async_summarize_no_op_inputs():
    assert await async_summarize_messages(None, "m", []) is None
    fake = _FakeAsyncOpenAI(response=_make_chat_response("x"))
    assert await async_summarize_messages(
        fake, "", [{"role": "user", "content": "x"}],
    ) is None
    assert await async_summarize_messages(fake, "m", []) is None


@pytest.mark.asyncio
async def test_async_summarize_propagates_focus_topic_into_user_prompt():
    fake = _FakeAsyncOpenAI(response=_make_chat_response("ok"))
    await async_summarize_messages(
        fake, "m",
        [{"role": "user", "content": "hello"}],
        focus_topic="task-X",
    )
    user_content = fake.completions.calls[0]["messages"][1]["content"]
    assert "task-X" in user_content


@pytest.mark.asyncio
async def test_async_summarize_passes_max_tokens_and_timeout():
    fake = _FakeAsyncOpenAI(response=_make_chat_response("ok"))
    await async_summarize_messages(
        fake, "m",
        [{"role": "user", "content": "hi"}],
        max_tokens=512,
        timeout=30,
    )
    call = fake.completions.calls[0]
    assert call["max_tokens"] == 512
    assert call["timeout"] == 30


@pytest.mark.asyncio
async def test_async_summarize_uses_extract_content_or_reasoning():
    """Empty content + reasoning_content should still surface text via
    the existing helper."""
    class _Msg:
        content = ""
        reasoning_content = "a reasoning-only response"
        reasoning = None

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]

    fake = _FakeAsyncOpenAI(response=_Resp())
    out = await async_summarize_messages(
        fake, "m", [{"role": "user", "content": "hi"}],
    )
    assert out == "a reasoning-only response"


# ── extract_content_or_reasoning compat ───────────────────────────────


def test_extract_content_or_reasoning_works_against_fake_async_response():
    """Sanity that the response shape used in async tests is the same
    one the existing extract helper consumes — no async-specific drift."""
    resp = _make_chat_response("hello world")
    assert extract_content_or_reasoning(resp) == "hello world"


# ── web_tools integration smoke ──────────────────────────────────────


@pytest.mark.asyncio
async def test_web_tools_consumes_live_async_path(monkeypatch, tmp_path):
    """End-to-end: web_tools._call_summarizer_llm → async_call_llm →
    fake AsyncOpenAI → returns processed content (not None / not
    fallback to truncated raw).

    Validates that wave 3's async surface unblocks web_tools'
    summary path.  Before wave 3 the stub raised RuntimeError so this
    test would land in the 'no auxiliary' branch returning None.
    """
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://localhost:1234/v1")
    monkeypatch.setenv("PHALANX_MODEL", "main-m")
    monkeypatch.setenv("AUXILIARY_WEB_EXTRACT_MODEL", "")
    from hermes_cli import config as cfg_mod
    cfg_mod._RAW_CONFIG_CACHE.clear()

    fake = _FakeAsyncOpenAI(
        response=_make_chat_response("# Summary\n- key fact"),
    )
    import openai
    monkeypatch.setattr(openai, "AsyncOpenAI", lambda **kw: fake)

    from tools.web_tools import _call_summarizer_llm
    result = await _call_summarizer_llm(
        content="x" * 6000,           # > min_length, single-pass path
        context_str="Title: T\n\n",
        model="aux-m",
    )
    assert result == "# Summary\n- key fact"
    # Confirm the call really hit the fake — at least one create call.
    assert fake.completions.calls
    call = fake.completions.calls[0]
    assert call["model"] == "aux-m"
    # web_tools' system / user prompts both appear.
    assert any("expert content analyst" in m["content"] for m in call["messages"])
