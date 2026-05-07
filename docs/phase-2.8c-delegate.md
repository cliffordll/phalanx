# Phase 2.8.c 设计文档 — Delegate / Sub-agent（Critic + Reviewer + Async Auxiliary）

> **状态：未实现 / Planning Doc**。本文是 §2.8.c 落地前的设计共识——文档先行，避免动手时来回返工。所有 wave 标 🚧。

> 配套阅读：
> - [`MIGRATION_PLAN.md`](MIGRATION_PLAN.md) §2.8.c+ — 计划层面的子系统清单
> - [`agent-self-evolution.md`](agent-self-evolution.md) §2.2 (反思) §2.8 (元控制) §4 (落地顺序) — 战略地图
> - [`phase-2.8a-evaluation.md`](phase-2.8a-evaluation.md) — 评估闭环（§2.8.c 改完用 `phalanx eval --baseline X --diff` 验证 critic 是否真让成功率上升的入口）
> - [`phase-2.8b-memory-context.md`](phase-2.8b-memory-context.md) — memory 子系统（critic 子 agent 看 session 历史的载体）
> - [`run-loop-vs-upstream.md`](run-loop-vs-upstream.md) — `_invoke_tool` 已为 delegate 留了占位

本文记录 **§2.8.c 把"agent 派子 agent"装到 phalanx 的过程**——`tools/delegate_tool.py` 全新（主 agent 通过工具调用孵化子 AIAgent 跑独立子任务）+ critic/reviewer 角色化（子 agent 当审稿人）+ `agent/auxiliary_client.py` 异步 surface 真实化（Phase-2.2 / 2.8.b 留的 async stub 升真，让 delegate 子 agent 流式向主 agent 回吐）+ 集成测试 + 5 个 delegate 类目 golden task。

§2.8.c 的存在前提是 §2.8.b memory（critic 需要看 session 历史）+ §2.8.a evaluation（critic 是否有效要可量化）。两者都已落地，当前推荐下一站。

## 0. 范围与 wave 划分

| Wave | 内容 | 估行 | 状态 |
|---|---|---|---|
| 1 | `tools/delegate_tool.py` 全新——`delegate_task(task_description, role?, max_iterations_subagent?, share_memory?)` 工具：spawns 一个新 `AIAgent`（共享 `IterationBudget`，避免子 fan-out 烧 budget），跑完返回 `{final_response, tool_calls, usage_totals, stop_reason}`。`delegation_depth` 计数防递归（默认上限 2）。注册到 `tools/registry.py`。30 个新单测 | ~250 | 🚧 |
| 2 | critic / reviewer 角色化——`delegate_task` 接受 `role="critic"`/`"planner"`/`"executor"` 三档，每档对应一个不同的 sub-agent system prompt（critic 强调列问题 / 评 1-5 分；planner 强调分解 + 步骤；executor 是默认执行）。可选 `subject_artifact: str` 传"被审对象"。CLI `phalanx oneshot --critic-model <name> "<task>"` 主跑 + critic 兜底。20 个新单测 | ~200 | 🚧 |
| 3 | `agent/auxiliary_client.py` 异步 surface 真实化——`async_call_llm` / `get_async_text_auxiliary_client` 不再 stub，接入真实 AsyncOpenAI；让 delegate 子 agent 的 stream callback 能 async 地往主 agent 回吐 token；`tools/web_tools.py` 改成消费真实 async path（之前 fallback 到 truncated raw content）。25 个新单测 | ~300 | 🚧 |
| 4 | 集成 + golden tasks——`AIAgent` 默认把 `delegate` 加进 tool registry；`tests/test_delegate_integration.py` 三 hook 集成（memory + reference + delegate）；5 个 delegate 类目 golden task：`delegate_basic_split` / `delegate_critic_catches_bug` / `delegate_planner_decomposes` / `delegate_recursion_capped` / `delegate_shared_budget`。`docs/MIGRATION_PLAN.md` §2.8.c row 标 ✅ | ~250 | 🚧 |

