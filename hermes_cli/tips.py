"""Random tips shown at REPL startup.

Trimmed port of upstream ``hermes_cli/tips.py`` — kept only the
entries that map to features phalanx actually ships today (Phase
2.6).  When more subsystems land (skin / personality / worktree /
checkpoint / browser / cron / skills) future waves can re-add the
relevant lines.

The corpus here is intentionally small (~20 tips).  Its purpose is
discovery for first-time users, not entertainment density.
"""

from __future__ import annotations

import random
from typing import Optional


TIPS: list[str] = [
    # ── Slash commands (active in phalanx) ──
    "/help lists every registered slash command grouped by category.",
    "/new starts a fresh conversation; the prior session is still in state.db.",
    "/clear is /new plus a screen wipe — handy when the scrollback is noisy.",
    "/history prints the current in-process conversation as a numbered list.",
    "/model swaps models for the next turn (no arg → show the current model).",
    "/debug on flips verbose logging on; /debug off / /debug status round it out.",
    "/tools list shows registered tools; /tools disable <name> hides one for the session.",
    "/resume <prefix> picks up a stored session — 8 chars of the id is usually enough.",
    "/quit, /exit, or :q all leave the REPL cleanly.  Ctrl+D works too.",

    # ── Top-level CLI ──
    "hermes oneshot \"...\" runs a single turn from the shell, then exits.",
    "hermes --resume <id> oneshot \"...\" continues a stored session in one shot.",
    "hermes session list shows recent sessions with id / source / model / preview.",
    "hermes session dump <id> emits one JSON line per message — pipe to jq.",
    "hermes logs --level WARNING --since 1h tails today's warnings only.",
    "hermes logs --component tools narrows the tail to tool-related loggers.",

    # ── REPL ergonomics ──
    "Tab completes /<command> names and their subcommands.",
    "History persists across runs at ~/.hermes/cli_history — Up arrow walks it.",
    "Alt+Enter inserts a newline; plain Enter submits the current input.",
    "Slash commands are case-insensitive — /HELP, /Help, /help all work.",

    # ── Sessions / persistence ──
    "Every CLI turn writes to ~/.hermes/state.db automatically; nothing extra to enable.",
    "Use 8-char prefixes when /resume'ing — only fully unique prefixes resolve.",
]


def pick_tip(tips: Optional[list[str]] = None) -> Optional[str]:
    """Return one random tip from *tips* (default: the module corpus).

    Returns ``None`` when the corpus is empty so the caller can skip
    rendering without special-casing.
    """
    pool = tips if tips is not None else TIPS
    if not pool:
        return None
    return random.choice(pool)
