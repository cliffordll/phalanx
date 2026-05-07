"""§2.8.d wave 3 tests — event_log table + auto-hooks + CLI/REPL surface.

Three layers under test:

* :class:`SessionDB` event_log CRUD — log_event / query_events /
  event_count / get_event filters, content-hash determinism, JSON
  metadata round-trip.
* Auto-hooks fired by phalanx subsystems — checkpoint_create /
  rollback events emitted by ``CheckpointManager`` when a SessionDB is
  bound, memory_store events emitted by ``SessionDB.store_memory``
  itself.  The tool_call_pre / tool_call_post / guardrail_verdict
  hooks live on ``AIAgent`` and are exercised through a stub registry.
* CLI + REPL surfaces — ``phalanx audit log/count/show`` and the
  ``/audit`` slash command.
"""

from __future__ import annotations

import io
import json
import time

import pytest

from hermes_state import SCHEMA_VERSION, SessionDB
from tools.checkpoint_manager import CheckpointManager


# ─── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path):
    """Per-test SessionDB pointing at a temp state.db."""
    inst = SessionDB(db_path=tmp_path / "state.db")
    yield inst
    inst.close()


@pytest.fixture
def fresh_home(tmp_path, monkeypatch):
    """PHALANX_HOME pointing at a fresh tmp dir."""
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    return tmp_path


# ─── Schema sanity ───────────────────────────────────────────────────────


def test_event_log_table_exists(db):
    rows = db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='event_log'"
    ).fetchall()
    assert len(rows) == 1


def test_schema_version_is_at_least_13(db):
    row = db._conn.execute(
        "SELECT version FROM schema_version LIMIT 1"
    ).fetchone()
    assert row[0] >= 13
    assert SCHEMA_VERSION >= 13


def test_event_log_indexes_exist(db):
    idx_rows = db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND tbl_name='event_log'"
    ).fetchall()
    names = {r[0] for r in idx_rows}
    assert "idx_event_log_session" in names
    assert "idx_event_log_type" in names
    assert "idx_event_log_timestamp" in names


# ─── log_event / get_event ───────────────────────────────────────────────


def test_log_event_basic_insert_returns_id(db):
    eid = db.log_event(
        "tool_call_pre",
        session_id="sess-1",
        agent_id="sess-1#d0",
        target="echo",
        content_hash="abc123",
        metadata={"args": {"x": 1}},
    )
    assert isinstance(eid, int)
    assert eid > 0


def test_log_event_metadata_roundtrips_via_get(db):
    payload = {"args": {"x": [1, 2]}, "duration_ms": 42}
    eid = db.log_event("tool_call_post", target="echo", metadata=payload)
    row = db.get_event(eid)
    assert row is not None
    assert row["event_type"] == "tool_call_post"
    assert row["metadata"] == payload


def test_log_event_handles_unserialisable_metadata(db):
    """Non-JSON-serialisable metadata falls back to ``str(...)`` so the
    event still lands."""
    class Weird:
        def __repr__(self) -> str:
            return "<Weird>"

    eid = db.log_event("tool_call_pre", target="x", metadata={"obj": Weird()})
    row = db.get_event(eid)
    # Metadata should still be stored (as JSON-encoded string fallback or
    # decoded structure with the str-coerced value).
    assert row is not None
    assert "Weird" in json.dumps(row["metadata"])


def test_log_event_default_timestamp_uses_now(db):
    before = time.time()
    eid = db.log_event("config_write", target="config.yaml")
    after = time.time()
    row = db.get_event(eid)
    assert before - 1 <= row["timestamp"] <= after + 1


def test_log_event_explicit_timestamp_honoured(db):
    pinned = 1700000000.0
    eid = db.log_event("memory_store", timestamp=pinned)
    row = db.get_event(eid)
    assert row["timestamp"] == pinned


def test_get_event_unknown_id_returns_none(db):
    assert db.get_event(99999) is None


