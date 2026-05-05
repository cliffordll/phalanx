"""SessionDB wave-1 tests — schema, CRUD, encoding round-trip."""

from __future__ import annotations

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


# ── Resume helpers (wave 3) ──────────────────────────────────────────────


def test_resolve_session_id_exact_match(stub_session_db):
    stub_session_db.create_session("sess_xyz", source="cli")
    assert stub_session_db.resolve_session_id("sess_xyz") == "sess_xyz"


def test_resolve_session_id_unique_prefix(stub_session_db):
    stub_session_db.create_session("sess_aaaa", source="cli")
    stub_session_db.create_session("sess_bbbb", source="cli")
    assert stub_session_db.resolve_session_id("sess_aa") == "sess_aaaa"


def test_resolve_session_id_ambiguous_prefix_returns_none(stub_session_db):
    stub_session_db.create_session("sess_aaaa", source="cli")
    stub_session_db.create_session("sess_aabb", source="cli")
    assert stub_session_db.resolve_session_id("sess_aa") is None


def test_resolve_session_id_no_match_returns_none(stub_session_db):
    stub_session_db.create_session("sess_zzz", source="cli")
    assert stub_session_db.resolve_session_id("missing") is None


def test_resolve_session_id_escapes_like_metacharacters(stub_session_db):
    """``%`` and ``_`` in the input must not match arbitrary characters."""
    stub_session_db.create_session("sess_real", source="cli")
    stub_session_db.create_session("sess_other", source="cli")
    # If the LIKE escape is broken, ``s_ss%`` would match either id.
    assert stub_session_db.resolve_session_id("s%") is None


def test_resolve_resume_session_id_returns_self_when_messages_present(
    stub_session_db,
):
    stub_session_db.create_session("sess_self", source="cli")
    stub_session_db.append_message("sess_self", "user", content="hi")
    assert (
        stub_session_db.resolve_resume_session_id("sess_self") == "sess_self"
    )


def test_resolve_resume_session_id_walks_to_compression_descendant(
    stub_session_db,
):
    """Empty parent compresses into a child that holds the messages."""
    stub_session_db.create_session("sess_parent", source="cli")
    stub_session_db.end_session("sess_parent", end_reason="compression")
    stub_session_db.create_session(
        "sess_child", source="cli", parent_session_id="sess_parent",
    )
    stub_session_db.append_message("sess_child", "user", content="continued")
    assert (
        stub_session_db.resolve_resume_session_id("sess_parent") == "sess_child"
    )


def test_resolve_resume_session_id_returns_self_when_no_descendants(
    stub_session_db,
):
    stub_session_db.create_session("sess_lone", source="cli")
    assert (
        stub_session_db.resolve_resume_session_id("sess_lone") == "sess_lone"
    )


def test_get_messages_as_conversation_strips_db_metadata(stub_session_db):
    """The replay shape is OpenAI-style: role+content (+ tool_* / reasoning)."""
    stub_session_db.create_session("sess_rep", source="cli")
    stub_session_db.append_message("sess_rep", "user", content="hi")
    stub_session_db.append_message(
        "sess_rep", "assistant",
        content="calling echo",
        tool_calls=[
            {"id": "c1", "type": "function",
             "function": {"name": "echo", "arguments": "{}"}},
        ],
        finish_reason="tool_calls",
    )
    stub_session_db.append_message(
        "sess_rep", "tool",
        content="echo-result",
        tool_call_id="c1",
        tool_name="echo",
    )
    msgs = stub_session_db.get_messages_as_conversation("sess_rep")
    # No timestamp / id / token_count leaking through.
    for m in msgs:
        assert "timestamp" not in m
        assert "id" not in m
        assert "token_count" not in m
    assert msgs[0] == {"role": "user", "content": "hi"}
    assert msgs[1]["role"] == "assistant"
    assert msgs[1]["content"] == "calling echo"
    assert msgs[1]["tool_calls"][0]["function"]["name"] == "echo"
    assert msgs[1]["finish_reason"] == "tool_calls"
    assert msgs[2] == {
        "role": "tool",
        "content": "echo-result",
        "tool_call_id": "c1",
        "tool_name": "echo",
    }


