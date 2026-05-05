#!/usr/bin/env python3
"""
Phalanx Agent CLI — Phase 2.6 wave 1.

This is the trimmed counterpart to hermes-agent's cli.py (originally
12,043 lines of prompt_toolkit-based TUI).  Wave 1 of Phase 2.6 grows
the REPL from a Phase-1 ``input()`` loop into a real prompt_toolkit
session with persistent history and ghost-text auto-suggestion.

Public surface kept stable:

  - ``main(...)`` at module top-level, since ``hermes_cli/main.py``
    defers to it via ``from cli import main as cli_main`` (matching
    upstream's calling convention).
  - ``python cli.py`` entry that drops the user into the same REPL.

Slash-command dispatch + completion + ``patch_stdout`` streaming
arrive in waves 2/3 of this phase (see ``docs/phase-2.6-repl.md``).

Usage::

    python cli.py                          # default REPL
    python cli.py --model gpt-4o-mini --query "hello"
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Avoid noisy startup output even if other modules log on import.
os.environ.setdefault("HERMES_QUIET", "1")

# prompt_toolkit is a soft dependency — when it can't be imported the
# REPL falls back to a plain ``input()`` loop.  Lazy / module-level so
# the import only fires when ``cli.py`` itself is loaded; the
# ``hermes oneshot`` / ``hermes session`` / ``hermes tools`` paths
# never import this module and keep their cold-start time intact.
try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.key_binding import KeyBindings
    _PT_AVAILABLE = True
except ImportError:  # pragma: no cover — exercised by the fallback test
    _PT_AVAILABLE = False


def _build_agent(
    *,
    model: Optional[str],
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    max_iterations: int = 90,
    max_tokens: Optional[int] = None,
    system: Optional[str] = None,
    verbose: bool = False,
    quiet: bool = False,
):
    """Construct an AIAgent for the REPL, resolving missing fields from env."""
    from run_agent import AIAgent
    try:
        from hermes_cli.config import load_config, cfg_get
    except ImportError:
        load_config = lambda: {}  # noqa: E731
        cfg_get = lambda cfg, *keys, default=None: default  # noqa: E731

    cfg = load_config()
    resolved_model = (
        model
        or os.environ.get("PHALANX_MODEL")
        or os.environ.get("OPENAI_MODEL")
        or cfg_get(cfg, "model", "default")
    )
    if not resolved_model:
        sys.stderr.write(
            "error: no model configured.  Set --model, $PHALANX_MODEL, "
            "$OPENAI_MODEL, or model.default in ~/.phalanx/config.yaml\n"
        )
        sys.exit(2)

    resolved_base = base_url or os.environ.get("OPENAI_BASE_URL") or cfg_get(cfg, "model", "base_url")
    resolved_key = api_key or os.environ.get("OPENAI_API_KEY") or os.environ.get("PHALANX_API_KEY")

    return AIAgent(
        base_url=resolved_base,
        api_key=resolved_key,
        model=resolved_model,
        max_iterations=max_iterations,
        max_tokens=max_tokens,
        verbose_logging=verbose,
        quiet_mode=quiet,
        ephemeral_system_prompt=system,
    )


def main(
    model: Optional[str] = None,
    *,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    query: Optional[str] = None,
    system: Optional[str] = None,
    max_iterations: int = 90,
    max_tokens: Optional[int] = None,
    verbose: bool = False,
    quiet: bool = False,
    # Accepted-but-ignored upstream knobs (kept so callers don't
    # break when delegating from hermes_cli/main.py).  Each arrives in a
    # later phase as noted in the migration plan.
    provider: Optional[str] = None,        # Phase 4
    toolsets: Optional[List[str]] = None,  # Phase 2.1.4 / 2.2
    skills: Optional[List[str]] = None,    # Phase 7+
    image: Optional[str] = None,           # Phase 4 (vision)
    resume: Optional[str] = None,          # Phase 5
    worktree: bool = False,                # Phase 7+
    checkpoints: bool = False,             # Phase 7+
    pass_session_id: bool = False,         # Phase 5
    ignore_rules: bool = False,            # Phase 7+
    ignore_user_config: bool = False,
) -> int:
    """Run the phalanx CLI.

    Two modes:
      - ``query`` provided   → single-turn (oneshot) and exit
      - ``query`` omitted    → enter the plain-text REPL

    Returns the process exit code.
    """
    if verbose:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    # Surface unsupported knobs so the user knows what's silently ignored.
    _warn_unsupported(provider=provider, toolsets=toolsets, skills=skills, image=image,
                      resume=resume, worktree=worktree, checkpoints=checkpoints,
                      pass_session_id=pass_session_id, ignore_rules=ignore_rules,
                      ignore_user_config=ignore_user_config, verbose=verbose)

    agent = _build_agent(
        model=model,
        base_url=base_url,
        api_key=api_key,
        max_iterations=max_iterations,
        max_tokens=max_tokens,
        system=system,
        verbose=verbose,
        quiet=quiet,
    )

    try:
        if query:
            return _run_oneshot(agent, query, verbose=verbose)
        return _run_repl(agent)
    finally:
        agent.close()


# ── Internals ──────────────────────────────────────────────────────────


def _print_random_tip() -> None:
    """Show one random tip from ``hermes_cli.tips`` at REPL startup.

    Best-effort — if the tips module is missing or empty we just
    skip silently rather than failing.
    """
    try:
        from hermes_cli.tips import pick_tip
        tip = pick_tip()
    except Exception:
        return
    if tip:
        print(f"tip: {tip}")


def _open_stream_context():
    """Return a context manager wrapping ``run_conversation``.

    Tries ``prompt_toolkit.patch_stdout`` first so streaming token
    deltas paint above the live prompt line.  ``patch_stdout`` can
    raise on ``__enter__`` (e.g. on Windows under msys2 / pytest
    where the console buffer probe fails).  When that happens or
    prompt_toolkit isn't available, falls back to a no-op context
    manager so streaming still works — just without prompt-aware
    redraw.
    """
    if _PT_AVAILABLE:
        try:
            from prompt_toolkit.patch_stdout import patch_stdout
            ctx = patch_stdout()
            # Probe: enter / exit immediately to surface backend errors
            # while we can still fall back.  Cheap on a working TTY.
            ctx.__enter__()
            ctx.__exit__(None, None, None)
            return patch_stdout()
        except Exception:
            return _NullContext()
    return _NullContext()


def _run_turn(agent, message: str, history):
    """Run one conversation turn, streaming token deltas to stdout.

    Wraps the call in ``patch_stdout`` (when supported) so the
    prompt line stays put while the model paints its reply above
    it.  When prompt_toolkit's terminal probe fails we fall back to
    plain stdout writes — same streaming, just no prompt-aware
    redraw.
    """
    streamed: list[str] = []

    def stream_callback(delta: str) -> None:
        sys.stdout.write(delta)
        sys.stdout.flush()
        streamed.append(delta)

    with _open_stream_context():
        result = agent.run_conversation(
            message,
            conversation_history=history,
            stream_callback=stream_callback,
        )

    final = result.get("final_response", "")
    if streamed:
        # Model already painted everything via the callback; just
        # close the line so the next prompt starts cleanly.
        sys.stdout.write("\n")
    elif final:
        # Non-streaming path (callback never fired) — print the full
        # reply.  Common for tool-only turns where deltas are empty.
        print(final)
    return result


class _NullContext:
    """A trivial context manager used when ``patch_stdout`` is unavailable."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _run_oneshot(agent, query: str, *, verbose: bool) -> int:
    try:
        result = agent.run_conversation(query)
    except Exception as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1
    print(result.get("final_response", ""))
    if verbose:
        sys.stderr.write(
            f"\n[done] turns={result['api_calls']} stop={result['stop_reason']} "
            f"budget={result['iterations_used']}/{agent.max_iterations}\n"
        )
    return 0


