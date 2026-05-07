# Phase 2.8.a 设计文档 — Evaluation Loop（Golden Tasks + Verifiers + Persistence + Diff）

> 配套阅读：
> - [`MIGRATION_PLAN.md`](MIGRATION_PLAN.md) §2.8.a — 计划层面的 wave 分解与上游对照
> - [`agent-self-evolution.md`](agent-self-evolution.md) §2.6 — 评估闭环在自主进化八大技术点中的位置
> - [`ARCHITECTURE.md`](ARCHITECTURE.md) — 全局视图
> - [`phase-2.5-sessions.md`](phase-2.5-sessions.md) — SessionDB（trajectory / token 的源数据）
> - [`phase-2.3-prompt-context.md`](phase-2.3-prompt-context.md) — `agent.usage_pricing`（cost 计算依赖）

本文记录 **§2.8.a 四个 wave 把"任何改动都有数字"装到 phalanx 上的过程**——`tests/golden/` YAML schema + `hermes_cli/eval.py` 三件套（loader / runner / verifier）+ `hermes eval` 子命令 + 持久化 / diff / CI smoke。落地后 phalanx 第一次具备**单次跑全套 golden task → 落基线 → 改完代码 → diff 看回归**的完整闭环。

§2.8.a 是 §2.8 系列里**唯一无前置依赖**的子期，故放第一——后续每条（§2.8.b memory、§2.8.c delegate、§2.8.e skills…）改完都能 `hermes eval --baseline X --diff` 验证有没有变差，避免"感觉良好的猜测"。

## 0. 范围与 wave 划分

| Wave | 内容 | 估行 | 状态 |
|---|---|---|---|
| 1 | `tests/golden/` YAML schema + `hermes_cli/eval.py` skeleton（GoldenTask / RunRecord / VerifierResult / Verdict 数据形状 + loader + runner skeleton + 空 VERIFIERS registry）+ `hermes eval` argparse 子命令骨架 | ~330 | ✅ |
| 2 | 10 个种子 golden task（5 类别）：1 smoke + 3 file ops + 2 web + 2 plan + 2 multi-tool；其中 7 用 `tool_called`、2 用 `exact_match`、1 用 `file_state` | ~150 | ✅ |
| 3 | 三种 verifier（`exact_match` / `tool_called` / `file_state`）+ 在 `AIAgent` 加 `self.usage_totals` 累加器并通过 `agent.usage_pricing.estimate_usage_cost` 计算 `cost_usd` + 结构化 `tool_calls` 上 `RunRecord` + report 渲染加 per-category 分桶 + unknown-cost flag | ~430 | ✅ |
| 4 | 报告持久化到 `~/.phalanx/eval/<timestamp>/`（records.json / summary.json / tasks.json / report.txt）+ `--baseline <run_id>` / `--diff` / `--no-save` 三个 flag + `eval list --runs` + `tests/test_eval_ci_smoke.py`（stub agent 跑 3 类 verifier，结构性回归网） | ~700 | ✅ |

§2.8.a 落地后：

- `pip install -e .` 后一行命令 `hermes eval` 跑全套 golden task，文本报告打到 stdout
- 默认每次跑都落到 `~/.phalanx/eval/<timestamp>/` 一个目录里，机器可读 JSON + 人类可读 txt 各一份
- `hermes eval --baseline <run_id> --diff` 跟历史 run 比 verdict / token / cost / turns 增量
- CI 每次 push 自动跑 `tests/test_eval_ci_smoke.py` 防 runner-verifier-save 链路结构性退化（**不调真 model**，~1.4s 跑完）
- 真 model 周跑写在 `MIGRATION_PLAN.md §2.8.a "手动周跑"` 一节，本地手动触发

## 1. 闭环图与组件

