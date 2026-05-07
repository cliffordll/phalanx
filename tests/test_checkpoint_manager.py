"""§2.8.d wave 2 tests — checkpoint_manager + CLI + REPL slash.

Three layers under test:

* :class:`tools.checkpoint_manager.CheckpointManager` — pure
  functional tests against tmp directories.  No real ``$HOME``,
  no real cwd writes; all state goes through fixture-provided
  paths.
* CLI subcommands — drive
  ``hermes_cli.main.main`` in-process via the ``_run_cli`` helper.
* REPL slash commands — call ``_cmd_snapshot`` / ``_cmd_rollback`` /
  ``_cmd_checkpoints`` directly.

Git-stash paths are exercised against a real ``git init``-ed
fixture cwd so the integration is real, not mocked.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from tools.checkpoint_manager import (
    CheckpointManager,
    _new_checkpoint_id,
)


# ─── Helpers ─────────────────────────────────────────────────────────


def _git_init(path: Path) -> None:
    """Spin up a minimal git repo in *path* with one committed file
    so ``git stash create`` has something to compare against.

    Uses ``--initial-branch=main`` to avoid hostnames warnings on
    fresh git installs.
    """
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(path)],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@example.com"],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test"],
        check=True, capture_output=True, text=True,
    )
    (path / "tracked.txt").write_text("initial\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(path), "add", "tracked.txt"],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "init"],
        check=True, capture_output=True, text=True,
    )


def _seed_phalanx_home(home: Path) -> None:
    """Drop a config.yaml + .env + state.db into *home*."""
    (home / "config.yaml").write_text(
        "model:\n  default: test-model\n", encoding="utf-8",
    )
    (home / ".env").write_text("OPENAI_API_KEY=sk-test\n", encoding="utf-8")
    conn = sqlite3.connect(str(home / "state.db"))
    try:
        conn.execute("CREATE TABLE marker (val TEXT)")
        conn.execute("INSERT INTO marker VALUES ('original')")
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def fresh_home(tmp_path, monkeypatch):
    """Fresh PHALANX_HOME with seeded config + state.db."""
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    _seed_phalanx_home(tmp_path)
    return tmp_path


@pytest.fixture
def manager(fresh_home, tmp_path):
    """CheckpointManager pinned to a tmp checkpoints/ root + tmp cwd
    that is *not* a git repo (so git_stash_sha defaults to None
    unless tests explicitly enable it)."""
    cwd_dir = tmp_path / "work"
    cwd_dir.mkdir()
    return CheckpointManager(
        root=fresh_home / "checkpoints",
        cwd=cwd_dir,
        home=fresh_home,
    )


# ─── ID format ───────────────────────────────────────────────────────


def test_checkpoint_id_is_windows_safe():
    cid = _new_checkpoint_id()
    assert cid.startswith("ckpt-")
    # No colons (Windows-illegal in paths).
    assert ":" not in cid
    # Time-sortable prefix: T separator + Z suffix on the timestamp.
    assert "T" in cid
    assert "Z" in cid


def test_checkpoint_ids_are_unique_within_same_second():
    ids = {_new_checkpoint_id() for _ in range(50)}
    assert len(ids) == 50  # 4-hex random suffix prevents collision


# ─── Manager: create + read ──────────────────────────────────────────


def test_create_writes_metadata_and_state_db(manager, fresh_home):
    ckpt = manager.create(name="alpha", description="first snapshot")
    assert ckpt.id.startswith("ckpt-")
    assert ckpt.name == "alpha"

    ckpt_dir = fresh_home / "checkpoints" / ckpt.id
    assert (ckpt_dir / "metadata.json").exists()
    assert (ckpt_dir / "state.db").exists()  # state.db was seeded
    # Tarball present because config.yaml + .env were seeded.
    assert (ckpt_dir / "config.tar.gz").exists()

    # Round-trip through metadata.
    md = json.loads((ckpt_dir / "metadata.json").read_text())
    assert md["id"] == ckpt.id
    assert md["name"] == "alpha"


def test_create_handles_no_state_db(manager, fresh_home):
    """Fresh install with no state.db yet — checkpoint should still
    succeed, just without the state_db piece."""
    (fresh_home / "state.db").unlink()
    ckpt = manager.create()
    assert ckpt.state_db_path is None
    # config files were still seeded so the tarball exists.
    assert ckpt.config_tarball_path is not None


def test_create_handles_no_config_files(manager, fresh_home):
    """No config.yaml / .env → tarball not created."""
    (fresh_home / "config.yaml").unlink()
    (fresh_home / ".env").unlink()
    ckpt = manager.create()
    assert ckpt.config_tarball_path is None


def test_create_outside_git_repo_yields_no_stash(manager):
    """cwd that isn't a git repo → git_stash_sha is None, no error."""
    ckpt = manager.create()
    assert ckpt.git_stash_sha is None


