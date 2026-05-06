"""
Phalanx — Web UI server (Phase 2.7 wave 1 skeleton).

Provides a FastAPI backend that serves a Vite/React SPA and a small set of
REST endpoints over the subsystems already in place: SessionDB, config,
env loader, registry.  Designed to be portable across cherry-picks from
the upstream ``hermes_cli/web_server.py`` (4049 lines): the auth
middleware, Host-header guard, CORS regex, token injection in
``mount_spa``, and ``start_server`` are kept verbatim in shape.

Wave 1 ships only ``/api/status`` plus the static-SPA fallback.
``/api/sessions``, ``/api/logs``, ``/api/analytics``, ``/api/config*``,
``/api/env*`` arrive in waves 2-3.

CLI entry point::

    hermes web                          # default :9119, opens browser
    hermes web --port 8080 --no-open    # custom port, no browser
    hermes web --token deadbeef...      # fixed token (CI / tests)
    hermes web --bind 127.0.0.1         # default; pass --insecure for 0.0.0.0
"""

from __future__ import annotations

import hmac
import logging
import os
import secrets
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Optional

try:
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
    from pydantic import BaseModel
except ImportError as exc:
    raise SystemExit(
        "Web UI requires fastapi and uvicorn.\n"
        f"Install with: {sys.executable} -m pip install 'fastapi' 'uvicorn[standard]'"
    ) from exc

from hermes_cli import __release_date__, __version__
from hermes_cli.config import cfg_get, load_config
from hermes_constants import get_config_path, get_env_path, get_hermes_home

_log = logging.getLogger(__name__)


# ── Paths ──────────────────────────────────────────────────────────────

# ``hermes_cli/web_dist/`` is the npm build output; populated by
# ``cd web && npm run build`` before ``python -m build``.  Override with
# ``HERMES_WEB_DIST`` so dev can point at ``web/dist/`` directly while
# iterating without re-running the package build.
WEB_DIST = (
    Path(os.environ["HERMES_WEB_DIST"])
    if "HERMES_WEB_DIST" in os.environ
    else Path(__file__).parent / "web_dist"
)


# ── Session token ──────────────────────────────────────────────────────

# Generated fresh on every server start — dies when the process exits.
# The dashboard SPA receives the token via injection into the served
# ``index.html`` (see ``mount_spa``); subsequent /api/ calls present it
# in the dedicated ``X-Hermes-Session-Token`` header.  Avoids collisions
# with reverse proxies that already use ``Authorization``.
_SESSION_TOKEN: str = secrets.token_urlsafe(32)
_SESSION_HEADER_NAME = "X-Hermes-Session-Token"


def _set_session_token(token: str) -> None:
    """Override the auto-generated token (used by ``--token`` flag and tests)."""
    global _SESSION_TOKEN
    _SESSION_TOKEN = token


def _has_valid_session_token(request: "Request") -> bool:
    """True if the request carries a valid dashboard session token.

    Constant-time HMAC compare guards against timing side-channel.  Both
    the dedicated session header and ``Authorization: Bearer <token>``
    are accepted (the latter for compatibility with curl recipes that
    already use Bearer style).
    """
    session_header = request.headers.get(_SESSION_HEADER_NAME, "")
    if session_header and hmac.compare_digest(
        session_header.encode(), _SESSION_TOKEN.encode()
    ):
        return True
    auth = request.headers.get("authorization", "")
    expected = f"Bearer {_SESSION_TOKEN}"
    return hmac.compare_digest(auth.encode(), expected.encode())


# ── Public endpoints (no token required) ───────────────────────────────

# Keep this list minimal — only truly non-sensitive, read-only endpoints.
# Wave 1 has only ``/api/status`` here; waves 2-3 may add a couple of
# schema/defaults endpoints.
_PUBLIC_API_PATHS: frozenset = frozenset({"/api/status"})


# ── Loopback host validation ───────────────────────────────────────────

_LOOPBACK_HOST_VALUES: frozenset = frozenset({"localhost", "127.0.0.1", "::1"})


