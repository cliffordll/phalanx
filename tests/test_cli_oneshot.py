"""Phase 2.1.5 — CLI oneshot / version / doctor / config tests.

Drives ``hermes_cli.main:main`` via in-process argv injection so we
can patch the OpenAI client at module level (a real subprocess can't
be reached by ``monkeypatch``).
"""

from __future__ import annotations

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


def test_doctor_flags_missing_model(monkeypatch, capsys):
    # Strip every model-source so doctor's resolution falls through.
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
