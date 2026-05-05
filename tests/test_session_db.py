"""SessionDB wave-1 tests — schema, CRUD, encoding round-trip."""

from __future__ import annotations

import json
import sqlite3
import threading
import time

import pytest

from hermes_state import SessionDB, SCHEMA_VERSION


# ── Fixture ──────────────────────────────────────────────────────────────


@pytest.fixture
def stub_session_db(tmp_path):
    """Per-test SQLite file under tmp_path; auto-cleaned by pytest."""
    db = SessionDB(db_path=tmp_path / "state.db")
    yield db
    db.close()


# ── Schema / startup ─────────────────────────────────────────────────────


def test_init_creates_tables_and_indexes(stub_session_db):
    """Fresh DB should have all sessions/messages/state_meta + FTS tables."""
    rows = stub_session_db._conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type IN ('table','view') "
        "ORDER BY name"
    ).fetchall()
    names = {row[0] for row in rows}
    assert "sessions" in names
    assert "messages" in names
    assert "state_meta" in names
    assert "schema_version" in names
    assert "messages_fts" in names
    assert "messages_fts_trigram" in names


def test_init_records_current_schema_version(stub_session_db):
    row = stub_session_db._conn.execute(
        "SELECT version FROM schema_version LIMIT 1"
    ).fetchone()
    assert row[0] == SCHEMA_VERSION


