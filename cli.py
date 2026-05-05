#!/usr/bin/env python3
"""
Phalanx Agent CLI — Phase 1 thin shell.

This is the heavily-trimmed counterpart to hermes-agent's cli.py
(originally 12,043 lines of prompt_toolkit-based TUI).  Phase 1 keeps
only:

  - The function ``main(...)`` exported at module top-level, since
    ``hermes_cli/main.py`` defers to it via ``from cli import main as
    cli_main`` (matching upstream's calling convention).
  - A direct ``python cli.py`` entry that drops the user into the same
    plain-text REPL.

The full prompt_toolkit / TUI / streaming / slash-command experience
arrives in Phase 6 (see docs/MIGRATION_PLAN.md §2.6).

Usage::

    python cli.py                          # default REPL
    python cli.py --model gpt-4o-mini --query "hello"
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Avoid noisy startup output even if other modules log on import.
os.environ.setdefault("HERMES_QUIET", "1")


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


def _run_repl(agent) -> int:
    print(f"phalanx chat (model={agent.model}).  Ctrl-D / Ctrl-C / /exit to quit.")
    history: List[Dict[str, Any]] = []
    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line in ("/exit", "/quit", ":q"):
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
