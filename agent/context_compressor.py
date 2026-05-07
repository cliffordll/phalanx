"""Context compression for long-running agent loops (§2.8.b wave 2).

Replaces the Phase-2.3 message-pruning shim with a real summarisation
path: when the *prompt* token count for the next API call would cross
``threshold_tokens`` (= ``context_length × threshold_percent``), the
compressor calls :func:`agent.auxiliary_client.summarize_messages` on
the protected-middle window and substitutes the original turns with
one synthetic ``role=system`` message containing the summary.  The
head (system + first few turns) and tail (most recent N turns) stay
verbatim.

Falls back to message pruning (drop oldest user/assistant turns from
the middle) when the auxiliary client is unavailable or the
summarisation call returns ``None``.  This keeps the agent loop
responsive even when no separate auxiliary backend is configured —
the default zero-config install still gets bounded message lists.

Public surface (kept stable across the shim → real upgrade):

* :class:`ContextCompressor` with ``model`` / ``context_length`` /
  ``threshold_tokens`` / ``compression_count`` /
  ``last_prompt_tokens`` instance attrs.
* ``should_compress(prompt_tokens)`` — token-aware preflight check.
* ``should_compress_preflight(messages)`` — message-count tripwire so
  callers without a token estimate can still bound the messages list.
* ``compress(messages, current_tokens, focus_topic)`` — returns a new
  messages list.
* ``update_from_response(usage)`` — latch the most recent
  prompt/completion/total token counts for logging / preflight.
* ``update_model(model, context_length, …)`` — swap models mid-session.
* ``on_session_reset()`` — zero counters on /new or /reset.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

_DEFAULT_THRESHOLD_PERCENT = 0.7
_DEFAULT_PROTECT_FIRST_N = 3
_DEFAULT_PROTECT_LAST_N = 6
# Hard floor on message count — when the messages list is shorter than
# this, compression is a no-op regardless of token estimate.  Avoids
# pathological cases where the system prompt alone blows the threshold
# (which compressing the empty middle window can't help with).
_DEFAULT_MIN_MESSAGES = 12

# Marker that prefixes every synthetic summary message so subsequent
# compressions can recognise their own output and incrementally rewrite
# rather than re-summarise the same text.
_SUMMARY_PREFIX = "[context-summary]"


def _is_summary_message(msg: Dict[str, Any]) -> bool:
    if msg.get("role") != "system":
        return False
    content = msg.get("content")
    return isinstance(content, str) and content.startswith(_SUMMARY_PREFIX)


# ---------------------------------------------------------------------------
# Compressor
# ---------------------------------------------------------------------------

class ContextCompressor:
    """Token-aware summarising compressor.

    Construct with ``model`` + ``context_length`` (resolved by the
    agent via :mod:`agent.model_metadata`).  Provide a
    ``client_factory`` callable that returns ``(client, model)`` —
    typically :func:`agent.auxiliary_client.get_text_auxiliary_client`
    bound with the calling agent's *main_runtime* hints.  When
    ``client_factory`` is ``None`` the compressor falls straight to
    pruning.

    Threshold maths::

        threshold_tokens = int(context_length × threshold_percent)

    Trigger logic::

        should_compress(prompt_tokens) :=
            context_length > 0 and
            prompt_tokens > 0 and
            prompt_tokens >= threshold_tokens

    The fallback ``should_compress_preflight(messages)`` is intended
    for callers that have no live token count — it triggers on raw
    message-count alone (>= 60 by default).
    """

    name = "compressor"

    def __init__(
        self,
        model: str = "",
        context_length: int = 0,
        *,
        threshold_percent: float = _DEFAULT_THRESHOLD_PERCENT,
        protect_first_n: int = _DEFAULT_PROTECT_FIRST_N,
        protect_last_n: int = _DEFAULT_PROTECT_LAST_N,
        min_messages: int = _DEFAULT_MIN_MESSAGES,
        max_messages: int = 60,
        base_url: str = "",
        api_key: str = "",
        provider: str = "",
        api_mode: str = "",
        client_factory: Optional[
            Callable[[], Tuple[Optional[Any], Optional[str]]]
        ] = None,
    ) -> None:
        self.model = model
        self.context_length = max(0, int(context_length))
        self.base_url = base_url
        self.api_key = api_key
        self.provider = provider
        self.api_mode = api_mode

        self.threshold_percent = float(threshold_percent)
        self.protect_first_n = int(protect_first_n)
        self.protect_last_n = int(protect_last_n)
        self.min_messages = int(min_messages)
        self.max_messages = int(max_messages)

        self.client_factory = client_factory

        # Token-state fields (read by run_agent for /status display).
        self.last_prompt_tokens: int = 0
        self.last_completion_tokens: int = 0
        self.last_total_tokens: int = 0
        self.compression_count: int = 0

    # ------------------------------------------------------------------
    # Threshold maths
    # ------------------------------------------------------------------

    @property
    def threshold_tokens(self) -> int:
        if self.context_length <= 0:
            return 0
        return int(self.context_length * self.threshold_percent)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_session_reset(self) -> None:
        """Reset per-session counters on /new or /reset."""
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
        self.context_length = max(0, int(context_length))
        if base_url:
            self.base_url = base_url
        if api_key:
            self.api_key = api_key
        if provider:
            self.provider = provider
        if api_mode:
            self.api_mode = api_mode

    def update_from_response(self, usage: Any) -> None:
        """Latch the most recent prompt/completion/total token counts.

        Accepts either a dict-shaped usage block or an SDK usage object
        (``CompletionUsage`` from the openai package exposes the same
        attribute names).
        """
        if usage is None:
            return
        get = (
            usage.get if isinstance(usage, dict)
            else lambda k, default=0: getattr(usage, k, default)
        )
        try:
            self.last_prompt_tokens = int(get("prompt_tokens", 0) or 0)
            self.last_completion_tokens = int(get("completion_tokens", 0) or 0)
            self.last_total_tokens = int(get("total_tokens", 0) or 0)
        except (TypeError, ValueError):
            # Don't crash on a malformed usage block — bookkeeping is
            # purely advisory.
            logger.debug("ContextCompressor: ignoring malformed usage=%r", usage)

    # ------------------------------------------------------------------
    # Trigger checks
    # ------------------------------------------------------------------

    def should_compress(self, prompt_tokens: Optional[int] = None) -> bool:
        """Token-based preflight check.

        Returns True when ``prompt_tokens`` (live estimate) crosses
        ``threshold_tokens``.  Falsy estimates / missing context_length
        return False — caller can fall back to the message-count
        tripwire.
        """
        if self.context_length <= 0:
            return False
        if prompt_tokens is None:
            prompt_tokens = self.last_prompt_tokens
        if prompt_tokens <= 0:
            return False
        return prompt_tokens >= self.threshold_tokens

    def should_compress_preflight(self, messages: List[Dict[str, Any]]) -> bool:
        """Fallback message-count check used when no token estimate is
        available."""
        return len(messages) > self.max_messages

    def has_content_to_compress(self, messages: List[Dict[str, Any]]) -> bool:
        """True iff the protected-middle window is non-empty.

        Compression is pointless when head + tail already cover the
        whole list — the LLM call would just paraphrase the same
        content.
        """
        if len(messages) < self.min_messages:
            return False
        head_end, tail_start = self._protected_window(messages)
        return tail_start > head_end

    # ------------------------------------------------------------------
    # The actual compaction
    # ------------------------------------------------------------------

    def _protected_window(
        self, messages: List[Dict[str, Any]]
    ) -> Tuple[int, int]:
        """Return ``(head_end, tail_start)`` indexing the middle window
        that is eligible for summarisation.

        Always preserves the leading system message (slot 0) plus
        ``protect_first_n`` non-system messages, and the last
        ``protect_last_n`` messages of the list.  When the head and
        tail overlap (very short list) ``tail_start`` is clamped to
        ``head_end`` so the middle window is empty.
        """
        head_end = 0
        if messages and messages[0].get("role") == "system":
            head_end = 1
        head_end += self.protect_first_n
        head_end = min(head_end, len(messages))
        tail_start = max(head_end, len(messages) - self.protect_last_n)
        return head_end, tail_start

    def compress(
        self,
        messages: List[Dict[str, Any]],
        current_tokens: Optional[int] = None,
        focus_topic: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return a new messages list with the middle window replaced
        by a single ``[context-summary]`` synthetic system message.

        Falls back to oldest-pair pruning when:

        * ``client_factory`` is ``None``,
        * the auxiliary client returns ``(None, None)``,
        * :func:`summarize_messages` returns ``None``,
        * any unexpected exception is raised inside the LLM call.

        Either path increments ``compression_count`` so /status can
        surface "compressed N times this session".

        Returns the messages list unchanged when there is nothing to
        compress (head + tail cover the whole list).
        """
        if not self.has_content_to_compress(messages):
            return list(messages)

        head_end, tail_start = self._protected_window(messages)
        middle = list(messages[head_end:tail_start])

        # Filter out previous summary messages from the middle slice —
        # we'll merge their content into the new summary so successive
        # compressions don't accumulate ``[context-summary]`` blocks.
        prior_summaries = [m for m in middle if _is_summary_message(m)]
        live_middle = [m for m in middle if not _is_summary_message(m)]
        if not live_middle:
            return list(messages)

        summary_text = self._summarise(
            live_middle, prior_summaries, focus_topic=focus_topic
        )

        if summary_text is None:
            # Auxiliary unavailable / failed → drop oldest middle turns
            # to bound the messages list.  Pruning preserves the protected
            # head + tail just like summarisation does.
            return self._prune(messages, head_end, tail_start)

        synthetic = {
            "role": "system",
            "content": (
                f"{_SUMMARY_PREFIX} The following summarises "
                f"{len(live_middle)} earlier turn(s) that were "
                f"compressed to fit the context window:\n\n"
                f"{summary_text}"
            ),
        }
        new_messages = (
            list(messages[:head_end])
            + [synthetic]
            + list(messages[tail_start:])
        )
        self.compression_count += 1
        logger.info(
            "ContextCompressor: summarised %d middle turn(s) into one "
            "synthetic system message (compression_count=%d)",
            len(live_middle), self.compression_count,
        )
        return new_messages

    def _summarise(
        self,
        live_middle: List[Dict[str, Any]],
        prior_summaries: List[Dict[str, Any]],
        *,
        focus_topic: Optional[str],
    ) -> Optional[str]:
        """Produce a summary string, or ``None`` to signal fall-back.

        Combines any prior ``[context-summary]`` blocks with the live
        middle slice so the auxiliary LLM sees the complete
        compressed-so-far context.
        """
        if self.client_factory is None:
            return None
        try:
            client, aux_model = self.client_factory()
        except Exception as exc:
            logger.warning("auxiliary client_factory raised: %s", exc)
            return None
        if client is None or not aux_model:
            return None

        # Prepend prior summaries (if any) as context for the
        # incremental summary — preserves long-running session memory
        # without re-summarising the same span repeatedly.
        slice_for_summary: List[Dict[str, Any]] = []
        for s in prior_summaries:
            slice_for_summary.append(
                {"role": "system", "content": s.get("content", "")}
            )
        slice_for_summary.extend(live_middle)

        try:
            from agent.auxiliary_client import summarize_messages
            return summarize_messages(
                client,
                aux_model,
                slice_for_summary,
                focus_topic=focus_topic,
            )
        except Exception as exc:
            logger.warning("summarize_messages raised: %s", exc)
            return None

    def _prune(
        self,
        messages: List[Dict[str, Any]],
        head_end: int,
        tail_start: int,
    ) -> List[Dict[str, Any]]:
        """Fallback path: drop oldest user/assistant/tool turns from
        the middle window until the messages list shrinks back under
        ``max_messages``.  Tool messages are dropped too because a
        bare ``role=tool`` message after pruning the assistant turn
        that emitted its tool_call is invalid for OpenAI/Anthropic
        replay.
        """
        kept_middle = list(messages[head_end:tail_start])
        target_drop = max(0, len(messages) - self.max_messages)

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
        result = (
            list(messages[:head_end])
            + kept_middle
            + list(messages[tail_start:])
        )
        logger.info(
            "ContextCompressor: pruned %d/%d middle turn(s) (no LLM "
            "summary). compression_count=%d",
            dropped, target_drop, self.compression_count,
        )
        return result
