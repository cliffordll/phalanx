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
from typing import Any, Dict, List, Optional

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
    provider: Optional[str] = None
    resume: Optional[str] = None  # session_id or unique prefix


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
    parser.add_argument("--provider", default=None,
                        choices=["openai-compatible", "anthropic", "bedrock", "codex", "gemini"],
                        help="force a specific provider (overrides base_url-based auto-detection)")
    parser.add_argument("--resume", default=None, metavar="SESSION_ID",
                        help="resume an existing session (full id or unique prefix); "
                             "history is loaded from ~/.hermes/state.db")

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
    p_one.add_argument("--dump-messages", action="store_true",
                       help="after replying, print the full messages array as JSON to stderr")
    p_one.add_argument("--dump-tools", action="store_true",
                       help="after replying, print the tools schema array as JSON to stderr")
    p_stream = p_one.add_mutually_exclusive_group()
    p_stream.add_argument("--stream", dest="stream", action="store_true", default=None,
                          help="enable streaming (text deltas printed live to stdout)")
    p_stream.add_argument("--no-stream", dest="stream", action="store_false",
                          help="disable streaming (single round-trip; default)")
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
    p_tools_schema = p_tools_sub.add_parser(
        "schema", help="dump a tool's JSON Schema"
    )
    p_tools_schema.add_argument("name", help="tool name")
    p_tools_schema.set_defaults(func=cmd_tools_schema)
    p_tools_dry = p_tools_sub.add_parser(
        "dry-run", help="validate --args against the tool's schema without invoking",
    )
    p_tools_dry.add_argument("name", help="tool name")
    p_tools_dry.add_argument("--args", default="{}",
                             help="JSON-encoded tool arguments to validate")
    p_tools_dry.set_defaults(func=cmd_tools_dry_run)
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

    # prompt -----------------------------------------------------------
    p_prompt = sub.add_parser("prompt", help="inspect the assembled system prompt")
    p_prompt_sub = p_prompt.add_subparsers(dest="prompt_cmd", metavar="<subcmd>")
    p_prompt_show = p_prompt_sub.add_parser("show",
        help="dump the system prompt that AIAgent would build for this cwd")
    p_prompt_show.add_argument("--raw", action="store_true",
                               help="skip project context-file discovery (just identity + env)")
    p_prompt_show.add_argument("--system", default=None,
                               help="optional caller --system override to splice in")
    p_prompt_show.add_argument("--cwd", default=None,
                               help="working directory used for context discovery (default: $PWD)")
    p_prompt_show.set_defaults(func=cmd_prompt_show)
    p_prompt.set_defaults(func=cmd_prompt_help)

    # model ------------------------------------------------------------
    p_model = sub.add_parser("model", help="inspect model metadata / context windows")
    p_model_sub = p_model.add_subparsers(dest="model_cmd", metavar="<subcmd>")
    p_model_list = p_model_sub.add_parser("list", help="list known model entries")
    p_model_list.set_defaults(func=cmd_model_list)
    p_model_info = p_model_sub.add_parser("info", help="show context window / pricing for one model")
    p_model_info.add_argument("name", help="model name (e.g. gpt-4o-mini, qwen2.5:1.5b)")
    p_model_info.add_argument("--base-url", default=None,
                              help="endpoint base_url (overrides config)")
    p_model_info.set_defaults(func=cmd_model_info)
    p_model_switch = p_model_sub.add_parser("switch", help="set the default model (writes ~/.phalanx/config.yaml)")
    p_model_switch.add_argument("name", help="model name to use as the new default")
    p_model_switch.set_defaults(func=cmd_model_switch)
    p_model.set_defaults(func=cmd_model_help)

    # provider ---------------------------------------------------------
    p_prov = sub.add_parser("provider", help="inspect / verify configured providers")
    p_prov_sub = p_prov.add_subparsers(dest="provider_cmd", metavar="<subcmd>")
    p_prov_list = p_prov_sub.add_parser("list", help="list adapters phalanx currently knows about")
    p_prov_list.set_defaults(func=cmd_provider_list)
    p_prov_test = p_prov_sub.add_parser("test", help="send a tiny ping to verify connectivity")
    p_prov_test.add_argument("name", nargs="?", default="openai-compatible",
                             help="provider name (default: openai-compatible)")
    p_prov_test.set_defaults(func=cmd_provider_test)
    p_prov.set_defaults(func=cmd_provider_help)

    # pricing ----------------------------------------------------------
    p_price = sub.add_parser("pricing", help="rough pricing utilities")
    p_price_sub = p_price.add_subparsers(dest="pricing_cmd", metavar="<subcmd>")
    p_price_est = p_price_sub.add_parser("estimate", help="estimate USD cost for a token usage")
    p_price_est.add_argument("--model", required=True, help="model name")
    p_price_est.add_argument("--input-tokens", type=int, default=0)
    p_price_est.add_argument("--output-tokens", type=int, default=0)
    p_price_est.add_argument("--cache-read-tokens", type=int, default=0)
    p_price_est.add_argument("--cache-write-tokens", type=int, default=0)
    p_price_est.add_argument("--base-url", default=None)
    p_price_est.set_defaults(func=cmd_pricing_estimate)
    p_price.set_defaults(func=cmd_pricing_help)

    # session ----------------------------------------------------------
    p_sess = sub.add_parser("session", help="list / inspect / dump / delete persisted sessions")
    p_sess_sub = p_sess.add_subparsers(dest="session_cmd", metavar="<subcmd>")
    p_sess_list = p_sess_sub.add_parser("list", help="list recent sessions")
    p_sess_list.add_argument("--limit", type=int, default=20,
                             help="max rows to print (default 20)")
    p_sess_list.add_argument("--source", default=None,
                             help="filter by source ('cli', 'gateway', ...)")
    p_sess_list.add_argument("--json", action="store_true",
                             help="emit JSON array instead of the table view")
    p_sess_list.set_defaults(func=cmd_session_list)
    p_sess_show = p_sess_sub.add_parser(
        "show", help="render messages for a session (id or unique prefix)"
    )
    p_sess_show.add_argument("target", help="session id or unique prefix")
    p_sess_show.set_defaults(func=cmd_session_show)
    p_sess_dump = p_sess_sub.add_parser(
        "dump", help="emit raw JSONL of all messages for a session"
    )
    p_sess_dump.add_argument("target", help="session id or unique prefix")
    p_sess_dump.set_defaults(func=cmd_session_dump)
    p_sess_del = p_sess_sub.add_parser(
        "delete", help="delete a session and all its messages"
    )
    p_sess_del.add_argument("target", help="session id or unique prefix")
    p_sess_del.add_argument("--yes", action="store_true",
                            help="skip the confirmation prompt")
    p_sess_del.set_defaults(func=cmd_session_delete)
    p_sess.set_defaults(func=cmd_session_help)

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
    """Construct an AIAgent honoring global + subcommand flags.

    Returns a tuple ``(agent, conversation_history)`` so callers that
    pass the history into ``run_conversation`` don't need to re-query
    the DB.  ``conversation_history`` is ``None`` outside the resume
    path (the common case).
    """
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

    # SessionDB is constructed unconditionally so every CLI turn lands
    # in ~/.hermes/state.db.  Without that, ``--resume`` has nothing to
    # recover.  Failures fall back to ephemeral so a misconfigured DB
    # path can't block the agent loop.
    session_db = None
    session_id: Optional[str] = None
    history: Optional[List[Dict[str, Any]]] = None
    try:
        from hermes_state import SessionDB
        session_db = SessionDB()
    except Exception as exc:
        logger.warning("session DB init failed; running ephemeral: %s", exc)
        session_db = None

    if _Flags.resume:
        if session_db is None:
            sys.stderr.write(
                "error: --resume requires a working session DB\n"
            )
            sys.exit(2)
        resolved = session_db.resolve_session_id(_Flags.resume)
        if not resolved:
            sys.stderr.write(
                f"error: --resume {_Flags.resume!r}: no matching session "
                "(or prefix is ambiguous)\n"
            )
            sys.exit(2)
        # Compression chains live in §2.7 territory but the helper
        # short-circuits cleanly when there's no chain, so it's safe to
        # call unconditionally.
        session_id = session_db.resolve_resume_session_id(resolved)
        history = session_db.get_messages_as_conversation(session_id)
        # The session was end_session'd at the close of its last
        # run_conversation; reopen it so the upcoming end_session call
        # records the new stop_reason instead of being silently no-op.
        session_db.reopen_session(session_id)

    agent = AIAgent(
        base_url=base_url,
        api_key=api_key,
        model=model,
        max_iterations=max_iterations,
        max_tokens=max_tokens,
        verbose_logging=_Flags.debug,
        quiet_mode=_Flags.quiet,
        ephemeral_system_prompt=system,
        provider=_Flags.provider,
        session_db=session_db,
        session_id=session_id,
    )
    return agent, history