_EXIT_TOKENS = ("/exit", "/quit", ":q")

# Sentinel returned by slash handlers that want the REPL to terminate.
# Anything else (None / str / int) keeps the loop running.
_DISPATCH_EXIT = "__exit__"


def _cmd_help(args: str, state: dict) -> Optional[str]:
    """Render registered commands grouped by category.

    Active commands appear plainly; ``stub=True`` entries get a
    "[stub]" tag so users know they're complete-able but not wired
    up yet.  Aliases are inlined in parentheses on the canonical row
    rather than getting their own line.
    """
    from hermes_cli.commands import COMMAND_REGISTRY

    by_cat: dict[str, list] = {}
    for cmd in COMMAND_REGISTRY:
        by_cat.setdefault(cmd.category, []).append(cmd)

    print("Available slash commands:\n")
    for category in ("Session", "Configuration", "Tools", "Info", "Exit"):
        cmds = by_cat.get(category)
        if not cmds:
            continue
        print(f"  [{category}]")
        for cmd in cmds:
            label = f"/{cmd.name}"
            if cmd.aliases:
                label += " (" + ", ".join(f"/{a}" for a in cmd.aliases) + ")"
            tag = " [stub]" if cmd.stub else ""
            hint = f" {cmd.args_hint}" if cmd.args_hint else ""
            print(f"    {label}{hint}  — {cmd.description}{tag}")
        print()
    return None