def test_session_lineage_root_to_tip_orders_root_first(stub_session_db):
    stub_session_db.create_session("root", source="cli")
    stub_session_db.create_session("mid", source="cli", parent_session_id="root")
    stub_session_db.create_session("tip", source="cli", parent_session_id="mid")
    assert stub_session_db._session_lineage_root_to_tip("tip") == [
        "root", "mid", "tip",
    ]


def test_session_lineage_handles_cycle_defensively(stub_session_db):
    """A malformed cycle shouldn't loop forever."""
    stub_session_db._conn.execute(
        "INSERT INTO sessions (id, source, parent_session_id, started_at) "
        "VALUES ('a', 'cli', 'b', 0), ('b', 'cli', 'a', 0)"
    )
    stub_session_db._conn.commit()
    chain = stub_session_db._session_lineage_root_to_tip("a")
    assert chain[-1] == "a"  # always lands at the requested tip
    assert len(chain) <= 100


def test_is_duplicate_replayed_user_message_true_at_lineage_seam():
    """Same user prompt with no assistant progress in between → dupe."""
    history = [
        {"role": "user", "content": "second prompt"},
        # No assistant turn after — child session about to replay the
        # same user message it inherited from the parent.
    ]
    assert SessionDB._is_duplicate_replayed_user_message(
        history, {"role": "user", "content": "second prompt"}
    ) is True


def test_is_duplicate_replayed_user_message_false_when_assistant_progressed():
    """Real new turn — assistant produced content since last identical user."""
    history = [
        {"role": "user", "content": "second prompt"},
        {"role": "assistant", "content": "reply"},
    ]
    assert SessionDB._is_duplicate_replayed_user_message(
        history, {"role": "user", "content": "second prompt"}
    ) is False


def test_is_duplicate_replayed_user_message_false_for_different_content():
    history = [{"role": "user", "content": "first"}]
    assert SessionDB._is_duplicate_replayed_user_message(
        history, {"role": "user", "content": "second"}
    ) is False


# ── List / show / delete (wave 4) ────────────────────────────────────────


def test_list_sessions_rich_returns_recent_first(stub_session_db):
    """``started_at DESC`` — most recent session lands at row 0."""
    stub_session_db.create_session("sess_old", source="cli")
    time.sleep(0.01)
    stub_session_db.create_session("sess_new", source="cli")
    rows = stub_session_db.list_sessions_rich(limit=10)
    assert [r["id"] for r in rows[:2]] == ["sess_new", "sess_old"]


def test_list_sessions_rich_preview_first_user_message(stub_session_db):
    stub_session_db.create_session("sess_p", source="cli")
    stub_session_db.append_message(
        "sess_p", "system", content="ignored for preview",
    )
    stub_session_db.append_message(
        "sess_p", "user", content="first user msg here",
    )
    stub_session_db.append_message(
        "sess_p", "user", content="second user msg",
    )
    [row] = stub_session_db.list_sessions_rich(limit=10)
    assert row["preview"] == "first user msg here"


def test_list_sessions_rich_preview_truncates_long_content(stub_session_db):
    stub_session_db.create_session("sess_long", source="cli")
    long_text = "x" * 500
    stub_session_db.append_message("sess_long", "user", content=long_text)
    [row] = stub_session_db.list_sessions_rich(limit=10)
    assert row["preview"].endswith("...")
    assert len(row["preview"]) == 63


def test_list_sessions_rich_preview_flattens_newlines(stub_session_db):
    stub_session_db.create_session("sess_nl", source="cli")
    stub_session_db.append_message(
        "sess_nl", "user", content="line one\nline two\rline three",
    )
    [row] = stub_session_db.list_sessions_rich(limit=10)
    assert "\n" not in row["preview"]
    assert "\r" not in row["preview"]
    assert "line one line two line three" in row["preview"]


def test_list_sessions_rich_last_active_uses_message_timestamp(
    stub_session_db,
):
    stub_session_db.create_session("sess_la", source="cli")
    stub_session_db.append_message("sess_la", "user", content="ping")
    [row] = stub_session_db.list_sessions_rich(limit=10)
    assert row["last_active"] >= row["started_at"]


