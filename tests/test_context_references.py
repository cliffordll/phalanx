"""§2.8.b wave 3 tests — inline @-reference resolver + AIAgent + REPL + web.

Three layers under test:

* :mod:`agent.context_references` — regex parsing, individual handlers
  (file / diff / url / session), end-to-end ``resolve_references``.
* :meth:`run_agent.AIAgent._expand_user_references` — wire-in inside
  ``run_conversation``; ``_last_resolved_refs`` is exposed for
  introspection by /ref show.
* CLI ``/ref`` slash command + web ``POST /api/references/resolve``
  endpoint.

Network-touching paths (``@url:``) are tested via handler injection,
not real HTTP — keeps the suite fast and deterministic.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

import pytest

from agent.context_references import (
    ReferenceError,
    ResolvedRef,
    parse_references,
    resolve_references,
)


# ── parse_references ──────────────────────────────────────────────────


def test_parse_empty_text_returns_nothing():
    assert parse_references("") == []
    assert parse_references("no at signs at all") == []


def test_parse_single_file_reference():
    refs = parse_references("review @file:src/main.py please")
    assert len(refs) == 1
    assert refs[0].kind == "file"
    assert refs[0].value == "src/main.py"
    assert refs[0].raw == "@file:src/main.py"


def test_parse_bare_diff_and_diff_with_ref():
    refs = parse_references("compare @diff and @diff:HEAD~3")
    kinds_values = [(r.kind, r.value) for r in refs]
    assert kinds_values == [("diff", ""), ("diff", "HEAD~3")]


def test_parse_url_keeps_full_url():
    refs = parse_references("see @url:https://example.com/path?q=1")
    assert refs[0].kind == "url"
    assert refs[0].value == "https://example.com/path?q=1"


def test_parse_session_with_prefix():
    refs = parse_references("recall @session:abc12345")
    assert refs[0].kind == "session"
    assert refs[0].value == "abc12345"


def test_parse_does_not_match_email_or_decimal():
    """Negative lookbehind rules out email/decimal-like @-uses."""
    assert parse_references("contact email@domain.com") == []
    assert parse_references("v1.2@beta") == []


def test_parse_skips_unknown_kind():
    """A typo like @flie: is left alone, not silently mis-resolved."""
    refs = parse_references("typo @flie:src/main.py")
    assert refs == []


def test_parse_multiple_references_in_order():
    refs = parse_references(
        "look at @file:a.py and @file:b.py then run @diff:main"
    )
    assert [r.kind for r in refs] == ["file", "file", "diff"]
    assert [r.value for r in refs] == ["a.py", "b.py", "main"]
    # Spans monotonic.
    spans = [r.span for r in refs]
    assert all(spans[i][1] <= spans[i + 1][0] for i in range(len(spans) - 1))


def test_parse_value_stops_at_whitespace_and_close_paren():
    refs = parse_references("(see @file:foo/bar.py) for @diff")
    assert [r.value for r in refs] == ["foo/bar.py", ""]


# ── handler-injection harness ─────────────────────────────────────────


def _stub_handlers(**overrides):
    """Build a handler dict with explicit per-kind callables for tests.

    Every kind not in *overrides* raises ReferenceError so an
    unintentional fallthrough surfaces as a clear failure.
    """
    base = {
        "file":    lambda v, ctx: (_ for _ in ()).throw(
            ReferenceError("file handler not stubbed")
        ),
        "diff":    lambda v, ctx: (_ for _ in ()).throw(
            ReferenceError("diff handler not stubbed")
        ),
        "url":     lambda v, ctx: (_ for _ in ()).throw(
            ReferenceError("url handler not stubbed")
        ),
        "session": lambda v, ctx: (_ for _ in ()).throw(
            ReferenceError("session handler not stubbed")
        ),
    }
    base.update(overrides)
    return base


# ── @file: handler ────────────────────────────────────────────────────


def test_file_handler_reads_existing_file(tmp_path):
    target = tmp_path / "hello.txt"
    target.write_text("HELLO WORLD\n", encoding="utf-8")
    out, resolved = resolve_references(
        "show me @file:hello.txt please",
        cwd=str(tmp_path),
    )
    assert "<reference type=\"file\" key=\"hello.txt\">" in out
    assert "HELLO WORLD" in out
    assert resolved[0].error is None
    assert resolved[0].content_chars > 0


def test_file_handler_missing_file_renders_error(tmp_path):
    out, resolved = resolve_references(
        "@file:does_not_exist.txt",
        cwd=str(tmp_path),
    )
    assert resolved[0].error is not None
    assert "not found" in resolved[0].error
    # Error blocks render as self-closing <reference ... />.
    assert "<reference type=\"file\"" in out
    assert "/>" in out


def test_file_handler_blocks_traversal(tmp_path):
    parent = tmp_path / "parent"
    child = tmp_path / "parent" / "sub"
    child.mkdir(parents=True)
    secret = parent / "secret.txt"
    secret.write_text("SECRET", encoding="utf-8")
    # cwd is the child dir; @file:..\secret.txt should be rejected.
    out, resolved = resolve_references(
        "@file:../secret.txt",
        cwd=str(child),
    )
    assert resolved[0].error is not None
    assert "SECRET" not in out


def test_file_handler_directory_returns_error(tmp_path):
    (tmp_path / "subdir").mkdir()
    _, resolved = resolve_references(
        "@file:subdir",
        cwd=str(tmp_path),
    )
    assert resolved[0].error is not None
    assert "directory" in resolved[0].error


def test_file_handler_truncates_large_file(tmp_path):
    big = tmp_path / "big.txt"
    big.write_text("x" * 500_000, encoding="utf-8")
    _, resolved = resolve_references(
        "@file:big.txt", cwd=str(tmp_path),
    )
    assert resolved[0].error is None
    assert "truncated" in resolved[0].content


def test_file_handler_requires_value(tmp_path):
    """``@file`` without a colon-value should error, not crash."""
    out, resolved = resolve_references(
        "naked @file reference",
        cwd=str(tmp_path),
    )
    assert resolved[0].error is not None
    assert "requires a path" in resolved[0].error


# ── @diff handler (subprocess) ────────────────────────────────────────


def test_diff_handler_runs_in_repo(tmp_path, monkeypatch):
    """Stubs subprocess.run to assert the right git command goes out."""
    captured: Dict[str, Any] = {}

    class _Result:
        def __init__(self, returncode: int, stdout: str, stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["cwd"] = kwargs.get("cwd")
        return _Result(0, "diff --git a/foo b/foo\n+++ etc\n")

    monkeypatch.setattr("subprocess.run", _fake_run)
    out, resolved = resolve_references(
        "@diff:HEAD~3",
        cwd=str(tmp_path),
    )
    assert resolved[0].error is None
    assert "diff --git" in resolved[0].content
    assert captured["cmd"] == ["git", "diff", "HEAD~3"]
    assert captured["cwd"] == str(tmp_path)


def test_diff_handler_empty_returns_hint(tmp_path, monkeypatch):
    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr("subprocess.run", lambda *a, **kw: _Result())
    _, resolved = resolve_references("@diff", cwd=str(tmp_path))
    assert resolved[0].error is None
    assert "diff is empty" in resolved[0].content


def test_diff_handler_bad_ref_returns_error(tmp_path, monkeypatch):
    class _Result:
        returncode = 128
        stdout = ""
        stderr = "fatal: bad revision 'nope'\n"

    monkeypatch.setattr("subprocess.run", lambda *a, **kw: _Result())
    _, resolved = resolve_references("@diff:nope", cwd=str(tmp_path))
    assert resolved[0].error is not None
    assert "bad revision" in resolved[0].error


def test_diff_handler_rejects_unsafe_arg(tmp_path):
    _, resolved = resolve_references(
        "@diff:; rm -rf /",
        cwd=str(tmp_path),
    )
    # The regex parser stops the value at whitespace, so what gets passed
    # to the handler is just ``;``.  The handler refuses values containing
    # shell metacharacters.
    assert resolved[0].error is not None
    assert "unsafe" in resolved[0].error.lower()


def test_diff_handler_git_missing_renders_error(tmp_path, monkeypatch):
    def _raise(*a, **kw):
        raise FileNotFoundError("git: not found")

    monkeypatch.setattr("subprocess.run", _raise)
    _, resolved = resolve_references("@diff", cwd=str(tmp_path))
    assert resolved[0].error is not None


# ── @url: handler ─────────────────────────────────────────────────────


def test_url_handler_rejects_non_http():
    _, resolved = resolve_references("@url:file:///etc/passwd")
    assert resolved[0].error is not None
    assert "http" in resolved[0].error.lower()


def test_url_handler_requires_value():
    _, resolved = resolve_references("naked @url reference")
    assert resolved[0].error is not None


def test_url_handler_uses_urllib_text(monkeypatch):
    """Patch urllib.request.urlopen so the handler returns deterministic text."""
    class _FakeResp:
        headers = {"content-type": "text/plain; charset=utf-8"}

        def read(self, n):
            return b"page body bytes"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake_urlopen(req, timeout):
        return _FakeResp()

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    _, resolved = resolve_references(
        "see @url:https://example.com/page",
    )
    assert resolved[0].error is None
    assert "page body bytes" in resolved[0].content


def test_url_handler_rejects_binary_content_type(monkeypatch):
    class _FakeResp:
        headers = {"content-type": "application/octet-stream"}

        def read(self, n):
            return b"\x00\x01\x02"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: _FakeResp())
    _, resolved = resolve_references("@url:https://example.com/blob.bin")
    assert resolved[0].error is not None
    assert "octet-stream" in resolved[0].error or "non-text" in resolved[0].error


def test_url_handler_truncates_large_response(monkeypatch):
    class _FakeResp:
        headers = {"content-type": "text/plain"}

        def read(self, n):
            return b"y" * (n)  # exactly the cap+1

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: _FakeResp())
    _, resolved = resolve_references("@url:https://example.com/big")
    assert resolved[0].error is None
    assert "truncated" in resolved[0].content


# ── @session: handler ─────────────────────────────────────────────────


def test_session_handler_requires_db():
    _, resolved = resolve_references("@session:abc")
    assert resolved[0].error is not None
    assert "session DB" in resolved[0].error


def test_session_handler_resolves_existing(tmp_path):
    from hermes_state import SessionDB
    db = SessionDB(db_path=tmp_path / "state.db")
    sid = db.create_session("test-session-id-12345", source="cli")
    db.append_message(sid, "user", "what is the capital of france?")
    db.append_message(sid, "assistant", "Paris.")
    try:
        out, resolved = resolve_references(
            "remember @session:test-session-id",
            session_db=db,
        )
    finally:
        db.close()
    assert resolved[0].error is None
    assert "Paris" in resolved[0].content
    assert "session test-ses" in resolved[0].content


def test_session_handler_unknown_id(tmp_path):
    from hermes_state import SessionDB
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        _, resolved = resolve_references(
            "@session:nope-not-real",
            session_db=db,
        )
    finally:
        db.close()
    assert resolved[0].error is not None


# ── ReferenceResolver class + custom handlers ─────────────────────────


def test_resolver_accepts_handler_overrides():
    handlers = _stub_handlers(
        file=lambda v, ctx: f"FAKE FILE: {v}",
    )
    out, resolved = resolve_references(
        "@file:some/path.py", handlers=handlers,
    )
    assert "FAKE FILE: some/path.py" in resolved[0].content
    assert resolved[0].error is None


def test_resolver_renders_error_blocks_as_self_closing():
    handlers = _stub_handlers()  # all kinds raise ReferenceError
    out, resolved = resolve_references(
        "@file:nope @diff",
        handlers=handlers,
    )
    assert all(r.error is not None for r in resolved)
    # Both render as <reference ... />.  Match either order.
    assert out.count("/>") == 2


def test_resolver_swallows_handler_crash():
    """Non-ReferenceError exceptions get caught and rendered as 'crash'
    errors, not propagated."""
    def _crashy(value, ctx):
        raise RuntimeError("kaboom")

    handlers = _stub_handlers(file=_crashy)
    _, resolved = resolve_references(
        "@file:trigger", handlers=handlers,
    )
    assert resolved[0].error is not None
    assert "kaboom" in resolved[0].error


def test_resolver_no_op_when_no_at_signs(tmp_path):
    out, resolved = resolve_references("plain text", cwd=str(tmp_path))
    assert out == "plain text"
    assert resolved == []


def test_resolved_block_attr_quote_handles_special_chars():
    """Error attributes with quotes / newlines must not break the block."""
    def _err(value, ctx):
        raise ReferenceError('multi-line\nerror with "quotes"')

    handlers = _stub_handlers(file=_err)
    out, resolved = resolve_references("@file:x", handlers=handlers)
    # No raw newline / unescaped quote inside the attribute.
    block = out.splitlines()[-1]
    assert "\n" not in block.split("error=")[1].split("/>")[0]
    assert resolved[0].error.startswith("multi-line")


# ── AIAgent integration ──────────────────────────────────────────────


def test_aiagent_expand_user_references_no_at_returns_unchanged(monkeypatch):
    monkeypatch.setenv("PHALANX_HOME", str(Path(os.devnull).parent))
    from run_agent import AIAgent
    agent = AIAgent(model="dummy", base_url="", api_key="")
    out = agent._expand_user_references("plain message")
    assert out == "plain message"
    assert agent._last_resolved_refs == []


def test_aiagent_expand_user_references_records_outcome(monkeypatch, tmp_path):
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "code.py"
    target.write_text("def f(): pass\n", encoding="utf-8")

    from run_agent import AIAgent
    agent = AIAgent(model="dummy", base_url="", api_key="")
    out = agent._expand_user_references("review @file:code.py please")
    assert "<reference type=\"file\" key=\"code.py\">" in out
    assert "def f()" in out
    assert len(agent._last_resolved_refs) == 1
    assert agent._last_resolved_refs[0].error is None


def test_aiagent_expand_user_references_resets_between_turns(monkeypatch, tmp_path):
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "x.py").write_text("X", encoding="utf-8")
    from run_agent import AIAgent
    agent = AIAgent(model="dummy", base_url="", api_key="")
    agent._expand_user_references("read @file:x.py")
    assert len(agent._last_resolved_refs) == 1
    agent._expand_user_references("plain message")
    assert agent._last_resolved_refs == []


# ── /ref REPL slash command ───────────────────────────────────────────


def test_ref_slash_show_reports_last_resolved(capsys):
    from cli import _cmd_ref

    state = {
        "agent": type(
            "FakeAgent", (), {
                "_last_resolved_refs": [
                    ResolvedRef(type="file", key="a.py",
                                content="ok", content_chars=42),
                    ResolvedRef(type="diff", key="",
                                error="not a git repo"),
                ],
            },
        )(),
    }
    out = _cmd_ref("show", state)
    assert out is None
    captured = capsys.readouterr().out
    assert "@file:a.py" in captured and "42" in captured
    assert "@diff" in captured and "not a git repo" in captured


def test_ref_slash_show_empty(capsys):
    from cli import _cmd_ref

    class _Stub:
        _last_resolved_refs = []

    _cmd_ref("show", {"agent": _Stub()})
    assert "no references resolved" in capsys.readouterr().out


def test_ref_slash_help(capsys):
    from cli import _cmd_ref
    _cmd_ref("help", {"agent": None})
    out = capsys.readouterr().out
    assert "@file:" in out and "@diff" in out and "@url:" in out
    assert "@session:" in out


# ── Web /api/references/resolve endpoint ─────────────────────────────


pytest.importorskip("fastapi")
pytest.importorskip("starlette")
from fastapi.testclient import TestClient  # noqa: E402

from hermes_cli import web_server  # noqa: E402


@pytest.fixture
def web_client(monkeypatch, tmp_path):
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    monkeypatch.setattr(
        web_server.app.state, "bound_host", None, raising=False,
    )
    return TestClient(web_server.app)


def _auth(headers: Dict[str, str] | None = None) -> Dict[str, str]:
    h = dict(headers or {})
    h[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
    h["Content-Type"] = "application/json"
    return h


def test_web_resolve_no_refs(web_client):
    res = web_client.post(
        "/api/references/resolve",
        headers=_auth(),
        content=json.dumps({"text": "plain text without at signs"}),
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["rewritten_text"] == "plain text without at signs"
    assert body["resolved"] == []


def test_web_resolve_file(monkeypatch, tmp_path, web_client):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "hello.txt").write_text("WEB-HELLO", encoding="utf-8")
    res = web_client.post(
        "/api/references/resolve",
        headers=_auth(),
        content=json.dumps({"text": "look at @file:hello.txt"}),
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert "WEB-HELLO" in body["rewritten_text"]
    assert len(body["resolved"]) == 1
    assert body["resolved"][0]["type"] == "file"
    assert body["resolved"][0]["error"] is None


def test_web_resolve_requires_auth(web_client):
    res = web_client.post(
        "/api/references/resolve",
        headers={"Content-Type": "application/json"},
        content=json.dumps({"text": "hi"}),
    )
    assert res.status_code in (401, 403)


def test_web_resolve_error_block_carries_through(web_client):
    res = web_client.post(
        "/api/references/resolve",
        headers=_auth(),
        content=json.dumps({"text": "@file:does_not_exist_anywhere.xyz"}),
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["resolved"][0]["error"] is not None
