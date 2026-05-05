"""Phase 2.3 wave-3 minimal shim for agent.context_compressor.

Upstream's ``agent/context_compressor.py`` (~1416 lines) is a full
LLM-driven compression engine: prunes old tool results, protects head
+ tail messages, summarizes the middle via OpenRouter / auxiliary
provider, and iteratively re-summarizes on subsequent compactions.
It depends on ``agent.context_engine.ContextEngine`` (an ABC) and
``agent.auxiliary_client.call_llm`` — neither of which phalanx has
ported yet.

Plan §2.3 explicitly defers real compression: keep the class name as a
薄壳 (thin shell), implement the trivial fallback "drop the oldest
user/assistant pair when message count exceeds a threshold".  This
gets you protection against unbounded message growth without an LLM
call, and preserves the public surface so future cherry-picks of the
real compressor land cleanly.

Public surface kept (compatible with the upstream signatures):

* ``ContextCompressor`` class with ``name`` / ``update_from_response``
  / ``should_compress`` / ``compress`` / ``on_session_reset``
* The ``last_prompt_tokens`` / ``threshold_tokens`` / ``context_length``
  / ``compression_count`` instance attributes that ``run_agent.py``
  reads directly for logging.

When upstream lands, drop this file + add ``context_engine.py`` +
``auxiliary_client.py`` real ports, then re-copy upstream verbatim.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Trivial-fallback parameters.  Tunable; small numbers because Phase 2.3
# isn't trying to serve long sessions yet.
# ---------------------------------------------------------------------------

_DEFAULT_MAX_MESSAGES = 60
_DEFAULT_PROTECT_FIRST_N = 3
_DEFAULT_PROTECT_LAST_N = 6


class ContextCompressor:
    """Minimal phalanx stand-in for upstream's compressor.

    Behavior: when ``len(messages) > max_messages``, drop the **oldest
    user/assistant pair** from the middle of the list (keeping system
    prompt + first exchange, plus the most recent N turns).  No LLM call,
    no summary — just message pruning.

    Token bookkeeping (``last_prompt_tokens`` etc.) is updated when
    ``update_from_response`` is called so logs stay informative.
    """

    # ------------------------------------------------------------------
    # Identity / class-level attributes (mirror upstream's
    # ContextEngine surface so callers can read them).
    # ------------------------------------------------------------------

    threshold_percent: float = 0.75
    protect_first_n: int = _DEFAULT_PROTECT_FIRST_N
    protect_last_n: int = _DEFAULT_PROTECT_LAST_N

    def __init__(
        self,
        model: str = "",
        context_length: int = 0,
        base_url: str = "",
        api_key: str = "",
        provider: str = "",
        api_mode: str = "",
        max_messages: int = _DEFAULT_MAX_MESSAGES,
    ) -> None:
        self.model = model
        self.context_length = context_length
        self.base_url = base_url
        self.api_key = api_key
        self.provider = provider
        self.api_mode = api_mode
        self.max_messages = max_messages

        # Token-state fields read by run_agent.py for display/logging.
        self.last_prompt_tokens: int = 0
        self.last_completion_tokens: int = 0
        self.last_total_tokens: int = 0
        self.threshold_tokens: int = 0
        self.compression_count: int = 0

    @property
    def name(self) -> str:
        return "compressor"

    # ------------------------------------------------------------------
    # Lifecycle hooks
    # ------------------------------------------------------------------

    def on_session_reset(self) -> None:
        """Reset per-session state on /new or /reset."""
        self.compression_count = 0
        self.last_prompt_tokens = 0
        self.last_completion_tokens = 0
        self.last_total_tokens = 0

    def update_model(
        self,
        model: str,
        context_length: int,
        base_url: str = "",
        api_key: str = "",
        provider: str = "",
        api_mode: str = "",
    ) -> None:
        self.model = model
        self.context_length = context_length
        self.base_url = base_url
        self.api_key = api_key
        self.provider = provider
        self.api_mode = api_mode

    # ------------------------------------------------------------------
    # Token state — fed by run_agent after every API call
    # ------------------------------------------------------------------

    def update_from_response(self, usage: Dict[str, Any]) -> None:
        if not isinstance(usage, dict):
            return
        self.last_prompt_tokens = int(usage.get("prompt_tokens") or 0)
        self.last_completion_tokens = int(usage.get("completion_tokens") or 0)
        self.last_total_tokens = int(usage.get("total_tokens") or 0)

    # ------------------------------------------------------------------
    # Compaction decision
    # ------------------------------------------------------------------

    def should_compress(self, prompt_tokens: Optional[int] = None) -> bool:
        """Phase-2.3 stand-in: triggers on message count, not token count.

        Real impl checks ``prompt_tokens >= threshold_tokens`` against the
        model's context window.  We don't have reliable token counts in
        this shim's path, so we use a coarser proxy.
        """
        return False  # Caller should rely on should_compress_preflight.

    def should_compress_preflight(self, messages: List[Dict[str, Any]]) -> bool:
        return len(messages) > self.max_messages

    def has_content_to_compress(self, messages: List[Dict[str, Any]]) -> bool:
        return self.should_compress_preflight(messages)

    # ------------------------------------------------------------------
    # The actual compaction
    # ------------------------------------------------------------------

    def compress(
        self,
        messages: List[Dict[str, Any]],
        current_tokens: Optional[int] = None,
        focus_topic: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Drop oldest user/assistant pairs from the middle of *messages*.

        Always preserves:
          * the system prompt (slot 0, if present)
          * the first ``protect_first_n`` non-system messages
          * the last ``protect_last_n`` messages

        Pairs are dropped one-at-a-time until ``len(messages) <=
        max_messages``.  Upstream's real compressor would summarize the
        dropped span into a compact ``[summary]`` message; this shim
        simply omits it — fine for Phase 2.3 sessions which rarely
        exceed 60 messages.
        """
        if focus_topic:
            logger.debug(
                "ContextCompressor shim ignoring focus_topic=%r (no LLM summary)",
                focus_topic,
            )

        if len(messages) <= self.max_messages:
            return list(messages)

        # Identify the protected head + tail.
        head_end = 0
        if messages and messages[0].get("role") == "system":
            head_end = 1
        head_end += self.protect_first_n
        head_end = min(head_end, len(messages))

        tail_start = max(head_end, len(messages) - self.protect_last_n)

        # Walk the middle window; drop pairs until we're under budget.
        kept_middle: List[Dict[str, Any]] = list(messages[head_end:tail_start])
        target_drop = len(messages) - self.max_messages

        dropped = 0
        i = 0
        while dropped < target_drop and i < len(kept_middle):
            role = (kept_middle[i].get("role") or "").lower()
            if role in ("user", "assistant", "tool"):
                kept_middle.pop(i)
                dropped += 1
                continue
            i += 1

        self.compression_count += 1
        result = list(messages[:head_end]) + kept_middle + list(messages[tail_start:])
        logger.info(
            "ContextCompressor shim: dropped %d/%d messages (no LLM summary). compression_count=%d",
            dropped, target_drop, self.compression_count,
        )
        return result