def test_create_inside_git_repo_with_changes_creates_stash(
    fresh_home, tmp_path,
):
    """git init + dirty working tree → git_stash_sha populated."""
    cwd_dir = tmp_path / "git-work"
    cwd_dir.mkdir()
    _git_init(cwd_dir)
    # Dirty the tree.
    (cwd_dir / "tracked.txt").write_text("modified\n", encoding="utf-8")

    mgr = CheckpointManager(
        root=fresh_home / "checkpoints",
        cwd=cwd_dir,
        home=fresh_home,
    )
    ckpt = mgr.create()
    assert ckpt.git_stash_sha is not None
    assert len(ckpt.git_stash_sha) >= 7   # short SHA is 7 hex chars


def test_create_clean_git_repo_yields_no_stash(fresh_home, tmp_path):
    """git init + no changes → git stash create returns empty."""
    cwd_dir = tmp_path / "clean"
    cwd_dir.mkdir()
    _git_init(cwd_dir)
    mgr = CheckpointManager(
        root=fresh_home / "checkpoints",
        cwd=cwd_dir,
        home=fresh_home,
    )
    ckpt = mgr.create()
    assert ckpt.git_stash_sha is None


# ─── Manager: list / get ─────────────────────────────────────────────


def test_list_newest_first(manager):
    a = manager.create(name="a")
    b = manager.create(name="b")
    rows = manager.list()
    assert [r.id for r in rows[:2]] == [b.id, a.id]


def test_list_respects_limit(manager):
    for _ in range(5):
        manager.create()
    assert len(manager.list(limit=2)) == 2


def test_list_skips_dirs_without_metadata(manager, fresh_home):
    manager.create()
    (fresh_home / "checkpoints" / "garbage-dir").mkdir()
    rows = manager.list()
    assert len(rows) == 1


def test_list_tolerates_corrupt_metadata(manager, fresh_home):
    ckpt = manager.create()
    md_path = fresh_home / "checkpoints" / ckpt.id / "metadata.json"
    md_path.write_text("not json", encoding="utf-8")
    # Should not raise; corrupt entry just disappears from the list.
    rows = manager.list()
    assert all(r.id != ckpt.id for r in rows)


def test_get_by_id(manager):
    ckpt = manager.create()
    got = manager.get(ckpt.id)
    assert got is not None
    assert got.id == ckpt.id


def test_get_by_name(manager):
    a = manager.create(name="alpha")
    got = manager.get("alpha")
    assert got is not None
    assert got.id == a.id


def test_get_by_name_returns_newest_on_collision(manager):
    manager.create(name="dup")
    b = manager.create(name="dup")
    got = manager.get("dup")
    assert got.id == b.id  # newest wins


def test_get_unknown_returns_none(manager):
    assert manager.get("nope") is None


# ─── Manager: delete ─────────────────────────────────────────────────


