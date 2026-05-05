#!/usr/bin/env python3
"""
AI Agent Runner with Tool Calling — Phase 1 minimal port.

This is the phalanx Phase-1 cut-down of hermes-agent's run_agent.py.
It keeps the public entry points (``AIAgent`` class, ``run_conversation``,
``chat``, module-level ``main``, ``IterationBudget``, ``OpenAI`` lazy
proxy) so callers / tests written against the upstream interface work
unchanged.  Removed (will be reintroduced in later phases):

  - Multi-provider adapters (anthropic / bedrock / codex / gemini)
  - Streaming path, prompt caching, context compression
  - Credential pool, fallback runtime, ACP transport
  - Tool guardrails, checkpoints, steer, skill injection
  - Memory prefetch, trajectory persistence, surrogate sanitization

Usage:
    from run_agent import AIAgent
    agent = AIAgent(base_url="https://api.openai.com/v1", model="gpt-4o-mini")
    result = agent.run_conversation("Hello!")
    print(result["final_response"])
"""

import copy
import json
import logging
import os
import sys
import threading
import time
import uuid
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# Lazy import of OpenAI SDK — see _OpenAIProxy.
# Keeps cold-start fast and lets test code patch ``run_agent.OpenAI``.
_OPENAI_CLS_CACHE: Optional[type] = None


def _load_openai_cls() -> type:
    """Import and cache ``openai.OpenAI``."""
    global _OPENAI_CLS_CACHE
    if _OPENAI_CLS_CACHE is None:
        from openai import OpenAI as _cls
        _OPENAI_CLS_CACHE = _cls
    return _OPENAI_CLS_CACHE


class _OpenAIProxy:
    """Module-level proxy that looks like ``openai.OpenAI`` but imports lazily."""

    __slots__ = ()

    def __call__(self, *args, **kwargs):
        return _load_openai_cls()(*args, **kwargs)

    def __instancecheck__(self, obj):
        return isinstance(obj, _load_openai_cls())

    def __repr__(self):
        return "<lazy openai.OpenAI proxy>"


OpenAI = _OpenAIProxy()


# ── Stdio safety wrapper ────────────────────────────────────────────────

class _SafeWriter:
    """Transparent stdio wrapper that catches OSError/ValueError from broken pipes.

    When the agent runs as a daemon / Docker / piped subprocess, stdout
    can become unavailable mid-write.  This wrapper silently swallows
    OSError and ValueError so a print() inside an except handler can't
    double-fault the process.
    """

    __slots__ = ("_inner",)

    def __init__(self, inner):
        object.__setattr__(self, "_inner", inner)

    def write(self, data):
        try:
            return self._inner.write(data)
        except (OSError, ValueError):
            return len(data) if isinstance(data, str) else 0

    def flush(self):
        try:
            self._inner.flush()
        except (OSError, ValueError):
            pass

    def fileno(self):
        return self._inner.fileno()

    def isatty(self):
        try:
            return self._inner.isatty()
        except (OSError, ValueError):
            return False

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _install_safe_stdio() -> None:
    """Wrap stdout/stderr so best-effort console output cannot crash the agent."""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and not isinstance(stream, _SafeWriter):
            setattr(sys, stream_name, _SafeWriter(stream))


# ── Iteration budget ────────────────────────────────────────────────────

class IterationBudget:
    """Thread-safe iteration counter for an agent.

    Each agent gets its own ``IterationBudget`` capped at
    ``max_iterations`` (default 90).  Subagents inherit the parent's
    budget so tool-driven subagent fan-out can't bypass the cap.
    """

    def __init__(self, max_total: int):
        self.max_total = max_total
        self._used = 0
        self._lock = threading.Lock()

    def consume(self) -> bool:
        """Try to consume one iteration.  Returns True if allowed."""
        with self._lock:
            if self._used >= self.max_total:
                return False
            self._used += 1
            return True

    def refund(self) -> None:
        """Give back one iteration (rarely used in Phase 1)."""
        with self._lock:
            if self._used > 0:
                self._used -= 1

    @property
    def used(self) -> int:
        return self._used

    @property
    def remaining(self) -> int:
        with self._lock:
            return max(0, self.max_total - self._used)


# ── Concurrent-tool gating ──────────────────────────────────────────────
# These three sets steer ``_should_parallelize_tool_batch``.  Upstream
# populates them with read-only tools, path-scoped editors, and tools that
# must always run sequentially (e.g. ``terminal``).  phalanx leaves them
# empty so the gate ALWAYS returns False and every batch falls through to
# the sequential path — correct behavior until Phase 7+ wires up the
# concurrent executor.  Names match upstream so a future cherry-pick can
# fill them in without changing call sites.
_NEVER_PARALLEL_TOOLS: set = set()
_PARALLEL_SAFE_TOOLS: set = set()
_PATH_SCOPED_TOOLS: set = set()


def _should_parallelize_tool_batch(tool_calls) -> bool:
    """Return True when a tool-call batch is safe to run concurrently.

    Phalanx Phase 2.x stand-in: signature mirrors upstream's path-overlap
    aware check, but with empty allow-lists the function reduces to
    "always False".  ``_execute_tool_calls`` therefore always picks the
    sequential path.  When upstream's full implementation lands the
    function body can be replaced verbatim — call sites already match.
    """
    if not tool_calls or len(tool_calls) <= 1:
        return False
    tool_names = [getattr(tc.function, "name", "") for tc in tool_calls]
    if any(name in _NEVER_PARALLEL_TOOLS for name in tool_names):
        return False
    return all(name in _PARALLEL_SAFE_TOOLS for name in tool_names)


# ── Streaming accumulator ───────────────────────────────────────────────
# Accumulate ChatCompletion stream chunks into a non-streaming-shaped
# response object so the rest of run_conversation can stay unchanged.
# Each chunk's text delta is forwarded to *callback* live, but we still
# need a complete object at the end to extract tool_calls / finish_reason
# and append the assistant message to the history.

