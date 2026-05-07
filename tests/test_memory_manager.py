"""§2.8.b wave 1 tests — long-term memory CRUD, retrieval, prompt injection.

Three layers under test:

* :class:`hermes_state.SessionDB` memory CRUD + FTS5 trigram retrieval —
  schema reconciliation, store / list / update / delete round-trips,
  scope/category filtering, pinned-row inclusion, hit_count bump.
* :mod:`agent.memory_manager` — block formatter, sanitize_context tag
  stripping, MemoryManager search/inject helpers.
* :func:`run_agent.AIAgent._inject_memory_block` — the wire-in point
  that prepends the envelope to ``effective_system`` on turn 0.

Tests use ``stub_session_db`` from test_session_db.py-style fixtures
(per-test SQLite under ``tmp_path``).  No network, no real model.
"""

from __future__ import annotations

import io
import json
import time

import pytest

from hermes_state import SessionDB
from agent.memory_manager import (
    MEMORY_CONTEXT_CLOSE,
    MEMORY_CONTEXT_OPEN,
    MemoryManager,
    StreamingContextScrubber,
    build_memory_context_block,
    sanitize_context,
)


# ── fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def stub_db(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    yield db
    db.close()


# ── schema (§2.8.b wave 1 — task #1) ──────────────────────────────────


def test_memories_table_present_after_init(stub_db):
    rows = stub_db._conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type IN ('table','view') AND name LIKE 'memor%'"
    ).fetchall()
    names = {r[0] for r in rows}
    # row store + FTS5 virtual table
    assert "memories" in names
    assert "memories_fts" in names


def test_memories_v12_migration_from_v11(tmp_path):
    """An existing v11 DB without the memories table gets the v12 add.

    Simulates an older install by opening a fresh DB (which lands at the
    current schema), dropping the memories rows + FTS table, and
    rewinding ``schema_version`` to 11.  Re-opening must repopulate the
    table via the v12 migration branch and bump the version.
    """
    db_path = tmp_path / "old.db"
    db = SessionDB(db_path=db_path)
    db._conn.execute("DROP TABLE IF EXISTS memories")
    db._conn.execute("DROP TABLE IF EXISTS memories_fts")
    db._conn.execute("UPDATE schema_version SET version = 11")
    db._conn.commit()
    db.close()

    db = SessionDB(db_path=db_path)
    try:
        rows = db._conn.execute(
            "SELECT name FROM sqlite_master WHERE name='memories'"
        ).fetchall()
        assert rows, "v12 migration did not create memories table"
        ver = db._conn.execute(
            "SELECT version FROM schema_version"
        ).fetchone()[0]
        assert ver >= 12

        # And the row store must accept inserts.
        mid = db.store_memory("preference", "use spaces not tabs")
        assert mid > 0
    finally:
        db.close()


# ── CRUD (task #2) ────────────────────────────────────────────────────


def test_store_memory_round_trip(stub_db):
    mid = stub_db.store_memory(
        "preference", "user prefers pytest",
        scope="global", pinned=True,
    )
    row = stub_db.get_memory(mid)
    assert row is not None
    assert row["category"] == "preference"
    assert row["content"] == "user prefers pytest"
    assert row["scope"] == "global"
    assert row["pinned"] == 1
    assert row["hit_count"] == 0
    assert row["created_at"] > 0
    assert row["updated_at"] > 0


def test_store_memory_rejects_empty_content(stub_db):
    with pytest.raises(ValueError):
        stub_db.store_memory("preference", "")
    with pytest.raises(ValueError):
        stub_db.store_memory("preference", "   ")


def test_store_memory_rejects_unknown_scope(stub_db):
    with pytest.raises(ValueError):
        stub_db.store_memory("fact", "x", scope="bogus")


def test_store_memory_defaults_blank_category_to_note(stub_db):
    mid = stub_db.store_memory("", "raw observation")
    assert stub_db.get_memory(mid)["category"] == "note"


