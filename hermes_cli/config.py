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