# ─── query_events filters ────────────────────────────────────────────────


def test_query_events_filters_by_event_type(db):
    db.log_event("tool_call_pre", target="echo")
    db.log_event("tool_call_post", target="echo")
    db.log_event("tool_call_pre", target="terminal")

    pre = db.query_events(event_type="tool_call_pre")
    assert len(pre) == 2
    assert all(r["event_type"] == "tool_call_pre" for r in pre)

    post = db.query_events(event_type="tool_call_post")
    assert len(post) == 1


def test_query_events_filters_by_session(db):
    db.log_event("tool_call_pre", session_id="A", target="echo")
    db.log_event("tool_call_pre", session_id="B", target="echo")
    rows = db.query_events(session_id="A")
    assert len(rows) == 1
    assert rows[0]["session_id"] == "A"


def test_query_events_filters_by_target_glob(db):
    db.log_event("tool_call_pre", target="tools/echo_tool.py")
    db.log_event("tool_call_pre", target="tools/registry.py")
    db.log_event("tool_call_pre", target="agent/loop.py")

    rows = db.query_events(target_glob="tools/%")
    assert len(rows) == 2
    assert all(r["target"].startswith("tools/") for r in rows)


def test_query_events_since_until(db):
    db.log_event("x", target="early", timestamp=100.0)
    db.log_event("x", target="middle", timestamp=200.0)
    db.log_event("x", target="late", timestamp=300.0)

    rows = db.query_events(since=150.0, until=250.0)
    targets = [r["target"] for r in rows]
    assert targets == ["middle"]


def test_query_events_limit_offset_orders_newest_first(db):
    for i in range(5):
        db.log_event("x", target=f"t{i}", timestamp=100.0 + i)

    page1 = db.query_events(limit=2)
    assert [r["target"] for r in page1] == ["t4", "t3"]

    page2 = db.query_events(limit=2, offset=2)
    assert [r["target"] for r in page2] == ["t2", "t1"]


# ─── event_count ─────────────────────────────────────────────────────────


def test_event_count_total_and_filtered(db):
    db.log_event("tool_call_pre", target="echo")
    db.log_event("tool_call_pre", target="echo")
    db.log_event("guardrail_verdict", target="terminal")

    assert db.event_count() == 3
    assert db.event_count(event_type="tool_call_pre") == 2
    assert db.event_count(event_type="missing") == 0


# ─── memory_store hook (auto-fires inside SessionDB.store_memory) ────────


def test_store_memory_emits_memory_store_event(db):
    mem_id = db.store_memory(
        "preference", "user prefers terse answers",
        scope="global", source_session_id="sess-1",
    )
    rows = db.query_events(event_type="memory_store")
    assert len(rows) == 1
    row = rows[0]
    assert row["target"] == f"memory:{mem_id}"
    assert row["session_id"] == "sess-1"
    assert row["content_hash"]  # SHA256 hex, non-empty
    assert row["metadata"]["memory_id"] == mem_id
    assert row["metadata"]["scope"] == "global"
    assert row["metadata"]["pinned"] is False


def test_memory_store_content_hash_is_deterministic(db):
    db.store_memory("note", "same content")
    db.store_memory("note", "same content")
    rows = db.query_events(event_type="memory_store")
    assert len(rows) == 2
    assert rows[0]["content_hash"] == rows[1]["content_hash"]


# ─── checkpoint_create / rollback hooks ──────────────────────────────────


def test_checkpoint_create_emits_event_when_bound(db, fresh_home, tmp_path):
    cwd_dir = tmp_path / "work"
    cwd_dir.mkdir()
    mgr = CheckpointManager(
        root=fresh_home / "checkpoints",
        cwd=cwd_dir,
        home=fresh_home,
        session_db=db,
        session_id="sess-ck",
        agent_id="sess-ck#d0",
    )
    ckpt = mgr.create(name="alpha", triggered_by="manual")

    rows = db.query_events(event_type="checkpoint_create")
    assert len(rows) == 1
    assert rows[0]["target"] == ckpt.id
    assert rows[0]["session_id"] == "sess-ck"
    assert rows[0]["metadata"]["name"] == "alpha"
    assert rows[0]["metadata"]["triggered_by"] == "manual"


