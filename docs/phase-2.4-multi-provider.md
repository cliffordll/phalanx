# Phase 2.4 设计文档 — 多 provider 适配 + 流式输出

> 配套阅读：
> - [`MIGRATION_PLAN.md`](MIGRATION_PLAN.md) §2.4 — 计划层面的源文件清单与策略
> - [`ARCHITECTURE.md`](ARCHITECTURE.md) — 全局视图
> - [`run-loop-vs-upstream.md`](run-loop-vs-upstream.md) — phalanx 主循环与上游的逐节点差异
> - [`phase-2.3-prompt-context.md`](phase-2.3-prompt-context.md) — 前一阶段的 prompt 子系统设计

本文记录 **§2.4 六个 wave 的结构设计、数据格式转换和流式协议**——三个 provider 各自走不同的 SDK / endpoint / 事件流，但都收敛到相同的 OpenAI ChatCompletion 形态供 run loop 消费。

## 0. 范围与 wave 划分

| Wave | 内容 | 提交 |
|---|---|---|
| 1 | OpenAI 流式 (`_accumulate_stream`) + `_detect_provider` 框架 + CLI `--provider` / `--stream` / `provider list/test` / `model switch` | `7bcae52` |
| 2 | `agent/anthropic_adapter.py`（verbatim 1935 行）+ `tools/schema_sanitizer.py`（verbatim 257 行）+ AIAgent.provider 字段 | `33572f6` |
| 3 | `_call_anthropic_messages` 非流式路由 + `_anthropic_response_to_openai_shape` + `_ANTHROPIC_STOP_TO_OPENAI` 映射表 | `33572f6` |
| 4 | `_accumulate_anthropic_stream` (event-based via `messages.stream()`) + `stub_anthropic` 双队列 | `33572f6` |
| 5 | `agent/codex_responses_adapter.py`（verbatim 999 行）+ `_call_codex_responses` + `_codex_response_to_openai_shape` | `62092e6` |
| 6 | `_accumulate_codex_stream` (event-based via `responses.stream()`) + `stub_codex` 双队列 | `62092e6` |
| 收尾 | `_make_api_call` dispatcher 重构 — 与上游 `_interruptible_api_call` 命名对齐 | uncommitted |

§2.4 落地后 phalanx 同时支持三个 provider 的非流式 + 流式路径；bedrock / gemini 仍按需移。

## 1. 路由结构

### 1.1 Dispatcher / per-provider helper 分层

phalanx 沿用上游的两层结构（dispatcher + helper），名字也对齐：

| 上游 | phalanx | 职责 |
|---|---|---|
| `_interruptible_api_call(api_kwargs)` | `_make_api_call(messages, tools)` | 顶层 dispatcher：按 `self.provider` 分流 + retry |
| `_call_chat_completions()` (closure) | `_call_chat_completions(messages, tools, stream_callback)` | OpenAI-compat helper |
| `_anthropic_messages_create(api_kwargs)` | `_call_anthropic_messages(messages, tools, stream_callback)` | Anthropic helper |
| `_run_codex_stream(api_kwargs, ...)` | `_call_codex_responses(messages, tools, stream_callback)` | Codex helper |

**关键不同**：
- 上游 dispatcher 拆 streaming / non-streaming 两套（`_interruptible_api_call` vs `_interruptible_streaming_api_call`），各自携带 interrupt-thread + stale-detection 机制
- phalanx 合并成一个 `_make_api_call`，stream 与否由 `self._stream_callback` 决定；interrupt-thread 整套延后到 §2.5+
- 上游 retry 在 caller 级（fallback 模型 / 凭据池一起处理）；phalanx retry 在 dispatcher 内部（`_classify_error` + `_retry_delay`，5 次上限）

### 1.2 Per-provider helper 签名契约

三个 helper 签名完全对称，便于 dispatcher 调度：

```python
def _call_<provider>(
    self,
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]],
    stream_callback: Optional[Callable[[str], None]] = None,
) -> SimpleNamespace:  # OpenAI ChatCompletion shape
    ...
```

**输入**：永远是 OpenAI 格式的 `messages` + `tools`（run loop 对外只懂这一种）。

**输出**：永远是 `SimpleNamespace(choices=[SimpleNamespace(message=..., finish_reason=...)])`，run loop 只读 `.choices[0].message.{content, tool_calls}` 和 `.choices[0].finish_reason`，对底层 provider 无感知。

每个 helper 内部只做 4 件事：
1. 调用 provider-specific kwargs builder（messages / tools 转换）
2. 拿到 client（`_get_openai_client` 或 `_get_anthropic_client`）
3. 按 `stream_callback is None` 选择 `.create()` 或 `.stream()`
4. 把响应过对应的 `_*_response_to_openai_shape()`