# ── Subcommand handlers ────────────────────────────────────────────────


def cmd_oneshot(args: argparse.Namespace) -> int:
    msg = args.message or args.message_kw
    if not msg:
        sys.stderr.write("error: oneshot requires a message\n")
        return 2
    agent, history = _build_agent(
        args,
        max_iterations=args.max_iterations,
        max_tokens=args.max_tokens,
        system=args.system,
    )
    stream_callback = None
    if getattr(args, "stream", None):
        # --stream: pipe each text delta to stdout as it arrives.
        # The full final_response is still printed below, but for
        # tool-only turns the deltas may be empty (model went straight
        # to a tool_call) — that's expected.
        def stream_callback(delta: str) -> None:
            sys.stdout.write(delta)
            sys.stdout.flush()
    try:
        result = agent.run_conversation(
            msg,
            conversation_history=history,
            stream_callback=stream_callback,
        )
    finally:
        agent.close()
    if stream_callback is not None:
        # The stream already painted text to stdout; emit a newline so
        # the prompt doesn't collide with the next shell line.
        sys.stdout.write("\n")
    else:
        print(result.get("final_response", ""))
    if _Flags.debug:
        sys.stderr.write(
            f"\n[done] turns={result['api_calls']} stop={result['stop_reason']} "
            f"budget={result['iterations_used']}/{agent.max_iterations}\n"
        )
    if getattr(args, "dump_tools", False):
        # Tool schemas are not part of the messages array — they're sent
        # to the model via the OpenAI `tools=[...]` parameter.  Resolve
        # them through the same path run_conversation uses, so the dump
        # reflects what the model actually saw on this call.
        sys.stderr.write("\n--- tools ---\n")
        sys.stderr.write(json.dumps(
            agent._resolve_tool_schemas(), ensure_ascii=False, indent=2,
        ))
        sys.stderr.write("\n")
    if getattr(args, "dump_messages", False):
        # Goes to stderr so it doesn't pollute the captured final_response;
        # callers who pipe stdout into another tool still see the dump
        # interleaved when they want both (e.g. `... 2>&1 | jq ...`).
        sys.stderr.write("\n--- messages ---\n")
        sys.stderr.write(json.dumps(
            result.get("messages", []), ensure_ascii=False, indent=2,
        ))
        sys.stderr.write("\n")
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
    print("usage: hermes tools <list|run|schema|dry-run> ...")
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
    # Some tools (e.g. todo) require a per-session store that the agent
    # would normally own.  When invoked via `tools run` there is no
    # AIAgent, so spin up an ephemeral store local to this process.
    extra_kwargs: Dict[str, Any] = {}
    if args.name == "todo":
        try:
            from tools.todo_tool import TodoStore
            extra_kwargs["store"] = TodoStore()
        except Exception:
            pass
    result = dispatch(args.name, parsed_args, **extra_kwargs)
    if isinstance(result, str):
        print(result)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_tools_schema(args: argparse.Namespace) -> int:
    """Dump a single tool's JSON Schema as pretty-printed JSON."""
    registry = _load_tool_registry()
    if registry is None:
        sys.stderr.write("error: no tool registry loaded\n")
        return 2
    schema = registry.get_schema(args.name)
    if schema is None:
        sys.stderr.write(f"error: unknown tool {args.name!r}\n")
        return 2
    print(json.dumps(schema, ensure_ascii=False, indent=2))
    return 0