def test_rollback_emits_event(db, fresh_home, tmp_path):
    cwd_dir = tmp_path / "work"
    cwd_dir.mkdir()
    mgr = CheckpointManager(
        root=fresh_home / "checkpoints",
        cwd=cwd_dir,
        home=fresh_home,
        session_db=db,
        session_id="sess-rb",
    )
    ckpt = mgr.create(triggered_by="manual")
    mgr.rollback(ckpt.id)

    rows = db.query_events(event_type="rollback")
    assert len(rows) == 1
    assert rows[0]["target"] == ckpt.id


def test_checkpoint_no_db_binding_is_silent(fresh_home, tmp_path):
    """No session_db bound → checkpoint succeeds, no events written."""
    cwd_dir = tmp_path / "work"
    cwd_dir.mkdir()
    mgr = CheckpointManager(
        root=fresh_home / "checkpoints",
        cwd=cwd_dir,
        home=fresh_home,
    )
    ckpt = mgr.create()
    assert ckpt.id  # operation worked without complaint


# ─── tool_call_pre / tool_call_post hooks via AIAgent ────────────────────


class _StubRegistry:
    """Minimal tool registry — supplies dispatch + get_definitions so
    AIAgent._dispatch_tool_call exercises its full audit path."""

    def __init__(self, *, raises=False):
        self._raises = raises
        self.calls = []

    def get_definitions(self, names, quiet=False):
        return []

    def dispatch(self, tool_name, arguments, **_kwargs):
        self.calls.append((tool_name, arguments))
        if self._raises:
            raise RuntimeError("boom")
        return f"ran {tool_name}"


def _make_agent(db, *, registry):
    """AIAgent wired to a stub registry + real SessionDB.  The agent
    constructor demands a base_url + model but does no network I/O until
    run_conversation, so we can exercise the dispatch path in isolation.
    """
    from run_agent import AIAgent
    agent = AIAgent(
        base_url="https://example.invalid/v1",
        model="stub-model",
        session_id="sess-agent",
        session_db=db,
    )
    agent._tool_registry = registry
    return agent


def test_tool_call_pre_post_events_flow_through_dispatch(db):
    reg = _StubRegistry()
    agent = _make_agent(db, registry=reg)

    out = agent._dispatch_tool_call("echo", {"text": "hi"})
    assert out == "ran echo"

    pre = db.query_events(event_type="tool_call_pre")
    post = db.query_events(event_type="tool_call_post")
    assert len(pre) == 1 and len(post) == 1
    assert pre[0]["target"] == "echo"
    assert post[0]["target"] == "echo"
    assert post[0]["metadata"]["ok"] is True
    assert post[0]["metadata"]["duration_ms"] >= 0
    # Hashes link the pair through args.
    assert pre[0]["content_hash"]
    assert post[0]["content_hash"] != pre[0]["content_hash"]  # post hashes args+result


def test_tool_call_post_records_error_path(db):
    reg = _StubRegistry(raises=True)
    agent = _make_agent(db, registry=reg)

    out = agent._dispatch_tool_call("echo", {"x": 1})
    assert out.startswith("[error]")

    post = db.query_events(event_type="tool_call_post")
    assert len(post) == 1
    assert post[0]["metadata"]["ok"] is False
    assert post[0]["metadata"]["error_type"] == "RuntimeError"


def test_audit_agent_id_includes_delegation_depth(db):
    reg = _StubRegistry()
    agent = _make_agent(db, registry=reg)
    agent.delegation_depth = 2
    agent._dispatch_tool_call("echo", {})

    rows = db.query_events(event_type="tool_call_pre")
    assert rows[0]["agent_id"] == "sess-agent#d2"