```
        ┌────────────────────┐
        │  tests/golden/     │   YAML schema：task_id / prompt /
        │  *.yaml            │   verifier_type / expected / category
        └─────────┬──────────┘
                  │ load_golden_tasks()
                  ▼
        ┌────────────────────┐
        │  GoldenTask 列表   │   dataclass，字段映射 YAML
        └─────────┬──────────┘
                  │ run_tasks(factory, tasks)
                  ▼
        ┌────────────────────────────────────────────┐
        │  run_task(factory, task)                   │
        │    ① factory(task) → AIAgent              │
        │    ② agent.run_conversation(task.prompt)   │  ← 跑真 model（或 stub）
        │    ③ 抓 result["usage_totals"]            │
        │    ④ _extract_tool_calls(messages)        │
        │    ⑤ _compute_cost_usd(...)               │
        │    ⑥ _run_verifier(task, record)          │
        └─────────┬──────────────────────────────────┘
                  │
                  ▼
        ┌────────────────────┐
        │  RunRecord 列表    │   verdict / turns / tokens /
        │                    │   cost_usd / tool_calls / ...
        └─────────┬──────────┘
                  │ save_run(records, tasks=...)
                  │
        ┌─────────┴──────────┐ ┌───────────────────────┐
        ▼                    ▼ ▼                       │
  format_report_text    save_run() ──────────────► ~/.phalanx/eval/
  format_report_json     ┌────────────────────┐    <timestamp>/
                         │ records.json       │       records.json
                         │ summary.json       │       summary.json
                         │ tasks.json         │       tasks.json
                         │ report.txt         │       report.txt
                         └────────────────────┘
                                  │
                                  │  load_run(run_id)
                                  ▼
                         ┌────────────────────┐
                         │  format_diff(curr, │
                         │  baseline)         │   per-task verdict 变化 +
                         │                    │   token/cost/turns 增量 +
                         │                    │   added/removed task list
                         └────────────────────┘
```

**关键解耦**：`run_task` 不直接 import `run_agent.AIAgent`——通过 `AgentFactory = Callable[[GoldenTask], Any]` 让调用方注入工厂。这意味着：

- CLI 注入真 `AIAgent`（CLI 路径，调真 model）
- pytest CI 注入 `_StubAgent`（结构性测试，不调网络）
- 未来 §2.8.b 可注入"带 memory 预填的 agent"做 ablation 实验

工厂模式比"`run_task` 自己实例化 agent"重要——否则 eval 测试里**永远**得跑真 API。

## 2. Wave 1 — Skeleton（~330 行）

### 2.1 GoldenTask schema

```python
@dataclass
class GoldenTask:
    task_id: str
    prompt: str
    verifier_type: str          # exact_match / tool_called / file_state
    expected: Dict[str, Any]    # verifier-specific config
    category: str = "uncategorised"
    system: Optional[str] = None
    max_iterations: int = 30
    model: Optional[str] = None
    description: str = ""
```

YAML 字段一对一映射，外加一个 alias：顶层 `verifier:` 也接受为 `verifier_type` 的同义词（保留前向兼容空间）。

### 2.2 RunRecord schema

```python
@dataclass
class RunRecord:
    task_id: str
    verdict: Verdict            # PASS / FAIL / ERROR / SKIP
    reason: str = ""
    turns: int = 0
    api_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0  # （wave 3 加）
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0
    cost_status: str = "unknown" # estimated / actual / included / unknown
    duration_seconds: float = 0.0
    stop_reason: str = ""
    final_response: str = ""
    trajectory_summary: List[str] = field(default_factory=list)
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)  # （wave 3 加）
    error: Optional[str] = None
    session_id: Optional[str] = None
```

`Verdict` 借鉴 pytest 的 PASS / FAIL 模型，**ERROR** 用于 harness 自身崩溃（loader 抛异常 / agent.run_conversation 抛异常 / 返回非 dict），**SKIP** 用于 verifier_type 没注册（wave 1 时所有 task 都 SKIP，是 schema-locking placeholder）。

### 2.3 Verifier registry

```python
VERIFIERS: Dict[str, Callable[[GoldenTask, RunRecord], VerifierResult]] = {}

@register_verifier("name")
def _verify_name(task, record): ...
```

显式注册（vs 自动发现）和 `tools/__init__.py` 里 phalanx 的静态 import 模式一致。`_run_verifier(task, record)` 包一层 try/except → ERROR，单 verifier 崩溃不会带倒整个 batch。

