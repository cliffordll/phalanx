"""Phalanx config helpers — heavily trimmed from hermes_cli/config.py.

Phase 1 keeps the public surface needed by the loop and CLI:

  cfg_get(cfg, *keys, default=None)   — safe nested-dict traversal
  cfg_set(cfg, *keys, value)          — set a nested key (creates path)
  load_config()                       — read ~/.phalanx/config.yaml
  save_config(cfg)                    — atomic write
  read_raw_config()                   — alias for load_config (no defaults merge)
  is_managed()                        — package-managed install detection
                                        (always False until Phase 7)

All function names match upstream so cherry-picks land cleanly. The
heavy migration / deep-merge / managed-system pipeline (~4500 lines)
arrives when those features become relevant in later phases.
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import yaml

from hermes_constants import get_config_path

try:
    # Phase 0 utility — atomic writes with permission preservation.
    from utils import atomic_yaml_write
except ImportError:  # pragma: no cover - utils is part of Phase 0 baseline
    atomic_yaml_write = None  # type: ignore[assignment]


# ── cache ──────────────────────────────────────────────────────────────
# Keyed on str(config_path) so profile switches don't collide.
# Each entry: (mtime_ns, size, deepcopy of parsed data).

_RAW_CONFIG_CACHE: Dict[str, Tuple[int, int, Dict[str, Any]]] = {}


# ── core: cfg_get / cfg_set ────────────────────────────────────────────


def cfg_get(cfg: Optional[Dict[str, Any]], *keys: str, default: Any = None) -> Any:
    """Traverse nested dict keys safely, returning ``default`` on any miss.

    Mirrors upstream semantics:
      - Missing intermediate keys: return ``default``, no KeyError.
      - Intermediate value not a dict: return ``default``, no AttributeError.
      - ``cfg is None``: return ``default`` (callers sometimes pass
        ``load_config() or None``).
      - Explicit ``None`` values are returned as-is, matching
        ``dict.get(key, default)`` (default only fires on absence).

    Examples:
        >>> cfg_get({"agent": {"reasoning_effort": "high"}}, "agent", "reasoning_effort")
        'high'
        >>> cfg_get({}, "agent", "reasoning_effort", default="medium")
        'medium'
        >>> cfg_get(None, "anything", default=42)
        42
    """
    if not isinstance(cfg, dict):
        return default
    node: Any = cfg
    for key in keys:
        if not isinstance(node, dict):
            return default
        if key not in node:
            return default
        node = node[key]
    return node


def cfg_set(cfg: Dict[str, Any], *keys: str, value: Any) -> Dict[str, Any]:
    """Set a nested key, creating intermediate dicts as needed.

    Returns the same ``cfg`` for chaining.  Raises ``ValueError`` when an
    intermediate path collides with a non-dict scalar (so the caller
    knows the structure is incompatible rather than silently overwriting).

    Examples:
        >>> cfg_set({}, "model", "default", value="gpt-4o-mini")
        {'model': {'default': 'gpt-4o-mini'}}
    """
    if not keys:
        raise ValueError("cfg_set requires at least one key")
    node: Dict[str, Any] = cfg
    for key in keys[:-1]:
        existing = node.get(key)
        if existing is None:
            existing = {}
            node[key] = existing
        elif not isinstance(existing, dict):
            raise ValueError(
                f"cfg_set: intermediate key {key!r} is a {type(existing).__name__}, not a dict"
            )
        node = existing
    node[keys[-1]] = value
    return cfg


# ── managed-install detection ──────────────────────────────────────────

# Subset of upstream's _MANAGED_TRUE_VALUES — kept so future Phase 7
# ports of the full managed pipeline land without renaming.
_MANAGED_TRUE_VALUES = frozenset({"1", "true", "yes", "on", "homebrew", "nixos"})


def is_managed() -> bool:
    """Return True when phalanx is running in package-manager-managed mode.

    Phase 1 always returns False; full detection (HERMES_MANAGED env var,
    .managed marker file in HERMES_HOME) is reintroduced in Phase 7+.
    Callers (e.g. ``hermes_logging._ManagedRotatingFileHandler``) treat
    False as "regular install, no special permission handling needed".
    """
    return False


# ── load / save ────────────────────────────────────────────────────────


def ensure_hermes_home() -> Path:
    """Make sure ``~/.phalanx/`` exists; return its path."""
    from hermes_constants import get_hermes_home
    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    return home


def read_raw_config() -> Dict[str, Any]:
    """Read ``~/.phalanx/config.yaml`` as-is.

    Returns the raw YAML dict, or ``{}`` if the file is missing or
    unparseable.  Cached on (mtime_ns, size); deepcopy on every call.
    """
    try:
        config_path = get_config_path()
        st = config_path.stat()
        cache_key = (st.st_mtime_ns, st.st_size)
    except (FileNotFoundError, OSError):
        return {}

    path_key = str(config_path)
    cached = _RAW_CONFIG_CACHE.get(path_key)
    if cached is not None and cached[:2] == cache_key:
        return copy.deepcopy(cached[2])

    try:
        with open(config_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception:
        return {}

    if not isinstance(data, dict):
        data = {}
    _RAW_CONFIG_CACHE[path_key] = (cache_key[0], cache_key[1], copy.deepcopy(data))
    return data


def load_config() -> Dict[str, Any]:
    """Load ``~/.phalanx/config.yaml``.

    Phase 1 returns the raw YAML (no defaults merge / migration
    pipeline).  Upstream's deep-merge + schema migration arrives in
    later phases; until then ``load_config()`` is functionally identical
    to ``read_raw_config()``.
    """
    ensure_hermes_home()
    return read_raw_config()


def save_config(cfg: Dict[str, Any]) -> Path:
    """Atomically write ``cfg`` to ``~/.phalanx/config.yaml``.

    Returns the path written.  Invalidates the in-memory cache so the
    next ``load_config()`` re-reads from disk.
    """
    if not isinstance(cfg, dict):
        raise TypeError(f"save_config expects a dict, got {type(cfg).__name__}")

    ensure_hermes_home()
    config_path = get_config_path()

    if atomic_yaml_write is not None:
        atomic_yaml_write(config_path, cfg)
    else:  # pragma: no cover - utils.atomic_yaml_write is always available in Phase 0+
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)

    _RAW_CONFIG_CACHE.pop(str(config_path), None)
    return config_path


# ── env-var helpers (used by CLI doctor / debug output) ────────────────

def env_var_redacted(name: str) -> str:
    """Return ``"<set>"`` if the env var is present, else ``"<unset>"``.

    Used by ``phalanx config show`` to expose presence without leaking
    secret values.
    """
    return "<set>" if os.environ.get(name, "").strip() else "<unset>"


def redact_key(value: Optional[str]) -> str:
    """Render a secret as ``abcd…wxyz`` for safe display in the dashboard.

    Empty / very short values fall through to ``...`` so we never leak
    enough characters to lookup a real key.  Phalanx uses this in
    ``/api/env`` GET so the SPA can show "is set" without revealing the
    actual value (the explicit reveal endpoint takes care of that with
    a token + rate limit).
    """
    if not value:
        return ""
    if len(value) <= 8:
        return "..."
    return f"{value[:4]}…{value[-4:]}"


# ── Default config tree (used by /api/config/schema and seed) ─────────

# Keep this small and aligned with what phalanx's loop / CLI actually
# reads.  The web ConfigPage reflects exactly these fields; adding a
# new branch here automatically surfaces it as a form field.
DEFAULT_CONFIG: Dict[str, Any] = {
    "model": {
        "default": "",
        "base_url": "",
        "provider": "",
    },
    "agent": {
        "max_iterations": 90,
        "max_tokens": 4096,
        "reasoning_effort": "medium",
        "compression": {
            "enabled": True,
            "threshold_pct": 0.7,
            "protect_first_n": 3,
            "protect_last_n": 6,
        },
    },
    "memory": {
        "enabled": True,
        "retrieve_limit": 5,
    },
}


# Type / select-option overrides for fields where the default value
# alone doesn't tell the schema what it should be (e.g. an enum).  Keys
# are dotted paths (``model.provider``).
_SCHEMA_OVERRIDES: Dict[str, Dict[str, Any]] = {
    "model.provider": {
        "type": "select",
        "options": ["", "openai", "anthropic", "codex"],
        "description": "LLM provider routing (empty = auto-infer from base_url)",
    },
    "agent.reasoning_effort": {
        "type": "select",
        "options": ["low", "medium", "high"],
        "description": "Reasoning depth for o-series / claude thinking models",
    },
    "model.default": {
        "type": "string",
        "description": "Default model id (e.g. gpt-4o-mini, claude-sonnet-4.5)",
    },
    "model.base_url": {
        "type": "string",
        "description": "OpenAI-compatible endpoint URL",
    },
    "agent.max_iterations": {
        "type": "number",
        "description": "Hard cap on tool-call rounds per turn (90 default)",
    },
    "agent.max_tokens": {
        "type": "number",
        "description": "max_tokens hint passed to the model",
    },
    "memory.enabled": {
        "type": "boolean",
        "description": "Inject relevant long-term memories at session start",
    },
    "memory.retrieve_limit": {
        "type": "number",
        "description": "Max memories prepended to the system prompt per turn",
    },
    "agent.compression.enabled": {
        "type": "boolean",
        "description": "Auto-summarise old turns when prompt nears context window",
    },
    "agent.compression.threshold_pct": {
        "type": "number",
        "description": "Trigger when prompt_tokens / context_length crosses this (0-1)",
    },
    "agent.compression.protect_first_n": {
        "type": "number",
        "description": "Earliest non-system turns kept verbatim during compression",
    },
    "agent.compression.protect_last_n": {
        "type": "number",
        "description": "Most recent turns kept verbatim during compression",
    },
}


def _infer_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    return "string"


def build_config_schema(
    defaults: Optional[Dict[str, Any]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Walk ``defaults`` (DEFAULT_CONFIG) and return a flat dotted-path schema.

    Each entry is ``{type, default, description?, options?, category}``.
    Type comes from :func:`_infer_type`; ``_SCHEMA_OVERRIDES`` wins where
    present.  The web ConfigPage uses this to render appropriate inputs
    (text / number / select).
    """
    src = defaults if defaults is not None else DEFAULT_CONFIG
    schema: Dict[str, Dict[str, Any]] = {}

    def _walk(node: Dict[str, Any], prefix: str) -> None:
        for k, v in node.items():
            dotted = f"{prefix}{k}"
            if isinstance(v, dict):
                _walk(v, prefix=f"{dotted}.")
                continue
            entry: Dict[str, Any] = {
                "type": _infer_type(v),
                "default": v,
                "category": prefix.rstrip(".") or k,
            }
            entry.update(_SCHEMA_OVERRIDES.get(dotted, {}))
            schema[dotted] = entry

    _walk(src, prefix="")
    return schema