### 1.3 Provider 检测

```python
_ANTHROPIC_HOSTS = ("api.anthropic.com",)
_BEDROCK_HOSTS = ("bedrock-runtime",)  # bedrock-runtime.<region>.amazonaws.com
_GEMINI_HOSTS = ("generativelanguage.googleapis.com", "aiplatform.googleapis.com")
_CODEX_HOSTS = ("api.openai.com/v1/responses",)
```

`_detect_provider(base_url)` 走子串匹配（大小写不敏感），命中即返回；fallback 为 `"openai-compatible"`（含 OpenAI proper / Ollama / vLLM / LM Studio / Together / Groq 等所有走 `/v1/chat/completions` 的 endpoint）。

**override 链**：CLI `--provider` flag → AIAgent 构造参数 → `_detect_provider(self._base_url)`。

## 2. 数据格式转换

run loop 永远拿 OpenAI 格式的 messages（角色 + content + tool_calls）和工具 schema（`{"type":"function","function":{"name","parameters"}}`）。每个 provider helper 进入时第一步就转成本路 wire format，出来时反过来。

### 2.1 OpenAI Chat Completions（基线）

不做转换；`messages` / `tools` 直接传给 `client.chat.completions.create`。

### 2.2 OpenAI → Anthropic Messages

| 转换函数 | 输入 | 输出 | 关键变换 |
|---|---|---|---|
| `convert_messages_to_anthropic` (adapter:1393) | OpenAI messages list | `(system: str \| list, messages: list)` | system 抽顶层；assistant `tool_calls` → `tool_use` content blocks；`tool` role → `tool_result` blocks |
| `convert_tools_to_anthropic` (adapter:1239) | OpenAI tool list | Anthropic input_schema list | `parameters` → `input_schema`（过 `_normalize_tool_input_schema` → `tools.schema_sanitizer.strip_nullable_unions` 去掉 `[T, null]` union） |
| `build_anthropic_kwargs` (adapter:1723) | model, messages, tools, max_tokens, reasoning_config, base_url | full `messages.create` kwargs | 调上面两个 + 算 `max_tokens` 默认 + 处理 OAuth / fast_mode / thinking（phalanx 都默认关） |

`_call_anthropic_messages` 直接调 `build_anthropic_kwargs(model=, messages=, tools=, max_tokens=, reasoning_config=None, base_url=)` 拿 kwargs；不做手工拆装。

### 2.3 OpenAI → Codex Responses

| 转换函数 | 输入 | 输出 | 关键变换 |
|---|---|---|---|
| `_chat_messages_to_responses_input` (adapter:247) | OpenAI messages（去掉 system） | Responses API `input` items | role+content → `message` items；assistant `tool_calls` → 顶层 `function_call` items；`tool` role → `function_call_output` items |
| `_responses_tools` (adapter:205) | OpenAI tool list | Responses function 定义 | `{"type":"function","function":{...}}` → `{"type":"function","name","parameters","strict":False}`（顶层化） |

`_call_codex_responses` 自己拼 kwargs：
```python
api_kwargs = {
    "model": self.model,
    "instructions": instructions,                              # system prompt
    "input": _chat_messages_to_responses_input(payload),        # 不含 system
    "tool_choice": "auto",
    "parallel_tool_calls": True,
    "store": False,
}
```

**system 处理**：Responses API 把 system 单独走 `instructions` 字段，不在 `input` 里。`_call_codex_responses` 检测 `messages[0].role == "system"`，抽出来塞 `instructions`，剩下传给 input 转换器。

**max_tokens 重命名**：Responses API 用 `max_output_tokens`（与 Anthropic 同名），phalanx 在 helper 里映射 `self.max_tokens → api_kwargs["max_output_tokens"]`。

### 2.4 响应归一化（统一回到 OpenAI shape）

| 入口 | 调用 | 产物 |
|---|---|---|
| `_anthropic_response_to_openai_shape(response)` | 内联解析 `response.content` 内的 `text` / `tool_use` blocks | `SimpleNamespace(choices=[...])` |
| `_codex_response_to_openai_shape(response)` | 委托 `_normalize_codex_response()` 拿 `(assistant_message, finish_reason)` tuple | `SimpleNamespace(choices=[...])` |
| OpenAI 非流式 | 直接返回 SDK 原生 ChatCompletion | 同 shape |
| 三路流式 | `_accumulate_*_stream` 收尾时各自调上面的 shape converter | 同 shape |

**shape contract**：
```python
SimpleNamespace(choices=[
    SimpleNamespace(
        message=SimpleNamespace(
            role="assistant",
            content=str | None,
            tool_calls=List[ToolCall] | None,  # 每个 ToolCall 有 .id .function.name .function.arguments
        ),
        finish_reason=str,  # "stop" / "tool_calls" / "length" / "content_filter"
    ),
])
```

