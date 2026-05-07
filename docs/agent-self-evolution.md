# 智能体自主进化 — 技术地图与落地路径

> 本文是 phalanx 关于 **agent self-evolution / autonomous improvement** 的战略性文档。不绑定具体 phase，描述的是"agent 如何在使用过程中变得更好"这件事在系统层面需要哪些组件、它们如何协同、以及 phalanx 当前的差距。
>
> 配套阅读：
> - [`MIGRATION_PLAN.md`](MIGRATION_PLAN.md) §2.8 — 拆分后的落地路径（含 1-2 周近期计划）
> - [`ARCHITECTURE.md`](ARCHITECTURE.md) — 当前已有子系统全景
> - [`run-loop-vs-upstream.md`](run-loop-vs-upstream.md) — 主循环跟上游的差距清单（很多自主进化点的接入位置）

## 0. 我们说的"自主进化"是什么

"自主进化"在不同上下文里指完全不同的事情。phalanx 范围内**只**讨论以下 5 件事，研究范畴的"AGI 自我改写"不在内：

1. **跨 session 学习**——agent 记得过去发生了什么，下次不重蹈覆辙
2. **运行时反思**——单 turn / 单 task 内 agent 评估自己的输出并修正
3. **能力固化**——重复模式自动沉淀成 skill / tool / prompt template
4. **策略优化**——基于历史 reward 自动调 prompt / 模型 / `reasoning_effort`
5. **权重级训练**（可选 / 远期）——trajectory 数据回流做 SFT / RL

**不在范围**：完全无人值守的 self-modifying code、跨 user 共享的群体学习、链上去中心化训练等。

## 1. 自主进化的闭环

```
                     ┌──────────────────────┐
        所有 turn   →│  经验流              │
                     │  trajectory + memory │
                     │  + sessions FTS      │
                     └─────────┬────────────┘
                               │
                               ▼
                     ┌──────────────────────┐
                     │  反思 / 评估          │
                     │  reflect + critic +  │
                     │  golden task baseline│
                     └─────────┬────────────┘
                               │
                               ▼
                     ┌──────────────────────┐
                     │  更新                 │
                     │  prompt rewrite /    │
                     │  skill creation /    │
                     │  policy / weight     │
                     └─────────┬────────────┘
                               │
                               ▼
                       回到经验流（在新 prompt /
                       新 skill 下继续运行）
```

**关键洞察**：四个环节缺任何一个，进化就成了开环——

- 没经验流 → 反思无数据
- 没反思 → 经验只能堆积不能蒸馏
- 没更新机制 → 反思的产物落不到下次运行
- **没评估** → 任何更新都是猜测，可能让 agent 变差却没人知道

phalanx 当前状态（截至 §2.8.c wave 4）:经验流 **95%** 就位（trajectory + sessions + long-term memory + `@reference` 显式引用 + 自动 context compression 全部到位）;评估闭环 **85%**（§2.8.a 落地,目前 20 个 golden task,含 reference + delegate 类目）;**反思 60%**（§2.8.c delegate / critic / planner role 化 + auxiliary async surface 真实化 + IterationBudget 共享 + 深度上限）;更新机制（skill / prompt 改写 / RL）仍为零。

## 2. 八大技术点

下面每条标注 **phalanx 状态**：✅ 已实现 / ◇ 部分（shim / 基础设施）/ ✗ 未引入。

### 2.1 经验流（experience accumulation）