### 2.4 Loader 防御

`load_golden_tasks(directory)` 四种校验：

| 校验 | 错误 |
|---|---|
| 顶层不是 mapping | `ValueError: <file>: top-level must be a mapping, got <type>` |
| 缺 `task_id` 或 `prompt` | `ValueError: <file>: missing required field 'X'` |
| 缺 `verifier_type` 又没 `verifier` alias | `ValueError: <file>: missing required field 'verifier_type'` |
| 跨文件重复 task_id | `ValueError: <file>: duplicate task_id 'X'` |

`_` 前缀文件被跳过（用作禁用 / fixture）。

## 3. Wave 2 — 10 个种子 task（~150 行）

| 类别 | task_id | verifier | 检的是什么 |
|---|---|---|---|
| smoke | smoke_oneshot | exact_match | agent 能跑通最小 loop，回复包含字面量 sentinel |
| file | file_read_pyproject | tool_called + args_subset | 调 `read_file` 且 path=pyproject.toml |
| file | file_patch_create_file | file_state | 跑完后 `eval_artifact.txt` 存在且包含 marker |
| file | file_search_iteration_budget | tool_called | 使用 `search_files`（符号位置不可猜） |
| web | web_search_official | tool_called | 调 `web_search` 而不是幻觉 URL |
| web | web_extract_url | tool_called + args_subset | 调 `web_extract` 且 url=https://example.com |
| plan | plan_explain_closure | exact_match (and/case_insensitive) | 回复同时含 "closure" 和 "function" |
| plan | plan_suggest_improvement | tool_called + args_subset | 提建议前先读 `hermes_cli/eval.py` |
| multi-tool | multi_search_then_read | tool_called | 隐式 search → read 链路（验最后一步） |
| multi-tool | multi_read_then_propose | tool_called + args_subset | 提建议前先读 README.md |

**verifier 类型分布**：7× tool_called、2× exact_match、1× file_state。倾向 `tool_called` 是有意的——它捕获"agent 走到了正确的 action 形状"，比断言最终文本对模型 verbosity / 随机性更鲁棒。

## 4. Wave 3 — 三种 verifier + 成本接入（~430 行）

### 4.1 `exact_match`

```yaml
verifier_type: exact_match
expected:
  contains: ["pong-phalanx"]   # str | list[str]，AND 语义
  case_insensitive: true       # 可选，默认 false
```

`final_response` 必须**全部**包含 `contains` 里的子串；缺一个就 FAIL，错误信息列出**缺哪些**。`contains` 缺失或为空 → ERROR（task schema 写错，不是 agent 错）。

### 4.2 `tool_called`

```yaml
verifier_type: tool_called
expected:
  tool: read_file              # 必填
  args_subset:                 # 可选
    path: pyproject.toml       # 严格相等
```

走 `record.tool_calls`（wave 3 在 runner 里从 messages 提取的结构化列表）找名字 == `expected.tool` 的 call；如果有 `args_subset`，至少一个 matching call 的 `arguments` 必须每对 key/value **严格相等**。FAIL 时 reason 同时列**实际调用了什么**——避免"agent 调了 X 但报告说 Y 没调"这种不可调试的输出。

`args_subset` 当前**只支持严格相等**——不做正则 / 路径前缀 / glob 匹配。后续如果需要可加，但要单独立 schema（`args_match: { path: { regex: ... } }`），不挤进 `args_subset`。

### 4.3 `file_state`

```yaml
verifier_type: file_state
expected:
  path: eval_artifact.txt      # 必填，相对/绝对都接受
  exists: true                 # 可选，默认 true（exists=false 时验"必须不存在"）
  contains: "phalanx-eval-marker"   # 可选，str | list[str]
```

相对路径解析对照 `os.getcwd()`（CLI 默认仓库根，pytest 用 `monkeypatch.chdir(tmp_path)` 隔离）。读文件用 `errors="replace"` 防 binary artifact 把 verifier 整崩溃。

**已知限制**：`file_state` 当前不支持"文件应该 NOT 包含 X"或"行数 <= N"。每加一个就要扩 expected schema，等 wave 4+ 真有需求再加。