def test_reconcile_columns_adds_missing(tmp_path):
    """Old DB missing a recently-added column gets ALTERed on next open.

    Simulates a real upgrade path: pre-existing rows are present, the
    table has all columns referenced by SCHEMA_SQL indexes, but the
    ``api_call_count`` column was added later and isn't in the live
    table.  ``_reconcile_columns`` must ADD it.
    """
    db_path = tmp_path / "old.db"
    raw = sqlite3.connect(str(db_path))
    # Same shape as SCHEMA_SQL minus the api_call_count column at the
    # tail — that's the most recently added column in the lineage we
    # care about preserving compatibility with.
    raw.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            user_id TEXT,
            model TEXT,
            model_config TEXT,
            system_prompt TEXT,
            parent_session_id TEXT,
            started_at REAL NOT NULL,
            ended_at REAL,
            end_reason TEXT,
            message_count INTEGER DEFAULT 0,
            tool_call_count INTEGER DEFAULT 0,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cache_read_tokens INTEGER DEFAULT 0,
            cache_write_tokens INTEGER DEFAULT 0,
            reasoning_tokens INTEGER DEFAULT 0,
            billing_provider TEXT,
            billing_base_url TEXT,
            billing_mode TEXT,
            estimated_cost_usd REAL,
            actual_cost_usd REAL,
            cost_status TEXT,
            cost_source TEXT,
            pricing_version TEXT,
            title TEXT
        );
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version (version) VALUES (11);
        INSERT INTO sessions (id, source, started_at) VALUES ('legacy', 'cli', 0);
        """
    )
    raw.commit()
    raw.close()

    db = SessionDB(db_path=db_path)
    try:
        cols = {row[1] for row in db._conn.execute(
            'PRAGMA table_info("sessions")'
        ).fetchall()}
        # Pre-existing row survived the reconcile.
        assert db.get_session("legacy") is not None
    finally:
        db.close()
    assert "api_call_count" in cols


# ── Session lifecycle ────────────────────────────────────────────────────


def test_create_and_get_session(stub_session_db):
    sid = stub_session_db.create_session(
        "sess_aaa", source="cli", model="qwen2.5:1.5b",
    )
    assert sid == "sess_aaa"
    row = stub_session_db.get_session("sess_aaa")
    assert row is not None
    assert row["id"] == "sess_aaa"
    assert row["source"] == "cli"
    assert row["model"] == "qwen2.5:1.5b"
    assert row["ended_at"] is None
    assert row["message_count"] == 0


def test_get_session_missing_returns_none(stub_session_db):
    assert stub_session_db.get_session("does-not-exist") is None


def test_create_is_idempotent(stub_session_db):
    """INSERT OR IGNORE — second call must not raise or replace."""
    stub_session_db.create_session("sess_b", source="cli", model="m1")
    # Second call shouldn't error or change the row.
    stub_session_db.create_session("sess_b", source="tui", model="m2")
    row = stub_session_db.get_session("sess_b")
    # First write wins.
    assert row["source"] == "cli"
    assert row["model"] == "m1"


def test_end_session_sets_ended_at(stub_session_db):
    stub_session_db.create_session("sess_end", source="cli")
    stub_session_db.end_session("sess_end", end_reason="completed")
    row = stub_session_db.get_session("sess_end")
    assert row["ended_at"] is not None
    assert row["end_reason"] == "completed"


def test_end_session_first_reason_wins(stub_session_db):
    """A second end_session() with a different reason must not overwrite."""
    stub_session_db.create_session("sess_end2", source="cli")
    stub_session_db.end_session("sess_end2", end_reason="compression")
    stub_session_db.end_session("sess_end2", end_reason="completed")
    row = stub_session_db.get_session("sess_end2")
    assert row["end_reason"] == "compression"


def test_reopen_session_clears_end(stub_session_db):
    stub_session_db.create_session("sess_re", source="cli")
    stub_session_db.end_session("sess_re", end_reason="completed")
    stub_session_db.reopen_session("sess_re")
    row = stub_session_db.get_session("sess_re")
    assert row["ended_at"] is None
    assert row["end_reason"] is None


def test_ensure_session_inserts_or_ignores(stub_session_db):
    sid = stub_session_db.ensure_session(
        "sess_ens", source="cli", model="m"
    )
    assert sid == "sess_ens"
    # Repeat call doesn't error.
    stub_session_db.ensure_session("sess_ens", source="cli", model="m")
    assert stub_session_db.get_session("sess_ens")["model"] == "m"


def test_update_system_prompt(stub_session_db):
    stub_session_db.create_session("sess_sp", source="cli")
    stub_session_db.update_system_prompt(
        "sess_sp", system_prompt="You are a helpful agent."
    )
    row = stub_session_db.get_session("sess_sp")
    assert row["system_prompt"] == "You are a helpful agent."


# ── Token / cost counters ────────────────────────────────────────────────


def test_update_token_counts_increments_by_default(stub_session_db):
    stub_session_db.create_session("sess_tok", source="cli")
    stub_session_db.update_token_counts(
        "sess_tok", input_tokens=10, output_tokens=5,
        cache_read_tokens=2, reasoning_tokens=1, api_call_count=1,
    )
    stub_session_db.update_token_counts(
        "sess_tok", input_tokens=3, output_tokens=4,
        cache_read_tokens=1, reasoning_tokens=0, api_call_count=1,
    )
    row = stub_session_db.get_session("sess_tok")
    assert row["input_tokens"] == 13
    assert row["output_tokens"] == 9
    assert row["cache_read_tokens"] == 3
    assert row["reasoning_tokens"] == 1
    assert row["api_call_count"] == 2


def test_update_token_counts_absolute_overrides(stub_session_db):
    stub_session_db.create_session("sess_abs", source="cli")
    stub_session_db.update_token_counts(
        "sess_abs", input_tokens=100, output_tokens=50, api_call_count=3,
    )
    stub_session_db.update_token_counts(
        "sess_abs", input_tokens=20, output_tokens=10,
        api_call_count=1, absolute=True,
    )
    row = stub_session_db.get_session("sess_abs")
    assert row["input_tokens"] == 20
    assert row["output_tokens"] == 10
    assert row["api_call_count"] == 1


def test_update_token_counts_backfills_model(stub_session_db):
    """update_token_counts must only fill model when it's still NULL."""
    stub_session_db.create_session("sess_mdl", source="cli")
    stub_session_db.update_token_counts("sess_mdl", model="gpt-x")
    assert stub_session_db.get_session("sess_mdl")["model"] == "gpt-x"
    stub_session_db.update_token_counts("sess_mdl", model="other")
    assert stub_session_db.get_session("sess_mdl")["model"] == "gpt-x"


# ── Content encoding ─────────────────────────────────────────────────────


def test_encode_decode_string_passthrough():
    assert SessionDB._encode_content("hello") == "hello"
    assert SessionDB._decode_content("hello") == "hello"


def test_encode_decode_none_passthrough():
    assert SessionDB._encode_content(None) is None
    assert SessionDB._decode_content(None) is None


def test_encode_decode_multimodal_list_round_trip():
    parts = [
        {"type": "text", "text": "describe this"},
        {"type": "image_url", "image_url": {"url": "http://x/y.png"}},
    ]
    encoded = SessionDB._encode_content(parts)
    assert isinstance(encoded, str)
    assert encoded.startswith("\x00json:")
    decoded = SessionDB._decode_content(encoded)
    assert decoded == parts


