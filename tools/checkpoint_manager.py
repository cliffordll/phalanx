"""Checkpoint manager — three-piece state snapshot + rollback (§2.8.d wave 2).

Captures the union of state that an agent operation might mutate so a
single ``rollback`` restores everything in one shot:

1. **cwd working tree** via ``git stash create`` — SHA stored in
   checkpoint metadata, ``git stash apply`` on rollback.  When cwd
   isn't a git repo or has no changes, this piece is a no-op.
2. **~/.phalanx/state.db** via SQLite's online-safe ``Connection.backup()``
   API — the destination is a separate file copy under the checkpoint
   directory, no SAVEPOINT held open across the checkpoint lifespan
   (which would block other phalanx writers).
3. **~/.phalanx/ config files** (config.yaml + .env, excluding state.db
   which is backed up above) via tar.gz under the checkpoint dir.

Layout::

    ~/.phalanx/checkpoints/<id>/
      metadata.json       # serialised Checkpoint dataclass
      state.db            # binary copy
      config.tar.gz       # tar of config files (ex-state.db)

ID format: ``ckpt-<YYYY-MM-DDTHH-MM-SSZ>-<rand4>``.  Windows-safe (no
colons), time-sortable, plus a 4-char random suffix to avoid
same-second collisions when auto-checkpoint triggers fire rapidly.

Public surface:

* :class:`Checkpoint` — dataclass returned by all read methods.
* :class:`CheckpointManager` — owns ``~/.phalanx/checkpoints/`` and
  exposes ``create / list / get / rollback / delete``.

Wave 2 ships the manager + CLI/REPL surface (subcommands wired in
hermes_cli/main.py and cli.py).  Wave 3 layers audit-log writes onto
the same hooks.  Auto-checkpoint integration with the guardrail layer
(write_file / patch / dangerous terminal calls trigger an automatic
``create`` before dispatch) is a wave 4 concern; the manager itself
makes no assumption about who calls it.
"""

from __future__ import annotations

import json
import logging
import secrets
import shutil
import sqlite3
import subprocess
import tarfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)


# ─── Public types ─────────────────────────────────────────────────────


@dataclass
class Checkpoint:
    """One snapshot.  All fields are JSON-serialisable so a Checkpoint
    can be round-tripped via metadata.json without manual encoding."""

    id: str
    created_at: float
    cwd: str
    name: Optional[str] = None
    description: str = ""
    git_stash_sha: Optional[str] = None
    state_db_path: Optional[str] = None       # absolute path inside checkpoint dir
    config_tarball_path: Optional[str] = None
    triggered_by: str = "manual"              # "manual" / "auto" / "test" / ...
    extras: Dict[str, Any] = field(default_factory=dict)


# ─── Layout helpers ───────────────────────────────────────────────────