def cmd_tools_dry_run(args: argparse.Namespace) -> int:
    """Validate --args against the tool's JSON Schema without dispatching.

    Useful for catching missing required fields, wrong types, and JSON
    parse errors before invoking a tool with side effects (write_file,
    patch, terminal, …).
    """
    registry = _load_tool_registry()
    if registry is None:
        sys.stderr.write("error: no tool registry loaded\n")
        return 2

    schema = registry.get_schema(args.name)
    if schema is None:
        sys.stderr.write(f"error: unknown tool {args.name!r}\n")
        return 2

    try:
        parsed_args = json.loads(args.args)
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"error: --args must be valid JSON: {exc}\n")
        return 2
    if not isinstance(parsed_args, dict):
        sys.stderr.write("error: --args must decode to a JSON object\n")
        return 2

    # Tool schemas store the parameters object under "parameters" (OpenAI
    # function-calling convention); fall back to the top-level dict for
    # any registrations that already pre-extracted it.
    param_schema = schema.get("parameters") if isinstance(schema, dict) else None
    if not isinstance(param_schema, dict):
        param_schema = schema

    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        sys.stderr.write(
            "error: jsonschema is required for `tools dry-run`; "
            "run `pip install jsonschema` or reinstall phalanx.\n"
        )
        return 2

    try:
        validator = Draft202012Validator(param_schema)
    except Exception as exc:
        sys.stderr.write(f"error: tool {args.name!r} has invalid schema: {exc}\n")
        return 2

    errors = sorted(validator.iter_errors(parsed_args), key=lambda e: list(e.path))
    if not errors:
        print(json.dumps(
            {"tool": args.name, "valid": True, "args": parsed_args},
            ensure_ascii=False, indent=2,
        ))
        return 0

    formatted = []
    for err in errors:
        path = ".".join(str(p) for p in err.path) or "<root>"
        formatted.append({"path": path, "message": err.message})
    print(json.dumps(
        {"tool": args.name, "valid": False, "errors": formatted},
        ensure_ascii=False, indent=2,
    ))
    return 1