def _cmd_exit(args: str, state: dict) -> str:
    return _DISPATCH_EXIT


def _cmd_stub(name: str) -> Any:
    """Return a handler that prints 'not yet implemented' for *name*."""
    def _h(args: str, state: dict) -> None:
        print(f"/{name}: not yet implemented in phalanx (Phase 2.7+)")
        return None
    return _h


# ── wave 3 handlers ─────────────────────────────────────────────────────


def _cmd_new(args: str, state: dict) -> None:
    """Reset the in-process history + give the agent a fresh session id.

    The previous session row stays in ``state.db`` so ``/resume`` can
    find it; we just stop appending to it.  Resets the flush cursor
    so the next turn writes starting from message index 0.
    """
    import uuid
    agent = state["agent"]
    agent.session_id = str(uuid.uuid4())
    agent._session_db_created = False
    agent._last_flushed_db_idx = 0
    state["history"] = []
    print(f"started new session ({agent.session_id[:8]})")
    return None


def _cmd_clear(args: str, state: dict) -> None:
    """Clear the screen, then act like ``/new``."""
    os.system("cls" if os.name == "nt" else "clear")
    return _cmd_new(args, state)


def _cmd_history(args: str, state: dict) -> None:
    """Print the current in-process conversation history."""
    history = state.get("history") or []
    if not history:
        print("(no messages yet)")
        return None
    for i, msg in enumerate(history):
        role = msg.get("role") or "?"
        content = msg.get("content") or ""
        if isinstance(content, list):
            # Multimodal payload — show a short marker rather than
            # dumping the JSON.
            content = f"<{len(content)} content parts>"
        text = str(content).replace("\n", " ")
        if len(text) > 80:
            text = text[:77] + "..."
        print(f"  [{i}] {role}: {text}")
    return None


def _cmd_model(args: str, state: dict) -> None:
    """Show or switch the agent's model.

    Bare ``/model`` prints the current model.  ``/model <name>``
    swaps it; the next turn picks up the new value.  Phalanx doesn't
    re-validate the model name against ``provider list`` here —
    invalid names surface as an API error on the next turn, which
    matches upstream behavior.
    """
    agent = state["agent"]
    name = args.strip()
    if not name:
        print(f"current model: {agent.model}")
        return None
    old = agent.model
    agent.model = name
    print(f"model: {old} -> {name}")
    return None


def _cmd_debug(args: str, state: dict) -> None:
    """Toggle ``agent.verbose_logging``.

    Subcommands: ``on`` / ``off`` / ``status`` (default ``status``
    when called bare).  Also flips the root logger level so the
    change takes effect immediately for any module already
    importing ``logging.getLogger(__name__)``.
    """
    agent = state["agent"]
    sub = args.strip().lower() or "status"
    if sub == "on":
        agent.verbose_logging = True
        logging.getLogger().setLevel(logging.DEBUG)
        print("debug: on")
    elif sub == "off":
        agent.verbose_logging = False
        logging.getLogger().setLevel(logging.WARNING)
        print("debug: off")
    elif sub == "status":
        flag = "on" if agent.verbose_logging else "off"
        print(f"debug: {flag}")
    else:
        print(f"/debug: unknown sub-action {sub!r} (use on / off / status)")
    return None


