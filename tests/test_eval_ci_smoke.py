"""CI smoke test for the eval harness — Phase 2.8.a wave 4.

The real eval (``hermes eval``) calls an LLM, costs money, and is
flaky on the network — so it can't run in CI.  This file pins the
*structural* parts: loader → runner → verifier → report → save_run
chain works end-to-end against a stub agent, and the three verifier
types (exact_match / tool_called / file_state) each receive realistic
inputs and produce the expected verdict.

If anything in this file goes red on CI, the eval harness's wiring
broke independently of any model behavior — that's exactly what we
want to catch before it reaches a real run.
"""

from __future__ import annotations

import json
from pathlib import Path

from hermes_cli.eval import (
    Verdict,
    format_report_text,
    load_golden_tasks,
    load_run,
    run_task,
    save_run,
)


# ── Stub agent — same shape as run_conversation but no network ───────


class _StubAgent:
    """Predefined-response stand-in for AIAgent.

    Constructor takes ``final_response``, ``tool_calls`` (list of
    ``{name, arguments}`` dicts), and an optional ``side_effect``
    callable that the runner invokes during ``run_conversation`` —
    used by the file_state path to materialise the artifact the
    verifier checks for.
    """

    session_id = "stub-ci-smoke"
    model = "gpt-4o-mini"
    provider = "openai"
    base_url = "https://api.openai.com/v1"

    def __init__(self, *, final_response="", tool_calls=None, side_effect=None):
        self._final = final_response
        self._tool_calls = tool_calls or []
        self._side_effect = side_effect

    def run_conversation(self, prompt: str):
        if self._side_effect is not None:
            self._side_effect()
        # Build a messages list that matches the OpenAI chat.completions
        # tool-calls shape (function.arguments serialised as JSON string)
        # so _extract_tool_calls exercises its real parsing path.
        assistant_msg = {"role": "assistant", "content": self._final}
        if self._tool_calls:
            assistant_msg["tool_calls"] = [
                {
                    "id": f"call_{i}",
                    "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": json.dumps(call.get("arguments") or {}),
                    },
                }
                for i, call in enumerate(self._tool_calls)
            ]
        return {
            "final_response": self._final,
            "messages": [
                {"role": "user", "content": prompt},
                assistant_msg,
            ],
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


# ── End-to-end CI smoke ──────────────────────────────────────────────


def test_ci_stub_three_verifiers_full_chain(tmp_path, monkeypatch):
    """One pytest run that exercises:
      * loader (real wave-2 YAML on disk)
      * runner (run_task → run_conversation → tool_calls extraction)
      * each of the three wave-3 verifiers
      * report renderer (text)
      * save_run / load_run round-trip (wave 4)

    Three tasks are picked from the wave-2 seed set so this test stays
    in sync with shipped definitions:

      * smoke_oneshot          (exact_match)
      * file_read_pyproject    (tool_called + args_subset)
      * file_patch_create_file (file_state, side_effect writes the file)
    """
    monkeypatch.chdir(tmp_path)

    # Repo's actual golden dir, so the loader path runs against the
    # production schema.
    repo_root = Path(__file__).resolve().parent.parent
    tasks = {t.task_id: t for t in load_golden_tasks(repo_root / "tests" / "golden")}

    # 1. exact_match  — agent emits the literal sentinel.
    smoke = tasks["smoke_oneshot"]
    rec_smoke = run_task(
        lambda task: _StubAgent(final_response="here it is: pong-phalanx, end."),
        smoke,
    )
    assert rec_smoke.verdict == Verdict.PASS, rec_smoke.reason
    assert rec_smoke.input_tokens == 100
    assert rec_smoke.output_tokens == 25

    # 2. tool_called  — agent calls read_file with the matching path.
    read = tasks["file_read_pyproject"]
    rec_read = run_task(
        lambda task: _StubAgent(
            final_response="the project name is phalanx",
            tool_calls=[{"name": "read_file", "arguments": {"path": "pyproject.toml"}}],
        ),
        read,
    )
    assert rec_read.verdict == Verdict.PASS, rec_read.reason
    assert rec_read.tool_calls[0]["name"] == "read_file"

    # 3. file_state  — side_effect materialises the artifact the
    #    verifier expects to find under tmp_path (CWD).
    artifact = tmp_path / "eval_artifact.txt"
    patch = tasks["file_patch_create_file"]
    rec_patch = run_task(
        lambda task: _StubAgent(
            final_response="done",
            side_effect=lambda: artifact.write_text(
                "phalanx-eval-marker\n", encoding="utf-8"
            ),
        ),
        patch,
    )
    assert rec_patch.verdict == Verdict.PASS, rec_patch.reason
    assert artifact.exists()

    # Aggregate — text report renders without crashing and shows all 3.
    records = [rec_smoke, rec_read, rec_patch]
    txt = format_report_text(records, tasks=[smoke, read, patch])
    assert "smoke_oneshot" in txt
    assert "file_read_pyproject" in txt
    assert "file_patch_create_file" in txt
    assert "3 PASS" in txt

    # save_run / load_run round-trip into an isolated root so the test
    # never touches the real ~/.phalanx/eval/.
    run_root = tmp_path / "_eval_runs"
    saved = save_run(records, tasks=[smoke, read, patch], root=run_root, run_id="ci-test")
    assert saved.exists()
    assert (saved / "records.json").exists()
    assert (saved / "summary.json").exists()
    assert (saved / "tasks.json").exists()

    loaded = load_run("ci-test", root=run_root)
    assert len(loaded["records"]) == 3
    ids_loaded = {r["task_id"] for r in loaded["records"]}
    assert ids_loaded == {"smoke_oneshot", "file_read_pyproject", "file_patch_create_file"}
    assert loaded["summary"]["verdicts"]["PASS"] == 3


def test_ci_stub_picks_up_regressions_in_runner_to_verifier_path():
    """Counter-test: a stub that emits the *wrong* response should FAIL,
    proving the chain isn't silently passing everything.  Without this
    check the previous test could go green by accident if the verifier
    became permissive."""
    repo_root = Path(__file__).resolve().parent.parent
    tasks = {t.task_id: t for t in load_golden_tasks(repo_root / "tests" / "golden")}
    smoke = tasks["smoke_oneshot"]

    rec = run_task(
        # Missing the sentinel "pong-phalanx" — exact_match must FAIL.
        lambda task: _StubAgent(final_response="some other answer"),
        smoke,
    )
    assert rec.verdict == Verdict.FAIL
    assert "pong-phalanx" in rec.reason or "missing" in rec.reason
