"""Synchronous auxiliary-LLM client (§2.8.b wave 2).

Replaces the Phase-2.2 shim with a minimal "second LLM call" surface
used by :mod:`agent.context_compressor` to summarise old turns and
(future) by web-tools for page summarisation.

Scope vs. upstream's ~3914-line ``auxiliary_client.py``:

* Synchronous only — phalanx's compressor preflight runs on the main
  thread before the next API call.  Upstream's async client exists for
  the streaming gateway path which phalanx hasn't ported.
* No credential pool, no retries beyond what the OpenAI SDK does
  itself, no Nous routing tags — the whole point of this layer is to
  do exactly one summarisation request and either succeed or
  transparently bail out.
* Auxiliary config is read from ``auxiliary.<task>`` in
  ~/.phalanx/config.yaml when present.  Falls back to the *main_runtime*
  hint passed by the caller (the agent's own model / base_url /
  api_key).  Falls back further to environment defaults — which is how
  zero-config installs get a working compressor without setting up a
  separate model.

Public surface (callers should depend on this list, not internals):

* :func:`get_text_auxiliary_client` — sync ``(client, model)`` resolver.
  Returns ``(None, None)`` when no usable backend is available so
  callers (compressor) can degrade to a pruning fallback instead of
  crashing.
* :func:`summarize_messages` — one-shot summarisation helper.
* :func:`extract_content_or_reasoning` — pull text out of a chat
  completion response with multi-provider fallbacks.

The async / web-tools surface from the previous shim
(``get_async_text_auxiliary_client``, ``async_call_llm``,
``get_auxiliary_extra_body``) is preserved for backwards compatibility
with anything that still imports it.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------

def _resolve_auxiliary_config(task: str) -> Dict[str, Any]:
    """Pull ``auxiliary.<task>`` (if any) from ~/.phalanx/config.yaml.

    Returns a dict with possibly empty ``model`` / ``base_url`` /
    ``api_key`` entries.  Missing file or missing branch is fine —
    caller falls back to *main_runtime* hints next.
    """
    out: Dict[str, Any] = {"model": "", "base_url": "", "api_key": ""}
    try:
        from hermes_cli.config import cfg_get, load_config
        cfg = load_config()
        for key in ("model", "base_url", "api_key"):
            val = cfg_get(cfg, "auxiliary", task, key, default="")
            if not val:
                # Also accept a generic ``auxiliary.default.*`` block —
                # so an install can configure one summary backend that
                # serves every task.
                val = cfg_get(cfg, "auxiliary", "default", key, default="")
            if val:
                out[key] = val
    except Exception as exc:
        logger.debug("auxiliary config lookup failed: %s", exc)
    return out


def _apply_main_runtime_fallback(
    cfg: Dict[str, Any],
    main_runtime: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Fill blank cfg fields from the calling agent's runtime hints."""
    if not main_runtime:
        return cfg
    for key in ("model", "base_url", "api_key"):
        if not cfg.get(key):
            mr_val = main_runtime.get(key)
            if mr_val:
                cfg[key] = str(mr_val)
    return cfg


