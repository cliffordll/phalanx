"""§2.8.b wave 2 tests — context compression + auxiliary client.

Three layers under test:

* :class:`agent.context_compressor.ContextCompressor` — threshold
  maths, head/tail protection, LLM-summary path, pruning fallback.
* :mod:`agent.auxiliary_client` — config resolution, summarize_messages
  happy path + failure modes.
* :meth:`run_agent.AIAgent._maybe_compress` — preflight wire-in.

Tests use a fake summariser callable injected via ``client_factory``;
no real network, no real OpenAI client.
"""

from __future__ import annotations

from typing import Any, List, Optional


# ── Fake summariser plumbing ──────────────────────────────────────────


class _FakeChatCompletions:
    def __init__(self, responses: List[Any]) -> None:
        self._responses = list(responses)
        self.calls: List[dict] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("FakeAuxClient ran out of responses")
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


class _FakeChat:
    def __init__(self, completions: _FakeChatCompletions) -> None:
        self.completions = completions


class FakeAuxClient:
    def __init__(self, responses: List[Any]) -> None:
        self.completions = _FakeChatCompletions(responses)
        self.chat = _FakeChat(self.completions)


def _make_summary_response(text: str) -> Any:
    """Mimic the ``message.content`` shape extract_content_or_reasoning
    walks."""
    class _Msg:
        content = text
        reasoning_content = None
        reasoning = None

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]

    return _Resp()


# ── ContextCompressor unit tests ──────────────────────────────────────


def test_threshold_tokens_property():
    from agent.context_compressor import ContextCompressor
    c = ContextCompressor(
        model="m", context_length=10000, threshold_percent=0.7,
    )
    assert c.threshold_tokens == 7000

    c2 = ContextCompressor(model="m", context_length=0)
    assert c2.threshold_tokens == 0


def test_should_compress_below_threshold_false():
    from agent.context_compressor import ContextCompressor
    c = ContextCompressor(model="m", context_length=10000, threshold_percent=0.7)
    assert c.should_compress(5000) is False
    assert c.should_compress(0) is False
    assert c.should_compress(None) is False  # last_prompt_tokens=0


def test_should_compress_at_or_above_threshold_true():
    from agent.context_compressor import ContextCompressor
    c = ContextCompressor(model="m", context_length=10000, threshold_percent=0.7)
    assert c.should_compress(7000) is True
    assert c.should_compress(9999) is True


def test_should_compress_returns_false_when_context_length_unknown():
    from agent.context_compressor import ContextCompressor
    c = ContextCompressor(model="m", context_length=0)
    assert c.should_compress(1_000_000) is False


def test_update_from_response_dict_and_object():
    from agent.context_compressor import ContextCompressor
    c = ContextCompressor(model="m", context_length=10000)
    c.update_from_response({"prompt_tokens": 100, "completion_tokens": 20,
                            "total_tokens": 120})
    assert c.last_prompt_tokens == 100
    assert c.last_completion_tokens == 20

    class _Usage:
        prompt_tokens = 200
        completion_tokens = 40
        total_tokens = 240

    c.update_from_response(_Usage())
    assert c.last_prompt_tokens == 200
    assert c.last_total_tokens == 240


def test_update_from_response_handles_garbage():
    from agent.context_compressor import ContextCompressor
    c = ContextCompressor(model="m", context_length=10000)
    # Should not raise.
    c.update_from_response(None)
    c.update_from_response({"prompt_tokens": "not-a-number"})
    assert c.last_prompt_tokens == 0


def test_on_session_reset_clears_state():
    from agent.context_compressor import ContextCompressor
    c = ContextCompressor(model="m", context_length=10000)
    c.last_prompt_tokens = 1234
    c.compression_count = 3
    c.on_session_reset()
    assert c.last_prompt_tokens == 0
    assert c.compression_count == 0


def test_protected_window_with_system_message():
    from agent.context_compressor import ContextCompressor
    c = ContextCompressor(
        model="m", context_length=10000,
        protect_first_n=2, protect_last_n=3,
    )
    msgs = (
        [{"role": "system", "content": "sys"}]
        + [{"role": "user", "content": f"u{i}"} for i in range(10)]
    )
    head_end, tail_start = c._protected_window(msgs)
    # 1 system + 2 protect_first = 3
    assert head_end == 3
    # 11 - 3 protect_last = 8
    assert tail_start == 8