| 子能力 | 作用 | phalanx |
|---|---|---:|
| `trajectory` 记录 | 每个 turn 的 prompt / tool_calls / tokens / 成败完整存盘——训练 / 反思 / debug 的源数据 | ✅ §2.3 wave 2 |
| `SessionDB` 持久化 | 跨进程、跨 session 的对话 / token / cost 历史 | ✅ §2.5 全部 5 wave |
| FTS5 全文索引 | 语义级会话检索（"上次我让 agent 改 css 时它怎么做的"） | ◇ schema 已建（§2.5），`search_messages` 函数缺 |
| 长期记忆（cross-session） | "用户偏好 / 项目知识"持久化，新 session 起头自动拉相关 chunk；FTS5 trigram + bm25 + pinned-first 排序；`memory.enabled` / `memory.retrieve_limit` 配置项；`phalanx memory list/show/search/add/delete/pin` CLI | ✅ §2.8.b wave 1 |
| Context 自动压缩 | `prompt_tokens > context_length × 0.7` 时 auxiliary LLM 把"最早 N 个 turn"摘要成 `[context-summary]` 合成消息；auxiliary 失败回退 oldest-pair pruning；增量压缩（之前 summary 不重复总结） | ✅ §2.8.b wave 2 |
| `subdirectory_hints` | 工作目录语义提示（"这是 React 项目，不要建议 Vue"） | ✅ §2.3 |
| `@reference` 解析 | `@file:` / `@diff[:<ref>]` / `@url:` / `@session:<id|prefix>` 显式引用，`<reference type=... key=...>...</reference>` block 附加到 user 消息；REPL `/ref show` + web `POST /api/references/resolve` | ✅ §2.8.b wave 3 |

### 2.2 反思与自我修正（reflection / self-correction）

| 模式 | 作用 |
|---|---|
| **Reflect-then-retry** | 工具失败 / 测试不过时，模型先生成 critique（评审），再基于 critique 重写计划。比裸重试有效得多 |
| **Critic agent** | 独立模型审主 agent 输出（用更便宜模型，对照 `auxiliary_client` 模式） |
| **Self-debugging via trajectory replay** | 失败 turn 触发 sub-agent 拿 trajectory 切片做归因 |
| **Verification by execution** | "测试用例必须真跑过" / "diff 必须 lint 过" 等硬约束 |

phalanx 起点：~~`agent/auxiliary_client.py` 是预留的 critic 入口（目前 89 行 shim）；`delegate_task` 工具一进来就能并行起 critic（§2.8.c）。~~ — ✅ §2.8.c 已落地，`tools/delegate_tool.py` + critic / planner role 化 + AsyncOpenAI 路径都在跑。详见 [`phase-2.8c-delegate.md`](phase-2.8c-delegate.md)。

### 2.3 工具与技能演化（capability evolution）

| 层 | 含义 | phalanx |
|---|---|---|
| **静态扩展** | 用户写新工具放 `tools/`，registry 自动加载 | ✅ §2.1 wave 3 |
| **Skills 系统** | `system_prompt + 受限 tool 子集 + 资源`三件套打包，按需 `/skill <name>` 切换 | ✗ §2.8 `skills 系统`期 |
| **Skill discovery** | 用 LLM 把 trajectory cluster 成"重复任务模式"，建议固化为 skill | ✗ 研究方向，依赖 trajectory + skills 系统 |
| **动态工具创建** | agent 识别"重复模式"→ 自动 codegen 新 tool 文件 → 注册 → 下次用 | ✗ 高风险，必须 sandbox + verifier + capability gating |
| **Curator** | 单独 agent 评估技能集，淘汰失效 / 合并冗余 | ✗ 上游 `agent/curator.py` |
| **Tool composition / macro** | 多 tool 组合成 macro，记忆"调 search 后必跟 read_file"等 pattern | ✗ 研究方向 |

实际起点：先把 §2.8 `skills 系统`移过来，再写一个 `propose_skill` 工具。

### 2.4 提示与策略调优（prompt / policy auto-tuning）

| 方法 | 含义 |
|---|---|
| **Prompt 自动改写** | DSPy / TextGrad 风格——agent 跑完 N 个任务，按 failure 模式改 system_prompt |
| **APE** | LLM 模板搜索，按 reward 排序最优提示 |
| **Bandit 选模型** | 多 provider 时按任务类型 + 历史成功率 + 价格自动选 |
| **CoT depth control** | 自动决定 `reasoning_effort`（hard 题用 high，简单题用 low） |
| **Auto temperature / top_p** | 同样按任务类别学最优采样参数 |