def _apply_env_fallback(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Last-resort env vars — same names AIAgent uses."""
    if not cfg.get("api_key"):
        cfg["api_key"] = os.environ.get("OPENAI_API_KEY", "") or ""
    if not cfg.get("base_url"):
        cfg["base_url"] = os.environ.get("OPENAI_BASE_URL", "") or ""
    if not cfg.get("model"):
        cfg["model"] = (
            os.environ.get("PHALANX_MODEL")
            or os.environ.get("OPENAI_MODEL")
            or ""
        )
    return cfg


# ---------------------------------------------------------------------------
# Sync client (production path)
# ---------------------------------------------------------------------------

def get_text_auxiliary_client(
    task: str = "summary",
    *,
    main_runtime: Optional[Mapping[str, Any]] = None,
) -> Tuple[Optional[Any], Optional[str]]:
    """Resolve a sync OpenAI client + model id for *task*.

    Returns ``(None, None)`` when no usable model can be resolved or the
    OpenAI SDK itself fails to construct a client (e.g. missing API key
    against an endpoint that requires one).  Callers must handle the
    ``None`` case — never assume a client is always available.

    *main_runtime* lets the calling agent pass its own ``model`` /
    ``base_url`` / ``api_key`` so the auxiliary call can transparently
    reuse the main config when no auxiliary section is configured.
    """
    try:
        from openai import OpenAI
    except Exception as exc:
        logger.debug("OpenAI SDK unavailable for auxiliary client: %s", exc)
        return None, None

    cfg = _resolve_auxiliary_config(task)
    cfg = _apply_main_runtime_fallback(cfg, main_runtime)
    cfg = _apply_env_fallback(cfg)

    if not cfg.get("model"):
        logger.debug("auxiliary task=%r: no model resolved", task)
        return None, None

    kwargs: Dict[str, Any] = {}
    if cfg["api_key"]:
        kwargs["api_key"] = cfg["api_key"]
    if cfg["base_url"]:
        kwargs["base_url"] = cfg["base_url"]
    try:
        client = OpenAI(**kwargs)
    except Exception as exc:
        logger.debug("OpenAI() construction failed for auxiliary: %s", exc)
        return None, None
    return client, cfg["model"]


# ---------------------------------------------------------------------------
# Response extraction
# ---------------------------------------------------------------------------

def extract_content_or_reasoning(response: Any) -> str:
    """Pull the assistant text from a chat-completion response.

    Tries ``message.content`` first, then ``message.reasoning_content``
    (DeepSeek / Qwen reasoning models echo the visible answer there
    when the main content slot is empty).  Returns an empty string when
    nothing extractable is present.
    """
    if response is None:
        return ""
    try:
        choices = getattr(response, "choices", None) or []
        if not choices:
            return ""
        msg = getattr(choices[0], "message", None)
        if msg is None:
            return ""
        content = getattr(msg, "content", None)
        if isinstance(content, str) and content.strip():
            return content
        reasoning = getattr(msg, "reasoning_content", None) or getattr(
            msg, "reasoning", None
        )
        if isinstance(reasoning, str) and reasoning.strip():
            return reasoning
    except Exception as exc:
        logger.debug("extract_content_or_reasoning failed: %s", exc)
    return ""


# ---------------------------------------------------------------------------
# Summary helper
# ---------------------------------------------------------------------------

_SUMMARY_SYSTEM_PROMPT = (
    "You are a precise summarisation assistant.  You receive a slice of "
    "a multi-turn conversation between a user and an AI agent (with tool "
    "results).  Your output replaces those turns in the agent's context "
    "window so it must preserve every load-bearing detail: "
    "decisions taken, file paths touched, error messages encountered, "
    "outstanding questions, and any user instructions that have not yet "
    "been completed.  Drop greetings, acknowledgements, and tool output "
    "that has already been superseded.  Output a single tight paragraph "
    "in the same language the conversation used; do not include a "
    "preamble like 'Summary:' — your output IS the summary."
)


def _format_messages_for_summary(
    messages: List[Dict[str, Any]],
    *,
    max_chars: int = 16000,
) -> str:
    """Render a slice of the conversation as a plain transcript for the
    summariser.

    Truncates aggressively at *max_chars* per role-block — we already
    chose to drop these turns from the live context, so a lossy
    transcript is fine.
    """
    parts: List[str] = []
    for m in messages:
        role = m.get("role", "?")
        content = m.get("content")
        if isinstance(content, list):
            # Multimodal — flatten text parts only.
            text = " ".join(
                p.get("text", "") for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            )
        elif content is None:
            text = ""
        else:
            text = str(content)
        if m.get("tool_name"):
            text = f"[tool {m['tool_name']}] {text}"
        if m.get("tool_calls"):
            text = f"{text}\n[tool_calls: {m['tool_calls']}]"
        if len(text) > max_chars:
            text = text[: max_chars - 20] + "...[truncated]"
        parts.append(f"{role}: {text}")
    return "\n\n".join(parts)


def summarize_messages(
    client: Any,
    model: str,
    messages: List[Dict[str, Any]],
    *,
    focus_topic: Optional[str] = None,
    max_tokens: int = 1024,
    temperature: float = 0.0,
) -> Optional[str]:
    """One-shot summarisation of the supplied *messages* slice.

    Returns the summary text on success, or ``None`` on any failure
    (network error, empty response, model refusal).  Caller — the
    compressor — is expected to fall back to message pruning when this
    returns ``None``.

    *focus_topic*, when provided, nudges the summariser to keep details
    related to the current task even at the expense of older context.
    """
    if client is None or not model or not messages:
        return None

    transcript = _format_messages_for_summary(messages)
    if not transcript.strip():
        return None

    user_msg = (
        "Summarise the following conversation slice for use as a context "
        "anchor in subsequent turns.  Preserve all load-bearing details "
        "(decisions, file paths, errors, pending instructions); drop "
        "obsolete tool output and pleasantries.\n\n"
    )
    if focus_topic:
        user_msg += (
            f"The agent is currently working on: {focus_topic!r}.  "
            "Prioritise details related to this task.\n\n"
        )
    user_msg += "--- begin conversation slice ---\n"
    user_msg += transcript
    user_msg += "\n--- end conversation slice ---"

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _SUMMARY_SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=max_tokens,
            temperature=temperature,
        )
    except Exception as exc:
        logger.warning("auxiliary summarize_messages: API call failed: %s", exc)
        return None

    text = extract_content_or_reasoning(response)
    text = (text or "").strip()
    if not text:
        logger.debug("auxiliary summarize_messages: empty response")
        return None
    return text


# ---------------------------------------------------------------------------
# Async surface (§2.8.c wave 3)
# ---------------------------------------------------------------------------
#
# The async surface mirrors the sync surface above but uses AsyncOpenAI
# so callers in event-loop contexts (web_tools, future async REPL,
# delegate streaming) don't block the loop on network I/O.  Config
# resolution helpers (_resolve_auxiliary_config /
# _apply_main_runtime_fallback / _apply_env_fallback) are shared with
# the sync path so behavior is identical except for sync vs await.

# Default timeout for auxiliary calls.  Web-tools historically read
# ``auxiliary.web_extract.timeout`` from config; honour the same key
# for backwards compat, falling through to 360 s when unset.
_DEFAULT_AUXILIARY_TIMEOUT_S = 360.0


def _resolve_auxiliary_timeout(task: str) -> float:
    """Read ``auxiliary.<task>.timeout`` from config, default 360 s."""
    try:
        from hermes_cli.config import cfg_get, load_config
        cfg = load_config()
        val = cfg_get(cfg, "auxiliary", task, "timeout", default=None)
        if val is None:
            val = cfg_get(
                cfg, "auxiliary", "default", "timeout", default=None,
            )
        if val is not None:
            return float(val)
    except Exception:
        pass
    return _DEFAULT_AUXILIARY_TIMEOUT_S


def get_async_text_auxiliary_client(
    task: str = "",
    *,
    main_runtime: Optional[Mapping[str, Any]] = None,
) -> Tuple[Optional[Any], Optional[str]]:
    """Resolve an :class:`openai.AsyncOpenAI` client + model id.

    Mirrors :func:`get_text_auxiliary_client` but builds the *async*
    OpenAI client.  Sync construction (the SDK builds clients
    synchronously even though calls are async) — the function is
    sync; only the call paths are coroutines.

    Returns ``(None, None)`` on any failure so callers (web_tools,
    delegate critic, future ChatPage) can degrade gracefully without
    catching exceptions just to fall back to truncated content.
    """
    try:
        from openai import AsyncOpenAI
    except Exception as exc:
        logger.debug("AsyncOpenAI SDK unavailable: %s", exc)
        return None, None

    cfg = _resolve_auxiliary_config(task)
    cfg = _apply_main_runtime_fallback(cfg, main_runtime)
    cfg = _apply_env_fallback(cfg)

    if not cfg.get("model"):
        logger.debug("async auxiliary task=%r: no model resolved", task)
        return None, None

    kwargs: Dict[str, Any] = {}
    if cfg["api_key"]:
        kwargs["api_key"] = cfg["api_key"]
    if cfg["base_url"]:
        kwargs["base_url"] = cfg["base_url"]
    try:
        client = AsyncOpenAI(**kwargs)
    except Exception as exc:
        logger.debug("AsyncOpenAI() construction failed: %s", exc)
        return None, None
    return client, cfg["model"]


def get_auxiliary_extra_body() -> Dict[str, Any]:
    """No per-deployment routing tags in the phalanx port.

    Reserved for upstream's Nous gateway tagging.  Returning ``{}``
    keeps web_tools' Nous-detection branch a no-op.
    """
    return {}


async def async_call_llm(
    *,
    task: str = "",
    model: Optional[str] = None,
    messages: Optional[List[Dict[str, Any]]] = None,
    max_tokens: int = 4096,
    temperature: float = 0.0,
    extra_body: Optional[Dict[str, Any]] = None,
    timeout: Optional[float] = None,
    client: Optional[Any] = None,
    main_runtime: Optional[Mapping[str, Any]] = None,
) -> Any:
    """Run one async chat-completion call against the auxiliary backend.

    Resolution order for the client:

    1. If *client* is supplied, use it directly (caller already
       resolved).  *model* must also be supplied in that case.
    2. Otherwise call :func:`get_async_text_auxiliary_client` to
       resolve from config + main_runtime + env.

    Raises :class:`RuntimeError` when no usable client/model is
    available.  Web-tools' summariser catches this and returns
    ``None`` to its caller, which then degrades to truncated raw
    content.

    The OpenAI SDK call itself is **not** wrapped in try/except —
    callers want network errors / API errors to propagate so they
    can decide on their own retry / fallback policy.  Only the
    "no auxiliary configured" case becomes a RuntimeError; real API
    failures bubble up unchanged.
    """
    if not messages:
        raise RuntimeError("async_call_llm: messages is required")

    if client is None:
        resolved_client, resolved_model = get_async_text_auxiliary_client(
            task, main_runtime=main_runtime,
        )
        if resolved_client is None or not (model or resolved_model):
            raise RuntimeError(
                "async_call_llm: no auxiliary client/model available "
                f"for task={task!r}"
            )
        client = resolved_client
        if not model:
            model = resolved_model

    if timeout is None:
        timeout = _resolve_auxiliary_timeout(task or "default")

    call_kwargs: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if extra_body:
        call_kwargs["extra_body"] = extra_body
    if timeout is not None:
        call_kwargs["timeout"] = timeout

    return await client.chat.completions.create(**call_kwargs)


async def async_summarize_messages(
    client: Any,
    model: str,
    messages: List[Dict[str, Any]],
    *,
    focus_topic: Optional[str] = None,
    max_tokens: int = 1024,
    temperature: float = 0.0,
    timeout: Optional[float] = None,
) -> Optional[str]:
    """Async mirror of :func:`summarize_messages`.

    Same return contract: text on success, ``None`` on empty input /
    empty response / API exception.  Caller decides what to do with
    None (compressor falls back to pruning, web_tools to truncated
    raw, etc.).
    """
    if client is None or not model or not messages:
        return None

    transcript = _format_messages_for_summary(messages)
    if not transcript.strip():
        return None

    user_msg = (
        "Summarise the following conversation slice for use as a context "
        "anchor in subsequent turns.  Preserve all load-bearing details "
        "(decisions, file paths, errors, pending instructions); drop "
        "obsolete tool output and pleasantries.\n\n"
    )
    if focus_topic:
        user_msg += (
            f"The agent is currently working on: {focus_topic!r}.  "
            "Prioritise details related to this task.\n\n"
        )
    user_msg += "--- begin conversation slice ---\n"
    user_msg += transcript
    user_msg += "\n--- end conversation slice ---"

    call_kwargs: Dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if timeout is not None:
        call_kwargs["timeout"] = timeout

    try:
        response = await client.chat.completions.create(**call_kwargs)
    except Exception as exc:
        logger.warning("async_summarize_messages: API call failed: %s", exc)
        return None

    text = extract_content_or_reasoning(response)
    text = (text or "").strip()
    if not text:
        logger.debug("async_summarize_messages: empty response")
        return None
    return text