def _is_accepted_host(host_header: str, bound_host: str) -> bool:
    """True if the Host header targets the interface we bound to.

    Accepts:
    - Exact bound host (with or without port suffix)
    - Loopback aliases when bound to loopback
    - Any host when bound to 0.0.0.0 (operator opted into all-interfaces)
    """
    if not host_header:
        return False
    h = host_header.strip()
    if h.startswith("["):
        close = h.find("]")
        host_only = h[1:close] if close != -1 else h.strip("[]")
    else:
        host_only = h.rsplit(":", 1)[0] if ":" in h else h
    host_only = host_only.lower()

    if bound_host in ("0.0.0.0", "::"):
        return True

    bound_lc = bound_host.lower()
    if bound_lc in _LOOPBACK_HOST_VALUES:
        return host_only in _LOOPBACK_HOST_VALUES

    return host_only == bound_lc


# ── App + middleware ───────────────────────────────────────────────────

app = FastAPI(title="Phalanx", version=__version__)

# CORS: localhost only.  Binding to 0.0.0.0 with allow_origins=["*"] would
# let any website read/modify config and secrets through this server.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def host_header_middleware(request: "Request", call_next):
    """Reject requests whose Host header doesn't match the bound interface.

    Defends against DNS rebinding (GHSA-ppp5-vxwm-4cf7).  ``app.state.bound_host``
    is set by :func:`start_server`.  Tests bypass this guard by leaving
    ``app.state.bound_host`` unset.
    """
    bound_host = getattr(app.state, "bound_host", None)
    if bound_host:
        host_header = request.headers.get("host", "")
        if not _is_accepted_host(host_header, bound_host):
            return JSONResponse(
                status_code=400,
                content={
                    "detail": (
                        "Invalid Host header. Dashboard requests must use "
                        "the hostname the server was bound to."
                    ),
                },
            )
    return await call_next(request)


@app.middleware("http")
async def auth_middleware(request: "Request", call_next):
    """Require the session token on all /api/ routes except the public list."""
    path = request.url.path
    if path.startswith("/api/") and path not in _PUBLIC_API_PATHS:
        if not _has_valid_session_token(request):
            return JSONResponse(
                status_code=401, content={"detail": "Unauthorized"}
            )
    return await call_next(request)


# ── /api/status ────────────────────────────────────────────────────────


@app.get("/api/status")
async def get_status():
    """Snapshot of the current phalanx install.

    Phalanx-specific compared to upstream: no ``gateway_*`` fields (no
    gateway subsystem), no ``oauth_providers`` (no credential pool).
    Adds ``tools`` (list of registered tool names) so the StatusPage can
    show what the agent can call.
    """
    cfg = load_config()
    base_url = cfg_get(cfg, "model", "base_url") or os.environ.get("OPENAI_BASE_URL", "")
    model = cfg_get(cfg, "model", "default") or os.environ.get(
        "PHALANX_MODEL"
    ) or os.environ.get("OPENAI_MODEL")

    # Provider inference is best-effort — model_metadata is a chunky import,
    # don't pay for it on every status hit if the user hasn't set up models.
    provider: Optional[str] = None
    try:
        from agent.model_metadata import _infer_provider_from_url

        provider = _infer_provider_from_url(base_url) if base_url else None
    except Exception:
        pass

    # SessionDB count.  Best-effort — DB might not exist yet on a fresh
    # install (no sessions have run).
    session_count = 0
    active_sessions = 0
    try:
        from hermes_state import SessionDB

        db = SessionDB()
        try:
            sessions = db.list_sessions_rich(limit=1000)
            session_count = len(sessions)
            now = time.time()
            active_sessions = sum(
                1
                for s in sessions
                if s.get("ended_at") is None
                and (now - (s.get("last_active") or s.get("started_at") or 0)) < 300
            )
        finally:
            db.close()
    except Exception as exc:  # pragma: no cover — fresh install / no DB
        _log.debug("status: SessionDB unavailable: %s", exc)

    # Registry tool list — also best-effort, since import order matters.
    tools_list: list[str] = []
    try:
        from tools.registry import registry

        tools_list = sorted(registry.get_all_tool_names())
    except Exception as exc:  # pragma: no cover
        _log.debug("status: registry unavailable: %s", exc)

    return {
        "version": __version__,
        "release_date": __release_date__,
        "phalanx_home": str(get_hermes_home()),
        "config_path": str(get_config_path()),
        "env_path": str(get_env_path()),
        "model": model,
        "base_url": base_url,
        "provider": provider,
        "session_count": session_count,
        "active_sessions": active_sessions,
        "tools": tools_list,
    }


# ── Sessions endpoints (wave 2) ────────────────────────────────────────