def test_has_content_to_compress_short_list_false():
    from agent.context_compressor import ContextCompressor
    c = ContextCompressor(model="m", context_length=10000, min_messages=12)
    msgs = [{"role": "user", "content": str(i)} for i in range(5)]
    assert c.has_content_to_compress(msgs) is False


# ── compress() — LLM summary path ─────────────────────────────────────


def _factory_returning(client: Optional[Any], model: Optional[str]):
    return lambda: (client, model)


def _build_messages(n_middle: int) -> List[dict]:
    msgs = [{"role": "system", "content": "you are helpful"}]
    msgs.append({"role": "user", "content": "first user turn"})
    msgs.append({"role": "assistant", "content": "first assistant turn"})
    msgs.append({"role": "user", "content": "second user turn"})
    for i in range(n_middle):
        role = "user" if i % 2 == 0 else "assistant"
        msgs.append({"role": role, "content": f"middle turn {i}"})
    msgs.append({"role": "user", "content": "recent A"})
    msgs.append({"role": "assistant", "content": "recent B"})
    msgs.append({"role": "user", "content": "recent C"})
    msgs.append({"role": "assistant", "content": "recent D"})
    msgs.append({"role": "user", "content": "recent E"})
    msgs.append({"role": "assistant", "content": "recent F"})
    return msgs


def test_compress_replaces_middle_with_summary():
    from agent.context_compressor import ContextCompressor
    fake = FakeAuxClient([_make_summary_response("CONDENSED-MIDDLE")])
    c = ContextCompressor(
        model="m", context_length=10000,
        protect_first_n=3, protect_last_n=6, min_messages=8,
        client_factory=_factory_returning(fake, "aux-model"),
    )
    msgs = _build_messages(n_middle=10)
    out = c.compress(msgs)

    # System + first 3 + 1 synthetic + last 6 = 11
    assert len(out) == 1 + 3 + 1 + 6
    # Head preserved verbatim.
    assert out[0]["role"] == "system" and out[0]["content"] == "you are helpful"
    assert out[1]["content"] == "first user turn"
    # Synthetic system slot in the middle carries the summary.
    synthetic = out[4]
    assert synthetic["role"] == "system"
    assert synthetic["content"].startswith("[context-summary]")
    assert "CONDENSED-MIDDLE" in synthetic["content"]
    # Tail preserved verbatim.
    assert out[-1]["content"] == "recent F"
    assert out[-6]["content"] == "recent A"
    assert c.compression_count == 1


def test_compress_falls_back_to_pruning_when_no_client():
    from agent.context_compressor import ContextCompressor
    c = ContextCompressor(
        model="m", context_length=10000,
        protect_first_n=3, protect_last_n=6, min_messages=8,
        max_messages=10,
        client_factory=None,
    )
    msgs = _build_messages(n_middle=10)
    n_before = len(msgs)
    out = c.compress(msgs)
    assert len(out) < n_before
    # Pruning preserves head + tail.
    assert out[0]["role"] == "system"
    assert out[-1]["content"] == "recent F"
    # And does not insert a [context-summary] block.
    assert not any(
        m.get("role") == "system" and isinstance(m.get("content"), str)
        and m["content"].startswith("[context-summary]")
        for m in out
    )
    assert c.compression_count == 1


def test_compress_falls_back_when_summarize_returns_none():
    """A live auxiliary client that returns empty content must fall
    back to pruning, not propagate a None into the messages list."""
    from agent.context_compressor import ContextCompressor
    fake = FakeAuxClient([_make_summary_response("")])
    c = ContextCompressor(
        model="m", context_length=10000,
        protect_first_n=3, protect_last_n=6, min_messages=8,
        max_messages=10,
        client_factory=_factory_returning(fake, "aux-model"),
    )
    msgs = _build_messages(n_middle=10)
    out = c.compress(msgs)
    # Pruning path → no synthetic [context-summary] message.
    assert not any(
        isinstance(m.get("content"), str)
        and m["content"].startswith("[context-summary]")
        for m in out
    )
    assert len(out) < len(msgs)


def test_compress_no_op_when_middle_empty():
    from agent.context_compressor import ContextCompressor
    fake = FakeAuxClient([_make_summary_response("X")])
    c = ContextCompressor(
        model="m", context_length=10000,
        protect_first_n=3, protect_last_n=6, min_messages=8,
        client_factory=_factory_returning(fake, "aux-model"),
    )
    # Only 9 messages — head (4) + tail (6) overlap, middle empty.
    msgs = _build_messages(n_middle=0)
    out = c.compress(msgs)
    assert out == msgs
    assert c.compression_count == 0


