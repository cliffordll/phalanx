"""Phase 2.3 wave-3 minimal shim for agent.memory_manager.

Upstream's ``agent/memory_manager.py`` (~557 lines) ships a full memory
subsystem: a pluggable ``MemoryProvider`` backend, sanitization regexes,
streaming scrubber state machine, and the ``MemoryManager`` class that
orchestrates persistent agent memory across turns / sessions.

Phalanx Phase 2.3 explicitly defers memory support (plan §2.3 lists
``memory_manager.py`` as **不移植本期**).  This shim keeps every public
symbol that ``run_agent.py`` and other modules might import, all
returning safe defaults (no scrubbing, no memory, no nudges):

* ``sanitize_context(text)``        → returns *text* unchanged
* ``StreamingContextScrubber``      → identity scrubber (``feed`` returns input)
* ``build_memory_context_block(s)`` → returns ``""`` (no memory injected)
* ``MemoryManager``                 → minimal class with ``has_tool`` /
  ``handle_tool_call`` / ``on_memory_write`` hooks all no-op

When upstream lands the real module, drop this file and re-copy upstream
verbatim — no call sites need adjustment.
"""

from __future__ import annotations

from typing import Any, Optional


def sanitize_context(text: str) -> str:
    """Phase-2.3 stand-in: no scrubbing performed.

    Real impl strips ``<memory-context>...``, ``<internal-note>...``, and
    fence tags (``</tool_call>`` etc.) that providers sometimes echo back.
    Without these regexes, provider replay surfaces stay verbatim — fine
    for a CLI agent without a memory layer.
    """
    return text or ""


class StreamingContextScrubber:
    """Identity scrubber.

    Real impl runs a chunk-aware state machine that holds back partial
    tag tails so a ``<memory-context>...`` span split across stream
    deltas doesn't leak its payload to the UI.  Phalanx doesn't stream
    yet (§2.4) and doesn't inject memory-context, so feeding through
    unchanged is correct.
    """

    def __init__(self) -> None:
        pass

    def feed(self, delta: str) -> str:
        return delta or ""

    def reset(self) -> None:
        return None


def build_memory_context_block(raw_context: str) -> str:
    """Phase-2.3 stand-in: emit empty block.

    Real impl wraps *raw_context* (loaded from the memory store) into the
    ``<memory-context>...</memory-context>`` envelope that
    ``MemoryProvider`` parses on the next turn.  Returning ``""`` means
    AIAgent loops never inject any memory into the system prompt — fine
    until the memory tool is wired up.
    """
    return ""


class MemoryManager:
    """Minimal MemoryManager stand-in.

    Real impl owns a ``MemoryProvider`` backend, registers per-tool
    handlers (``recall`` / ``store`` / ``forget``), and emits write
    notifications back to the provider.  This shim:

    * advertises no tools (``has_tool`` is False for everything)
    * raises if anyone tries to dispatch a memory tool through it
    * silently accepts ``on_memory_write`` notifications

    AIAgent owns one instance via ``self._memory_manager`` upstream.
    Phalanx doesn't construct it yet (Phase 2.3 keeps the field out of
    AIAgent.__init__), but we provide the class so future code that
    imports ``MemoryManager`` doesn't break.
    """

    def __init__(self, provider: Optional[Any] = None) -> None:
        self.provider = provider

    def has_tool(self, name: str) -> bool:
        return False

    def handle_tool_call(self, name: str, args: dict) -> str:
        from tools.registry import tool_error
        return tool_error(
            f"MemoryManager is a Phase-2.3 stand-in and cannot dispatch {name!r}. "
            "Memory support arrives in §2.7."
        )

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        *,
        metadata: Optional[dict] = None,
    ) -> None:
        return None