def test_update_memory_changes_fields(stub_db):
    mid = stub_db.store_memory("fact", "v1 content")
    created_at = stub_db.get_memory(mid)["updated_at"]
    time.sleep(0.01)  # guarantee distinct updated_at
    ok = stub_db.update_memory(mid, content="v2 content", pinned=True)
    assert ok is True
    row = stub_db.get_memory(mid)
    assert row["content"] == "v2 content"
    assert row["pinned"] == 1
    assert row["updated_at"] > created_at


def test_update_memory_returns_false_for_unknown_id(stub_db):
    assert stub_db.update_memory(99999, content="nope") is False


def test_delete_memory(stub_db):
    mid = stub_db.store_memory("fact", "delete me")
    assert stub_db.delete_memory(mid) is True
    assert stub_db.get_memory(mid) is None
    assert stub_db.delete_memory(mid) is False


def test_list_memories_pinned_first_then_recent(stub_db):
    a = stub_db.store_memory("fact", "old fact", scope="global")
    time.sleep(0.01)
    b = stub_db.store_memory("fact", "new fact", scope="global")
    time.sleep(0.01)
    c = stub_db.store_memory("fact", "pinned fact", scope="global", pinned=True)
    rows = stub_db.list_memories()
    ids = [r["id"] for r in rows]
    assert ids[0] == c, "pinned row should sort first"
    assert ids[1] == b and ids[2] == a, "non-pinned rows by updated_at desc"


def test_list_memories_filters(stub_db):
    stub_db.store_memory("preference", "p1", scope="global")
    stub_db.store_memory("preference", "p2", scope="project")
    stub_db.store_memory("fact", "f1", scope="global", pinned=True)
    assert {r["content"] for r in stub_db.list_memories(category="preference")} == {"p1", "p2"}
    assert {r["content"] for r in stub_db.list_memories(scope="global")} == {"p1", "f1"}
    assert {r["content"] for r in stub_db.list_memories(pinned_only=True)} == {"f1"}


def test_memory_count(stub_db):
    stub_db.store_memory("fact", "x", scope="global")
    stub_db.store_memory("fact", "y", scope="project")
    assert stub_db.memory_count() == 2
    assert stub_db.memory_count(scope="global") == 1


# ── Retrieval (task #2) ───────────────────────────────────────────────


def test_retrieve_memories_basic_match(stub_db):
    stub_db.store_memory("fact", "the project uses ruff for linting")
    stub_db.store_memory("fact", "tests live under tests/ dir")
    rows = stub_db.retrieve_memories("ruff", limit=5, bump_hits=False)
    contents = [r["content"] for r in rows]
    assert "the project uses ruff for linting" in contents


def test_retrieve_memories_includes_pinned_even_without_match(stub_db):
    """A pinned global memory whose body has no token overlap with the
    query should still appear in the result set — that's how
    'always-true' user preferences stay surfaced regardless of phrasing.
    """
    stub_db.store_memory(
        "preference", "user prefers terse responses",
        scope="global", pinned=True,
    )
    stub_db.store_memory("fact", "the project uses ruff for linting")
    rows = stub_db.retrieve_memories(
        "completely unrelated query xyz123", limit=5, bump_hits=False,
    )
    contents = [r["content"] for r in rows]
    assert "user prefers terse responses" in contents


def test_retrieve_memories_scope_filter_excludes_session(stub_db):
    """Scope='session' shouldn't leak into a global lookup."""
    stub_db.store_memory("fact", "global ruff fact", scope="global")
    stub_db.store_memory("fact", "session ruff fact", scope="session")
    rows = stub_db.retrieve_memories(
        "ruff", limit=5, scopes=["global"], bump_hits=False,
    )
    contents = [r["content"] for r in rows]
    assert "global ruff fact" in contents
    assert "session ruff fact" not in contents


def test_retrieve_memories_rejects_unknown_scope(stub_db):
    with pytest.raises(ValueError):
        stub_db.retrieve_memories("x", scopes=["bogus"])


