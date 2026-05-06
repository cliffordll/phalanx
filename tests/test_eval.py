"""Eval harness tests — Phase 2.8.a waves 1–3 coverage.

Waves 1–2 shipped data shapes / loader / runner skeleton / CLI
dispatch / 10 seed golden tasks.  Wave 3 fills in the three concrete
verifiers (``exact_match`` / ``tool_called`` / ``file_state``), wires
``cost_usd`` via ``agent.usage_pricing``, and surfaces structured
tool_calls on RunRecord — those paths are exercised below.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import eval as eval_mod
from hermes_cli.eval import (
    GoldenTask,
    RunRecord,
    Verdict,
    VerifierResult,
    _extract_tool_calls,
    format_report_json,
    format_report_text,
    load_golden_tasks,
    load_task,
    register_verifier,
    run_task,
    run_tasks,
)


# ── YAML loader ──────────────────────────────────────────────────────


def _write(dir: Path, name: str, body: str) -> Path:
    p = dir / name
    p.write_text(body, encoding="utf-8")
    return p


def test_load_golden_tasks_returns_empty_when_dir_missing(tmp_path):
    out = load_golden_tasks(tmp_path / "does_not_exist")
    assert out == []


def test_load_golden_tasks_parses_basic_yaml(tmp_path):
    _write(
        tmp_path,
        "explain.yaml",
        "task_id: explain\nprompt: ping\nverifier_type: exact_match\n"
        "expected: { contains: [pong] }\ncategory: smoke\n",
    )
    tasks = load_golden_tasks(tmp_path)
    assert len(tasks) == 1
    t = tasks[0]
    assert t.task_id == "explain"
    assert t.verifier_type == "exact_match"
    assert t.expected == {"contains": ["pong"]}
    assert t.category == "smoke"
    assert t.max_iterations == 30  # default


def test_load_golden_tasks_skips_underscore_prefix(tmp_path):
    _write(tmp_path, "real.yaml",
           "task_id: real\nprompt: x\nverifier_type: exact_match\n")
    _write(tmp_path, "_disabled.yaml",
           "task_id: disabled\nprompt: x\nverifier_type: exact_match\n")
    ids = {t.task_id for t in load_golden_tasks(tmp_path)}
    assert ids == {"real"}


def test_load_golden_tasks_rejects_missing_required_field(tmp_path):
    _write(tmp_path, "bad.yaml", "task_id: x\nprompt: y\n")  # no verifier_type
    with pytest.raises(ValueError, match="verifier_type"):
        load_golden_tasks(tmp_path)


def test_load_golden_tasks_rejects_invalid_yaml(tmp_path):
    _write(tmp_path, "bad.yaml", "[ unclosed list\n")
    with pytest.raises(ValueError, match="invalid YAML"):
        load_golden_tasks(tmp_path)


def test_load_golden_tasks_rejects_non_mapping_root(tmp_path):
    _write(tmp_path, "bad.yaml", "- a\n- b\n")
    with pytest.raises(ValueError, match="must be a mapping"):
        load_golden_tasks(tmp_path)


def test_load_golden_tasks_rejects_duplicate_task_id(tmp_path):
    _write(tmp_path, "a.yaml",
           "task_id: dup\nprompt: a\nverifier_type: exact_match\n")
    _write(tmp_path, "b.yaml",
           "task_id: dup\nprompt: b\nverifier_type: exact_match\n")
    with pytest.raises(ValueError, match="duplicate task_id"):
        load_golden_tasks(tmp_path)


def test_load_golden_tasks_accepts_verifier_alias(tmp_path):
    _write(tmp_path, "a.yaml",
           "task_id: a\nprompt: x\nverifier: exact_match\n")
    [t] = load_golden_tasks(tmp_path)
    assert t.verifier_type == "exact_match"


def test_load_task_finds_by_id(tmp_path):
    _write(tmp_path, "a.yaml",
           "task_id: a\nprompt: x\nverifier_type: exact_match\n")
    _write(tmp_path, "b.yaml",
           "task_id: b\nprompt: y\nverifier_type: exact_match\n")
    assert load_task("b", tmp_path).prompt == "y"


def test_load_task_raises_on_missing(tmp_path):
    _write(tmp_path, "a.yaml",
           "task_id: a\nprompt: x\nverifier_type: exact_match\n")
    with pytest.raises(KeyError):
        load_task("missing", tmp_path)


# ── Repo's seeded golden task ────────────────────────────────────────


def test_repo_default_dir_loads():
    """tests/golden/ ships the wave-2 seed set; check it parses end to end
    and covers the categories the eval breakdown promised."""
    tasks = load_golden_tasks()
    assert len(tasks) >= 10, "wave 2 seeded 10 tasks; loader should return all of them"

    ids = {t.task_id for t in tasks}
    # Anchor a few representative ids per category so renames flag in CI.
    assert "smoke_oneshot" in ids
    assert "file_read_pyproject" in ids
    assert "web_search_official" in ids
    assert "plan_explain_closure" in ids
    assert "multi_search_then_read" in ids

    categories = {t.category for t in tasks}
    assert {"smoke", "file", "web", "plan", "multi-tool"} <= categories

    # Verifier_type values must all be in the (currently planned) wave-3 set.
    expected_verifiers = {"exact_match", "tool_called", "file_state"}
    actual_verifiers = {t.verifier_type for t in tasks}
    assert actual_verifiers <= expected_verifiers, (
        f"unexpected verifier types: {actual_verifiers - expected_verifiers}"
    )


# ── Runner skeleton ──────────────────────────────────────────────────


class _FakeAgent:
    """Minimal AIAgent stand-in for runner tests — shapes a fake response."""

    def __init__(self, *, raise_on_run: bool = False, return_value=None):
        self.raise_on_run = raise_on_run
        self.return_value = return_value
        self.session_id = "sess_fake_123"

    def run_conversation(self, prompt: str):
        if self.raise_on_run:
            raise RuntimeError("boom")
        if self.return_value is not None:
            return self.return_value
        return {
            "final_response": f"echoed: {prompt}",
            "messages": [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": f"echoed: {prompt}"},
            ],
            "api_calls": 1,
            "iterations_used": 1,
            "stop_reason": "completed",
        }


def _task(task_id="t", verifier="pending"):
    return GoldenTask(task_id=task_id, prompt="ping", verifier_type=verifier)


def test_run_task_skip_when_no_verifier_registered():
    rec = run_task(lambda task: _FakeAgent(), _task("t1"))
    assert rec.verdict == Verdict.SKIP
    assert "not implemented" in rec.reason
    assert rec.task_id == "t1"
    assert rec.turns == 1
    assert rec.stop_reason == "completed"
    assert rec.session_id == "sess_fake_123"
    assert rec.trajectory_summary[0].startswith("[system")


def test_run_task_records_factory_failure():
    def boom(task):
        raise RuntimeError("can't build")
    rec = run_task(boom, _task("t2"))
    assert rec.verdict == Verdict.ERROR
    assert "agent_factory failed" in rec.error
    assert "can't build" in rec.error


def test_run_task_records_run_conversation_failure():
    def factory(task):
        return _FakeAgent(raise_on_run=True)
    rec = run_task(factory, _task("t3"))
    assert rec.verdict == Verdict.ERROR
    assert "run_conversation failed" in rec.error
    assert "boom" in rec.error


def test_run_task_records_unexpected_return_type():
    def factory(task):
        return _FakeAgent(return_value="not a dict")
    rec = run_task(factory, _task("t4"))
    assert rec.verdict == Verdict.ERROR
    assert "expected dict" in rec.error


def test_run_task_dispatches_to_registered_verifier():
    @register_verifier("test_pass_through")
    def _v(task: GoldenTask, record: RunRecord) -> VerifierResult:
        return VerifierResult(Verdict.PASS, "ok")
    try:
        rec = run_task(lambda task: _FakeAgent(), _task("t5", "test_pass_through"))
        assert rec.verdict == Verdict.PASS
        assert rec.reason == "ok"
    finally:
        eval_mod.VERIFIERS.pop("test_pass_through", None)


def test_register_verifier_rejects_duplicate():
    @register_verifier("test_unique_v")
    def _v(task, record):
        return VerifierResult(Verdict.PASS)
    try:
        with pytest.raises(ValueError, match="already registered"):
            @register_verifier("test_unique_v")
            def _v2(task, record):
                return VerifierResult(Verdict.PASS)
    finally:
        eval_mod.VERIFIERS.pop("test_unique_v", None)


def test_run_tasks_collects_records_in_order():
    tasks = [_task(f"t{i}") for i in range(3)]
    records = run_tasks(lambda task: _FakeAgent(), tasks)
    assert [r.task_id for r in records] == ["t0", "t1", "t2"]
    assert all(r.verdict == Verdict.SKIP for r in records)


# ── Report renderers ────────────────────────────────────────────────


def _record(task_id, verdict, **kw):
    return RunRecord(task_id=task_id, verdict=verdict, **kw)


def test_format_report_text_empty():
    assert format_report_text([]) == "(no tasks ran)"


def test_format_report_text_aggregates_counts():
    out = format_report_text([
        _record("a", Verdict.PASS, turns=2),
        _record("b", Verdict.FAIL, turns=4, reason="missing keyword"),
        _record("c", Verdict.SKIP),
    ])
    assert "a" in out and "b" in out and "c" in out
    assert "1 PASS" in out
    assert "1 FAIL" in out
    assert "1 SKIP" in out
    assert "missing keyword" in out


def test_format_report_json_matches_summary_shape():
    records = [
        _record("a", Verdict.PASS, turns=2, input_tokens=10, output_tokens=20),
        _record("b", Verdict.FAIL, turns=3),
    ]
    parsed = json.loads(format_report_json(records))
    assert parsed["summary"]["task_count"] == 2
    assert parsed["summary"]["verdicts"]["PASS"] == 1
    assert parsed["summary"]["verdicts"]["FAIL"] == 1
    assert parsed["summary"]["pass_rate"] == 0.5
    assert parsed["summary"]["total_input_tokens"] == 10
    assert parsed["summary"]["total_output_tokens"] == 20
    assert len(parsed["records"]) == 2
    assert parsed["records"][0]["verdict"] == "PASS"


# ── CLI dispatch (eval list) ─────────────────────────────────────────


def test_cmd_eval_list_runs(capsys):
    """`hermes eval list` should not crash and should print the wave-2 set."""
    from hermes_cli.main import cmd_eval_list

    rc = cmd_eval_list(SimpleNamespace(dir=None))
    assert rc == 0
    out = capsys.readouterr().out
    assert "smoke_oneshot" in out
    assert "tasks" in out


def test_cmd_eval_list_handles_bad_dir(tmp_path, capsys):
    from hermes_cli.main import cmd_eval_list

    # Use a malformed YAML to trigger ValueError → return 1
    (tmp_path / "broken.yaml").write_text("[ unclosed", encoding="utf-8")
    rc = cmd_eval_list(SimpleNamespace(dir=str(tmp_path)))
    assert rc == 1
    err = capsys.readouterr().err
    assert "invalid YAML" in err


# ── Wave 3: tool_calls extraction ────────────────────────────────────


def test_extract_tool_calls_parses_json_arguments():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path": "pyproject.toml"}',
                    },
                },
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "..."},
    ]
    calls = _extract_tool_calls(messages)
    assert len(calls) == 1
    assert calls[0]["name"] == "read_file"
    assert calls[0]["arguments"] == {"path": "pyproject.toml"}
    assert calls[0]["id"] == "call_1"


def test_extract_tool_calls_keeps_raw_when_arguments_unparseable():
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_2",
                    "function": {"name": "x", "arguments": "not-json"},
                },
            ],
        },
    ]
    [call] = _extract_tool_calls(messages)
    assert call["name"] == "x"
    assert call["arguments"] is None
    assert call["arguments_raw"] == "not-json"


def test_extract_tool_calls_collects_calls_across_turns():
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {"function": {"name": "a", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "content": "..."},
        {
            "role": "assistant",
            "tool_calls": [
                {"function": {"name": "b", "arguments": '{"k": 1}'}},
                {"function": {"name": "c", "arguments": "{}"}},
            ],
        },
    ]
    names = [c["name"] for c in _extract_tool_calls(messages)]
    assert names == ["a", "b", "c"]


def test_extract_tool_calls_ignores_non_assistant_roles():
    # A user / tool message with stray ``tool_calls`` shouldn't leak into
    # the verifier surface.
    messages = [
        {"role": "user", "tool_calls": [{"function": {"name": "x", "arguments": "{}"}}]},
        {"role": "tool", "tool_calls": [{"function": {"name": "y", "arguments": "{}"}}]},
    ]
    assert _extract_tool_calls(messages) == []


# ── Wave 3: exact_match verifier ─────────────────────────────────────


def _record_with(final_response: str, **kw) -> RunRecord:
    return RunRecord(
        task_id=kw.pop("task_id", "t"),
        verdict=kw.pop("verdict", Verdict.SKIP),
        final_response=final_response,
        **kw,
    )


def test_exact_match_passes_when_all_substrings_present():
    task = GoldenTask(
        task_id="t", prompt="x", verifier_type="exact_match",
        expected={"contains": ["pong-phalanx"]},
    )
    rec = _record_with("the answer is pong-phalanx, end.")
    out = eval_mod.VERIFIERS["exact_match"](task, rec)
    assert out.verdict == Verdict.PASS


def test_exact_match_supports_list_and_and_semantics():
    task = GoldenTask(
        task_id="t", prompt="x", verifier_type="exact_match",
        expected={"contains": ["alpha", "beta"]},
    )
    # both present → PASS
    out = eval_mod.VERIFIERS["exact_match"](task, _record_with("alpha and beta"))
    assert out.verdict == Verdict.PASS
    # only one present → FAIL with the missing one in reason
    out = eval_mod.VERIFIERS["exact_match"](task, _record_with("alpha only"))
    assert out.verdict == Verdict.FAIL
    assert "beta" in out.reason


def test_exact_match_case_insensitive_flag():
    task = GoldenTask(
        task_id="t", prompt="x", verifier_type="exact_match",
        expected={"contains": ["Closure", "Function"], "case_insensitive": True},
    )
    out = eval_mod.VERIFIERS["exact_match"](task, _record_with("a closure is a function"))
    assert out.verdict == Verdict.PASS


def test_exact_match_errors_when_contains_missing():
    task = GoldenTask(
        task_id="t", prompt="x", verifier_type="exact_match",
        expected={},
    )
    out = eval_mod.VERIFIERS["exact_match"](task, _record_with("any"))
    assert out.verdict == Verdict.ERROR
    assert "contains" in out.reason


def test_exact_match_accepts_string_for_contains():
    task = GoldenTask(
        task_id="t", prompt="x", verifier_type="exact_match",
        expected={"contains": "single"},
    )
    out = eval_mod.VERIFIERS["exact_match"](task, _record_with("a single thing"))
    assert out.verdict == Verdict.PASS


# ── Wave 3: tool_called verifier ─────────────────────────────────────


def _record_with_calls(calls):
    return RunRecord(task_id="t", verdict=Verdict.SKIP, tool_calls=list(calls))


def test_tool_called_passes_when_tool_invoked():
    task = GoldenTask(
        task_id="t", prompt="x", verifier_type="tool_called",
        expected={"tool": "read_file"},
    )
    rec = _record_with_calls([{"name": "read_file", "arguments": {"path": "x"}}])
    assert eval_mod.VERIFIERS["tool_called"](task, rec).verdict == Verdict.PASS


def test_tool_called_fails_when_tool_never_invoked():
    task = GoldenTask(
        task_id="t", prompt="x", verifier_type="tool_called",
        expected={"tool": "search_files"},
    )
    rec = _record_with_calls([{"name": "read_file", "arguments": {}}])
    out = eval_mod.VERIFIERS["tool_called"](task, rec)
    assert out.verdict == Verdict.FAIL
    assert "search_files" in out.reason
    assert "read_file" in out.reason  # surfaces what *was* called


def test_tool_called_args_subset_passes_on_match():
    task = GoldenTask(
        task_id="t", prompt="x", verifier_type="tool_called",
        expected={"tool": "read_file", "args_subset": {"path": "pyproject.toml"}},
    )
    rec = _record_with_calls([
        {"name": "read_file", "arguments": {"path": "other.txt"}},
        {"name": "read_file", "arguments": {"path": "pyproject.toml", "encoding": "utf-8"}},
    ])
    out = eval_mod.VERIFIERS["tool_called"](task, rec)
    assert out.verdict == Verdict.PASS


def test_tool_called_args_subset_fails_on_mismatch():
    task = GoldenTask(
        task_id="t", prompt="x", verifier_type="tool_called",
        expected={"tool": "read_file", "args_subset": {"path": "pyproject.toml"}},
    )
    rec = _record_with_calls([{"name": "read_file", "arguments": {"path": "wrong.toml"}}])
    out = eval_mod.VERIFIERS["tool_called"](task, rec)
    assert out.verdict == Verdict.FAIL
    assert "wrong.toml" in out.reason or "args[" in out.reason


def test_tool_called_errors_on_missing_tool_field():
    task = GoldenTask(
        task_id="t", prompt="x", verifier_type="tool_called",
        expected={},
    )
    out = eval_mod.VERIFIERS["tool_called"](task, _record_with_calls([]))
    assert out.verdict == Verdict.ERROR


def test_tool_called_handles_none_arguments():
    # When the agent invokes a tool with malformed JSON args, our extractor
    # records ``arguments=None``; verifier with no args_subset still passes.
    task = GoldenTask(
        task_id="t", prompt="x", verifier_type="tool_called",
        expected={"tool": "echo"},
    )
    rec = _record_with_calls([{"name": "echo", "arguments": None}])
    out = eval_mod.VERIFIERS["tool_called"](task, rec)
    assert out.verdict == Verdict.PASS


# ── Wave 3: file_state verifier ──────────────────────────────────────


def test_file_state_passes_when_file_exists_with_substring(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "out.txt").write_text("phalanx-eval-marker\n", encoding="utf-8")
    task = GoldenTask(
        task_id="t", prompt="x", verifier_type="file_state",
        expected={"path": "out.txt", "contains": "phalanx-eval-marker"},
    )
    out = eval_mod.VERIFIERS["file_state"](task, RunRecord(task_id="t", verdict=Verdict.SKIP))
    assert out.verdict == Verdict.PASS


def test_file_state_fails_when_substring_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "out.txt").write_text("nope\n", encoding="utf-8")
    task = GoldenTask(
        task_id="t", prompt="x", verifier_type="file_state",
        expected={"path": "out.txt", "contains": "phalanx-eval-marker"},
    )
    out = eval_mod.VERIFIERS["file_state"](task, RunRecord(task_id="t", verdict=Verdict.SKIP))
    assert out.verdict == Verdict.FAIL


def test_file_state_fails_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    task = GoldenTask(
        task_id="t", prompt="x", verifier_type="file_state",
        expected={"path": "missing.txt"},
    )
    out = eval_mod.VERIFIERS["file_state"](task, RunRecord(task_id="t", verdict=Verdict.SKIP))
    assert out.verdict == Verdict.FAIL
    assert "missing.txt" in out.reason


def test_file_state_supports_must_not_exist(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # File absent + exists=false → PASS
    task = GoldenTask(
        task_id="t", prompt="x", verifier_type="file_state",
        expected={"path": "should_not_be_there.txt", "exists": False},
    )
    out = eval_mod.VERIFIERS["file_state"](task, RunRecord(task_id="t", verdict=Verdict.SKIP))
    assert out.verdict == Verdict.PASS

    # File present + exists=false → FAIL
    (tmp_path / "should_not_be_there.txt").write_text("oops", encoding="utf-8")
    out = eval_mod.VERIFIERS["file_state"](task, RunRecord(task_id="t", verdict=Verdict.SKIP))
    assert out.verdict == Verdict.FAIL


def test_file_state_errors_on_missing_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    task = GoldenTask(
        task_id="t", prompt="x", verifier_type="file_state",
        expected={},
    )
    out = eval_mod.VERIFIERS["file_state"](task, RunRecord(task_id="t", verdict=Verdict.SKIP))
    assert out.verdict == Verdict.ERROR


def test_file_state_resolves_absolute_path(tmp_path):
    target = tmp_path / "abs.txt"
    target.write_text("absolute-marker", encoding="utf-8")
    task = GoldenTask(
        task_id="t", prompt="x", verifier_type="file_state",
        expected={"path": str(target), "contains": "absolute-marker"},
    )
    out = eval_mod.VERIFIERS["file_state"](task, RunRecord(task_id="t", verdict=Verdict.SKIP))
    assert out.verdict == Verdict.PASS


# ── Wave 3: runner end-to-end with real verifiers ────────────────────


class _UsageStubAgent:
    """FakeAgent variant that returns ``usage_totals`` like the real loop."""

    session_id = "sess_usage_1"
    model = "gpt-4o-mini"
    provider = "openai"
    base_url = "https://api.openai.com/v1"

    def __init__(self, *, final_response="hello pong-phalanx", tool_calls=None):
        self._final = final_response
        self._tool_calls = tool_calls or []

    def run_conversation(self, prompt: str):
        messages = [
            {"role": "user", "content": prompt},
            {
                "role": "assistant",
                "content": self._final,
                "tool_calls": self._tool_calls,
            },
        ]
        return {
            "final_response": self._final,
            "messages": messages,
            "api_calls": 1,
            "iterations_used": 1,
            "stop_reason": "completed",
            "usage_totals": {
                "input_tokens": 100,
                "output_tokens": 25,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "reasoning_tokens": 0,
            },
        }


def test_run_task_wave3_exact_match_pass_with_usage():
    """End-to-end: run_task should populate token totals from usage_totals
    and dispatch through the registered exact_match verifier."""
    task = GoldenTask(
        task_id="smoke", prompt="hi", verifier_type="exact_match",
        expected={"contains": ["pong-phalanx"]},
    )
    rec = run_task(lambda t: _UsageStubAgent(), task)
    assert rec.verdict == Verdict.PASS
    assert rec.input_tokens == 100
    assert rec.output_tokens == 25
    # cost_status comes from estimate_usage_cost — for a known model it'll
    # land as "estimated"; fallback "unknown" is also acceptable in case
    # the pricing snapshot doesn't include this model id.
    assert rec.cost_status in ("estimated", "actual", "included", "unknown")


def test_run_task_wave3_tool_called_pass():
    task = GoldenTask(
        task_id="reads", prompt="read it", verifier_type="tool_called",
        expected={"tool": "read_file", "args_subset": {"path": "pyproject.toml"}},
    )
    agent = _UsageStubAgent(
        final_response="done",
        tool_calls=[{
            "id": "1",
            "type": "function",
            "function": {"name": "read_file", "arguments": '{"path": "pyproject.toml"}'},
        }],
    )
    rec = run_task(lambda t: agent, task)
    assert rec.verdict == Verdict.PASS
    assert rec.tool_calls[0]["name"] == "read_file"
    assert rec.tool_calls[0]["arguments"] == {"path": "pyproject.toml"}


def test_run_task_wave3_file_state_pass(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    task = GoldenTask(
        task_id="fs", prompt="create it", verifier_type="file_state",
        expected={"path": "made.txt", "contains": "marker"},
    )
    # The "agent" doesn't actually do anything — the test simulates the
    # tool side-effect by writing the file beforehand.  This isolates the
    # verifier from the loop without spinning up real tools.
    (tmp_path / "made.txt").write_text("the marker is here", encoding="utf-8")
    rec = run_task(lambda t: _UsageStubAgent(final_response="ok"), task)
    assert rec.verdict == Verdict.PASS


# ── Wave 3: report renderer surface ──────────────────────────────────


def test_format_report_text_includes_category_breakdown():
    records = [
        _record("a", Verdict.PASS),
        _record("b", Verdict.FAIL),
        _record("c", Verdict.PASS),
    ]
    tasks = [
        GoldenTask(task_id="a", prompt="x", verifier_type="exact_match", category="file"),
        GoldenTask(task_id="b", prompt="x", verifier_type="exact_match", category="web"),
        GoldenTask(task_id="c", prompt="x", verifier_type="exact_match", category="file"),
    ]
    out = format_report_text(records, tasks=tasks)
    # Per-category footer lines surface the bucket counts.
    assert "file" in out
    assert "web" in out
    assert "2/2 pass" in out  # file: both pass
    assert "0/1 pass" in out  # web: zero pass


def test_format_report_json_includes_category_breakdown():
    records = [_record("a", Verdict.PASS), _record("b", Verdict.FAIL)]
    tasks = [
        GoldenTask(task_id="a", prompt="x", verifier_type="exact_match", category="file"),
        GoldenTask(task_id="b", prompt="x", verifier_type="exact_match", category="web"),
    ]
    parsed = json.loads(format_report_json(records, tasks=tasks))
    cb = parsed["summary"]["category_breakdown"]
    assert cb["file"]["PASS"] == 1
    assert cb["web"]["FAIL"] == 1


def test_format_report_text_flags_unknown_cost():
    records = [
        _record("a", Verdict.PASS, cost_status="unknown"),
        _record("b", Verdict.PASS, cost_status="estimated", cost_usd=0.0001),
    ]
    out = format_report_text(records)
    # The aggregate footer flags "(some unknown)" when at least one row is
    # priced as unknown — keeps the demo number from looking authoritative.
    assert "(some unknown)" in out
    # Per-row marker "?" appears next to unknown-cost rows.
    assert "?" in out