def _cmd_tools(args: str, state: dict) -> None:
    """List or toggle available tools.

    ``/tools`` and ``/tools list`` print the registry.  ``/tools
    disable <name>`` adds *name* to ``agent.disabled_toolsets``;
    ``/tools enable <name>`` removes it.  The schemas cache is
    invalidated so the change takes effect on the next turn.
    """
    agent = state["agent"]
    sub, _, rest = args.strip().partition(" ")
    sub = sub or "list"
    if sub == "list":
        registry = getattr(agent, "_tool_registry", None)
        if registry is None:
            print("(no tool registry loaded)")
            return None
        names = registry.get_all_tool_names()
        if not names:
            print("(no tools registered)")
            return None
        for name in names:
            toolset = registry.get_toolset_for_tool(name) or "?"
            schema = registry.get_schema(name) or {}
            desc_first_line = (schema.get("description") or "").splitlines()
            desc = desc_first_line[0] if desc_first_line else ""
            disabled = (
                " (disabled)" if toolset in (agent.disabled_toolsets or [])
                else ""
            )
            print(f"  {name:24s} [{toolset}]{disabled}  {desc}")
        return None
    if sub in ("disable", "enable"):
        target = rest.strip()
        if not target:
            print(f"/tools {sub}: missing tool / toolset name")
            return None
        disabled = list(agent.disabled_toolsets or [])
        if sub == "disable":
            if target not in disabled:
                disabled.append(target)
            print(f"disabled: {target}")
        else:
            disabled = [t for t in disabled if t != target]
            print(f"enabled: {target}")
        agent.disabled_toolsets = disabled
        agent._tool_schemas_cache = None
        return None
    print(f"/tools: unknown sub-action {sub!r} (use list / disable / enable)")
    return None


def _cmd_resume(args: str, state: dict) -> None:
    """Resume a previously-stored session in-place.

    Resolves a full id or a unique prefix via SessionDB, replays the
    history into ``state['history']`` so the next turn carries the
    full context, and re-points ``agent.session_id`` so writes land
    on the same row.  ``reopen_session`` undoes the prior
    ``end_session`` so the upcoming end-of-turn close records the new
    ``stop_reason``.
    """
    target = args.strip()
    if not target:
        print("/resume: usage /resume <session_id_or_prefix>")
        return None
    agent = state["agent"]
    db = getattr(agent, "_session_db", None)
    if db is None:
        print("/resume: session DB unavailable in this REPL")
        return None
    try:
        from hermes_state import SessionDB  # noqa: F401  (typing only)
    except Exception as exc:  # pragma: no cover
        print(f"/resume: session DB unavailable: {exc}")
        return None
    sid = db.resolve_session_id(target)
    if not sid:
        print(f"/resume: no matching session {target!r} (or prefix is ambiguous)")
        return None
    sid = db.resolve_resume_session_id(sid)
    history = db.get_messages_as_conversation(sid)
    db.reopen_session(sid)
    agent.session_id = sid
    agent._session_db_created = True
    agent._last_flushed_db_idx = len(history)
    state["history"] = history
    print(f"resumed session {sid[:8]} ({len(history)} messages restored)")
    return None


# Dispatch table — wave 3 wires the day-to-day handlers.  Anything not
# listed here that's still in the registry falls through to
# ``_cmd_stub`` which prints 'not yet implemented'.
_SLASH_HANDLERS: dict[str, Any] = {
    "help":    _cmd_help,
    "new":     _cmd_new,
    "clear":   _cmd_clear,
    "history": _cmd_history,
    "model":   _cmd_model,
    "debug":   _cmd_debug,
    "tools":   _cmd_tools,
    "resume":  _cmd_resume,
    "quit":    _cmd_exit,
    "exit":    _cmd_exit,
}


def _dispatch_slash(line: str, state: dict) -> Optional[str]:
    """Route a ``/<cmd> <args>`` line to the right handler.

    * Resolves aliases via :func:`hermes_cli.commands.resolve_command`
      so ``/reset`` and ``/new`` both reach the same handler.
    * Unknown commands print a helpful "not registered" line.
    * Registered-but-not-yet-wired commands print the stub message
      from :func:`_cmd_stub`.
    * Returns :data:`_DISPATCH_EXIT` only when the handler asks for
      the REPL loop to terminate (``/exit`` / ``/quit``).
    """
    from hermes_cli.commands import resolve_command

    head, _, args = line[1:].partition(" ")
    cmd = resolve_command(head)
    if cmd is None:
        print(f"unknown command: /{head} (type /help for the list)")
        return None
    handler = _SLASH_HANDLERS.get(cmd.name)
    if handler is None:
        # Registered but no handler yet — wave-2 stub.
        return _cmd_stub(cmd.name)(args, state)
    return handler(args, state)


