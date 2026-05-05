"""Phase 2.2 wave-4 minimal shim for agent.auxiliary_client.

Upstream's ``agent/auxiliary_client.py`` (~3914 lines) implements a
fully-fledged auxiliary-LLM client with credential pooling, base-URL
routing, OpenRouter / Nous gateway preference, retries, structured
streaming, and per-task config knobs.  Web-tools uses it to
LLM-summarise large web pages so the model receives a compact
markdown digest instead of a 100-KB page.

Wave 4 only ports the *web tools surface* — search / extract /
crawl — and explicitly does NOT depend on the auxiliary LLM working.
``web_tools.process_content_with_llm`` already handles
``aux_client is None`` by returning ``None``, which causes the caller
to fall back to *truncated raw content*.  That's the behavior we want
until upstream's auxiliary client lands proper.

This shim therefore exposes the four public symbols web_tools imports
and makes them safe no-ops:

    async_call_llm                 → raises RuntimeError if ever called
    extract_content_or_reasoning   → returns "" (never reached because
                                     async_call_llm short-circuits)
    get_async_text_auxiliary_client→ returns (None, None) → web_tools
                                     skips LLM summarisation
    get_auxiliary_extra_body       → returns {}

When upstream lands the real module, drop this file and re-copy
upstream verbatim — no call sites need adjustment.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple


def get_async_text_auxiliary_client(
    task: str = "",
    *,
    main_runtime: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[Any], Optional[str]]:
    """Phase-2.2 stand-in: report 'no auxiliary client available' so
    web_tools falls back to returning raw (truncated) page content.

    Real impl resolves AsyncOpenAI client + default model from
    ``auxiliary.<task>`` config / env.
    """
    return None, None


def get_auxiliary_extra_body() -> Dict[str, Any]:
    """Phase-2.2 stand-in.  Real impl returns the per-deployment
    ``extra_body`` (e.g. Nous routing tags) merged into chat-completion
    requests.  No client → no extra body."""
    return {}


def extract_content_or_reasoning(response: Any) -> str:
    """Phase-2.2 stand-in.  Real impl pulls text from
    ``response.choices[0].message.{content,reasoning}`` with
    multi-provider fallbacks.  Never reached in this shim because
    ``async_call_llm`` raises before producing a response."""
    if response is None:
        return ""
    try:
        choices = getattr(response, "choices", None) or []
        if choices:
            msg = getattr(choices[0], "message", None)
            if msg is not None:
                content = getattr(msg, "content", None)
                if content:
                    return str(content)
    except Exception:
        pass
    return ""


async def async_call_llm(**_kwargs: Any) -> Any:
    """Phase-2.2 stand-in.  Real impl drives an AsyncOpenAI chat call
    against the auxiliary backend with retries and pool-aware
    credential rotation.  Web-tools' summarise path checks
    ``aux_client is None`` *before* calling us, so this is unreachable
    in normal flow — raise loudly if any other caller tries to use it
    so we notice the missing port."""
    raise RuntimeError(
        "agent.auxiliary_client.async_call_llm is a Phase-2.2 shim and "
        "must not be invoked.  Web-tools should detect 'no auxiliary "
        "client' via get_async_text_auxiliary_client() returning "
        "(None, None) and fall back to truncated raw content."
    )