@app.get("/api/sessions")
async def get_sessions(limit: int = 20, offset: int = 0):
    """Paginated session list.  ``is_active`` is true if no ``ended_at``
    row exists and the last activity was within 5 minutes."""
    from hermes_state import SessionDB

    db = SessionDB()
    try:
        sessions = db.list_sessions_rich(limit=limit, offset=offset)
        total = db.session_count()
        now = time.time()
        for s in sessions:
            last = s.get("last_active") or s.get("started_at") or 0
            s["is_active"] = s.get("ended_at") is None and (now - last) < 300
        return {
            "sessions": sessions,
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    finally:
        db.close()


@app.get("/api/sessions/{session_id}")
async def get_session_detail(session_id: str):
    from hermes_state import SessionDB

    db = SessionDB()
    try:
        sid = db.resolve_session_id(session_id)
        session = db.get_session(sid) if sid else None
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        return session
    finally:
        db.close()


@app.get("/api/sessions/{session_id}/messages")
async def get_session_messages(session_id: str):
    from hermes_state import SessionDB

    db = SessionDB()
    try:
        sid = db.resolve_session_id(session_id)
        if not sid:
            raise HTTPException(status_code=404, detail="Session not found")
        messages = db.get_messages(sid)
        return {"session_id": sid, "messages": messages}
    finally:
        db.close()


@app.delete("/api/sessions/{session_id}")
async def delete_session_endpoint(session_id: str):
    """Delete a session and its messages.  Accepts a full id or unique
    prefix; returns 404 if neither matches."""
    from hermes_state import SessionDB

    db = SessionDB()
    try:
        sid = db.resolve_session_id(session_id)
        if not sid or not db.delete_session(sid):
            raise HTTPException(status_code=404, detail="Session not found")
        return {"ok": True}
    finally:
        db.close()


class _SessionTitleUpdate(BaseModel):
    title: Optional[str] = None


@app.put("/api/sessions/{session_id}/title")
async def set_session_title_endpoint(
    session_id: str, body: _SessionTitleUpdate
):
    """Set (or clear with ``null``) a session's display title.

    409 on unique-index conflict — the SessionsPage falls back to a
    "title already in use" toast and lets the user pick another.
    """
    from hermes_state import SessionDB

    db = SessionDB()
    try:
        sid = db.resolve_session_id(session_id)
        if not sid:
            raise HTTPException(status_code=404, detail="Session not found")
        try:
            db.set_session_title(sid, body.title)
        except sqlite3.IntegrityError as exc:
            raise HTTPException(
                status_code=409,
                detail=f"Title already in use: {body.title!r}",
            ) from exc
        return {"ok": True, "session_id": sid, "title": body.title}
    finally:
        db.close()


# ── Logs endpoint (wave 2) ─────────────────────────────────────────────


@app.get("/api/logs")
async def get_logs(
    file: str = "agent",
    lines: int = 100,
    level: Optional[str] = None,
    component: Optional[str] = None,
    search: Optional[str] = None,
):
    """Tail a phalanx log file with optional level / component / search filtering.

    File must be one of ``LOG_FILES`` (currently agent / errors / gateway).
    ``level`` filters at or above the threshold; ``component`` matches the
    upstream-style logger-prefix groups in ``hermes_logging.COMPONENT_PREFIXES``.
    ``search`` is a case-insensitive substring filter applied after the
    structural filters.
    """
    from hermes_cli.logs import LOG_FILES, _read_tail

    log_name = LOG_FILES.get(file)
    if not log_name:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown log file: {file!r}. "
                f"Available: {', '.join(sorted(LOG_FILES))}"
            ),
        )
    log_path = get_hermes_home() / "logs" / log_name
    if not log_path.exists():
        return {"file": file, "lines": []}

    try:
        from hermes_logging import COMPONENT_PREFIXES
    except ImportError:  # pragma: no cover
        COMPONENT_PREFIXES = {}

    # ALL / "" / None => no level filter
    min_level = level if level and level.upper() != "ALL" else None
    if component and component.lower() != "all":
        comp_prefixes = COMPONENT_PREFIXES.get(component.lower())
        if comp_prefixes is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Unknown component: {component!r}. "
                    f"Available: {', '.join(sorted(COMPONENT_PREFIXES))}"
                ),
            )
    else:
        comp_prefixes = None

    has_filters = bool(min_level or comp_prefixes or search)
    cap = max(1, min(lines, 500))
    raw = _read_tail(
        log_path,
        cap if not search else 2000,
        has_filters=has_filters,
        min_level=min_level,
        component_prefixes=comp_prefixes,
    )
    if search:
        needle = search.lower()
        raw = [line for line in raw if needle in line.lower()][-cap:]
    return {"file": file, "lines": raw}