def _history_path() -> Optional[Path]:
    """Resolve the persistent CLI history file path under PHALANX_HOME.

    Returns ``None`` when the home directory can't be created (e.g. a
    read-only filesystem); the REPL then runs without persistent
    history but still works.
    """
    try:
        from hermes_constants import get_hermes_home
        home = get_hermes_home()
        home.mkdir(parents=True, exist_ok=True)
        return home / "cli_history"
    except Exception as exc:
        logger.debug("could not resolve history path: %s", exc)
        return None


def _build_prompt_session() -> "PromptSession[str]":
    """Construct the prompt_toolkit session with history + auto-suggest.

    ``Alt+Enter`` inserts a newline (multiline input); plain ``Enter``
    submits — this is prompt_toolkit's default for ``multiline=False``
    plus a key binding that injects a newline literal.  Tab-completes
    slash commands and their subcommands via
    :class:`hermes_cli.commands.SlashCommandCompleter` (wave 2).
    """
    history_obj = None
    history_path = _history_path()
    if history_path is not None:
        try:
            history_obj = FileHistory(str(history_path))
        except Exception as exc:
            logger.debug("FileHistory init failed: %s", exc)

    bindings = KeyBindings()

    @bindings.add("escape", "enter")  # Alt+Enter / Esc-then-Enter
    def _newline(event):
        event.app.current_buffer.insert_text("\n")

    # Lazy import — keeps this module loadable when prompt_toolkit
    # itself is missing (the fallback path stays available).
    try:
        from hermes_cli.commands import SlashCommandCompleter
        completer: Optional[Any] = SlashCommandCompleter()
    except Exception as exc:
        logger.debug("slash completer unavailable: %s", exc)
        completer = None

    return PromptSession(
        history=history_obj,
        auto_suggest=AutoSuggestFromHistory() if history_obj else None,
        key_bindings=bindings,
        enable_history_search=True,
        complete_while_typing=True if completer else False,
        completer=completer,
    )


def _run_repl(agent) -> int:
    """Drive the interactive REPL.

    Uses prompt_toolkit when importable (history + auto-suggest +
    Alt+Enter newline); falls back to a plain ``input()`` loop when
    not (e.g. on an embedded interpreter or when prompt_toolkit
    couldn't load).  Either way the slash-command surface stays the
    same: ``/exit`` / ``/quit`` / ``:q`` ends the session.
    """
    print(f"phalanx chat (model={agent.model}).  Ctrl-D / Ctrl-C / /exit to quit.")
    _print_random_tip()
    state: Dict[str, Any] = {"agent": agent, "history": []}

    if _PT_AVAILABLE:
        session = _build_prompt_session()

        def _read() -> str:
            return session.prompt("> ")
    else:
        def _read() -> str:
            return input("> ")

    while True:
        try:
            line = _read().strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line.startswith("/") or line in _EXIT_TOKENS:
            # ``:q`` is a vim-ism — keep the legacy alias mapped to /exit.
            cmd_line = "/exit" if line == ":q" else line
            outcome = _dispatch_slash(cmd_line, state)
            if outcome == _DISPATCH_EXIT:
                return 0
            continue
        try:
            result = _run_turn(agent, line, state["history"])
        except Exception as exc:
            sys.stderr.write(f"[error] {exc}\n")
            continue
        state["history"] = result["messages"]
        # Final response prints in _run_turn after streaming.


def _warn_unsupported(**flags) -> None:
    """Emit a single-line warning per flag the Phase-1 shell ignores."""
    notable = {
        "provider": "Phase 4 (multi-provider adapters)",
        "toolsets": "Phase 2.1.4 / 2.2 (tool registry filters)",
        "skills": "Phase 7+ (skills system)",
        "image": "Phase 4 (vision)",
        "resume": "Phase 5 (session persistence)",
        "worktree": "Phase 7+",
        "checkpoints": "Phase 7+",
        "pass_session_id": "Phase 5",
        "ignore_rules": "Phase 7+",
        "ignore_user_config": "Phase 7+ (config layering)",
    }
    for key, when in notable.items():
        value = flags.get(key)
        active = (
            (isinstance(value, bool) and value)
            or (isinstance(value, (str, list)) and value)
        )
        if active:
            logger.info("flag --%s ignored (arrives in %s)", key.replace("_", "-"), when)


if __name__ == "__main__":  # pragma: no cover
    try:
        import fire
    except ImportError:
        sys.stderr.write("error: 'fire' package required; pip install fire\n")
        sys.exit(2)
    sys.exit(fire.Fire(main))