### 4.4 `agent.usage_pricing` 接入

wave 1 commit 里写 "cost_usd 在 wave 3 wires usage_pricing"——wave 3 兑现。两段改动：

**`run_agent.py` 里加 `self.usage_totals`**（最薄修改，~30 行）：

```python
# __init__:
self.usage_totals = {
    "input_tokens": 0, "output_tokens": 0,
    "cache_read_tokens": 0, "cache_write_tokens": 0,
    "reasoning_tokens": 0,
}

# _accumulate_usage(response):
canon = normalize_usage(response.usage, provider=self.provider, api_mode=...)
self.usage_totals["input_tokens"] += canon.input_tokens
# ...

# run_conversation:
#   - 起头 reset usage_totals
#   - 每次 _make_api_call 成功后 _accumulate_usage(response)
#   - return 加一项 "usage_totals": dict(self.usage_totals)
```

`api_mode` 按 `self.provider` 映射（anthropic_messages / codex_responses / 默认 chat completions），让三种 API 形状的 usage 都能正确 normalize。

**`eval.py` 里 `_compute_cost_usd(agent, usage_totals)`**：

```python
canon = CanonicalUsage(input_tokens=..., output_tokens=..., ...)
result = estimate_usage_cost(
    agent.model, canon,
    provider=agent.provider, base_url=agent.base_url,
)
return float(result.amount_usd or 0), result.status
```

`result.status` 直接落到 `RunRecord.cost_status`，是四值字面量（`estimated` / `actual` / `included` / `unknown`），report 渲染时**unknown** 会带 `?` 标记，footer 会附 "(some unknown)"——避免 demo 里 0.0000 看起来像"免费"。

整段 try/except 包住，pricing 数据缺失（小众模型 / OpenRouter 失联）→ `(0.0, "unknown")`，eval 不会因为"算不出钱"而崩。

### 4.5 `_extract_tool_calls(messages)`

走每条 `role: assistant` 消息的 `tool_calls`，把 `function.arguments` 从 JSON 字符串 parse 成 dict。三种特殊情况：

- arguments 已经是 dict（某些 SDK shim）→ 直接用
- arguments 是 JSON 字符串 → `json.loads` parse
- parse 失败 → `arguments=None` + `arguments_raw=<原字符串>`，让 verifier 至少能报"args 不可解析"

非 assistant role 上的 stray `tool_calls` 字段被忽略——保护面，避免 prompt injection 走那条路。

## 5. Wave 4 — 持久化 + Diff + CI smoke（~700 行）

### 5.1 目录布局：`~/.phalanx/eval/<run_id>/`

```
~/.phalanx/eval/
├── 2026-05-06T16-32-33Z/
│   ├── records.json     ← list[RunRecord.to_dict()]
│   ├── summary.json     ← _summary(records, tasks) 输出
│   ├── tasks.json       ← 每个 GoldenTask 的精简快照
│   └── report.txt       ← format_report_text 渲染的人类可读报告
├── 2026-05-05T18-00-12Z/
│   └── ...
```

**run_id 格式**：`YYYY-MM-DDTHH-MM-SSZ`——ISO 8601 但把 `:` 换成 `-`，原因是 Windows 文件系统不接受冒号。`Z` 表 UTC。

**tasks.json 必要性**：`load_run` 时不需要再回 `tests/golden/` 拉 YAML——任何 baseline 都自携 tasks 快照。这意味着 wave 5 里如果有 task 改了 prompt / verifier，老 baseline 仍然能 diff（虽然 verdict 含义已经变了，那是 user 的事；harness 不去仲裁）。

`_serialise_task(task)` 只保 `task_id / prompt / verifier_type / category / expected`——`description` 和 `system` 不进，省空间。

### 5.2 `save_run` / `load_run` / `list_runs`

```python
save_run(records, *, tasks=None, root=None, run_id=None) -> Path
load_run(run_id, root=None) -> {"records": [...], "summary": {...}, "tasks": [...], "path": "..."}
list_runs(root=None) -> [{"run_id": "...", "path": "...", "summary": {...}}, ...]
```

