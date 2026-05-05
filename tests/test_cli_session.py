"""``hermes session`` subcommand end-to-end tests (Phase 2.5 wave 4)."""

from __future__ import annotations

import json

import pytest

from hermes_cli.main import main as cli_main
from hermes_state import SessionDB


@pytest.fixture
def isolated_phalanx_home(tmp_path, monkeypatch):
    """Force every config / DB lookup into tmp_path for this test."""
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    yield tmp_path


def _seed(home, **kwargs):
    """Insert a session row + a couple messages into the test DB."""
    db = SessionDB(db_path=home / "state.db")
    sid = kwargs.pop("session_id", "test_session_1234")
    source = kwargs.pop("source", "cli")
    model = kwargs.pop("model", "qwen2.5:1.5b")
    db.create_session(sid, source=source, model=model)
    db.append_message(sid, "system", content="system text")
    db.append_message(sid, "user", content="hello world")
    db.append_message(sid, "assistant", content="hi back")
    db.end_session(sid, end_reason="completed")
    db.close()
    return sid


# ── list ────────────────────────────────────────────────────────────────


def test_session_list_empty_db(isolated_phalanx_home, capsys):
    rc = cli_main(["session", "list"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "no sessions persisted" in captured.out


def test_session_list_shows_seeded_session(isolated_phalanx_home, capsys):
    sid = _seed(isolated_phalanx_home)
    rc = cli_main(["session", "list"])
    captured = capsys.readouterr()
    assert rc == 0
    # Header + one row.
    assert "PREVIEW" in captured.out
    assert sid[:8] in captured.out
    assert "qwen2.5:1.5b" in captured.out
    assert "hello world" in captured.out


def test_session_list_json_dumps_array(isolated_phalanx_home, capsys):
    _seed(isolated_phalanx_home)
    rc = cli_main(["session", "list", "--json"])
    captured = capsys.readouterr()
    assert rc == 0
    parsed = json.loads(captured.out)
    assert isinstance(parsed, list)
    assert len(parsed) == 1
    assert parsed[0]["preview"] == "hello world"


def test_session_list_filters_by_source(isolated_phalanx_home, capsys):
    _seed(isolated_phalanx_home, session_id="test_cli_a", source="cli")
    _seed(isolated_phalanx_home, session_id="test_gw_b", source="gateway")
    rc = cli_main(["session", "list", "--source", "gateway", "--json"])
    captured = capsys.readouterr()
    assert rc == 0
    parsed = json.loads(captured.out)
    assert {r["id"] for r in parsed} == {"test_gw_b"}


def test_session_list_respects_limit(isolated_phalanx_home, capsys):
    for i in range(5):
        _seed(isolated_phalanx_home, session_id=f"test_sess_{i}")
    rc = cli_main(["session", "list", "--limit", "2", "--json"])
    captured = capsys.readouterr()
    assert rc == 0
    parsed = json.loads(captured.out)
    assert len(parsed) == 2


# ── show ────────────────────────────────────────────────────────────────


def test_session_show_renders_metadata_and_messages(
    isolated_phalanx_home, capsys,
):
    sid = _seed(isolated_phalanx_home)
    rc = cli_main(["session", "show", sid])
    captured = capsys.readouterr()
    assert rc == 0
    assert sid in captured.out
    assert "qwen2.5:1.5b" in captured.out
    # The messages are printed as "--- [n] role ---" sections.
    assert "--- [0] system ---" in captured.out
    assert "--- [1] user ---" in captured.out
    assert "--- [2] assistant ---" in captured.out
    assert "hello world" in captured.out
    assert "hi back" in captured.out


def test_session_show_accepts_unique_prefix(isolated_phalanx_home, capsys):
    sid = _seed(isolated_phalanx_home, session_id="prefix_aaaa")
    rc = cli_main(["session", "show", "prefix_a"])
    captured = capsys.readouterr()
    assert rc == 0
    assert sid in captured.out


def test_session_show_unknown_id_errors(isolated_phalanx_home, capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli_main(["session", "show", "nope-not-here"])
    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert "not found" in captured.err


# ── dump ────────────────────────────────────────────────────────────────


def test_session_dump_emits_jsonl(isolated_phalanx_home, capsys):
    sid = _seed(isolated_phalanx_home)
    rc = cli_main(["session", "dump", sid])
    captured = capsys.readouterr()
    assert rc == 0
    lines = [l for l in captured.out.splitlines() if l.strip()]
    assert len(lines) == 3
    parsed = [json.loads(l) for l in lines]
    assert [m["role"] for m in parsed] == ["system", "user", "assistant"]
    assert parsed[1]["content"] == "hello world"


def test_session_dump_unknown_id_errors(isolated_phalanx_home, capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli_main(["session", "dump", "nope"])
    assert exc_info.value.code == 2


# ── delete ──────────────────────────────────────────────────────────────


def test_session_delete_requires_yes_flag(isolated_phalanx_home, capsys):
    sid = _seed(isolated_phalanx_home)
    rc = cli_main(["session", "delete", sid])
    captured = capsys.readouterr()
    # No --yes: aborts with exit code 1, session still present.
    assert rc == 1
    assert "pass --yes" in captured.err
    db = SessionDB(db_path=isolated_phalanx_home / "state.db")
    try:
        assert db.get_session(sid) is not None
    finally:
        db.close()


def test_session_delete_with_yes_removes_row(isolated_phalanx_home, capsys):
    sid = _seed(isolated_phalanx_home)
    rc = cli_main(["session", "delete", sid, "--yes"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "deleted session" in captured.out
    db = SessionDB(db_path=isolated_phalanx_home / "state.db")
    try:
        assert db.get_session(sid) is None
        assert db.get_messages(sid) == []
    finally:
        db.close()


def test_session_delete_unknown_id_errors(isolated_phalanx_home, capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli_main(["session", "delete", "nope", "--yes"])
    assert exc_info.value.code == 2


# ── help ────────────────────────────────────────────────────────────────


def test_session_help_prints_usage(isolated_phalanx_home, capsys):
    rc = cli_main(["session"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "list|show|dump|delete" in captured.out