def test_retrieve_memories_bumps_hit_count(stub_db):
    mid = stub_db.store_memory("fact", "ruff is configured for the project")
    assert stub_db.get_memory(mid)["hit_count"] == 0
    stub_db.retrieve_memories("ruff", limit=5, bump_hits=True)
    row = stub_db.get_memory(mid)
    assert row["hit_count"] == 1
    assert row["last_used_at"] is not None
    stub_db.retrieve_memories("ruff", limit=5, bump_hits=True)
    assert stub_db.get_memory(mid)["hit_count"] == 2


def test_retrieve_memories_no_bump_with_flag(stub_db):
    mid = stub_db.store_memory("fact", "ruff configures linting")
    stub_db.retrieve_memories("ruff", limit=5, bump_hits=False)
    assert stub_db.get_memory(mid)["hit_count"] == 0


def test_retrieve_memories_trigram_substring(stub_db):
    """Trigram tokenizer matches on substrings (not whole words)."""
    stub_db.store_memory("fact", "the database is SQLite-backed")
    rows = stub_db.retrieve_memories("SQLi", limit=5, bump_hits=False)
    assert any("SQLite" in r["content"] for r in rows)


def test_retrieve_memories_handles_fts_special_chars(stub_db):
    """User queries with FTS5 operators like AND/OR/* must not blow up."""
    stub_db.store_memory("fact", "user uses AND or OR sometimes")
    # Should not raise; AND/OR get quoted as literal terms.
    stub_db.retrieve_memories("AND OR *", limit=5, bump_hits=False)


def test_fts_escape_strips_quotes_and_control(stub_db):
    """The escape helper must produce safe MATCH input even from junk."""
    out = SessionDB._fts_escape('hello "world"\x00\x01 foo')
    # Only printable, no embedded quotes; tokens wrapped in their own
    # double-quotes.
    assert '"' in out
    assert "\x00" not in out and "\x01" not in out


# ── memory_manager block formatter ────────────────────────────────────


def test_build_memory_context_block_empty_returns_empty():
    assert build_memory_context_block([]) == ""
    assert build_memory_context_block(None) == ""
    assert build_memory_context_block("") == ""


def test_build_memory_context_block_renders_rows():
    rows = [
        {"category": "preference", "scope": "global", "pinned": 1,
         "content": "user prefers pytest"},
        {"category": "fact", "scope": "project", "pinned": 0,
         "content": "ruff lint runs on commit"},
    ]
    out = build_memory_context_block(rows)
    assert out.startswith(MEMORY_CONTEXT_OPEN)
    assert out.rstrip().endswith(MEMORY_CONTEXT_CLOSE)
    # Pinned marker must be visible.
    assert "[preference/global*]" in out
    assert "[fact/project]" in out
    assert "user prefers pytest" in out


def test_build_memory_context_block_legacy_string_form():
    out = build_memory_context_block("hand-built body text")
    assert MEMORY_CONTEXT_OPEN in out and "hand-built body text" in out


def test_sanitize_context_strips_envelope():
    raw = (
        "leading text\n"
        f"{MEMORY_CONTEXT_OPEN}\nsecret memory\n{MEMORY_CONTEXT_CLOSE}"
        "\ntrailing text"
    )
    cleaned = sanitize_context(raw)
    assert "secret memory" not in cleaned
    assert "leading text" in cleaned and "trailing text" in cleaned


def test_sanitize_context_passthrough_when_no_tag():
    assert sanitize_context("hello") == "hello"
    assert sanitize_context("") == ""


def test_streaming_scrubber_drops_envelope_payload():
    scrub = StreamingContextScrubber()
    out = scrub.feed("before ")
    out += scrub.feed(MEMORY_CONTEXT_OPEN)
    out += scrub.feed("hidden ")
    out += scrub.feed("payload")
    out += scrub.feed(MEMORY_CONTEXT_CLOSE)
    out += scrub.feed(" after")
    out += scrub.flush()
    assert "before" in out
    assert "after" in out
    assert "hidden" not in out and "payload" not in out


