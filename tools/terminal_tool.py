"""Phase 2.2 Wave 1 minimal terminal_tool.

Upstream's ``tools/terminal_tool.py`` (~3000 lines) supports many
backends — local, docker, singularity, ssh, modal, daytona,
vercel_sandbox — plus an approval queue, persistent shell sessions,
container-aware cwd tracking, and a cleanup thread that kills idle
sandboxes.

This shim keeps **only** the public surface ``tools/file_tools.py``
and ``tools/file_operations.py`` import lazily, so Wave-1 file ops
work end-to-end against the local host.  When upstream lands the
real module, drop this file and re-copy upstream verbatim — no call
sites need adjustment.

Specifically provided:

* ``LocalTerminalEnv``  – ``execute(command, cwd=None, timeout=None,
  stdin_data=None, **_)`` returns ``{"output": str, "returncode": int}``.
  On Windows the executable is ``bash.exe`` when discoverable
  (Git-Bash / MSYS), otherwise the system shell.  ``file_operations``
  pipes ``sed``/``head``/``tail`` through this so a POSIX-flavoured
  shell is required for read/search to work.
* Module globals: ``_active_environments``, ``_env_lock``,
  ``_last_activity``, ``_creation_locks``, ``_creation_locks_lock``,
  ``_task_env_overrides``.
* Helpers: ``_resolve_container_task_id``, ``_get_env_config``,
  ``_create_environment``, ``_start_cleanup_thread``.
* Self-registers a ``terminal`` tool so the agent (and ``hermes
  tools run terminal``) can drive the same backend.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Any, Dict, Optional

# Windows CreateProcess command-line limit is 32767 chars; allow plenty of
# headroom for shell wrapping and switch to a tempfile-spilled invocation
# above this threshold so callers like tool_result_storage._write_to_sandbox
# (which embeds whole tool outputs in heredocs) work on Windows too.
_LONG_CMD_THRESHOLD = 16 * 1024

from tools.registry import registry, tool_result

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level state expected by file_tools / file_operations.
# ---------------------------------------------------------------------------

_active_environments: Dict[str, "LocalTerminalEnv"] = {}
_env_lock = threading.Lock()
_last_activity: Dict[str, float] = {}
_creation_locks: Dict[str, threading.Lock] = {}
_creation_locks_lock = threading.Lock()
_task_env_overrides: Dict[str, Dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# Backend selection.  file_operations builds POSIX pipelines (sed/head/tail),
# so on Windows we route through bash.exe when present.
# ---------------------------------------------------------------------------

def _find_bash() -> Optional[str]:
    """Return a bash executable path or None if not found."""
    candidate = shutil.which("bash")
    if candidate:
        return candidate
    if os.name == "nt":
        for guess in (
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files\Git\usr\bin\bash.exe",
            r"C:\msys64\usr\bin\bash.exe",
        ):
            if os.path.exists(guess):
                return guess
    return None


_BASH_PATH = _find_bash()


class LocalTerminalEnv:
    """Minimal local-shell backend used by ShellFileOperations.

    The real upstream implementation tracks a persistent shell process
    and forwards ``cd`` between tool calls; this Phase-2.2 stand-in
    spawns a fresh subprocess per ``execute`` call which is enough for
    Wave-1 file ops but does NOT preserve cd / env between calls.
    """

    def __init__(self, cwd: Optional[str] = None, timeout: int = 120):
        self.cwd = cwd or os.getcwd()
        self.timeout = timeout
        self.config = type("EnvConfig", (), {"cwd": self.cwd})()

    def execute(
        self,
        command: str,
        cwd: Optional[str] = None,
        timeout: Optional[int] = None,
        stdin_data: Optional[str] = None,
        **_kwargs: Any,
    ) -> Dict[str, Any]:
        run_cwd = cwd or self.cwd or os.getcwd()
        run_timeout = timeout or self.timeout

        # Windows-only: spill very long commands to a tempfile and source it
        # so we don't trip CreateProcess's 32 KB command-line cap.  Linux
        # ARG_MAX is in the megabytes so this branch is a no-op there.
        spill_path: Optional[str] = None
        if os.name == "nt" and len(command) > _LONG_CMD_THRESHOLD:
            try:
                fd, spill_path = tempfile.mkstemp(prefix="phalanx-cmd-", suffix=".sh")
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(command)
                if _BASH_PATH:
                    argv = [_BASH_PATH, spill_path]
                    shell = False
                else:
                    argv = command  # no bash → keep inline; will likely fail loudly
                    shell = True
            except OSError as exc:
                logger.warning("phalanx terminal tempfile spill failed: %s", exc)
                spill_path = None

        if spill_path is None:
            if _BASH_PATH:
                argv = [_BASH_PATH, "-c", command]
                shell = False
            else:
                argv = command
                shell = True

        try:
            proc = subprocess.run(
                argv,
                shell=shell,
                cwd=run_cwd,
                input=stdin_data,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=run_timeout,
            )
            output = proc.stdout or ""
            if proc.stderr:
                output = output + (("\n" if output and not output.endswith("\n") else "") + proc.stderr)
            return {"output": output, "returncode": proc.returncode}
        except subprocess.TimeoutExpired as exc:
            stdout = (exc.stdout or b"").decode("utf-8", errors="replace") if isinstance(exc.stdout, (bytes, bytearray)) else (exc.stdout or "")
            stderr = (exc.stderr or b"").decode("utf-8", errors="replace") if isinstance(exc.stderr, (bytes, bytearray)) else (exc.stderr or "")
            output = (stdout or "") + (stderr or "") + f"\n[terminal_tool] command timed out after {run_timeout}s"
            return {"output": output, "returncode": 124}
        except FileNotFoundError as exc:
            return {"output": f"[terminal_tool] {exc}", "returncode": 127}
        finally:
            if spill_path:
                try:
                    os.unlink(spill_path)
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# Helpers used by file_tools.get_or_create_file_ops.
# ---------------------------------------------------------------------------

def _resolve_container_task_id(task_id: str) -> str:
    """Phase-2.2 stand-in: subagent task collapsing isn't wired up yet,
    so every task id maps to itself."""
    return task_id or "default"


def _get_env_config() -> Dict[str, Any]:
    """Return the currently-effective environment config.

    Wave-1 hard-codes ``env_type='local'`` and the host's cwd.  Real
    impl reads ``terminal.env_type`` from config.yaml and supports
    docker/singularity/ssh/modal/daytona/vercel_sandbox."""
    return {
        "env_type": "local",
        "cwd": os.getcwd(),
        "host_cwd": os.getcwd(),
        "timeout": 120,
        "docker_image": "",
        "singularity_image": "",
        "modal_image": "",
        "daytona_image": "",
        "container_cpu": 1,
        "container_memory": 5120,
        "container_disk": 51200,
        "container_persistent": False,
        "vercel_runtime": "",
        "docker_volumes": [],
        "docker_mount_cwd_to_workspace": False,
        "docker_forward_env": [],
        "docker_run_as_host_user": False,
        "ssh_host": "",
        "ssh_user": "",
        "ssh_port": 22,
        "ssh_key": "",
        "ssh_persistent": False,
        "local_persistent": False,
    }


def _create_environment(
    env_type: str = "local",
    image: str = "",
    cwd: Optional[str] = None,
    timeout: int = 120,
    ssh_config: Optional[Dict[str, Any]] = None,
    container_config: Optional[Dict[str, Any]] = None,
    local_config: Optional[Dict[str, Any]] = None,
    task_id: str = "default",
    host_cwd: Optional[str] = None,
) -> LocalTerminalEnv:
    """Wave-1 only supports ``env_type='local'``.  Other values raise so
    the missing wiring fails loudly rather than silently downgrading."""
    if env_type != "local":
        raise NotImplementedError(
            f"terminal_tool Phase-2.2 shim only supports env_type='local'; "
            f"got {env_type!r}"
        )
    return LocalTerminalEnv(cwd=cwd or os.getcwd(), timeout=timeout)


def _start_cleanup_thread() -> None:
    """Phase-2.2 no-op.  Real impl spawns a daemon thread that kills
    idle sandboxes after TERMINAL_IDLE_TIMEOUT seconds."""
    return None


# ---------------------------------------------------------------------------
# Direct ``terminal`` tool registration so the agent (and `hermes tools
# run terminal`) can shell out using the same backend file ops use.
# ---------------------------------------------------------------------------

TERMINAL_SCHEMA = {
    "name": "terminal",
    "description": (
        "Execute a shell command on the local host and return its "
        "stdout, stderr (concatenated), and exit code.  Phase-2.2 "
        "minimal: no persistent session, no approval queue, no "
        "container backends."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "cmd": {
                "type": "string",
                "description": "Shell command line.  POSIX syntax — bash is invoked when available.",
            },
            "cwd": {
                "type": "string",
                "description": "Working directory for the command.  Defaults to the agent's cwd.",
            },
            "timeout": {
                "type": "integer",
                "description": "Max seconds before the command is killed.  Defaults to 120.",
                "default": 120,
            },
        },
        "required": ["cmd"],
    },
}


def terminal(args: Dict[str, Any], task_id: str = "default", **_kwargs: Any) -> str:
    cmd = str(args.get("cmd") or "").strip()
    if not cmd:
        return tool_result(error="terminal: 'cmd' is required and must be non-empty")
    cwd = args.get("cwd") or None
    timeout_arg = args.get("timeout")
    try:
        timeout_val = int(timeout_arg) if timeout_arg is not None else 120
    except (TypeError, ValueError):
        timeout_val = 120

    task_id = _resolve_container_task_id(task_id)
    with _env_lock:
        env = _active_environments.get(task_id)
    if env is None:
        env = LocalTerminalEnv(cwd=cwd or os.getcwd(), timeout=timeout_val)
        with _env_lock:
            _active_environments[task_id] = env
            _last_activity[task_id] = time.time()

    result = env.execute(cmd, cwd=cwd, timeout=timeout_val)
    with _env_lock:
        _last_activity[task_id] = time.time()
    return tool_result(
        output=result.get("output", ""),
        returncode=result.get("returncode", 0),
    )


def check_terminal_requirements() -> bool:
    """Always available: subprocess is part of the stdlib."""
    return True


registry.register(
    name="terminal",
    toolset="terminal",
    schema=TERMINAL_SCHEMA,
    handler=terminal,
    check_fn=check_terminal_requirements,
    description="Execute a shell command on the local host (Phase-2.2 minimal).",
    emoji="💻",
)