phalanx 起点：`agent/prompt_builder.py`（§2.3）已有结构化拼接；`agent/usage_pricing.py` + `model_metadata.py`（§2.3 wave 2）做选模型决策的数据基础已就绪。**差一个统一的 reward 信号源**——回到 §2.3 评估闭环。

### 2.5 强化学习 / 训练闭环（RL fine-tuning）

| 组件 | 作用 |
|---|---|
| **Trajectory → SFT 数据** | 把成功 turn 标 reward=1，监督微调让 base model "天然知道怎么用工具" |
| **Reward model** | 用 verifier 输出训判分模型，避免每次都跑真测试 |
| **Atropos / RL framework** | 在沙箱 env 跑 agent，收集 trajectory，做 PPO/GRPO；上游 `tinker-atropos/` |
| **Replay buffer** | 跨 session 储好失败案例，定期重放 |
| **Offline eval** | golden task set 跑 regression（防"训练后某类能力退化"） |

phalanx 起点：`tinker-atropos/` 是上游独立子项目（§2.8 `RL / training` 期才动）；`batch_runner.py` 是数据生成入口（§2.8 `batch / dataset gen`）。**离 RL 最近的小动作**：写个 `phalanx eval` 子命令跑 golden tasks——为后续 reward 提供基线。

### 2.6 评估闭环（evaluation harness）

| 工具 | 作用 |
|---|---|
| **Golden task set** | "agent 必须能跑通的 N 个 mini 任务"——每次模型 / 提示改完先跑一遍 |
| **SWE-bench / mini_swe_runner** | 真实 PR 修复任务集——上游 `mini_swe_runner.py`（§2.8） |
| **A/B compare runner** | 同一 prompt 跑两个版本 agent，diff trajectory + diff 最终答案 |
| **`hermes eval` CLI** | 离线跑 N 个 task → 出报告（成功率 / token 消耗 / 平均轮数） |

~~**没有评估闭环，前面五条都没基准——这是 phalanx 当前真正的瓶颈点**~~ — ✅ §2.8.a 已落地，`phalanx eval` + 15 个 golden task + run 持久化 + baseline diff + CI smoke 全部就位。详见 [`phase-2.8a-evaluation.md`](phase-2.8a-evaluation.md)。

### 2.7 安全 / 失控保护（autonomy prerequisites）

自主化提升 = 失控代价提升。下面这些**必须**在 §2.8 自主化能力之前 / 同步落地：

| 机制 | 作用 | phalanx |
|---|---|---|
| **Tool guardrails** | 危险命令二次确认（`rm -rf` / 数据库 drop）；上游 `agent/tool_guardrails.py` | ✗ §2.8 |
| **Sandboxing** | 不可信工具调用进 docker / vercel sandbox 不是 LocalTerminalEnv | ◇ phalanx 仅 local backend |
| **IterationBudget** | 主防"无限工具调用循环烧 API" | ✅ §2.1 |
| **Checkpoint / rollback** | 任何"自我修改"操作前先 snapshot；`/snapshot` `/rollback` 回退 | ✗ §2.8 `checkpoints` |
| **Capability gating** | "仅在白名单任务里允许 agent 写 `tools/`"——动态工具创建必须配这个 | ✗ 全新 |
| **Audit log** | 所有"agent 修改了系统状态"的事件可审计 | ◇ logs/ 有，但没专门 audit channel |

### 2.8 元控制（meta orchestration）

| 模式 | 含义 |
|---|---|
| **Delegate / sub-agent** | 主 agent 派子 agent 跑独立子任务，共享 budget；上游 `tools/delegate_tool.py` |
| **Plan-execute-verify 三段** | 不直接 chat completion，先生成 plan → 执行 → verify，每段独立 model 调用 |
| **"什么时候停"控制** | 超过 N 轮自动 escalate 到人；困惑度 / token 消耗超阈值触发反思 |
| **Multi-agent debate** | 多 agent 互相质询，收敛到共识；研究方向 |