def cmd_prompt_help(args: argparse.Namespace) -> int:
    print("usage: hermes prompt <show> ...")
    return 0


def cmd_prompt_show(args: argparse.Namespace) -> int:
    """Dump the system prompt that AIAgent would assemble for this cwd.

    With --raw, skip project context-file discovery (.hermes.md, AGENTS.md,
    CLAUDE.md, .cursorrules) and just emit identity + environment hints.
    """
    from agent.prompt_builder import build_system_prompt
    prompt = build_system_prompt(
        user_system=args.system,
        ephemeral=None,
        cwd=args.cwd,
        include_context_files=not args.raw,
    )
    print(prompt)
    return 0


def cmd_model_help(args: argparse.Namespace) -> int:
    print("usage: hermes model <list|info> ...")
    return 0


def cmd_model_list(args: argparse.Namespace) -> int:
    """Dump the curated DEFAULT_CONTEXT_LENGTHS table.

    These are static fallbacks used when live probes (OpenRouter API,
    endpoint /models, Ollama /api/tags) all fail.  Sorted by descending
    context length so the most capable models surface first.
    """
    from agent.model_metadata import DEFAULT_CONTEXT_LENGTHS
    rows = sorted(
        DEFAULT_CONTEXT_LENGTHS.items(), key=lambda kv: (-kv[1], kv[0])
    )
    name_w = max(len(name) for name, _ in rows)
    print(f"{'model':<{name_w}}  {'ctx':>10}")
    print(f"{'-' * name_w}  {'-' * 10}")
    for name, ctx in rows:
        print(f"{name:<{name_w}}  {ctx:>10,}")
    print(f"\n{len(rows)} entries")
    return 0


def cmd_model_info(args: argparse.Namespace) -> int:
    """Show context window + (best-effort) pricing for one model name.

    For the context length we run get_model_context_length, which checks
    cache → live endpoint metadata → DEFAULT_CONTEXT_LENGTHS substring
    match → safe fallback.  For pricing we only consult the cached
    OpenRouter / endpoint metadata (no API call to avoid surprising
    network usage from a `model info` invocation).
    """
    from agent.model_metadata import (
        get_model_context_length,
        is_local_endpoint,
        DEFAULT_CONTEXT_LENGTHS,
    )
    from agent.usage_pricing import has_known_pricing, get_pricing_entry

    cfg = load_config()
    base_url = (
        args.base_url
        or os.environ.get("OPENAI_BASE_URL")
        or cfg_get(cfg, "model", "base_url", default="")
        or ""
    )
    api_key = os.environ.get("OPENAI_API_KEY") or ""

    print(f"model:    {args.name}")
    print(f"base_url: {base_url or '<unset>'}")
    print(f"local:    {is_local_endpoint(base_url) if base_url else 'unknown'}")

    try:
        ctx = get_model_context_length(args.name, base_url=base_url, api_key=api_key)
        print(f"context:  {ctx:,} tokens")
    except Exception as exc:
        print(f"context:  <probe failed: {exc}>")

    fallback = None
    for prefix, value in DEFAULT_CONTEXT_LENGTHS.items():
        if prefix.lower() in args.name.lower():
            fallback = (prefix, value)
            break
    if fallback:
        print(f"fallback: '{fallback[0]}' → {fallback[1]:,} (DEFAULT_CONTEXT_LENGTHS substring match)")

    if has_known_pricing(args.name, base_url=base_url, api_key=api_key):
        entry = get_pricing_entry(args.name, base_url=base_url, api_key=api_key)
        if entry:
            print(f"pricing:  input ${entry.input_cost_per_million}/1M  output ${entry.output_cost_per_million}/1M  (source: {entry.source})")
    else:
        print("pricing:  unknown")
    return 0


