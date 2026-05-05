#!/usr/bin/env python3
"""SQLite session store for phalanx.

Ported from hermes-agent's ``hermes_state.py`` (~2248 lines) per
docs/phase-2.5-sessions.md §3.1.  Wave 1 covers the schema + connection
layer + the nine core CRUD methods plus content encoding helpers; later
waves add resume helpers (§3.2) and rich list/show queries (§3.3).

Design highlights kept verbatim from upstream:

* WAL mode + ``BEGIN IMMEDIATE`` + jitter retry — multi-writer (gateway,
  CLI, worktree agents) contention surfaces immediately and is broken by
  random 20-150 ms backoff instead of SQLite's deterministic convoy.
* Declarative column reconciliation — adding a column to ``SCHEMA_SQL``
  is enough; ``_reconcile_columns`` ALTERs live tables on next startup.
* FTS5 (unicode61) plus a trigram FTS5 table for CJK / substring search.
* Multimodal content uses a NUL-prefixed JSON sentinel so sqlite3's bind
  layer (which only accepts scalars) never sees a ``list``/``dict``.

Phalanx defaults to ``~/.hermes/state.db`` so a coexisting hermes-agent
install can read the same database — see the design doc §1.1 for why.
"""

from __future__ import annotations

import json
import logging
import random
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TypeVar

from agent.memory_manager import sanitize_context
from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

T = TypeVar("T")

DEFAULT_DB_PATH = get_hermes_home() / "state.db"

SCHEMA_VERSION = 11

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
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
    title TEXT,
    api_call_count INTEGER DEFAULT 0,
    FOREIGN KEY (parent_session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,
    content TEXT,
    tool_call_id TEXT,
    tool_calls TEXT,
    tool_name TEXT,
    timestamp REAL NOT NULL,
    token_count INTEGER,
    finish_reason TEXT,
    reasoning TEXT,
    reasoning_content TEXT,
    reasoning_details TEXT,
    codex_reasoning_items TEXT,
    codex_message_items TEXT
);

CREATE TABLE IF NOT EXISTS state_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_source ON sessions(source);
CREATE INDEX IF NOT EXISTS idx_sessions_parent ON sessions(parent_session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, timestamp);
"""

FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content
);

CREATE TRIGGER IF NOT EXISTS messages_fts_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (
        new.id,
        COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, '') || ' ' || COALESCE(new.tool_calls, '')
    );
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_delete AFTER DELETE ON messages BEGIN
    DELETE FROM messages_fts WHERE rowid = old.id;
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_update AFTER UPDATE ON messages BEGIN
    DELETE FROM messages_fts WHERE rowid = old.id;
    INSERT INTO messages_fts(rowid, content) VALUES (
        new.id,
        COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, '') || ' ' || COALESCE(new.tool_calls, '')
    );
END;
"""

FTS_TRIGRAM_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts_trigram USING fts5(
    content,
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS messages_fts_trigram_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts_trigram(rowid, content) VALUES (
        new.id,
        COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, '') || ' ' || COALESCE(new.tool_calls, '')
    );
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_trigram_delete AFTER DELETE ON messages BEGIN
    DELETE FROM messages_fts_trigram WHERE rowid = old.id;
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_trigram_update AFTER UPDATE ON messages BEGIN
    DELETE FROM messages_fts_trigram WHERE rowid = old.id;
    INSERT INTO messages_fts_trigram(rowid, content) VALUES (
        new.id,
        COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, '') || ' ' || COALESCE(new.tool_calls, '')
    );