§2.8.c 落地后：

- 主 agent 在主循环中调 `delegate_task("review the patch I just made", role="critic")` 工具，主 agent 拿到一份审稿意见再决定是否 commit
- `phalanx oneshot --critic-model gpt-4o-mini "..."` 主 agent 跑完后用便宜 critic 模型自动复审一遍
- `phalanx eval --baseline pre-delegate --diff` 验证"加 critic 后通过率到底升了还是降了，token 多花了多少"
- `delegation_depth` 防止子 agent 递归调 delegate 烧出整个 budget

## 1. 闭环图与组件

```
                     主 AIAgent.run_conversation
                              │
                              │  tool_calls 内含 delegate_task(...)
                              ▼
                     ┌──────────────────────┐
                     │  delegate_tool.run() │
                     │   (registry dispatch)│
                     └─────────┬────────────┘
                               │
                               │  factory(parent=self, ...)
                               ▼
                     ┌──────────────────────────────┐
                     │  Sub-AIAgent                 │
                     │   • 共享 parent.iteration_   │
                     │     budget                   │
                     │   • parent_session_id =      │
                     │     parent.session_id        │
                     │   • 角色化 system prompt     │
                     │     （critic / planner / …） │
                     │   • 可选 share_memory=true   │
                     │     用同一个 SessionDB       │
                     └─────────┬────────────────────┘
                               │
                               ▼
                       run_conversation(...)
                       ↓
                 final_response / tool_calls / usage_totals
                               │
                               ▼
                     ┌──────────────────────┐
                     │  delegate_tool.run() │
                     │   返回结构化 result   │
                     └─────────┬────────────┘
                               │
                               ▼
                     主 agent 看到 tool result
                     继续主 loop
```

**关键不变量**：

- 子 agent **必须**共享父 IterationBudget。否则一个 deep delegate chain 能烧出整个 max_iterations × N 的预算
- 子 session_id 走 `parent_session_id` 链，`session show` 能完整看到 fan-out 树
- 子 agent 的 `usage_totals` 累计回父——cost / token 报告不会"漏算"子 agent 的消耗
- `delegation_depth` 计数硬上限——不靠 budget 本身防递归（budget 可能很大），而是显式 N=2 限制

## 2. Wave 1 — Delegate Tool 骨架（~250 行）🚧

### 2.1 工具签名

```python
@tool_def
def delegate_task(
    task_description: str,
    role: str = "executor",          # executor / critic / planner
    max_iterations_subagent: int = 20,
    share_memory: bool = True,        # 共享同一个 SessionDB
    subject_artifact: Optional[str] = None,  # 可选，"被审对象"
) -> Dict[str, Any]:
    """
    Delegate a sub-task to a fresh sub-agent.  Sub-agent shares the
    parent's IterationBudget so total cost is bounded.

    Returns:
      {
        "final_response": str,       # sub-agent's last assistant text
        "tool_calls": List[Dict],    # tools the sub-agent invoked
        "usage_totals": Dict,        # input/output tokens, cache, ...
        "stop_reason": str,          # completed / budget_exhausted / ...
        "iterations_used": int,
        "sub_session_id": str,       # for `phalanx session show <id>`
      }
    """
```

### 2.2 IterationBudget 共享

`tools/delegate_tool.py` 拿到 parent agent 的引用（通过 `tool_call_context`，wave 3 of §2.1 已经预留），构造子 agent 时传：

```python
sub_agent = AIAgent(
    model=parent.model,             # 同模型，除非 critic-model 显式覆盖
    base_url=parent._base_url,
    api_key=parent._api_key,
    iteration_budget=parent.iteration_budget,  # ← 共享对象，不是 copy
    session_db=parent._session_db if share_memory else None,
    parent_session_id=parent.session_id,
    max_iterations=max_iterations_subagent,
)
```