def _checkpoints_root() -> Path:
    """Default location: ``$PHALANX_HOME/checkpoints/``.

    Lazy resolution so monkeypatched ``PHALANX_HOME`` env (test
    fixtures) is honoured.
    """
    root = get_hermes_home() / "checkpoints"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _new_checkpoint_id() -> str:
    """``ckpt-<UTC ISO with - for :>-<6-hex>``.

    24 bits of entropy on the suffix so 50+ checkpoints created in
    the same second (auto-checkpoint storms) don't collide.  The
    second-resolution timestamp + 6 hex chars gives ~16 million
    unique IDs per second.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    return f"ckpt-{now}-{secrets.token_hex(3)}"


# ─── Per-piece snapshot helpers ───────────────────────────────────────


def _git_stash_create(cwd: Path) -> Optional[str]:
    """Run ``git stash create`` in *cwd*; return the SHA or None.

    ``git stash create`` is the read-only flavour: it builds the
    stash commit and prints the SHA but does NOT modify the working
    tree (no pop / no clean).  Empty when there are no changes (no
    output, returncode 0).  Failures (not a repo, git missing) →
    None, never raise — checkpoint creation must never be aborted by
    a git problem.
    """
    try:
        proc = subprocess.run(
            ["git", "stash", "create"],
            cwd=str(cwd),
            capture_output=True, text=True, timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("git stash create failed: %s", exc)
        return None
    if proc.returncode != 0:
        # Not a repo / detached / corrupt — log at debug, treat as no
        # snapshot.
        logger.debug(
            "git stash create returncode=%d stderr=%s",
            proc.returncode, (proc.stderr or "")[:200],
        )
        return None
    sha = (proc.stdout or "").strip()
    return sha or None


def _git_stash_apply(sha: str, cwd: Path) -> bool:
    """Apply a previously-created stash by SHA.  Returns True on
    success, False on any failure (caller decides whether to abort the
    rest of the rollback)."""
    try:
        proc = subprocess.run(
            ["git", "stash", "apply", sha],
            cwd=str(cwd),
            capture_output=True, text=True, timeout=60,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("git stash apply failed: %s", exc)
        return False
    if proc.returncode != 0:
        logger.warning(
            "git stash apply %s returncode=%d stderr=%s",
            sha[:12], proc.returncode, (proc.stderr or "")[:200],
        )
        return False
    return True


def _backup_state_db(src: Path, dst: Path) -> bool:
    """SQLite online backup from *src* to *dst*.

    Uses ``Connection.backup()`` which is the supported
    online-consistent backup primitive (no file locking, safe even
    with concurrent writers).  Returns True on success.
    """
    if not src.exists():
        # Fresh install — nothing to back up.  Caller treats absence of
        # a backup file as "nothing to restore" on rollback.
        return False
    try:
        src_conn = sqlite3.connect(str(src))
        try:
            dst_conn = sqlite3.connect(str(dst))
            try:
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
        finally:
            src_conn.close()
        return True
    except Exception as exc:
        logger.warning("state.db backup failed: %s", exc)
        # Best-effort cleanup of partial dst file.
        try:
            dst.unlink()
        except Exception:
            pass
        return False


def _restore_state_db(src: Path, dst: Path) -> bool:
    """Restore *src* (checkpoint copy) to *dst* (live state.db).

    Uses the same ``Connection.backup()`` call in reverse.  Doesn't
    touch the destination if the source doesn't exist (the checkpoint
    was created when state.db was missing).
    """
    if not src.exists():
        return True  # nothing to do
    try:
        src_conn = sqlite3.connect(str(src))
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst_conn = sqlite3.connect(str(dst))
            try:
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
        finally:
            src_conn.close()
        return True
    except Exception as exc:
        logger.warning("state.db restore failed: %s", exc)
        return False


# Files inside ~/.phalanx that go into the config tarball.  state.db is
# excluded (handled separately).  Subdirectories like sessions / eval /
# memory / logs / checkpoints are NOT included — they're append-only
# and rolling them back would lose unrelated data.
_CONFIG_TARBALL_FILES = (
    "config.yaml",
    ".env",
)


def _tar_config_files(home: Path, dst: Path) -> bool:
    """Tar ``config.yaml`` + ``.env`` from *home* into *dst*.

    Returns True iff at least one file was archived.  Files that
    don't exist are silently skipped — fresh installs commonly have
    no .env yet.
    """
    archived: List[Path] = []
    try:
        with tarfile.open(dst, "w:gz") as tf:
            for name in _CONFIG_TARBALL_FILES:
                src = home / name
                if not src.exists():
                    continue
                tf.add(src, arcname=name)
                archived.append(src)
    except Exception as exc:
        logger.warning("config tarball create failed: %s", exc)
        try:
            dst.unlink()
        except Exception:
            pass
        return False
    if not archived:
        # Empty archive — drop the file rather than leave a zero-byte
        # marker.
        try:
            dst.unlink()
        except Exception:
            pass
        return False
    return True


def _untar_config_files(src: Path, home: Path) -> bool:
    """Extract config tarball back to *home*.

    Always extracts directly under *home* (the archive's arcname is
    just the filename, so no path traversal risk — but we still
    validate each member's name to be safe).
    """
    if not src.exists():
        return True  # nothing to do
    try:
        with tarfile.open(src, "r:gz") as tf:
            for member in tf.getmembers():
                # Defence in depth: refuse anything with a path
                # separator in the arcname.  Legitimate entries are
                # just "config.yaml" / ".env".
                if "/" in member.name or "\\" in member.name or member.name.startswith(".."):
                    logger.warning(
                        "checkpoint tarball: refusing suspicious "
                        "member %r", member.name,
                    )
                    continue
                tf.extract(member, path=home)
        return True
    except Exception as exc:
        logger.warning("config tarball restore failed: %s", exc)
        return False


# ─── Manager ──────────────────────────────────────────────────────────


class CheckpointManager:
    """Owns ``$PHALANX_HOME/checkpoints/``; create / list / rollback.

    Construct without args for the production path; tests pass
    *root* to point at a tmp directory.  ``cwd`` defaults to the
    process's current working dir but tests can pin it.
    ``home`` defaults to ``get_hermes_home()`` for the same reason.
    """

    METADATA_FILENAME = "metadata.json"
    STATE_DB_FILENAME = "state.db"
    CONFIG_TARBALL_FILENAME = "config.tar.gz"

    def __init__(
        self,
        *,
        root: Optional[Path] = None,
        cwd: Optional[Path] = None,
        home: Optional[Path] = None,
    ) -> None:
        self._root = Path(root) if root else _checkpoints_root()
        self._root.mkdir(parents=True, exist_ok=True)
        self._cwd = Path(cwd) if cwd else Path.cwd()
        self._home = Path(home) if home else get_hermes_home()

    # ── Create ────────────────────────────────────────────────────────

    def create(
        self,
        *,
        name: Optional[str] = None,
        description: str = "",
        triggered_by: str = "manual",
        extras: Optional[Dict[str, Any]] = None,
    ) -> Checkpoint:
        """Snapshot all three pieces; return the Checkpoint."""
        ckpt_id = _new_checkpoint_id()
        ckpt_dir = self._root / ckpt_id
        ckpt_dir.mkdir(parents=True, exist_ok=False)

        # Piece 1: cwd working tree via git stash create.
        git_sha = _git_stash_create(self._cwd)

        # Piece 2: state.db online backup.
        state_src = self._home / "state.db"
        state_dst = ckpt_dir / self.STATE_DB_FILENAME
        state_backed_up = _backup_state_db(state_src, state_dst)

        # Piece 3: config tarball.
        tarball_dst = ckpt_dir / self.CONFIG_TARBALL_FILENAME
        config_archived = _tar_config_files(self._home, tarball_dst)

        ckpt = Checkpoint(
            id=ckpt_id,
            name=name,
            description=description,
            created_at=time.time(),
            cwd=str(self._cwd),
            git_stash_sha=git_sha,
            state_db_path=str(state_dst) if state_backed_up else None,
            config_tarball_path=str(tarball_dst) if config_archived else None,
            triggered_by=triggered_by,
            extras=dict(extras or {}),
        )

        # metadata.json
        try:
            (ckpt_dir / self.METADATA_FILENAME).write_text(
                json.dumps(asdict(ckpt), indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            # Metadata write is the one thing that *must* succeed —
            # without it the checkpoint isn't readable.  Cleanup and
            # raise.
            shutil.rmtree(ckpt_dir, ignore_errors=True)
            raise RuntimeError(f"checkpoint metadata write failed: {exc}") from exc

        logger.info(
            "checkpoint create id=%s git=%s state_db=%s config=%s",
            ckpt_id,
            "yes" if git_sha else "no",
            "yes" if state_backed_up else "no",
            "yes" if config_archived else "no",
        )
        return ckpt

    # ── Read ──────────────────────────────────────────────────────────

    def get(self, id_or_name: str) -> Optional[Checkpoint]:
        """Resolve by exact ID or by user-provided name; newest match
        wins on name collisions."""
        # Try exact ID first.
        candidate = self._root / id_or_name
        if candidate.is_dir():
            return self._read_metadata(candidate)
        # Fall through to name search across all dirs.
        matches: List[Checkpoint] = []
        for ckpt_dir in self._root.iterdir():
            if not ckpt_dir.is_dir():
                continue
            md = self._read_metadata(ckpt_dir)
            if md is None:
                continue
            if md.name == id_or_name:
                matches.append(md)
        if not matches:
            return None
        matches.sort(key=lambda c: -c.created_at)
        return matches[0]

    def list(self, *, limit: int = 50) -> List[Checkpoint]:
        """Newest-first."""
        results: List[Checkpoint] = []
        for ckpt_dir in self._root.iterdir():
            if not ckpt_dir.is_dir():
                continue
            md = self._read_metadata(ckpt_dir)
            if md is not None:
                results.append(md)
        results.sort(key=lambda c: -c.created_at)
        return results[:limit]

    def _read_metadata(self, ckpt_dir: Path) -> Optional[Checkpoint]:
        path = ckpt_dir / self.METADATA_FILENAME
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.debug("checkpoint %s metadata unreadable: %s", ckpt_dir.name, exc)
            return None
        try:
            return Checkpoint(**data)
        except TypeError as exc:
            logger.debug("checkpoint %s metadata schema mismatch: %s", ckpt_dir.name, exc)
            return None

    # ── Rollback ──────────────────────────────────────────────────────

    def rollback(self, id_or_name: str) -> Checkpoint:
        """Restore state from a previously-created checkpoint.

        Order of operations:
          1. git stash apply (if a SHA is stored)
          2. SQLite restore for state.db
          3. config tarball extract

        Each piece is best-effort: a failure in one step logs a warning
        but doesn't abort the others.  The Checkpoint is returned so
        callers can inspect what was applied.

        Raises ``KeyError`` if the checkpoint can't be resolved.
        """
        ckpt = self.get(id_or_name)
        if ckpt is None:
            raise KeyError(f"checkpoint not found: {id_or_name!r}")

        if ckpt.git_stash_sha:
            ok = _git_stash_apply(ckpt.git_stash_sha, Path(ckpt.cwd))
            logger.info(
                "rollback %s: git stash apply %s",
                ckpt.id, "ok" if ok else "FAIL",
            )

        if ckpt.state_db_path:
            ok = _restore_state_db(
                Path(ckpt.state_db_path),
                self._home / "state.db",
            )
            logger.info(
                "rollback %s: state.db restore %s",
                ckpt.id, "ok" if ok else "FAIL",
            )

        if ckpt.config_tarball_path:
            ok = _untar_config_files(
                Path(ckpt.config_tarball_path), self._home,
            )
            logger.info(
                "rollback %s: config restore %s",
                ckpt.id, "ok" if ok else "FAIL",
            )

        return ckpt

    # ── Delete ────────────────────────────────────────────────────────

    def delete(self, id_or_name: str) -> bool:
        """Remove a checkpoint directory.  Returns True iff something
        was deleted.  The git stash entry (if any) is left in place —
        ``git stash drop`` is a separate user concern; we don't touch
        the user's stash list."""
        ckpt = self.get(id_or_name)
        if ckpt is None:
            return False
        ckpt_dir = self._root / ckpt.id
        try:
            shutil.rmtree(ckpt_dir, ignore_errors=False)
        except Exception as exc:
            logger.warning("checkpoint %s rmtree failed: %s", ckpt.id, exc)
            return False
        return True