phalanx 起点：`AIAgent.iteration_budget` 已为 sub-agent 共享预算预留接口（§2.1 §4.1）；`delegate_task` 一移植即接通。

## 3. phalanx 当前真实差距

phalanx 截至 §2.8.b wave 4 能做：

✅ 单 agent 单 turn 串行工具调用
✅ 多 provider（OpenAI / Anthropic / Codex）流式响应
✅ 跨 session 持久化对话历史 + token / cost 计量
✅ trajectory 全量记录（reward 信号的源数据）
✅ 浏览器 dashboard 看 sessions / logs / config / env / analytics
✅ **评估闭环** —— `phalanx eval` + 15 个 golden task + run 持久化 + diff vs baseline + CI smoke（§2.8.a）
✅ **跨 session memory** —— `agent/memory_manager.py` 真实版本 + FTS5 trigram + pinned-first + turn 0 自动注入 system prompt（§2.8.b wave 1）
✅ **Context 自动压缩** —— LLM-driven summary + auxiliary client + threshold preflight + pruning fallback（§2.8.b wave 2）
✅ **`@reference` 显式引用** —— `@file:` / `@diff` / `@url:` / `@session:` 解析 + REPL `/ref` + web `/api/references/resolve`（§2.8.b wave 3）

✅ **delegate / sub-agent** —— 主 agent 通过 `delegate_task` 工具孵化 sub-AIAgent (executor/critic/planner 三档 role + 共享 IterationBudget + depth cap=2 + parent_session_id 链)（§2.8.c）
✅ **反思 / critic** —— `--critic-model` CLI + REPL `/critic` + 强制 VERDICT 一行 + 便宜模型混搭主模型（§2.8.c wave 2-3）
✗ **skill 系统** —— 经验只能在 user prompt 里积累，无法固化
✗ **guardrails / sandboxing** —— LocalTerminalEnv 直跑 host shell

**§2.8.a + §2.8.b + §2.8.c 完成后**：「经验流 → 反思 → 评估」三段已闭，agent 第一次能跑 main + critic 双 agent 模式 + 任何改动有 baseline 可衡量。**剩下最大瓶颈是更新机制（skill 系统）** —— 需要 §2.8.d 让经验固化为可复用 skill。

## 4. 推荐落地顺序

按 **投入产出比 × 解锁能力 × 风险** 排序：

1. ~~**评估闭环**（§2.8.a）~~ — ✅ 落地，`phalanx eval` + 15 个 golden task（10 基础 + 5 reference）+ run 持久化 + baseline diff + CI smoke。
2. ~~**memory & context**（§2.8.b）~~ — ✅ 落地，long-term memory + FTS5 + 自动 context compression + `@reference` 显式引用 + 三 hook 集成。详见 [`phase-2.8b-memory-context.md`](phase-2.8b-memory-context.md)。
3. ~~**delegate / 子 agent**（§2.8.c）~~ — ✅ 落地，`tools/delegate_tool.py` 解锁 critic / planner 双 agent 模式 + auxiliary async surface 真实化 + 5 个 delegate 类目 golden + IterationBudget 共享 bug 修复。详见 [`phase-2.8c-delegate.md`](phase-2.8c-delegate.md)。
4. **skills 系统**（§2.8.d）— "经验固化 → 可复用 skill"的载体。前置：delegate（curator 是个独立 agent）。
5. **guardrails + checkpoints**（§2.8.e）— 自主化前的安全网。**应跟 delegate 同期或更早**。
6. **RL / training**（§2.8.f）— 真正的"权重级进化"，依赖前面所有数据。
7. **动态工具创建 / 自动 prompt 改写**（§2.8.g+）— 研究方向，前置依赖最重。

**§2.8.c 已落地**——memory + evaluation + delegate / critic 三段闭环。下一站 **§2.8.d skills 系统** + **§2.8.e guardrails + checkpoints**（强烈建议并行）让经验固化 + 自主化前的安全网。完成后 §2.8.f RL 才有数据基础。