def _accumulate_stream(stream, callback: Callable[[str], None]) -> Any:
    """Drain a ChatCompletion stream into a non-streaming-shaped object.

    OpenAI streams emit chunks where each chunk's ``choices[0].delta``
    has a slice of ``content`` and/or a slice of one or more
    ``tool_calls``.  Tool-call slices are indexed: a single tool call
    arrives as multiple chunks where ``id`` shows up first, then
    ``function.name``, then ``function.arguments`` typed out token by
    token.  We rebuild the full assistant message by indexing on
    ``tc_delta.index`` and concatenating the per-field strings.

    Returns a ``SimpleNamespace`` that quacks like
    ``client.chat.completions.create(stream=False)`` for the attribute
    accesses run_conversation actually performs:
    ``response.choices[0].message.{content, tool_calls, role}`` plus
    ``response.choices[0].finish_reason``.
    """
    content_parts: list[str] = []
    tool_calls_acc: dict[int, dict[str, Any]] = {}
    finish_reason: Optional[str] = None

    for chunk in stream:
        choices = getattr(chunk, "choices", None) or []
        if not choices:
            continue
        choice = choices[0]
        delta = getattr(choice, "delta", None)
        if delta is None:
            continue

        delta_text = getattr(delta, "content", None)
        if delta_text:
            content_parts.append(delta_text)
            try:
                callback(delta_text)
            except Exception:
                logger.exception("stream callback failed; continuing accumulation")

        tc_deltas = getattr(delta, "tool_calls", None) or []
        for tc_delta in tc_deltas:
            idx = getattr(tc_delta, "index", None)
            if idx is None:
                idx = len(tool_calls_acc)
            slot = tool_calls_acc.setdefault(
                idx, {"id": None, "name": "", "arguments_parts": []}
            )
            tc_id = getattr(tc_delta, "id", None)
            if tc_id:
                slot["id"] = tc_id
            fn = getattr(tc_delta, "function", None)
            if fn is not None:
                fn_name = getattr(fn, "name", None)
                if fn_name:
                    slot["name"] = fn_name
                fn_args = getattr(fn, "arguments", None)
                if fn_args:
                    slot["arguments_parts"].append(fn_args)

        choice_finish = getattr(choice, "finish_reason", None)
        if choice_finish:
            finish_reason = choice_finish

    content = "".join(content_parts) or None
    if tool_calls_acc:
        tool_calls = [
            SimpleNamespace(
                id=slot["id"],
                type="function",
                function=SimpleNamespace(
                    name=slot["name"],
                    arguments="".join(slot["arguments_parts"]) or "{}",
                ),
            )
            for _idx, slot in sorted(tool_calls_acc.items())
        ]
    else:
        tool_calls = None

    message = SimpleNamespace(
        role="assistant",
        content=content,
        tool_calls=tool_calls,
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
    )


# ── Anthropic response normalization ────────────────────────────────────
# Wave 3 light path: take whatever ``Anthropic.messages.create`` returned
# and dress it up as an OpenAI ChatCompletion so the rest of the agent
# loop is provider-agnostic.  Mirrors ``agent/transports/anthropic.py``'s
# ``normalize_response`` upstream, but flattened — phalanx has no
# transport ABC / NormalizedResponse dataclass yet, so we emit the same
# SimpleNamespace shape that ``_accumulate_stream`` produces.

# Anthropic stop_reason → OpenAI finish_reason mapping (kept in sync with
# upstream agent/transports/anthropic.py:_STOP_REASON_MAP).
_ANTHROPIC_STOP_TO_OPENAI = {
    "end_turn": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
    "stop_sequence": "stop",
    "refusal": "content_filter",
    "model_context_window_exceeded": "length",
}


# ── Codex Responses API normalization ───────────────────────────────────
# Wave 5 light path: take whatever ``client.responses.create`` returned
# and dress it up as an OpenAI ChatCompletion so the rest of the agent
# loop is provider-agnostic.  ``_normalize_codex_response`` already does
# 90% of the work — it produces an assistant_message SimpleNamespace
# with .content / .tool_calls plus a finish_reason string; we just have
# to wrap that in the .choices[0] container.

def _codex_response_to_openai_shape(response: Any) -> Any:
    """Wrap a Responses API output in the OpenAI ChatCompletion shape.

    ``_normalize_codex_response`` extracts content_parts / reasoning /
    function_call / custom_tool_call output items into a synthetic
    assistant message and computes a finish_reason.  We promote it to
    the ``.choices[0].message`` slot so ``run_conversation`` doesn't
    have to know the difference.
    """
    from agent.codex_responses_adapter import _normalize_codex_response

    assistant_message, finish_reason = _normalize_codex_response(response)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=assistant_message, finish_reason=finish_reason)],
    )


def _accumulate_codex_stream(
    stream_ctx: Any,
    callback: Callable[[str], None],
) -> Any:
    """Drain a Responses API stream, firing ``callback`` per text delta.

    ``stream_ctx`` is the value returned from ``client.responses.stream(**kw)``
    — a context manager that yields ``response.*`` events on iteration.
    We forward ``response.output_text.delta`` events (the user-visible
    answer tokens) to the callback live; reasoning / function_call /
    output_item.done events are left to the SDK's
    ``stream.get_final_response()`` which assembles the canonical
    Response with all output items already populated.

    Returns the same ``SimpleNamespace`` shape as the non-streaming path
    so the run loop in ``run_conversation`` doesn't need to branch on
    whether streaming was used.
    """
    with stream_ctx as stream:
        for event in stream:
            event_type = getattr(event, "type", "") or ""
            # gpt-5 / o1 over native /v1/responses uses
            # "response.output_text.delta"; some backends drop the prefix.
            # Match either form.
            if "output_text.delta" not in event_type:
                continue
            delta_text = getattr(event, "delta", "")
            if not delta_text:
                continue
            try:
                callback(delta_text)
            except Exception:
                logger.exception("codex stream callback failed; continuing accumulation")
        final = stream.get_final_response()
    return _codex_response_to_openai_shape(final)


