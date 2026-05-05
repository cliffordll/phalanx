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
    plus a key binding that injects a newline literal.  Slash-command
    completion lands in wave 2; this wave wires the session shell.
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

    return PromptSession(
        history=history_obj,
        auto_suggest=AutoSuggestFromHistory() if history_obj else None,
        key_bindings=bindings,
        enable_history_search=True,
        complete_while_typing=False,
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
    history: List[Dict[str, Any]] = []

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
        if line in _EXIT_TOKENS:
            return 0
        try:
            result = agent.run_conversation(line, conversation_history=history)
        except Exception as exc:
            sys.stderr.write(f"[error] {exc}\n")
            continue
        history = result["messages"]
        print(result.get("final_response", ""))


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