`max_iterations_subagent` 只是子 agent 自己的 hard cap，**真正生效的是父子共享 budget 的剩余值**——`min(sub_max, parent.budget.remaining)`。

### 2.3 递归深度计数

`AIAgent` 加 `self.delegation_depth = 0` 字段；`delegate_task` 工具构造子 agent 前 `parent.delegation_depth + 1`，传给子。子 agent 自己再调 `delegate_task` 检查 depth ≥ `_DELEGATION_DEPTH_MAX`（默认 2）→ tool 直接返回 error 而非再递归。

```python
_DELEGATION_DEPTH_MAX = 2

def delegate_task(...) -> Dict:
    parent = tool_call_context["agent"]
    if parent.delegation_depth >= _DELEGATION_DEPTH_MAX:
        return tool_error(
            f"delegate: max depth {_DELEGATION_DEPTH_MAX} reached "
            f"({parent.delegation_depth} already in chain)"
        )
    # ...
```

**为什么 depth=2 而不是更深**：上游 hermes 默认 3，但 phalanx 在评估闭环 baseline 之前不应该 fan-out 太深。两层够支持"主 agent → 派一个 critic"和"主 agent → 派 planner → planner 派 executor"，再深通常是设计错误。后续可调高。

### 2.4 失败与异常

子 agent 的所有失败都被 wrap 成 tool result 返回主 agent，**不**抛异常打断主 loop：

| 子 agent 状态 | tool result |
|---|---|
| 正常完成 | `{"stop_reason": "completed", "final_response": "...", ...}` |
| budget 耗尽 | `{"stop_reason": "budget_exhausted", ...}` |
| API 错误 | `{"error": "API call failed: ...", "stop_reason": "api_error:...", ...}` |
| 子 agent 抛异常 | `tool_error("delegate: sub-agent crashed: <type>: <msg>")` |
| depth 超限 | `tool_error("delegate: max depth ...")` |

主 agent 拿到 tool result 后自己决定是 retry / 改写任务 / 放弃——**不**自动重试 delegate（怕循环）。

### 2.5 持久化

子 session 通过 `parent_session_id` 链接到父；`session show <parent>` 已经支持显示子链路（§2.5 wave 4）。`phalanx session list` 默认隐藏子（`include_children=False`），保持顶层视图清爽。

## 3. Wave 2 — Critic / Reviewer 角色化（~200 行）🚧

### 3.1 三档 role + system prompt

```python
_ROLE_SYSTEM_PROMPTS = {
    "executor": "",  # 复用 build_system_prompt 默认即可
    "critic": (
        "You are a senior code reviewer.  You will be given a task and "
        "an artifact (the work to review).  Output a numbered list of "
        "issues found, ranked by severity (1=blocker, 5=nitpick).  "
        "End with one line: 'VERDICT: ACCEPT|REJECT|REVISE'.  Be "
        "concrete: cite file paths and line numbers, propose fixes."
    ),
    "planner": (
        "You decompose tasks into a numbered step plan.  Each step "
        "must be ≤1 sentence and produce a checkable artifact "
        "(file changed / test passed / output produced).  End with "
        "an estimate of total turns needed.  Don't execute, only plan."
    ),
}
```

### 3.2 `subject_artifact` 字段

被审对象作为 system prompt 后的第二层注入：

```python
if role == "critic" and subject_artifact:
    sub_agent_system_prompt += (
        "\n\nThe artifact under review:\n\n"
        f"<artifact>\n{subject_artifact}\n</artifact>"
    )
```

`subject_artifact` 通常是主 agent 刚生成的 patch / diff / 文档。复用 `<reference>`/`<artifact>` 一类 XML 块约定（与 §2.8.b wave 3 reference resolver 风格一致）。

### 3.3 CLI surface — `--critic-model`

```bash
phalanx oneshot --critic-model gpt-4o-mini "Refactor src/foo.py to use list comp"
```

