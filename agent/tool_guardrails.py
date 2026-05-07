"""Pre-dispatch guardrail layer for tool calls (§2.8.d wave 1).

Inserts a classification + approval step between the agent's
``_dispatch_tool_call`` and the actual ``registry.dispatch(...)``.
Three possible verdicts:

* :data:`GuardrailVerdict.ALLOW` — read-only / known-safe call,
  pass through with zero overhead.
* :data:`GuardrailVerdict.REQUIRE_APPROVAL` — write / shell / self-
  modify call that needs explicit user consent.  In an interactive
  shell the user gets a y/N prompt (default N).  In a non-interactive
  context (oneshot, gateway, web, delegate sub-agent) the call
  defaults to deny unless ``yolo_mode=True`` was set on the agent.
* :data:`GuardrailVerdict.DENY` — blocked outright.  Currently fires
  when the call would touch self-modification paths (tools/, skills/,
  agent/, hermes_cli/, run_agent.py, ~/.phalanx/config.yaml) and
  ``enable_self_mod`` is False.

Wave 1 ships pure classification logic + the approval-prompt UI;
the dispatch hook itself wires up in :class:`run_agent.AIAgent`.
Wave 2 (checkpoint manager) and wave 3 (audit log) layer on top of
the same hook.

Public surface:

* :class:`GuardrailVerdict` — three-value enum.
* :class:`GuardrailDecision` — verdict + reason + danger_class +
  affected_paths.
* :func:`classify_tool_call` — pure function, no I/O.
* :func:`ask_for_approval` — interactive prompt or non-interactive
  default-deny resolver.

Design intent: classification is **deterministic** (regex + path
prefix), **conservative** (default ALLOW, only flag known-bad
patterns), and **independent of the LLM** so a stuck / hallucinating
agent can't bypass it.  Adversarial inputs (base64-encoded shells,
multi-stage commands) are a known limit; see
:mod:`docs.phase-2.8d-guardrails` §7 for follow-up wave candidates.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ─── Public types ─────────────────────────────────────────────────────


class GuardrailVerdict(Enum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


@dataclass
class GuardrailDecision:
    """Outcome of pre-dispatch classification.

    ``danger_class`` is a short identifier used for audit-log
    grouping later (§2.8.d wave 3).  ``affected_paths`` lists the
    file-system paths the tool call would touch — empty for shell
    commands without a clearly-identified target.  ``reason`` is a
    human-readable message suitable for direct display in the
    approval prompt.
    """

    verdict: GuardrailVerdict
    reason: str = ""
    danger_class: str = ""
    affected_paths: List[Path] = field(default_factory=list)


# ─── Tunables ─────────────────────────────────────────────────────────

# Dangerous shell-command regexes for terminal-tool calls.  Order
# doesn't matter; first match wins.  Each entry is
# (compiled_regex, danger_class, human_reason).
_DANGEROUS_TERMINAL_REGEXES: List[tuple] = [
    # Filesystem destruction.
    (re.compile(r"\brm\s+(-[rRf]+\s+)+/(?!tmp\b)"),
     "rm-rf-system",     "rm -rf on a system path"),
    (re.compile(r"\brm\s+(-[rRf]+\s+)+~(\s|/|$)"),
     "rm-rf-home",       "rm -rf on the home directory"),
    (re.compile(r"\brm\s+(-[rRf]+\s+)+\$HOME"),
     "rm-rf-home",       "rm -rf on $HOME"),
    # SQL destruction.
    (re.compile(r"\bDROP\s+(TABLE|DATABASE|SCHEMA)\b", re.IGNORECASE),
     "sql-drop",         "DROP TABLE / DATABASE / SCHEMA"),
    # Git destruction.
    (re.compile(r"\bgit\s+push\b[^\n]*--force\b"),
     "force-push",       "git push --force"),
    (re.compile(r"\bgit\s+push\b[^\n]*-f\b"),
     "force-push",       "git push -f"),
    (re.compile(r"\bgit\s+reset\s+--hard\s+(origin|HEAD~)"),
     "hard-reset",       "destructive git reset --hard"),
    (re.compile(r"\bgit\s+clean\s+-(?:[a-z]*f[a-z]*)\b"),
     "git-clean",        "git clean (force) drops untracked files"),
    # Permission loosening.
    (re.compile(r"\bchmod\s+(-R\s+)?0?7?77\b"),
     "chmod-777",        "chmod 777 makes targets world-writable"),
    # Privilege escalation.
    (re.compile(r"\bsudo\b"),
     "sudo",             "sudo invocation"),
    # Raw disk / dd.
    (re.compile(r">\s*/dev/(sd[a-z]|nvme0n1|hda)\b"),
     "raw-disk-write",   "raw block-device write"),
    (re.compile(r"\bdd\s+(if=|of=)"),
     "dd",               "dd command"),
    # Shell evaluation of unquoted expressions.
    (re.compile(r"\beval\s+\$"),
     "eval-expansion",   "eval of a $-expanded expression"),
    # Curl piped to shell — common malware vector.
    (re.compile(r"\bcurl\b[^|]*\|\s*(?:sudo\s+)?(?:bash|sh)\b"),
     "curl-pipe-sh",     "curl piped to a shell interpreter"),
    (re.compile(r"\bwget\s+-O-?\s.*\|\s*(?:sudo\s+)?(?:bash|sh)\b"),
     "wget-pipe-sh",     "wget piped to a shell interpreter"),
]

# File-system paths whose modification triggers the self-mod gate.
# Relative paths are resolved against the agent's cwd; absolute
# paths match by prefix.  ~/.phalanx/config.yaml is matched
# specially because it's not under cwd.
_SELF_MOD_PATH_PREFIXES = (
    "tools/",
    "skills/",
    "agent/",
    "hermes_cli/",
    "run_agent.py",
    "hermes_state.py",
    "hermes_constants.py",
    "cli.py",
)

_SELF_MOD_HOME_TARGETS = (
    ".phalanx/config.yaml",
    ".phalanx/.env",
    ".hermes/config.yaml",
    ".hermes/.env",
)

# Tools whose semantics are read-only / pure-read.  Always ALLOW
# regardless of args — saves a regex pass on every dispatch.
_ALWAYS_ALLOW_TOOLS = frozenset({
    "echo",
    "read_file",
    "search_files",
    "todo",          # mutates per-session in-memory store; no FS write
    "delegate_task", # gated separately by depth + the sub-agent's own
                     # _dispatch_tool_call; no point double-flagging
    "memory_recall",
    "web_search",
    "web_extract",
    "web_crawl",
})


# ─── Path helpers ─────────────────────────────────────────────────────


def _looks_like_self_mod(path_str: str, cwd: Path) -> bool:
    """Return True when *path_str* (as given to the tool) names a
    self-modification target.

    Three categories trigger:

    1. relative path under one of ``_SELF_MOD_PATH_PREFIXES``
       (e.g. "tools/foo.py", "agent/bar.py")
    2. absolute path that resolves under ``cwd`` and matches the same
       prefixes
    3. ``~/.phalanx/config.yaml`` etc. expanded by tilde or by env
    """
    if not path_str:
        return False

    # Tilde expansion (handles "~/.phalanx/config.yaml" as a literal
    # tool argument, common pattern).
    expanded = os.path.expanduser(path_str)

    # 3. user-config targets (anywhere on disk, prefix-matched after
    # expansion).
    for tail in _SELF_MOD_HOME_TARGETS:
        # Handle both forward and back slashes — the tool call could
        # use either on Windows.
        if expanded.replace("\\", "/").endswith("/" + tail):
            return True

    # 1 / 2. project-tree self-mod.  Normalise to a relative path under
    # cwd when possible, fall back to absolute prefix match.
    p = Path(expanded)
    rel_str: Optional[str] = None
    if p.is_absolute():
        try:
            rel = p.resolve().relative_to(cwd.resolve())
            rel_str = str(rel).replace("\\", "/")
        except ValueError:
            return False
    else:
        rel_str = str(p).replace("\\", "/")

    for prefix in _SELF_MOD_PATH_PREFIXES:
        if rel_str == prefix.rstrip("/") or rel_str.startswith(prefix):
            return True
    return False


# ─── Per-tool classifiers ─────────────────────────────────────────────


def _classify_terminal(args: Dict[str, Any]) -> Optional[GuardrailDecision]:
    """Run command through the dangerous-pattern table.

    Returns ``None`` when nothing matched (caller should treat as
    ALLOW).  Any match → ``REQUIRE_APPROVAL`` with the relevant
    danger_class.
    """
    cmd = str(args.get("command") or "")
    if not cmd:
        return None
    for regex, danger_class, reason in _DANGEROUS_TERMINAL_REGEXES:
        if regex.search(cmd):
            return GuardrailDecision(
                verdict=GuardrailVerdict.REQUIRE_APPROVAL,
                reason=f"{reason}: {cmd[:120]}"
                       + ("..." if len(cmd) > 120 else ""),
                danger_class=danger_class,
            )
    return None


def _classify_write(
    args: Dict[str, Any],
    *,
    cwd: Path,
    enable_self_mod: bool,
) -> Optional[GuardrailDecision]:
    """Common classifier for ``write_file`` / ``patch``.

    Both tools take a ``path`` arg.  When the path lands inside the
    self-mod prefix list, the verdict depends on ``enable_self_mod``:

      * False → DENY (with hint to set --enable-self-mod)
      * True  → REQUIRE_APPROVAL (still wants user consent per call)

    Non-self-mod writes are ALLOW.  Returns None for ALLOW path.
    """
    path = str(args.get("path") or args.get("file_path") or "")
    if not path:
        return None
    if not _looks_like_self_mod(path, cwd):
        return None

    if not enable_self_mod:
        return GuardrailDecision(
            verdict=GuardrailVerdict.DENY,
            reason=(
                f"path {path!r} is in the self-modification zone "
                "(tools/, skills/, agent/, …); pass "
                "--enable-self-mod to allow this kind of write"
            ),
            danger_class="self-mod-disabled",
            affected_paths=[Path(path)],
        )
    return GuardrailDecision(
        verdict=GuardrailVerdict.REQUIRE_APPROVAL,
        reason=f"self-modification target: {path}",
        danger_class="self-mod",
        affected_paths=[Path(path)],
    )


# ─── Public API ───────────────────────────────────────────────────────


def classify_tool_call(
    name: str,
    args: Dict[str, Any],
    *,
    cwd: Optional[Path] = None,
    enable_self_mod: bool = False,
) -> GuardrailDecision:
    """Pure pre-dispatch classification.

    Returns the appropriate :class:`GuardrailDecision`.  Default is
    :data:`GuardrailVerdict.ALLOW` — only known-bad patterns flag.
    Conservative-by-default keeps existing tool calls (echo, read_file,
    todo, …) working at zero overhead.

    No I/O.  No side effects.  Safe to call any number of times.
    """
    if not isinstance(args, dict):
        # Malformed dispatch — let the caller deal with the type
        # error rather than guess.
        return GuardrailDecision(verdict=GuardrailVerdict.ALLOW)

    if name in _ALWAYS_ALLOW_TOOLS:
        return GuardrailDecision(verdict=GuardrailVerdict.ALLOW)

    cwd = cwd or Path.cwd()

    if name == "terminal":
        decision = _classify_terminal(args)
        if decision is not None:
            return decision
        return GuardrailDecision(verdict=GuardrailVerdict.ALLOW)

    if name in ("write_file", "patch"):
        decision = _classify_write(
            args, cwd=cwd, enable_self_mod=enable_self_mod,
        )
        if decision is not None:
            return decision
        return GuardrailDecision(verdict=GuardrailVerdict.ALLOW)

    # Unknown tool — ALLOW by default.  New write-side tools should
    # add an entry to _ALWAYS_ALLOW_TOOLS or get a dedicated
    # classifier; until then, don't break anything.
    return GuardrailDecision(verdict=GuardrailVerdict.ALLOW)


# ─── Approval flow ────────────────────────────────────────────────────


def _is_interactive_stdin() -> bool:
    """True when stdin is a TTY (REPL / terminal session).

    We avoid reading the agent's ``platform`` field directly because
    a "cli" agent could be running inside a non-interactive context
    (CI, redirect, Docker exec) where prompting hangs the build.
    ``isatty()`` is the source of truth for "can we ask the user?".
    """
    try:
        return bool(sys.stdin.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def ask_for_approval(
    decision: GuardrailDecision,
    *,
    yolo_mode: bool = False,
    interactive: Optional[bool] = None,
    stdin: Optional[Any] = None,
    stderr: Optional[Any] = None,
) -> bool:
    """Resolve a REQUIRE_APPROVAL decision into approve / deny.

    Returns True iff the call should proceed.

    * ``yolo_mode=True`` → always approve, regardless of context.
      Caller is responsible for warning the user when yolo is active.
    * ``interactive=True`` (or stdin is a TTY when not specified) →
      prompt y/N on stderr, default N.  ``interactive`` lets tests
      drive the path without monkeypatching ``sys.stdin.isatty``.
    * Non-interactive non-yolo → deny with a stderr note explaining
      how to override.

    All output goes to stderr so the prompt doesn't pollute the
    agent's stdout / a piped final_response.
    """
    out = stderr if stderr is not None else sys.stderr

    if yolo_mode:
        out.write(
            f"🚨 YOLO mode: auto-approving {decision.danger_class} "
            f"({decision.reason[:80]})\n"
        )
        return True

    if interactive is None:
        interactive = _is_interactive_stdin()

    if not interactive:
        out.write(
            f"🚨 GUARDRAIL: refusing {decision.danger_class} in a "
            f"non-interactive context.\n"
            f"   Reason: {decision.reason}\n"
            f"   To override: run with --yolo (use with care).\n"
        )
        return False

    # Interactive prompt.  Read from the supplied stdin (defaults to
    # sys.stdin) so tests can inject a StringIO.
    inp = stdin if stdin is not None else sys.stdin
    out.write(
        f"\n🚨 GUARDRAIL: this tool call is flagged as "
        f"{decision.danger_class!r}.\n"
        f"   Reason: {decision.reason}\n"
        f"   Approve this action? [y/N] "
    )
    out.flush()
    try:
        answer = inp.readline().strip().lower()
    except Exception as exc:
        out.write(f"\n🚨 prompt read failed ({exc}); denying.\n")
        return False
    if answer in ("y", "yes"):
        return True
    out.write("   denied.\n")
    return False