def test_delete_removes_checkpoint_dir(manager, fresh_home):
    ckpt = manager.create()
    assert (fresh_home / "checkpoints" / ckpt.id).exists()
    assert manager.delete(ckpt.id) is True
    assert not (fresh_home / "checkpoints" / ckpt.id).exists()
    assert manager.get(ckpt.id) is None


def test_delete_unknown_returns_false(manager):
    assert manager.delete("nope") is False


# ─── Manager: rollback ───────────────────────────────────────────────


def test_rollback_restores_state_db(manager, fresh_home):
    """state.db backup + restore round-trip preserves rows."""
    ckpt = manager.create()
    # Mutate the live state.db.
    conn = sqlite3.connect(str(fresh_home / "state.db"))
    try:
        conn.execute("UPDATE marker SET val = 'changed'")
        conn.commit()
    finally:
        conn.close()
    # Rollback should restore "original".
    manager.rollback(ckpt.id)
    conn = sqlite3.connect(str(fresh_home / "state.db"))
    try:
        val = conn.execute("SELECT val FROM marker").fetchone()[0]
    finally:
        conn.close()
    assert val == "original"


def test_rollback_restores_config_files(manager, fresh_home):
    ckpt = manager.create()
    # Tamper with config files.
    (fresh_home / "config.yaml").write_text(
        "model:\n  default: TAMPERED\n", encoding="utf-8",
    )
    (fresh_home / ".env").write_text(
        "OPENAI_API_KEY=sk-tampered\n", encoding="utf-8",
    )
    manager.rollback(ckpt.id)
    assert "test-model" in (fresh_home / "config.yaml").read_text(encoding="utf-8")
    assert "sk-test" in (fresh_home / ".env").read_text(encoding="utf-8")


def test_rollback_unknown_raises(manager):
    with pytest.raises(KeyError):
        manager.rollback("nope")


def test_rollback_by_name_works(manager, fresh_home):
    manager.create(name="pre-experiment")
    (fresh_home / "config.yaml").write_text("tampered", encoding="utf-8")
    manager.rollback("pre-experiment")
    assert "test-model" in (fresh_home / "config.yaml").read_text(encoding="utf-8")


def test_rollback_partial_backup_doesnt_crash(manager, fresh_home):
    """Checkpoint created without state.db → rollback skips that
    piece without raising."""
    (fresh_home / "state.db").unlink()
    ckpt = manager.create()
    # Restore should be a no-op for state.db.
    manager.rollback(ckpt.id)
    # state.db remains absent.
    assert not (fresh_home / "state.db").exists()


# ─── Tarball safety ──────────────────────────────────────────────────


def test_tarball_only_packages_whitelisted_files(manager, fresh_home):
    """state.db must NOT be inside the config tarball — it's backed up
    separately and including it would double-store + introduce a
    subtle inconsistency on restore."""
    import tarfile

    ckpt = manager.create()
    with tarfile.open(ckpt.config_tarball_path, "r:gz") as tf:
        names = [m.name for m in tf.getmembers()]
    assert set(names) <= {"config.yaml", ".env"}
    assert "state.db" not in names


# ─── CLI integration ─────────────────────────────────────────────────


def _run_cli(monkeypatch, capsys, argv):
    monkeypatch.setattr("sys.argv", ["phalanx", *argv])
    from hermes_cli.main import main
    rc = main()
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def test_cli_checkpoint_create_and_list(fresh_home, monkeypatch, capsys):
    monkeypatch.chdir(fresh_home)
    rc, out, err = _run_cli(
        monkeypatch, capsys,
        ["checkpoint", "create", "--name", "cli-test"],
    )
    assert rc == 0, err
    assert "created checkpoint" in out
    assert "cli-test" in out

    rc, out, err = _run_cli(monkeypatch, capsys, ["checkpoint", "list"])
    assert rc == 0
    assert "cli-test" in out