主 agent 跑完，phalanx 自动 spawn 一个 critic sub-agent（`role="critic"`，model 用 `--critic-model`），把主 agent 的 `final_response` + 触动的 patch 传进 `subject_artifact`。critic 的 verdict 打到 stdout 末尾：

```
[main agent output ...]

──────────────────────────────────────
[critic gpt-4o-mini]:
1. (blocker) src/foo.py:45 — list comp drops the early-return guard
2. (nitpick) variable name 'x' could be 'item'
VERDICT: REVISE
──────────────────────────────────────
```

主 agent 进程 exit code 沿用主 agent，critic 不参与 exit code 决策。critic 只是辅助信号。

### 3.4 在 REPL 内

slash 命令 `/critic [last|<turn>]` 让用户在 REPL 跑完一轮后手动触发 critic 复审上一轮（或某轮）的输出。`last` 是默认值。

## 4. Wave 3 — Auxiliary Client Async Surface（~300 行）🚧

### 4.1 现状回顾

`agent/auxiliary_client.py` 当前（§2.8.b wave 2 落地后）：

- `get_text_auxiliary_client(task, *, main_runtime)` ✅ 同步生产路径
- `summarize_messages(client, model, messages)` ✅ 同步
- `extract_content_or_reasoning(response)` ✅
- `get_async_text_auxiliary_client(...)` 🚧 stub，永远返回 `(None, None)`
- `async_call_llm(**kwargs)` 🚧 stub，调到就抛 `RuntimeError`

`tools/web_tools.py` 因此走"无 auxiliary 走 truncated raw content"路径——大网页返回 ~5KB 截断而不是 LLM 摘要后的精简 markdown。

### 4.2 Wave 3 实化目标

```python
async def get_async_text_auxiliary_client(
    task: str = "",
    *, main_runtime: Optional[Mapping] = None,
) -> Tuple[Optional[Any], Optional[str]]:
    """Async 版 get_text_auxiliary_client；配置解析逻辑共用。
    返回 AsyncOpenAI client + model id；任意失败返回 (None, None)。"""

async def async_call_llm(
    *, client: AsyncOpenAI, model: str, messages: List[Dict],
    max_tokens: int = 4096, stream: bool = False,
    extra_body: Optional[Dict] = None,
) -> Any:
    """单次 chat.completions.create（async），可选 stream。"""

async def async_summarize_messages(
    client, model, messages, *, focus_topic=None,
) -> Optional[str]:
    """summarize_messages 的 async 版，让 web_tools 在事件循环里跑。"""
```

实现优先级：

1. **复用同步路径的配置解析**——`_resolve_auxiliary_config` / `_apply_main_runtime_fallback` / `_apply_env_fallback` 不区分 sync/async，提取成共用 helper
2. **AsyncOpenAI 实例化**——OpenAI SDK 的 `AsyncOpenAI` class 跟 `OpenAI` 几乎对称
3. **Stream 处理**——`stream=True` 时返回一个 async generator，每个 chunk 是 `{type, delta}`；调用方 `async for chunk in resp: ...`

### 4.3 Delegate 用 async 干什么

子 agent 现在跑完才返回（同步 wave 1 设计）。引入 async 后，可以让子 agent **流式**回吐 token 给主 agent——主 agent 的 stream_callback 把子 agent 的输出实时渲染到 stdout，体验上像"嵌套对话"。

但这需要 `delegate_task` 支持流式返回，意味着 tool 协议要扩展（当前 tool result 是单个 dict）。Wave 3 **暂不**改 tool 协议——只把 async 客户端铺好，让 web_tools / 未来的流式 delegate 都能用。

### 4.4 web_tools 升级

`tools/web_tools.py` 在 §2.4 wave 4 移植时设计上就预留了 LLM-summary 路径：fetch 网页 → 大于阈值 → 调 auxiliary 摘要成 markdown → 返回主 agent。当前因 auxiliary async stub 全程 fallback 到 truncated raw。Wave 3 接通后：