def test_audit_event_swallows_session_db_failures(db, monkeypatch):
    """A broken log_event must NOT abort tool dispatch."""
    reg = _StubRegistry()
    agent = _make_agent(db, registry=reg)

    def boom(*_a, **_kw):
        raise RuntimeError("audit broken")

    monkeypatch.setattr(db, "log_event", boom)
    out = agent._dispatch_tool_call("echo", {"x": 1})
    assert out == "ran echo"


# ─── guardrail_verdict hook ──────────────────────────────────────────────


def test_guardrail_deny_writes_guardrail_verdict_event(db, monkeypatch):
    """When classify returns DENY, _guardrail_check should emit a
    guardrail_verdict row and the dispatcher should also emit a
    tool_call_pre marked as blocked."""
    reg = _StubRegistry()
    agent = _make_agent(db, registry=reg)

    from agent import tool_guardrails as tg

    def fake_classify(name, args, *, cwd=None, enable_self_mod=False):
        return tg.GuardrailDecision(
            verdict=tg.GuardrailVerdict.DENY,
            reason="rm -rf /",
            danger_class="rm-rf",
            affected_paths=[],
        )

    monkeypatch.setattr(tg, "classify_tool_call", fake_classify)

    out = agent._dispatch_tool_call("terminal", {"command": "rm -rf /"})
    assert out.startswith("[guardrail] DENY")
    # Tool was NOT actually invoked.
    assert reg.calls == []

    verdicts = db.query_events(event_type="guardrail_verdict")
    assert len(verdicts) == 1
    md = verdicts[0]["metadata"]
    assert md["verdict"] == "DENY"
    assert md["danger_class"] == "rm-rf"

    pre = db.query_events(event_type="tool_call_pre")
    assert len(pre) == 1
    assert pre[0]["metadata"]["blocked"] is True


# ─── CLI: phalanx audit log / count / show ───────────────────────────────


def _run_cli(argv, monkeypatch, fresh_home):
    """Drive ``hermes_cli.main.main(argv)`` in-process and capture stdout/err."""
    import hermes_cli.main as cli_main

    out_buf = io.StringIO()
    err_buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", out_buf)
    monkeypatch.setattr("sys.stderr", err_buf)
    monkeypatch.setenv("PHALANX_HOME", str(fresh_home))
    rc = cli_main.main(list(argv))
    return rc, out_buf.getvalue(), err_buf.getvalue()


def test_cli_audit_log_lists_events(fresh_home, monkeypatch):
    db = SessionDB(db_path=fresh_home / "state.db")
    db.log_event("tool_call_pre", session_id="s1", target="echo")
    db.log_event("tool_call_post", session_id="s1", target="echo")
    db.close()

    rc, out, _ = _run_cli(["audit", "log"], monkeypatch, fresh_home)
    assert rc == 0
    assert "echo" in out
    assert "tool_call_pre" in out
    assert "tool_call_post" in out


def test_cli_audit_log_filters_by_type(fresh_home, monkeypatch):
    db = SessionDB(db_path=fresh_home / "state.db")
    db.log_event("tool_call_pre", target="echo")
    db.log_event("guardrail_verdict", target="terminal")
    db.close()

    rc, out, _ = _run_cli(
        ["audit", "log", "--type", "guardrail_verdict"],
        monkeypatch, fresh_home,
    )
    assert rc == 0
    assert "guardrail_verdict" in out
    assert "tool_call_pre" not in out


def test_cli_audit_log_json_emits_valid_json(fresh_home, monkeypatch):
    db = SessionDB(db_path=fresh_home / "state.db")
    db.log_event("tool_call_pre", target="echo", metadata={"args": {"x": 1}})
    db.close()

    rc, out, _ = _run_cli(
        ["audit", "log", "--json"], monkeypatch, fresh_home,
    )
    assert rc == 0
    payload = json.loads(out)
    assert isinstance(payload, list)
    assert payload[0]["event_type"] == "tool_call_pre"
    assert payload[0]["metadata"]["args"] == {"x": 1}


