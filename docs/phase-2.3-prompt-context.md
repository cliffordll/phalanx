# Phase 2.3 设计文档 — Prompt 系统 + 模型元信息 + 上下文管理

> 配套阅读：
> - [`MIGRATION_PLAN.md`](MIGRATION_PLAN.md) §2.3 — 计划层面的源文件清单与策略
> - [`ARCHITECTURE.md`](ARCHITECTURE.md) — 全局视图（§2.3 落地后未单独拆章节，差异看 §13.x 关键设计）
> - [`run-loop-vs-upstream.md`](run-loop-vs-upstream.md) — phalanx 主循环与上游的逐节点差异

本文记录 **§2.3 三个 wave 的非平凡设计决策**——为什么某些上游模块整体 verbatim、某些写 shim、某些直接跳过。代码细节看实际文件；本文聚焦"为什么是这样"。

## 0. 范围

§2.3 覆盖 hermes-agent 的 **prompt 子系统 + 模型元信息 + 上下文管理薄壳**，落地为 3 个独立 wave：

| Wave | 内容 | 提交 |
|---|---|---|
| 1 | `agent/prompt_builder.py`（裁剪）+ `subdirectory_hints.py` / `prompt_caching.py`（verbatim）+ AIAgent 主循环接入 + API 错误重试（顺手 P0） | `36c933a` |
| 2 | `agent/model_metadata.py` / `usage_pricing.py` / `trajectory.py`（verbatim）+ CLI `model list/info` `pricing estimate` | `2a2486c` |
| 3 | `agent/memory_manager.py` / `agent/context_compressor.py`（shim）+ CLI `prompt show [--raw]` + `oneshot --dump-messages` | `e3ab035` |

整个 §2.3 落地后已注册工具数没变（仍 9 个），但 system prompt 内容、model 元信息查询、context 安全网都进入了 phalanx 的产线代码。

## 1. Wave 1 — Prompt 系统与 API retry

### 1.1 `prompt_builder.py` — 上游 1180 → phalanx 333 行

**保留（verbatim）**：

- `_scan_context_content` + `_CONTEXT_THREAT_PATTERNS` (10 条) + `_CONTEXT_INVISIBLE_CHARS` (10 个零宽 / RTL 控制字符) — 安全网
- `_find_git_root` / `_HERMES_MD_NAMES` / `_find_hermes_md` / `_strip_yaml_frontmatter` — context 文件发现
- `WSL_ENVIRONMENT_HINT` / `build_environment_hints()` — 执行环境提示
- `_truncate_content` / `load_soul_md` / `_load_hermes_md` / `_load_agents_md` / `_load_claude_md` / `_load_cursorrules` / `build_context_files_prompt` — context 加载

**删除**：

- `build_skills_system_prompt` / `clear_skills_system_prompt_cache` / `_skills_prompt_snapshot_path` — Skills 索引整套（phalanx 不做 §2.7 之前不上 skills）
- `build_nous_subscription_prompt` — Nous 订阅功能
- `PLATFORM_HINTS` 表 — 消息平台特定的提示（Telegram、Discord、IRC 等），phalanx 没接消息平台

**新增（phalanx-only）**：

- `build_system_prompt(user_system, ephemeral, cwd, include_context_files)` — 5 槽组装顶层入口

### 1.2 5-Slot System Prompt 架构

按高→低特异性组装：

```
1. Identity         ← SOUL.md if present, else DEFAULT_AGENT_IDENTITY
2. Environment      ← WSL_ENVIRONMENT_HINT (扩展点：Termux / Docker / WSL2 / SSH)
3. Project context  ← first-match-wins:
                       .hermes.md / HERMES.md  (walks to git root)
                       AGENTS.md / agents.md   (cwd only)
                       CLAUDE.md / claude.md   (cwd only)
                       .cursorrules + .cursor/rules/*.mdc  (cwd only)
                     SOUL.md never repeats here (skip_soul=True)
4. user_system      ← --system flag / AIAgent.system_message 参数
5. ephemeral        ← AIAgent.ephemeral_system_prompt 参数（per-call instructions）
```

空槽自动跳过；最终 `\n\n` join。