```python
async def fetch_url_with_summary(url, *, max_chars=8000):
    raw = await fetch_url_raw(url)
    if len(raw) > max_chars:
        client, model = await get_async_text_auxiliary_client("web_summary")
        if client is not None:
            return await async_summarize_messages(
                client, model,
                [{"role": "user", "content": raw}],
                focus_topic=f"web page at {url}",
            )
    return raw[:max_chars]  # 仍保留 fallback
```

代价：网页类工具调用平均延迟 +1-3 秒（auxiliary 调一次）。收益：主 agent 看到的是结构化摘要而非 5KB 截断 HTML。

## 5. Wave 4 — 集成 + Golden Tasks（~250 行）🚧

### 5.1 AIAgent 默认注册 delegate 工具

```python
# tools/registry.py 加默认导入
from tools.delegate_tool import delegate_task  # noqa: registers tool
```

`AIAgent.__init__` 不需要改——registry 自动发现。但加 `enabled_toolsets` 时 `delegate` 默认在内（除非用户显式 disable）。

### 5.2 三 Hook + Delegate 集成回归

`tests/test_delegate_integration.py` — 类似 wave 4 §2.8.b 的 `test_run_conversation_hooks.py`，但加 delegate：

| 测试 | 覆盖 |
|---|---|
| `test_delegate_inherits_memory_and_compression` | 父 agent 跑一阵进了压缩；调 delegate 起子 agent；子的 system prompt 含父注入的 memory；子的 messages list 不会因父被压缩而丢失上下文 |
| `test_delegate_shares_iteration_budget` | 父 max_iterations=10，跑掉 6；调 delegate(max_iterations_subagent=20)——子实际只能用 4（剩余 budget），不会扩到 20 |
| `test_delegate_depth_capped_at_2` | 父调 delegate（depth=1），子又调 delegate（depth=2 ≥ MAX）→ 第三次 tool 返回 error，主 loop 不崩 |

### 5.3 5 个 Delegate 类目 Golden Task

| task_id | verifier | 验什么 |
|---|---|---|
| `delegate_basic_split` | tool_called `delegate_task` | 主 agent 接受"先 plan 再 execute"提示，应该调 delegate 起 planner |
| `delegate_critic_catches_bug` | exact_match | 给主 agent 一个有 bug 的 patch 然后 `--critic-model`；critic 输出含 "VERDICT: REJECT" 或 "VERDICT: REVISE" 之一 |
| `delegate_planner_decomposes` | exact_match | role=planner 子 agent 输出 ≥3 步骤（含 "1." "2." "3." token） |
| `delegate_recursion_capped` | tool_called + args_subset | 子 agent 调 delegate 试图深入第三层；tool result 含 "max depth ... reached" |
| `delegate_shared_budget` | exact_match | 给极小 max_iterations，verify 主 agent 收到 sub stop_reason="budget_exhausted" 且自己仍能产生 final_response |

memory 和 session 类的 golden 仍然受 §2.8.b wave 4 的限制（缺 `setup:` hook）——继续延迟。

### 5.4 Eval baseline diff

```bash
# §2.8.b 完成时已落基线
phalanx eval --baseline 2026-05-XX --diff   # 在 §2.8.c 改完 wave 4 后跑
```

期望 diff:

- **新增 5 个 delegate_* task**——passes 一升一降不等
- **既有 task 的 token / cost 可能略升**——`delegate` 工具加进 registry 占了 schema slot 几百 token；不应有 PASS→FAIL
- **如果有既有 task PASS→FAIL**——立即 stop，要么改 delegate schema 描述更紧凑，要么调 default registry 让 delegate opt-in

## 6. 设计决策与权衡

### 6.1 为什么 delegate 是 *工具* 而不是 *主 loop hook*？

主 loop 加 delegate hook（如 turn N 自动 fan-out critic）的方案被否：