def cmd_model_switch(args: argparse.Namespace) -> int:
    """Persist a new default model to ~/.phalanx/config.yaml.

    Reads the existing config (if any), overwrites ``model.default``,
    and atomically rewrites the file.  This is the one CLI command that
    *writes* config — kept narrow on purpose so it can't accidentally
    clobber unrelated keys.
    """
    from hermes_cli.config import save_config
    cfg = load_config() or {}
    model_section = cfg.get("model")
    if not isinstance(model_section, dict):
        model_section = {}
    previous = model_section.get("default")
    model_section["default"] = args.name
    cfg["model"] = model_section
    save_config(cfg)
    if previous and previous != args.name:
        print(f"model.default: {previous} → {args.name}")
    else:
        print(f"model.default: {args.name}")
    return 0


def cmd_provider_help(args: argparse.Namespace) -> int:
    print("usage: hermes provider <list|test> ...")
    return 0


def cmd_provider_list(args: argparse.Namespace) -> int:
    """List the adapters phalanx currently knows about.

    The active row is whatever ``run_agent._detect_provider(base_url)``
    resolves to (overridable via ``--provider``).  Wave 1 wired the
    OpenAI-compatible path; wave 2 ported the anthropic adapter module;
    wave 3 dispatches anthropic API calls through the SDK
    (``_call_anthropic_messages``).  Streaming on the anthropic route is
    still non-streaming-only — the wire protocol differs from OpenAI's
    SSE and lands in wave 4.
    """
    from run_agent import _detect_provider

    cfg = load_config()
    base_url = (
        _Flags.base_url
        or os.environ.get("OPENAI_BASE_URL")
        or cfg_get(cfg, "model", "base_url", default="")
        or ""
    )
    detected = _Flags.provider or _detect_provider(base_url)

    statuses = {
        "openai-compatible": "wired (chat.completions, streaming)",
        "anthropic": "wired (messages.create + messages.stream)",
        "codex": "wired (responses.create + responses.stream)",
        "bedrock": "not yet ported",
        "gemini": "not yet ported",
    }
    print(f"base_url: {base_url or '<unset>'}")
    print(f"detected: {detected}" + ("  (forced via --provider)" if _Flags.provider else ""))
    print()
    print("adapters:")
    for name, status in statuses.items():
        marker = "  [active]" if name == detected else ""
        print(f"  {name:<20s} {status}{marker}")
    return 0