def _accumulate_anthropic_stream(
    stream_ctx: Any,
    callback: Callable[[str], None],
) -> Any:
    """Drain an Anthropic Messages stream, firing ``callback`` per text delta.

    ``stream_ctx`` is the value returned from ``client.messages.stream(**kw)``
    — a context manager that yields ``content_block_*`` / ``message_*``
    events from ``__iter__``.  We forward ``text_delta`` events to the
    live callback so a TTY consumer sees tokens as they arrive; everything
    else (tool_use input_json_delta, thinking_delta) is left to the SDK's
    ``stream.get_final_message()`` which assembles the canonical Message
    object.

    Returns the same ``SimpleNamespace`` shape as the non-streaming path so
    the run loop in ``run_conversation`` doesn't need to branch on whether
    streaming was used.
    """
    with stream_ctx as stream:
        for event in stream:
            if getattr(event, "type", None) != "content_block_delta":
                continue
            delta = getattr(event, "delta", None)
            if delta is None:
                continue
            if getattr(delta, "type", None) != "text_delta":
                continue
            text = getattr(delta, "text", "")
            if not text:
                continue
            try:
                callback(text)
            except Exception:
                logger.exception("anthropic stream callback failed; continuing accumulation")
        final = stream.get_final_message()
    return _anthropic_response_to_openai_shape(final)


def _anthropic_response_to_openai_shape(response: Any) -> Any:
    """Wrap an Anthropic Messages response in the OpenAI ChatCompletion shape.

    The run loop reads only:
      response.choices[0].message.content       (str | None)
      response.choices[0].message.tool_calls    (list | None)
      response.choices[0].finish_reason         (str | None)

    plus ``_serialize_tool_calls`` walks each tool_call's ``id`` /
    ``function.name`` / ``function.arguments``.  This converter populates
    exactly those fields and nothing else; reasoning blocks are dropped on
    the floor for now (they show up if/when the assistant_message builder
    in §2.4 wave 4 lands — see docs/MIGRATION_PLAN.md §2.4).
    """
    text_parts: list[str] = []
    tool_calls: list[Any] = []

    for block in getattr(response, "content", None) or []:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            text_parts.append(getattr(block, "text", "") or "")
        elif block_type == "tool_use":
            args = getattr(block, "input", None)
            tool_calls.append(SimpleNamespace(
                id=getattr(block, "id", None),
                type="function",
                function=SimpleNamespace(
                    name=getattr(block, "name", "") or "",
                    arguments=json.dumps(args if args is not None else {}),
                ),
            ))
        # thinking / redacted_thinking blocks are intentionally ignored
        # here — wave 4 will pick them up via _build_assistant_message.

    raw_stop = getattr(response, "stop_reason", None)
    finish_reason = _ANTHROPIC_STOP_TO_OPENAI.get(raw_stop, "stop")

    message = SimpleNamespace(
        role="assistant",
        content="".join(text_parts) or None,
        tool_calls=tool_calls or None,
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
    )


# ── Provider detection ──────────────────────────────────────────────────
# Inspect the base_url to guess which adapter should handle a turn.
# Phase 2.4 wave 2 only uses this to advertise an "active" provider in
# `hermes provider list`; full SDK routing inside _call_chat_completions
# is gated by §2.4 wave 3 (driven by an actual Claude usage signal).

_ANTHROPIC_HOSTS = ("api.anthropic.com",)
_BEDROCK_HOSTS = ("bedrock-runtime",)  # appears as bedrock-runtime.<region>.amazonaws.com
_GEMINI_HOSTS = ("generativelanguage.googleapis.com", "aiplatform.googleapis.com")
_CODEX_HOSTS = ("api.openai.com/v1/responses",)


def _detect_provider(base_url: str) -> str:
    """Map a base_url to a provider name.

    Defaults to ``"openai-compatible"`` (which serves OpenAI proper,
    Ollama, vLLM, LM Studio, Together, Groq, …).  Returns ``"anthropic"``
    only when the URL points at api.anthropic.com — Bedrock / Vertex
    flavours of Claude need their own adapters and aren't detected here.
    """
    url = (base_url or "").lower()
    if not url:
        return "openai-compatible"
    for host in _ANTHROPIC_HOSTS:
        if host in url:
            return "anthropic"
    for host in _BEDROCK_HOSTS:
        if host in url:
            return "bedrock"
    for host in _GEMINI_HOSTS:
        if host in url:
            return "gemini"
    for host in _CODEX_HOSTS:
        if host in url:
            return "codex"
    return "openai-compatible"


# ── Tool registry plumbing ──────────────────────────────────────────────
# Phase 2.1.4 will provide tools.registry.  Until then ``_load_tool_registry``
# returns None and the agent runs in tool-less mode (still a valid loop).

def _load_tool_registry():
    """Return the singleton ``tools.registry.registry``, or None.

    Importing ``tools.registry`` runs ``tools/__init__.py`` first, which
    triggers each built-in tool module's top-level ``registry.register(...)``
    call.  By the time we return, all built-in tools are already registered.
    """
    try:
        from tools.registry import registry  # type: ignore[import-not-found]
        # Force tools package init so self-registering modules load.
        import tools  # type: ignore[import-not-found]  # noqa: F401
        return registry
    except ImportError:
        return None


# ── Optional integrations (best-effort lazy imports) ────────────────────

def _set_session_log_context(session_id: str) -> None:
    """Tag log records on this thread with the session id, if hermes_logging is available."""
    try:
        from hermes_logging import set_session_context
        set_session_context(session_id)
    except Exception:
        pass


def _classify_error(exc: Exception, *, provider: str = "", model: str = ""):
    """Classify an API exception, falling back to a generic retryable verdict."""
    try:
        from agent.error_classifier import classify_api_error
        return classify_api_error(exc, provider=provider, model=model)
    except Exception:
        # Minimal fallback so the loop still works without error_classifier.
        class _Fallback:
            retryable = True
            should_compress = False
            should_rotate_credential = False
        return _Fallback()


def _retry_delay(attempt: int) -> float:
    """Compute a jittered backoff delay for the given attempt."""
    try:
        from agent.retry_utils import jittered_backoff
        return jittered_backoff(attempt)
    except Exception:
        # Fallback: simple exponential backoff capped at 60s.
        return min(2 ** max(0, attempt - 1), 60.0)


# ── Main agent class ────────────────────────────────────────────────────

