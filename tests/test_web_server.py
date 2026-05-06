"""Web dashboard backend tests (Phase 2.7 wave 1).

Wave 1 covers the FastAPI app skeleton: token auth middleware,
Host-header DNS rebinding guard, ``/api/status`` shape, and the
"Frontend not built" SPA fallback.  Wave 2-3 endpoints (sessions /
logs / analytics / config / env) ship in subsequent test modules.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("starlette")

from fastapi.testclient import TestClient

from hermes_cli import web_server


@pytest.fixture
def client(monkeypatch):
    """Fresh TestClient with bound_host unset (so Host guard is bypassed
    by default — most tests want to focus on auth, not Host).

    The session token survives across tests; tests needing a fresh token
    monkeypatch ``web_server._SESSION_TOKEN`` directly.
    """
    # Tests bypass Host-header middleware unless they explicitly set bound_host
    monkeypatch.setattr(web_server.app.state, "bound_host", None, raising=False)
    return TestClient(web_server.app)


# ── /api/status (public, no token needed) ─────────────────────────────


def test_status_returns_required_fields(client):
    res = client.get("/api/status")
    assert res.status_code == 200
    body = res.json()
    # Phalanx-specific fields — gateway_* / oauth_* deliberately absent.
    for key in (
        "version",
        "release_date",
        "phalanx_home",
        "config_path",
        "env_path",
        "model",
        "base_url",
        "provider",
        "session_count",
        "active_sessions",
        "tools",
    ):
        assert key in body, f"missing field: {key}"
    assert isinstance(body["session_count"], int)
    assert isinstance(body["active_sessions"], int)
    assert isinstance(body["tools"], list)


def test_status_is_public_no_token_required(client):
    """Status is in _PUBLIC_API_PATHS — works without any auth header."""
    assert "/api/status" in web_server._PUBLIC_API_PATHS
    res = client.get("/api/status")
    assert res.status_code == 200


# ── Auth middleware ───────────────────────────────────────────────────


def test_protected_endpoint_rejects_missing_token(client):
    """Any /api/ path not in PUBLIC requires a token — even if the route
    doesn't exist (auth middleware runs before routing)."""
    res = client.get("/api/nonexistent")
    assert res.status_code == 401
    assert res.json() == {"detail": "Unauthorized"}


def test_protected_endpoint_rejects_wrong_token(client):
    res = client.get(
        "/api/nonexistent",
        headers={web_server._SESSION_HEADER_NAME: "wrong-token"},
    )
    assert res.status_code == 401


def test_protected_endpoint_with_session_header_passes_auth(client):
    """Right token via X-Hermes-Session-Token gets past auth (404 because
    the path itself doesn't exist — auth no longer the gate)."""
    res = client.get(
        "/api/nonexistent",
        headers={web_server._SESSION_HEADER_NAME: web_server._SESSION_TOKEN},
    )
    assert res.status_code == 404


def test_protected_endpoint_with_bearer_passes_auth(client):
    """Bearer Authorization is also accepted — keeps curl recipes compatible."""
    res = client.get(
        "/api/nonexistent",
        headers={"Authorization": f"Bearer {web_server._SESSION_TOKEN}"},
    )
    assert res.status_code == 404


def test_set_session_token_overrides_module_singleton():
    """``_set_session_token`` replaces the global so tests / --token flag work."""
    original = web_server._SESSION_TOKEN
    try:
        web_server._set_session_token("forced-token")
        assert web_server._SESSION_TOKEN == "forced-token"
    finally:
        web_server._set_session_token(original)


# ── Host header (DNS rebinding guard) ─────────────────────────────────


def test_host_header_rejected_when_bound_to_loopback(monkeypatch):
    """When app.state.bound_host=127.0.0.1, a Host header pointing at an
    attacker hostname must 400."""
    monkeypatch.setattr(
        web_server.app.state, "bound_host", "127.0.0.1", raising=False
    )
    client = TestClient(web_server.app)
    res = client.get("/api/status", headers={"Host": "evil.com"})
    assert res.status_code == 400
    assert "Invalid Host header" in res.json()["detail"]


def test_host_header_accepts_loopback_aliases(monkeypatch):
    """``localhost`` / ``127.0.0.1`` / ``[::1]`` all valid when bound to loopback."""
    monkeypatch.setattr(
        web_server.app.state, "bound_host", "127.0.0.1", raising=False
    )
    client = TestClient(web_server.app)
    for host in ("localhost", "127.0.0.1", "127.0.0.1:9119", "[::1]"):
        res = client.get("/api/status", headers={"Host": host})
        assert res.status_code == 200, f"loopback alias {host!r} should pass"


def test_host_header_unset_bypasses_check(client):
    """``app.state.bound_host=None`` (default in tests) → no Host check."""
    res = client.get("/api/status", headers={"Host": "anything.com"})
    assert res.status_code == 200


def test_host_header_zero_bind_accepts_any(monkeypatch):
    """0.0.0.0 bind = operator opted into all-interfaces; Host check skipped."""
    monkeypatch.setattr(
        web_server.app.state, "bound_host", "0.0.0.0", raising=False
    )
    client = TestClient(web_server.app)
    res = client.get("/api/status", headers={"Host": "anywhere.example"})
    assert res.status_code == 200


# ── _is_accepted_host helper unit tests ───────────────────────────────


@pytest.mark.parametrize(
    "host_header,bound,expected",
    [
        # Loopback bound
        ("localhost",       "127.0.0.1", True),
        ("localhost:9119",  "127.0.0.1", True),
        ("127.0.0.1",       "127.0.0.1", True),
        ("127.0.0.1:9119",  "127.0.0.1", True),
        ("[::1]",           "127.0.0.1", True),
        ("[::1]:9119",      "127.0.0.1", True),
        ("evil.com",        "127.0.0.1", False),
        ("evil.com:9119",   "127.0.0.1", False),
        # Empty
        ("",                "127.0.0.1", False),
        # 0.0.0.0 — anything goes
        ("anywhere.test",   "0.0.0.0",   True),
        # Explicit non-loopback bind: exact match required
        ("phalanx.local",   "phalanx.local", True),
        ("evil.com",        "phalanx.local", False),
    ],
)
def test_is_accepted_host_table(host_header, bound, expected):
    assert web_server._is_accepted_host(host_header, bound) is expected