def cmd_provider_test(args: argparse.Namespace) -> int:
    """Send a tiny ping through the active provider to verify connectivity.

    Builds a one-off AIAgent (no tools, max_iterations=1, no streaming)
    and runs a single-turn conversation with a "ping" message.  Reports
    pass/fail with the round-trip latency.

    Routes by provider name (matched against ``run_agent._detect_provider``
    output): ``openai-compatible`` and ``anthropic`` are wired (§2.4 waves
    1 + 3); ``bedrock`` / ``codex`` / ``gemini`` are still rejected up
    front rather than letting them 404 / hang against the wrong endpoint.
    """
    _SUPPORTED = {"openai-compatible", "anthropic", "codex"}
    if args.name not in _SUPPORTED:
        sys.stderr.write(
            f"error: provider {args.name!r} is not yet wired up. "
            "Active providers in this build: " + ", ".join(sorted(_SUPPORTED)) + "\n"
        )
        return 2

    from run_agent import AIAgent
    cfg = load_config()
    model = (
        _Flags.model
        or os.environ.get("PHALANX_MODEL")
        or os.environ.get("OPENAI_MODEL")
        or cfg_get(cfg, "model", "default", default="")
        or ""
    )
    base_url = (
        _Flags.base_url
        or os.environ.get("OPENAI_BASE_URL")
        or cfg_get(cfg, "model", "base_url", default="")
        or ""
    )
    api_key = (
        _Flags.api_key
        or os.environ.get("OPENAI_API_KEY")
        or ""
    )
    if not model:
        sys.stderr.write("error: no model configured (set --model, PHALANX_MODEL, or model.default)\n")
        return 2

    print(f"provider: {args.name}")
    print(f"model:    {model}")
    print(f"base_url: {base_url or '<unset>'}")
    agent = AIAgent(
        base_url=base_url, api_key=api_key, model=model,
        max_iterations=1, quiet_mode=True,
        provider=args.name,
    )
    import time as _t
    t0 = _t.perf_counter()
    try:
        result = agent.run_conversation("ping — reply with the single word 'pong'")
    except Exception as exc:
        elapsed = _t.perf_counter() - t0
        sys.stderr.write(f"FAIL ({elapsed*1000:.0f} ms): {type(exc).__name__}: {exc}\n")
        return 1
    finally:
        agent.close()
    elapsed = _t.perf_counter() - t0
    reply = (result.get("final_response") or "").strip()
    if not reply:
        sys.stderr.write(f"FAIL ({elapsed*1000:.0f} ms): provider returned an empty reply\n")
        return 1
    print(f"reply:    {reply[:80]}{'...' if len(reply) > 80 else ''}")
    print(f"latency:  {elapsed*1000:.0f} ms")
    print("OK")
    return 0


def cmd_pricing_help(args: argparse.Namespace) -> int:
    print("usage: hermes pricing <estimate> ...")
    return 0


def cmd_pricing_estimate(args: argparse.Namespace) -> int:
    """Estimate USD cost for a hypothetical token usage."""
    from agent.usage_pricing import CanonicalUsage, estimate_usage_cost

    cfg = load_config()
    base_url = (
        args.base_url
        or os.environ.get("OPENAI_BASE_URL")
        or cfg_get(cfg, "model", "base_url", default="")
        or ""
    )
    api_key = os.environ.get("OPENAI_API_KEY") or ""

    usage = CanonicalUsage(
        input_tokens=args.input_tokens,
        output_tokens=args.output_tokens,
        cache_read_tokens=args.cache_read_tokens,
        cache_write_tokens=args.cache_write_tokens,
    )
    result = estimate_usage_cost(args.model, usage, base_url=base_url, api_key=api_key)

    print(f"model:    {args.model}")
    print(f"input:    {usage.input_tokens:>10,} tokens")
    print(f"output:   {usage.output_tokens:>10,} tokens")
    if usage.cache_read_tokens:
        print(f"cache-rd: {usage.cache_read_tokens:>10,} tokens")
    if usage.cache_write_tokens:
        print(f"cache-wr: {usage.cache_write_tokens:>10,} tokens")
    print(f"status:   {result.status}")
    print(f"source:   {result.source}")
    print(f"cost:     {result.label}")
    if result.notes:
        for note in result.notes:
            print(f"          note: {note}")
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


# ── session subcommand (Phase 2.5 wave 4) ──────────────────────────────


def _format_session_age(timestamp: Optional[float]) -> str:
    """Render a relative-age string like '3m ago' / '2h ago'."""
    if not timestamp:
        return "—"
    import time
    delta = time.time() - timestamp
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta / 60)}m ago"
    if delta < 86400:
        return f"{int(delta / 3600)}h ago"
    return f"{int(delta / 86400)}d ago"


def cmd_session_help(args: argparse.Namespace) -> int:
    print("usage: hermes session <list|show|dump|delete> ...")
    return 0


