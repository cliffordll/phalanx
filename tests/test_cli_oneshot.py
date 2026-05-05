"""Phase 2.1.5 — CLI oneshot / version / doctor / config tests.

Drives ``hermes_cli.main:main`` via in-process argv injection so we
can patch the OpenAI client at module level (a real subprocess can't
be reached by ``monkeypatch``).
"""

from __future__ import annotations

import json

from hermes_cli.main import main as cli_main
from tests.conftest import (
    make_anthropic_text_response,
    make_anthropic_tool_response,
    make_text_response,
    make_tool_response,
)


# ── version ──────────────────────────────────────────────────────────


def test_version_prints_and_exits_zero(capsys):
    rc = cli_main(["version"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "phalanx" in captured.out.lower()
    # Version constant from hermes_cli/__init__.py
    assert "0.12.0" in captured.out


# ── doctor ───────────────────────────────────────────────────────────


def test_doctor_passes_when_model_is_set(monkeypatch, capsys):
    monkeypatch.setenv("PHALANX_MODEL", "gpt-test")
    rc = cli_main(["doctor"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "all checks passed" in captured.out
    assert "PHALANX_HOME" in captured.out
    assert "tool registry" in captured.out
    # Tool registry is now loaded (Phase 2.1.4) — should report available.
    assert "available" in captured.out


def test_doctor_flags_missing_model(monkeypatch, capsys, tmp_path):
    """Point PHALANX_HOME at an empty tmp dir so the host's real
    ~/.phalanx/config.yaml can't satisfy the model-resolution chain."""
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    for var in ("PHALANX_MODEL", "OPENAI_MODEL"):
        monkeypatch.delenv(var, raising=False)
    rc = cli_main(["doctor"])
    captured = capsys.readouterr()
    # Doctor should exit non-zero when the issue list is non-empty.
    assert rc != 0
    assert "no model configured" in captured.out


# ── config show / get ────────────────────────────────────────────────


def test_config_show_when_file_missing(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    rc = cli_main(["config", "show"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "no config file" in captured.out


def test_config_get_returns_one_below_when_unset(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    rc = cli_main(["config", "get", "model.default"])
    captured = capsys.readouterr()
    # Exit code 1 = key not found (not an argparse error which would be 2).
    assert rc == 1
    assert "<unset>" in captured.err or "<unset>" in captured.out


def test_config_get_reads_nested_value(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("model:\n  default: gpt-4o-mini\n", encoding="utf-8")
    rc = cli_main(["config", "get", "model.default"])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.strip() == "gpt-4o-mini"


# ── oneshot ──────────────────────────────────────────────────────────


def test_oneshot_returns_assistant_text_when_no_tool_calls(stub_openai, capsys):
    stub_openai([make_text_response("hi back")])
    rc = cli_main([
        "--model", "gpt-test", "--api-key", "sk-x", "--base-url", "https://x/v1",
        "oneshot", "say hi",
    ])
    captured = capsys.readouterr()
    assert rc == 0
    assert "hi back" in captured.out


def test_oneshot_drives_full_tool_round_trip(stub_openai, capsys):
    stub = stub_openai([
        make_tool_response([("call_1", "echo", '{"text":"phalanx","uppercase":true}')]),
        make_text_response("Echo loop closed."),
    ])
    rc = cli_main([
        "--model", "gpt-test", "--api-key", "sk-x", "--base-url", "https://x/v1",
        "oneshot", "echo phalanx in upper",
    ])
    captured = capsys.readouterr()
    assert rc == 0
    assert "Echo loop closed." in captured.out
    # Two API calls actually fired against our stub.
    assert len(stub.calls) == 2


def test_oneshot_requires_a_message(monkeypatch, capsys):
    monkeypatch.setenv("PHALANX_MODEL", "gpt-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    rc = cli_main(["oneshot"])
    captured = capsys.readouterr()
    # Exit code 2 = argparse-style usage error.
    assert rc == 2
    assert "requires a message" in captured.err


def test_oneshot_debug_prints_loop_summary_to_stderr(stub_openai, capsys):
    stub_openai([make_text_response("ok")])
    rc = cli_main([
        "--debug",
        "--model", "gpt-test", "--api-key", "sk-x", "--base-url", "https://x/v1",
        "oneshot", "ping",
    ])
    captured = capsys.readouterr()
    assert rc == 0
    # --debug surfaces the per-turn summary on stderr; stdout stays clean.
    assert "ok" in captured.out
    assert "[done]" in captured.err
    assert "turns=" in captured.err


def test_oneshot_dump_messages(stub_openai, capsys):
    stub_openai([make_text_response("hi")])
    rc = cli_main([
        "--model", "gpt-test", "--api-key", "sk-x", "--base-url", "https://x/v1",
        "oneshot", "--dump-messages", "ping",
    ])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.strip() == "hi"
    assert "--- messages ---" in captured.err
    # Pull the JSON section out of stderr; it's a list of role-tagged dicts.
    body = captured.err.split("--- messages ---", 1)[1]
    payload = json.loads(body.strip())
    roles = [m["role"] for m in payload]
    assert roles == ["system", "user", "assistant"]
    # tools schema must NOT leak into messages dump.
    assert "tools" not in captured.err.split("--- messages ---")[0]


def test_oneshot_dump_tools(stub_openai, capsys):
    stub_openai([make_text_response("hi")])
    rc = cli_main([
        "--model", "gpt-test", "--api-key", "sk-x", "--base-url", "https://x/v1",
        "oneshot", "--dump-tools", "ping",
    ])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.strip() == "hi"
    assert "--- tools ---" in captured.err
    # Body is a JSON list of OpenAI function-calling tool schemas.
    body = captured.err.split("--- tools ---", 1)[1]
    payload = json.loads(body.strip())
    assert isinstance(payload, list) and payload, "expected at least one tool schema"
    # echo is always registered → must appear.
    names = {entry["function"]["name"] for entry in payload}
    assert "echo" in names


def test_oneshot_dump_tools_and_messages_independent(stub_openai, capsys):
    stub_openai([make_text_response("hi")])
    rc = cli_main([
        "--model", "gpt-test", "--api-key", "sk-x", "--base-url", "https://x/v1",
        "oneshot", "--dump-tools", "--dump-messages", "ping",
    ])
    captured = capsys.readouterr()
    assert rc == 0
    # Both blocks present, in the order: tools first, then messages.
    tools_idx = captured.err.find("--- tools ---")
    msgs_idx = captured.err.find("--- messages ---")
    assert tools_idx >= 0 and msgs_idx > tools_idx


# ── §2.4 wave 1: streaming + provider CLI ────────────────────────────


def _make_stream_chunk(*, content_delta=None, tool_call_delta=None, finish_reason=None):
    """Build one ChatCompletionChunk-like object for _accumulate_stream tests."""
    from types import SimpleNamespace
    delta_kwargs: dict = {"content": content_delta, "tool_calls": None}
    if tool_call_delta is not None:
        fn = SimpleNamespace(
            name=tool_call_delta.get("name"),
            arguments=tool_call_delta.get("arguments"),
        )
        tc = SimpleNamespace(
            index=tool_call_delta.get("index", 0),
            id=tool_call_delta.get("id"),
            function=fn,
        )
        delta_kwargs["tool_calls"] = [tc]
    delta = SimpleNamespace(**delta_kwargs)
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice])


def test_accumulate_stream_text_deltas_and_callback():
    from run_agent import _accumulate_stream
    chunks = [
        _make_stream_chunk(content_delta="Hello"),
        _make_stream_chunk(content_delta=" world"),
        _make_stream_chunk(content_delta="!", finish_reason="stop"),
    ]
    deltas: list = []
    response = _accumulate_stream(iter(chunks), deltas.append)
    assert deltas == ["Hello", " world", "!"]
    msg = response.choices[0].message
    assert msg.content == "Hello world!"
    assert msg.tool_calls is None
    assert response.choices[0].finish_reason == "stop"


def test_accumulate_stream_rebuilds_tool_calls():
    """Tool-call slices arrive across chunks: id, name, then args byte-by-byte."""
    from run_agent import _accumulate_stream
    chunks = [
        _make_stream_chunk(tool_call_delta={"index": 0, "id": "call_1"}),
        _make_stream_chunk(tool_call_delta={"index": 0, "name": "echo"}),
        _make_stream_chunk(tool_call_delta={"index": 0, "arguments": '{"text"'}),
        _make_stream_chunk(tool_call_delta={"index": 0, "arguments": ': "hi"}'}),
        _make_stream_chunk(finish_reason="tool_calls"),
    ]
    response = _accumulate_stream(iter(chunks), lambda s: None)
    msg = response.choices[0].message
    assert msg.content is None
    assert len(msg.tool_calls) == 1
    tc = msg.tool_calls[0]
    assert tc.id == "call_1"
    assert tc.function.name == "echo"
    assert tc.function.arguments == '{"text": "hi"}'
    assert response.choices[0].finish_reason == "tool_calls"


def test_accumulate_stream_callback_failure_does_not_break_run(caplog):
    """If the user's stream_callback raises, accumulation must continue."""
    from run_agent import _accumulate_stream
    chunks = [
        _make_stream_chunk(content_delta="A"),
        _make_stream_chunk(content_delta="B", finish_reason="stop"),
    ]

    def bad_callback(_d):
        raise RuntimeError("ui crashed")

    response = _accumulate_stream(iter(chunks), bad_callback)
    assert response.choices[0].message.content == "AB"


def test_provider_list_shows_active_openai_compatible(stub_openai, capsys):
    rc = cli_main([
        "--model", "gpt-test", "--base-url", "https://x/v1",
        "provider", "list",
    ])
    captured = capsys.readouterr()
    assert rc == 0
    assert "openai-compatible" in captured.out
    assert "[active]" in captured.out
    assert "anthropic" in captured.out
    assert "not yet ported" in captured.out


def test_provider_test_pings_and_reports_ok(stub_openai, capsys):
    stub_openai([make_text_response("pong")])
    rc = cli_main([
        "--model", "gpt-test", "--api-key", "sk-x", "--base-url", "https://x/v1",
        "provider", "test",
    ])
    captured = capsys.readouterr()
    assert rc == 0
    assert "OK" in captured.out
    assert "reply:" in captured.out and "pong" in captured.out
    assert "latency:" in captured.out


def test_provider_test_unknown_provider_rejects(monkeypatch, capsys):
    """bedrock/gemini still rejected up front (codex landed in wave 5)."""
    monkeypatch.setenv("PHALANX_MODEL", "gpt-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")

    for unsupported in ("bedrock", "gemini"):
        rc = cli_main(["provider", "test", unsupported])
        captured = capsys.readouterr()
        assert rc == 2, f"{unsupported} should be rejected"
        assert "not yet wired up" in captured.err


def test_detect_provider_resolves_known_hosts():
    """Module-level _detect_provider maps a base_url to its adapter name."""
    from run_agent import _detect_provider

    assert _detect_provider("") == "openai-compatible"
    assert _detect_provider(None) == "openai-compatible"  # type: ignore[arg-type]
    assert _detect_provider("http://localhost:11434/v1") == "openai-compatible"
    assert _detect_provider("https://api.openai.com/v1") == "openai-compatible"
    assert _detect_provider("https://api.anthropic.com") == "anthropic"
    assert _detect_provider("https://API.ANTHROPIC.com/v1") == "anthropic"  # case-insensitive
    assert _detect_provider("https://bedrock-runtime.us-east-1.amazonaws.com") == "bedrock"
    assert _detect_provider("https://generativelanguage.googleapis.com/v1beta") == "gemini"
    assert _detect_provider("https://api.openai.com/v1/responses") == "codex"


def test_provider_flag_overrides_autodetect(stub_openai, capsys):
    """`hermes --provider anthropic provider list` shows the forced override."""
    rc = cli_main([
        "--model", "gpt-test", "--base-url", "http://localhost:11434/v1",
        "--provider", "anthropic",
        "provider", "list",
    ])
    captured = capsys.readouterr()
    assert rc == 0
    assert "detected: anthropic" in captured.out
    assert "forced via --provider" in captured.out


def test_provider_list_marks_anthropic_active_for_anthropic_url(stub_openai, capsys):
    """Auto-detection: pointing base_url at anthropic.com flips [active]."""
    rc = cli_main([
        "--model", "claude", "--base-url", "https://api.anthropic.com",
        "provider", "list",
    ])
    captured = capsys.readouterr()
    assert rc == 0
    # The [active] marker should be on the anthropic row, not openai-compatible.
    anthropic_line = next(
        line for line in captured.out.splitlines() if line.lstrip().startswith("anthropic")
    )
    assert "[active]" in anthropic_line


def test_model_switch_writes_config(monkeypatch, tmp_path, capsys):
    """`hermes model switch X` rewrites model.default in ~/.phalanx/config.yaml."""
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    rc = cli_main(["model", "switch", "qwen2.5:1.5b"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "qwen2.5:1.5b" in captured.out

    # Verify the file actually exists with the right content.
    cfg_path = tmp_path / "config.yaml"
    assert cfg_path.exists()
    import yaml
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert cfg["model"]["default"] == "qwen2.5:1.5b"

    # Switch again to a different name; previous → new arrow shows up.
    rc = cli_main(["model", "switch", "claude-3-5-sonnet"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "qwen2.5:1.5b → claude-3-5-sonnet" in captured.out


# ── §2.4 wave 3: anthropic SDK routing ───────────────────────────────


def test_anthropic_response_to_openai_shape_text_only():
    """Pure-text Anthropic responses normalize to a single .choices[0]."""
    from run_agent import _anthropic_response_to_openai_shape

    resp = _anthropic_response_to_openai_shape(make_anthropic_text_response("hi"))
    assert resp.choices[0].message.content == "hi"
    assert resp.choices[0].message.tool_calls is None
    assert resp.choices[0].finish_reason == "stop"


def test_anthropic_response_to_openai_shape_tool_use():
    """tool_use blocks become OpenAI-shape tool_calls with JSON arguments."""
    from run_agent import _anthropic_response_to_openai_shape

    raw = make_anthropic_tool_response(
        text="thinking out loud",
        tool_calls=[("toolu_abc", "echo", {"text": "hi"})],
    )
    resp = _anthropic_response_to_openai_shape(raw)
    assert resp.choices[0].finish_reason == "tool_calls"
    assert resp.choices[0].message.content == "thinking out loud"
    tcs = resp.choices[0].message.tool_calls
    assert len(tcs) == 1
    assert tcs[0].id == "toolu_abc"
    assert tcs[0].function.name == "echo"
    assert json.loads(tcs[0].function.arguments) == {"text": "hi"}


def test_anthropic_stop_reason_mapping():
    """Anthropic stop_reasons map to OpenAI finish_reasons (max_tokens → length, etc.)."""
    from run_agent import _anthropic_response_to_openai_shape
    from tests.conftest import FakeAnthropicResponse, FakeAnthropicTextBlock

    cases = {
        "end_turn": "stop",
        "tool_use": "tool_calls",
        "max_tokens": "length",
        "stop_sequence": "stop",
        "refusal": "content_filter",
        "model_context_window_exceeded": "length",
        "something-the-api-added-later": "stop",  # unmapped → "stop"
    }
    for raw, expected in cases.items():
        resp = _anthropic_response_to_openai_shape(
            FakeAnthropicResponse([FakeAnthropicTextBlock("x")], raw)
        )
        assert resp.choices[0].finish_reason == expected, raw


def test_provider_test_anthropic_routes_through_anthropic_sdk(
    stub_anthropic, capsys, monkeypatch,
):
    """`hermes provider test anthropic` exercises the wave-3 routing end-to-end."""
    monkeypatch.setenv("PHALANX_MODEL", "claude-3-5-sonnet")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    stub = stub_anthropic([make_anthropic_text_response("pong")])

    rc = cli_main(["provider", "test", "anthropic"])
    captured = capsys.readouterr()

    assert rc == 0
    assert "OK" in captured.out
    assert "pong" in captured.out
    # Round-trip went through the Anthropic SDK stub.
    assert len(stub.calls) == 1
    assert stub.calls[0]["model"]  # api kwargs reached messages.create


def test_oneshot_anthropic_provider_drives_full_flow(stub_anthropic, capsys):
    """`oneshot --provider anthropic` runs the full run_conversation through anthropic."""
    stub = stub_anthropic([make_anthropic_text_response("hi from claude")])
    rc = cli_main([
        "--model", "claude-3-5-sonnet",
        "--api-key", "sk-x",
        "--base-url", "https://api.anthropic.com",
        "--provider", "anthropic",
        "oneshot", "say hi",
    ])
    captured = capsys.readouterr()
    assert rc == 0
    assert "hi from claude" in captured.out
    # api_kwargs had the system prompt extracted and messages array built.
    kwargs = stub.calls[0]
    assert kwargs["model"]  # build_anthropic_kwargs ran normalize_model_name
    assert isinstance(kwargs["messages"], list)
    # convert_messages_to_anthropic pulls system messages into a separate
    # "system" kwarg — phalanx's build_system_prompt always provides one.
    assert "system" in kwargs


def test_accumulate_anthropic_stream_forwards_text_deltas():
    """text_delta events fire the callback in order; final message wins for shape."""
    from run_agent import _accumulate_anthropic_stream
    from tests.conftest import (
        FakeAnthropicStream,
        FakeAnthropicStreamEvent,
        make_anthropic_text_delta_event,
        make_anthropic_text_response,
    )

    events = [
        FakeAnthropicStreamEvent("message_start"),
        FakeAnthropicStreamEvent("content_block_start"),
        make_anthropic_text_delta_event("hi "),
        make_anthropic_text_delta_event("there"),
        FakeAnthropicStreamEvent("content_block_stop"),
        FakeAnthropicStreamEvent("message_stop"),
    ]
    final = make_anthropic_text_response("hi there")
    stream = FakeAnthropicStream(events, final)

    seen: list = []
    response = _accumulate_anthropic_stream(stream, seen.append)

    assert seen == ["hi ", "there"]
    assert response.choices[0].message.content == "hi there"
    assert response.choices[0].finish_reason == "stop"


def test_accumulate_anthropic_stream_callback_failure_does_not_break_run(caplog):
    """A buggy stream callback is logged but doesn't prevent get_final_message."""
    import logging
    from run_agent import _accumulate_anthropic_stream
    from tests.conftest import (
        FakeAnthropicStream,
        make_anthropic_text_delta_event,
        make_anthropic_text_response,
    )

    caplog.set_level(logging.ERROR, logger="run_agent")
    stream = FakeAnthropicStream(
        [make_anthropic_text_delta_event("A"), make_anthropic_text_delta_event("B")],
        make_anthropic_text_response("AB"),
    )

    def bad_callback(_d: str) -> None:
        raise RuntimeError("ui crashed")

    response = _accumulate_anthropic_stream(stream, bad_callback)
    assert response.choices[0].message.content == "AB"


# ── §2.4 wave 5: codex (Responses API) routing ──────────────────────


def test_codex_response_to_openai_shape_text_only():
    """Pure-text Codex responses normalize to a single .choices[0]."""
    from run_agent import _codex_response_to_openai_shape
    from tests.conftest import make_codex_text_response

    resp = _codex_response_to_openai_shape(make_codex_text_response("hello"))
    assert resp.choices[0].message.content == "hello"
    assert not resp.choices[0].message.tool_calls
    assert resp.choices[0].finish_reason == "stop"


def test_codex_response_to_openai_shape_function_call():
    """function_call output items become OpenAI-shape tool_calls."""
    from run_agent import _codex_response_to_openai_shape
    from tests.conftest import make_codex_tool_response

    raw = make_codex_tool_response([("call_abc", "echo", '{"text":"hi"}')])
    resp = _codex_response_to_openai_shape(raw)
    assert resp.choices[0].finish_reason == "tool_calls"
    tcs = resp.choices[0].message.tool_calls
    assert len(tcs) == 1
    assert tcs[0].id == "call_abc"
    assert tcs[0].function.name == "echo"
    assert json.loads(tcs[0].function.arguments) == {"text": "hi"}


def test_provider_test_codex_routes_through_responses_api(
    stub_codex, capsys, monkeypatch,
):
    """`hermes provider test codex` exercises the wave-5 routing end-to-end."""
    monkeypatch.setenv("PHALANX_MODEL", "gpt-5")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    from tests.conftest import make_codex_text_response
    stub = stub_codex([make_codex_text_response("pong")])

    rc = cli_main(["provider", "test", "codex"])
    captured = capsys.readouterr()

    assert rc == 0
    assert "OK" in captured.out
    assert "pong" in captured.out
    assert len(stub.calls) == 1
    # Responses API shape: instructions + input + tool_choice/parallel/store keys
    kwargs = stub.calls[0]
    assert "instructions" in kwargs
    assert isinstance(kwargs["input"], list)
    assert kwargs["tool_choice"] == "auto"
    assert kwargs["parallel_tool_calls"] is True
    assert kwargs["store"] is False


def test_oneshot_codex_provider_drives_full_flow(stub_codex, capsys):
    """`oneshot --provider codex` runs the full run_conversation through codex."""
    from tests.conftest import make_codex_text_response

    stub = stub_codex([make_codex_text_response("hi from gpt-5")])
    rc = cli_main([
        "--model", "gpt-5",
        "--api-key", "sk-x",
        "--base-url", "https://api.openai.com/v1",
        "--provider", "codex",
        "oneshot", "say hi",
    ])
    captured = capsys.readouterr()
    assert rc == 0
    assert "hi from gpt-5" in captured.out
    kwargs = stub.calls[0]
    # System prompt was extracted into instructions, not left in input.
    assert kwargs["instructions"]
    # The remaining payload is the user turn (and any tool turns).
    assert any(item.get("role") == "user" or item.get("type") == "message"
               for item in kwargs["input"])


def test_accumulate_codex_stream_forwards_text_deltas():
    """response.output_text.delta events fire the callback in order."""
    from run_agent import _accumulate_codex_stream
    from tests.conftest import (
        FakeCodexStream,
        FakeCodexStreamEvent,
        make_codex_text_delta_event,
        make_codex_text_response,
    )

    events = [
        FakeCodexStreamEvent("response.created"),
        FakeCodexStreamEvent("response.output_item.added"),
        make_codex_text_delta_event("hi "),
        make_codex_text_delta_event("there"),
        FakeCodexStreamEvent("response.output_item.done"),
        FakeCodexStreamEvent("response.completed"),
    ]
    final = make_codex_text_response("hi there")
    stream = FakeCodexStream(events, final)

    seen: list = []
    response = _accumulate_codex_stream(stream, seen.append)

    assert seen == ["hi ", "there"]
    assert response.choices[0].message.content == "hi there"
    assert response.choices[0].finish_reason == "stop"


def test_accumulate_codex_stream_callback_failure_does_not_break_run(caplog):
    """A buggy stream callback is logged but get_final_response still wins."""
    import logging
    from run_agent import _accumulate_codex_stream
    from tests.conftest import (
        FakeCodexStream,
        make_codex_text_delta_event,
        make_codex_text_response,
    )

    caplog.set_level(logging.ERROR, logger="run_agent")
    stream = FakeCodexStream(
        [make_codex_text_delta_event("A"), make_codex_text_delta_event("B")],
        make_codex_text_response("AB"),
    )

    def bad_callback(_d: str) -> None:
        raise RuntimeError("ui crashed")

    response = _accumulate_codex_stream(stream, bad_callback)
    assert response.choices[0].message.content == "AB"


def test_oneshot_codex_streaming_drives_real_stream(stub_codex, capsys):
    """--stream on the codex route now drives responses.stream() for real."""
    from tests.conftest import (
        make_codex_text_delta_event,
        make_codex_text_response,
    )

    events = [
        make_codex_text_delta_event("hi "),
        make_codex_text_delta_event("from "),
        make_codex_text_delta_event("gpt-5"),
    ]
    final = make_codex_text_response("hi from gpt-5")
    stub = stub_codex([(events, final)])

    rc = cli_main([
        "--model", "gpt-5",
        "--api-key", "sk-x",
        "--base-url", "https://api.openai.com/v1",
        "--provider", "codex",
        "oneshot", "--stream", "hi",
    ])
    captured = capsys.readouterr()
    assert rc == 0
    # Streaming wrote the deltas to stdout in order, then a trailing newline.
    assert captured.out == "hi from gpt-5\n"
    # And we hit responses.stream(), not responses.create().
    assert len(stub.stream_calls) == 1
    assert len(stub.calls) == 0


def test_oneshot_anthropic_streaming_drives_real_stream(stub_anthropic, capsys):
    """--stream on the anthropic route now drives messages.stream() for real."""
    from tests.conftest import make_anthropic_text_delta_event

    events = [
        make_anthropic_text_delta_event("hi "),
        make_anthropic_text_delta_event("from "),
        make_anthropic_text_delta_event("claude"),
    ]
    final = make_anthropic_text_response("hi from claude")
    stub = stub_anthropic([(events, final)])

    rc = cli_main([
        "--model", "claude-3-5-sonnet",
        "--api-key", "sk-x",
        "--base-url", "https://api.anthropic.com",
        "--provider", "anthropic",
        "oneshot", "--stream", "hi",
    ])
    captured = capsys.readouterr()
    assert rc == 0
    # Streaming wrote the deltas to stdout in order, then a trailing newline.
    assert captured.out == "hi from claude\n"
    # And we hit messages.stream(), not messages.create().
    assert len(stub.stream_calls) == 1
    assert len(stub.calls) == 0