def test_streaming_scrubber_holds_back_partial_open_tag():
    """Tag straddling chunk boundaries must not leak its payload."""
    scrub = StreamingContextScrubber()
    out = scrub.feed("ok prefix " + MEMORY_CONTEXT_OPEN[:8])
    out += scrub.feed(MEMORY_CONTEXT_OPEN[8:] + "secret" + MEMORY_CONTEXT_CLOSE)
    out += scrub.feed(" suffix")
    out += scrub.flush()
    assert "secret" not in out
    assert "ok prefix" in out and "suffix" in out


# ── MemoryManager ─────────────────────────────────────────────────────


def test_memory_manager_disabled_when_db_none():
    mgr = MemoryManager(db=None, enabled=True)
    assert mgr.enabled is False
    assert mgr.list() == []
    assert mgr.search("anything") == []


def test_memory_manager_search_does_not_bump(stub_db):
    mid = stub_db.store_memory("fact", "ruff is configured")
    mgr = MemoryManager(stub_db, enabled=True)
    rows = mgr.search("ruff")
    assert any(r["id"] == mid for r in rows)
    # User-facing search defaults to bump_hits=False.
    assert stub_db.get_memory(mid)["hit_count"] == 0


def test_memory_manager_retrieve_for_prompt_bumps(stub_db):
    mid = stub_db.store_memory("fact", "ruff is configured")
    mgr = MemoryManager(stub_db, enabled=True)
    mgr.retrieve_for_prompt("ruff")
    assert stub_db.get_memory(mid)["hit_count"] == 1


def test_memory_manager_inject_prepends_block(stub_db):
    stub_db.store_memory(
        "preference", "user prefers terse",
        scope="global", pinned=True,
    )
    mgr = MemoryManager(stub_db, enabled=True)
    out = mgr.inject_into_system_prompt("BASE PROMPT", query="anything")
    assert MEMORY_CONTEXT_OPEN in out
    assert MEMORY_CONTEXT_CLOSE in out
    assert "user prefers terse" in out
    assert out.endswith("BASE PROMPT")


def test_memory_manager_inject_no_op_when_no_memories(stub_db):
    mgr = MemoryManager(stub_db, enabled=True)
    out = mgr.inject_into_system_prompt("BASE PROMPT", query="anything")
    assert out == "BASE PROMPT"


def test_memory_manager_inject_no_op_when_disabled(stub_db):
    stub_db.store_memory(
        "preference", "user prefers terse",
        scope="global", pinned=True,
    )
    mgr = MemoryManager(stub_db, enabled=False)
    out = mgr.inject_into_system_prompt("BASE PROMPT", query="anything")
    assert out == "BASE PROMPT"


# ── AIAgent wire-in (task #4) ─────────────────────────────────────────


def test_aiagent_inject_memory_block_when_db_present(tmp_path, monkeypatch):
    """AIAgent.run_conversation should prepend a memory block on turn 0
    when the bound SessionDB has matching memories.

    We exercise just the helper (``_inject_memory_block``) with a real
    DB so the test doesn't need a stub OpenAI client.
    """
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    db = SessionDB(db_path=tmp_path / "state.db")
    db.store_memory(
        "preference", "user prefers terse",
        scope="global", pinned=True,
    )
    try:
        from run_agent import AIAgent
        agent = AIAgent(model="dummy", session_db=db, base_url="http://x")
        out = agent._inject_memory_block(
            "BASE PROMPT", "please write a function"
        )
        assert MEMORY_CONTEXT_OPEN in out
        assert "user prefers terse" in out
    finally:
        db.close()


def test_aiagent_inject_no_op_when_no_db(tmp_path, monkeypatch):
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    from run_agent import AIAgent
    agent = AIAgent(model="dummy", session_db=None, base_url="http://x")
    out = agent._inject_memory_block("BASE PROMPT", "any query")
    assert out == "BASE PROMPT"