def test_compress_merges_prior_summary_blocks():
    """A second compression should NOT re-summarise its own previous
    output verbatim — the prior [context-summary] feeds into the new
    summarisation slice as context, not as a fresh middle turn."""
    from agent.context_compressor import ContextCompressor
    fake = FakeAuxClient([_make_summary_response("MERGED-SUMMARY")])

    c = ContextCompressor(
        model="m", context_length=10000,
        protect_first_n=3, protect_last_n=6, min_messages=8,
        client_factory=_factory_returning(fake, "aux-model"),
    )
    msgs = (
        [{"role": "system", "content": "you are helpful"}]
        + [{"role": "user", "content": "first turn"}]
        + [{"role": "assistant", "content": "first reply"}]
        + [{"role": "user", "content": "second turn"}]
        + [{"role": "system", "content": "[context-summary] earlier slice."}]
        + [{"role": "user", "content": f"middle {i}"} for i in range(8)]
        + [{"role": "assistant", "content": "recent A"},
           {"role": "user", "content": "recent B"},
           {"role": "assistant", "content": "recent C"},
           {"role": "user", "content": "recent D"},
           {"role": "assistant", "content": "recent E"},
           {"role": "user", "content": "recent F"}]
    )
    out = c.compress(msgs)

    # The summariser sees both the prior summary AND the live middle
    # rows, and writes one new [context-summary] in their place.
    summary_blocks = [
        m for m in out
        if isinstance(m.get("content"), str)
        and m["content"].startswith("[context-summary]")
    ]
    assert len(summary_blocks) == 1
    assert "MERGED-SUMMARY" in summary_blocks[0]["content"]


def test_compress_swallows_factory_exception():
    from agent.context_compressor import ContextCompressor

    def _bad_factory():
        raise RuntimeError("auxiliary not configured")

    c = ContextCompressor(
        model="m", context_length=10000,
        protect_first_n=3, protect_last_n=6, min_messages=8,
        max_messages=10,
        client_factory=_bad_factory,
    )
    msgs = _build_messages(n_middle=10)
    # Must not raise — falls back to pruning.
    out = c.compress(msgs)
    assert len(out) < len(msgs)
    assert c.compression_count == 1


# ── auxiliary_client ──────────────────────────────────────────────────


def test_extract_content_or_reasoning_prefers_content():
    from agent.auxiliary_client import extract_content_or_reasoning

    class _Msg:
        content = "the answer"
        reasoning_content = "thinking..."

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]

    assert extract_content_or_reasoning(_Resp()) == "the answer"


def test_extract_content_or_reasoning_falls_back_to_reasoning():
    from agent.auxiliary_client import extract_content_or_reasoning

    class _Msg:
        content = ""
        reasoning_content = "I am thinking."

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]

    assert extract_content_or_reasoning(_Resp()) == "I am thinking."


def test_extract_content_or_reasoning_handles_none():
    from agent.auxiliary_client import extract_content_or_reasoning
    assert extract_content_or_reasoning(None) == ""


def test_summarize_messages_returns_text_on_success():
    from agent.auxiliary_client import summarize_messages
    fake = FakeAuxClient([_make_summary_response("the summary")])
    out = summarize_messages(
        fake, "m",
        [{"role": "user", "content": "hello"},
         {"role": "assistant", "content": "world"}],
    )
    assert out == "the summary"
    assert fake.completions.calls, "client.chat.completions.create not called"
    call = fake.completions.calls[0]
    assert call["model"] == "m"
    # System slot is the summarisation prompt, then user payload is the
    # transcript request.
    assert call["messages"][0]["role"] == "system"
    assert call["messages"][1]["role"] == "user"


def test_summarize_messages_returns_none_on_empty_response():
    from agent.auxiliary_client import summarize_messages
    fake = FakeAuxClient([_make_summary_response("")])
    out = summarize_messages(
        fake, "m",
        [{"role": "user", "content": "hi"}],
    )
    assert out is None


def test_summarize_messages_returns_none_on_api_error():
    from agent.auxiliary_client import summarize_messages
    fake = FakeAuxClient([RuntimeError("boom")])
    out = summarize_messages(
        fake, "m",
        [{"role": "user", "content": "hi"}],
    )
    assert out is None


def test_summarize_messages_no_op_inputs():
    from agent.auxiliary_client import summarize_messages
    assert summarize_messages(None, "m", []) is None
    fake = FakeAuxClient([_make_summary_response("x")])
    assert summarize_messages(fake, "", [{"role": "user", "content": "x"}]) is None
    assert summarize_messages(fake, "m", []) is None