class AIAgent:
    """AI Agent with tool calling capabilities (Phase 1 minimal version).

    Targets OpenAI-compatible chat completions endpoints.  Other
    providers (anthropic, bedrock, codex) ship in Phase 4.
    """

    @property
    def base_url(self) -> str:
        return self._base_url

    @base_url.setter
    def base_url(self, value: str) -> None:
        self._base_url = value or ""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: str = "",
        max_iterations: int = 90,
        tool_delay: float = 1.0,
        enabled_toolsets: Optional[List[str]] = None,
        disabled_toolsets: Optional[List[str]] = None,
        session_id: Optional[str] = None,
        verbose_logging: bool = False,
        quiet_mode: bool = False,
        max_tokens: Optional[int] = None,
        ephemeral_system_prompt: Optional[str] = None,
        iteration_budget: Optional[IterationBudget] = None,
        provider: Optional[str] = None,
    ):
        """Initialize the AI Agent.

        Phase 1 keeps a minimal parameter surface — Phases 2+ reintroduce
        the dropped knobs (providers_*, callbacks, fallback_model,
        credential_pool, prefill_messages, …) as they become relevant.

        ``provider`` overrides the auto-detection in
        ``_detect_provider``.  Pass it explicitly when you want to force
        a route (e.g. ``provider="anthropic"`` against an Anthropic-compatible
        base_url).  Wired routes:

        - ``"openai-compatible"`` (default) — ``chat.completions.create``
          with streaming.
        - ``"anthropic"`` (§2.4 waves 3-4) — ``messages.create`` +
          event-based ``messages.stream``.
        - ``"codex"`` (§2.4 waves 5-6) — ``responses.create`` +
          event-based ``responses.stream``.

        ``"bedrock" / "gemini"`` are still advertised-only — those
        adapters remain unported.
        """
        _install_safe_stdio()

        self.model = model
        self.max_iterations = max_iterations
        self.iteration_budget = iteration_budget or IterationBudget(max_iterations)
        self.tool_delay = tool_delay
        self.enabled_toolsets = list(enabled_toolsets) if enabled_toolsets else []
        self.disabled_toolsets = list(disabled_toolsets) if disabled_toolsets else []
        self.verbose_logging = verbose_logging
        self.quiet_mode = quiet_mode
        self.ephemeral_system_prompt = ephemeral_system_prompt
        self.max_tokens = max_tokens

        self.base_url = base_url or os.environ.get("OPENAI_BASE_URL", "")
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.session_id = session_id or str(uuid.uuid4())
        self.provider = provider or _detect_provider(self.base_url)

        # Lazy-built OpenAI client; created on first API call.
        self._client = None
        self._client_lock = threading.RLock()

        # Lazy-built Anthropic client (§2.4 wave 3) — only constructed
        # when self.provider == "anthropic" hits its first API call.
        self._anthropic_client = None
        self._anthropic_client_lock = threading.RLock()

        # Per-turn state — reset at the start of each run_conversation.
        self._current_task_id: Optional[str] = None
        self._api_call_count = 0
        self._interrupt_requested = False
        # Streaming callback — set by run_conversation when the caller
        # provides one; consumed by _call_chat_completions to decide
        # streaming vs non-streaming.
        self._stream_callback: Optional[Callable[[str], None]] = None

        # Resolved tool registry (None means "no tools available").
        self._tool_registry = _load_tool_registry()

        # Cached schemas — built lazily, invalidated by switch_tools().
        self._tool_schemas_cache: Optional[List[Dict[str, Any]]] = None

        # Per-session TodoStore — Phase 2.2 wave 2.  Plumbed into
        # ``dispatch(name, args, store=...)`` so the ``todo`` tool can
        # read/write across iterations within this conversation.
        try:
            from tools.todo_tool import TodoStore
            self._todo_store = TodoStore()
        except Exception:
            self._todo_store = None

    # ── small helpers ────────────────────────────────────────────────

    def _safe_print(self, *args, **kwargs) -> None:
        """print() that survives broken stdout pipes (delegates to _SafeWriter)."""
        if self.quiet_mode and not kwargs.pop("force", False):
            return
        try:
            print(*args, **kwargs)
        except (OSError, ValueError):
            pass

    def _vprint(self, *args, **kwargs) -> None:
        """Verbose-only print; controlled by ``verbose_logging``."""
        if self.verbose_logging:
            self._safe_print(*args, **kwargs)

    # ── client management ────────────────────────────────────────────

    def _build_client_kwargs(self) -> Dict[str, Any]:
        """Build kwargs for ``OpenAI(...)`` from agent state."""
        kwargs: Dict[str, Any] = {}
        if self._api_key:
            kwargs["api_key"] = self._api_key
        if self._base_url:
            kwargs["base_url"] = self._base_url
        return kwargs

    def _get_openai_client(self):
        """Return a cached OpenAI client, building on first call."""
        with self._client_lock:
            if self._client is None:
                self._client = OpenAI(**self._build_client_kwargs())
            return self._client

    def _get_anthropic_client(self):
        """Return a cached Anthropic SDK client, building on first call.

        Routed via ``agent.anthropic_adapter.build_anthropic_client`` so
        proxy normalization / OAuth / Claude Code beta headers all flow
        through the same code path the upstream uses.
        """
        with self._anthropic_client_lock:
            if self._anthropic_client is None:
                from agent.anthropic_adapter import build_anthropic_client
                self._anthropic_client = build_anthropic_client(
                    self._api_key,
                    self._base_url or None,
                )
            return self._anthropic_client

    def close(self) -> None:
        """Close any cached SDK clients.  Safe to call multiple times."""
        with self._client_lock:
            client = self._client
            self._client = None
        if client is not None:
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        with self._anthropic_client_lock:
            ant = self._anthropic_client
            self._anthropic_client = None
        if ant is not None:
            close = getattr(ant, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

    # ── tool plumbing ────────────────────────────────────────────────

    def _resolve_tool_schemas(self) -> List[Dict[str, Any]]:
        """Return the OpenAI-format tool schemas this agent should expose.

        Uses upstream's ``get_all_tool_names()`` + ``get_definitions()``
        pair — ``get_definitions`` already filters out tools whose
        ``check_fn()`` reports unavailable, and emits the
        ``{"type": "function", "function": {...}}`` envelope the SDK
        expects.  Falls back to ``[]`` (tool-less mode) when the
        registry isn't loaded.
        """
        if self._tool_schemas_cache is not None:
            return self._tool_schemas_cache

        registry = self._tool_registry
        if registry is None:
            self._tool_schemas_cache = []
            return self._tool_schemas_cache

        try:
            all_names = registry.get_all_tool_names()
        except Exception as exc:
            logger.warning("registry.get_all_tool_names() failed: %s", exc)
            self._tool_schemas_cache = []
            return self._tool_schemas_cache

        # toolset filtering: drop tools whose toolset is disabled or not enabled.
        if self.enabled_toolsets or self.disabled_toolsets:
            enabled_lower = {t.lower() for t in self.enabled_toolsets}
            disabled_lower = {t.lower() for t in self.disabled_toolsets}
            kept = []
            for name in all_names:
                toolset = (registry.get_toolset_for_tool(name) or "").lower()
                if disabled_lower and toolset in disabled_lower:
                    continue
                if enabled_lower and toolset not in enabled_lower:
                    continue
                kept.append(name)
            all_names = kept

        try:
            schemas = registry.get_definitions(set(all_names), quiet=self.quiet_mode)
        except Exception as exc:
            logger.warning("registry.get_definitions() failed: %s", exc)
            schemas = []

        self._tool_schemas_cache = schemas
        return schemas

    def _dispatch_tool_call(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        """Run a single tool by name, returning a string result."""
        registry = self._tool_registry
        if registry is None:
            return f"[error] no tool registry loaded; cannot run {tool_name!r}"
        dispatch = getattr(registry, "dispatch", None)
        if not callable(dispatch):
            return f"[error] tools.registry has no dispatch(); cannot run {tool_name!r}"
        try:
            return dispatch(tool_name, arguments, store=self._todo_store)
        except Exception as exc:
            logger.exception("tool %s failed", tool_name)
            return f"[error] tool {tool_name} raised {type(exc).__name__}: {exc}"

    def _get_active_terminal_env(self):
        """Return the active LocalTerminalEnv for this task, or None.

        Used by tool_result_storage to spill oversized results into the
        sandbox via env.execute(...).  When no terminal env has been
        created yet (no file/terminal tool has run), returns None and
        the storage layer falls back to inline truncation.
        """
        try:
            from tools.terminal_tool import _active_environments, _env_lock
        except Exception:
            return None
        task_id = self._current_task_id or "default"
        with _env_lock:
            return _active_environments.get(task_id)

    # ── tool-execution subsystem ────────────────────────────────────
    # Three-layer dispatch matching upstream's structure:
    #   _execute_tool_calls         — entry; picks sequential vs concurrent
    #   _execute_tool_calls_sequential / _concurrent — actual execution
    #   _invoke_tool                — single-tool branch table
    # phalanx Phase-2.x only implements the sequential path; the rest are
    # signature-compatible stand-ins so future cherry-picks of upstream's
    # plugins / guardrails / parallel executor drop in cleanly.

    def _execute_tool_calls(
        self,
        assistant_message,
        messages: list,
        effective_task_id: str,
        api_call_count: int = 0,
    ) -> None:
        """Execute a tool-call batch and append results to *messages*.

        Decides between sequential and concurrent execution.  Currently
        every batch falls through to sequential because
        ``_should_parallelize_tool_batch`` returns False until phalanx
        ports the parallel executor.
        """
        tool_calls = list(getattr(assistant_message, "tool_calls", None) or [])
        if not tool_calls:
            return
        if not _should_parallelize_tool_batch(tool_calls):
            return self._execute_tool_calls_sequential(
                assistant_message, messages, effective_task_id, api_call_count
            )
        return self._execute_tool_calls_concurrent(
            assistant_message, messages, effective_task_id, api_call_count
        )

    def _execute_tool_calls_concurrent(
        self,
        assistant_message,
        messages: list,
        effective_task_id: str,
        api_call_count: int = 0,
    ) -> None:
        """Phalanx Phase-2.x stand-in: defer to sequential execution.

        Upstream uses a thread pool with shared session_id / interrupt /
        approval-queue state.  Until §2.4+ ports that machinery, this
        method exists only so call sites match upstream — it forwards to
        the sequential path.
        """
        logger.debug("phalanx: concurrent tool exec not yet wired; using sequential")
        return self._execute_tool_calls_sequential(
            assistant_message, messages, effective_task_id, api_call_count
        )

    def _execute_tool_calls_sequential(
        self,
        assistant_message,
        messages: list,
        effective_task_id: str,
        api_call_count: int = 0,
    ) -> None:
        """Sequential execution: dispatch each tool, persist oversized
        results (layer 2), enforce per-turn aggregate budget (layer 3).
        """
        from tools.tool_result_storage import maybe_persist_tool_result, enforce_turn_budget

        tool_calls = list(getattr(assistant_message, "tool_calls", None) or [])
        if not tool_calls:
            return
        first_appended = len(messages)

        for tool_call in tool_calls:
            if self._interrupt_requested:
                # Mirror upstream: skip remaining tools and emit a
                # cancellation tool message for each so the API sees
                # a complete tool-result sequence per protocol.
                idx = tool_calls.index(tool_call)
                for skipped in tool_calls[idx:]:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": skipped.id,
                        "content": (
                            f"[Tool execution cancelled — {skipped.function.name} "
                            "was skipped due to user interrupt]"
                        ),
                    })
                break

            function_name = tool_call.function.name
            try:
                function_args = json.loads(tool_call.function.arguments)
            except json.JSONDecodeError as exc:
                logger.warning("Could not parse args for %s: %s", function_name, exc)
                function_args = {}
            if not isinstance(function_args, dict):
                function_args = {}

            self._vprint(f"[loop]   tool: {function_name}({list(function_args.keys())})")

            result_str = self._invoke_tool(
                function_name,
                function_args,
                effective_task_id,
                tool_call_id=tool_call.id,
                messages=messages,
            )
            if not isinstance(result_str, str):
                result_str = str(result_str)

            # Layer 2: per-result persistence — spill oversized results
            # into the sandbox temp dir so the model only sees a preview
            # + file-path reference instead of burning context.
            result_str = maybe_persist_tool_result(
                content=result_str,
                tool_name=function_name,
                tool_use_id=tool_call.id,
                env=self._get_active_terminal_env(),
            )
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": result_str,
            })
            if self.tool_delay:
                time.sleep(self.tool_delay)

        # Layer 3: aggregate per-turn budget — if the combined size of
        # this round's tool results still exceeds the turn budget, spill
        # the largest non-persisted ones until under budget.
        enforce_turn_budget(messages[first_appended:], env=self._get_active_terminal_env())

    def _invoke_tool(
        self,
        function_name: str,
        function_args: dict,
        effective_task_id: str,
        tool_call_id: Optional[str] = None,
        messages: Optional[list] = None,
        pre_tool_block_checked: bool = False,
    ) -> str:
        """Single-tool dispatch branch table.

        Mirrors upstream's structure slot-for-slot.  phalanx currently
        implements only the ``todo`` branch (state plumbed via
        ``self._todo_store``) and the registry catch-all; other agent-
        level branches (memory / clarify / delegate_task / session_search
        / plugin pre-call hooks) are commented placeholders so future
        cherry-picks land cleanly.
        """
        # _ = pre_tool_block_checked  # reserved for future plugin parity

        if function_name == "todo":
            from tools.todo_tool import todo_tool as _todo_tool
            return _todo_tool(
                todos=function_args.get("todos"),
                merge=function_args.get("merge", False),
                store=self._todo_store,
            )

        # The branches below are reserved for upstream parity.  They are
        # not yet ported — falling through to registry dispatch returns a
        # clean "Unknown tool" JSON error if the model invokes them.
        # elif function_name == "session_search":   # §2.5 conversation persistence
        #     ...
        # elif function_name == "memory":           # §2.7 memory manager
        #     ...
        # elif function_name == "clarify":          # §2.6 interactive prompts
        #     ...
        # elif function_name == "delegate_task":    # §2.4 sub-agent fan-out
        #     return self._dispatch_delegate_task(function_args)

        return self._dispatch_tool_call(function_name, function_args)

    @staticmethod
    def _parse_tool_arguments(raw: Any) -> Dict[str, Any]:
        """Best-effort parse of ``tool_calls[*].function.arguments`` to a dict."""
        if isinstance(raw, dict):
            return raw
        if not raw:
            return {}
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", errors="replace")
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _serialize_tool_calls(tool_calls: Any) -> List[Dict[str, Any]]:
        """Convert OpenAI SDK tool_call objects into JSON-serializable dicts."""
        out: List[Dict[str, Any]] = []
        for tc in tool_calls or []:
            tc_id = getattr(tc, "id", None) or (tc.get("id") if isinstance(tc, dict) else None)
            fn = getattr(tc, "function", None)
            if fn is None and isinstance(tc, dict):
                fn = tc.get("function") or {}
            name = getattr(fn, "name", None) if not isinstance(fn, dict) else fn.get("name")
            args = getattr(fn, "arguments", None) if not isinstance(fn, dict) else fn.get("arguments")
            out.append({
                "id": tc_id or AIAgent._fallback_call_id(name or "", args or "", len(out)),
                "type": "function",
                "function": {"name": name or "", "arguments": args or "{}"},
            })
        return out

    @staticmethod
    def _fallback_call_id(fn_name: str, arguments: str, index: int) -> str:
        """Build a deterministic call_id when the SDK didn't provide one."""
        import hashlib
        h = hashlib.sha1(f"{fn_name}|{arguments}|{index}".encode("utf-8")).hexdigest()
        return f"call_{h[:24]}"

    # ── API call with retry ─────────────────────────────────────────

    def _call_chat_completions(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
    ) -> Any:
        """Invoke the model API with retries on retryable errors.

        When ``self.provider == "anthropic"``, dispatches to
        ``_call_anthropic_messages``; the Anthropic Messages response is
        wrapped in a ``SimpleNamespace`` shaped like an OpenAI ChatCompletion
        so the rest of ``run_conversation`` is provider-agnostic.

        Otherwise calls ``client.chat.completions.create``.  When
        ``self._stream_callback`` is set the SDK is asked to stream and the
        resulting chunks are accumulated into the same non-streaming-shaped
        object; each text delta is forwarded to the callback live so a TTY
        / TTS consumer can show progress before the full reply lands.  The
        anthropic route uses ``client.messages.stream(...)`` (event-based
        protocol — see ``_accumulate_anthropic_stream``); the OpenAI-compat
        route uses ``stream=True`` on ``chat.completions.create``.

        Retries are gated by ``self.iteration_budget.remaining`` so an
        agent stuck in retry loops cannot exceed its budget.
        """
        stream_callback: Optional[Callable[[str], None]] = self._stream_callback
        is_anthropic = self.provider == "anthropic"
        is_codex = self.provider == "codex"

        attempt = 0
        last_exc: Optional[Exception] = None
        while True:
            attempt += 1
            try:
                if is_anthropic:
                    return self._call_anthropic_messages(messages, tools, stream_callback)
                if is_codex:
                    return self._call_codex_responses(messages, tools, stream_callback)
                client = self._get_openai_client()
                api_kwargs: Dict[str, Any] = {
                    "model": self.model,
                    "messages": messages,
                }
                if tools:
                    api_kwargs["tools"] = tools
                if self.max_tokens is not None:
                    api_kwargs["max_tokens"] = self.max_tokens
                if stream_callback is None:
                    return client.chat.completions.create(**api_kwargs)
                stream = client.chat.completions.create(stream=True, **api_kwargs)
                return _accumulate_stream(stream, stream_callback)
            except Exception as exc:
                last_exc = exc
                classified = _classify_error(exc, provider=self.provider, model=self.model)
                if not getattr(classified, "retryable", True):
                    logger.warning("non-retryable API error: %s", exc)
                    raise
                if attempt >= 5 or self.iteration_budget.remaining == 0:
                    logger.warning("retry budget exhausted after %d attempts: %s", attempt, exc)
                    raise
                delay = _retry_delay(attempt)
                logger.info("API error (attempt %d): %s; retrying in %.1fs", attempt, exc, delay)
                time.sleep(delay)

        # Unreachable; mypy guard.
        raise last_exc if last_exc else RuntimeError("unreachable")

    def _call_anthropic_messages(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
        stream_callback: Optional[Callable[[str], None]] = None,
    ) -> Any:
        """Send one Anthropic ``messages.create`` round-trip.

        Builds the API kwargs through ``build_anthropic_kwargs`` (handles
        message / tool format conversion, system-prompt extraction, output
        token resolution) and converts the response into the OpenAI
        ChatCompletion-shaped ``SimpleNamespace`` the run loop already
        knows how to unpack.

        When ``stream_callback`` is set, ``messages.stream(...)`` is used
        instead and ``_accumulate_anthropic_stream`` forwards each
        ``text_delta`` to the callback live before assembling the final
        message via ``stream.get_final_message()`` (§2.4 wave 4).

        OAuth / Bedrock / fast_mode / reasoning are all left at their
        defaults — those wirings come on demand.
        """
        from agent.anthropic_adapter import build_anthropic_kwargs

        api_kwargs = build_anthropic_kwargs(
            model=self.model,
            messages=messages,
            tools=list(tools) if tools else None,
            max_tokens=self.max_tokens,
            reasoning_config=None,
            base_url=self._base_url or None,
        )
        client = self._get_anthropic_client()
        if stream_callback is None:
            response = client.messages.create(**api_kwargs)
            return _anthropic_response_to_openai_shape(response)
        return _accumulate_anthropic_stream(
            client.messages.stream(**api_kwargs),
            stream_callback,
        )

    def _call_codex_responses(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
        stream_callback: Optional[Callable[[str], None]] = None,
    ) -> Any:
        """Send one ``responses.create`` round-trip (Codex / gpt-5 / o1).

        The Responses API takes:
          - ``instructions``: system prompt (extracted from messages[0])
          - ``input``: the rest of the chat history converted via
            ``_chat_messages_to_responses_input``
          - ``tools``: function definitions converted via
            ``_responses_tools``

        When ``stream_callback`` is set, ``responses.stream(...)`` is used
        instead and ``_accumulate_codex_stream`` forwards each
        ``response.output_text.delta`` to the callback live before
        assembling the final response via ``stream.get_final_response()``
        (§2.4 wave 6).

        Reasoning effort / encrypted multi-turn continuity / GitHub-Models
        backend / xAI-Grok backend / Codex-OAuth backend are all left at
        their defaults — those wirings come on demand.
        """
        from agent.codex_responses_adapter import (
            _chat_messages_to_responses_input,
            _responses_tools,
        )

        # Split system out of the messages array so it lands in
        # ``instructions`` rather than as an input item.
        instructions = ""
        payload_messages = messages
        if messages and messages[0].get("role") == "system":
            instructions = str(messages[0].get("content") or "").strip()
            payload_messages = messages[1:]
        if not instructions:
            from agent.prompt_builder import DEFAULT_AGENT_IDENTITY
            instructions = DEFAULT_AGENT_IDENTITY

        api_kwargs: Dict[str, Any] = {
            "model": self.model,
            "instructions": instructions,
            "input": _chat_messages_to_responses_input(payload_messages),
            "tool_choice": "auto",
            "parallel_tool_calls": True,
            "store": False,
        }
        converted_tools = _responses_tools(tools) if tools else None
        if converted_tools:
            api_kwargs["tools"] = converted_tools
        if self.max_tokens is not None:
            # Responses API renamed max_tokens → max_output_tokens for
            # consistency with Anthropic's naming.
            api_kwargs["max_output_tokens"] = self.max_tokens

        client = self._get_openai_client()
        if stream_callback is None:
            response = client.responses.create(**api_kwargs)
            return _codex_response_to_openai_shape(response)
        return _accumulate_codex_stream(
            client.responses.stream(**api_kwargs),
            stream_callback,
        )

    # ── main conversation loop ───────────────────────────────────────

    def run_conversation(
        self,
        user_message: str,
        system_message: Optional[str] = None,
        conversation_history: Optional[List[Dict[str, Any]]] = None,
        task_id: Optional[str] = None,
        stream_callback: Optional[Callable[[str], None]] = None,
        persist_user_message: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Run a full tool-calling loop until the model returns no tool_calls.

        Args:
            user_message: The user's prompt.
            system_message: Override for the system prompt; falls back to
                ``self.ephemeral_system_prompt`` then a generic default.
            conversation_history: Prior messages to seed the conversation.
            task_id: Caller-supplied task id (auto-generated if missing).
            stream_callback: When provided, every text delta from the
                model is passed to this callable as it arrives.  The
                final response is still returned in full via the result
                dict.  See ``_accumulate_stream`` for the chunk-rebuild
                semantics (tool_calls are reassembled from index-keyed
                deltas).  When None, the SDK is called in non-streaming
                mode (single round-trip).
            persist_user_message: Accepted for forward-compat; ignored.

        Returns:
            ``{"final_response": str, "messages": list, "api_calls": int,
              "stop_reason": str, "iterations_used": int}``
        """
        # Per-turn budget reset — matches upstream behavior.
        self.iteration_budget = IterationBudget(self.max_iterations)
        self._interrupt_requested = False
        self._current_task_id = task_id or str(uuid.uuid4())
        self._stream_callback = stream_callback

        _set_session_log_context(self.session_id)

        # Build initial messages list.
        messages: List[Dict[str, Any]] = (
            [copy.deepcopy(m) for m in conversation_history] if conversation_history else []
        )

        # Assemble identity + environment + project context + caller overrides.
        # build_system_prompt handles SOUL.md / .hermes.md / AGENTS.md /
        # CLAUDE.md / .cursorrules discovery, WSL hints, and slot ordering.
        from agent.prompt_builder import build_system_prompt
        effective_system = build_system_prompt(
            user_system=system_message,
            ephemeral=self.ephemeral_system_prompt,
            cwd=os.getcwd(),
        )
        # Ensure system message is first.
        if not messages or messages[0].get("role") != "system":
            messages.insert(0, {"role": "system", "content": effective_system})
        else:
            messages[0]["content"] = effective_system

        messages.append({"role": "user", "content": user_message})

        tools = self._resolve_tool_schemas()
        api_call_count = 0
        stop_reason = "completed"
        final_text = ""

        while api_call_count < self.max_iterations and self.iteration_budget.remaining > 0:
            if self._interrupt_requested:
                stop_reason = "interrupted"
                break

            api_call_count += 1
            self._api_call_count = api_call_count
            if not self.iteration_budget.consume():
                stop_reason = "budget_exhausted"
                break

            self._vprint(f"[loop] turn {api_call_count}: calling {self.model}")

            # API call with classify-and-retry — covers transient network /
            # provider errors without burning the whole iteration budget.
            # Helpers ``_classify_error`` / ``_retry_delay`` (run_agent.py:187-208)
            # have been wired up for a while; this block finally consumes them.
            response = None
            attempt = 0
            max_api_attempts = 5
            while True:
                try:
                    response = self._call_chat_completions(messages, tools)
                    break
                except Exception as exc:
                    attempt += 1
                    classification = _classify_error(
                        exc, provider="openai", model=self.model,
                    )
                    if not getattr(classification, "retryable", False) or attempt >= max_api_attempts:
                        stop_reason = f"api_error:{type(exc).__name__}"
                        logger.error(
                            "API call failed permanently after %d attempt(s): %s",
                            attempt, exc,
                        )
                        final_text = f"[error] API call failed: {exc}"
                        break
                    delay = _retry_delay(attempt)
                    logger.warning(
                        "API call failed (attempt %d/%d) — retrying in %.1fs: %s",
                        attempt, max_api_attempts, delay, exc,
                    )
                    time.sleep(delay)
            if response is None:
                # All retries exhausted (or non-retryable).  stop_reason and
                # final_text were already populated above.
                break

            choice = response.choices[0] if getattr(response, "choices", None) else None
            if choice is None:
                stop_reason = "empty_response"
                break

            assistant_msg = choice.message
            content = getattr(assistant_msg, "content", None) or ""
            raw_tool_calls = getattr(assistant_msg, "tool_calls", None) or []
            serialized_calls = self._serialize_tool_calls(raw_tool_calls)

            assistant_record: Dict[str, Any] = {"role": "assistant", "content": content}
            if serialized_calls:
                assistant_record["tool_calls"] = serialized_calls
            messages.append(assistant_record)

            # No tool calls → model is done.  Surface the final text.
            if not serialized_calls:
                final_text = content
                stop_reason = "completed"
                break

            # Dispatch the whole batch through the tool-execution subsystem.
            # _execute_tool_calls picks sequential vs concurrent (currently
            # always sequential — see _should_parallelize_tool_batch).
            self._execute_tool_calls(
                assistant_msg,
                messages,
                effective_task_id=self._current_task_id or "default",
                api_call_count=api_call_count,
            )

        else:
            # Loop exhausted max_iterations cleanly.
            stop_reason = "max_iterations" if api_call_count >= self.max_iterations else stop_reason

        # If we never hit the "no tool_calls" branch, fall back to the last
        # assistant content (or empty string) so the caller always sees text.
        if not final_text:
            for msg in reversed(messages):
                if msg.get("role") == "assistant":
                    final_text = msg.get("content") or ""
                    break

        return {
            "final_response": final_text,
            "messages": messages,
            "api_calls": api_call_count,
            "stop_reason": stop_reason,
            "iterations_used": self.iteration_budget.used,
        }

    # ── convenience entry ───────────────────────────────────────────

    def chat(self, message: str, stream_callback: Optional[Callable[[str], None]] = None) -> str:
        """Send a single message and return the model's final text reply.

        Thin wrapper around ``run_conversation`` for callers that only
        need the text and don't care about the message history.
        """
        result = self.run_conversation(message, stream_callback=stream_callback)
        return result.get("final_response", "")

    def request_interrupt(self, message: Optional[str] = None) -> None:
        """Ask the loop to stop after the current turn.  Thread-safe."""
        self._interrupt_requested = True
        if message:
            logger.info("interrupt requested: %s", message)


# ── CLI entry (`python run_agent.py ...`) ──────────────────────────────

def main(
    message: Optional[str] = None,
    *,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    max_iterations: int = 90,
    max_tokens: Optional[int] = None,
    verbose: bool = False,
    quiet: bool = False,
    system: Optional[str] = None,
) -> int:
    """Bare-bones CLI entry — bypass ``hermes_cli`` and call the agent directly.

    Examples::

        python run_agent.py --message "Hello" --model gpt-4o-mini
        python run_agent.py "Hello" --model gpt-4o-mini --base-url ...

    Returns the process exit code (0 = success, 1 = failure).
    """
    if verbose:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if not message:
        print("error: --message <text> is required", file=sys.stderr)
        return 2

    resolved_model = model or os.environ.get("PHALANX_MODEL") or os.environ.get("OPENAI_MODEL", "")
    if not resolved_model:
        print(
            "error: --model <name> is required (or set PHALANX_MODEL / OPENAI_MODEL)",
            file=sys.stderr,
        )
        return 2

    agent = AIAgent(
        base_url=base_url,
        api_key=api_key,
        model=resolved_model,
        max_iterations=max_iterations,
        max_tokens=max_tokens,
        verbose_logging=verbose,
        quiet_mode=quiet,
        ephemeral_system_prompt=system,
    )
    try:
        result = agent.run_conversation(message)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        agent.close()

    print(result.get("final_response", ""))
    if verbose:
        print(
            f"\n[done] turns={result['api_calls']} stop={result['stop_reason']} "
            f"budget={result['iterations_used']}/{agent.max_iterations}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    try:
        import fire
    except ImportError:
        print("error: 'fire' package required for CLI; install with: pip install fire", file=sys.stderr)
        sys.exit(2)
    sys.exit(fire.Fire(main))