## 5. 安全前置：自主化的红线

任何启用"agent 改 phalanx 自身"的能力之前，下面三条必须满足：

1. **所有自我修改操作走 checkpoint**：动 `~/.phalanx/` `tools/` `skills/` `agent/` 之前先 snapshot，`/rollback` 一键回退
2. **trajectory 永久审计 trail**：`hermes_state.SessionDB.event_log` 表（schema 未建——§2.8 时加），"agent 创建了哪个 skill / 改了哪个 prompt"必须留痕
3. **capability gating 默认关闭**：动态 tool / skill 创建走 `--enable-self-mod` 显式 opt-in flag，配额硬上限（每天 ≤ N 次）

phalanx 当前**没**有自主修改能力——这是好事，趁着还没引入先把红线立起来。

## 6. 常见误解 / 反模式

- **"加更长的 context 就行"**——错。context 增长是逃避问题，没有反思的更长 context 只是更贵地犯同样错误
- **"用更聪明的模型替换现有模型"**——能解一时之痛，但跨 session 学习这件事换模型也不会自动出现
- **"先做 RL 再说"**——没有 reward signal 的 RL 是无效投入，先建评估闭环
- **"全自动化"**——所有自主化能力都应该有"escalate 到人"的清晰边界，不要追求 100% 无人值守
- **"先做反思 / critic"** —— 没有 long-term memory 的反思只能在单 turn 内有效，下个 session 又从零开始

## 7. 参考阅读（外部）

- DSPy / TextGrad — 自动 prompt 改写
- ReAct / Reflexion — single-turn reflection 范式
- Voyager — Minecraft 里 skill discovery 的早期工作
- AlphaCode 2 / Self-Refine — verification + critique loop
- Atropos（Nous Research） — phalanx 上游用的 RL framework
- Constitutional AI — Anthropic 的 self-critique 提示框架

## 8. 下一步

§2.8.a + §2.8.b + §2.8.c 已经落地（详见 [`phase-2.8a-evaluation.md`](phase-2.8a-evaluation.md) / [`phase-2.8b-memory-context.md`](phase-2.8b-memory-context.md) / [`phase-2.8c-delegate.md`](phase-2.8c-delegate.md)）。phalanx 现在**第一次具备"经验流 → 反思 → 评估"三段闭环**——agent 跨 session 累积经验 + critic 双 agent 反思 + 任何改动有 baseline 可衡量。剩下缺"更新"机制（skill 固化 / prompt 改写 / RL）。

接下来按 ROI 推荐:

1. **§2.8.e Guardrails + checkpoints**（约 3-5 工作日）—— `tools/checkpoint_manager.py` + `agent/tool_guardrails.py`。前置:无。**强烈建议先做**——§2.8.c delegate 已经能让 sub-agent 并行写盘,没 checkpoint 就是裸奔;§2.8.d skills 一旦让 agent 创建 skill 文件,没 guardrail 就是 agent 修改自己。
2. **§2.8.d Skills 系统**（约 7-10 工作日）—— `skills/` + `tools/skill*.py` + `tools/skills_hub.py` + `propose_skill` 工具 + `agent/curator.py`（淘汰失效 / 合并冗余 skill）。前置:delegate（✅,curator 是独立 agent）+ guardrails（**最好先有 §2.8.e**)。
3. **§2.8.f RL / training**（约 2-3 周）—— 真正的"权重级进化"。前置:全部 skills + memory + eval baseline 数据回流。

注意 memory + evaluation + delegate 的存在让上面三步**第一次可衡量 + 第一次可反思**——任何 skill / guardrail 改动都能 `phalanx eval --baseline X --diff` 验证有没有让 agent 变笨 / 变慢 / 变贵,critic agent 还能在落地前对设计本身做一次评审。这是 §2.8.a + §2.8.c 存在的全部理由。