# ── Analytics endpoint (wave 2) ────────────────────────────────────────


@app.get("/api/analytics/usage")
async def get_usage_analytics(days: int = 30):
    """Token / cost / session counts aggregated from SessionDB.

    Phalanx omits the upstream ``insights`` skill summary block — no
    skills subsystem yet.  Daily / by_model / totals buckets match the
    upstream shape so AnalyticsPage cherry-picks land cleanly.
    """
    from hermes_state import SessionDB

    days = max(1, min(days, 365))
    db = SessionDB()
    try:
        cutoff = time.time() - (days * 86400)
        cur = db._conn.execute(
            """
            SELECT date(started_at, 'unixepoch') AS day,
                   COALESCE(SUM(input_tokens), 0)  AS input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens,
                   COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                   COALESCE(SUM(reasoning_tokens), 0)  AS reasoning_tokens,
                   COALESCE(SUM(estimated_cost_usd), 0) AS estimated_cost,
                   COALESCE(SUM(actual_cost_usd), 0)    AS actual_cost,
                   COUNT(*) AS sessions,
                   COALESCE(SUM(api_call_count), 0) AS api_calls
            FROM sessions WHERE started_at > ?
            GROUP BY day ORDER BY day
            """,
            (cutoff,),
        )
        daily = [dict(r) for r in cur.fetchall()]

        cur2 = db._conn.execute(
            """
            SELECT model,
                   COALESCE(SUM(input_tokens), 0)  AS input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens,
                   COALESCE(SUM(estimated_cost_usd), 0) AS estimated_cost,
                   COUNT(*) AS sessions,
                   COALESCE(SUM(api_call_count), 0) AS api_calls
            FROM sessions
            WHERE started_at > ? AND model IS NOT NULL
            GROUP BY model
            ORDER BY SUM(input_tokens) + SUM(output_tokens) DESC
            """,
            (cutoff,),
        )
        by_model = [dict(r) for r in cur2.fetchall()]

        cur3 = db._conn.execute(
            """
            SELECT COALESCE(SUM(input_tokens), 0)  AS total_input,
                   COALESCE(SUM(output_tokens), 0) AS total_output,
                   COALESCE(SUM(cache_read_tokens), 0) AS total_cache_read,
                   COALESCE(SUM(reasoning_tokens), 0)  AS total_reasoning,
                   COALESCE(SUM(estimated_cost_usd), 0) AS total_estimated_cost,
                   COALESCE(SUM(actual_cost_usd), 0)    AS total_actual_cost,
                   COUNT(*) AS total_sessions,
                   COALESCE(SUM(api_call_count), 0) AS total_api_calls
            FROM sessions WHERE started_at > ?
            """,
            (cutoff,),
        )
        totals = dict(cur3.fetchone()) if cur3 else {}

        return {
            "daily": daily,
            "by_model": by_model,
            "totals": totals,
            "period_days": days,
        }
    finally:
        db.close()


# ── Config endpoints (wave 3) ──────────────────────────────────────────


class _ConfigUpdate(BaseModel):
    config: dict


class _ConfigRawUpdate(BaseModel):
    yaml_text: str


@app.get("/api/config")
async def get_config():
    """Return the current ``~/.phalanx/config.yaml`` parsed as a dict.

    Falls back to ``{}`` when the file doesn't exist (fresh install).
    Internal underscore-prefixed keys are filtered out so the SPA
    doesn't echo state-meta back through PUT.
    """
    cfg = load_config()
    return {k: v for k, v in cfg.items() if not k.startswith("_")}


@app.put("/api/config")
async def update_config(body: _ConfigUpdate):
    """Replace ``~/.phalanx/config.yaml`` with the supplied dict.

    No deep-merge — the SPA round-trips the full config from GET, edits
    a few fields, and PUTs the whole thing back.  This matches upstream
    semantics and avoids the "config file shrinks because the SPA only
    sent half the keys" trap.
    """
    from hermes_cli.config import save_config

    save_config(body.config)
    return {"ok": True}


@app.get("/api/config/raw")
async def get_config_raw():
    """Return the YAML text exactly as on disk.

    Lets the ConfigPage's "Raw YAML" tab display formatting / comments
    that ``load_config`` would silently strip when round-tripped through
    the parser.
    """
    from hermes_constants import get_config_path

    path = get_config_path()
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    return {"yaml": text}


