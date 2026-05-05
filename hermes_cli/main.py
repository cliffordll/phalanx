#!/usr/bin/env python3
"""
Phalanx CLI entry point — heavily trimmed from hermes_cli/main.py.

Phase 1 subcommands (per docs/MIGRATION_PLAN.md §2.1.3):

    hermes oneshot "<msg>"        single-turn query, prints final reply
    hermes chat                    interactive REPL (plain input(), no prompt_toolkit)
    hermes tools list              dump tools.registry's tool catalogue
    hermes tools run NAME --args   call a tool directly, bypass the loop
    hermes config show             dump ~/.phalanx/config.yaml
    hermes config get KEY[.KEY]    fetch one nested config value
    hermes version                 print phalanx version / release date
    hermes doctor                  inspect env, paths, API key presence
    hermes --debug ...             global flag → INFO logging + verbose_logging

Upstream's gateway / setup / cron / honcho / sessions / claw / acp …
subcommands arrive in later phases; the dispatch table here is
purposely small to keep first-pass debugging cycles fast.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import List, Optional

from hermes_cli import __version__, __release_date__
from hermes_cli.config import cfg_get, load_config
from hermes_cli.env_loader import load_hermes_dotenv
from hermes_constants import (
    display_hermes_home,
    get_config_path,
    get_env_path,
    get_hermes_home,
)

logger = logging.getLogger("hermes_cli")


# ── Global state pulled from CLI flags ─────────────────────────────────

class _Flags:
    """Mutable holder for global CLI flags shared across subcommands."""
    debug: bool = False
    quiet: bool = False
    model: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None


# ── Argparse wiring ────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    """Construct the top-level argparse parser with all subcommands.

    Mirrors upstream hermes_cli/_parser.py's flat structure: global
    flags live on the top-level parser only; subparsers re-declare any
    flag they happen to need.  Result: ``hermes --debug oneshot "..."``
    works, ``hermes oneshot --debug "..."`` does not — same as upstream.
    """
    parser = argparse.ArgumentParser(
        prog="hermes",
        description="Phalanx — minimal AI agent ported from hermes-agent",
    )
    parser.add_argument("--debug", action="store_true", help="enable verbose debug output")
    parser.add_argument("--quiet", action="store_true", help="suppress non-essential output")
    parser.add_argument("--model", default=None, help="LLM model id (overrides env)")
    parser.add_argument("--base-url", dest="base_url", default=None,
                        help="OpenAI-compatible endpoint base URL")
    parser.add_argument("--api-key", dest="api_key", default=None,
                        help="API key (overrides env)")

    sub = parser.add_subparsers(dest="cmd", metavar="<command>")

    # oneshot ----------------------------------------------------------
    p_one = sub.add_parser("oneshot", help="single-turn query, prints final reply")
    p_one.add_argument("message", nargs="?", help="user message")
    p_one.add_argument("--message", dest="message_kw", default=None,
                       help="user message (alternative form)")
    p_one.add_argument("--system", default=None, help="custom system prompt")
    p_one.add_argument("--max-iterations", type=int, default=90,
                       help="cap on tool-calling iterations (default 90)")
    p_one.add_argument("--max-tokens", type=int, default=None,
                       help="max tokens for the model response")
    p_one.set_defaults(func=cmd_oneshot)

    # chat -------------------------------------------------------------
    p_chat = sub.add_parser("chat", help="plain interactive REPL")
    p_chat.add_argument("--system", default=None, help="custom system prompt")
    p_chat.add_argument("--max-iterations", type=int, default=90)
    p_chat.set_defaults(func=cmd_chat)

    # tools ------------------------------------------------------------
    p_tools = sub.add_parser("tools", help="inspect / call individual tools")
    p_tools_sub = p_tools.add_subparsers(dest="tools_cmd", metavar="<subcmd>")
    p_tools_list = p_tools_sub.add_parser("list", help="list registered tools")
    p_tools_list.add_argument("--verbose", action="store_true",
                              help="include parameter schemas")
    p_tools_list.set_defaults(func=cmd_tools_list)
    p_tools_run = p_tools_sub.add_parser("run", help="invoke a tool directly")
    p_tools_run.add_argument("name", help="tool name")
    p_tools_run.add_argument("--args", default="{}",
                             help="JSON-encoded tool arguments")
    p_tools_run.set_defaults(func=cmd_tools_run)
    p_tools.set_defaults(func=cmd_tools_help)

    # config -----------------------------------------------------------
    p_cfg = sub.add_parser("config", help="show / read user config")
    p_cfg_sub = p_cfg.add_subparsers(dest="config_cmd", metavar="<subcmd>")
    p_cfg_show = p_cfg_sub.add_parser("show", help="dump entire config")
    p_cfg_show.set_defaults(func=cmd_config_show)
    p_cfg_get = p_cfg_sub.add_parser("get", help="fetch one nested value")
    p_cfg_get.add_argument("key", help="dotted key path, e.g. model.default")
    p_cfg_get.set_defaults(func=cmd_config_get)
    p_cfg.set_defaults(func=cmd_config_help)

    # version ----------------------------------------------------------
    p_ver = sub.add_parser("version", help="print phalanx version")
    p_ver.set_defaults(func=cmd_version)

    # doctor -----------------------------------------------------------
    p_doc = sub.add_parser("doctor", help="environment / config sanity check")
    p_doc.set_defaults(func=cmd_doctor)

    return parser


# ── Setup ──────────────────────────────────────────────────────────────


def _setup_logging(debug: bool) -> None:
    """Wire root logger to stderr at INFO (or DEBUG with --debug)."""
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _load_dotenv_best_effort() -> List[Path]:
    """Try to load ~/.phalanx/.env and ./.env, ignoring failures."""
    try:
        return load_hermes_dotenv(
            hermes_home=get_hermes_home(),
            project_env=Path.cwd() / ".env",
        ) or []
    except Exception as exc:
        logger.debug("dotenv load skipped: %s", exc)
        return []


def _build_agent(args: argparse.Namespace, *,
                 max_iterations: int = 90,
                 max_tokens: Optional[int] = None,
                 system: Optional[str] = None):
    """Construct an AIAgent honoring global + subcommand flags."""
    # Lazy import — keeps `hermes version` / `hermes doctor` fast even
    # when openai SDK can't load (e.g. missing httpx variant).
    from run_agent import AIAgent

    cfg = load_config()
    model = (
        _Flags.model
        or os.environ.get("PHALANX_MODEL")
        or os.environ.get("OPENAI_MODEL")
        or cfg_get(cfg, "model", "default")
    )
    if not model:
        sys.stderr.write(
            "error: no model configured.  Set --model, $PHALANX_MODEL, "
            "$OPENAI_MODEL, or model.default in ~/.phalanx/config.yaml\n"
        )
        sys.exit(2)

    base_url = (
        _Flags.base_url
        or os.environ.get("OPENAI_BASE_URL")
        or cfg_get(cfg, "model", "base_url")
    )
    api_key = (
        _Flags.api_key
        or os.environ.get("OPENAI_API_KEY")
        or os.environ.get("PHALANX_API_KEY")
    )

    return AIAgent(
        base_url=base_url,
        api_key=api_key,
        model=model,
        max_iterations=max_iterations,
        max_tokens=max_tokens,
        verbose_logging=_Flags.debug,
        quiet_mode=_Flags.quiet,
        ephemeral_system_prompt=system,
    )


# ── Subcommand handlers ────────────────────────────────────────────────


def cmd_oneshot(args: argparse.Namespace) -> int:
    msg = args.message or args.message_kw
    if not msg:
        sys.stderr.write("error: oneshot requires a message\n")
        return 2
    agent = _build_agent(
        args,
        max_iterations=args.max_iterations,
        max_tokens=args.max_tokens,
        system=args.system,
    )
    try:
        result = agent.run_conversation(msg)
    finally:
        agent.close()
    print(result.get("final_response", ""))
    if _Flags.debug:
        sys.stderr.write(
            f"\n[done] turns={result['api_calls']} stop={result['stop_reason']} "
            f"budget={result['iterations_used']}/{agent.max_iterations}\n"
        )
    return 0


def cmd_chat(args: argparse.Namespace) -> int:
    """Delegate to cli.py:main for the interactive REPL.

    Mirrors upstream's pattern at hermes_cli/main.py:1313, where
    ``hermes chat`` and the bare ``hermes`` invocation both call
    ``from cli import main as cli_main``.  Keeping the delegation
    means future cherry-picks of cli.py changes need no rewiring.
    """
    from cli import main as cli_main
    return int(cli_main(
        model=_Flags.model,
        base_url=_Flags.base_url,
        api_key=_Flags.api_key,
        max_iterations=args.max_iterations,
        system=args.system,
        verbose=_Flags.debug,
        quiet=_Flags.quiet,
    ) or 0)


def cmd_tools_help(args: argparse.Namespace) -> int:
    print("usage: hermes tools <list|run> ...")
    return 0


def cmd_tools_list(args: argparse.Namespace) -> int:
    registry = _load_tool_registry()
    if registry is None:
        print("no tool registry loaded (tools/registry.py missing)")
        return 0
    names = registry.get_all_tool_names()
    if not names:
        print("(no tools registered)")
        return 0
    for name in names:
        toolset = registry.get_toolset_for_tool(name) or "?"
        schema = registry.get_schema(name) or {}
        desc = schema.get("description", "")
        # Trim long descriptions to one line for the default view.
        first_line = desc.splitlines()[0] if desc else ""
        print(f"  {name:24s} [{toolset}]  {first_line}")
        if args.verbose:
            params = schema.get("parameters")
            if params:
                print("      schema:", json.dumps(params, ensure_ascii=False))
    return 0


def cmd_tools_run(args: argparse.Namespace) -> int:
    registry = _load_tool_registry()
    if registry is None:
        sys.stderr.write("error: no tool registry loaded\n")
        return 2
    dispatch = getattr(registry, "dispatch", None)
    if not callable(dispatch):
        sys.stderr.write("error: tools.registry has no dispatch()\n")
        return 2
    try:
        parsed_args = json.loads(args.args)
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"error: --args must be valid JSON: {exc}\n")
        return 2
    if not isinstance(parsed_args, dict):
        sys.stderr.write("error: --args must decode to a JSON object\n")
        return 2
    result = dispatch(args.name, parsed_args)
    if isinstance(result, str):
        print(result)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_config_help(args: argparse.Namespace) -> int:
    print("usage: hermes config <show|get> ...")
    return 0


def cmd_config_show(args: argparse.Namespace) -> int:
    cfg = load_config()
    if not cfg:
        path = get_config_path()
        print(f"# no config file at {path}")
        return 0
    import yaml
    sys.stdout.write(yaml.dump(cfg, default_flow_style=False, sort_keys=False, allow_unicode=True))
    return 0


def cmd_config_get(args: argparse.Namespace) -> int:
    keys = [k for k in args.key.split(".") if k]
    if not keys:
        sys.stderr.write("error: empty key path\n")
        return 2
    cfg = load_config()
    value = cfg_get(cfg, *keys, default=None)
    if value is None:
        sys.stderr.write(f"# {args.key}: <unset>\n")
        return 1
    if isinstance(value, (dict, list)):
        print(json.dumps(value, ensure_ascii=False, indent=2))
    else:
        print(value)
    return 0


def cmd_version(args: argparse.Namespace) -> int:
    print(f"phalanx (hermes-agent fork) {__version__} ({__release_date__})")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Sanity-check environment + config + module imports."""
    issues: List[str] = []
    print("== phalanx doctor ==")
    print(f"phalanx version : {__version__}  ({__release_date__})")
    print(f"python          : {sys.version.split()[0]}  ({sys.executable})")

    home = get_hermes_home()
    print(f"PHALANX_HOME    : {display_hermes_home()}  (resolved: {home})")
    print(f"  exists        : {home.exists()}")
    cfg_path = get_config_path()
    print(f"config.yaml     : {cfg_path}  exists={cfg_path.exists()}")
    env_path = get_env_path()
    print(f".env            : {env_path}  exists={env_path.exists()}")

    # API key presence (never print value)
    keys_seen = []
    for var in ("OPENAI_API_KEY", "PHALANX_API_KEY", "ANTHROPIC_API_KEY"):
        keys_seen.append(f"{var}={'<set>' if os.environ.get(var) else '<unset>'}")
    print("api keys        : " + ", ".join(keys_seen))

    # Resolved model
    cfg = load_config()
    resolved_model = (
        _Flags.model
        or os.environ.get("PHALANX_MODEL")
        or os.environ.get("OPENAI_MODEL")
        or cfg_get(cfg, "model", "default")
        or "<not configured>"
    )
    print(f"resolved model  : {resolved_model}")
    if resolved_model == "<not configured>":
        issues.append("no model configured (set --model, $PHALANX_MODEL, or model.default)")

    # Module import probe
    print("module imports  :")
    for mod in ("run_agent", "agent.retry_utils", "agent.error_classifier"):
        try:
            __import__(mod)
            print(f"  ok  {mod}")
        except Exception as exc:
            print(f"  FAIL {mod}: {type(exc).__name__}: {exc}")
            issues.append(f"import {mod} failed")

    # Tool registry
    registry = _load_tool_registry()
    print(f"tool registry   : {'available' if registry else 'absent (Phase 2.1.4 not done)'}")

    print("---")
    if issues:
        print(f"{len(issues)} issue(s):")
        for i in issues:
            print(f"  - {i}")
        return 1
    print("all checks passed")
    return 0


# ── Helpers ────────────────────────────────────────────────────────────


def _load_tool_registry():
    """Return the singleton ``tools.registry.registry``, or None.

    Importing ``tools`` runs its ``__init__.py``, which triggers each
    built-in tool module's top-level ``registry.register(...)`` call.
    By the time we hand back the singleton, all built-ins are loaded.
    """
    try:
        from tools.registry import registry  # type: ignore[import-not-found]
        import tools  # type: ignore[import-not-found]  # noqa: F401
        return registry
    except ImportError:
        return None


# ── Entry ──────────────────────────────────────────────────────────────


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    _Flags.debug = bool(getattr(args, "debug", False))
    _Flags.quiet = bool(getattr(args, "quiet", False))
    _Flags.model = getattr(args, "model", None)
    _Flags.base_url = getattr(args, "base_url", None)
    _Flags.api_key = getattr(args, "api_key", None)

    _setup_logging(_Flags.debug)
    _load_dotenv_best_effort()

    # Default subcommand: chat
    if not getattr(args, "cmd", None):
        parser.print_help()
        return 0

    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 0

    try:
        return int(func(args) or 0)
    except KeyboardInterrupt:
        sys.stderr.write("\ninterrupted\n")
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
