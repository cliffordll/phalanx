"""Slash-command registry + completer for the phalanx REPL.

Trimmed port of upstream ``hermes-agent/hermes_cli/commands.py``
(originally ~1700 lines).  This wave keeps the **registry layer** —
``CommandDef`` + ``COMMAND_REGISTRY`` + lookup helpers + a minimal
``SlashCommandCompleter`` for prompt_toolkit — and drops gateway /
messaging / plugin / skill machinery that depends on subsystems
phalanx hasn't ported yet.

Commands fall into two buckets (see ``docs/phase-2.6-repl.md`` §3.1):

* ``active`` — phalanx implements the handler today (wave 3).
* ``stub``   — phalanx exposes the name in ``/help`` and tab-complete
  but the handler prints "not yet implemented".

35-odd upstream commands that depend on §2.7+ subsystems
(checkpoint / skin / cron / skills / kanban / browser / voice /
worktree / background) are simply omitted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional

# prompt_toolkit is a soft dep — when it's missing the registry still
# works; only ``SlashCommandCompleter`` becomes unusable.  cli.py's
# wiring already gates on ``_PT_AVAILABLE`` before importing the
# completer class.
try:
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.document import Document
    _PT_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PT_AVAILABLE = False
    Completer = object  # type: ignore[assignment,misc]
    Completion = None   # type: ignore[assignment]


# ── CommandDef + central registry ───────────────────────────────────────


@dataclass(frozen=True)
class CommandDef:
    """Definition of a single slash command.

    Field semantics match upstream so future cherry-picks land
    cleanly:

    * ``name``        — canonical command, no leading slash
    * ``description`` — single-line help text shown by ``/help``
    * ``category``    — coarse grouping for the help listing
    * ``aliases``     — alternative names, all sharing the handler
    * ``args_hint``   — placeholder shown after the command in help
    * ``subcommands`` — tab-completable second-position tokens
    * ``stub``        — phalanx-specific: True when ``/help`` should
      tag the entry as "not yet implemented" (still completes)
    """

    name: str
    description: str
    category: str
    aliases: tuple[str, ...] = ()
    args_hint: str = ""
    subcommands: tuple[str, ...] = ()
    stub: bool = False


COMMAND_REGISTRY: list[CommandDef] = [
    # Session ────────────────────────────────────────────────────────────
    CommandDef("new", "Start a new session (fresh session ID + history)",
               "Session", aliases=("reset",)),
    CommandDef("clear", "Clear screen and start a new session", "Session"),
    CommandDef("history", "Show conversation history", "Session"),
    CommandDef("save", "Save (title) the current session",
               "Session", args_hint="<title>", stub=True),
    CommandDef("resume", "Resume a previously-stored session",
               "Session", args_hint="<id_or_prefix>"),
    CommandDef("retry", "Retry the last message", "Session", stub=True),
    CommandDef("undo", "Remove the last user/assistant exchange",
               "Session", stub=True),
    CommandDef("title", "Set a title for the current session",
               "Session", args_hint="[name]", stub=True),
    CommandDef("branch", "Branch the current session",
               "Session", aliases=("fork",), args_hint="[name]", stub=True),
    CommandDef("compress", "Manually compress conversation context",
               "Session", args_hint="[focus]", stub=True),

    # Configuration ──────────────────────────────────────────────────────
    CommandDef("model", "Switch model for this session",
               "Configuration", args_hint="[name]"),
    CommandDef("debug", "Toggle verbose logging on / off",
               "Configuration", args_hint="[on|off|status]",
               subcommands=("on", "off", "status")),
    CommandDef("personality", "Set a predefined personality",
               "Configuration", args_hint="[name]", stub=True),
    CommandDef("yolo", "Toggle YOLO mode (skip dangerous-command approvals)",
               "Configuration", stub=True),
    CommandDef("reasoning", "Manage reasoning effort and display",
               "Configuration", args_hint="[level|show|hide]",
               subcommands=("none", "minimal", "low", "medium", "high",
                            "show", "hide", "on", "off"), stub=True),

    # Tools ──────────────────────────────────────────────────────────────
    CommandDef("tools", "List or toggle available tools",
               "Tools", args_hint="[list|disable|enable] [name...]",
               subcommands=("list", "disable", "enable")),

    # Context ────────────────────────────────────────────────────────────
    CommandDef("ref",
               "Inspect @file:/@diff/@url:/@session references resolved this turn",
               "Context", args_hint="[show|help]",
               subcommands=("show", "help")),
    CommandDef("critic",
               "Spawn a critic sub-agent to review the last assistant reply",
               "Context", args_hint="[help]",
               subcommands=("help",)),

    # Info ───────────────────────────────────────────────────────────────
    CommandDef("help", "Show available commands", "Info"),

    # Exit ───────────────────────────────────────────────────────────────
    CommandDef("quit", "Exit the REPL", "Exit", aliases=("exit",)),
]


# ── Derived lookups ─────────────────────────────────────────────────────


def _build_command_lookup() -> dict[str, CommandDef]:
    """Map every name and alias to its CommandDef."""
    lookup: dict[str, CommandDef] = {}
    for cmd in COMMAND_REGISTRY:
        lookup[cmd.name] = cmd
        for alias in cmd.aliases:
            lookup[alias] = cmd
    return lookup


_COMMAND_LOOKUP: dict[str, CommandDef] = _build_command_lookup()


def resolve_command(name: str) -> Optional[CommandDef]:
    """Resolve a command name or alias to its CommandDef.

    Accepts names with or without the leading slash; case-insensitive.
    Returns ``None`` for unknown commands.
    """
    return _COMMAND_LOOKUP.get(name.lower().lstrip("/"))


def _build_description(cmd: CommandDef) -> str:
    """Build a CLI-facing description string including usage hint."""
    base = (
        f"{cmd.description} (usage: /{cmd.name} {cmd.args_hint})"
        if cmd.args_hint
        else cmd.description
    )
    if cmd.stub:
        base += "  [stub — Phase 2.7+]"
    return base


# Flat dict: "/command" → description (with usage + stub marker).
COMMANDS: dict[str, str] = {}
for _cmd in COMMAND_REGISTRY:
    COMMANDS[f"/{_cmd.name}"] = _build_description(_cmd)
    for _alias in _cmd.aliases:
        COMMANDS[f"/{_alias}"] = f"{_cmd.description} (alias for /{_cmd.name})"


# Categorized dict: category → ("/command" → description)
COMMANDS_BY_CATEGORY: dict[str, dict[str, str]] = {}
for _cmd in COMMAND_REGISTRY:
    _cat = COMMANDS_BY_CATEGORY.setdefault(_cmd.category, {})
    _cat[f"/{_cmd.name}"] = COMMANDS[f"/{_cmd.name}"]
    for _alias in _cmd.aliases:
        _cat[f"/{_alias}"] = COMMANDS[f"/{_alias}"]


# Subcommand lookup: "/cmd" → ["sub1", "sub2", ...]
SUBCOMMANDS: dict[str, list[str]] = {}
for _cmd in COMMAND_REGISTRY:
    if _cmd.subcommands:
        SUBCOMMANDS[f"/{_cmd.name}"] = list(_cmd.subcommands)


# ── prompt_toolkit completer ────────────────────────────────────────────


class SlashCommandCompleter(Completer):  # type: ignore[misc]
    """Tab-complete slash commands and their subcommands.

    Two cases:

    1. **Top-level** — the buffer starts with ``/`` and contains no
       space yet.  Yield every ``COMMANDS`` key whose prefix matches.
       Aliases participate so ``/ex<TAB>`` finds ``/exit``.
    2. **Subcommand** — the buffer matches ``/<known> <prefix>`` (one
       token after a known command).  Yield ``SUBCOMMANDS[/cmd]``
       members whose prefix matches.

    Anything else (more than one space, ``@`` references, raw text)
    yields no completions — Phase 2.6 wave 2 keeps it minimal.
    Phase 2.7+ slots ``@file:`` / file-path / skill completion in
    here.
    """

    def get_completions(
        self, document: "Document", complete_event,
    ) -> Iterable["Completion"]:
        text = document.text_before_cursor

        if not text.startswith("/"):
            return

        # Top-level: still on the first token (no spaces yet).
        if " " not in text:
            for slash_name, description in sorted(COMMANDS.items()):
                if slash_name.startswith(text):
                    yield Completion(
                        slash_name,
                        start_position=-len(text),
                        display_meta=description,
                    )
            return

        # Subcommand path: "<known> <maybe-partial>".  Only fires when
        # there's exactly one space (i.e. caller is typing the second
        # token) so we don't try to complete arbitrary args.
        head, _, tail = text.partition(" ")
        if " " in tail:
            return
        subs = SUBCOMMANDS.get(head)
        if not subs:
            return
        for sub in subs:
            if sub.startswith(tail):
                yield Completion(
                    sub,
                    start_position=-len(tail),
                    display_meta=f"{head} {sub}",
                )


# ── Convenience helpers (consumed by /help) ─────────────────────────────


def iter_active_commands() -> List[CommandDef]:
    """Return registered commands whose handlers are wired up today."""
    return [c for c in COMMAND_REGISTRY if not c.stub]


def iter_stub_commands() -> List[CommandDef]:
    """Return registered commands whose handlers print 'not yet implemented'."""
    return [c for c in COMMAND_REGISTRY if c.stub]
