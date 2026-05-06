"""
Phalanx evaluation harness — Phase 2.8.a waves 1–3.

Loads "golden tasks" from YAML files, runs each one through ``AIAgent.
run_conversation``, captures a ``RunRecord`` (verdict + tokens + cost +
turns), and renders a comparable report.  Wave 1 shipped the data
shapes, loader, runner skeleton, and report renderer; wave 2 added the
seed task set; wave 3 fills in the three verifier types
(``exact_match`` / ``tool_called`` / ``file_state``), wires per-task
cost via ``agent.usage_pricing``, and surfaces structured tool_calls
on ``RunRecord``.

Design rationale: see [`docs/agent-self-evolution.md`](../docs/agent-self-evolution.md)
§2.6 and [`docs/MIGRATION_PLAN.md`](../docs/MIGRATION_PLAN.md) §2.8.a.
"""

from __future__ import annotations

import json
import logging
import os
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

    ``tool_calls`` is the structured list of (name, arguments) tuples
    extracted from assistant turns — this is what ``tool_called`` /
    ``args_subset`` verifiers run against.  ``cost_status`` mirrors
    ``CostResult.status`` from agent.usage_pricing ("estimated" /
    "actual" / "included" / "unknown") so reports can flag rows whose
    pricing isn't reliable.
    """

    task_id: str
    verdict: Verdict
    reason: str = ""
    turns: int = 0
    api_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0
    cost_status: str = "unknown"
    duration_seconds: float = 0.0
    stop_reason: str = ""
    final_response: str = ""
    trajectory_summary: List[str] = field(default_factory=list)
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None
    session_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["verdict"] = self.verdict.value
        return d


# ── Verifier registry ────────────────────────────────────────────────

VerifierFn = Callable[[GoldenTask, RunRecord], "VerifierResult"]


@dataclass
class VerifierResult:
    """Output of a verifier — verdict + a short human-readable reason."""

    verdict: Verdict
    reason: str = ""


VERIFIERS: Dict[str, VerifierFn] = {}


def register_verifier(name: str) -> Callable[[VerifierFn], VerifierFn]:
    """Decorator to register a verifier under ``name``.

    Wave 3 ships ``exact_match`` / ``tool_called`` / ``file_state``
    via this decorator.  Keeping registration explicit (vs
    auto-discovery) mirrors phalanx's static-import pattern in
    ``tools/__init__.py``.
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


# ── Concrete verifiers (wave 3) ──────────────────────────────────────


