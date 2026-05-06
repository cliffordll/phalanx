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
import sys
import threading
import time
from pathlib import Path
from typing import Optional

try:
    from fastapi import FastAPI, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
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