- **可控性差**：用户看不到 delegate 何时触发，调试困难
- **token 不可预期**：每个 turn 都 fan-out 让 cost 暴涨
- **agent 自己决定**才有意义：主 agent 看到自己的 patch 才决定该不该叫 critic 来审；不该的场景就不叫，省钱

工具化 → 主 agent 在 prompt 引导下学会"这种任务我应该派 critic"。这才是 self-evolution 的形状。

### 6.2 为什么共享 IterationBudget 而不是给子 agent 独立 budget？

独立 budget 看似干净（"父 90 子 90 互不影响"），但：

- 父 agent 调 5 个子 agent → 实际可烧 5 × 90 = 450 iterations，main_iterations=90 等于谎言
- 用户配的 max_iterations=90 是**总成本上限**意图，不是"主 agent 上限"

共享 budget 让 max_iterations 保持"总轮数"语义。代价：父 agent 自己跑慢的时候子 agent 可用的 budget 就少——这是 feature，不是 bug：父跑得慢说明任务复杂，子也别太放纵。

### 6.3 为什么 critic 跟 executor 不用同一个 model 是默认？

`--critic-model` 默认空（与主模型同）。但在文档里**强烈建议**用更便宜模型当 critic：

- gpt-4o 主跑 + gpt-4o-mini critic：成本 +5%，覆盖 80% 的 silly 错误
- 同模型 critic 大概率"附和"主 agent（已知 LLM 的心理倾向）

但默认不强制——零配置安装就能 work。`docs/MIGRATION_PLAN.md` 加 §2.8.c 章节会列推荐 model 配对。

### 6.4 为什么 critic 输出强制 "VERDICT: ACCEPT|REJECT|REVISE"？

无结构化 critic 输出 = 无法编程消费。主 agent 自己解析自然语言判断 critic 同意没同意，不可靠。强制 VERDICT 一行让上层（CLI / 主 agent）可 grep 判断后续动作：

```
VERDICT: ACCEPT  → 提交
VERDICT: REJECT  → 重做
VERDICT: REVISE  → 改某些点后重 critic
```

system prompt 强制约定 + parser 端 fallback（找不到 VERDICT 时报 ERROR 而非默认 ACCEPT）—— fail-loud。

### 6.5 为什么 `delegation_depth=2` 而不是更高？

每多一层：

- token 消耗指数级（depth=3 已经 4-5 倍主 agent token）
- 调试困难（树状 trajectory 看不过来）
- 子→孙→曾孙的失败几率累乘

phalanx 在 §2.8.c 是"第一次有 sub-agent"。先窄定 depth=2 跑两个月看实际场景，再考虑放宽。上游 hermes 默认 3，可以参考但不强求一致。

### 6.6 为什么 wave 3 把 web_tools 升级也算进来？

web_tools 是当前 phalanx **唯一**的 auxiliary 消费者。wave 3 落 async surface 不接通它，等于做了个空货架。让 web_tools 升级一并落，验证 async path 跑得通——之后 delegate 流式或别的子系统再加，async 已经验证过。

### 6.7 为什么不在 wave 3 给 delegate 加流式？

流式 delegate 需要扩 tool 协议（tool result 不再是单 dict）。这是个**结构性**改动，影响所有现有工具——超出 §2.8.c "新功能" 边界。

留到未来 wave（可能 §2.8.f 或更晚）：先证明 sync delegate 价值大，再投资协议改造。

### 6.8 为什么 curator 不在 §2.8.c？

`agent/curator.py` 上游是"周期性扫所有 skills 淘汰冗余"的 background worker。phalanx 现在还没 skills 系统（§2.8.e 才做）。curator 没 skill 可 curate。

放到 §2.8.e skills 期一起做更合理——skills 系统的设计会决定 curator 接什么数据。

## 7. 已知风险 & 后续 wave 候选

