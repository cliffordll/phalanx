"""``hermes logs`` subcommand + filter helper tests (Phase 2.5 wave 5)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from hermes_cli.logs import (
    _LEVEL_ORDER,
    _extract_level,
    _extract_logger_name,
    _matches_filters,
    _parse_line_timestamp,
    _parse_since,
    _read_last_n_lines,
    list_logs,
    tail_log,
)
from hermes_cli.main import main as cli_main


# ── Filter helper unit tests ─────────────────────────────────────────────


def test_parse_since_accepts_each_unit():
    now = datetime.now()
    for spec, delta in [
        ("30s", timedelta(seconds=30)),
        ("5m", timedelta(minutes=5)),
        ("1h", timedelta(hours=1)),
        ("2d", timedelta(days=2)),
    ]:
        result = _parse_since(spec)
        assert result is not None
        # Allow a 5-second slack for clock drift between calls.
        assert abs((now - delta) - result) < timedelta(seconds=5)


def test_parse_since_rejects_garbage():
    assert _parse_since("garbage") is None
    assert _parse_since("10x") is None
    assert _parse_since("") is None


def test_parse_line_timestamp_ok():
    line = "2026-04-05 22:35:00,123 INFO agent.main: hello\n"
    ts = _parse_line_timestamp(line)
    assert ts == datetime(2026, 4, 5, 22, 35, 0)


def test_parse_line_timestamp_no_match():
    assert _parse_line_timestamp("not a log line") is None


def test_extract_level():
    assert _extract_level(
        "2026-04-05 22:35:00 INFO agent.main: hello\n"
    ) == "INFO"
    assert _extract_level(
        "2026-04-05 22:35:00 WARNING agent.main: heads up\n"
    ) == "WARNING"
    assert _extract_level("no level in here") is None


def test_extract_logger_name_with_session_tag():
    line = "2026-04-05 22:35:00 INFO [sess_abc] tools.terminal: ran cmd\n"
    assert _extract_logger_name(line) == "tools.terminal"


def test_extract_logger_name_without_session_tag():
    line = "2026-04-05 22:35:00 INFO agent.main: hello\n"
    assert _extract_logger_name(line) == "agent.main"


def test_matches_filters_level_threshold():
    line = "2026-04-05 22:35:00 INFO agent.main: hello\n"
    assert _matches_filters(line, min_level="DEBUG") is True
    assert _matches_filters(line, min_level="INFO") is True
    assert _matches_filters(line, min_level="WARNING") is False


def test_matches_filters_session_substring():
    line = "2026-04-05 22:35:00 INFO [sess_abc] agent: hi\n"
    assert _matches_filters(line, session_filter="sess_abc") is True
    assert _matches_filters(line, session_filter="sess_xyz") is False


def test_matches_filters_since_drops_old():
    line = "2026-01-01 00:00:00 INFO agent: ancient\n"
    cutoff = datetime(2026, 4, 1, 0, 0, 0)
    assert _matches_filters(line, since=cutoff) is False
    cutoff_old = datetime(2025, 1, 1, 0, 0, 0)
    assert _matches_filters(line, since=cutoff_old) is True


def test_matches_filters_component_prefix():
    line = "2026-04-05 22:35:00 INFO tools.terminal: ran\n"
    assert _matches_filters(line, component_prefixes=("tools",)) is True
    assert _matches_filters(line, component_prefixes=("gateway",)) is False


def test_matches_filters_unparseable_timestamp_passes_since():
    """Lines without a timestamp shouldn't be silently dropped by --since."""
    line = "no-timestamp INFO agent: hi\n"
    cutoff = datetime(2099, 1, 1)
    # No timestamp means we can't decide → keep the line.
    assert _matches_filters(line, since=cutoff) is True


def test_level_order_is_monotonic():
    assert _LEVEL_ORDER["DEBUG"] < _LEVEL_ORDER["INFO"]
    assert _LEVEL_ORDER["INFO"] < _LEVEL_ORDER["WARNING"]
    assert _LEVEL_ORDER["WARNING"] < _LEVEL_ORDER["ERROR"]
    assert _LEVEL_ORDER["ERROR"] < _LEVEL_ORDER["CRITICAL"]


# ── _read_last_n_lines ───────────────────────────────────────────────────


def test_read_last_n_lines_small_file(tmp_path):
    p = tmp_path / "small.log"
    p.write_text("\n".join(f"line {i}" for i in range(10)) + "\n")
    out = _read_last_n_lines(p, 3)
    assert out == ["line 7\n", "line 8\n", "line 9\n"]


def test_read_last_n_lines_empty_file(tmp_path):
    p = tmp_path / "empty.log"
    p.write_text("")
    assert _read_last_n_lines(p, 5) == []


def test_read_last_n_lines_more_than_file_has(tmp_path):
    p = tmp_path / "short.log"
    p.write_text("only line\n")
    assert _read_last_n_lines(p, 100) == ["only line\n"]


# ── tail_log error paths ────────────────────────────────────────────────


@pytest.fixture
def isolated_phalanx_home(tmp_path, monkeypatch):
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    yield tmp_path


def _seed_log(home, name, lines):
    log_dir = home / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / name).write_text("\n".join(lines) + "\n")


def test_tail_log_unknown_name(isolated_phalanx_home, capsys):
    rc = tail_log("does-not-exist")
    captured = capsys.readouterr()
    assert rc == 2
    assert "Unknown log" in captured.err


def test_tail_log_missing_file(isolated_phalanx_home, capsys):
    rc = tail_log("agent")
    captured = capsys.readouterr()
    assert rc == 1
    assert "Log file not found" in captured.err