@app.put("/api/config/raw")
async def update_config_raw(body: _ConfigRawUpdate):
    """Atomically write raw YAML text to ``~/.phalanx/config.yaml``.

    Validates parseability before writing — bad YAML returns 400 instead
    of corrupting the file.
    """
    import yaml

    from hermes_cli.config import ensure_hermes_home
    from hermes_constants import get_config_path

    try:
        parsed = yaml.safe_load(body.yaml_text)
    except yaml.YAMLError as exc:
        raise HTTPException(
            status_code=400, detail=f"Invalid YAML: {exc}"
        ) from exc
    if parsed is not None and not isinstance(parsed, dict):
        raise HTTPException(
            status_code=400,
            detail=f"Top-level YAML must be a dict, got {type(parsed).__name__}",
        )

    ensure_hermes_home()
    path = get_config_path()
    path.write_text(body.yaml_text, encoding="utf-8")

    # Bust the load_config cache so the next GET reflects the write.
    from hermes_cli.config import _RAW_CONFIG_CACHE

    _RAW_CONFIG_CACHE.pop(str(path), None)
    return {"ok": True}


@app.get("/api/config/defaults")
async def get_config_defaults():
    """Canonical defaults — used by the "Reset" button on each field."""
    from hermes_cli.config import DEFAULT_CONFIG

    return DEFAULT_CONFIG


@app.get("/api/config/schema")
async def get_config_schema():
    """Auto-inferred schema for the ConfigPage form renderer.

    Type / select-option overrides are baked in for fields where the
    default value alone doesn't carry the intent (e.g. ``model.provider``
    is a string default but renders as a select).
    """
    from hermes_cli.config import build_config_schema

    fields = build_config_schema()
    # Stable category order — providers / agent first, phalanx-specific last.
    seen: list[str] = []
    for entry in fields.values():
        cat = entry.get("category", "")
        if cat and cat not in seen:
            seen.append(cat)
    return {"fields": fields, "category_order": seen}


# ── Env endpoints (wave 3) ─────────────────────────────────────────────


class _EnvVarWrite(BaseModel):
    key: str
    value: str = ""


class _EnvVarDelete(BaseModel):
    key: str


class _EnvVarReveal(BaseModel):
    key: str


# Reveal rate limit — the dashboard shouldn't be a brute-force oracle.
_REVEAL_TIMESTAMPS: list[float] = []
_REVEAL_MAX_PER_WINDOW = 5
_REVEAL_WINDOW_SECONDS = 30


def _env_path() -> "Path":
    """Resolve ``~/.phalanx/.env`` lazily so monkeypatched ``PHALANX_HOME``
    in tests redirects everything in this module without re-imports."""
    return get_env_path()


@app.get("/api/env")
async def get_env_vars():
    """Enumerate the optional env vars phalanx knows about.

    Returns ``{is_set, redacted_value, description, ...}`` per key; the
    actual value is never returned here — :func:`reveal_env_var` is the
    only path that surfaces secrets, and only with a token + rate limit.
    """
    from hermes_cli.config import OPTIONAL_ENV_VARS, redact_key
    from hermes_cli.env_loader import read_env_file

    on_disk = read_env_file(_env_path())
    out: dict[str, dict] = {}
    for var, info in OPTIONAL_ENV_VARS.items():
        value = on_disk.get(var) or os.environ.get(var, "")
        out[var] = {
            "is_set": bool(value),
            "redacted_value": redact_key(value) if value else None,
            "description": info.get("description", ""),
            "url": info.get("url"),
            "category": info.get("category", ""),
            "is_password": info.get("is_password", False),
            "advanced": info.get("advanced", False),
        }
    return out


@app.put("/api/env")
async def set_env_var(body: _EnvVarWrite):
    from hermes_cli.env_loader import save_env_value

    try:
        save_env_value(_env_path(), body.key, body.value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "key": body.key}


@app.delete("/api/env")
async def remove_env_var(body: _EnvVarDelete):
    from hermes_cli.env_loader import remove_env_value

    if not remove_env_value(_env_path(), body.key):
        raise HTTPException(
            status_code=404, detail=f"{body.key} not found in .env"
        )
    return {"ok": True, "key": body.key}