这与 OpenAI SDK 真实 ChatCompletion 对象的 `.choices[0]` 接口一致——run loop 用同一段代码读所有 provider。

### 2.5 stop_reason / finish_reason 映射

OpenAI 用 `finish_reason`（`stop` / `tool_calls` / `length` / `content_filter`）。其他 provider 自己一套，需要映射：

**Anthropic**（`run_agent.py:_ANTHROPIC_STOP_TO_OPENAI`）：

| Anthropic stop_reason | OpenAI finish_reason |
|---|---|
| `end_turn` | `stop` |
| `tool_use` | `tool_calls` |
| `max_tokens` | `length` |
| `stop_sequence` | `stop` |
| `refusal` | `content_filter` |
| `model_context_window_exceeded` | `length` |
| 未知 | `stop`（fallback） |

**Codex**：直接由 `_normalize_codex_response()` 算（基于 `response.status` + output items 内容），值域已经是 OpenAI 风格（`stop` / `tool_calls` / `length` / `incomplete`）。

## 3. 流式输出协议对照

三个 provider 流式返回的事件结构都不一样，但 phalanx 统一收成同一个 `SimpleNamespace`。

### 3.1 OpenAI Chat Completions 流（`_accumulate_stream`）

**事件单位**：每个 SSE chunk 是一个 ChatCompletionChunk，含 `choices[0].delta`。

**关键字段**：
- `delta.content` — 文本切片（逐 token）
- `delta.tool_calls[i]` — tool call 切片，**索引化**（`tc_delta.index`）
  - `tc_delta.id` — 一般出现在第一个切片
  - `tc_delta.function.name` — 一般出现在第二个切片
  - `tc_delta.function.arguments` — 之后一直追加，一字符一字符切

**还原逻辑**（`run_agent.py:194`）：
```python
tool_calls_acc: dict[int, dict] = {}
for chunk in stream:
    delta = chunk.choices[0].delta
    if delta.content:
        callback(delta.content)
    for tc_delta in delta.tool_calls or []:
        slot = tool_calls_acc.setdefault(tc_delta.index, {"id": None, "name": "", "arguments_parts": []})
        # 各字段独立追加
```

**回调时机**：每收到一个 `delta.content` 立刻 fire；tool_call 切片不向 callback 转发（参数 JSON 拼到一半给 UI 没意义）。

### 3.2 Anthropic Messages 流（`_accumulate_anthropic_stream`）

**事件单位**：context manager `with client.messages.stream(**kw) as stream:` 加 `for event in stream:`。

**事件类型表**：

| event.type | 用途 | phalanx 处理 |
|---|---|---|
| `message_start` | 开头元信息 | 跳过 |
| `content_block_start` | block 开头（text / tool_use / thinking） | 跳过 |
| `content_block_delta` + `delta.type == "text_delta"` | 文本切片 | callback(delta.text) |
| `content_block_delta` + `delta.type == "input_json_delta"` | tool_use 参数切片 | 跳过（SDK 还原） |
| `content_block_delta` + `delta.type == "thinking_delta"` | 思考链切片 | 跳过（reasoning 留给后续 wave） |
| `content_block_stop` / `message_stop` | 收尾 | 跳过 |

**回调时机**：仅 `text_delta` 转发。

**还原逻辑**：依赖 SDK 的 `stream.get_final_message()` —— 它内部把 `tool_use` block 的 input_json_delta 拼回完整 JSON，把 thinking_delta 拼回完整 thinking block。phalanx 出 with 块前调一次，然后过 `_anthropic_response_to_openai_shape`。

### 3.3 Codex Responses 流（`_accumulate_codex_stream`）

**事件单位**：context manager `with client.responses.stream(**kw) as stream:` 加 `for event in stream:`。

**事件类型表**：

| event.type | 用途 | phalanx 处理 |
|---|---|---|
| `response.created` / `response.in_progress` | 状态变更 | 跳过 |
| `response.output_item.added` | 新 output item 开始（message / function_call / reasoning） | 跳过 |
| `response.output_text.delta` | message 内的文本切片 | callback(event.delta) |
| `response.function_call_arguments.delta` | function_call 参数切片 | 跳过（SDK 还原） |
| `response.reasoning_summary_text.delta` | reasoning 切片 | 跳过（reasoning 留给后续 wave） |
| `response.output_item.done` / `response.completed` | 收尾 | 跳过 |
| `response.incomplete` / `response.failed` | 异常终止 | 跳过（SDK 抛错） |

**回调时机**：phalanx 用 substring 匹配 `"output_text.delta" in event.type`，兼容带 / 不带 `response.` 前缀的 backend（chatgpt.com 子路径偶尔丢前缀）。