`root` 默认 `<PHALANX_HOME>/eval/`（懒读 `hermes_constants.get_hermes_home()`）；测试可注入 `tmp_path / "eval"` 完全隔离。

`list_runs` 新到旧排序（reverse `iterdir`），corrupt summary.json 不会让条目消失（容错读：JSON 解析失败 → `summary={}`），让"半写"的 run 在 `eval list --runs` 里仍然可见。

### 5.3 `format_diff(current, baseline_run, *, tasks=None)`

per-task：

```
b                              PASS → FAIL                   ← verdict 变化
a                              PASS (unchanged)  tokens +100/+0
+ c                            (new)  PASS                   ← 新加
- removed: b                                                  ← 没了
```

页脚：

```
Pass rate: 7/10 (70%) → 9/10 (90%) │ 3 task(s) changed │ 1 new │ 1 removed
```

判定"显著变化"的两个轴：

- **verdict 变化**——任何变化都计
- **token / cost / turns 增量** ≠ 0——即使 verdict 没变也计（agent 多烧 100 token 是回归信号）

verdict 没变 + 增量为 0 → 该 task 不打印（保持 diff 简洁）。

### 5.4 CLI 接入

```bash
hermes eval                              # 跑全套 → stdout 文本报告 + 默认落盘
hermes eval --task <id>                  # 只跑一个
hermes eval --json                       # 机器可读
hermes eval --no-save                    # 跑完不落盘
hermes eval --baseline <run_id>          # 报告末尾追加 diff 段
hermes eval --baseline <run_id> --diff   # 只输出 diff，省掉重复 per-task 段
hermes eval list                         # 列 golden task
hermes eval list --runs                  # 列已存档 run
```

`--diff` 没带 `--baseline` → exit 2 + `eval: --diff requires --baseline <run_id>` 提示。`--baseline` 指向不存在 run_id → exit 1 + `eval: eval run 'X' not found at <path>`。

### 5.5 CI Smoke：`tests/test_eval_ci_smoke.py`

唯一 fixture-style 文件，两个测试：

| 测试 | 覆盖 |
|---|---|
| `test_ci_stub_three_verifiers_full_chain` | loader（真 wave-2 YAML）+ runner + 三类 verifier + report + save/load 全链路；`_StubAgent` 预设 final_response / tool_calls / side_effect 让三个真 task 都过 |
| `test_ci_stub_picks_up_regressions...` | 反向证伪：故意让 stub 输出错答案，验证 `exact_match` FAIL；防止链路"沉默通过" |

CI 跑 ~1.4s，**不调网络**。真 model eval 在 `MIGRATION_PLAN.md §2.8.a "手动周跑"` 一节，本地手动触发：

```bash
export OPENAI_API_KEY=sk-...
hermes eval --json > weekly-2026-W19.json     # 落基线
# ... 改完代码后 ...
hermes eval list --runs                        # 拿上周 run_id
hermes eval --baseline 2026-05-06T16-32-33Z --diff
```

## 6. 设计决策与权衡

### 6.1 为什么 verifier 默认 SKIP 而不是 FAIL？

Wave 1 出 skeleton 时有 task 但没 verifier。如果默认 FAIL，wave 1 的 CI 会全红——但 wave 1 的目的是 schema-locking，不是 model gating。SKIP 让 wave 1 commit 通过 CI，wave 3 后所有 task verifier_type 都在三类内，再没 task 会 SKIP。

未来如果有人写了 `verifier_type: my_new_kind` 但忘了实现，eval 会 SKIP 该 task 而非整体崩溃——单 task 的拼写错不应该让其余 9 个失去信号。

### 6.2 为什么 `args_subset` 只支持严格相等？

更复杂的匹配（regex / 路径前缀 / 模糊）需要每条匹配规则单独写 schema。当前 wave-2 task 都能用严格相等表达需求；先加了规则反而会让人写 `args_subset` 时挑哪种语义。

如果未来真出现"agent 可能把 path 写成绝对路径或相对路径都接受"这种场景，应该新立 `args_match` schema 而非给 `args_subset` 重载语义。

