"""Phase 2.1.5 — `hermes tools list` / `hermes tools run` tests.

Exercises the registry through the CLI surface; importing
``hermes_cli.main`` triggers ``import tools`` which auto-loads
``echo_tool``.
"""

from __future__ import annotations

import json

from hermes_cli.main import main as cli_main


# ── tools list ───────────────────────────────────────────────────────


def test_tools_list_includes_echo(capsys):
    rc = cli_main(["tools", "list"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "echo" in captured.out
    # Toolset bracket should render even for a single-tool registry.
    assert "[echo]" in captured.out


def test_tools_list_verbose_includes_schema(capsys):
    rc = cli_main(["tools", "list", "--verbose"])
    captured = capsys.readouterr()
    assert rc == 0
    # Verbose form dumps the JSON schema; required field for echo is "text".
    assert "schema:" in captured.out
    assert '"text"' in captured.out


# ── tools run ────────────────────────────────────────────────────────


def test_tools_run_echo_returns_payload(capsys):
    rc = cli_main([
        "tools", "run", "echo",
        "--args", '{"text": "hello phalanx"}',
    ])
    captured = capsys.readouterr()
    assert rc == 0
    payload = json.loads(captured.out.strip())
    assert payload["text"] == "hello phalanx"
    assert payload["call_count"] >= 1


def test_tools_run_uppercase_flag(capsys):
    rc = cli_main([
        "tools", "run", "echo",
        "--args", '{"text": "hi", "uppercase": true}',
    ])
    captured = capsys.readouterr()
    assert rc == 0
    payload = json.loads(captured.out.strip())
    assert payload["text"] == "HI"


def test_tools_run_invalid_json_exits_with_two(capsys):
    rc = cli_main(["tools", "run", "echo", "--args", "not-json"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "must be valid JSON" in captured.err


def test_tools_run_non_object_args_rejected(capsys):
    rc = cli_main(["tools", "run", "echo", "--args", "[1, 2, 3]"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "JSON object" in captured.err


def test_tools_run_unknown_tool_returns_error_payload(capsys):
    rc = cli_main(["tools", "run", "no_such_tool", "--args", "{}"])
    captured = capsys.readouterr()
    # Registry returns a JSON error string with rc=0 (the *handler* failed,
    # not the dispatcher). The error structure is what callers assert on.
    assert rc == 0
    payload = json.loads(captured.out.strip())
    assert "error" in payload
    assert "Unknown tool" in payload["error"]


# ── tools schema ────────────────────────────────────────────────────


def test_tools_schema_dumps_echo(capsys):
    rc = cli_main(["tools", "schema", "echo"])
    captured = capsys.readouterr()
    assert rc == 0
    schema = json.loads(captured.out.strip())
    assert schema["name"] == "echo"
    assert schema["parameters"]["required"] == ["text"]
    assert "text" in schema["parameters"]["properties"]


def test_tools_schema_unknown_tool_exits_two(capsys):
    rc = cli_main(["tools", "schema", "no_such_tool"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "unknown tool" in captured.err


# ── tools dry-run ───────────────────────────────────────────────────


def test_tools_dry_run_valid_args(capsys):
    rc = cli_main([
        "tools", "dry-run", "echo",
        "--args", '{"text": "hi"}',
    ])
    captured = capsys.readouterr()
    assert rc == 0
    payload = json.loads(captured.out.strip())
    assert payload["valid"] is True
    assert payload["tool"] == "echo"
    assert payload["args"] == {"text": "hi"}


def test_tools_dry_run_missing_required_field(capsys):
    rc = cli_main([
        "tools", "dry-run", "echo",
        "--args", '{"uppercase": true}',
    ])
    captured = capsys.readouterr()
    assert rc == 1
    payload = json.loads(captured.out.strip())
    assert payload["valid"] is False
    assert any("'text'" in err["message"] for err in payload["errors"])


def test_tools_dry_run_wrong_type(capsys):
    rc = cli_main([
        "tools", "dry-run", "echo",
        "--args", '{"text": 42}',
    ])
    captured = capsys.readouterr()
    assert rc == 1
    payload = json.loads(captured.out.strip())
    assert payload["valid"] is False
    err = next(e for e in payload["errors"] if e["path"] == "text")
    assert "string" in err["message"]


def test_tools_dry_run_invalid_json_exits_two(capsys):
    rc = cli_main(["tools", "dry-run", "echo", "--args", "not-json"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "must be valid JSON" in captured.err


def test_tools_dry_run_unknown_tool_exits_two(capsys):
    rc = cli_main(["tools", "dry-run", "no_such_tool", "--args", "{}"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "unknown tool" in captured.err


def test_tools_dry_run_does_not_invoke_handler(monkeypatch, tmp_path, capsys):
    """write_file dry-run must NOT actually create the file."""
    target = tmp_path / "should_not_exist.txt"
    rc = cli_main([
        "tools", "dry-run", "write_file",
        "--args", json.dumps({"path": str(target), "content": "hi"}),
    ])
    captured = capsys.readouterr()
    assert rc == 0
    payload = json.loads(captured.out.strip())
    assert payload["valid"] is True
    assert not target.exists(), "dry-run produced a side effect"