def test_aiagent_inject_no_op_when_config_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    # Seed a config that disables memory injection.
    cfg_dir = tmp_path
    cfg_dir.mkdir(exist_ok=True)
    (tmp_path / "config.yaml").write_text(
        "memory:\n  enabled: false\n", encoding="utf-8"
    )
    db = SessionDB(db_path=tmp_path / "state.db")
    db.store_memory(
        "preference", "user prefers terse",
        scope="global", pinned=True,
    )
    try:
        # Reload config cache so the new file is seen.
        from hermes_cli import config as cfg_mod
        cfg_mod._RAW_CONFIG_CACHE.clear()
        from run_agent import AIAgent
        agent = AIAgent(model="dummy", session_db=db, base_url="http://x")
        out = agent._inject_memory_block("BASE PROMPT", "any query")
        assert out == "BASE PROMPT"
    finally:
        db.close()


# ── CLI (task #5) ─────────────────────────────────────────────────────


def _run_cli(monkeypatch, capsys, argv, stdin: str = ""):
    """Invoke phalanx CLI in-process with the given argv (excluding 'phalanx').

    Returns ``(rc, stdout, stderr)``.
    """
    monkeypatch.setattr("sys.argv", ["phalanx", *argv])
    if stdin:
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    from hermes_cli.main import main
    rc = main()
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def test_cli_memory_add_and_list(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    rc, out, err = _run_cli(
        monkeypatch, capsys,
        ["memory", "add", "--category", "preference",
         "--scope", "global", "--pinned", "user wants concise output"],
    )
    assert rc == 0, err
    assert "stored memory" in out

    rc, out, err = _run_cli(monkeypatch, capsys, ["memory", "list"])
    assert rc == 0, err
    assert "user wants concise" in out
    assert "preference" in out


def test_cli_memory_add_from_stdin(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    rc, out, err = _run_cli(
        monkeypatch, capsys,
        ["memory", "add", "--category", "fact"],
        stdin="multi\nline\nfact body",
    )
    assert rc == 0, err
    assert "stored memory" in out


def test_cli_memory_search_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    db = SessionDB(db_path=tmp_path / "state.db")
    db.store_memory("fact", "ruff lints the project")
    db.close()
    rc, out, err = _run_cli(
        monkeypatch, capsys,
        ["memory", "search", "ruff", "--json"],
    )
    assert rc == 0, err
    parsed = json.loads(out)
    assert any("ruff" in r["content"] for r in parsed)


def test_cli_memory_show_and_delete(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    db = SessionDB(db_path=tmp_path / "state.db")
    mid = db.store_memory("fact", "delete-me content")
    db.close()

    rc, out, err = _run_cli(monkeypatch, capsys, ["memory", "show", str(mid)])
    assert rc == 0
    assert "delete-me content" in out

    # Without --yes, should refuse and exit 1.
    rc, out, err = _run_cli(
        monkeypatch, capsys, ["memory", "delete", str(mid)],
    )
    assert rc == 1
    # Memory still present.
    db = SessionDB(db_path=tmp_path / "state.db")
    assert db.get_memory(mid) is not None
    db.close()

    rc, out, err = _run_cli(
        monkeypatch, capsys, ["memory", "delete", str(mid), "--yes"],
    )
    assert rc == 0
    assert f"deleted memory {mid}" in out


def test_cli_memory_pin_toggle(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    db = SessionDB(db_path=tmp_path / "state.db")
    mid = db.store_memory("fact", "pin me")
    db.close()
    rc, out, err = _run_cli(monkeypatch, capsys, ["memory", "pin", str(mid)])
    assert rc == 0
    db = SessionDB(db_path=tmp_path / "state.db")
    assert db.get_memory(mid)["pinned"] == 1
    db.close()
    rc, out, err = _run_cli(
        monkeypatch, capsys, ["memory", "pin", str(mid), "--unpin"],
    )
    assert rc == 0
    db = SessionDB(db_path=tmp_path / "state.db")
    assert db.get_memory(mid)["pinned"] == 0
    db.close()


def test_cli_memory_show_missing_returns_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    rc, out, err = _run_cli(monkeypatch, capsys, ["memory", "show", "999"])
    assert rc == 2
    assert "not found" in err