def test_cli_audit_count(fresh_home, monkeypatch):
    db = SessionDB(db_path=fresh_home / "state.db")
    for _ in range(3):
        db.log_event("tool_call_pre", target="echo")
    db.log_event("guardrail_verdict", target="terminal")
    db.close()

    rc, out, _ = _run_cli(
        ["audit", "count", "--type", "tool_call_pre"],
        monkeypatch, fresh_home,
    )
    assert rc == 0
    assert out.strip() == "3"


def test_cli_audit_show_unknown_id_errors(fresh_home, monkeypatch):
    SessionDB(db_path=fresh_home / "state.db").close()
    rc, _out, err = _run_cli(
        ["audit", "show", "12345"], monkeypatch, fresh_home,
    )
    assert rc == 2
    assert "not found" in err


def test_cli_audit_show_renders_event(fresh_home, monkeypatch):
    db = SessionDB(db_path=fresh_home / "state.db")
    eid = db.log_event(
        "checkpoint_create", target="ckpt-x", metadata={"name": "alpha"},
    )
    db.close()
    rc, out, _ = _run_cli(
        ["audit", "show", str(eid), "--json"], monkeypatch, fresh_home,
    )
    assert rc == 0
    parsed = json.loads(out)
    assert parsed["event_type"] == "checkpoint_create"
    assert parsed["metadata"]["name"] == "alpha"


def test_cli_audit_log_since_relative(fresh_home, monkeypatch):
    db = SessionDB(db_path=fresh_home / "state.db")
    # Old event (1 hour ago) and a fresh one.
    db.log_event("x", target="old", timestamp=time.time() - 3600 * 2)
    db.log_event("x", target="recent")
    db.close()

    rc, out, _ = _run_cli(
        ["audit", "log", "--since", "30m"], monkeypatch, fresh_home,
    )
    assert rc == 0
    assert "recent" in out
    assert "old" not in out


# ─── REPL: /audit slash ──────────────────────────────────────────────────


def test_repl_audit_lists_session_events(db, capsys):
    db.log_event(
        "tool_call_pre", session_id="sess-cli",
        target="echo",
    )
    db.log_event(
        "tool_call_pre", session_id="other",
        target="terminal",
    )

    from cli import _cmd_audit

    class _StubAgent:
        session_id = "sess-cli"
        _session_db = db

    _cmd_audit("", {"agent": _StubAgent()})
    out = capsys.readouterr().out
    assert "tool_call_pre" in out
    assert "echo" in out
    assert "terminal" not in out


def test_repl_audit_show_all_ignores_session_filter(db, capsys):
    db.log_event(
        "tool_call_pre", session_id="sess-cli", target="echo",
    )
    db.log_event(
        "tool_call_pre", session_id="other", target="terminal",
    )

    from cli import _cmd_audit

    class _StubAgent:
        session_id = "sess-cli"
        _session_db = db

    _cmd_audit("show all", {"agent": _StubAgent()})
    out = capsys.readouterr().out
    assert "echo" in out
    assert "terminal" in out


def test_repl_audit_show_with_event_type(db, capsys):
    db.log_event("tool_call_pre", session_id="s", target="echo")
    db.log_event("guardrail_verdict", session_id="s", target="terminal")

    from cli import _cmd_audit

    class _StubAgent:
        session_id = "s"
        _session_db = db

    _cmd_audit("show guardrail_verdict", {"agent": _StubAgent()})
    out = capsys.readouterr().out
    assert "guardrail_verdict" in out
    assert "tool_call_pre" not in out


def test_repl_audit_handles_missing_db(capsys):
    from cli import _cmd_audit

    class _StubAgent:
        session_id = "s"
        _session_db = None

    _cmd_audit("", {"agent": _StubAgent()})
    out = capsys.readouterr().out
    assert "session DB unavailable" in out