# ── Optional env vars (used by /api/env to enumerate known keys) ──────

# Categories: ``providers`` (LLM API keys), ``tools`` (web search etc),
# ``phalanx`` (path / timezone / skills).  ``advanced=True`` keeps a key
# out of the default EnvPage view.
OPTIONAL_ENV_VARS: Dict[str, Dict[str, Any]] = {
    "OPENAI_API_KEY": {
        "description": "OpenAI / OpenAI-compatible API key",
        "category": "providers",
        "is_password": True,
        "url": "https://platform.openai.com/api-keys",
    },
    "OPENAI_BASE_URL": {
        "description": "OpenAI-compatible endpoint base URL",
        "category": "providers",
        "is_password": False,
    },
    "ANTHROPIC_API_KEY": {
        "description": "Anthropic API key",
        "category": "providers",
        "is_password": True,
        "url": "https://console.anthropic.com/settings/keys",
    },
    "OPENROUTER_API_KEY": {
        "description": "OpenRouter key — for auxiliary_client web summarisation when ported",
        "category": "providers",
        "is_password": True,
        "url": "https://openrouter.ai/keys",
    },
    "FIRECRAWL_API_KEY": {
        "description": "Firecrawl key — web_extract backend",
        "category": "tools",
        "is_password": True,
        "url": "https://www.firecrawl.dev/app/api-keys",
    },
    "TAVILY_API_KEY": {
        "description": "Tavily search API key",
        "category": "tools",
        "is_password": True,
        "url": "https://app.tavily.com/home",
    },
    "EXA_API_KEY": {
        "description": "Exa search API key",
        "category": "tools",
        "is_password": True,
        "url": "https://dashboard.exa.ai/api-keys",
    },
    "PARALLEL_API_KEY": {
        "description": "Parallel.ai search API key",
        "category": "tools",
        "is_password": True,
    },
    "PHALANX_HOME": {
        "description": "Override default ~/.phalanx data directory",
        "category": "phalanx",
        "is_password": False,
        "advanced": True,
    },
    "PHALANX_TIMEZONE": {
        "description": "Override timezone for log timestamps (default = local)",
        "category": "phalanx",
        "is_password": False,
        "advanced": True,
    },
    "PHALANX_OPTIONAL_SKILLS": {
        "description": "Override the optional-skills directory path",
        "category": "phalanx",
        "is_password": False,
        "advanced": True,
    },
}