def _as_str_list(value: Any) -> List[str]:
    """Accept either a single string or a list of strings as ``contains``."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value]
    raise ValueError(f"expected string or list of strings, got {type(value).__name__}")


@register_verifier("exact_match")
def _verify_exact_match(task: GoldenTask, record: RunRecord) -> VerifierResult:
    """Assert that ``record.final_response`` contains every ``contains``
    substring (AND semantics).  ``case_insensitive`` (bool, default
    false) lowercases both sides before comparing.

    Schema (under ``expected``):
      * ``contains``: str | list[str]   — required
      * ``case_insensitive``: bool      — optional, default false
    """
    needles = _as_str_list(task.expected.get("contains"))
    if not needles:
        return VerifierResult(
            Verdict.ERROR,
            "exact_match: expected.contains is required and must be non-empty",
        )
    haystack = record.final_response or ""
    if task.expected.get("case_insensitive"):
        haystack_cmp = haystack.lower()
        needles_cmp = [n.lower() for n in needles]
    else:
        haystack_cmp = haystack
        needles_cmp = needles
    missing = [orig for orig, cmp_ in zip(needles, needles_cmp) if cmp_ not in haystack_cmp]
    if missing:
        return VerifierResult(
            Verdict.FAIL,
            f"final_response missing substring(s): {missing!r}",
        )
    return VerifierResult(
        Verdict.PASS,
        f"final_response contains {len(needles)} required substring(s)",
    )


def _args_subset_match(actual: Dict[str, Any], expected: Dict[str, Any]) -> Optional[str]:
    """Return None on match, or a human-readable mismatch description.

    Subset semantics: every key in ``expected`` must be present in
    ``actual`` with an equal value.  Missing keys / mismatched values
    produce a single-line reason — wave 3 keeps this strict-equality
    only; richer matchers (regex / contains / glob on path) can land in
    later waves if needed.
    """
    for key, want in expected.items():
        if key not in actual:
            return f"args missing key {key!r}"
        if actual[key] != want:
            return f"args[{key!r}] = {actual[key]!r}, expected {want!r}"
    return None


@register_verifier("tool_called")
def _verify_tool_called(task: GoldenTask, record: RunRecord) -> VerifierResult:
    """Assert that the agent invoked a specific tool (optionally with
    matching args).  Walks ``record.tool_calls`` (populated by the
    runner from each assistant turn).

    Schema (under ``expected``):
      * ``tool``: str          — required, tool name to match
      * ``args_subset``: dict  — optional, every key/value must match
    """
    want = task.expected.get("tool")
    if not want:
        return VerifierResult(
            Verdict.ERROR,
            "tool_called: expected.tool is required",
        )
    args_subset = task.expected.get("args_subset") or {}
    if not isinstance(args_subset, dict):
        return VerifierResult(
            Verdict.ERROR,
            f"tool_called: expected.args_subset must be a mapping, got {type(args_subset).__name__}",
        )

    matching_name = [c for c in record.tool_calls if c.get("name") == want]
    if not matching_name:
        called = sorted({c.get("name", "") for c in record.tool_calls if c.get("name")})
        return VerifierResult(
            Verdict.FAIL,
            f"tool {want!r} never called; called instead: {called or '(none)'}",
        )

    if not args_subset:
        return VerifierResult(Verdict.PASS, f"tool {want!r} called {len(matching_name)} time(s)")

    # Find at least one call whose args satisfy the subset.
    last_mismatch = ""
    for call in matching_name:
        actual_args = call.get("arguments") or {}
        if not isinstance(actual_args, dict):
            last_mismatch = f"args not a dict (got {type(actual_args).__name__})"
            continue
        mismatch = _args_subset_match(actual_args, args_subset)
        if mismatch is None:
            return VerifierResult(
                Verdict.PASS,
                f"tool {want!r} called with matching args_subset",
            )
        last_mismatch = mismatch
    return VerifierResult(
        Verdict.FAIL,
        f"tool {want!r} called {len(matching_name)} time(s) but no call matched args_subset: {last_mismatch}",
    )


@register_verifier("file_state")
def _verify_file_state(task: GoldenTask, record: RunRecord) -> VerifierResult:
    """Assert post-run filesystem state at ``expected.path``.

    Path resolution: relative paths resolve against ``os.getcwd()`` (the
    eval CLI runs at the repo root, so a relative ``eval_artifact.txt``
    lands beside ``pyproject.toml``).  Wave 4 may add per-task tmp
    sandboxing so this stops touching the working tree.

    Schema (under ``expected``):
      * ``path``: str           — required
      * ``exists``: bool        — optional, default true
      * ``contains``: str | list[str]  — optional substring(s) to match
    """
    rel = task.expected.get("path")
    if not rel:
        return VerifierResult(
            Verdict.ERROR,
            "file_state: expected.path is required",
        )
    target = Path(rel)
    if not target.is_absolute():
        target = Path(os.getcwd()) / target
    must_exist = task.expected.get("exists", True)

    if not target.exists():
        if not must_exist:
            return VerifierResult(Verdict.PASS, f"{rel!r} absent as expected")
        return VerifierResult(Verdict.FAIL, f"{rel!r} not found at {target}")

    if not must_exist:
        return VerifierResult(Verdict.FAIL, f"{rel!r} unexpectedly exists at {target}")

    needles = _as_str_list(task.expected.get("contains"))
    if not needles:
        return VerifierResult(Verdict.PASS, f"{rel!r} exists")

    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return VerifierResult(Verdict.ERROR, f"file_state: read of {rel!r} failed: {exc}")

    missing = [n for n in needles if n not in text]
    if missing:
        return VerifierResult(
            Verdict.FAIL,
            f"{rel!r} missing substring(s): {missing!r}",
        )
    return VerifierResult(Verdict.PASS, f"{rel!r} contains all required substrings")


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


def _extract_tool_calls(messages: Sequence[Any]) -> List[Dict[str, Any]]:
    """Walk ``messages`` and return every assistant tool_call as a flat
    list of ``{name, arguments, id}`` dicts.

    ``arguments`` is parsed from JSON when possible (the OpenAI SDK
    serialises it as a JSON string); on parse failure the raw string is
    kept under ``arguments_raw`` so verifiers can still surface
    something useful in their reason.
    """
    out: List[Dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            name = fn.get("name") if isinstance(fn, dict) else getattr(fn, "name", None)
            args_raw = fn.get("arguments") if isinstance(fn, dict) else getattr(fn, "arguments", None)
            parsed: Any
            if isinstance(args_raw, dict):
                parsed = args_raw
                args_raw_str: Optional[str] = None
            else:
                args_raw_str = args_raw if isinstance(args_raw, str) else (
                    None if args_raw is None else str(args_raw)
                )
                if args_raw_str:
                    try:
                        parsed = json.loads(args_raw_str)
                    except (ValueError, TypeError):
                        parsed = None
                else:
                    parsed = {}
            entry: Dict[str, Any] = {
                "name": name or "",
                "arguments": parsed if isinstance(parsed, dict) else None,
                "id": tc.get("id"),
            }
            if args_raw_str is not None and not isinstance(parsed, dict):
                entry["arguments_raw"] = args_raw_str
            out.append(entry)
    return out


def _compute_cost_usd(
    agent: Any, usage_totals: Dict[str, int]
) -> tuple[float, str]:
    """Estimate USD cost for the run via ``agent.usage_pricing``.

    Returns ``(cost_usd, status)`` where status is one of
    "estimated" / "actual" / "included" / "unknown".  Returns
    ``(0.0, "unknown")`` on any failure — eval should never crash
    because pricing data is missing for some model.
    """
    try:
        from agent.usage_pricing import CanonicalUsage, estimate_usage_cost
        canon = CanonicalUsage(
            input_tokens=int(usage_totals.get("input_tokens", 0)),
            output_tokens=int(usage_totals.get("output_tokens", 0)),
            cache_read_tokens=int(usage_totals.get("cache_read_tokens", 0)),
            cache_write_tokens=int(usage_totals.get("cache_write_tokens", 0)),
            reasoning_tokens=int(usage_totals.get("reasoning_tokens", 0)),
        )
        result = estimate_usage_cost(
            getattr(agent, "model", "") or "",
            canon,
            provider=getattr(agent, "provider", None),
            base_url=getattr(agent, "base_url", None) or None,
        )
    except Exception as exc:
        logger.debug("cost estimation failed: %s", exc)
        return 0.0, "unknown"
    amount = result.amount_usd
    if amount is None:
        return 0.0, result.status
    try:
        return float(amount), result.status
    except (TypeError, ValueError):
        return 0.0, result.status


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
    record.tool_calls = _extract_tool_calls(messages)

    usage_totals = result.get("usage_totals") or {}
    if not isinstance(usage_totals, dict):
        usage_totals = {}
    record.input_tokens = int(usage_totals.get("input_tokens") or 0)
    record.output_tokens = int(usage_totals.get("output_tokens") or 0)
    record.cache_read_tokens = int(usage_totals.get("cache_read_tokens") or 0)
    record.cache_write_tokens = int(usage_totals.get("cache_write_tokens") or 0)
    record.reasoning_tokens = int(usage_totals.get("reasoning_tokens") or 0)
    record.cost_usd, record.cost_status = _compute_cost_usd(agent, usage_totals)
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


def format_report_text(
    records: Sequence[RunRecord],
    *,
    tasks: Optional[Sequence[GoldenTask]] = None,
) -> str:
    """Human-readable summary of a run set.

    Per-task block (verdict + turns + tokens + duration), optional
    reason / error lines, then an aggregate footer with per-category
    pass-rate breakdown when ``tasks`` is supplied.  Designed to fit
    inside a terminal — long entries are truncated upstream.
    """
    if not records:
        return "(no tasks ran)"

    # Build task_id → category lookup so the per-category footer can
    # bucket records without re-loading YAML.
    category_by_id: Dict[str, str] = {}
    if tasks is not None:
        for t in tasks:
            category_by_id[t.task_id] = t.category

    lines: List[str] = []
    pass_count = 0
    fail_count = 0
    error_count = 0
    skip_count = 0
    total_turns = 0
    total_cost = 0.0
    has_unknown_cost = False
    # per-category aggregates: {category: [pass, fail, error, skip]}
    cat_buckets: Dict[str, List[int]] = {}
    for r in records:
        marker = {
            Verdict.PASS: "✓",
            Verdict.FAIL: "✗",
            Verdict.ERROR: "!",
            Verdict.SKIP: "~",
        }[r.verdict]
        cost_marker = "" if r.cost_status in ("estimated", "actual") else " ?"
        lines.append(
            f"{marker} {r.task_id:30s}  {r.verdict.value:5s}  "
            f"turns={r.turns:<3}  tokens={r.input_tokens}+{r.output_tokens:<5}  "
            f"${r.cost_usd:.4f}{cost_marker}  {r.duration_seconds:.1f}s"
        )
        if r.reason:
            lines.append(f"    reason: {r.reason}")
        if r.error:
            lines.append(f"    error:  {r.error}")
        total_turns += r.turns
        total_cost += r.cost_usd
        if r.cost_status == "unknown":
            has_unknown_cost = True
        idx = {
            Verdict.PASS: 0, Verdict.FAIL: 1, Verdict.ERROR: 2, Verdict.SKIP: 3,
        }[r.verdict]
        cat = category_by_id.get(r.task_id, "uncategorised")
        bucket = cat_buckets.setdefault(cat, [0, 0, 0, 0])
        bucket[idx] += 1
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
        + (" (some unknown)" if has_unknown_cost else "")
    )
    if cat_buckets and (tasks is not None):
        # Stable, alphabetical category order so day-over-day diffs line up.
        for cat in sorted(cat_buckets):
            p, f_, e, s = cat_buckets[cat]
            total = p + f_ + e + s
            lines.append(
                f"  · {cat:12s} {p}/{total} pass │ "
                f"{f_} fail │ {e} err │ {s} skip"
            )
    return "\n".join(lines)


def format_report_json(
    records: Sequence[RunRecord],
    *,
    tasks: Optional[Sequence[GoldenTask]] = None,
) -> str:
    """Machine-readable shape — same fields the text renderer uses."""
    return json.dumps(
        {
            "records": [r.to_dict() for r in records],
            "summary": _summary(records, tasks=tasks),
        },
        indent=2,
    )


def _summary(
    records: Sequence[RunRecord],
    *,
    tasks: Optional[Sequence[GoldenTask]] = None,
) -> Dict[str, Any]:
    n = len(records)
    counts = {v.value: 0 for v in Verdict}
    for r in records:
        counts[r.verdict.value] += 1
    total_turns = sum(r.turns for r in records)
    total_cost = sum(r.cost_usd for r in records)
    total_in = sum(r.input_tokens for r in records)
    total_out = sum(r.output_tokens for r in records)

    # Per-category PASS/FAIL/ERROR/SKIP buckets when tasks are known.
    category_breakdown: Dict[str, Dict[str, int]] = {}
    if tasks is not None:
        category_by_id = {t.task_id: t.category for t in tasks}
        for r in records:
            cat = category_by_id.get(r.task_id, "uncategorised")
            bucket = category_breakdown.setdefault(
                cat, {v.value: 0 for v in Verdict}
            )
            bucket[r.verdict.value] += 1

    return {
        "task_count": n,
        "verdicts": counts,
        "total_turns": total_turns,
        "avg_turns": (total_turns / n) if n else 0.0,
        "total_input_tokens": total_in,
        "total_output_tokens": total_out,
        "total_cost_usd": total_cost,
        "pass_rate": (counts[Verdict.PASS.value] / n) if n else 0.0,
        "category_breakdown": category_breakdown,
    }


# ── Run persistence (wave 4) ─────────────────────────────────────────


# Snapshot directory layout:
#
#   ~/.phalanx/eval/
#       2026-05-06T16-32-33Z/
#           records.json     ← list of RunRecord dicts (same shape as
#                              format_report_json's "records")
#           summary.json     ← _summary() output
#           tasks.json       ← serialised GoldenTask list (id + category +
#                              verifier_type + prompt) so --diff works
#                              without re-loading the YAML
#           report.txt       ← format_report_text(records, tasks=...)
#
# Timestamps avoid colons (Windows-incompatible) — we use
# YYYY-MM-DDTHH-MM-SSZ.  ``run_id`` is always the directory name.


def _eval_root() -> Path:
    """Resolve the eval persistence root, lazily so test fixtures that
    monkeypatch ``PHALANX_HOME`` get the override."""
    try:
        from hermes_constants import get_hermes_home
        return get_hermes_home() / "eval"
    except Exception:
        # Fallback for ultra-minimal test environments where
        # hermes_constants can't be imported (shouldn't happen in
        # phalanx itself, but keeps eval.py decoupled).
        return Path.home() / ".phalanx" / "eval"


def _make_run_id(now: Optional[float] = None) -> str:
    """Filesystem-safe ISO-ish timestamp: ``2026-05-06T16-32-33Z``."""
    from datetime import datetime, timezone
    ts = datetime.fromtimestamp(now or time.time(), tz=timezone.utc)
    return ts.strftime("%Y-%m-%dT%H-%M-%SZ")


def _serialise_task(task: GoldenTask) -> Dict[str, Any]:
    """Pickle-free task snapshot for the run dir.

    Only fields that influence diffs / future re-runs are kept;
    description/system are dropped to keep the snapshot small.
    """
    return {
        "task_id": task.task_id,
        "prompt": task.prompt,
        "verifier_type": task.verifier_type,
        "category": task.category,
        "expected": task.expected,
    }


def save_run(
    records: Sequence[RunRecord],
    *,
    tasks: Optional[Sequence[GoldenTask]] = None,
    root: Optional[Path] = None,
    run_id: Optional[str] = None,
) -> Path:
    """Persist a finished eval run.  Returns the run directory.

    ``root`` defaults to ``<PHALANX_HOME>/eval/``.  ``run_id`` defaults
    to a fresh UTC timestamp.  Overwrites any existing run dir with the
    same id (only happens in tests that pin run_id).
    """
    base = (root or _eval_root())
    rid = run_id or _make_run_id()
    run_dir = base / rid
    run_dir.mkdir(parents=True, exist_ok=True)

    records_payload = [r.to_dict() for r in records]
    summary_payload = _summary(records, tasks=tasks)
    tasks_payload = [_serialise_task(t) for t in tasks] if tasks else []

    (run_dir / "records.json").write_text(
        json.dumps(records_payload, indent=2), encoding="utf-8"
    )
    (run_dir / "summary.json").write_text(
        json.dumps(summary_payload, indent=2), encoding="utf-8"
    )
    if tasks_payload:
        (run_dir / "tasks.json").write_text(
            json.dumps(tasks_payload, indent=2), encoding="utf-8"
        )
    (run_dir / "report.txt").write_text(
        format_report_text(records, tasks=tasks), encoding="utf-8"
    )
    return run_dir


def list_runs(root: Optional[Path] = None) -> List[Dict[str, Any]]:
    """List persisted runs in newest-first order.

    Each entry: ``{run_id, path, summary}``.  ``summary`` is the cached
    summary.json contents (or ``{}`` if the file is missing/corrupt —
    lets a partially-written run show up in ``hermes eval list --runs``
    rather than silently disappearing).
    """
    base = (root or _eval_root())
    if not base.exists():
        return []
    out: List[Dict[str, Any]] = []
    for child in sorted(base.iterdir(), reverse=True):
        if not child.is_dir():
            continue
        summary: Dict[str, Any] = {}
        sf = child / "summary.json"
        if sf.exists():
            try:
                summary = json.loads(sf.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                summary = {}
        out.append({"run_id": child.name, "path": str(child), "summary": summary})
    return out


def load_run(
    run_id: str, root: Optional[Path] = None
) -> Dict[str, Any]:
    """Read a persisted run back as ``{records, summary, tasks}`` dicts.

    Raises ``FileNotFoundError`` if ``run_id`` doesn't exist or is
    missing the required ``records.json``.  ``tasks`` is ``[]`` when
    the run pre-dates wave 4 (or the run was saved without tasks).
    """
    base = (root or _eval_root())
    run_dir = base / run_id
    rf = run_dir / "records.json"
    if not rf.exists():
        raise FileNotFoundError(f"eval run {run_id!r} not found at {run_dir}")
    records = json.loads(rf.read_text(encoding="utf-8"))
    sf = run_dir / "summary.json"
    summary = json.loads(sf.read_text(encoding="utf-8")) if sf.exists() else {}
    tf = run_dir / "tasks.json"
    tasks = json.loads(tf.read_text(encoding="utf-8")) if tf.exists() else []
    return {"records": records, "summary": summary, "tasks": tasks, "path": str(run_dir)}


# ── Run diff (wave 4) ────────────────────────────────────────────────


def format_diff(
    current: Sequence[RunRecord],
    baseline_run: Dict[str, Any],
    *,
    tasks: Optional[Sequence[GoldenTask]] = None,
) -> str:
    """Render a textual diff: current run vs a persisted baseline.

    Per task, prints:
      * verdict change (PASS → FAIL, etc.)
      * token delta (signed, only when non-zero)
      * cost delta (signed, four decimals)
      * tasks present in only one of the two runs

    Designed to be skim-readable in CI; large numeric deltas surface
    even when verdicts haven't moved (regression in token usage is
    just as interesting as a verdict flip for the eval loop).
    """
    base_records = {
        r.get("task_id"): r for r in baseline_run.get("records", [])
        if isinstance(r, dict) and r.get("task_id")
    }
    curr_records = {r.task_id: r for r in current}

    all_ids = sorted(set(base_records) | set(curr_records))
    lines: List[str] = []
    lines.append(f"diff vs baseline run with {len(base_records)} task(s)")
    lines.append("─" * 70)

    summary_pass_now = 0
    summary_pass_was = 0
    summary_changed = 0
    only_curr: List[str] = []
    only_base: List[str] = []

    for tid in all_ids:
        bv = base_records.get(tid)
        cv = curr_records.get(tid)
        if cv is None:
            only_base.append(tid)
            continue
        if bv is None:
            only_curr.append(tid)
            lines.append(f"+ {tid:30s}  (new)  {cv.verdict.value}")
            if cv.verdict == Verdict.PASS:
                summary_pass_now += 1
            continue

        was_verdict = str(bv.get("verdict", ""))
        now_verdict = cv.verdict.value
        if was_verdict == "PASS":
            summary_pass_was += 1
        if now_verdict == "PASS":
            summary_pass_now += 1

        d_in = cv.input_tokens - int(bv.get("input_tokens", 0) or 0)
        d_out = cv.output_tokens - int(bv.get("output_tokens", 0) or 0)
        d_cost = cv.cost_usd - float(bv.get("cost_usd", 0.0) or 0.0)
        d_turns = cv.turns - int(bv.get("turns", 0) or 0)

        verdict_changed = was_verdict != now_verdict
        deltas_meaningful = d_in or d_out or d_turns or abs(d_cost) > 1e-6
        if not verdict_changed and not deltas_meaningful:
            continue

        summary_changed += 1
        if verdict_changed:
            arrow = f"{was_verdict} → {now_verdict}"
        else:
            arrow = f"{now_verdict} (unchanged)"
        parts = [f"  {tid:30s}  {arrow}"]
        deltas = []
        if d_in or d_out:
            deltas.append(f"tokens {d_in:+d}/{d_out:+d}")
        if d_turns:
            deltas.append(f"turns {d_turns:+d}")
        if abs(d_cost) > 1e-6:
            deltas.append(f"cost {d_cost:+.4f}")
        if deltas:
            parts.append("  " + " │ ".join(deltas))
        lines.append("".join(parts))

    if only_base:
        lines.append("")
        lines.append("removed tasks (in baseline only):")
        for tid in only_base:
            lines.append(f"  - {tid}")

    lines.append("")
    lines.append("─" * 70)
    n_curr = len(curr_records)
    pass_now_pct = (summary_pass_now / n_curr * 100) if n_curr else 0.0
    n_base = len(base_records)
    pass_was_pct = (summary_pass_was / n_base * 100) if n_base else 0.0
    lines.append(
        f"Pass rate: {summary_pass_was}/{n_base} ({pass_was_pct:.0f}%) → "
        f"{summary_pass_now}/{n_curr} ({pass_now_pct:.0f}%) │ "
        f"{summary_changed} task(s) changed │ "
        f"{len(only_curr)} new │ {len(only_base)} removed"
    )
    # Suppress "tasks" if not used — we only consume it for symmetry with
    # other renderers; future waves may surface category-level diff.
    _ = tasks
    return "\n".join(lines)
