# `run_conversation` 主循环：phalanx vs 上游对照

> 配套阅读：[`ARCHITECTURE.md`](ARCHITECTURE.md) §4 是当前 phalanx 主循环的正向描述，本文是与 hermes-agent 上游的**差异清单**。

## 0. 体量

| 项目 | 行数 | 路径 |
|---|---:|---|
| phalanx | ~120 | `run_agent.py:707-829` |
| hermes-agent (上游) | ~3400 | `run_agent.py:10382-13800+` |

差距几乎全部集中在 6 个明确方向：state 重置、上下文管理、API 调用增强、响应解析容错、工具执行钩子、turn-end 收尾。本文按这 6 个节点逐项对照。

## 1. 节点对照

主循环骨架（伪代码）一致：

```python
while api_call_count < max_iterations and budget.remaining > 0:
    consume()                                 # ① 入口 setup
    response = call_chat_completions(...)     # ② API 调用
    parse(response)                           # ③ 响应解析
    execute_tool_calls(...)                   # ④ 工具执行
turn_end()                                    # ⑤ 收尾
```

差异都在每一步**内部**做了多少额外动作。

### ① turn-级 setup（进入 `run_conversation` 后、while 前）

| 上游做的事 | phalanx 现状 | 备注 |
|---|---|---|
| `_install_safe_stdio()` 每次都装一次 | 只在 `__init__` 装一次 | 防 broken pipe |
| `_ensure_db_session()` | 无 | session DB |
| `_restore_primary_runtime()` 恢复上一轮 fallback | 无 | fallback model 切回 |
| `_sanitize_surrogates(user_message)` 过滤 lone surrogate | **无** ⚠️ | 富文本粘贴会崩 OpenAI SDK JSON 序列化 |
| 重置 7 个 retry 计数器（`_invalid_tool_retries` / `_invalid_json_retries` / `_empty_content_retries` / `_incomplete_scratchpad_retries` / `_codex_incomplete_retries` / `_thinking_prefill_retries` / `_post_tool_empty_retried`） | 无 | 给容错重试用 |
| `_tool_guardrails.reset_for_turn()` | 无 | 工具黑名单 |
| `_cleanup_dead_connections()` 清死 TCP socket | 无 | 防 zombie connection 上来就挂 |
| `_compression_warning` replay 给 gateway | 无 | 上下文压缩告警 |
| `_stream_context_scrubber.reset()` | 无 | 流式响应上下文 scrub |
| `_user_turn_count++` | 无 | 跨 turn 计数 |
| `_hydrate_todo_store(history)` —— gateway 模式从 history 重建 todo | 无 | gateway 上线时必加 |
| `iteration_budget` 重置 | ✓ | phalanx 已实现 |
| `_set_session_log_context(session_id)` | ✓ | phalanx 已实现 |

### ② messages 列表初始化 / assistant message 构造

| 上游 | phalanx | 备注 |
|---|---|---|
| `messages = list(history)`（**浅拷贝**） | `[copy.deepcopy(m) for m in history]`（**深拷贝**） | phalanx 更保守，性能略差 |
| 系统提示词 prefix caching：建一次缓存到 `self._cached_system_msg`，仅压缩后失效 | 每次重建 `effective_system` | 浪费 prompt cache 命中机会 |
| **prefill_messages 注入**（few-shot priming）：API 调用时即时插入但不落 messages | 无 | 待 §2.7 |
| `original_user_message = persist_user_message or user_message` —— 区分 API-only synthetic prefix 与真实 user message | 无 | persist_user_message 当前签收但忽略 |
| `_should_review_memory` 计算（memory_nudge_interval） | 无 | 待 §2.7 |
| `_build_assistant_message(assistant_message, finish_reason)`（`run_agent.py:8584`，~150 行）—— assistant 消息构造单一入口 | `_serialize_tool_calls(tool_calls)` + 内联 `assistant_record` 字典构造（phalanx 自加，~30 行） | **phalanx 命名 / 实现都是自己写的**，不是上游 verbatim。详见下文"`_build_assistant_message` 对照" |