def test_encode_falls_back_to_str_on_unserializable():
    class Weird:
        def __repr__(self) -> str:
            return "<weird>"

    encoded = SessionDB._encode_content(Weird())
    assert encoded == "<weird>"


# ── Message round-trip ───────────────────────────────────────────────────


def test_append_and_get_messages_string_content(stub_session_db):
    stub_session_db.create_session("sess_msg", source="cli")
    rid1 = stub_session_db.append_message("sess_msg", "user", content="hello")
    rid2 = stub_session_db.append_message(
        "sess_msg", "assistant", content="hi back"
    )
    assert rid2 > rid1
    msgs = stub_session_db.get_messages("sess_msg")
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert [m["content"] for m in msgs] == ["hello", "hi back"]


def test_append_message_increments_counters(stub_session_db):
    stub_session_db.create_session("sess_cnt", source="cli")
    stub_session_db.append_message("sess_cnt", "user", content="a")
    stub_session_db.append_message(
        "sess_cnt", "assistant",
        content=None,
        tool_calls=[
            {"id": "c1", "type": "function",
             "function": {"name": "echo", "arguments": "{}"}},
            {"id": "c2", "type": "function",
             "function": {"name": "echo", "arguments": "{}"}},
        ],
    )
    row = stub_session_db.get_session("sess_cnt")
    assert row["message_count"] == 2
    assert row["tool_call_count"] == 2


def test_append_message_persists_tool_call_json(stub_session_db):
    stub_session_db.create_session("sess_tc", source="cli")
    stub_session_db.append_message(
        "sess_tc", "assistant",
        content=None,
        tool_calls=[
            {"id": "t1", "type": "function",
             "function": {"name": "echo", "arguments": '{"text":"hi"}'}},
        ],
    )
    msgs = stub_session_db.get_messages("sess_tc")
    assert msgs[0]["tool_calls"] == [
        {"id": "t1", "type": "function",
         "function": {"name": "echo", "arguments": '{"text":"hi"}'}},
    ]


def test_append_message_multimodal_content_round_trip(stub_session_db):
    stub_session_db.create_session("sess_mm", source="cli")
    parts = [
        {"type": "text", "text": "see this"},
        {"type": "image_url", "image_url": {"url": "http://x"}},
    ]
    stub_session_db.append_message("sess_mm", "user", content=parts)
    msgs = stub_session_db.get_messages("sess_mm")
    assert msgs[0]["content"] == parts


def test_get_messages_orders_by_timestamp(stub_session_db):
    stub_session_db.create_session("sess_ord", source="cli")
    stub_session_db.append_message("sess_ord", "user", content="1")
    stub_session_db.append_message("sess_ord", "assistant", content="2")
    stub_session_db.append_message("sess_ord", "user", content="3")
    msgs = stub_session_db.get_messages("sess_ord")
    assert [m["content"] for m in msgs] == ["1", "2", "3"]


def test_corrupt_tool_calls_falls_back_to_empty(stub_session_db):
    """Bad JSON in tool_calls column shouldn't crash get_messages."""
    stub_session_db.create_session("sess_bad", source="cli")
    stub_session_db.append_message("sess_bad", "assistant", content="x")
    # Inject corrupt JSON manually.
    with stub_session_db._lock:
        stub_session_db._conn.execute(
            "UPDATE messages SET tool_calls = ? WHERE session_id = ?",
            ("not-json{", "sess_bad"),
        )
        stub_session_db._conn.commit()
    msgs = stub_session_db.get_messages("sess_bad")
    assert msgs[0]["tool_calls"] == []


# ── Concurrency smoke ────────────────────────────────────────────────────


def test_concurrent_appends_serialize(tmp_path):
    """Two threads appending shouldn't lose rows or corrupt counters.

    Uses one shared SessionDB (single connection in WAL mode).  The
    write helper's BEGIN IMMEDIATE + jitter retry serializes them.
    """
    db = SessionDB(db_path=tmp_path / "concurrent.db")
    try:
        db.create_session("sess_cc", source="cli")

        def worker(prefix: str) -> None:
            for i in range(20):
                db.append_message("sess_cc", "user", content=f"{prefix}{i}")

        t1 = threading.Thread(target=worker, args=("a",))
        t2 = threading.Thread(target=worker, args=("b",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        msgs = db.get_messages("sess_cc")
        assert len(msgs) == 40
        assert db.get_session("sess_cc")["message_count"] == 40
    finally:
        db.close()
