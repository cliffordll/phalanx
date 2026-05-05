#!/usr/bin/env python3
"""
Echo tool — Phase 1 smoke-test built-in.

A trivially-simple tool that echoes its input back, so the loop
(``run_conversation`` → tool dispatch → assistant turn → final reply)
can be exercised end-to-end without depending on file I/O, network,
shell, or any provider-specific behavior.

This file is phalanx-specific (not present in upstream hermes-agent).
The migration plan §2.1.4 explicitly listed adding a smoke tool as
acceptable: the alternative was carving a no-state subset out of
``tools/todo_tool.py``, but todo's per-AIAgent ``store`` parameter
plumbs through ``dispatch(name, args, **kwargs)`` which Phase 1
doesn't yet expose — the cleanest way to give the loop something to
exercise is a self-contained handler.  Replace / remove once a real
tool subset (file_tools, todo, web_tools) lands in §2.2.
"""

from typing import Any, Dict

from tools.registry import registry, tool_result


ECHO_SCHEMA = {
    "name": "echo",
    "description": (
        "Echo the supplied text back verbatim.  Use this to verify the "
        "tool-calling loop works.  Always returns the input unchanged "
        "alongside the call count for the current process."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "Text to echo back.",
            },
            "uppercase": {
                "type": "boolean",
                "description": "When true, return TEXT in upper case.",
                "default": False,
            },
        },
        "required": ["text"],
    },
}


_call_count = 0


def echo(args: Dict[str, Any], **_kwargs) -> str:
    """Handler for the ``echo`` tool.

    Returns a JSON string — every registry-dispatched handler must.
    Tracks a per-process invocation count so tests can assert that the
    loop actually called us (not just that the schema was registered).
    """
    global _call_count
    _call_count += 1

    text = str(args.get("text", "") or "")
    if args.get("uppercase"):
        text = text.upper()

    return tool_result(text=text, call_count=_call_count)


def check_echo_requirements() -> bool:
    """Echo tool has no external dependencies — always available."""
    return True


registry.register(
    name="echo",
    toolset="echo",
    schema=ECHO_SCHEMA,
    handler=echo,
    check_fn=check_echo_requirements,
    description="Echo the supplied text back verbatim (smoke-test tool).",
    emoji="🔁",
)