#### `_build_assistant_message` 对照

phalanx 的 `_serialize_tool_calls` **不对应**上游的同名函数。上游 `run_agent.py` 没有同名函数（仅 `plugins/observability/langfuse/__init__.py:354` 有同名但用途不同——给 Langfuse 上报）。**真正的对应是上游 `_build_assistant_message`**，但它做的事情远不止 tool_calls 序列化：

| 上游 `_build_assistant_message` 包揽的能力 | phalanx 现状 |
|---|---|
| 提取 `tool_calls`（结构化保留） | ✓ `_serialize_tool_calls` 做这一项（约占上游 1/8） |
| 提取 `reasoning`（5 种字段路径 fallback） | 无 |
| 提取 `reasoning_content`（DeepSeek / Kimi thinking mode 必需，否则 replay 时 HTTP 400） | 无 |
| 提取 `reasoning_details`（OpenRouter / Anthropic 跨 turn 推理延续） | 无 |
| 从 `content` 里抢救 `<think>...</think>` 块到 reasoning | 无 |
| 把 think 块从 stored content 里剥掉（防泄漏到下游） | 无 |
| `_sanitize_surrogates(content)` 防 JSON 序列化崩溃 | 无 |
| Codex Responses API 的 encrypted reasoning items 保留 | 无 |
| `finish_reason` 一并存储 | 无 |

**未来对齐路径**：在 §2.4 multi-provider 适配阶段，verbatim 复制上游 `_build_assistant_message` + 它的 4 个依赖函数（`_extract_reasoning` / `_strip_think_blocks` / `_sanitize_surrogates` / `_needs_thinking_reasoning_pad`，共 ~200 行），删掉 phalanx 的 `_serialize_tool_calls`，主循环里 `assistant_record = {...}` 改为 `assistant_record = self._build_assistant_message(assistant_msg, finish_reason)`。届时 reasoning 字段处理对 anthropic / codex / gemini 都是必需的，依赖正好齐备。

### ③ API 调用（while 循环每轮的核心动作）

| 上游 | phalanx | 备注 |
|---|---|---|
| `_interruptible_api_call(...)` 包了中断 + 流式回调 + 完整重试机制 | `_call_chat_completions(messages, tools)` 直接调 SDK | |
| **流式响应** via `stream_callback`：增量返回 delta，TTS / UI 可同步消费 | 接收 `stream_callback` 参数但忽略 ⚠️ | 待 §2.4 |
| API 错误分类 + 重试：`_classify_error` → `retryable / should_compress / should_rotate_credential` 三态决策；`_retry_delay(attempt)` 抖动退避 | `try/except`，单次失败直接 break ⚠️ | **helper 已存在，主循环没接**——P0 |
| **fallback model 切换**：primary 模型连续失败 → 切到 `fallback_model`；下一轮 `_restore_primary_runtime` 切回 | 无 | |
| 凭据池轮换（多 OPENAI_API_KEY）+ provider rotation | 无 | 待 §2.7 |
| **anthropic / codex / bedrock / gemini 多 provider 路径** | 仅 OpenAI 兼容 | 待 §2.4 |
| 上下文压缩：`_should_compress` → `_compress_messages_if_needed`，把大段 history 折叠成摘要 | **无** ⚠️ | 长对话 context overflow 时模型 400——P0 |

### ④ 响应解析 + 容错重试（**最大缺失区**）