def test_tail_log_invalid_since_value(isolated_phalanx_home, capsys):
    _seed_log(isolated_phalanx_home, "agent.log", ["whatever"])
    rc = tail_log("agent", since="garbage")
    captured = capsys.readouterr()
    assert rc == 2
    assert "Invalid --since" in captured.err


def test_tail_log_invalid_level(isolated_phalanx_home, capsys):
    _seed_log(isolated_phalanx_home, "agent.log", ["whatever"])
    rc = tail_log("agent", level="LOUD")
    captured = capsys.readouterr()
    assert rc == 2
    assert "Invalid --level" in captured.err


def test_tail_log_invalid_component(isolated_phalanx_home, capsys):
    _seed_log(isolated_phalanx_home, "agent.log", ["whatever"])
    rc = tail_log("agent", component="rocket-engine")
    captured = capsys.readouterr()
    assert rc == 2
    assert "Unknown component" in captured.err


# ── tail_log happy paths ────────────────────────────────────────────────


def test_tail_log_default_prints_last_lines(isolated_phalanx_home, capsys):
    _seed_log(
        isolated_phalanx_home, "agent.log",
        [f"2026-04-05 22:35:0{i} INFO agent.main: line {i}" for i in range(8)],
    )
    rc = tail_log("agent", num_lines=3)
    captured = capsys.readouterr()
    assert rc == 0
    assert "(last 3)" in captured.out
    assert "line 7" in captured.out
    assert "line 6" in captured.out
    assert "line 5" in captured.out
    assert "line 4" not in captured.out


def test_tail_log_filters_by_level(isolated_phalanx_home, capsys):
    _seed_log(isolated_phalanx_home, "agent.log", [
        "2026-04-05 22:35:00 INFO agent.main: routine",
        "2026-04-05 22:35:01 WARNING agent.main: heads up",
        "2026-04-05 22:35:02 ERROR agent.main: oops",
    ])
    rc = tail_log("agent", num_lines=10, level="WARNING")
    captured = capsys.readouterr()
    assert rc == 0
    assert "heads up" in captured.out
    assert "oops" in captured.out
    assert "routine" not in captured.out


def test_tail_log_filters_by_session(isolated_phalanx_home, capsys):
    _seed_log(isolated_phalanx_home, "agent.log", [
        "2026-04-05 22:35:00 INFO [sess_aaa] agent: from a",
        "2026-04-05 22:35:01 INFO [sess_bbb] agent: from b",
    ])
    rc = tail_log("agent", num_lines=10, session="sess_aaa")
    captured = capsys.readouterr()
    assert rc == 0
    assert "from a" in captured.out
    assert "from b" not in captured.out


def test_tail_log_filters_by_component(isolated_phalanx_home, capsys):
    _seed_log(isolated_phalanx_home, "agent.log", [
        "2026-04-05 22:35:00 INFO agent.main: agent line",
        "2026-04-05 22:35:01 INFO tools.terminal: tools line",
        "2026-04-05 22:35:02 INFO gateway.run: gateway line",
    ])
    rc = tail_log("agent", num_lines=10, component="tools")
    captured = capsys.readouterr()
    assert rc == 0
    assert "tools line" in captured.out
    assert "agent line" not in captured.out
    assert "gateway line" not in captured.out


def test_tail_log_emits_filter_descriptor_in_header(
    isolated_phalanx_home, capsys,
):
    _seed_log(isolated_phalanx_home, "agent.log", [
        "2026-04-05 22:35:00 INFO agent.main: x",
    ])
    tail_log("agent", level="INFO", session="sess_abc")
    captured = capsys.readouterr()
    assert "level>=INFO" in captured.out
    assert "session=sess_abc" in captured.out


# ── list_logs ───────────────────────────────────────────────────────────


def test_list_logs_no_directory(isolated_phalanx_home, capsys):
    rc = list_logs()
    captured = capsys.readouterr()
    assert rc == 0
    assert "No logs directory" in captured.out


def test_list_logs_empty_directory(isolated_phalanx_home, capsys):
    (isolated_phalanx_home / "logs").mkdir()
    rc = list_logs()
    captured = capsys.readouterr()
    assert rc == 0
    assert "no log files yet" in captured.out


def test_list_logs_lists_each_file_with_size(isolated_phalanx_home, capsys):
    _seed_log(isolated_phalanx_home, "agent.log", ["x"])
    _seed_log(isolated_phalanx_home, "errors.log", ["y" * 2000])
    rc = list_logs()
    captured = capsys.readouterr()
    assert rc == 0
    assert "agent.log" in captured.out
    assert "errors.log" in captured.out


# ── End-to-end via cli_main ─────────────────────────────────────────────


def test_cli_logs_default_routes_to_agent(isolated_phalanx_home, capsys):
    _seed_log(isolated_phalanx_home, "agent.log", [
        "2026-04-05 22:35:00 INFO agent.main: hello",
    ])
    rc = cli_main(["logs"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "hello" in captured.out


def test_cli_logs_list_action(isolated_phalanx_home, capsys):
    _seed_log(isolated_phalanx_home, "agent.log", ["x"])
    rc = cli_main(["logs", "list"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "agent.log" in captured.out


def test_cli_logs_with_filters(isolated_phalanx_home, capsys):
    _seed_log(isolated_phalanx_home, "agent.log", [
        "2026-04-05 22:35:00 INFO agent.main: routine",
        "2026-04-05 22:35:01 ERROR agent.main: bad",
    ])
    rc = cli_main(["logs", "agent", "--level", "ERROR"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "bad" in captured.out
    assert "routine" not in captured.out
