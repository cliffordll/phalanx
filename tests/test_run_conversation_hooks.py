"""§2.8.b wave 4 — three-hook integration regression test.

Wave 1 / 2 / 3 each added one independent hook to ``run_conversation``:

* turn 0 → memory injection prepended to the assembled system prompt
* user input → ``@`` reference expansion in the user message
* preflight → context compression of the messages list

This test boots a single AIAgent with all three triggers active in
the same turn and asserts the resulting API request seen by the
stub OpenAI client looks exactly the way it should — i.e. the hooks
compose without stomping on each other.

Specifically the regressions guarded against are:

* memory block disappearing because reference expansion overwrote
  the system slot
* @file: expansion failing because the resolver was never bound
  before persistence
* compression preflight using the EXPANDED user message as
  ``focus_topic`` (which would balloon the summariser's prompt
  with the full inlined file).
"""

from __future__ import annotations

from hermes_state import SessionDB
from run_agent import AIAgent
from tests.conftest import make_text_response


def test_three_hooks_compose_in_one_turn(stub_openai, monkeypatch, tmp_path):
    """Memory + reference + compression all fire on one turn and
    produce a coherent request payload."""
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    # 1. A memory in the DB that the retrieval should pick up.
    db = SessionDB(db_path=tmp_path / "state.db")
    db.store_memory(
        "preference", "user prefers terse pytest-style answers",
        scope="global", pinned=True,
    )

    # 2. A file the @file: reference will read.
    target_file = tmp_path / "calc.py"
    target_file.write_text(
        "def add(a, b):\n    return a + b\n",
        encoding="utf-8",
    )

    # 3. Stub OpenAI returns a single text response so the loop exits
    # after one API call (we want to inspect the FIRST request, not
    # drive a multi-turn flow).
    stub = stub_openai([make_text_response("done")])

    agent = AIAgent(
        model="gpt-test", api_key="sk-x", base_url="https://x/v1",
        session_db=db,
    )

    try:
        result = agent.run_conversation(
            "review @file:calc.py and respond briefly"
        )
    finally:
        agent.close()
        db.close()

    # ── Assertions on the API payload ──
    assert len(stub.calls) == 1, "expected exactly one API call"
    payload = stub.calls[0]
    sent = payload["messages"]

    # First message is the system prompt; it must include the memory
    # envelope.
    sys_msg = sent[0]
    assert sys_msg["role"] == "system"
    assert "<memory-context>" in sys_msg["content"]
    assert "user prefers terse pytest-style answers" in sys_msg["content"]

    # Last user message is the EXPANDED @file: reference: the original
    # user prose stays as a prefix, the resolved <reference> block is
    # appended.
    user_msg = next(m for m in sent if m["role"] == "user")
    assert "review @file:calc.py" in user_msg["content"]
    assert '<reference type="file" key="calc.py">' in user_msg["content"]
    assert "def add(a, b)" in user_msg["content"]

    # The agent's last_resolved_refs surfaces exactly one resolution.
    assert len(agent._last_resolved_refs) == 1
    assert agent._last_resolved_refs[0].error is None
    assert agent._last_resolved_refs[0].type == "file"

    # Conversation result reflects what came back from the stub.
    assert result["final_response"] == "done"


def test_compression_focus_topic_uses_original_not_expanded(
    monkeypatch, tmp_path,
):
    """``focus_topic`` for the compressor must be the ORIGINAL user
    prompt, not the post-expansion text — otherwise an inlined 100KB
    file leaks into the summariser's user-prompt as 'agent is currently
    working on: ...'."""
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    # Force the compressor to actually trigger by:
    #   - small context_length so the threshold is crossed by little
    #     payload
    #   - long enough messages list to pass _COMPRESS_PROBE_FLOOR
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *a, **kw: 200,
    )

    captured: dict = {}

    # Stub the auxiliary client: capture the focus_topic argument that
    # ContextCompressor passes through to summarize_messages.
    def _spy_summarize_messages(client, model, messages, *, focus_topic=None,
                                max_tokens=1024, temperature=0.0):
        captured["focus_topic"] = focus_topic
        return "MERGED-SUMMARY"

    monkeypatch.setattr(
        "agent.auxiliary_client.summarize_messages", _spy_summarize_messages,
    )

    # File for @file: expansion.
    big = "X" * 5000  # not actually huge, but visibly distinct
    (tmp_path / "big.txt").write_text(big, encoding="utf-8")

    db = SessionDB(db_path=tmp_path / "state.db")
    agent = AIAgent(
        model="gpt-test", api_key="sk-x", base_url="https://x/v1",
        session_db=db,
    )

    # Fake auxiliary client_factory so the compressor is willing to
    # run summarisation (instead of falling back to pruning).
    comp = agent._get_compressor()
    assert comp is not None
    comp.client_factory = lambda: (object(), "aux-model")

    # Long history that survives the protect-first/last cutoffs and has
    # a non-empty middle window.
    history = (
        [{"role": "user" if i % 2 == 0 else "assistant",
          "content": "filler turn " + ("x" * 50)} for i in range(20)]
    )

    from tests.conftest import make_text_response, StubClient
    stub = StubClient([make_text_response("ok")])
    monkeypatch.setattr("run_agent.OpenAI", lambda *a, **kw: stub)

    user_in = "look at @file:big.txt and explain"
    try:
        agent.run_conversation(user_in, conversation_history=history)
    finally:
        agent.close()
        db.close()

    # focus_topic must be the user's original prose — not the expanded
    # text containing the full file.  The big "X"*5000 must NOT appear
    # in focus_topic.
    assert "focus_topic" in captured, "summarizer was not called"
    assert "@file:big.txt" in captured["focus_topic"]
    assert "X" * 100 not in (captured["focus_topic"] or ""), (
        "expanded file content leaked into focus_topic"
    )


def test_memory_query_uses_original_user_text(
    stub_openai, monkeypatch, tmp_path,
):
    """Memory retrieval at turn 0 receives the original user message
    (no <reference> blocks polluting the FTS query)."""
    monkeypatch.setenv("PHALANX_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    db = SessionDB(db_path=tmp_path / "state.db")
    # A memory whose retrieval depends on a token in the *original*
    # user text only.
    db.store_memory(
        "preference", "user wrote 'pytest-style' once",
        scope="global", pinned=True,
    )
    (tmp_path / "x.py").write_text("# unrelated", encoding="utf-8")

    captured = {}
    real_retrieve = db.retrieve_memories

    def _spy(query, **kw):
        captured["query"] = query
        return real_retrieve(query, **kw)

    monkeypatch.setattr(db, "retrieve_memories", _spy)

    stub_openai([make_text_response("ok")])
    agent = AIAgent(
        model="gpt-test", api_key="sk-x", base_url="https://x/v1",
        session_db=db,
    )
    try:
        agent.run_conversation("compare @file:x.py to last version")
    finally:
        agent.close()
        db.close()

    # The retrieval query must be the user's prose; the @file: token
    # itself is OK to be in there (memory FTS is forgiving), but the
    # resolved file content must NOT have leaked in.
    assert "compare" in captured["query"]
    assert "@file:x.py" in captured["query"]
    assert "<reference type" not in captured["query"]