| 风险 / 限制 | 缓解 / 后续 wave |
|---|---|
| 子 agent 与父共享 SessionDB，并发写盘可能竞态 | SessionDB 已有 WAL + 重试（§2.5），但 delegate 真上线压一压再说；wave 5 候选：per-subagent 临时 db 目录 |
| critic verdict 是 LLM 输出，可能 hallucinate VERDICT 段不存在 | parser fail-loud + 提供 `--no-critic` 跳过；长期看引入 LLM-judge eval 验 critic 自身质量 |
| delegate 工具的 schema 描述吃 token，每个主 agent turn 都付 | 测 delta，必要时让 delegate 工具 opt-in（非默认注册） |
| 父子 usage_totals 加总有边角情况（重叠的 cache_read 部分）| `_accumulate_usage` 已 normalize；wave 4 加专门测试 |
| sub-agent 跑一半被父 interrupt，子 session 状态可能 inconsistent | wave 1 加 `interrupt_sub_agent_on_parent_interrupt` flag（默认 True） |
| async auxiliary 用了 AsyncOpenAI，但 phalanx 自身 main loop 仍是 sync | 仅 web_tools 跑 async；主 loop 不动；后续 §2.9 / §3.0 整体 async 化时再统一 |

§2.8.c 落地后必跑 `phalanx eval --baseline pre-delegate` 验证：

- 主 agent 在新 task（delegate_*）上有合理表现
- 既有 task 没 PASS→FAIL（delegate schema 不挤掉别人）
- token / cost delta 在合理范围（< 10% 增长）

如果 critic 加上去既有 task 平均 cost 增 50%，那是过度——应该让 critic 默认 opt-in（不加进 registry，靠用户 `--critic-model` 显式触发）。

## 8. 操作指引

### 8.1 加新 role

```python
# tools/delegate_tool.py
_ROLE_SYSTEM_PROMPTS["my_role"] = (
    "You are a ...  Your output should be ...  End with a single "
    "line summarising your conclusion."
)
```

写完加：

- 单测 — happy path / VERDICT 缺失 fallback / role 拼写错走 executor 默认
- 一个 golden task — 让真模型跑一次，确认 system prompt 引导有效

### 8.2 调 depth 上限

`tools/delegate_tool.py::_DELEGATION_DEPTH_MAX`。改完跑：

```bash
phalanx eval --task delegate_recursion_capped --no-save
```

确认仍然在新 cap 上正确触发 error。

### 8.3 调主 model + critic model 配对

`~/.phalanx/config.yaml`：

```yaml
critic:
  model: gpt-4o-mini       # 默认 critic 模型
  base_url: ""             # 空 → 复用主 base_url
  api_key: ""              # 空 → 复用主 api_key
```

`phalanx oneshot --critic-model X` 仍可单次覆盖。

### 8.4 跑 eval 验证 §2.8.c 改动

```bash
# 改 critic system prompt 之前
phalanx eval > /tmp/before.txt
phalanx eval list --runs | head -1   # 拿 run_id

# 改完之后
phalanx eval --baseline <run_id> --diff
```

着重看：

- `delegate_critic_catches_bug` 仍 PASS（critic 真的找到 bug）
- 既有 task 平均 token 没暴涨

## 9. 跟上游对照

| 上游文件 | phalanx 文件 | 关系 |
|---|---|---|
| `tools/delegate_tool.py` | 同名（精简版） | 上游有 sandbox / approval flow / artifact passing 高级形态；phalanx 先做基础 spawn + role + budget 共享 |
| `agent/auxiliary_client.py`（~3914 行） | 同名（继续扩） | §2.8.b wave 2 已落同步；§2.8.c wave 3 落异步；credential pool / Nous 路由 / OAuth 可能始终留 stub |
| `agent/curator.py` | 待 §2.8.e / §2.8.d 移植 | 跟 skills 系统耦合，§2.8.c 不动 |
| `tools/skill_*` `tools/skills_hub.py` | §2.8.e | curator / skill discovery 一并做 |

phalanx **不**直接搬上游 delegate 实现因为：

