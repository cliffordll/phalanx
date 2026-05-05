"""Phase 1 / 2.2 minimal shim for tools.file_state.

Upstream's ``tools/file_state.py`` (~332 lines) implements a per-task
cross-tool registry that tracks read/write timestamps so the agent
can warn about stale reads, detect concurrent edits across delegated
sub-agents, and serialize writes to the same path.  The full
implementation arrives later (see ``MIGRATION_PLAN.md §2.7``); for
now ``tools/file_tools.py`` only needs the public surface to be
importable and harmless.

This shim keeps **every identifier** ``file_tools.py`` references,
all returning safe defaults (no tracking, no staleness, no locking).
When upstream lands the real module, drop this file and re-copy
upstream verbatim — no call sites need adjustment.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import List, Optional, Union

PathLike = Union[str, Path]


class FileStateRegistry:
    """Stand-in for the real per-task registry.  Public only for type hints."""

    def __init__(self) -> None:
        pass


_registry = FileStateRegistry()


def get_registry() -> FileStateRegistry:
    return _registry


def record_read(task_id: str, resolved_or_path: PathLike, *, partial: bool = False) -> None:
    """Phase-1 no-op.  Real impl tracks (task, path) → last_read_ts."""
    return None


def note_write(task_id: str, resolved_or_path: PathLike) -> None:
    """Phase-1 no-op.  Real impl tracks (task, path) → last_write_ts."""
    return None


def check_stale(task_id: str, resolved_or_path: PathLike) -> Optional[str]:
    """Phase-1 no-op.  Real impl returns a warning string when another
    agent wrote to ``resolved_or_path`` since this task last read it."""
    return None


@contextlib.contextmanager
def lock_path(resolved_or_path: PathLike):
    """Phase-1 no-op context manager.  Real impl serializes concurrent
    writers via a path-keyed lock table."""
    yield


def writes_since(task_id: str, *, since_ts: float = 0.0) -> List[str]:
    """Phase-1 returns empty list.  Real impl enumerates paths the
    given task has touched since *since_ts*."""
    return []


def known_reads(task_id: str) -> List[str]:
    """Phase-1 returns empty list.  Real impl enumerates paths the
    given task has read so far."""
    return []
