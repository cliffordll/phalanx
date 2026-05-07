"""Inline ``@reference`` resolver for user messages (§2.8.b wave 3).

Lets the user paste structured pointers into a prompt and have phalanx
expand them into the conversation before sending to the model.  Four
reference kinds ship in wave 3:

    @file:path/to/x.py     — read the file (path-secured to cwd)
    @diff                  — `git diff` working tree vs HEAD
    @diff:<ref>            — `git diff <ref>` (e.g. main, HEAD~3, branch..HEAD)
    @url:https://...       — HTTP GET (text only, size-capped, timeout-bounded)
    @session:<id-or-prefix> — last N turns of an earlier session via SessionDB

The resolver does **not** rewrite the user's prose: the token stays
where the user typed it, so the model sees the original intent.
Resolved content gets appended to the message in a structured block::

    <reference type="file" key="src/main.py">
    ... contents ...
    </reference>

Failures (file missing, URL timeout, traversal blocked, …) render as
an ``error="..."`` attribute on the same wrapper so the model knows
the user *tried* to attach something even when the fetch failed —
this is far more recoverable than swallowing the reference and
leaving the model guessing what the user meant.

Public surface:

* :func:`parse_references` — pure regex pass; returns the list of
  matches without touching the filesystem / network.
* :func:`resolve_references` — full pipeline: parse → resolve each
  match → return ``(rewritten_text, list[ResolvedRef])``.
* :class:`ReferenceResolver` — the resolver class itself; tests inject
  fakes via the ``handlers=`` parameter so the network path stays
  out of the test suite.
* :class:`ResolvedRef` — dataclass returned for each resolved match
  (``type``, ``key``, ``content``, ``error``, ``content_chars``).
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Regex
# ---------------------------------------------------------------------------

# Allowed reference kinds — a closed set so a typo like ``@flie:`` is
# left alone instead of silently failing to resolve.
_KINDS = ("file", "diff", "url", "session")

# Negative lookbehind ensures we don't match emails / decimals like
# ``email@domain.com`` or ``v1.2@beta``.  The trailing value is greedy
# until whitespace, ``>`` (avoid swallowing markup), or a close paren.
# When no value is present (bare ``@diff``) the value group is empty.
_REF_RE = re.compile(
    r"(?<![A-Za-z0-9_.\-])@(?P<kind>"
    + "|".join(_KINDS)
    + r")(?::(?P<value>[^\s)>]+))?"
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ParsedRef:
    """One regex hit, before any I/O."""

    kind: str
    value: str
    span: Tuple[int, int]
    raw: str


@dataclass
class ResolvedRef:
    """Outcome of resolving one parsed reference.

    ``content`` is the final inline text (empty when ``error`` is set).
    ``content_chars`` is the rendered length post-cap so the REPL
    ``/ref show`` view can flag oversized fetches without printing the
    body itself.
    """

    type: str
    key: str
    content: str = ""
    error: Optional[str] = None
    content_chars: int = 0
    raw: str = ""
    extras: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_references(text: str) -> List[ParsedRef]:
    """Pure regex scan — no I/O, deterministic, safe to call on
    untrusted input.  Returns matches in document order.
    """
    if not text or "@" not in text:
        return []
    out: List[ParsedRef] = []
    for m in _REF_RE.finditer(text):
        kind = m.group("kind")
        value = m.group("value") or ""
        out.append(
            ParsedRef(
                kind=kind,
                value=value,
                span=(m.start(), m.end()),
                raw=text[m.start() : m.end()],
            )
        )
    return out


# ---------------------------------------------------------------------------
# Handler signatures
# ---------------------------------------------------------------------------

# A handler receives the raw value (string after the colon, possibly
# empty) plus the resolver context dict and returns either the resolved
# content string or raises a ``ReferenceError``.  Handlers are intended
# to be cheap to override in tests via the ``handlers=`` kwarg.
HandlerFn = Callable[[str, Dict[str, Any]], str]


class ReferenceError(Exception):
    """Raised by a handler when resolution fails.  Caught by
    :class:`ReferenceResolver.resolve` and converted into an
    ``error=`` attribute on the rendered ``<reference>`` block."""


# ---------------------------------------------------------------------------
# Default handlers
# ---------------------------------------------------------------------------

# Conservative caps — large blobs blow the model's context window
# faster than the user expects.  Per-handler so the URL path can be
# tighter than file reads.
_DEFAULT_FILE_MAX_BYTES = 200_000
_DEFAULT_URL_MAX_BYTES = 100_000
_DEFAULT_URL_TIMEOUT_S = 5
_DEFAULT_DIFF_MAX_BYTES = 200_000
_DEFAULT_SESSION_TURN_LIMIT = 30


def _truncate_text(text: str, max_bytes: int, label: str) -> str:
    """Cap *text* at *max_bytes* (UTF-8) and append a marker.

    Counted in bytes (not chars) so a CJK-heavy file doesn't slip past
    a chars-based cap by being 3x the byte size.
    """
    if not text:
        return text
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text
    head = encoded[: max_bytes - 64]
    safe = head.decode("utf-8", errors="ignore")
    return (
        f"{safe}\n"
        f"[... {label} truncated: "
        f"{len(encoded) - len(head)} more bytes elided ...]"
    )


def _handle_file(value: str, ctx: Dict[str, Any]) -> str:
    if not value:
        raise ReferenceError("@file: requires a path (e.g. @file:src/main.py)")
    cwd = Path(ctx.get("cwd") or ".").resolve()
    target = (cwd / value).resolve() if not Path(value).is_absolute() else Path(value).resolve()

    # Path-security: must live under cwd, must not contain raw ``..``.
    from tools.path_security import has_traversal_component, validate_within_dir
    if has_traversal_component(value):
        raise ReferenceError(
            f"@file:{value}: traversal components ('..') are not allowed"
        )
    err = validate_within_dir(target, cwd)
    if err:
        raise ReferenceError(f"@file:{value}: {err}")
    if not target.exists():
        raise ReferenceError(f"@file:{value}: file not found")
    if target.is_dir():
        raise ReferenceError(f"@file:{value}: is a directory, not a file")
    try:
        body = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ReferenceError(f"@file:{value}: read error: {exc}") from exc
    return _truncate_text(body, _DEFAULT_FILE_MAX_BYTES, "file")


def _handle_diff(value: str, ctx: Dict[str, Any]) -> str:
    cwd = ctx.get("cwd") or "."
    cmd = ["git", "diff"]
    if value:
        # Allow only safe-looking diff arguments — no shell metacharacters.
        if not re.fullmatch(r"[A-Za-z0-9_.\-/=^~:]+", value):
            raise ReferenceError(
                f"@diff:{value}: argument contains unsafe characters"
            )
        cmd.append(value)
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=10,
        )
    except FileNotFoundError as exc:
        raise ReferenceError(f"@diff: git not on PATH: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ReferenceError(f"@diff: git timed out: {exc}") from exc
    if proc.returncode != 0:
        # git diff prints to stderr on bad refs.
        msg = (proc.stderr or "").strip() or "git diff failed"
        raise ReferenceError(f"@diff: {msg}")
    out = proc.stdout or ""
    if not out.strip():
        # Working-tree clean is a normal outcome — render it as a hint
        # rather than an error.
        return "[diff is empty — no working-tree changes]"
    return _truncate_text(out, _DEFAULT_DIFF_MAX_BYTES, "diff")


def _handle_url(value: str, ctx: Dict[str, Any]) -> str:
    if not value:
        raise ReferenceError("@url: requires a URL (e.g. @url:https://example.com)")
    if not (value.startswith("http://") or value.startswith("https://")):
        raise ReferenceError(
            f"@url:{value}: only http(s) URLs are supported"
        )

    # urllib is plenty for a one-shot text fetch; the heavier
    # tools.web_tools surface would pull in the full Phase-2.4 web
    # subsystem unnecessarily.
    try:
        import urllib.request
        req = urllib.request.Request(value, headers={"User-Agent": "phalanx/0.1"})
        with urllib.request.urlopen(  # noqa: S310 — http(s) checked above
            req, timeout=_DEFAULT_URL_TIMEOUT_S,
        ) as resp:
            ctype = (resp.headers.get("content-type") or "").lower()
            if "text" not in ctype and "json" not in ctype and "xml" not in ctype:
                raise ReferenceError(
                    f"@url:{value}: refusing non-text content-type {ctype!r}"
                )
            raw = resp.read(_DEFAULT_URL_MAX_BYTES + 1)
            text = raw.decode("utf-8", errors="replace")
    except ReferenceError:
        raise
    except Exception as exc:
        raise ReferenceError(f"@url:{value}: {exc}") from exc

    if len(raw) > _DEFAULT_URL_MAX_BYTES:
        return _truncate_text(text, _DEFAULT_URL_MAX_BYTES, "url")
    return text


def _handle_session(value: str, ctx: Dict[str, Any]) -> str:
    if not value:
        raise ReferenceError(
            "@session: requires a session id or unique prefix"
        )
    db = ctx.get("session_db")
    if db is None:
        raise ReferenceError(
            "@session: not available — agent has no session DB bound"
        )
    sid = db.resolve_session_id(value)
    if sid is None:
        raise ReferenceError(
            f"@session:{value}: not found (or prefix is ambiguous)"
        )
    msgs = db.get_messages_as_conversation(sid, include_ancestors=False)
    if not msgs:
        return f"[session {sid[:8]} has no messages]"
    msgs = msgs[-_DEFAULT_SESSION_TURN_LIMIT:]
    out_parts: List[str] = [
        f"[session {sid[:8]}, last {len(msgs)} turn(s)]"
    ]
    for m in msgs:
        role = m.get("role") or "?"
        content = m.get("content")
        if isinstance(content, list):
            content = " ".join(
                p.get("text", "") for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            )
        elif content is None:
            content = ""
        text = str(content)
        out_parts.append(f"{role}: {text}")
    body = "\n\n".join(out_parts)
    return _truncate_text(body, _DEFAULT_DIFF_MAX_BYTES, "session")


_DEFAULT_HANDLERS: Dict[str, HandlerFn] = {
    "file":    _handle_file,
    "diff":    _handle_diff,
    "url":     _handle_url,
    "session": _handle_session,
}


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------

class ReferenceResolver:
    """Convert a user message containing ``@<kind>[:<value>]`` tokens
    into a fully-expanded message + per-reference outcome list.

    Construct with the agent's ``cwd`` and (optionally) its ``session_db``.
    Tests inject ``handlers=`` to swap out file / url / diff / session
    fetchers with stubs.
    """

    def __init__(
        self,
        *,
        cwd: Optional[str] = None,
        session_db: Optional[Any] = None,
        handlers: Optional[Dict[str, HandlerFn]] = None,
    ) -> None:
        self.cwd = cwd or "."
        self.session_db = session_db
        self.handlers: Dict[str, HandlerFn] = (
            dict(handlers) if handlers is not None else dict(_DEFAULT_HANDLERS)
        )

    def context(self) -> Dict[str, Any]:
        """Bind-time context passed to every handler."""
        return {"cwd": self.cwd, "session_db": self.session_db}

    def resolve(self, text: str) -> Tuple[str, List[ResolvedRef]]:
        """Run every parsed reference through its handler.

        Returns ``(rewritten_text, resolved)``.  The rewritten text
        equals the original *text* with a structured block appended
        for each resolved reference; the user's ``@<kind>:<value>``
        token is left in place so the model sees the prose + the
        anchor side-by-side.

        Returns ``(text, [])`` when no references are present.
        """
        parsed = parse_references(text)
        if not parsed:
            return text, []

        ctx = self.context()
        resolved: List[ResolvedRef] = []
        for p in parsed:
            handler = self.handlers.get(p.kind)
            r = ResolvedRef(
                type=p.kind,
                key=p.value,
                raw=p.raw,
            )
            if handler is None:
                r.error = f"no handler registered for @{p.kind}"
            else:
                try:
                    r.content = handler(p.value, ctx) or ""
                except ReferenceError as exc:
                    r.error = str(exc)
                except Exception as exc:
                    logger.warning(
                        "@%s handler raised: %s", p.kind, exc, exc_info=True,
                    )
                    r.error = f"handler crashed: {exc}"
            r.content_chars = len(r.content) if not r.error else 0
            resolved.append(r)

        rendered_blocks = [_render_block(r) for r in resolved]
        rewritten = text + "\n\n" + "\n\n".join(rendered_blocks)
        return rewritten, resolved


# ---------------------------------------------------------------------------
# Block rendering
# ---------------------------------------------------------------------------

def _render_block(r: ResolvedRef) -> str:
    """Render one resolved reference as a ``<reference>`` block.

    Error attribute is XML-style for clarity but the content payload
    is **not** XML-escaped — the model gets to see file content, code,
    diff hunks etc. verbatim.  Anything that breaks an XML parser is
    irrelevant because nothing parses these as XML; they exist only
    to be read by a language model.
    """
    attrs = [f'type="{r.type}"']
    if r.key:
        attrs.append(f'key="{_attr_quote(r.key)}"')
    if r.error:
        attrs.append(f'error="{_attr_quote(r.error)}"')
        return f"<reference {' '.join(attrs)} />"
    return (
        f"<reference {' '.join(attrs)}>\n"
        f"{r.content}\n"
        f"</reference>"
    )


def _attr_quote(s: str) -> str:
    """Minimal escape for the value of an XML-ish attribute."""
    return (
        s.replace("\\", "\\\\")
         .replace('"', '\\"')
         .replace("\n", " ")
         .replace("\r", " ")
    )


# ---------------------------------------------------------------------------
# One-shot helper
# ---------------------------------------------------------------------------

def resolve_references(
    text: str,
    *,
    cwd: Optional[str] = None,
    session_db: Optional[Any] = None,
    handlers: Optional[Dict[str, HandlerFn]] = None,
) -> Tuple[str, List[ResolvedRef]]:
    """Convenience wrapper: build a resolver and run it once."""
    resolver = ReferenceResolver(
        cwd=cwd, session_db=session_db, handlers=handlers,
    )
    return resolver.resolve(text)