def test_cli_checkpoint_show_json(fresh_home, monkeypatch, capsys):
    monkeypatch.chdir(fresh_home)
    _run_cli(
        monkeypatch, capsys,
        ["checkpoint", "create", "--name", "show-test"],
    )
    capsys.readouterr()  # drain
    rc, out, err = _run_cli(
        monkeypatch, capsys, ["checkpoint", "show", "show-test"],
    )
    assert rc == 0, err
    parsed = json.loads(out)
    assert parsed["name"] == "show-test"


def test_cli_rollback_requires_yes(fresh_home, monkeypatch, capsys):
    monkeypatch.chdir(fresh_home)
    _run_cli(monkeypatch, capsys, ["checkpoint", "create", "--name", "rb"])
    capsys.readouterr()
    rc, out, err = _run_cli(
        monkeypatch, capsys, ["checkpoint", "rollback", "rb"],
    )
    assert rc == 1
    assert "--yes" in err


def test_cli_rollback_with_yes(fresh_home, monkeypatch, capsys):
    monkeypatch.chdir(fresh_home)
    _run_cli(monkeypatch, capsys, ["checkpoint", "create", "--name", "rb"])
    capsys.readouterr()
    # Tamper with config so we can verify rollback worked.
    (fresh_home / "config.yaml").write_text("tampered", encoding="utf-8")
    rc, out, err = _run_cli(
        monkeypatch, capsys, ["checkpoint", "rollback", "rb", "--yes"],
    )
    assert rc == 0, err
    assert "rolled back" in out
    assert "test-model" in (fresh_home / "config.yaml").read_text(encoding="utf-8")


def test_cli_show_unknown_returns_2(fresh_home, monkeypatch, capsys):
    monkeypatch.chdir(fresh_home)
    rc, out, err = _run_cli(
        monkeypatch, capsys, ["checkpoint", "show", "nope"],
    )
    assert rc == 2
    assert "not found" in err


def test_cli_delete_requires_yes(fresh_home, monkeypatch, capsys):
    monkeypatch.chdir(fresh_home)
    _run_cli(monkeypatch, capsys, ["checkpoint", "create"])
    capsys.readouterr()
    rc, out, err = _run_cli(
        monkeypatch, capsys, ["checkpoint", "delete", "any-id"],
    )
    assert rc == 1
    assert "--yes" in err


# ─── REPL slash ──────────────────────────────────────────────────────


def test_repl_snapshot_and_checkpoints(fresh_home, monkeypatch, capsys):
    monkeypatch.chdir(fresh_home)
    from cli import _cmd_snapshot, _cmd_checkpoints
    _cmd_snapshot("repl-test", state={})
    out = capsys.readouterr().out
    assert "checkpoint" in out
    assert "repl-test" in out

    _cmd_checkpoints("", state={})
    out = capsys.readouterr().out
    assert "(repl-test)" in out


def test_repl_rollback_no_args_lists(fresh_home, monkeypatch, capsys):
    monkeypatch.chdir(fresh_home)
    from cli import _cmd_snapshot, _cmd_rollback
    _cmd_snapshot("foo", state={})
    capsys.readouterr()
    _cmd_rollback("", state={})
    out = capsys.readouterr().out
    assert "Recent checkpoints" in out
    assert "(foo)" in out


def test_repl_rollback_unknown(fresh_home, monkeypatch, capsys):
    monkeypatch.chdir(fresh_home)
    from cli import _cmd_rollback
    _cmd_rollback("nope-not-real", state={})
    out = capsys.readouterr().out
    assert "not found" in out


def test_repl_rollback_restores(fresh_home, monkeypatch, capsys):
    monkeypatch.chdir(fresh_home)
    from cli import _cmd_snapshot, _cmd_rollback
    _cmd_snapshot("before", state={})
    capsys.readouterr()
    (fresh_home / "config.yaml").write_text("tampered", encoding="utf-8")
    _cmd_rollback("before", state={})
    out = capsys.readouterr().out
    assert "rolled back" in out
    assert "test-model" in (fresh_home / "config.yaml").read_text(encoding="utf-8")
