"""Web dashboard backend tests — sessions / logs / analytics (Phase 2.7 wave 2)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from hermes_cli import web_server
from hermes_state import SessionDB


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def phalanx_home(tmp_path, monkeypatch):
    """Redirect ~/.phalanx to tmp_path so SessionDB / log paths land
    inside the per-test sandbox.  Both endpoint code and test setup
    read the same env, so they end up looking at the same files."""
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture
def client(phalanx_home, monkeypatch):
    monkeypatch.setattr(web_server.app.state, "bound_host", None, raising=False)
    return TestClient(web_server.app)


@pytest.fixture
def auth(monkeypatch):
    """Header dict with a valid session token — applied per-request."""
    return {web_server._SESSION_HEADER_NAME: web_server._SESSION_TOKEN}


def _seed_session(
    tmp: Path,
    session_id: str,
    *,
    started_at: Optional[float] = None,
    model: Optional[str] = None,
    title: Optional[str] = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost: float = 0.0,
    user_message: Optional[str] = None,
    end_reason: Optional[str] = None,
) -> None:
    db = SessionDB(db_path=tmp / "state.db")
    try:
        db.create_session(session_id, source="oneshot")
        if started_at is not None:
            db._conn.execute(
                "UPDATE sessions SET started_at = ? WHERE id = ?",
                (started_at, session_id),
            )
            db._conn.commit()
        if model or input_tokens or output_tokens or cost:
            db.update_token_counts(
                session_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                model=model,
                estimated_cost_usd=cost or None,
            )
        if title is not None:
            db.set_session_title(session_id, title)
        if user_message is not None:
            db.append_message(session_id, role="user", content=user_message)
        if end_reason:
            db.end_session(session_id, end_reason=end_reason)
    finally:
        db.close()


# ── /api/sessions list / detail / messages ────────────────────────────


def test_sessions_list_empty(client, auth):
    res = client.get("/api/sessions", headers=auth)
    assert res.status_code == 200
    assert res.json() == {"sessions": [], "total": 0, "limit": 20, "offset": 0}


def test_sessions_list_returns_seeded_rows(phalanx_home, client, auth):
    _seed_session(phalanx_home, "sess_a", user_message="hi from a")
    _seed_session(phalanx_home, "sess_b", user_message="hi from b")
    res = client.get("/api/sessions", headers=auth)
    body = res.json()
    assert body["total"] == 2
    ids = {s["id"] for s in body["sessions"]}
    assert ids == {"sess_a", "sess_b"}
    # is_active is computed on the response, not stored
    for s in body["sessions"]:
        assert "is_active" in s


def test_sessions_list_pagination(phalanx_home, client, auth):
    for i in range(5):
        _seed_session(phalanx_home, f"sess_{i}")
    res = client.get("/api/sessions?limit=2&offset=1", headers=auth)
    body = res.json()
    assert body["total"] == 5
    assert body["limit"] == 2
    assert body["offset"] == 1
    assert len(body["sessions"]) == 2


def test_session_detail_by_full_id(phalanx_home, client, auth):
    _seed_session(phalanx_home, "sess_full", title="my session")
    res = client.get("/api/sessions/sess_full", headers=auth)
    assert res.status_code == 200
    assert res.json()["id"] == "sess_full"
    assert res.json()["title"] == "my session"


def test_session_detail_resolves_prefix(phalanx_home, client, auth):
    _seed_session(phalanx_home, "sess_uniqueprefix")
    res = client.get("/api/sessions/sess_uniqu", headers=auth)
    assert res.status_code == 200
    assert res.json()["id"] == "sess_uniqueprefix"


def test_session_detail_404(client, auth):
    res = client.get("/api/sessions/nope", headers=auth)
    assert res.status_code == 404


def test_session_messages_round_trip(phalanx_home, client, auth):
    _seed_session(phalanx_home, "sess_m", user_message="hello")
    res = client.get("/api/sessions/sess_m/messages", headers=auth)
    assert res.status_code == 200
    body = res.json()
    assert body["session_id"] == "sess_m"
    roles = [m["role"] for m in body["messages"]]
    assert "user" in roles


def test_session_messages_404(client, auth):
    res = client.get("/api/sessions/missing/messages", headers=auth)
    assert res.status_code == 404


# ── DELETE /api/sessions/{id} ─────────────────────────────────────────


def test_delete_session_ok(phalanx_home, client, auth):
    _seed_session(phalanx_home, "sess_to_del")
    res = client.delete("/api/sessions/sess_to_del", headers=auth)
    assert res.status_code == 200
    assert res.json() == {"ok": True}
    res2 = client.get("/api/sessions/sess_to_del", headers=auth)
    assert res2.status_code == 404


def test_delete_session_idempotent_404(client, auth):
    res = client.delete("/api/sessions/never_existed", headers=auth)
    assert res.status_code == 404


# ── PUT /api/sessions/{id}/title ──────────────────────────────────────


def test_set_title_ok(phalanx_home, client, auth):
    _seed_session(phalanx_home, "sess_t")
    res = client.put(
        "/api/sessions/sess_t/title",
        headers=auth,
        json={"title": "renamed"},
    )
    assert res.status_code == 200
    detail = client.get("/api/sessions/sess_t", headers=auth).json()
    assert detail["title"] == "renamed"


def test_set_title_clear_with_null(phalanx_home, client, auth):
    _seed_session(phalanx_home, "sess_clear", title="existing")
    res = client.put(
        "/api/sessions/sess_clear/title",
        headers=auth,
        json={"title": None},
    )
    assert res.status_code == 200
    detail = client.get("/api/sessions/sess_clear", headers=auth).json()
    assert detail["title"] is None


def test_set_title_conflict_409(phalanx_home, client, auth):
    _seed_session(phalanx_home, "sess_a", title="taken")
    _seed_session(phalanx_home, "sess_b")
    res = client.put(
        "/api/sessions/sess_b/title",
        headers=auth,
        json={"title": "taken"},
    )
    assert res.status_code == 409
    assert "taken" in res.json()["detail"]


def test_set_title_404_unknown_id(client, auth):
    res = client.put(
        "/api/sessions/nope/title",
        headers=auth,
        json={"title": "x"},
    )
    assert res.status_code == 404


# ── /api/logs ─────────────────────────────────────────────────────────


def test_logs_unknown_file_400(client, auth):
    res = client.get("/api/logs?file=nonexistent", headers=auth)
    assert res.status_code == 400
    assert "Unknown log file" in res.json()["detail"]


def test_logs_missing_file_returns_empty(phalanx_home, client, auth):
    """``logs/agent.log`` doesn't exist on a fresh tmp install — endpoint
    returns an empty ``lines`` list rather than 500."""
    res = client.get("/api/logs?file=agent", headers=auth)
    assert res.status_code == 200
    assert res.json() == {"file": "agent", "lines": []}


def test_logs_returns_tail(phalanx_home, client, auth):
    log = phalanx_home / "logs" / "agent.log"
    log.write_text(
        "2026-04-05 22:35:00 INFO agent: hi line 1\n"
        "2026-04-05 22:35:01 INFO agent: hi line 2\n"
    )
    res = client.get("/api/logs?file=agent", headers=auth)
    body = res.json()
    assert body["file"] == "agent"
    # _read_tail returns each line; we wrote 2.  May or may not include
    # trailing newline depending on splitter — just check substrings.
    joined = "\n".join(body["lines"])
    assert "hi line 1" in joined
    assert "hi line 2" in joined


def test_logs_unknown_component_400(phalanx_home, client, auth):
    (phalanx_home / "logs" / "agent.log").write_text("x\n")
    res = client.get("/api/logs?file=agent&component=bogus", headers=auth)
    assert res.status_code == 400
    assert "Unknown component" in res.json()["detail"]


# ── /api/analytics/usage ──────────────────────────────────────────────


def test_analytics_empty_buckets_when_no_sessions(client, auth):
    res = client.get("/api/analytics/usage?days=7", headers=auth)
    assert res.status_code == 200
    body = res.json()
    assert body["daily"] == []
    assert body["by_model"] == []
    assert body["totals"]["total_sessions"] == 0
    assert body["period_days"] == 7


def test_analytics_aggregates_two_sessions(phalanx_home, client, auth):
    now = time.time()
    _seed_session(
        phalanx_home, "sess_x", started_at=now,
        model="gpt-4o-mini", input_tokens=100, output_tokens=50, cost=0.01,
    )
    _seed_session(
        phalanx_home, "sess_y", started_at=now,
        model="gpt-4o-mini", input_tokens=200, output_tokens=80, cost=0.02,
    )
    res = client.get("/api/analytics/usage?days=30", headers=auth)
    body = res.json()
    assert body["totals"]["total_sessions"] == 2
    assert body["totals"]["total_input"] == 300
    assert body["totals"]["total_output"] == 130
    assert pytest.approx(body["totals"]["total_estimated_cost"], 1e-9) == 0.03
    assert len(body["by_model"]) == 1
    assert body["by_model"][0]["model"] == "gpt-4o-mini"
    assert body["by_model"][0]["sessions"] == 2


def test_analytics_days_clamped(client, auth):
    """Out-of-range days should clamp rather than 500."""
    res = client.get("/api/analytics/usage?days=99999", headers=auth)
    assert res.status_code == 200
    assert res.json()["period_days"] == 365
    res2 = client.get("/api/analytics/usage?days=0", headers=auth)
    assert res2.status_code == 200
    assert res2.json()["period_days"] == 1


# ── Auth: wave-2 endpoints all require token ──────────────────────────


@pytest.mark.parametrize(
    "method,path,kwargs",
    [
        ("GET",    "/api/sessions",              {}),
        ("GET",    "/api/sessions/x",            {}),
        ("GET",    "/api/sessions/x/messages",   {}),
        ("DELETE", "/api/sessions/x",            {}),
        ("PUT",    "/api/sessions/x/title",      {"json": {"title": "y"}}),
        ("GET",    "/api/logs",                  {}),
        ("GET",    "/api/analytics/usage",       {}),
    ],
)
def test_wave2_endpoints_require_token(client, method, path, kwargs):
    res = client.request(method, path, **kwargs)
    assert res.status_code == 401