def test_list_sessions_rich_filters_by_source(stub_session_db):
    stub_session_db.create_session("sess_a", source="cli")
    stub_session_db.create_session("sess_b", source="gateway")
    rows = stub_session_db.list_sessions_rich(source="gateway", limit=10)
    assert [r["id"] for r in rows] == ["sess_b"]


def test_list_sessions_rich_excludes_subagent_children_by_default(
    stub_session_db,
):
    """Sub-agent runs (parent still alive) shouldn't surface at top level."""
    stub_session_db.create_session("sess_parent", source="cli")
    # Child started while parent is still alive — sub-agent shape.
    stub_session_db.create_session(
        "sess_subagent", source="cli", parent_session_id="sess_parent",
    )
    rows = stub_session_db.list_sessions_rich(limit=10)
    ids = [r["id"] for r in rows]
    assert "sess_parent" in ids
    assert "sess_subagent" not in ids


def test_list_sessions_rich_includes_branch_children(stub_session_db):
    """``end_reason='branched'`` siblings stay visible."""
    stub_session_db.create_session("sess_root", source="cli")
    stub_session_db.end_session("sess_root", end_reason="branched")
    # Branch child must start at-or-after the parent's ended_at.
    parent_ended_at = stub_session_db.get_session("sess_root")["ended_at"]
    stub_session_db._conn.execute(
        "INSERT INTO sessions (id, source, parent_session_id, started_at) "
        "VALUES (?, ?, ?, ?)",
        ("sess_branch", "cli", "sess_root", parent_ended_at + 1),
    )
    stub_session_db._conn.commit()
    rows = stub_session_db.list_sessions_rich(limit=10)
    ids = {r["id"] for r in rows}
    assert "sess_branch" in ids


def test_list_sessions_rich_include_children_shows_all(stub_session_db):
    stub_session_db.create_session("sess_p", source="cli")
    stub_session_db.create_session(
        "sess_c", source="cli", parent_session_id="sess_p",
    )
    rows = stub_session_db.list_sessions_rich(
        limit=10, include_children=True,
    )
    ids = {r["id"] for r in rows}
    assert ids == {"sess_p", "sess_c"}


def test_list_sessions_rich_pagination(stub_session_db):
    for i in range(5):
        stub_session_db.create_session(f"sess_{i}", source="cli")
    page1 = stub_session_db.list_sessions_rich(limit=2, offset=0)
    page2 = stub_session_db.list_sessions_rich(limit=2, offset=2)
    assert len(page1) == 2
    assert len(page2) == 2
    assert {r["id"] for r in page1}.isdisjoint({r["id"] for r in page2})


def test_get_session_rich_row_includes_preview_and_last_active(
    stub_session_db,
):
    stub_session_db.create_session("sess_r", source="cli")
    stub_session_db.append_message("sess_r", "user", content="payload")
    row = stub_session_db._get_session_rich_row("sess_r")
    assert row is not None
    assert row["preview"] == "payload"
    assert row["last_active"] >= row["started_at"]


def test_get_session_rich_row_missing_returns_none(stub_session_db):
    assert stub_session_db._get_session_rich_row("does-not-exist") is None


def test_delete_session_removes_session_and_messages(stub_session_db):
    stub_session_db.create_session("sess_del", source="cli")
    stub_session_db.append_message("sess_del", "user", content="hi")
    assert stub_session_db.delete_session("sess_del") is True
    assert stub_session_db.get_session("sess_del") is None
    assert stub_session_db.get_messages("sess_del") == []


def test_delete_session_orphans_children(stub_session_db):
    stub_session_db.create_session("sess_p", source="cli")
    stub_session_db.create_session(
        "sess_c", source="cli", parent_session_id="sess_p",
    )
    stub_session_db.delete_session("sess_p")
    child = stub_session_db.get_session("sess_c")
    assert child is not None
    assert child["parent_session_id"] is None


def test_delete_session_returns_false_for_missing(stub_session_db):
    assert stub_session_db.delete_session("does-not-exist") is False


def test_is_duplicate_replayed_ignores_non_user_role():
    assert SessionDB._is_duplicate_replayed_user_message(
        [], {"role": "assistant", "content": "x"}
    ) is False