### 6.3 为什么不把 cost 当 verifier 类型？

"这个 task 应该花 < $0.01"听起来很合理，但放进 verifier 会让 verdict 跟模型选择强耦合——切了模型 = 全盘 FAIL。cost 应该是**diff 通道**的信号（"换 prompt 后 task X 多烧了 50 token"），不是 verdict 维度。

`format_diff` 把 cost / token / turns 增量统一处理，跟 verdict 变化分两栏列出，正是这个考虑。

### 6.4 为什么 run_id 是时间戳而不是 hash？

time-sortable + 人类一眼能看懂时间顺序。两次跑撞同一秒的概率低，撞了 `save_run` 也只是覆盖（没人会 1s 内手动跑两次同一 eval）。

如果未来 `batch_runner` 集成进来要并发跑多个 eval，再加随机后缀（`2026-05-06T16-32-33Z-a1b2`）。

### 6.5 为什么不用 SQLite 存 run？

`~/.phalanx/state.db` 是上游 SessionDB，schema 已经够紧。eval run 数据按 run-dir 散在文件系统：

- 直接 `cat records.json | jq` 调试方便
- 单 run 删除/归档是 `rm -rf <run_id>` 而不是写 SQL
- baseline 跨机器分享只要 tarball 一个目录
- 未来 `agent.curator` 跑 trajectory clustering 时直接读 records.json，不用 wrap DB 接口

代价：`list_runs` 是 O(n) 扫目录而不是 SELECT，但 n 是手动周跑的次数（一年 ~50），不是问题。

### 6.6 为什么 `_compute_cost_usd` 这么多 try/except？

`agent.usage_pricing` 牵涉模型元数据下载（`fetch_model_metadata`）、网络（OpenRouter 价格 API）、SDK 版本差异（不同 OpenAI / Anthropic 版本的 usage 字段）。任何一处崩 → eval 失去 50% 价值。

设计原则：**eval 永远跑得完。**pricing 缺数 → `(0.0, "unknown")`，模型字段拼错 → `(0.0, "unknown")`，CanonicalUsage 字段格式错 → `(0.0, "unknown")`。`unknown` 在 report 里有清楚的 `?` 标记，让人不会误以为"真 0 元"。

## 7. 已知限制 & 后续 wave 候选

§2.8.a 落地后 phalanx **第一次具备改动可衡量的能力**，但还有几条短板，按 ROI 列：

| 限制 | 后续 wave |
|---|---|
| `file_state` 测试会污染仓库工作树（artifact 写到 `os.getcwd()`） | wave 5 候选：per-task tmp 沙箱 |
| Verifier 全是规则匹配，没 LLM-as-judge | §2.8.c delegate 接通后可加 `llm_judge` verifier |
| baseline 只能 diff "verdict / token / cost"——agent 思路变化（trajectory_summary）没参与 diff | wave 6 候选：把 trajectory_summary 也对比，列出 diverged turn |
| 真 model eval 没自动化（手动周跑） | 引入 cron + 通知（`§2.8.c+ cron` 期回填） |
| 没有"task 难度"维度——一个新 task 加进来不知道其他模型在它上的 baseline | wave 7 候选：跨模型的 task baseline matrix |
| `args_subset` 只支持严格相等 | 真有需求时加 `args_match` schema |

§2.8.b memory 落地后必跑 `hermes eval --baseline <pre-memory>` 验证"memory 让 agent 更聪明 / 还是更慢"——这是 §2.8.a 存在的全部理由。

## 8. 操作指引

### 8.1 给 task 加新 verifier 类型

```python
# hermes_cli/eval.py
@register_verifier("my_kind")
def _verify_my_kind(task: GoldenTask, record: RunRecord) -> VerifierResult:
    expected = task.expected.get("...")
    if not expected:
        return VerifierResult(Verdict.ERROR, "my_kind: ...")
    # 走 record.final_response / record.tool_calls / 文件系统 / etc.
    if condition_passes:
        return VerifierResult(Verdict.PASS, "human-readable reason")
    return VerifierResult(Verdict.FAIL, "what went wrong")
```

