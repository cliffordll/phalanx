"""Eval harness tests — Phase 2.8.a wave 1 skeleton coverage.

Wave 1 ships data shapes / loader / runner skeleton / CLI dispatch.
Verifiers are absent (everything resolves to SKIP) — wave 3 fills them
in and adds the real-model golden-task tests.
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