@app.post("/api/env/reveal")
async def reveal_env_var(body: _EnvVarReveal):
    """Return the unredacted value of a single env var.

    Already gated by the auth middleware on /api/.  Adds a sliding-window
    rate limit so a compromised XSS context can't drain every secret in
    one burst.  Audit-logs the key name.
    """
    from hermes_cli.env_loader import read_env_file

    now = time.time()
    cutoff = now - _REVEAL_WINDOW_SECONDS
    _REVEAL_TIMESTAMPS[:] = [t for t in _REVEAL_TIMESTAMPS if t > cutoff]
    if len(_REVEAL_TIMESTAMPS) >= _REVEAL_MAX_PER_WINDOW:
        raise HTTPException(
            status_code=429,
            detail="Too many reveal requests. Try again shortly.",
        )
    _REVEAL_TIMESTAMPS.append(now)

    on_disk = read_env_file(_env_path())
    value = on_disk.get(body.key)
    if value is None:
        raise HTTPException(
            status_code=404, detail=f"{body.key} not found in .env"
        )
    _log.info("env/reveal: %s", body.key)
    return {"key": body.key, "value": value}


# ── SPA mount ──────────────────────────────────────────────────────────


def mount_spa(application: "FastAPI") -> None:
    """Mount the built SPA. Falls back to ``index.html`` for client-side routing.

    When ``WEB_DIST`` doesn't exist (first-time install before ``npm run
    build``), serves a JSON 404 with a recovery hint.

    The session token is injected into ``index.html`` via a ``<script>``
    tag so the SPA can authenticate against protected endpoints without a
    separate (unauthenticated) token-dispensing endpoint.
    """
    if not WEB_DIST.exists():

        @application.get("/{full_path:path}")
        async def no_frontend(full_path: str):
            return JSONResponse(
                {"error": "Frontend not built. Run: cd web && npm run build"},
                status_code=404,
            )

        return

    _index_path = WEB_DIST / "index.html"

    def _serve_index():
        """Return ``index.html`` with the session token injected."""
        html = _index_path.read_text(encoding="utf-8")
        token_script = (
            f'<script>window.__HERMES_SESSION_TOKEN__="{_SESSION_TOKEN}";</script>'
        )
        html = html.replace("</head>", f"{token_script}</head>", 1)
        return HTMLResponse(
            html,
            headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
        )

    assets_dir = WEB_DIST / "assets"
    if assets_dir.exists():
        application.mount(
            "/assets", StaticFiles(directory=assets_dir), name="assets"
        )

    @application.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        file_path = WEB_DIST / full_path
        # Prevent path traversal (%2e%2e/) — resolved path must stay inside WEB_DIST.
        if (
            full_path
            and file_path.resolve().is_relative_to(WEB_DIST.resolve())
            and file_path.exists()
            and file_path.is_file()
        ):
            return FileResponse(file_path)
        return _serve_index()


mount_spa(app)


# ── Server entry ───────────────────────────────────────────────────────


def start_server(
    host: str = "127.0.0.1",
    port: int = 9119,
    *,
    open_browser: bool = True,
    token: Optional[str] = None,
    allow_public: bool = False,
) -> None:
    """Start the FastAPI dashboard with a single uvicorn worker.

    A single worker is intentional: ``_SESSION_TOKEN`` is module-level
    state, and multi-worker forking would give each fork a different
    token — every other request would 401.  Phalanx's dashboard is a
    personal-developer tool; multi-worker / multi-user is a §2.8+ concern.
    """
    import uvicorn

    if token:
        _set_session_token(token)

    _LOCALHOST = ("127.0.0.1", "localhost", "::1")
    if host not in _LOCALHOST and not allow_public:
        raise SystemExit(
            f"Refusing to bind to {host} — the dashboard exposes API keys "
            f"and config. Use --insecure to override (NOT recommended on "
            f"untrusted networks)."
        )
    if host not in _LOCALHOST:
        _log.warning(
            "Binding to %s with --insecure — the dashboard has no robust "
            "authentication. Only use on trusted networks.",
            host,
        )

    # Stash bound host so host_header_middleware can validate.
    app.state.bound_host = host
    app.state.bound_port = port

    if open_browser:
        import webbrowser

        def _open():
            time.sleep(1.0)
            webbrowser.open(f"http://{host}:{port}")

        threading.Thread(target=_open, daemon=True).start()

    print(f"  Phalanx Web UI → http://{host}:{port}")
    print(f"  Session token: {_SESSION_TOKEN}")
    uvicorn.run(app, host=host, port=port, log_level="warning")