| 上游 | phalanx | 备注 |
|---|---|---|
| 从 `content` 字段里抢救 XML 形式的 `<tool_calls>...</tool_calls>` —— 模型把 tool_calls 错放进 content 时仍能识别（`run_agent.py:3052+`） | **无** ⚠️ | 小模型经常这么做 |
| `_handle_no_tool_calls` 系列退化处理 | 无 | |
| empty content + finish_reason=stop 但还在 tool 链里 → `_post_tool_empty_response` 重试 | 无 | |
| 不完整 scratchpad（thinking 没收尾）→ retry | 无 | |
| thinking-prefill 重试 | 无 | |
| 无效工具名（model 编了一个不存在的工具）→ `_invalid_tool_retries`，把错误塞回 messages | 无 | |
| JSON 参数解析失败 → `_invalid_json_retries`，告诉模型重试 | 当前是 `function_args = {}` 然后照样 dispatch，不告诉模型 | |
| finish_reason="tool_calls" 但 tool_calls 列表为空的退化处理 | 无 | |

### ⑤ tool_calls 执行 — **2026-05 已对齐结构**

| 上游 | phalanx | 备注 |
|---|---|---|
| `_execute_tool_calls` 入口分流 | ✓ | 同名同签名 |
| 顺序 + 并发两条路径 | ✓ | concurrent stub 转 sequential |
| `_invoke_tool` 分流：todo / memory / clarify / delegate / session_search / catch-all | ✓ | todo + catch-all 实现，其他占位注释 |
| `_should_parallelize_tool_batch`：path-overlap aware | ✓ | 空 allow-list 永远返回 False |
| 中断时跳过剩余工具，给每个 skipped tool 加 cancellation message | ✓ | |
| layer 2 `maybe_persist_tool_result` + layer 3 `enforce_turn_budget` | ✓ | wave 5 已落地 |
| 每个 tool 跑前 `pre_tool_call` plugin hook + `_tool_guardrails.before_call` 黑名单检查 | 无 | 待 §2.7 |
| `tool_progress_callback` / `tool_start_callback` / `tool_complete_callback` | 无 | gateway 用 |
| 文件突变前 checkpoint snapshot（`_checkpoint_mgr`） | 无 | 待 §2.6 |
| `_subdirectory_hints.check_tool_call` 提示模型注意子目录访问 | 无 | |
| `/steer` 注入：用户在工具运行时附加指令，下次模型迭代看到 | 无 | 待 §2.6 |
| `_apply_pending_steer_to_tool_results` | 无 | |
| `_track_activity` / `set_activity_callback` 防 idle timeout | 无 | gateway 用 |

### ⑥ 循环退出后的 turn-end 收尾

| 上游 | phalanx | 备注 |
|---|---|---|
| 详尽的 turn-end 日志：`reason / api_calls / budget / tool_turns / last_msg_role / response_len / last_tool_name` | 无 | observability |
| 检测到 `last_msg_role == "tool"` + 非中断 → `logger.warning` "Turn ended with pending tool result (agent may appear stuck)" | 无 | 排查"agent 突然不说话"用 |
| `post_llm_call` plugin hook（外部 memory 同步等） | 无 | 待 §2.7 |
| 提取最后一条 assistant 的 `reasoning` 字段并返回给调用方 | 无 | 反思链支持 |
| `_store_assistant_message_in_db` / 持久化到 session DB | 无 | 待 §2.5 |
| memory_nudge / skill_nudge 触发条件评估 | 无 | 待 §2.7 |
| `_emit_status` 推送状态给 gateway client | 无 | gateway 用 |
| fallback 到最后 assistant content + 返回 result dict | ✓ | phalanx 已实现 |

## 2. 缺失项的影响优先级