def test_get_text_auxiliary_client_returns_none_when_no_model(tmp_path, monkeypatch):
    """No auxiliary config + no main_runtime + clean env → (None, None)."""
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("PHALANX_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    from agent.auxiliary_client import get_text_auxiliary_client
    client, model = get_text_auxiliary_client("summary")
    assert client is None and model is None


def test_get_text_auxiliary_client_uses_main_runtime(tmp_path, monkeypatch):
    """With main_runtime hints, OpenAI() should construct successfully."""
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    from agent.auxiliary_client import get_text_auxiliary_client
    client, model = get_text_auxiliary_client(
        "summary",
        main_runtime={"model": "gpt-x", "base_url": "http://localhost:11434/v1",
                      "api_key": "sk-test"},
    )
    assert model == "gpt-x"
    assert client is not None


# ── AIAgent._maybe_compress wire-in ──────────────────────────────────


def test_aiagent_maybe_compress_short_list_skips_probe(monkeypatch, tmp_path):
    """Lists below the probe floor must NOT trigger context_length
    resolution — the test would otherwise stall on cold-cache probes."""
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    from run_agent import AIAgent

    called = {"n": 0}

    def _spy(*a, **kw):
        called["n"] += 1
        return 256000

    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length", _spy
    )
    agent = AIAgent(model="dummy", base_url="", api_key="")
    out = agent._maybe_compress(
        [{"role": "user", "content": "x"}] * 4
    )
    assert out is not None and len(out) == 4
    assert called["n"] == 0, "should not probe context_length for tiny list"


def test_aiagent_maybe_compress_returns_unchanged_when_disabled(
    monkeypatch, tmp_path,
):
    """memory disabled in config → no compression even past the floor."""
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "agent:\n  compression:\n    enabled: false\n", encoding="utf-8",
    )
    from hermes_cli import config as cfg_mod
    cfg_mod._RAW_CONFIG_CACHE.clear()

    from run_agent import AIAgent
    agent = AIAgent(model="dummy", base_url="", api_key="")
    msgs = [{"role": "user", "content": f"m{i}"} for i in range(15)]
    out = agent._maybe_compress(msgs)
    assert out == msgs


def test_aiagent_maybe_compress_invokes_compress_above_threshold(
    monkeypatch, tmp_path,
):
    """Past floor + estimate >= threshold → compressor.compress runs
    and the result is returned to the caller."""
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    from hermes_cli import config as cfg_mod
    cfg_mod._RAW_CONFIG_CACHE.clear()

    # Patch context_length resolution to a small number so a tiny
    # estimated payload crosses the threshold without needing a network
    # probe (and without writing huge messages).
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *a, **kw: 100,
    )

    from run_agent import AIAgent
    agent = AIAgent(model="dummy", base_url="", api_key="")

    msgs = [{"role": "system", "content": "S"}] + [
        {"role": "user" if i % 2 == 0 else "assistant",
         "content": "x" * 200}
        for i in range(20)
    ]

    # Patch the auxiliary client_factory used inside the compressor so
    # it returns a fake summariser whose response is observable.
    fake = FakeAuxClient([_make_summary_response("MERGED")])

    def _patched_factory(self):
        return fake, "aux-model"

    # Monkey-patch the lazy client_factory by intercepting compressor
    # construction: build the agent, force-build the compressor, then
    # swap its client_factory.
    comp = agent._get_compressor()
    assert comp is not None, "compressor should bind with these monkeypatches"
    comp.client_factory = lambda: (fake, "aux-model")

    out = agent._maybe_compress(msgs, focus_topic="test")
    # Compression happened: at least one [context-summary] block.
    assert any(
        isinstance(m.get("content"), str)
        and m["content"].startswith("[context-summary]")
        for m in out
    )
    assert len(out) < len(msgs)


def test_aiagent_get_compressor_caches_skip(monkeypatch, tmp_path):
    """A skipped bind (config disabled) must not re-probe on the next
    preflight."""
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "agent:\n  compression:\n    enabled: false\n", encoding="utf-8",
    )
    from hermes_cli import config as cfg_mod
    cfg_mod._RAW_CONFIG_CACHE.clear()

    from run_agent import AIAgent
    agent = AIAgent(model="dummy", base_url="", api_key="")

    assert agent._get_compressor() is None
    assert agent._compressor_skipped is True
    # Second call short-circuits via the latch.
    assert agent._get_compressor() is None