**为什么不在 system prompt 里列工具**：OpenAI function calling 协议已经通过 `tools=[...]` 参数把所有 schema 发给模型——在 system prompt 里再列一遍只浪费 token、还可能与实际 registry 状态不同步。SOUL.md 里写"工具使用策略"（"先 search_files 再 read_file"）属于元指令，不是工具描述。

### 1.3 安全网：`_scan_context_content`

每个外部文件载入前都过 10 条 prompt-injection 正则 + 10 个不可见字符检测：

| 类别 | 示例 |
|---|---|
| 显式越狱 | `ignore (previous\|all\|above) instructions` / `system prompt override` |
| 欺骗 | `do not tell the user` / `disregard your rules` / `act as if you have no restrictions` |
| HTML / CSS 注入 | `<!-- ... ignore ... -->` / `display: none` 隐藏 div |
| 凭据外泄 | `curl ... $TOKEN` / `cat .env` / `cat .netrc` |
| Unicode 隐藏 | U+200B-200E、U+202A-202E、U+2060、U+FEFF (10 个) |

命中替换为 `[BLOCKED: ... contained potential prompt injection ...]` 字符串 + `logger.warning`。这条防线每次 `load_soul_md` / `_load_hermes_md` 等都跑——SOUL.md 也不豁免，因为用户的 home 目录可能被恶意软件染指。

### 1.4 API 错误重试（顺手补 P0）

`run_agent.py` 早就有 `_classify_error` (`agent.error_classifier.classify_api_error`，1000 行 verbatim) + `_retry_delay` (`agent.retry_utils.jittered_backoff`，57 行 verbatim)，但主循环的 `try/except` 块只 `break`，从没用过这两个 helper。

Wave 1 把它们终于接通：

```python
attempt = 0
max_api_attempts = 5
while True:
    try:
        response = self._call_chat_completions(messages, tools)
        break
    except Exception as exc:
        attempt += 1
        classification = _classify_error(exc, provider="openai", model=self.model)
        if not classification.retryable or attempt >= max_api_attempts:
            stop_reason = f"api_error:{type(exc).__name__}"
            final_text = f"[error] API call failed: {exc}"
            break
        time.sleep(_retry_delay(attempt))   # jittered exp backoff, max 60s
```

5 次上限 + 抖动指数退避。`classification.retryable=False` 时（认证错、配额超出、模型不存在等）直接终止——不会盲目重试浪费配额。

## 2. Wave 2 — 模型元信息与估价

### 2.1 `model_metadata.py` 整体 verbatim（vs plan 的"裁剪 5-10 模型"）

**Plan 原话**："仅保留你当前要用的 5–10 个模型条目（OpenAI/Anthropic 主线），数百行模型表全删"

**实际决策**：整体 verbatim，1472 行。

原因：

- `DEFAULT_CONTEXT_LENGTHS` 表实际只 60 个条目 ≈ 100 行，远小于 plan 估计
- 删了的话用户切到 grok / deepseek / kimi / qwen 等模型时，context window 探测**回退到 64K 默认**，体验显著退化
- 这张表是常量，运行时零成本

Plan 的 "61668 行" 估算是错的（看起来是文件字节数，不是行数）——本身就不该按那个数字裁剪。

### 2.2 多源 context 探测链

`get_model_context_length(model, base_url, api_key)` 的探测顺序：

```
1. on-disk cache (PHALANX_HOME/context_length_cache.yaml, TTL 1h)
2. live endpoint /models metadata (OpenAI/Anthropic/Ollama 兼容 API)
3. Ollama /api/show (本地 Ollama 专有，能给出 num_ctx)
4. OpenRouter /api/v1/models (TTL 5min)
5. DEFAULT_CONTEXT_LENGTHS substring match (e.g. "qwen2.5:1.5b" → "qwen" → 131072)
6. CONTEXT_PROBE_TIERS 默认 (64K)
```

实测：本地 Ollama qwen2.5:1.5b 最终从 `/api/show` 拿到真实 32768，而不是 substring fallback 的 131072——多源探测的价值就在这里。

### 2.3 `usage_pricing.py` Decimal 精度

整体 verbatim 721 行。`CanonicalUsage` / `BillingRoute` / `PricingEntry` / `CostResult` dataclass + `Decimal("0")` 精确累计：

```python
amount += Decimal(usage.input_tokens) * entry.input_cost_per_million / _ONE_MILLION
```

