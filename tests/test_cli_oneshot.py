"""Phase 2.1.5 — CLI oneshot / version / doctor / config tests.

Drives ``hermes_cli.main:main`` via in-process argv injection so we
can patch the OpenAI client at module level (a real subprocess can't
be reached by ``monkeypatch``).
"""

from __future__ import annotations

import json

from hermes_cli.main import main as cli_main
from tests.conftest import make_text_response, make_tool_response


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
    monkeypatch.setenv("PHALANX_MODEL", "gpt-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    rc = cli_main(["provider", "test", "anthropic"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "not yet wired up" in captured.err


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
