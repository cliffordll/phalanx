"""Web dashboard backend tests — config / env (Phase 2.7 wave 3)."""

from __future__ import annotations

import time

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from hermes_cli import web_server
from hermes_cli.config import OPTIONAL_ENV_VARS


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def phalanx_home(tmp_path, monkeypatch):
    # Strip developer-shell exports BEFORE setting PHALANX_HOME — the
    # delenv loop must run first because OPTIONAL_ENV_VARS itself
    # contains PHALANX_HOME / PHALANX_TIMEZONE etc., and we want the
    # tmp redirect to survive.
    for key in OPTIONAL_ENV_VARS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def client(phalanx_home, monkeypatch):
    monkeypatch.setattr(web_server.app.state, "bound_host", None, raising=False)
    # Reset reveal rate limiter — module-level state survives across tests
    web_server._REVEAL_TIMESTAMPS.clear()
    return TestClient(web_server.app)


@pytest.fixture
def auth():
    return {web_server._SESSION_HEADER_NAME: web_server._SESSION_TOKEN}


# ── /api/config GET / PUT ─────────────────────────────────────────────


def test_get_config_empty_when_no_file(client, auth):
    res = client.get("/api/config", headers=auth)
    assert res.status_code == 200
    assert res.json() == {}


def test_get_config_returns_parsed_yaml(phalanx_home, client, auth):
    cfg_file = phalanx_home / "config.yaml"
    cfg_file.write_text("model:\n  default: gpt-4o-mini\n  base_url: https://x\n")
    res = client.get("/api/config", headers=auth)
    body = res.json()
    assert body["model"]["default"] == "gpt-4o-mini"
    assert body["model"]["base_url"] == "https://x"


def test_get_config_strips_underscore_keys(phalanx_home, client, auth):
    """Internal underscore keys (e.g. _config_version) shouldn't echo to SPA."""
    (phalanx_home / "config.yaml").write_text(
        "model:\n  default: gpt-4o\n_internal: secret\n"
    )
    res = client.get("/api/config", headers=auth)
    body = res.json()
    assert "_internal" not in body
    assert "model" in body


def test_put_config_writes_yaml(phalanx_home, client, auth):
    res = client.put(
        "/api/config",
        headers=auth,
        json={"config": {"model": {"default": "claude-sonnet-4.5"}}},
    )
    assert res.status_code == 200
    on_disk = (phalanx_home / "config.yaml").read_text()
    assert "claude-sonnet-4.5" in on_disk


def test_put_config_round_trips(phalanx_home, client, auth):
    """PUT then GET should return what we wrote."""
    payload = {"model": {"default": "qwen-max", "base_url": "https://q"}}
    client.put("/api/config", headers=auth, json={"config": payload})
    res = client.get("/api/config", headers=auth)
    assert res.json() == payload


# ── /api/config/raw GET / PUT ─────────────────────────────────────────


def test_get_config_raw_empty_when_no_file(client, auth):
    res = client.get("/api/config/raw", headers=auth)
    assert res.status_code == 200
    assert res.json() == {"yaml": ""}


def test_get_config_raw_returns_text(phalanx_home, client, auth):
    src = "# comment\nmodel:\n  default: x\n"
    (phalanx_home / "config.yaml").write_text(src)
    res = client.get("/api/config/raw", headers=auth)
    assert res.json() == {"yaml": src}


def test_put_config_raw_persists(phalanx_home, client, auth):
    src = "model:\n  default: hello\n"
    res = client.put(
        "/api/config/raw", headers=auth, json={"yaml_text": src}
    )
    assert res.status_code == 200
    assert (phalanx_home / "config.yaml").read_text() == src


def test_put_config_raw_rejects_invalid_yaml(client, auth):
    res = client.put(
        "/api/config/raw",
        headers=auth,
        json={"yaml_text": "model:\n  default: [unclosed"},
    )
    assert res.status_code == 400
    assert "Invalid YAML" in res.json()["detail"]


def test_put_config_raw_rejects_non_dict_root(client, auth):
    res = client.put(
        "/api/config/raw", headers=auth, json={"yaml_text": "- a list\n- not a dict\n"}
    )
    assert res.status_code == 400
    assert "must be a dict" in res.json()["detail"]


# ── /api/config/defaults + /api/config/schema ─────────────────────────


def test_config_defaults_returns_canonical(client, auth):
    res = client.get("/api/config/defaults", headers=auth)
    body = res.json()
    assert "model" in body
    assert "agent" in body
    assert body["agent"]["max_iterations"] == 90


def test_config_schema_lists_known_fields(client, auth):
    res = client.get("/api/config/schema", headers=auth)
    body = res.json()
    fields = body["fields"]
    assert "model.default" in fields
    assert "model.provider" in fields
    assert "agent.max_iterations" in fields

    # Type inference
    assert fields["agent.max_iterations"]["type"] == "number"
    assert fields["model.default"]["type"] == "string"

    # Override applied for select fields
    assert fields["model.provider"]["type"] == "select"
    assert "openai" in fields["model.provider"]["options"]


def test_config_schema_category_order_present(client, auth):
    res = client.get("/api/config/schema", headers=auth)
    body = res.json()
    assert isinstance(body["category_order"], list)
    assert len(body["category_order"]) >= 1


# ── /api/env GET / PUT / DELETE ───────────────────────────────────────