不用 `float` 防累积误差。billing-route 区分 `subscription_included` / `endpoint_native` / `openrouter_fallback` 三态 —— Nous 订阅用户的 included 模型直接返回 `cost=0` + `status=included`，避免误报。

phalanx 当前没接 OpenRouter，`pricing estimate` 多数情况返回 `unknown / n/a` —— 用户配 OPENAI_API_KEY=ollama 时这是预期行为。

### 2.4 CLI 子命令

| 命令 | 实现 | 实测 |
|---|---|---|
| `hermes model list` | 60 条目按 ctx 降序打印 | grok-4-1-fast 2M / gpt-5.4 1.05M / qwen 131K |
| `hermes model info <name> [--base-url URL]` | 多源探测 + 显式列出 fallback 来源 | Ollama qwen2.5:1.5b → context: 32,768 (真实) + fallback: 'qwen' → 131,072 |
| `hermes pricing estimate --model NAME --input-tokens N --output-tokens M` | Decimal 估价 + 优雅降级 | 无 OpenRouter 缓存时 status=unknown / cost=n/a |

## 3. Wave 3 — 上下文管理薄壳与 prompt CLI

### 3.1 `memory_manager.py` shim 策略（99 行 vs 上游 557）

**Plan 明确**："不移植本期：build_memory_context_block 写空字符串 stub，函数名保留"

phalanx 落地保留 4 个公开符号（与上游签名一致）：

| 符号 | 上游行为 | phalanx shim 行为 |
|---|---|---|
| `sanitize_context(text)` | 剥 `<memory-context>` / `<internal-note>` / fence tag 标签 | 直返不变 |
| `StreamingContextScrubber` | 跨 stream delta 持有部分标签的状态机 | identity scrubber，`feed(delta)` 直返 |
| `build_memory_context_block(s)` | 包成 `<memory-context>...</memory-context>` 注入 | 返回 `""`（无 memory 注入） |
| `MemoryManager` | 注册 recall/store/forget 工具，调用 MemoryProvider 后端 | 类存在，`has_tool()` 永远 False，`handle_tool_call()` 返回 `tool_error` |

**为什么保留这些**：未来 cherry-pick 上游真实 memory 系统时，调用方代码 (`run_agent.py` / `cli.py` / 上游各处) 不需要改任何 import 行——只要把 shim 文件覆盖即可。

### 3.2 `context_compressor.py` 薄壳（168 行 vs 上游 1416）

**Plan 明确**："不移植本期：用'消息超过 N 条就丢最早 user/assistant 对'的回退策略；保留类名 ContextCompressor 作为薄壳"

落地的修剪算法：

```python
保留:
  系统消息 (slot 0)
  头 protect_first_n=3 个非 system 消息
  尾 protect_last_n=6 个消息

丢弃:
  中间所有 user/assistant/tool 消息（不含 system）
  直到 len(messages) <= max_messages=60
```

**关键决策：不继承 `ContextEngine` ABC**。上游 `ContextCompressor(ContextEngine)`，而 `ContextEngine` 是 1416 行外加 abstract method 的复杂基类，引入它就拖出整个 `context_engine.py`。phalanx shim 让 `ContextCompressor` 直接继承 `object`，自己实现 `name` / `update_from_response` / `should_compress` / `compress` / `on_session_reset` 等公开接口——签名一致，但少一层 ABC。

**实测**：16 条输入（1 system + 15 user/assistant 交替）+ `max_messages=5` → 输出 10 条（1 system + 头 3 + 尾 6），丢了 6 条 ✓

### 3.3 `display.py` 故意未移植

Plan §2.3 写"裁剪保留 print_*"，但上游 `display.py`（1002 行）公开符号是 `LocalEditSnapshot` / `KawaiiSpinner` / `build_tool_preview` / `extract_edit_diff` 等，**没有任何 print_* 函数**。

phalanx 当前也没引用 display 任何符号——主循环没接 progress callback，CLI 用 `print()` 直出。**强行裁剪反而增加沉默冲突风险**——上游下次更新 display 时不知道哪些是 phalanx 的版本、哪些是上游版本。

策略：等真有第一处 phalanx 调用站点（比如 §2.4 给 streaming UI 加 progress display）时再裁剪到当时实际需要的最小子集。

