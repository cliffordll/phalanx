"""
Phalanx evaluation harness — Phase 2.8.a wave 1 skeleton.

Loads "golden tasks" from YAML files, runs each one through ``AIAgent.
run_conversation``, captures a ``RunRecord`` (verdict + tokens + cost +
turns), and renders a comparable report.  Wave 1 ships the data shapes,
loader, runner skeleton, and report renderer; the three concrete
verifier types (``exact_match`` / ``tool_called`` / ``file_state``)
arrive in wave 3, the seed task set in wave 2.

Design rationale: see [`docs/agent-self-evolution.md`](../docs/agent-self-evolution.md)
§2.6 and [`docs/MIGRATION_PLAN.md`](../docs/MIGRATION_PLAN.md) §2.8.a.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

import yaml

logger = logging.getLogger(__name__)


# ── Verdict / RunRecord / GoldenTask ─────────────────────────────────


class Verdict(str, Enum):
    """Per-task evaluation outcome.

    Mirrors pytest's PASS/FAIL pattern with ERROR for harness failures
    (loader exception, agent crash) and SKIP for tasks whose verifier
    type isn't registered yet — wave 1 returns SKIP for everything
    because the verifier registry is empty.
    """

    PASS = "PASS"
    FAIL = "FAIL"
    ERROR = "ERROR"
    SKIP = "SKIP"


@dataclass
class GoldenTask:
    """A frozen golden task loaded from ``tests/golden/<id>.yaml``.

    The YAML schema matches this dataclass field-for-field, with one
    aliasing: top-level ``verifier`` (single-word) is also accepted as
    ``verifier_type`` for forward-compat with future verifier-config
    blocks.
    """

    task_id: str
    prompt: str
    verifier_type: str
    expected: Dict[str, Any] = field(default_factory=dict)
    category: str = "uncategorised"
    system: Optional[str] = None
    max_iterations: int = 30
    # Optional per-task model override; falls back to the agent's
    # configured default when None.
    model: Optional[str] = None
    description: str = ""


@dataclass
class RunRecord:
    """One task × one agent run.

    Captures everything needed for the eval report and `--diff` against
    a stored baseline.  ``trajectory_summary`` is intentionally a
    summary (role + tool name only) rather than full message content —
    full trajectory lives in SessionDB and is too noisy for diffing
    runs across model versions.
    """

    task_id: str
    verdict: Verdict
    reason: str = ""
    turns: int = 0
    api_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    duration_seconds: float = 0.0
    stop_reason: str = ""
    final_response: str = ""
    trajectory_summary: List[str] = field(default_factory=list)
    error: Optional[str] = None
    session_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["verdict"] = self.verdict.value
        return d


# ── Verifier registry ────────────────────────────────────────────────

# Wave 1 ships an empty registry so any task whose ``verifier_type``
# isn't recognised falls back to SKIP (rather than crashing).  Wave 3
# fills in ``exact_match`` / ``tool_called`` / ``file_state``.
VerifierFn = Callable[[GoldenTask, RunRecord], "VerifierResult"]


@dataclass
class VerifierResult:
    """Output of a verifier — verdict + a short human-readable reason."""

    verdict: Verdict
    reason: str = ""


VERIFIERS: Dict[str, VerifierFn] = {}


def register_verifier(name: str) -> Callable[[VerifierFn], VerifierFn]:
    """Decorator to register a verifier under ``name``.

    Wave 3 adds the three concrete types via this decorator.  Keeping
    registration explicit (vs auto-discovery) mirrors phalanx's
    static-import pattern in ``tools/__init__.py``.
    """

    def _wrap(fn: VerifierFn) -> VerifierFn:
        if name in VERIFIERS:
            raise ValueError(f"verifier {name!r} already registered")
        VERIFIERS[name] = fn
        return fn

    return _wrap


def _run_verifier(task: GoldenTask, record: RunRecord) -> VerifierResult:
    """Dispatch to the registered verifier or SKIP if unknown."""
    fn = VERIFIERS.get(task.verifier_type)
    if fn is None:
        return VerifierResult(
            Verdict.SKIP,
            f"verifier_type {task.verifier_type!r} not implemented yet",
        )
    try:
        return fn(task, record)
    except Exception as exc:  # pragma: no cover — safety net
        logger.exception("verifier %s crashed", task.verifier_type)
        return VerifierResult(Verdict.ERROR, f"verifier crashed: {exc}")


# ── YAML loader ──────────────────────────────────────────────────────


GOLDEN_DIR = Path(__file__).resolve().parent.parent / "tests" / "golden"

REQUIRED_FIELDS = ("task_id", "prompt")
# Either `verifier_type` (canonical) or its alias `verifier` must be present.
VERIFIER_KEYS = ("verifier_type", "verifier")


def load_golden_tasks(directory: Path | str | None = None) -> List[GoldenTask]:
    """Load every ``*.yaml`` under ``directory`` (default: tests/golden/).

    Each YAML must be a single mapping with at least ``task_id``,
    ``prompt``, ``verifier_type``.  Files starting with ``_`` are
    skipped (convention for fixtures / disabled tasks).  Malformed
    files surface as ``ValueError`` — caller decides whether to skip
    or abort the run.
    """
    root = Path(directory) if directory is not None else GOLDEN_DIR
    if not root.exists():
        return []
    tasks: List[GoldenTask] = []
    seen_ids: set[str] = set()
    for path in sorted(root.glob("*.yaml")):
        if path.name.startswith("_"):
            continue
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ValueError(f"{path.name}: invalid YAML: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError(
                f"{path.name}: top-level must be a mapping, got {type(raw).__name__}"
            )
        for key in REQUIRED_FIELDS:
            if key not in raw:
                raise ValueError(f"{path.name}: missing required field {key!r}")
        # Either ``verifier_type`` (canonical) or ``verifier`` (alias).
        verifier_type = raw.get("verifier_type") or raw.get("verifier")
        if not verifier_type:
            raise ValueError(
                f"{path.name}: missing required field 'verifier_type'"
            )
        task = GoldenTask(
            task_id=str(raw["task_id"]),
            prompt=str(raw["prompt"]),
            verifier_type=str(verifier_type),
            expected=dict(raw.get("expected") or {}),
            category=str(raw.get("category", "uncategorised")),
            system=raw.get("system"),
            max_iterations=int(raw.get("max_iterations", 30)),
            model=raw.get("model"),
            description=str(raw.get("description", "")),
        )
        if task.task_id in seen_ids:
            raise ValueError(
                f"{path.name}: duplicate task_id {task.task_id!r}"
            )
        seen_ids.add(task.task_id)
        tasks.append(task)
    return tasks


def load_task(task_id: str, directory: Path | str | None = None) -> GoldenTask:
    """Lookup a single task by id.  Raises ``KeyError`` when missing."""
    for t in load_golden_tasks(directory):
        if t.task_id == task_id:
            return t
    raise KeyError(f"unknown task: {task_id}")


# ── Runner ──────────────────────────────────────────────────────────


# Caller injects an agent factory to keep eval.py free of run_agent
# imports at module load time.  Tests / wave-3 stub the factory.
AgentFactory = Callable[[GoldenTask], Any]


def _summarise_messages(messages: Sequence[Any]) -> List[str]:
    """Compact ``[role:tool?] preview`` summary for diff-friendly display.

    Avoids dumping full content into RunRecord — full trajectory lives
    in SessionDB.  Each line is one message: role, optional tool name,
    first 80 chars of content (newlines stripped).
    """
    out: List[str] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "?")
        tool = msg.get("tool_name") or msg.get("name") or ""
        content = msg.get("content") or ""
        if isinstance(content, list):
            content = f"<{len(content)} content parts>"
        snippet = str(content).replace("\n", " ")[:80]
        prefix = f"{role}:{tool}" if tool else role
        out.append(f"[{prefix}] {snippet}")
    return out


def _extract_token_totals(messages: Sequence[Any]) -> tuple[int, int]:
    """Best-effort token total reconstruction from message metadata.

    Wave 1 returns (0, 0) when usage isn't attached — actual aggregation
    moves to wave 3 once the SessionDB query is wired in.  Keeping the
    signature stable now means the report renderer doesn't change later.
    """
    input_tokens = 0
    output_tokens = 0
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        usage = msg.get("usage") or {}
        if isinstance(usage, dict):
            input_tokens += int(usage.get("prompt_tokens") or 0)
            output_tokens += int(usage.get("completion_tokens") or 0)
    return input_tokens, output_tokens


def run_task(
    agent_factory: AgentFactory,
    task: GoldenTask,
) -> RunRecord:
    """Execute a single golden task end-to-end.

    Builds an agent via the factory, calls ``run_conversation`` once
    with the task's prompt, captures structured fields into a
    ``RunRecord``, then dispatches to the registered verifier.  Any
    exception below the harness level becomes an ERROR record (the
    eval doesn't crash because one task crashed).
    """
    started = time.time()
    record = RunRecord(task_id=task.task_id, verdict=Verdict.ERROR)
    try:
        agent = agent_factory(task)
    except Exception as exc:
        record.error = f"agent_factory failed: {exc}"
        record.duration_seconds = time.time() - started
        record.reason = record.error
        logger.exception("agent_factory failed for %s", task.task_id)
        return record

    try:
        result = agent.run_conversation(task.prompt)
    except Exception as exc:
        record.error = f"run_conversation failed: {exc}"
        record.duration_seconds = time.time() - started
        record.reason = record.error
        logger.exception("run_conversation failed for %s", task.task_id)
        return record
    finally:
        # Best-effort: agents that opened a SessionDB row should close it
        # even when the run aborted mid-way.
        close = getattr(agent, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    if not isinstance(result, dict):
        record.error = f"run_conversation returned {type(result).__name__}, expected dict"
        record.reason = record.error
        record.duration_seconds = time.time() - started
        return record

    messages = result.get("messages") or []
    record.api_calls = int(result.get("api_calls") or 0)
    record.turns = int(result.get("iterations_used") or 0)
    record.stop_reason = str(result.get("stop_reason") or "")
    record.final_response = str(result.get("final_response") or "")
    record.trajectory_summary = _summarise_messages(messages)
    record.input_tokens, record.output_tokens = _extract_token_totals(messages)
    record.session_id = getattr(agent, "session_id", None)
    record.duration_seconds = time.time() - started

    verifier_result = _run_verifier(task, record)
    record.verdict = verifier_result.verdict
    record.reason = verifier_result.reason
    return record


def run_tasks(
    agent_factory: AgentFactory,
    tasks: Sequence[GoldenTask],
) -> List[RunRecord]:
    """Run every task in order, collecting RunRecords.  Errors per task
    don't abort the batch — only harness-level loader failures do."""
    return [run_task(agent_factory, t) for t in tasks]


# ── Report renderers ────────────────────────────────────────────────


def format_report_text(records: Sequence[RunRecord]) -> str:
    """Human-readable summary of a run set.

    First a per-task block, then an aggregate footer.  Designed to fit
    inside a terminal — long final_response / trajectory entries are
    truncated.  The ``cost_usd`` figure is left at 0.0 in wave 1; it
    fills in when wave 3 wires usage_pricing.
    """
    if not records:
        return "(no tasks ran)"

    lines: List[str] = []
    pass_count = 0
    fail_count = 0
    error_count = 0
    skip_count = 0
    total_turns = 0
    total_cost = 0.0
    for r in records:
        marker = {
            Verdict.PASS: "✓",
            Verdict.FAIL: "✗",
            Verdict.ERROR: "!",
            Verdict.SKIP: "~",
        }[r.verdict]
        lines.append(
            f"{marker} {r.task_id:30s}  {r.verdict.value:5s}  "
            f"turns={r.turns:<3}  tokens={r.input_tokens}+{r.output_tokens:<5}  "
            f"{r.duration_seconds:.1f}s"
        )
        if r.reason:
            lines.append(f"    reason: {r.reason}")
        if r.error:
            lines.append(f"    error:  {r.error}")
        total_turns += r.turns
        total_cost += r.cost_usd
        if r.verdict == Verdict.PASS:
            pass_count += 1
        elif r.verdict == Verdict.FAIL:
            fail_count += 1
        elif r.verdict == Verdict.ERROR:
            error_count += 1
        else:
            skip_count += 1

    n = len(records)
    avg_turns = total_turns / n if n else 0.0
    lines.append("")
    lines.append("─" * 70)
    lines.append(
        f"Total: {n} tasks │ {pass_count} PASS │ {fail_count} FAIL │ "
        f"{error_count} ERROR │ {skip_count} SKIP │ "
        f"{avg_turns:.1f} avg turns │ ${total_cost:.4f}"
    )
    return "\n".join(lines)


def format_report_json(records: Sequence[RunRecord]) -> str:
    """Machine-readable shape — same fields the text renderer uses."""
    return json.dumps(
        {
            "records": [r.to_dict() for r in records],
            "summary": _summary(records),
        },
        indent=2,
    )


def _summary(records: Sequence[RunRecord]) -> Dict[str, Any]:
    n = len(records)
    counts = {v.value: 0 for v in Verdict}
    for r in records:
        counts[r.verdict.value] += 1
    total_turns = sum(r.turns for r in records)
    total_cost = sum(r.cost_usd for r in records)
    total_in = sum(r.input_tokens for r in records)
    total_out = sum(r.output_tokens for r in records)
    return {
        "task_count": n,
        "verdicts": counts,
        "total_turns": total_turns,
        "avg_turns": (total_turns / n) if n else 0.0,
        "total_input_tokens": total_in,
        "total_output_tokens": total_out,
        "total_cost_usd": total_cost,
        "pass_rate": (counts[Verdict.PASS.value] / n) if n else 0.0,
    }