- 上游 delegate 跟 sandbox / approval / GitHub PR creation 这些高级面强耦合
- phalanx 想要"先有可用、再有花哨"——基础 spawn + role 三档够大多数 use case
- 数据形状（sub-agent return shape）要稳定先于上游集成，将来对齐手动改 schema

## 10. 验收（本期落地后）

```bash
pytest tests/                                                # 期望 +75 (3 wave 测试) → ~610 passed
ruff check tools/delegate_tool.py agent/auxiliary_client.py tests/  # All checks passed

# 基础 delegate 单测端到端
phalanx tools list | grep delegate                           # delegate_task 在列
phalanx tools schema delegate_task                           # 看 schema

# 真 model 跑（需 API key）
phalanx oneshot "Plan a refactor of agent/__init__.py: list 3 steps."
# 期望 agent 调 delegate_task(role="planner") 拿到 step list

# Critic 端到端
phalanx oneshot --critic-model gpt-4o-mini "Add error handling to function f"
# 期望主 agent 输出后跟 critic 段，含 "VERDICT:" 一行

# Eval baseline diff
phalanx eval list --runs | head -1                           # 拿 pre-delegate 基线 run_id
phalanx eval --baseline <run_id> --diff
# 期望：5 个新 delegate_* task 出现，既有 task 不退化
```

## 11. 参考实现 / 文件清单

| 文件 | 状态 | 关系 |
|---|---|---|
| `tools/delegate_tool.py` | 全新（wave 1） | 主入口 |
| `tools/registry.py` | 改（wave 4） | 默认导入 delegate_tool |
| `agent/auxiliary_client.py` | 扩（wave 3） | 同步 surface 已有，加 async |
| `tools/web_tools.py` | 改（wave 3） | 接通真实 async auxiliary path |
| `run_agent.py` | 微改 | `AIAgent` 加 `delegation_depth` 字段 |
| `hermes_cli/main.py` | 改（wave 2） | `--critic-model` 参数 |
| `cli.py` + `hermes_cli/commands.py` | 改（wave 2） | `/critic` REPL slash |
| `tests/test_delegate_tool.py` | 全新（wave 1, ~30 用例） | 工具单测 |
| `tests/test_delegate_critic.py` | 全新（wave 2, ~20 用例） | role 化测试 |
| `tests/test_async_auxiliary_client.py` | 全新（wave 3, ~25 用例） | async surface |
| `tests/test_delegate_integration.py` | 全新（wave 4, ~3 用例） | 三 hook + delegate 集成 |
| `tests/golden/delegate_*.yaml` | 全新（wave 4, 5 个） | golden 类目 |
| `docs/MIGRATION_PLAN.md` | 改（wave 4） | §2.8.c 行 ✅ |
| `docs/agent-self-evolution.md` | 改（wave 4） | §2.2 / §2.8 / §3 / §4 / §8 同步进度 |

## 12. 时间线估算

| 工作日 | 内容 |
|---|---|
| Day 1-2 | wave 1（delegate_tool 骨架 + IterationBudget 共享 + recursion 计数 + 30 单测） |
| Day 3 | wave 2（critic / planner role + `--critic-model` CLI + 20 单测） |
| Day 4-5 | wave 3（async auxiliary surface + web_tools 升级 + 25 单测） |
| Day 6 | wave 4（集成测试 + 5 golden + plan / self-evolution doc 同步 + eval baseline diff） |
| Day 7 | buffer / 问题排查 |

总计 5-7 工作日。乐观 5 天，悲观 7 天，与 `agent-self-evolution.md` §8 估算一致。

---

**§2.8.c 落地后 phalanx 闭环图里的"反思"段第一次有了真实组件**——经验流（§2.8.b memory）→ 反思（§2.8.c critic）→ 评估（§2.8.a eval）三段半全部接通，再补一个"更新"机制（skill 创建 / prompt 改写 / RL）就闭环。这是为什么 §2.8.c 的优先级排在 §2.8.e skills 之前。