END;
"""


class SessionDB:
    """SQLite-backed session storage with FTS5 search.

    Thread-safe for the gateway pattern (multiple reader threads, single
    writer via WAL mode).  Each method opens its own cursor; writes go
    through ``_execute_write`` which serializes them under ``self._lock``
    and retries on transient lock contention.
    """

    # ── Write-contention tuning ──
    # With multiple phalanx processes (gateway + CLI sessions + worktree
    # agents) all sharing one state.db, WAL write-lock contention causes
    # visible TUI freezes.  SQLite's built-in busy handler uses a
    # deterministic sleep schedule that causes convoy effects under high
    # concurrency.
    #
    # Instead, we keep the SQLite timeout short (1s) and handle retries
    # at the application level with random jitter, which staggers
    # competing writers and avoids the convoy.
    _WRITE_MAX_RETRIES = 15
    _WRITE_RETRY_MIN_S = 0.020   # 20 ms
    _WRITE_RETRY_MAX_S = 0.150   # 150 ms
    # Attempt a PASSIVE WAL checkpoint every N successful writes.
    _CHECKPOINT_EVERY_N_WRITES = 50

    def __init__(self, db_path: Optional[Path] = None):
        # Resolve the default lazily so PHALANX_HOME overrides set after
        # this module's import (e.g. by pytest fixtures) still take
        # effect.  ``DEFAULT_DB_PATH`` is kept as a module-level
        # snapshot for callers who want to inspect / monkeypatch it.
        if db_path is None:
            db_path = get_hermes_home() / "state.db"
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()
        self._write_count = 0
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            # Short timeout — application-level retry with random jitter
            # handles contention instead of sitting in SQLite's internal
            # busy handler for up to 30 s.
            timeout=1.0,
            # Autocommit mode: Python's default isolation_level="" auto-
            # starts transactions on DML, which conflicts with our
            # explicit BEGIN IMMEDIATE.  None = we manage transactions
            # ourselves.
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")

        self._init_schema()

    # ── Core write helper ──

    def _execute_write(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Execute a write transaction with BEGIN IMMEDIATE and jitter retry.

        *fn* receives the connection and should perform INSERT/UPDATE/
        DELETE statements.  The caller must NOT call ``commit()`` —
        that's handled here after *fn* returns.

        BEGIN IMMEDIATE acquires the WAL write lock at transaction
        start (not at commit time), so lock contention surfaces
        immediately.  On ``database is locked``, we release the Python
        lock, sleep a random 20-150 ms, and retry — breaking the convoy
        pattern that SQLite's built-in deterministic backoff creates.

        Returns whatever *fn* returns.
        """
        last_err: Optional[Exception] = None
        for attempt in range(self._WRITE_MAX_RETRIES):
            try:
                with self._lock:
                    self._conn.execute("BEGIN IMMEDIATE")
                    try:
                        result = fn(self._conn)
                        self._conn.commit()
                    except BaseException:
                        try:
                            self._conn.rollback()
                        except Exception:
                            pass
                        raise
                # Success — periodic best-effort checkpoint.
                self._write_count += 1
                if self._write_count % self._CHECKPOINT_EVERY_N_WRITES == 0:
                    self._try_wal_checkpoint()
                return result
            except sqlite3.OperationalError as exc:
                err_msg = str(exc).lower()
                if "locked" in err_msg or "busy" in err_msg:
                    last_err = exc
                    if attempt < self._WRITE_MAX_RETRIES - 1:
                        jitter = random.uniform(
                            self._WRITE_RETRY_MIN_S,
                            self._WRITE_RETRY_MAX_S,
                        )
                        time.sleep(jitter)
                        continue
                raise
        raise last_err or sqlite3.OperationalError(
            "database is locked after max retries"
        )

    def _try_wal_checkpoint(self) -> None:
        """Best-effort PASSIVE WAL checkpoint.  Never blocks, never raises.

        Flushes committed WAL frames back into the main DB file for any
        frames no other connection currently needs.  Keeps the WAL from
        growing unbounded when many processes hold persistent
        connections.
        """
        try:
            with self._lock:
                result = self._conn.execute(
                    "PRAGMA wal_checkpoint(PASSIVE)"
                ).fetchone()
                if result and result[1] > 0:
                    logger.debug(
                        "WAL checkpoint: %d/%d pages checkpointed",
                        result[2], result[1],
                    )
        except Exception:
            pass

    def close(self) -> None:
        """Close the database connection.

        Attempts a PASSIVE WAL checkpoint first so that exiting
        processes help keep the WAL file from growing unbounded.
        """
        with self._lock:
            if self._conn:
                try:
                    self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                except Exception:
                    pass
                self._conn.close()
                self._conn = None  # type: ignore[assignment]

    # ── Schema reconciliation ──

    @staticmethod
    def _parse_schema_columns(schema_sql: str) -> Dict[str, Dict[str, str]]:
        """Extract expected columns per table from SCHEMA_SQL.

        Uses an in-memory SQLite database to parse the SQL — SQLite
        itself handles all syntax (DEFAULT expressions with commas,
        inline REFERENCES, CHECK constraints, etc.) so there are zero
        regex edge cases.  The in-memory DB is opened, the schema DDL
        is executed, and PRAGMA table_info extracts the column metadata.

        Adding a column to SCHEMA_SQL is all that's needed; the
        reconciliation loop picks it up automatically.
        """
        ref = sqlite3.connect(":memory:")
        try:
            ref.executescript(schema_sql)
            table_columns: Dict[str, Dict[str, str]] = {}
            for (tbl,) in ref.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall():
                cols: Dict[str, str] = {}
                for row in ref.execute(
                    f'PRAGMA table_info("{tbl}")'
                ).fetchall():
                    # row: (cid, name, type, notnull, dflt_value, pk)
                    col_name = row[1]
                    col_type = row[2] or ""
                    notnull = row[3]
                    default = row[4]
                    pk = row[5]
                    parts = [col_type] if col_type else []
                    if notnull and not pk:
                        parts.append("NOT NULL")
                    if default is not None:
                        parts.append(f"DEFAULT {default}")
                    cols[col_name] = " ".join(parts)
                table_columns[tbl] = cols
            return table_columns
        finally:
            ref.close()

    def _reconcile_columns(self, cursor: sqlite3.Cursor) -> None:
        """Ensure live tables have every column declared in SCHEMA_SQL.

        Follows the Beets / sqlite-utils pattern: the CREATE TABLE
        definition in SCHEMA_SQL is the single source of truth for the
        desired schema.  On every startup this method diffs the live
        columns (via PRAGMA table_info) against the declared columns,
        and ADDs any that are missing.

        Makes column additions a declarative operation — just add the
        column to SCHEMA_SQL and it appears on the next startup.
        Version-gated migration blocks are no longer needed for ADD
        COLUMN.
        """
        expected = self._parse_schema_columns(SCHEMA_SQL)
        for table_name, declared_cols in expected.items():
            try:
                rows = cursor.execute(
                    f'PRAGMA table_info("{table_name}")'
                ).fetchall()
            except sqlite3.OperationalError:
                continue
            live_cols = set()
            for row in rows:
                name = row[1] if isinstance(row, (tuple, list)) else row["name"]
                live_cols.add(name)

            for col_name, col_type in declared_cols.items():
                if col_name not in live_cols:
                    safe_name = col_name.replace('"', '""')
                    try:
                        cursor.execute(
                            f'ALTER TABLE "{table_name}" '
                            f'ADD COLUMN "{safe_name}" {col_type}'
                        )
                    except sqlite3.OperationalError as exc:
                        # Expected: "duplicate column name" from a race
                        # or re-run.  Log at DEBUG so it's still visible
                        # in agent.log if a real schema mistake leaks.
                        logger.debug(
                            "reconcile %s.%s: %s",
                            table_name, col_name, exc,
                        )

    def _init_schema(self) -> None:
        """Create tables and FTS if they don't exist, reconcile columns.

        Schema management follows the declarative reconciliation
        pattern (Beets, sqlite-utils): SCHEMA_SQL is the single source
        of truth.  On existing databases, _reconcile_columns() diffs
        live columns against SCHEMA_SQL and ADDs any missing ones.

        The schema_version table is retained for future data migrations
        (transforming existing rows) which cannot be handled
        declaratively.  Versions <11 carried FTS5 backfill chores; new
        phalanx databases start at the current version directly so the
        legacy v10/v11 branches only fire when a hermes-shared DB
        upgrades.
        """
        cursor = self._conn.cursor()

        cursor.executescript(SCHEMA_SQL)

        self._reconcile_columns(cursor)

        cursor.execute("SELECT version FROM schema_version LIMIT 1")
        row = cursor.fetchone()
        if row is None:
            cursor.execute(
                "INSERT INTO schema_version (version) VALUES (?)",
                (SCHEMA_VERSION,),
            )
        else:
            current_version = (
                row["version"] if isinstance(row, sqlite3.Row) else row[0]
            )
            if current_version < 10:
                # v10: trigram FTS5 table for CJK / substring search.
                # The virtual table + triggers are created
                # unconditionally below, but existing rows need a
                # one-time backfill into the FTS index.
                try:
                    cursor.execute("SELECT * FROM messages_fts_trigram LIMIT 0")
                    _fts_trigram_exists = True
                except sqlite3.OperationalError:
                    _fts_trigram_exists = False
                if not _fts_trigram_exists:
                    cursor.executescript(FTS_TRIGRAM_SQL)
                    cursor.execute(
                        "INSERT INTO messages_fts_trigram(rowid, content) "
                        "SELECT id, content FROM messages "
                        "WHERE content IS NOT NULL"
                    )
            if current_version < 11:
                # v11: re-index FTS5 tables to cover tool_name +
                # tool_calls and switch from external-content to
                # inline mode.  Existing DBs have old-schema FTS tables
                # and triggers that IF NOT EXISTS won't overwrite, so
                # we drop them explicitly and let the post-migration
                # existence checks (below) recreate them, then backfill
                # every message row.
                for _trig in (
                    "messages_fts_insert",
                    "messages_fts_delete",
                    "messages_fts_update",
                    "messages_fts_trigram_insert",
                    "messages_fts_trigram_delete",
                    "messages_fts_trigram_update",
                ):
                    try:
                        cursor.execute(f"DROP TRIGGER IF EXISTS {_trig}")
                    except sqlite3.OperationalError:
                        pass
                for _tbl in ("messages_fts", "messages_fts_trigram"):
                    try:
                        cursor.execute(f"DROP TABLE IF EXISTS {_tbl}")
                    except sqlite3.OperationalError:
                        pass
                cursor.executescript(FTS_SQL)
                cursor.executescript(FTS_TRIGRAM_SQL)
                cursor.execute(
                    "INSERT INTO messages_fts(rowid, content) "
                    "SELECT id, "
                    "COALESCE(content, '') || ' ' || "
                    "COALESCE(tool_name, '') || ' ' || "
                    "COALESCE(tool_calls, '') "
                    "FROM messages"
                )
                cursor.execute(
                    "INSERT INTO messages_fts_trigram(rowid, content) "
                    "SELECT id, "
                    "COALESCE(content, '') || ' ' || "
                    "COALESCE(tool_name, '') || ' ' || "
                    "COALESCE(tool_calls, '') "
                    "FROM messages"
                )
            if current_version < SCHEMA_VERSION:
                cursor.execute(
                    "UPDATE schema_version SET version = ?",
                    (SCHEMA_VERSION,),
                )

        # Unique title index — always ensure it exists.
        try:
            cursor.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "idx_sessions_title_unique "
                "ON sessions(title) WHERE title IS NOT NULL"
            )
        except sqlite3.OperationalError:
            pass

        # FTS5 setup — separate because CREATE VIRTUAL TABLE can't be
        # in executescript with IF NOT EXISTS reliably.
        try:
            cursor.execute("SELECT * FROM messages_fts LIMIT 0")
        except sqlite3.OperationalError:
            cursor.executescript(FTS_SQL)

        try:
            cursor.execute("SELECT * FROM messages_fts_trigram LIMIT 0")
        except sqlite3.OperationalError:
            cursor.executescript(FTS_TRIGRAM_SQL)

        self._conn.commit()

    # =====================================================================
    # Session lifecycle
    # =====================================================================

    def _insert_session_row(
        self,
        session_id: str,
        source: str,
        model: Optional[str] = None,
        model_config: Optional[Dict[str, Any]] = None,
        system_prompt: Optional[str] = None,
        user_id: Optional[str] = None,
        parent_session_id: Optional[str] = None,
    ) -> None:
        """Shared INSERT OR IGNORE for session rows."""
        def _do(conn: sqlite3.Connection) -> None:
            conn.execute(
                """INSERT OR IGNORE INTO sessions (id, source, user_id, model,
                   model_config, system_prompt, parent_session_id, started_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    source,
                    user_id,
                    model,
                    json.dumps(model_config) if model_config else None,
                    system_prompt,
                    parent_session_id,
                    time.time(),
                ),
            )
        self._execute_write(_do)

    def create_session(self, session_id: str, source: str, **kwargs: Any) -> str:
        """Create a new session record.  Returns the session_id."""
        self._insert_session_row(session_id, source, **kwargs)
        return session_id

    def end_session(self, session_id: str, end_reason: str) -> None:
        """Mark a session as ended.

        No-ops when the session is already ended.  The first end_reason
        wins: compression-split sessions must keep their
        ``end_reason='compression'`` record even if a later stale
        ``end_session()`` call (e.g. from a desynced CLI session_id
        after ``/resume`` or ``/branch``) targets them with a different
        reason.  Use ``reopen_session()`` first if you intentionally
        need to re-end a closed session with a new reason.
        """
        def _do(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE sessions SET ended_at = ?, end_reason = ? "
                "WHERE id = ? AND ended_at IS NULL",
                (time.time(), end_reason, session_id),
            )
        self._execute_write(_do)

    def reopen_session(self, session_id: str) -> None:
        """Clear ended_at / end_reason so a session can be resumed."""
        def _do(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE sessions SET ended_at = NULL, end_reason = NULL "
                "WHERE id = ?",
                (session_id,),
            )
        self._execute_write(_do)

    def ensure_session(
        self,
        session_id: str,
        source: str = "unknown",
        model: Optional[str] = None,
        **kwargs: Any,
    ) -> str:
        """Ensure a session row exists (INSERT OR IGNORE)."""
        self._insert_session_row(session_id, source, model=model, **kwargs)
        return session_id

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get a session by ID."""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            )
            row = cursor.fetchone()
        return dict(row) if row else None

    def update_system_prompt(self, session_id: str, system_prompt: str) -> None:
        """Store the full assembled system prompt snapshot."""
        def _do(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE sessions SET system_prompt = ? WHERE id = ?",
                (system_prompt, session_id),
            )
        self._execute_write(_do)

    def update_token_counts(
        self,
        session_id: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        model: Optional[str] = None,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        reasoning_tokens: int = 0,
        estimated_cost_usd: Optional[float] = None,
        actual_cost_usd: Optional[float] = None,
        cost_status: Optional[str] = None,
        cost_source: Optional[str] = None,
        pricing_version: Optional[str] = None,
        billing_provider: Optional[str] = None,
        billing_base_url: Optional[str] = None,
        billing_mode: Optional[str] = None,
        api_call_count: int = 0,
        absolute: bool = False,
    ) -> None:
        """Update token counters and backfill model if not already set.

        When *absolute* is False (default), values are **incremented** —
        use this for per-API-call deltas (CLI path).

        When *absolute* is True, values are **set directly** — use this
        when the caller already holds cumulative totals (gateway path,
        where the cached agent accumulates across messages).
        """
        if absolute:
            sql = """UPDATE sessions SET
                   input_tokens = ?,
                   output_tokens = ?,
                   cache_read_tokens = ?,
                   cache_write_tokens = ?,
                   reasoning_tokens = ?,
                   estimated_cost_usd = COALESCE(?, 0),
                   actual_cost_usd = CASE
                       WHEN ? IS NULL THEN actual_cost_usd
                       ELSE ?
                   END,
                   cost_status = COALESCE(?, cost_status),
                   cost_source = COALESCE(?, cost_source),
                   pricing_version = COALESCE(?, pricing_version),
                   billing_provider = COALESCE(billing_provider, ?),
                   billing_base_url = COALESCE(billing_base_url, ?),
                   billing_mode = COALESCE(billing_mode, ?),
                   model = COALESCE(model, ?),
                   api_call_count = ?
                   WHERE id = ?"""
        else:
            sql = """UPDATE sessions SET
                   input_tokens = input_tokens + ?,
                   output_tokens = output_tokens + ?,
                   cache_read_tokens = cache_read_tokens + ?,
                   cache_write_tokens = cache_write_tokens + ?,
                   reasoning_tokens = reasoning_tokens + ?,
                   estimated_cost_usd = COALESCE(estimated_cost_usd, 0)
                       + COALESCE(?, 0),
                   actual_cost_usd = CASE
                       WHEN ? IS NULL THEN actual_cost_usd
                       ELSE COALESCE(actual_cost_usd, 0) + ?
                   END,
                   cost_status = COALESCE(?, cost_status),
                   cost_source = COALESCE(?, cost_source),
                   pricing_version = COALESCE(?, pricing_version),
                   billing_provider = COALESCE(billing_provider, ?),
                   billing_base_url = COALESCE(billing_base_url, ?),
                   billing_mode = COALESCE(billing_mode, ?),
                   model = COALESCE(model, ?),
                   api_call_count = COALESCE(api_call_count, 0) + ?
                   WHERE id = ?"""
        params = (
            input_tokens,
            output_tokens,
            cache_read_tokens,
            cache_write_tokens,
            reasoning_tokens,
            estimated_cost_usd,
            actual_cost_usd,
            actual_cost_usd,
            cost_status,
            cost_source,
            pricing_version,
            billing_provider,
            billing_base_url,
            billing_mode,
            model,
            api_call_count,
            session_id,
        )

        def _do(conn: sqlite3.Connection) -> None:
            conn.execute(sql, params)
        self._execute_write(_do)

    # =====================================================================
    # Session listing / deletion (§2.5 wave 4)
    # =====================================================================

    def list_sessions_rich(
        self,
        source: Optional[str] = None,
        exclude_sources: Optional[List[str]] = None,
        limit: int = 20,
        offset: int = 0,
        include_children: bool = False,
    ) -> List[Dict[str, Any]]:
        """List sessions with preview + last_active, ordered by start time.

        Returns dicts with all ``sessions.*`` columns plus:

        * ``preview`` — first 60 chars of the first user message (with
          newlines flattened to spaces); empty string if the session
          has no user message yet.
        * ``last_active`` — most recent message timestamp, or
          ``started_at`` if the session has no messages.

        Phalanx omits the upstream ``order_by_last_active`` /
        ``project_compression_tips`` paths — both depend on
        compression-continuation chains that don't exist yet (§2.7
        territory).  Default sort is ``started_at DESC`` with LIMIT/
        OFFSET pagination.

        ``include_children=False`` (default) hides sessions whose
        parent was still alive when the child started — these are
        sub-agent runs / compression continuations / branches that
        a CLI user shouldn't see at the top level.  Branch children
        (``parent.end_reason='branched'`` and ``child.started_at >=
        parent.ended_at``) stay visible.
        """
        where_clauses: List[str] = []
        params: List[Any] = []

        if not include_children:
            where_clauses.append(
                "(s.parent_session_id IS NULL"
                " OR EXISTS (SELECT 1 FROM sessions p"
                "            WHERE p.id = s.parent_session_id"
                "            AND p.end_reason = 'branched'"
                "            AND s.started_at >= p.ended_at))"
            )
        if source:
            where_clauses.append("s.source = ?")
            params.append(source)
        if exclude_sources:
            placeholders = ",".join("?" for _ in exclude_sources)
            where_clauses.append(f"s.source NOT IN ({placeholders})")
            params.extend(exclude_sources)

        where_sql = (
            f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        )
        query = f"""
            SELECT s.*,
                COALESCE(
                    (SELECT SUBSTR(REPLACE(REPLACE(m.content, X'0A', ' '), X'0D', ' '), 1, 63)
                     FROM messages m
                     WHERE m.session_id = s.id AND m.role = 'user'
                       AND m.content IS NOT NULL
                     ORDER BY m.timestamp, m.id LIMIT 1),
                    ''
                ) AS _preview_raw,
                COALESCE(
                    (SELECT MAX(m2.timestamp) FROM messages m2
                     WHERE m2.session_id = s.id),
                    s.started_at
                ) AS last_active
            FROM sessions s
            {where_sql}
            ORDER BY s.started_at DESC
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])

        with self._lock:
            cursor = self._conn.execute(query, params)
            rows = cursor.fetchall()
        sessions: List[Dict[str, Any]] = []
        for row in rows:
            s = dict(row)
            raw = s.pop("_preview_raw", "").strip()
            if raw:
                text = raw[:60]
                s["preview"] = text + ("..." if len(raw) > 60 else "")
            else:
                s["preview"] = ""
            sessions.append(s)
        return sessions

    def _get_session_rich_row(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Fetch one session with the same enriched columns as ``list_sessions_rich``.

        Returns the row plus ``preview`` + ``last_active``, or ``None``
        when the session doesn't exist.
        """
        query = """
            SELECT s.*,
                COALESCE(
                    (SELECT SUBSTR(REPLACE(REPLACE(m.content, X'0A', ' '), X'0D', ' '), 1, 63)
                     FROM messages m
                     WHERE m.session_id = s.id AND m.role = 'user'
                       AND m.content IS NOT NULL
                     ORDER BY m.timestamp, m.id LIMIT 1),
                    ''
                ) AS _preview_raw,
                COALESCE(
                    (SELECT MAX(m2.timestamp) FROM messages m2
                     WHERE m2.session_id = s.id),
                    s.started_at
                ) AS last_active
            FROM sessions s
            WHERE s.id = ?
        """
        with self._lock:
            cursor = self._conn.execute(query, (session_id,))
            row = cursor.fetchone()
        if not row:
            return None
        s = dict(row)
        raw = s.pop("_preview_raw", "").strip()
        if raw:
            text = raw[:60]
            s["preview"] = text + ("..." if len(raw) > 60 else "")
        else:
            s["preview"] = ""
        return s

    def delete_session(self, session_id: str) -> bool:
        """Delete a session and all its messages.

        Child sessions are orphaned (``parent_session_id`` set to
        NULL) rather than cascade-deleted so that, e.g., a compression
        chain doesn't lose the live tip when an ancestor is pruned.
        Returns True iff the session existed and was deleted.
        """
        def _do(conn: sqlite3.Connection) -> bool:
            cursor = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE id = ?", (session_id,)
            )
            if cursor.fetchone()[0] == 0:
                return False
            conn.execute(
                "UPDATE sessions SET parent_session_id = NULL "
                "WHERE parent_session_id = ?",
                (session_id,),
            )
            conn.execute(
                "DELETE FROM messages WHERE session_id = ?", (session_id,)
            )
            conn.execute(
                "DELETE FROM sessions WHERE id = ?", (session_id,)
            )
            return True

        return self._execute_write(_do)

    # =====================================================================
    # Message storage
    # =====================================================================

    # Sentinel prefix used to distinguish JSON-encoded structured content
    # (multimodal messages: lists of parts like text + image_url) from
    # plain string content.  The NUL byte is not legal in normal text,
    # so this cannot collide with real user content.
    _CONTENT_JSON_PREFIX = "\x00json:"

    @classmethod
    def _encode_content(cls, content: Any) -> Any:
        """Serialize structured (list/dict) message content for sqlite.

        sqlite3 can only bind ``str``, ``bytes``, ``int``, ``float``,
        and ``None`` to query parameters.  Multimodal messages have
        ``content`` as a list of parts (``[{"type": "text", ...},
        {"type": "image_url", ...}]``), which raises
        ``ProgrammingError: Error binding parameter N: type 'list' is
        not supported`` when bound directly.

        Returns the value unchanged when it's already a safe scalar, or
        a sentinel-prefixed JSON string for lists / dicts.  Paired with
        :meth:`_decode_content` on read.
        """
        if content is None or isinstance(content, (str, bytes, int, float)):
            return content
        try:
            return cls._CONTENT_JSON_PREFIX + json.dumps(content)
        except (TypeError, ValueError):
            return str(content)

    @classmethod
    def _decode_content(cls, content: Any) -> Any:
        """Reverse :meth:`_encode_content`; returns scalars unchanged."""
        if isinstance(content, str) and content.startswith(cls._CONTENT_JSON_PREFIX):
            try:
                return json.loads(content[len(cls._CONTENT_JSON_PREFIX):])
            except (json.JSONDecodeError, TypeError):
                logger.warning(
                    "Failed to decode JSON-encoded message content; "
                    "returning raw string"
                )
                return content
        return content

    def append_message(
        self,
        session_id: str,
        role: str,
        content: Any = None,
        tool_name: Optional[str] = None,
        tool_calls: Any = None,
        tool_call_id: Optional[str] = None,
        token_count: Optional[int] = None,
        finish_reason: Optional[str] = None,
        reasoning: Optional[str] = None,
        reasoning_content: Optional[str] = None,
        reasoning_details: Any = None,
        codex_reasoning_items: Any = None,
        codex_message_items: Any = None,
    ) -> int:
        """Append a message to a session.  Returns the message row ID.

        Also increments the session's message_count (and tool_call_count
        if role is 'tool' or tool_calls is present).
        """
        # Serialize structured fields to JSON before entering the write
        # txn to keep the critical section short.
        reasoning_details_json = (
            json.dumps(reasoning_details) if reasoning_details else None
        )
        codex_items_json = (
            json.dumps(codex_reasoning_items)
            if codex_reasoning_items else None
        )
        codex_message_items_json = (
            json.dumps(codex_message_items)
            if codex_message_items else None
        )
        tool_calls_json = json.dumps(tool_calls) if tool_calls else None
        # Multimodal content (list of parts) must be JSON-encoded:
        # sqlite3 cannot bind list / dict parameters directly.
        stored_content = self._encode_content(content)

        num_tool_calls = 0
        if tool_calls is not None:
            num_tool_calls = (
                len(tool_calls) if isinstance(tool_calls, list) else 1
            )

        def _do(conn: sqlite3.Connection) -> int:
            cursor = conn.execute(
                """INSERT INTO messages (session_id, role, content,
                   tool_call_id, tool_calls, tool_name, timestamp,
                   token_count, finish_reason, reasoning,
                   reasoning_content, reasoning_details,
                   codex_reasoning_items, codex_message_items)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    role,
                    stored_content,
                    tool_call_id,
                    tool_calls_json,
                    tool_name,
                    time.time(),
                    token_count,
                    finish_reason,
                    reasoning,
                    reasoning_content,
                    reasoning_details_json,
                    codex_items_json,
                    codex_message_items_json,
                ),
            )
            msg_id = cursor.lastrowid

            if num_tool_calls > 0:
                conn.execute(
                    "UPDATE sessions SET "
                    "message_count = message_count + 1, "
                    "tool_call_count = tool_call_count + ? "
                    "WHERE id = ?",
                    (num_tool_calls, session_id),
                )
            else:
                conn.execute(
                    "UPDATE sessions SET "
                    "message_count = message_count + 1 "
                    "WHERE id = ?",
                    (session_id,),
                )
            return msg_id

        return self._execute_write(_do)

    def get_messages(self, session_id: str) -> List[Dict[str, Any]]:
        """Load all messages for a session, ordered by timestamp."""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM messages WHERE session_id = ? "
                "ORDER BY timestamp, id",
                (session_id,),
            )
            rows = cursor.fetchall()
        result: List[Dict[str, Any]] = []
        for row in rows:
            msg = dict(row)
            if "content" in msg:
                msg["content"] = self._decode_content(msg["content"])
            if msg.get("tool_calls"):
                try:
                    msg["tool_calls"] = json.loads(msg["tool_calls"])
                except (json.JSONDecodeError, TypeError):
                    logger.warning(
                        "Failed to deserialize tool_calls in get_messages, "
                        "falling back to []"
                    )
                    msg["tool_calls"] = []
            result.append(msg)
        return result

    # =====================================================================
    # Resume — id resolution + conversation replay (§2.5 wave 3)
    # =====================================================================

    def resolve_session_id(self, session_id_or_prefix: str) -> Optional[str]:
        """Resolve an exact or uniquely prefixed session ID to the full ID.

        Returns the exact ID when it exists.  Otherwise treats the input
        as a prefix and returns the single matching session ID if the
        prefix is unambiguous.  Returns None for no matches or
        ambiguous prefixes (>1 hit).
        """
        exact = self.get_session(session_id_or_prefix)
        if exact:
            return exact["id"]

        escaped = (
            session_id_or_prefix
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        with self._lock:
            cursor = self._conn.execute(
                "SELECT id FROM sessions WHERE id LIKE ? ESCAPE '\\' "
                "ORDER BY started_at DESC LIMIT 2",
                (f"{escaped}%",),
            )
            matches = [row["id"] for row in cursor.fetchall()]
        if len(matches) == 1:
            return matches[0]
        return None

    def resolve_resume_session_id(self, session_id: str) -> str:
        """Redirect a resume target to the descendant session that holds the messages.

        Context compression ends the current session and forks a new
        child session (linked via ``parent_session_id``).  The flush
        cursor is reset, so the child is where new messages actually
        land — the parent ends up with ``message_count = 0`` rows
        unless messages had already been flushed to it before
        compression.

        This helper walks ``parent_session_id`` forward from
        ``session_id`` and returns the first descendant in the chain
        that has at least one message row.  If the original session
        already has messages, or no descendant has any, the original
        ``session_id`` is returned unchanged.

        The chain is always walked via the child whose ``started_at``
        is latest; that matches the single-chain shape that
        compression creates.  A depth cap (32) guards against
        accidental loops in malformed data.
        """
        if not session_id:
            return session_id

        with self._lock:
            try:
                row = self._conn.execute(
                    "SELECT 1 FROM messages WHERE session_id = ? LIMIT 1",
                    (session_id,),
                ).fetchone()
            except Exception:
                return session_id
            if row is not None:
                return session_id

            current = session_id
            seen = {current}
            for _ in range(32):
                try:
                    child_row = self._conn.execute(
                        "SELECT id FROM sessions "
                        "WHERE parent_session_id = ? "
                        "ORDER BY started_at DESC, id DESC LIMIT 1",
                        (current,),
                    ).fetchone()
                except Exception:
                    return session_id
                if child_row is None:
                    return session_id
                child_id = (
                    child_row["id"] if hasattr(child_row, "keys")
                    else child_row[0]
                )
                if not child_id or child_id in seen:
                    return session_id
                seen.add(child_id)
                try:
                    msg_row = self._conn.execute(
                        "SELECT 1 FROM messages WHERE session_id = ? LIMIT 1",
                        (child_id,),
                    ).fetchone()
                except Exception:
                    return session_id
                if msg_row is not None:
                    return child_id
                current = child_id
        return session_id

    def get_messages_as_conversation(
        self,
        session_id: str,
        include_ancestors: bool = False,
    ) -> List[Dict[str, Any]]:
        """Load messages in the OpenAI conversation format.

        Returns a list of ``{"role": str, "content": Any, ...}`` dicts
        ready to seed ``run_conversation(conversation_history=...)``.

        With ``include_ancestors=True`` the lineage chain (root → tip
        via ``parent_session_id``) is walked and replayed in order;
        duplicate replayed user messages are dropped via
        ``_is_duplicate_replayed_user_message``.  Single-session resume
        leaves that path off (default).
        """
        session_ids = [session_id]
        if include_ancestors:
            session_ids = self._session_lineage_root_to_tip(session_id)

        with self._lock:
            placeholders = ",".join("?" for _ in session_ids)
            rows = self._conn.execute(
                "SELECT role, content, tool_call_id, tool_calls, tool_name, "
                "finish_reason, reasoning, reasoning_content, "
                "reasoning_details, codex_reasoning_items, "
                "codex_message_items "
                f"FROM messages WHERE session_id IN ({placeholders}) "
                "ORDER BY timestamp, id",
                tuple(session_ids),
            ).fetchall()

        messages: List[Dict[str, Any]] = []
        for row in rows:
            content = self._decode_content(row["content"])
            if row["role"] in {"user", "assistant"} and isinstance(content, str):
                content = sanitize_context(content).strip()
            msg: Dict[str, Any] = {"role": row["role"], "content": content}
            if row["tool_call_id"]:
                msg["tool_call_id"] = row["tool_call_id"]
            if row["tool_name"]:
                msg["tool_name"] = row["tool_name"]
            if row["tool_calls"]:
                try:
                    msg["tool_calls"] = json.loads(row["tool_calls"])
                except (json.JSONDecodeError, TypeError):
                    logger.warning(
                        "Failed to deserialize tool_calls in conversation "
                        "replay, falling back to []"
                    )
                    msg["tool_calls"] = []
            # Restore reasoning fields on assistant messages so providers
            # that replay reasoning (OpenRouter, OpenAI, Nous) receive
            # coherent multi-turn reasoning context.
            if row["role"] == "assistant":
                if row["finish_reason"]:
                    msg["finish_reason"] = row["finish_reason"]
                if row["reasoning"]:
                    msg["reasoning"] = row["reasoning"]
                if row["reasoning_content"] is not None:
                    msg["reasoning_content"] = row["reasoning_content"]
                if row["reasoning_details"]:
                    try:
                        msg["reasoning_details"] = json.loads(
                            row["reasoning_details"]
                        )
                    except (json.JSONDecodeError, TypeError):
                        logger.warning(
                            "Failed to deserialize reasoning_details, "
                            "falling back to None"
                        )
                        msg["reasoning_details"] = None
                if row["codex_reasoning_items"]:
                    try:
                        msg["codex_reasoning_items"] = json.loads(
                            row["codex_reasoning_items"]
                        )
                    except (json.JSONDecodeError, TypeError):
                        logger.warning(
                            "Failed to deserialize codex_reasoning_items, "
                            "falling back to None"
                        )
                        msg["codex_reasoning_items"] = None
                if row["codex_message_items"]:
                    try:
                        msg["codex_message_items"] = json.loads(
                            row["codex_message_items"]
                        )
                    except (json.JSONDecodeError, TypeError):
                        logger.warning(
                            "Failed to deserialize codex_message_items, "
                            "falling back to None"
                        )
                        msg["codex_message_items"] = None
            if include_ancestors and self._is_duplicate_replayed_user_message(
                messages, msg
            ):
                continue
            messages.append(msg)
        return messages

    def _session_lineage_root_to_tip(self, session_id: str) -> List[str]:
        """Walk parent_session_id chain back to the root and return root→tip order.

        Bounded at 100 hops to defend against malformed cycles.
        """
        if not session_id:
            return [session_id]

        chain: List[str] = []
        current = session_id
        seen: set = set()
        with self._lock:
            for _ in range(100):
                if not current or current in seen:
                    break
                seen.add(current)
                chain.append(current)
                row = self._conn.execute(
                    "SELECT parent_session_id FROM sessions WHERE id = ?",
                    (current,),
                ).fetchone()
                if row is None:
                    break
                current = (
                    row["parent_session_id"] if hasattr(row, "keys")
                    else row[0]
                )
        return list(reversed(chain)) or [session_id]

    @staticmethod
    def _is_duplicate_replayed_user_message(
        messages: List[Dict[str, Any]],
        msg: Dict[str, Any],
    ) -> bool:
        """Detect a user message replayed identically across a lineage seam.

        When concatenating the messages of a parent session and its
        child via lineage replay, the most recent user prompt sometimes
        appears in both — once as the last turn of the parent and once
        as the seed of the child.  Drop the second occurrence iff
        nothing new (assistant content / tool_calls) has been emitted
        since the matching prior user message.
        """
        if msg.get("role") != "user":
            return False
        content = msg.get("content")
        if not isinstance(content, str) or not content:
            return False
        for prev in reversed(messages):
            if prev.get("role") == "user" and prev.get("content") == content:
                return True
            if prev.get("role") == "assistant" and (
                prev.get("content") or prev.get("tool_calls")
            ):
                return False
        return False