**还原逻辑**：SDK 的 `stream.get_final_response()` 还原所有 output items；phalanx 出 with 块前调一次，然后过 `_codex_response_to_openai_shape`。

### 3.4 错误处理

三个 accumulator 都把 callback 包在 `try/except`：
```python
try:
    callback(delta_text)
except Exception:
    logger.exception("... stream callback failed; continuing accumulation")
```

UI 崩了不能让流断——继续累积，最后还能拿到完整响应。

## 4. 测试 stub 模式

每个 provider 有自己的 stub fixture，结构对称（`tests/conftest.py`）：

| Fixture | 调度 | 双队列 | patch target |
|---|---|---|---|
| `stub_openai` | `chat.completions.create` | 单队列（`FakeResponse`） | `run_agent.OpenAI` |
| `stub_anthropic` | `messages.create` / `messages.stream` | 双队列：`FakeAnthropicResponse` 走 create，`(events, final)` tuple 走 stream | `agent.anthropic_adapter.build_anthropic_client` |
| `stub_codex` | `responses.create` / `responses.stream` | 双队列：`FakeCodexResponse` 走 create，`(events, final)` tuple 走 stream | `run_agent.OpenAI` |

**双队列 + assertion 防混用**：stub 进 `create` 时检查 next item 是不是 streaming tuple，反之亦然；不匹配抛 AssertionError 直接报错——不会让测试静默走错路径。

**streaming 测试模式**：
```python
events = [
    make_anthropic_text_delta_event("hi "),
    make_anthropic_text_delta_event("there"),
]
final = make_anthropic_text_response("hi there")
stub = stub_anthropic([(events, final)])

# 跑 cli_main(["--provider", "anthropic", "oneshot", "--stream", ...])
# 断言 captured.out == "hi there\n"
# 断言 stub.stream_calls / stub.calls 长度
```

`FakeAnthropicStream` / `FakeCodexStream` 实现 context manager + iterable + `get_final_message()` / `get_final_response()`，跟真实 SDK 接口完全一致——helper 不知道是不是被 stub 替换了。

## 5. CLI 暴露面

```bash
# 检测哪个 provider 当前 active
hermes provider list

# Force 指定 provider（顶层 flag，覆盖自动检测）
hermes --provider anthropic oneshot "..."
hermes --provider codex oneshot "..."

# 强制流式 / 非流式（oneshot 子命令的 mutex group）
hermes oneshot --stream "..."
hermes oneshot --no-stream "..."

# 发 ping 测连通性（每个 provider 走自己的 SDK）
hermes provider test openai-compatible
hermes provider test anthropic
hermes provider test codex
```

`provider list` 状态文案随 wave 进度更新：

```
adapters:
  openai-compatible    wired (chat.completions, streaming)
  anthropic            wired (messages.create + messages.stream)
  codex                wired (responses.create + responses.stream)  [active]
  bedrock              not yet ported
  gemini               not yet ported
```

## 6. 留给后续

按"代价 / 收益"排序：

1. **Bedrock adapter**（~50K 行）+ `_call_bedrock_converse` — 真要用 AWS Bedrock Claude / Llama 时再移；boto3 的 `converse_stream()` 跟 anthropic stream 协议又不一样
2. **Gemini adapter**（~70K 行 × 2 个文件）— Gemini 用自家 SDK，response shape 又是另一套
3. **`_build_assistant_message` 替换 `_serialize_tool_calls`** — 把 reasoning blocks（anthropic thinking / codex encrypted reasoning）写回 history，多轮思考链才能续；当前 phalanx 默认关 reasoning，不影响 round-trip 但失去思考链
4. **OAuth / fast_mode / context-1m / Claude Code 兼容路径** — anthropic adapter 都已 verbatim 包含，只是 `_call_anthropic_messages` 的 `is_oauth=False / fast_mode=False` 默认没接 CLI flag
5. **Codex backend 区分**（chatgpt.com / GitHub Models / xAI Grok）— `agent/transports/codex.py` 上游用 `is_codex_backend / is_github_responses / is_xai_responses` 三标志走不同的 cache key / extra_headers；phalanx 暂全走 native `/v1/responses`
6. **Interrupt-thread 机制** — 上游 `_interruptible_api_call` 把 SDK 调用放后台线程，主线程 poll `_interrupt_requested` 实现 `/stop` 即时打断；phalanx 同步调用，`/stop` 等 SDK 自然返回。gateway 模式才需要

§2.4 全 6 wave 完成后，phalanx 已具备日常本地三 provider（Ollama OpenAI-compat + Claude API + GPT-5 Responses）的完整 round-trip + streaming 能力，CLI 单次调用足够覆盖大多数场景。