写完加单测（参见 `tests/test_eval.py` wave 3 verifier 测试段）：每条 verifier 至少四个 case——happy path、缺必填、不满足、edge case。

### 8.2 加新 golden task

`tests/golden/<id>.yaml`：

```yaml
task_id: my_new_task
category: file
description: |
  一句话说"这个 task 在测什么"——给后人 grep 用
prompt: "..."
verifier_type: tool_called
expected:
  tool: read_file
  args_subset:
    path: my_file.py
```

`hermes eval list` 立刻看到。`hermes eval --task my_new_task` 单独跑一次验证 schema 正确。

### 8.3 开发自测

```bash
pytest tests/test_eval.py tests/test_eval_ci_smoke.py -v
hermes eval list                  # 10 个 task 应当列出
hermes eval --task smoke_oneshot  # 单 task smoke（需要 API key）
```

### 8.4 周跑流程

```bash
# 周一上午
export OPENAI_API_KEY=sk-...
hermes eval                                          # 跑全套，落基线
hermes eval list --runs | head -5                    # 拿到 run_id

# 周中改完某个子系统（如 §2.8.b memory）
hermes eval --baseline 2026-05-06T16-32-33Z --diff   # 看回归
```

如果 diff 里出现 `PASS → FAIL` → 立即 stop，要么改回，要么加 task / 调 prompt 重新校准 baseline。`tokens +N` 大幅增加（>20%）也是回归信号——agent 多烧 token 表示走了更长的工具链 / 更冗的 prompt。

## 9. 跟上游对照

`hermes-agent` 上游有 `mini_swe_runner.py`（SWE-bench mini）、`batch_runner.py`（trajectory 数据生成）、`tinker-atropos/`（RL framework）。phalanx §2.8.a 是**前面三者的前置**——所有三者最终都要 reward signal，而 reward signal 的源头就是 verifier。

`mini_swe_runner.py` 移植排在 §2.8.f RL 期，**会复用** `hermes_cli/eval.py` 的 `Verdict / RunRecord / save_run` 数据形状，verifier 可能新加 `swe_bench` 类型（patch + 跑测试套件 → PASS/FAIL）。

phalanx 不直接搬 hermes 的 eval 实现因为：

- hermes 的 eval 代码散在 `tinker-atropos/` 和 `cli.py` 里，跟 RL 训练流耦合
- phalanx 想要"先有可衡量、再有训练"——`hermes eval` 是独立产物，不绑定 atropos 的存在
- 数据形状（GoldenTask / RunRecord）要稳定先于上游集成，不然将来对齐很麻烦

## 10. 验收

```bash
pytest tests/                                                # 416 passed + 1 skipped
ruff check hermes_cli/ run_agent.py tests/                   # All checks passed
python -m hermes_cli eval list                               # 10 个 task
python -m hermes_cli eval list --runs                        # 列已存档 run（首次空）
python -m hermes_cli eval run --help                         # 三个新 flag 显示
python -m hermes_cli eval --task smoke_oneshot --no-save     # 真 model 单 task（需 API key）
```

第二次跑 + diff：

```bash
python -m hermes_cli eval                                    # 落第一次 baseline
python -m hermes_cli eval list --runs                        # 拿 run_id（如 2026-05-06T16-32-33Z）
python -m hermes_cli eval --baseline 2026-05-06T16-32-33Z    # 报告末尾追加 diff
python -m hermes_cli eval --baseline 2026-05-06T16-32-33Z --diff   # 只看 diff
```

## 11. 参考实现

| 上游文件 | phalanx 文件 | 关系 |
|---|---|---|
| `tinker-atropos/.../eval_*.py` | `hermes_cli/eval.py` | 概念借鉴，代码独立写（不耦合 RL） |
| `agent/usage_pricing.py` | 同名 | wave 3 通过 `normalize_usage` + `estimate_usage_cost` 接入 |
| `batch_runner.py` | 暂未移植 | §2.8.f 接 RL 时再移植；`save_run` 数据形状已对齐 |
| `mini_swe_runner.py` | 暂未移植 | §2.8.f 期补，会加 `swe_bench` verifier 类型 |
