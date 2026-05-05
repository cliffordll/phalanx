"""CLI ``--resume`` flag end-to-end tests (Phase 2.5 wave 3).

Each test points ``PHALANX_HOME`` at a tmp dir, runs one ``oneshot`` to
seed a session row + messages, then runs a second ``oneshot --resume``
to confirm the history is loaded and threaded through to the model.
"""

from __future__ import annotations

import pytest

from hermes_cli.main import main as cli_main
from hermes_state import SessionDB
from tests.conftest import make_text_response


@pytest.fixture
def isolated_phalanx_home(tmp_path, monkeypatch):
    """Force every config / DB lookup into tmp_path for this test."""
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    monkeypatch.setenv("PHALANX_MODEL", "gpt-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://x/v1")
    yield tmp_path


def _seed_session(stub_openai, capsys):
    """Run one oneshot to land a session row + messages in the DB.

    Returns ``(session_id, full_state_db_path)``.  Captures and discards
    the seed run's stdout so the caller starts with a clean buffer.
    """
    stub_openai([make_text_response("seeded reply")])
    rc = cli_main(["oneshot", "first prompt"])
    capsys.readouterr()
    assert rc == 0


def test_resume_full_id_loads_history(isolated_phalanx_home, stub_openai, capsys):
    """A second turn against an existing session_id sees the prior turns."""
    # Seed.
    _seed_session(stub_openai, capsys)

    # Find the session id we just landed.
    db = SessionDB(db_path=isolated_phalanx_home / "state.db")
    rows = db._conn.execute(
        "SELECT id FROM sessions ORDER BY started_at DESC LIMIT 1"
    ).fetchall()
    assert len(rows) == 1
    sid = rows[0][0]
    seed_msgs = db.get_messages(sid)
    db.close()

    # Sanity: seed turn persisted system + user + assistant under one id.
    assert [m["role"] for m in seed_msgs] == ["system", "user", "assistant"]
    assert seed_msgs[1]["content"] == "first prompt"
    assert seed_msgs[2]["content"] == "seeded reply"

    # Resume.
    stub_openai([make_text_response("second reply")])
    rc = cli_main(["--resume", sid, "oneshot", "follow-up"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "second reply" in captured.out

    # Both turns now share one session row; the new user + assistant
    # pair were appended after the originals (no duplicate user prompt
    # because the seam-dedupe doesn't fire — there's an assistant turn
    # in between).
    db = SessionDB(db_path=isolated_phalanx_home / "state.db")
    try:
        all_msgs = db.get_messages(sid)
        roles = [m["role"] for m in all_msgs]
        contents = [m["content"] for m in all_msgs]
        assert roles == [
            "system", "user", "assistant",   # seeded turn
            "user", "assistant",             # resumed turn (no new system row)
        ]
        assert contents[1] == "first prompt"
        assert contents[2] == "seeded reply"
        assert contents[3] == "follow-up"
        assert contents[4] == "second reply"

        # Session row aggregates: ended_at re-stamped, end_reason = the
        # latest run's stop_reason.
        sess = db.get_session(sid)
        assert sess["end_reason"] == "completed"
    finally:
        db.close()


def test_resume_unique_prefix_resolves(isolated_phalanx_home, stub_openai, capsys):
    _seed_session(stub_openai, capsys)

    db = SessionDB(db_path=isolated_phalanx_home / "state.db")
    sid = db._conn.execute(
        "SELECT id FROM sessions ORDER BY started_at DESC LIMIT 1"
    ).fetchone()[0]
    db.close()

    # 8-char prefix of a uuid4 is overwhelmingly unique.
    prefix = sid[:8]
    stub_openai([make_text_response("ok")])
    rc = cli_main(["--resume", prefix, "oneshot", "again"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "ok" in captured.out


def test_resume_unknown_id_errors_with_code_2(isolated_phalanx_home, capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli_main(["--resume", "no-such-session", "oneshot", "hi"])
    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert "no matching session" in captured.err


def test_resume_ambiguous_prefix_errors(isolated_phalanx_home, capsys):
    """Two sessions sharing a prefix → resolve_session_id returns None."""
    # Hand-craft two collidable session ids by writing directly to the DB.
    db = SessionDB(db_path=isolated_phalanx_home / "state.db")
    db.create_session("dup_aaaa", source="cli")
    db.create_session("dup_aabb", source="cli")
    db.close()

    with pytest.raises(SystemExit) as exc_info:
        cli_main(["--resume", "dup_aa", "oneshot", "hi"])
    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert "no matching session" in captured.err