| 优先级 | 缺失 | 实际影响 |
|---|---|---|
| 🔴 **P0** | **API 错误重试 + 退避** | 网络抖动一次就 break。**helper（`_classify_error`/`_retry_delay`）已经移植，主循环没接** |
| 🔴 **P0** | **上下文压缩** | 长对话直接 OOM，模型返回 400 |
| 🟡 P1 | 流式响应（`stream_callback`） | 长输出的可感知延迟、TTS 集成 |
| 🟡 P1 | 容错重试套件（XML tool_calls 抢救、empty content、invalid JSON、scratchpad） | 小模型（如 Qwen 1.5B）频繁生成不规范 tool_calls，不重试就直接停 |
| 🟡 P1 | `_build_assistant_message` 替换 `_serialize_tool_calls`（含 reasoning / surrogate / think-block 处理） | DeepSeek / Kimi thinking mode 模型 replay 时 HTTP 400；富文本粘贴 surrogate 字符崩 JSON 序列化；reasoning 链路丢失 |
| 🟢 P2 | surrogate 过滤 | 罕见，但富文本粘贴会崩 |
| 🟢 P2 | turn-end 日志 / DB 持久化 / plugins | 单 CLI 不必要，gateway 模式必加 |
| ⚪ P3 | guardrails / steer / checkpoint / activity | 高级特性，等 §2.6+ |

## 3. 建议补全顺序

按"最小代码改动 / 最大收益"排序：

1. **P0：API 错误重试**（~40 行）
   - 位置：`run_conversation` 主循环里 `_call_chat_completions` 的 `try/except` 块
   - 现状：`except: break`
   - 目标：调用 `_classify_error(exc)`，按 `retryable` 决定是否重试，用 `_retry_delay(attempt)` 计算退避；非 retryable 才 break
   - **helper 已就绪**（`run_agent.py:187-208`），只需主循环接入

2. **P1：流式响应**（~80 行 + provider 桥）
   - 拓展 `_call_chat_completions` 接受 `stream_callback`
   - delta 增量回调
   - 等 §2.4 多 provider 适配层落地后一起做

3. **P1：容错重试套件**（每条 ~20-50 行，可分次落）
   - 优先：JSON 参数解析失败重试（最常见）
   - 其次：XML tool_calls 抢救（小模型常见）
   - 最后：empty content / scratchpad / thinking-prefill 重试

4. **P0：上下文压缩**（~200 行 + summarizer）
   - 需要先移植 `agent.compression` 系列模块
   - 进 §2.3（context window 管理）

5. **P1：`_build_assistant_message` 替换 `_serialize_tool_calls`**（~200 行 + 4 依赖）
   - verbatim 复制上游 `_build_assistant_message` + `_extract_reasoning` / `_strip_think_blocks` / `_sanitize_surrogates` / `_needs_thinking_reasoning_pad`
   - 删 phalanx 自加的 `_serialize_tool_calls`
   - 一次性补齐 reasoning / surrogate / think-block 处理
   - 跟 §2.4 multi-provider 适配一起做最自然——届时 reasoning 字段对 anthropic / codex / gemini 是必需的

6. **P2：turn-end 日志**（~30 行）
   - 立刻可做，纯观测无业务影响
   - 帮你排查"agent 不说话"类问题

## 4. 何时应该补到 verbatim 状态

短答：**等 §2.4 完成后**。

理由：上游 `run_conversation` 体量 3400 行，一次性 verbatim 复制会拖入 streaming / multi-provider / fallback runtime / credential pool / guardrails 等大量未移植子系统的依赖。**每一项要么 verbatim 跟着搬，要么写 shim**——总工程量超过 §2.2 wave 1+2+4+5 之和。

合理路径是按上述优先级**渐进补**，每补一项都保持 phalanx 主循环可工作。verbatim 完整移植留到 §2.4 / §2.7 落地、依赖齐备时再做一次性 cherry-pick。

## 5. 不打算补的项

少数差异是 phalanx 故意的，不必对齐：

- **深拷贝 conversation_history**：phalanx 用 `copy.deepcopy`，上游用浅拷贝。phalanx 的 caller 包括 CLI / pytest / 未来的 REPL，比 gateway 场景更易出现"caller 复用同一 history list"的误用，深拷贝更安全。性能差异可忽略（history 很少超过几 KB）。
- **`_install_safe_stdio()` 只在 `__init__` 装一次**：phalanx 没有 systemd / daemon 部署场景；turn 之间 stdio 不会被替换。
- **`_ensure_db_session` / `_emit_status`**：gateway-only 钩子，CLI 模式无需。