def test_env_get_lists_known_keys_unset_when_no_file(client, auth):
    res = client.get("/api/env", headers=auth)
    body = res.json()
    # Every OPTIONAL_ENV_VAR is enumerated.  The fixture sets PHALANX_HOME
    # itself (to redirect data writes into tmp); the rest must be unset
    # because the fixture stripped shell exports and there's no .env.
    for key in OPTIONAL_ENV_VARS:
        assert key in body
        if key == "PHALANX_HOME":
            assert body[key]["is_set"] is True
        else:
            assert body[key]["is_set"] is False
            assert body[key]["redacted_value"] is None


def test_env_get_redacts_set_value(phalanx_home, client, auth):
    (phalanx_home / ".env").write_text(
        'OPENAI_API_KEY="sk-aaaabbbbccccddddeeeeffff"\n'
    )
    res = client.get("/api/env", headers=auth)
    entry = res.json()["OPENAI_API_KEY"]
    assert entry["is_set"] is True
    # redact_key returns first4…last4
    assert entry["redacted_value"].startswith("sk-a")
    assert entry["redacted_value"].endswith("ffff")
    # Real value never appears in this response.
    assert "sk-aaaabbbbccccddddeeeeffff" not in str(res.json())


def test_env_put_writes_to_dotenv(phalanx_home, client, auth):
    res = client.put(
        "/api/env",
        headers=auth,
        json={"key": "OPENAI_API_KEY", "value": "sk-test-12345"},
    )
    assert res.status_code == 200
    text = (phalanx_home / ".env").read_text()
    assert "OPENAI_API_KEY=sk-test-12345" in text


def test_env_put_replaces_existing_line(phalanx_home, client, auth):
    (phalanx_home / ".env").write_text(
        "ANTHROPIC_API_KEY=old\nOTHER=keep\n"
    )
    client.put(
        "/api/env",
        headers=auth,
        json={"key": "ANTHROPIC_API_KEY", "value": "new"},
    )
    text = (phalanx_home / ".env").read_text()
    assert "ANTHROPIC_API_KEY=new" in text
    assert "ANTHROPIC_API_KEY=old" not in text
    assert "OTHER=keep" in text


def test_env_put_rejects_bad_key(client, auth):
    res = client.put(
        "/api/env", headers=auth, json={"key": "bad name", "value": "x"}
    )
    assert res.status_code == 400


def test_env_delete_removes_key(phalanx_home, client, auth):
    (phalanx_home / ".env").write_text("FIRECRAWL_API_KEY=abc\nKEEP=ok\n")
    res = client.request(
        "DELETE", "/api/env", headers=auth, json={"key": "FIRECRAWL_API_KEY"}
    )
    assert res.status_code == 200
    text = (phalanx_home / ".env").read_text()
    assert "FIRECRAWL_API_KEY" not in text
    assert "KEEP=ok" in text


def test_env_delete_404_when_missing(client, auth):
    res = client.request(
        "DELETE", "/api/env", headers=auth, json={"key": "OPENAI_API_KEY"}
    )
    assert res.status_code == 404


# ── /api/env/reveal ───────────────────────────────────────────────────


def test_env_reveal_returns_real_value(phalanx_home, client, auth):
    (phalanx_home / ".env").write_text("OPENAI_API_KEY=sk-realvalue\n")
    res = client.post(
        "/api/env/reveal", headers=auth, json={"key": "OPENAI_API_KEY"}
    )
    assert res.status_code == 200
    assert res.json() == {"key": "OPENAI_API_KEY", "value": "sk-realvalue"}


def test_env_reveal_404_for_unset(client, auth):
    res = client.post(
        "/api/env/reveal", headers=auth, json={"key": "OPENAI_API_KEY"}
    )
    assert res.status_code == 404


def test_env_reveal_rate_limited_after_5(phalanx_home, client, auth):
    (phalanx_home / ".env").write_text("OPENAI_API_KEY=x\n")
    for _ in range(5):
        res = client.post(
            "/api/env/reveal", headers=auth, json={"key": "OPENAI_API_KEY"}
        )
        assert res.status_code == 200
    # 6th call within the window must 429
    res = client.post(
        "/api/env/reveal", headers=auth, json={"key": "OPENAI_API_KEY"}
    )
    assert res.status_code == 429
    assert "Too many reveal" in res.json()["detail"]


def test_env_reveal_window_slides(phalanx_home, client, auth, monkeypatch):
    """After the 30s window expires, reveals reset."""
    (phalanx_home / ".env").write_text("OPENAI_API_KEY=x\n")
    # Stuff the limiter's history with 5 timestamps in the past.
    web_server._REVEAL_TIMESTAMPS[:] = [time.time() - 60.0] * 5
    res = client.post(
        "/api/env/reveal", headers=auth, json={"key": "OPENAI_API_KEY"}
    )
    assert res.status_code == 200


# ── Auth: every wave-3 endpoint requires token ────────────────────────


@pytest.mark.parametrize(
    "method,path,kwargs",
    [
        ("GET",    "/api/config",          {}),
        ("PUT",    "/api/config",          {"json": {"config": {}}}),
        ("GET",    "/api/config/raw",      {}),
        ("PUT",    "/api/config/raw",      {"json": {"yaml_text": ""}}),
        ("GET",    "/api/config/defaults", {}),
        ("GET",    "/api/config/schema",   {}),
        ("GET",    "/api/env",             {}),
        ("PUT",    "/api/env",             {"json": {"key": "K", "value": "v"}}),
        ("DELETE", "/api/env",             {"json": {"key": "K"}}),
        ("POST",   "/api/env/reveal",      {"json": {"key": "K"}}),
    ],
)
def test_wave3_endpoints_require_token(client, method, path, kwargs):
    res = client.request(method, path, **kwargs)
    assert res.status_code == 401