def cmd_session_list(args: argparse.Namespace) -> int:
    """Print the most recent persisted sessions."""
    from hermes_state import SessionDB
    try:
        db = SessionDB()
    except Exception as exc:
        sys.stderr.write(f"error: cannot open session DB: {exc}\n")
        return 2
    try:
        rows = db.list_sessions_rich(
            source=args.source, limit=args.limit, offset=0,
        )
    finally:
        db.close()
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("(no sessions persisted yet)")
        return 0
    # Compact human-readable table.  ID is shown as the first 8 chars
    # so users can copy/paste into ``--resume``.
    print(f"{'ID':10s} {'SOURCE':10s} {'MODEL':18s} {'MSGS':>5s}  "
          f"{'LAST':10s}  PREVIEW")
    for r in rows:
        sid = (r.get("id") or "")[:8]
        source = r.get("source") or "?"
        model = (r.get("model") or "?")[:18]
        msgs = r.get("message_count") or 0
        last = _format_session_age(r.get("last_active"))
        preview = r.get("preview") or ""
        print(f"{sid:10s} {source:10s} {model:18s} {msgs:>5d}  "
              f"{last:10s}  {preview}")
    return 0


def _resolve_session_target(target: str):
    """Open SessionDB + resolve target id-or-prefix; exit(2) on failure."""
    from hermes_state import SessionDB
    try:
        db = SessionDB()
    except Exception as exc:
        sys.stderr.write(f"error: cannot open session DB: {exc}\n")
        sys.exit(2)
    sid = db.resolve_session_id(target)
    if not sid:
        sys.stderr.write(
            f"error: session {target!r} not found "
            "(or prefix is ambiguous)\n"
        )
        db.close()
        sys.exit(2)
    return db, sid


def cmd_session_show(args: argparse.Namespace) -> int:
    """Pretty-print one session's metadata + messages."""
    db, sid = _resolve_session_target(args.target)
    try:
        sess = db._get_session_rich_row(sid)
        if sess is None:
            sys.stderr.write(f"error: session {sid!r} disappeared\n")
            return 2
        msgs = db.get_messages(sid)
    finally:
        db.close()

    print(f"id        : {sess['id']}")
    print(f"source    : {sess.get('source')}")
    print(f"model     : {sess.get('model')}")
    print(f"started   : {sess.get('started_at')}")
    print(f"ended     : {sess.get('ended_at')}  ({sess.get('end_reason') or '—'})")
    print(f"messages  : {sess.get('message_count')}  "
          f"(tool calls: {sess.get('tool_call_count')})")
    print(f"tokens    : in={sess.get('input_tokens')} "
          f"out={sess.get('output_tokens')} "
          f"cache_r={sess.get('cache_read_tokens')} "
          f"cache_w={sess.get('cache_write_tokens')} "
          f"reasoning={sess.get('reasoning_tokens')}")
    print(f"preview   : {sess.get('preview')}")
    print()
    for i, m in enumerate(msgs):
        role = m.get("role") or "?"
        content = m.get("content")
        if isinstance(content, list):
            content = json.dumps(content, ensure_ascii=False)
        elif content is None:
            content = ""
        # Trim very long content for readability — full payload is in
        # ``session dump``.
        content_str = str(content)
        if len(content_str) > 500:
            content_str = content_str[:497] + "..."
        print(f"--- [{i}] {role} ---")
        if m.get("tool_name"):
            print(f"  tool: {m['tool_name']}  call_id: {m.get('tool_call_id')}")
        if m.get("tool_calls"):
            print(f"  tool_calls: {json.dumps(m['tool_calls'], ensure_ascii=False)}")
        if content_str:
            print(content_str)
    return 0


def cmd_session_dump(args: argparse.Namespace) -> int:
    """Emit each message as one JSON line on stdout."""
    db, sid = _resolve_session_target(args.target)
    try:
        msgs = db.get_messages(sid)
    finally:
        db.close()
    for m in msgs:
        print(json.dumps(m, ensure_ascii=False, default=str))
    return 0


def cmd_session_delete(args: argparse.Namespace) -> int:
    """Remove a session and all its messages."""
    db, sid = _resolve_session_target(args.target)
    if not args.yes:
        sys.stderr.write(
            f"about to delete session {sid!r} and all its messages.\n"
            "pass --yes to confirm.\n"
        )
        db.close()
        return 1
    try:
        deleted = db.delete_session(sid)
    finally:
        db.close()
    if not deleted:
        sys.stderr.write(f"error: session {sid!r} could not be deleted\n")
        return 2
    print(f"deleted session {sid}")
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
    _Flags.provider = getattr(args, "provider", None)
    _Flags.resume = getattr(args, "resume", None)

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