### 3.4 新增 CLI（plan §2.3.1 收尾）

| 命令 | 实现 | 用途 |
|---|---|---|
| `hermes prompt show [--raw] [--system X] [--cwd Y]` | 调 `build_system_prompt(...)` 直接 print | 调试 SOUL.md / context 文件加载顺序 |
| `hermes oneshot --dump-messages "..."` | 跑完后把完整 messages JSON 写到 stderr | 看模型实际收到什么对话历史，调试工具调用闭环 |

**stdout / stderr 分流**：`final_response` 走 stdout，`--dump-messages` 走 stderr。这样 `... 2>/dev/null` 仍然干净拿到回复，`... | jq` 只接到回复文本；想要 messages 用 `... 2>&1 | jq`。

## 4. 已知降级 / 推迟项

| 功能 | 现状 | 解锁时机 |
|---|---|---|
| LLM 摘要式上下文压缩 | 退化为"消息超 N 条丢最早" | §2.7（移植 auxiliary_client + context_engine） |
| Memory 子系统 | 完全 stub，`has_tool` 永远 False | §2.7 |
| Streaming 响应 | `stream_callback` 接收但忽略 | §2.4 |
| Skills 索引 | 完全删除（不是 stub） | §2.7+ |
| Platform hints (Telegram / Discord) | 完全删除 | 不计划做 |
| `_build_assistant_message` 完整 reasoning 处理 | 当前是 phalanx 自加的 `_serialize_tool_calls`（覆盖 1/8） | §2.4（multi-provider 适配阶段一起做最自然） |
| `display.py` 任何符号 | 未移植 | 第一处实际调用时按需裁剪 |
| OpenRouter pricing 实时拉取 | 默认无 API key 时 cost=n/a | 用户配 OPENROUTER_API_KEY 后自动启用 |

## 5. 对外接口稳定性承诺

§2.3 落地后，**这些公开 API 可以被项目内代码安全引用**，未来 cherry-pick 上游不会破坏：

```python
from agent.prompt_builder import (
    build_system_prompt,           # phalanx-only，但 5-slot 架构稳定
    load_soul_md,
    build_context_files_prompt,
    build_environment_hints,
    _scan_context_content,         # subdirectory_hints 也用
)
from agent.model_metadata import (
    get_model_context_length,
    DEFAULT_CONTEXT_LENGTHS,
    is_local_endpoint,
    estimate_tokens_rough,
)
from agent.usage_pricing import (
    CanonicalUsage,
    BillingRoute,
    estimate_usage_cost,
    has_known_pricing,
)
from agent.memory_manager import (
    sanitize_context,
    StreamingContextScrubber,
    build_memory_context_block,
    MemoryManager,
)
from agent.context_compressor import ContextCompressor
```

注意：**`MemoryManager` / `ContextCompressor` / `build_memory_context_block` 当前是 shim，行为是空 / 修剪 / 空字符串**。引用时要意识到这一点——不要 assumption "调了 memory 就有记忆"。

## 6. 配套阅读

- 主循环执行节点的差异：[`run-loop-vs-upstream.md`](run-loop-vs-upstream.md) §1（Wave 1 关心节点 ②，Wave 3 关心节点 ⑥）
- 缺失项的优先级：[`run-loop-vs-upstream.md`](run-loop-vs-upstream.md) §2 — `_build_assistant_message`、streaming、容错重试
- 上游迁移路线：[`MIGRATION_PLAN.md`](MIGRATION_PLAN.md) §2.3 / §2.4 / §2.7
- 整体架构（含 §13 关键设计）：[`ARCHITECTURE.md`](ARCHITECTURE.md)

## 7. 下一步

§2.3 全部落地后下一阶段是 **§2.4 — 多 provider 适配 + Provider CLI**：

- 主线候选：`agent/anthropic_adapter.py`（用 Claude API 时立刻移）
- 跟着搬的：`_build_assistant_message`（reasoning / surrogate / think-block 处理，§2.4 阶段对 anthropic / codex / gemini 是必需的）
- 顺带可补：streaming `stream_callback`（拓展 `_call_chat_completions`，~80 行 + provider 桥）

按 phalanx 一贯策略：每个 provider 独立移植 + 必要时补 shim，避免一次性拖入所有 adapter（每个都几十 KB）。
