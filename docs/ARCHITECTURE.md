# Phalanx 架构说明（current state）

> 本文描述**当前已实现**的代码结构与运行时数据流；与之配对的**前瞻规划**见 [`MIGRATION_PLAN.md`](MIGRATION_PLAN.md)。
> 各 phase 的设计推导见独立设计文档：
> - [`phase-2.0-skeleton.md`](phase-2.0-skeleton.md) — 项目骨架 + 方案 B env 隔离
> - [`phase-2.1-minimal-loop.md`](phase-2.1-minimal-loop.md) — `AIAgent` 最小 loop + 最小 CLI + tool registry
> - [`phase-2.2-tools.md`](phase-2.2-tools.md) — 真实工具栈落地（file / todo / web / result-storage）
> - [`phase-2.3-prompt-context.md`](phase-2.3-prompt-context.md) — prompt 系统 / API retry / model metadata / pricing / trajectory
> - [`phase-2.4-multi-provider.md`](phase-2.4-multi-provider.md) — anthropic / codex provider + 流式 + `_make_api_call` 分发器
> - [`phase-2.5-sessions.md`](phase-2.5-sessions.md) — `SessionDB` / `--resume` / session CLI / logs
> - [`phase-2.6-repl.md`](phase-2.6-repl.md) — prompt_toolkit REPL + 斜杠命令 + tips
>
> 当前进度：**Phase 2.6 wave 3 完成**——交互式 REPL（prompt_toolkit + 斜杠命令 + 流式渲染）+ SessionDB 持久化 + multi-provider（OpenAI / Anthropic / Codex Responses）+ 流式输出 + 9 个内置工具齐备。已注册工具：`echo` `read_file` `write_file` `patch` `search_files` `terminal` `todo` `web_search` `web_extract`。

## 1. 一图看懂

```
                                        ┌───────────────────────────────┐
                       CLI subcommands  │  hermes_cli/main.py           │
   user terminal ─────────────────────► │   (argparse dispatcher)       │
                                        │  cmd_oneshot / cmd_chat /     │
                                        │  cmd_tools_list /             │
                                        │  cmd_tools_run / cmd_doctor / │
                                        │  cmd_config_show / ...        │
                                        └────────────┬──────────────────┘
                                                     │  delegate_chat
                                                     ▼
                                        ┌───────────────────────────────┐
                                        │  cli.py:main(...)              │
                                        │  §2.6 prompt_toolkit REPL or  │
                                        │  oneshot                      │
                                        └────────────┬──────────────────┘
                                                     │  AIAgent(...)
                                                     ▼
   ┌──── per-turn loop ────────────────────────────────────────────────────┐
   │                                                                       │
   │  ┌────────────────┐    1. build api_kwargs   ┌─────────────────────┐  │
   │  │ AIAgent        │ ────────────────────────►│  _make_api_call     │  │
   │  │ run_conversa-  │                           │  dispatcher (§2.4): │  │
   │  │ tion()         │ ◄──── 2. response ───────│  openai/anthropic/  │  │
   │  └──────┬─────────┘                           │  codex responses    │  │
   │         │                                     └─────────────────────┘  │
   │         │ 3. dispatch tool_calls                                       │
   │         ▼                                                              │
   │  ┌────────────────┐                          ┌─────────────────────┐  │
   │  │ tools.registry │ ── registry.dispatch ──► │ echo / file / todo  │  │
   │  │ singleton      │                           │ terminal / web /... │  │
   │  └────────────────┘                          └──────────┬──────────┘  │
   │         ▲                                                │             │
   │         │ 4a. wrap result via maybe_persist_tool_result  │             │
   │         │    (spill to sandbox if > 100 KB)              ▼             │
   │         │                                    ┌─────────────────────┐  │
   │         └──────────── tool_result_storage ◄──┤ LocalTerminalEnv    │  │
   │                                              │ (sandbox writes)    │  │
   │                                              └─────────────────────┘  │
   │                                                                       │
   │  4b. enforce_turn_budget across the whole batch (200 KB aggregate)    │
   │  5.  append tool result, loop again until no tool_calls or budget end │
   └───────────────────────────────────────────────────────────────────────┘
```

## 2. 目录结构（实际落盘文件）

```
phalanx/
├── pyproject.toml                  pip 元数据 + 依赖 + ruff/pytest 配置 + 2 个 console_script
├── MANIFEST.in                     sdist 打包清单（极简）
├── .gitignore                      build/dist/cache/log 排除
├── README.md                       项目入口指引
│
├── run_agent.py                    AIAgent + IterationBudget + OpenAI 代理 + def main 旁路 CLI
├── cli.py                          §2.6 prompt_toolkit REPL（被 hermes_cli/main 委托）
├── hermes_constants.py             路径/平台常量（PHALANX_HOME 等）
├── hermes_logging.py               日志工厂（session_tag、redact、rotation）
├── hermes_time.py                  时区辅助（PHALANX_TIMEZONE）
├── hermes_state.py                 §2.5 SessionDB：SQLite 会话持久化（schema + CRUD + resume）
├── utils.py                        atomic_*_write、env helpers、URL 解析
│
├── agent/                          AIAgent 内部支持模块
│   ├── __init__.py
│   ├── retry_utils.py              jittered_backoff（§2.1）
│   ├── error_classifier.py         classify_api_error / FailoverReason（§2.1）
│   ├── file_safety.py              get_read_block_error（§2.2 wave 1）
│   ├── redact.py                   redact_sensitive_text（§2.2 wave 1）
│   ├── auxiliary_client.py         §2.2 wave 4 shim（4 个符号，3914→89 行）
│   │
│   │  ── §2.3：prompt 系统 + model 元数据 ──
│   ├── prompt_builder.py           system prompt 渲染 + 注入
│   ├── prompt_caching.py           Anthropic prompt cache 控制
│   ├── memory_manager.py           §2.3 wave 3 shim（StreamingContextScrubber 等）
│   ├── context_compressor.py       §2.3 wave 3 shim（compress 入口）
│   ├── subdirectory_hints.py       SubdirectoryHintTracker
│   ├── model_metadata.py           模型能力查询（max_tokens / 工具支持等）
│   ├── usage_pricing.py            estimate_usage_cost / normalize_usage
│   ├── trajectory.py               turn-level trajectory 记录
│   │
│   │  ── §2.4：multi-provider ──
│   ├── anthropic_adapter.py        anthropic Messages API 适配
│   └── codex_responses_adapter.py  codex Responses API 适配（OpenAI o-series）
│
├── tools/                          工具系统
│   ├── __init__.py                 静态 import 触发各工具自注册
│   ├── registry.py                 ToolRegistry 单例 + ToolEntry + tool_error/tool_result
│   ├── echo_tool.py                Phase 1 smoke 工具（phalanx 专属）
│   │
│   │  ── §2.2 wave 1：文件 / 终端 ──
│   ├── file_tools.py               read_file / write_file / patch / search_files 入口
│   ├── file_operations.py          ShellFileOperations：sed/head/tail/grep 后端
│   ├── path_security.py            敏感系统路径黑名单
│   ├── binary_extensions.py        二进制扩展名集合
│   ├── fuzzy_match.py              patch 用的 9 策略模糊匹配
│   ├── tool_output_limits.py       max_lines / max_line_length 配置入口
│   ├── terminal_tool.py            §2.2 minimal LocalTerminalEnv（自动 spill 长 cmd）
│   ├── file_state.py               cross-tool 读写时间戳（Phase 1 no-op shim）
│   │
│   │  ── §2.2 wave 2 ──
│   ├── todo_tool.py                单个 todo 工具，per-AIAgent TodoStore
│   │
│   │  ── §2.2 wave 4：web ──
│   ├── web_tools.py                web_search / web_extract（多 backend）
│   ├── url_safety.py               IP/SSRF 黑白名单
│   ├── website_policy.py           域名级访问策略
│   ├── managed_tool_gateway.py     Nous gateway 路由
│   ├── tool_backend_helpers.py     managed_nous_tools_enabled / prefers_gateway
│   ├── debug_helpers.py            DebugSession 调试日志
│   ├── interrupt.py                per-thread 中断信号（提前 wave 4 引入）
│   │
│   │  ── §2.2 wave 5：context budget ──
│   ├── budget_config.py            BudgetConfig + DEFAULT_RESULT_SIZE_CHARS
│   ├── tool_result_storage.py      maybe_persist_tool_result + enforce_turn_budget
│   │
│   │  ── §2.4：tool schema 兼容层 ──
│   └── schema_sanitizer.py         provider 间 JSON Schema 差异修平
│
├── hermes_cli/                     CLI 入口与子命令
│   ├── __init__.py                 __version__ + Windows utf-8 setup
│   ├── __main__.py                 python -m hermes_cli 入口
│   ├── main.py                     argparse 分发器（oneshot/chat/tools/config/version/doctor/session/logs）
│   ├── _parser.py                  argparse 辅助（来自 upstream）
│   ├── env_loader.py               load_hermes_dotenv：~/.phalanx/.env + 项目 .env
│   ├── config.py                   cfg_get / cfg_set / load_config / save_config
│   ├── timeouts.py                 get_provider_request_timeout 等
│   ├── banner.py                   ASCII art / 版本横幅
│   ├── colors.py                   ANSI 色码常量
│   ├── cli_output.py               print 包装、密码读取
│   ├── commands.py                 §2.6 wave 2：CommandDef + COMMAND_REGISTRY + SlashCommandCompleter
│   ├── tips.py                     §2.6 wave 3：21 条 REPL 启动提示
│   └── logs.py                     §2.5 wave 5：hermes logs 子命令
│
├── tests/                          pytest 套件（约 130 用例）
│   ├── __init__.py
│   ├── conftest.py                 StubClient + stub_openai + reset_echo_call_count
│   ├── test_minimal_loop.py        §2.1 — 18 用例：IterationBudget / __init__ / run_conversation / 工具序列化
│   ├── test_cli_oneshot.py         §2.1 — 10 用例：version/doctor/config/oneshot/--debug
│   ├── test_cli_tools.py           §2.1+§2.2 — 15 用例：tools list/run/schema/dry-run
│   ├── test_session_db.py          §2.5 wave 1 — SessionDB CRUD
│   ├── test_session_db_integration.py §2.5 wave 2 — run_conversation 持久化集成
│   ├── test_cli_resume.py          §2.5 wave 3 — --resume CLI flag
│   ├── test_cli_session.py         §2.5 wave 4 — session list/show/dump/delete
│   ├── test_cli_logs.py            §2.5 wave 5 — logs 子命令
│   ├── test_cli_repl.py            §2.6 wave 1 — REPL 路径 smoke
│   ├── test_cli_commands.py        §2.6 wave 2 — CommandDef / completer / dispatch alias
│   └── test_cli_handlers.py        §2.6 wave 3 — 34 用例：每个 _cmd_* handler + 流式 _run_turn
│
├── docs/
│   ├── ARCHITECTURE.md             本文
│   ├── MIGRATION_PLAN.md           分期移植路线
│   └── guides/
│       ├── build-vs-setup-py.md    打包工具链对比
│       └── ci-cd.md                CI/CD 工作流原理
│
└── .github/workflows/ci.yml         GitHub Actions：lint + import + pytest + build
```

## 3. 模块职责矩阵

| 文件 | 职责 | 行数 | 上游对照 |
|---|---|---:|---|
| `run_agent.py` | `AIAgent` orchestrator、`IterationBudget`、`OpenAI` lazy proxy、`_make_api_call` provider 分发器、`def main` 旁路 CLI | ~1200 | upstream 14123 行裁剪 |
| `cli.py` | §2.6 prompt_toolkit REPL：`PromptSession` + `FileHistory(~/.hermes/cli_history)` + `SlashCommandCompleter` + `patch_stdout` 流式渲染 + 11 个斜杠 handler | ~580 | upstream 12043 行裁剪 |
| `hermes_cli/main.py` | argparse 顶层分发：`oneshot` / `chat` / `tools` / `config` / `version` / `doctor` / `session` / `logs` 8 子命令；flat 结构（global flag 在 top-level） | ~700 | upstream 10439 行裁剪 |
| `hermes_state.py` | §2.5 `SessionDB`：SQLite schema + `add_message` / `set_session_title` / `list_sessions` / `get_messages_as_conversation` / `resolve_session_id` / `reopen_session` 等 9+ CRUD | ~1300 | upstream 2248 行裁剪 |
| `hermes_cli/commands.py` | §2.6 `CommandDef` dataclass + `COMMAND_REGISTRY`（裁剪到 ~18 项）+ `resolve_command` + `SlashCommandCompleter` | ~270 | upstream ~700 行裁剪（去掉 telegram/discord/slack/gateway 命令） |
| `hermes_cli/tips.py` | §2.6 wave 3：21 条 REPL 启动随机提示 | 60 | verbatim 裁剪 |
| `hermes_cli/logs.py` | §2.5 wave 5：`hermes logs <session_id> [--follow] [--level]` | ~150 | phalanx minimal |
| `tools/registry.py` | `ToolRegistry` 单例、`ToolEntry`、register/dispatch/`get_definitions`/`get_all_tool_names`/`get_schema`/`get_toolset_for_tool` | ~547 | near-verbatim（model_tools 1 处 lazy fallback；budget_config 已对齐上游） |
| `tools/echo_tool.py` | smoke 工具，自注册 `echo` | ~83 | phalanx 专属 |
| `tools/file_tools.py` | `read_file` / `write_file` / `patch` / `search_files` 入口；resolve_path、device 黑名单、二进制守卫、敏感路径检查 | 1143 | verbatim |
| `tools/file_operations.py` | `ShellFileOperations`：把工具调用转成 shell 命令（sed/head/tail/grep/rg），返回 `SearchResult` 等结构 | 1287 | verbatim |
| `tools/fuzzy_match.py` | `fuzzy_find_and_replace`，patch 工具用的 9 策略模糊匹配 | 704 | verbatim |
| `tools/path_security.py` / `binary_extensions.py` / `tool_output_limits.py` | 敏感路径前缀、二进制扩展集合、`get_max_lines`/`get_max_line_length` 配置入口 | 43 / 42 / 92 | verbatim |
| `tools/terminal_tool.py` | `LocalTerminalEnv.execute`（subprocess + bash），module-level globals (`_active_environments`, `_env_lock`, `_creation_locks`, …) 给 file_tools 用；自注册 `terminal` 工具 | ~250 | phalanx minimal（上游 ~3000 行涵盖 docker/ssh/modal 多 backend，本期只接 local） |
| `tools/file_state.py` | cross-tool 读写时间戳追踪（warn stale read / serialize concurrent writes） | 73 | phalanx no-op shim（upstream 332 行） |
| `tools/todo_tool.py` | `TodoStore`（per-AIAgent）+ 单个 `todo` 工具，merge/replace 两种写入语义 | 277 | verbatim |
| `tools/web_tools.py` | `web_search` / `web_extract` 入口，多 backend（Firecrawl / Tavily / Exa / Parallel）；page-level LLM 摘要可选 | 2153 | verbatim |
| `tools/url_safety.py` / `website_policy.py` | IP/SSRF 黑白名单、域名级访问策略 | 231 / 282 | verbatim |
| `tools/managed_tool_gateway.py` / `tool_backend_helpers.py` / `debug_helpers.py` | Nous gateway 路由、`managed_nous_tools_enabled` 等开关、`DebugSession` 调试日志 | 167 / 144 / 105 | verbatim |
| `tools/interrupt.py` | per-thread 中断信号（`is_interrupted()`），用于工具内部协作中断 | 98 | verbatim |
| `tools/budget_config.py` | `BudgetConfig` dataclass + `DEFAULT_BUDGET` 单例，常量 `DEFAULT_RESULT_SIZE_CHARS=100K` / `DEFAULT_TURN_BUDGET_CHARS=200K` / `DEFAULT_PREVIEW_SIZE_CHARS=1.5K` | 52 | verbatim |
| `tools/tool_result_storage.py` | `maybe_persist_tool_result`（layer 2 单结果落盘）+ `enforce_turn_budget`（layer 3 整轮预算）+ `<persisted-output>` preview block 构造 | 226 | verbatim |
| `agent/file_safety.py` | `get_read_block_error`：拒绝某些路径的读，给模型清晰指引 | 111 | verbatim |
| `agent/redact.py` | `redact_sensitive_text`：从工具返回里抹掉 token / API key / SSN 等敏感字串 | 394 | verbatim |
| `agent/auxiliary_client.py` | LLM 摘要客户端（OpenRouter Gemini 等）；shim 让 web_tools 走"返回原文"降级路径 | 89 | phalanx minimal shim（upstream 3914 行） |
| `agent/retry_utils.py` | `jittered_backoff(attempt)` | 57 | verbatim |
| `agent/error_classifier.py` | `classify_api_error(error, ...)` + `FailoverReason` enum | 1000 | verbatim |
| `agent/prompt_builder.py` | §2.3：system prompt 渲染、注入、节点拼接 | — | verbatim |
| `agent/prompt_caching.py` | §2.3：Anthropic prompt cache 控制（cache_control 标记） | — | verbatim |
| `agent/memory_manager.py` / `context_compressor.py` | §2.3 wave 3 shim：保留 `StreamingContextScrubber` / `compress` 等公开符号，body no-op | — | phalanx shim |
| `agent/subdirectory_hints.py` | `SubdirectoryHintTracker`：跨 turn 追踪用户工作目录的语义提示 | — | verbatim |
| `agent/model_metadata.py` | 模型能力查询（`max_tokens` / 工具支持 / 流式支持）+ provider 推断 | — | verbatim |
| `agent/usage_pricing.py` | `estimate_usage_cost` / `normalize_usage` | — | verbatim |
| `agent/trajectory.py` | turn-level trajectory 记录（每轮 prompt / tool_calls / tokens） | — | verbatim |
| `agent/anthropic_adapter.py` | §2.4 wave 2：Anthropic Messages API 适配（消息 format 转换 + 流式 delta 拼装） | — | verbatim |
| `agent/codex_responses_adapter.py` | §2.4 wave 5：OpenAI o-series Responses API 适配（reasoning summary + 流式 chunk） | — | verbatim |
| `tools/schema_sanitizer.py` | §2.4：跨 provider 的 JSON Schema 兼容修平（去掉 anthropic 不支持的字段等） | — | verbatim |
| `hermes_constants.py` | `get_hermes_home`、`get_config_path`、`is_termux/is_wsl/is_container` | 345 | verbatim + 方案 B 路径/env 改名 |
| `hermes_logging.py` | `setup_logging`、`set_session_context`、`_install_session_record_factory` | 389 | verbatim |
| `hermes_time.py` | `now()` 时区感知 datetime | 104 | verbatim + env 改名 |
| `utils.py` | `atomic_json_write` / `atomic_yaml_write` / `base_url_hostname` | 297 | verbatim |
| `hermes_cli/config.py` | `cfg_get` / `cfg_set` / `load_config` / `save_config` / `is_managed`（永远 False） | 206 | upstream 4831 行裁剪 |

## 4. 控制流：一次 oneshot 调用的全链路

以 `python -m hermes_cli --debug oneshot "echo hi"` 为例（PHALANX 已配置 Ollama）：

```
1. shell  ─►  python -m hermes_cli --debug oneshot "echo hi"

2. hermes_cli/__main__.py:
     from hermes_cli.main import main
     sys.exit(main())            # argv 默认走 sys.argv

3. hermes_cli/main.py:main():
     parser = _build_parser()
     args = parser.parse_args(argv)   # args.debug=True, args.cmd='oneshot', args.message='echo hi'
     _Flags.debug = True              # 落到全局 _Flags 容器
     _setup_logging(debug=True)       # logging.basicConfig(DEBUG, stderr)
     _load_dotenv_best_effort()       # 加载 ~/.phalanx/.env → OPENAI_API_KEY
     args.func(args)                  # = cmd_oneshot(args)

4. hermes_cli/main.py:cmd_oneshot():
     agent = _build_agent(args, ...)  # 构造 AIAgent
     result = agent.run_conversation(msg)
     print(result['final_response'])
     return 0

5. _build_agent():
     cfg = load_config()              # 读 ~/.phalanx/config.yaml
     model = _Flags.model
            or os.environ.get('PHALANX_MODEL')
            or os.environ.get('OPENAI_MODEL')
            or cfg_get(cfg, 'model', 'default')   # ← qwen2.5:1.5b
     base_url = _Flags.base_url
            or os.environ.get('OPENAI_BASE_URL')
            or cfg_get(cfg, 'model', 'base_url')  # ← http://localhost:11434/v1
     api_key = _Flags.api_key
            or os.environ.get('OPENAI_API_KEY')   # ← 'ollama'（来自 .env）
     return AIAgent(base_url, api_key, model, ..., verbose_logging=True)

6. AIAgent.__init__():
     self.iteration_budget = IterationBudget(90)
     self._tool_registry = _load_tool_registry()  # = tools.registry.registry
     # → import tools 触发 echo_tool 自注册
     # 此时 self._tool_registry 已含 'echo'

7. agent.run_conversation('echo hi'):
     messages = [{'role':'system', ...}, {'role':'user', 'content':'echo hi'}]
     tools = self._resolve_tool_schemas()
     # → registry.get_all_tool_names() = ['echo']
     # → registry.get_definitions({'echo'}) = [{'type':'function','function':{...}}]

     while iteration_budget.remaining > 0:
         iteration_budget.consume()
         response = self._make_api_call(messages, tools)
         # ↳ dispatcher → _call_chat_completions / _call_anthropic_messages /
         #   _call_codex_responses based on self.provider; OpenAI-compat path:
         #   POST http://localhost:11434/v1/chat/completions
         #   { model: 'qwen2.5:1.5b', messages, tools: [...] }

         msg = response.choices[0].message
         if msg.tool_calls:
             messages.append({'role':'assistant','tool_calls': [...]})
             for tc in msg.tool_calls:
                 result = registry.dispatch('echo', {'text':'hi'})
                 # = echo_tool.echo({'text':'hi'}) → '{"text":"hi","call_count":1}'
                 messages.append({'role':'tool','tool_call_id':tc.id,'content':result})
             continue
         else:
             messages.append({'role':'assistant','content':msg.content})
             return {'final_response': msg.content, 'messages': messages, ...}
```

终态 `messages` 角色序列：`['system','user','assistant','tool','assistant']`（最少 2 turn）。

### 4.1 IterationBudget — 跨 agent 的执行预算

主循环里的 `while iteration_budget.remaining > 0` 与 `iteration_budget.consume()` 不是简单计数器——是 agent 系统级的"模型调用配额表"。

```python
# run_agent.py:119
class IterationBudget:
    max_total: int              # 上限（默认 90）
    _used: int                  # 已用计数
    _lock: threading.Lock       # 跨线程同步

    consume() -> bool           # 抢一个名额，True=允许，False=已满
    refund() -> None            # 退还（出错重试时偶尔用）
    used / remaining            # 只读属性
```

每跑一轮 `chat.completions` 就 `consume()` 一次；用完则主循环退出，`stop_reason="budget_exhausted"`。一轮 = 一次模型 HTTP + 0~N 次工具调用，整体只扣 1 格。

`__init__` 提供 `iteration_budget: Optional[IterationBudget]` 参数：子 agent 派生时**把父级实例直接传过去**所有 agent 共享同一个计数器，`threading.Lock` 保证跨线程安全。`self.max_iterations` 是当前实例硬上限，`self.iteration_budget` 是可继承预算池——主循环条件两者都要满足，谁先到先停。

phalanx 当前还没移植 `delegate_task`，每个 `AIAgent` 都自己 new 一个 `IterationBudget`，等价于一个简单的 90 轮上限；接口已经留好——后续接入子 agent 派生时一行传参就接通。返回值 `iterations_used`（`run_agent.py:652`）向调用方透出实际烧了几轮，便于成本观测。

> 设计推导（为什么独立成类、防失控循环、可继承全局额度、重试不重复扣费）见 [`phase-2.1-minimal-loop.md`](phase-2.1-minimal-loop.md) §2。

## 5. 工具系统

### 注册（Phase 1：静态）

`tools/__init__.py` 在 import 时**显式 import** 每个内置工具模块：

```python
# tools/__init__.py
from tools import registry           # 暴露子模块
from tools import echo_tool          # Phase 1 smoke 工具
from tools import terminal_tool      # §2.2 wave 1：file_tools 的 LocalTerminalEnv 后端
from tools import file_tools         # §2.2 wave 1：read_file / write_file / patch / search_files
from tools import todo_tool          # §2.2 wave 2：in-memory task list
from tools import web_tools          # §2.2 wave 4：web_search / web_extract
```

每个工具文件顶层调一次 `registry.register(name, toolset, schema, handler, ...)`。**不再有别处主动加载工具**——只要 `import tools` 跑过，单例就装满。

`terminal_tool` 必须在 `file_tools` 之前 import：file_tools 的 `get_or_create_file_ops` 用到 terminal_tool 的 module-level 全局（`_active_environments` / `_env_lock` 等）。其他工具之间无顺序依赖。

```python
# tools/echo_tool.py 末尾
registry.register(
    name="echo",
    toolset="echo",
    schema=ECHO_SCHEMA,
    handler=echo,
    check_fn=check_echo_requirements,
    emoji="🔁",
)
```

### 调度（runtime）

```
AIAgent.run_conversation
    │
    ├─ _resolve_tool_schemas()
    │     ├─ registry.get_all_tool_names()     # ['echo', ...]
    │     ├─ 按 enabled_toolsets / disabled_toolsets 过滤
    │     └─ registry.get_definitions(set(names), quiet=...)
    │           # 内部调每个 entry 的 check_fn (TTL-cache 30s)
    │           # 返回 [{'type':'function','function':schema_with_name},...]
    │
    └─ when assistant returns tool_calls:
        │
        └─ registry.dispatch(name, args_dict, **kw)
              ├─ entry = get_entry(name)
              ├─ if is_async: asyncio.run(entry.handler(...))
              ├─ else: entry.handler(args, **kw)
              └─ except: return JSON error string
```

### check_fn 缓存

`tools/registry.py:_check_fn_cached` 维护一个 TTL=30s 的 `{check_fn: (timestamp, bool)}` cache，避免每次 `get_definitions` 都对 check_fn 进行 IO 探测（如 ping docker / 检查 playwright 安装）。echo 的 `check_echo_requirements` 永远返回 True。

### 当前缺什么

- 异步 handler：`registry.dispatch` 已支持 `is_async=True`（web_extract 就用了），但回退到 `asyncio.run`，不支持外层已有运行中事件循环的嵌套场景。`model_tools._run_async` 桥 Phase 7+ 落地。
- `discover_builtin_tools()`：upstream 用 AST 自动扫 `tools/*.py` 是否含 `registry.register(...)` 顶层调用。phalanx 用静态 import 替代——更明确，但加新工具要改 `__init__.py`。
- 派生 / delegate：`delegate_task` 工具（fork 子 agent）尚未引入。`AIAgent.iteration_budget` 已经设计为可继承（见 §4.1），等 §2.4+ 接入即可零结构变更地共享预算。

> 静态 import vs 上游 AST discover、两处 lazy fallback、`check_fn` TTL 缓存的设计推导见 [`phase-2.1-minimal-loop.md`](phase-2.1-minimal-loop.md) §3。

## 6. 配置与环境

### 三层覆盖

```
       优先级高 ──────────────────────────────────► 优先级低
   CLI flag       env var (PHALANX_* / OPENAI_*)        ~/.phalanx/config.yaml
   --model               PHALANX_MODEL                   model.default
   --base-url            OPENAI_BASE_URL                 model.base_url
   --api-key             OPENAI_API_KEY / PHALANX_API_KEY    (尚未支持)
   --debug                       –                              –
```

实现位于 `hermes_cli/main.py:_build_agent()` 与 `cli.py:_build_agent()`（两处一致）。

### 路径解析（方案 B）

`hermes_constants.get_hermes_home()`：

```python
val = os.environ.get("PHALANX_HOME", "").strip()
return Path(val) if val else Path.home() / ".phalanx"
```

**关键**：`HERMES_HOME` env 变量**不再被 phalanx 识别**——避免与系统已装的 hermes-agent 数据互相污染。详见 `MIGRATION_PLAN.md §1.5 / §5`。

### .env 加载

`hermes_cli/env_loader.load_hermes_dotenv(hermes_home, project_env)`：

1. 先加载 `~/.phalanx/.env`（用户级密钥）
2. 再加载 `<project>/.env`（项目级覆盖，dev 友好）
3. 内部用 `python-dotenv.load_dotenv()` + `utils.atomic_replace` 安全写

CLI 启动时由 `_load_dotenv_best_effort()` 自动调用一次。

## 7. CLI 路由（与 upstream 一致的 flat argparse）

```
hermes [<global flags>] <subcmd> [<subcmd flags>] [<positional>]

global flags（top-level only，子命令前）:
   --debug   --quiet   --model   --base-url   --api-key   --provider   --resume

subcommands:
   oneshot     <message>     [--system …] [--max-iterations N] [--max-tokens N] [--stream]
   chat                       [--system …] [--max-iterations N]                    # → cli.py REPL
   tools list                 [--verbose]
   tools run    <name>        --args '<json>'
   tools schema <name>                                                              # §2.2 wave 6
   tools dry-run <name>       --args '<json>'                                       # §2.2 wave 6
   session list               [--limit N]                                           # §2.5 wave 4
   session show  <id-prefix>
   session dump  <id-prefix>  [--format json|text]
   session delete <id-prefix>
   logs         <id-prefix>   [--follow] [--level INFO|DEBUG]                       # §2.5 wave 5
   config show
   config get   <dot.key>
   version
   doctor

REPL 内斜杠命令（§2.6 wave 3）:
   /help /clear /new /history /quit /exit /model /tools /debug /save /resume
```

**位置敏感**：因为 upstream 不用 `parents=[]` 给子命令注入 global flag，phalanx 也保持 flat。结果：

```bash
hermes --debug oneshot "..."        ✓
hermes oneshot --debug "..."        ✗  argparse: unrecognized argument
```

`hermes_cli/main.py:cmd_chat` 通过 `from cli import main as cli_main` 委托 `cli.py`，复刻 upstream `hermes_cli/main.py:1313` 的调用模式。

> flat argparse 的撤销决策（为何放弃 `parents=[common]` 给子命令注入全局 flag、cherry-pick 友好 vs UX 友好的取舍）见 [`phase-2.1-minimal-loop.md`](phase-2.1-minimal-loop.md) §5.3。

## 8. 三个入口

| 入口 | 文件 | 调用模式 | 用途 |
|---|---|---|---|
| `hermes` | `hermes_cli/main.py:main` | `pyproject.toml` 注册的 console_script | 用户主入口（argparse） |
| `hermes-agent` | `run_agent.py:main` | `pyproject.toml` 注册的 console_script | 旁路：直接 fire 进 AIAgent，bypass argparse |
| `cli.py` | 非注册入口 | 被 `hermes_cli/main.py:cmd_chat` 委托；也能 `python cli.py` 直跑 | 交互式 REPL 实现 |

## 9. 测试策略

| 文件 | 引入 phase | 关键 fixture / 模式 |
|---|---|---|
| `tests/conftest.py` | §2.1 | `stub_openai(responses) → StubClient`：monkeypatch `run_agent.OpenAI` 为返回固定 response 的工厂；`reset_echo_call_count` autouse 清隔离 |
| `tests/test_minimal_loop.py` | §2.1 | IterationBudget 单元、AIAgent 构造、run_conversation 全闭环（mock 模型 + 真 registry + 真 echo handler） |
| `tests/test_cli_oneshot.py` | §2.1 | argparse 入口、doctor、config show/get、oneshot 单 turn / 双 turn / 缺 message / `--debug` 输出 |
| `tests/test_cli_tools.py` | §2.1 + §2.2 | tools list/run 标准+错误分支、§2.2 wave 6 加 schema/dry-run 验证（含 dry-run 不写盘断言） |
| `tests/test_session_db.py` | §2.5 wave 1 | `SessionDB` schema 创建、9 个 CRUD 方法、content 编码 / 重新打开 |
| `tests/test_session_db_integration.py` | §2.5 wave 2 | `run_conversation` 走 SessionDB 持久化端到端、消息批量 flush |
| `tests/test_cli_resume.py` | §2.5 wave 3 | `--resume <id>` 恢复历史、id 前缀解析 |
| `tests/test_cli_session.py` | §2.5 wave 4 | `session list/show/dump/delete` |
| `tests/test_cli_logs.py` | §2.5 wave 5 | `logs <id>` + `--follow` + `--level` 过滤 |
| `tests/test_cli_repl.py` | §2.6 wave 1 | REPL skeleton smoke、prompt_toolkit 退到 `input()` 路径 |
| `tests/test_cli_commands.py` | §2.6 wave 2 | `CommandDef` lookup、`SlashCommandCompleter` 补全、dispatch alias 解析 |
| `tests/test_cli_handlers.py` | §2.6 wave 3 | 34 用例：每个 `_cmd_*` handler + 流式 `_run_turn` + tips picker |

跑法：

```bash
pytest tests/                     # 全过 ~3s（约 130 用例）
pytest tests/ -v                  # verbose
pytest tests/test_minimal_loop.py::test_loop_dispatches_real_echo_tool   # 单条
```

CI（`.github/workflows/ci.yml`）每次 push/PR 自动跑 ruff + import smoke + pytest + build + twine check。

## 10. upstream 兼容契约

phalanx 的核心原则是"标识符严格保留，便于上游 cherry-pick"。具体落地：

- **文件名 / 类名 / 函数名**：与 hermes-agent 上游一致。`AIAgent` / `IterationBudget` / `OpenAI` / `_OpenAIProxy` / `_SafeWriter` / `cfg_get` / `cfg_set` / `load_config` / `ToolRegistry` / `ToolEntry` / `tool_error` / `tool_result` / `classify_api_error` / `FailoverReason` 等全部沿用
- **方法签名**：`run_conversation(user_message, system_message, conversation_history, task_id, stream_callback, persist_user_message)` 与 upstream 一致；多余参数 Phase 1 接受但忽略
- **环境变量名**：path/timezone/optional-skills 这类**用户数据归属**变量改为 `PHALANX_*` 防污染（方案 B）；upstream-only 的 `HERMES_QUIET` / `HERMES_INFERENCE_MODEL` / `HERMES_ACCEPT_HOOKS` 等运行时控制类暂未实现，将来 verbatim 接入
- **打包名 / console_script**：dist name 改为 `phalanx`；entry points 名字 `hermes` / `hermes-agent` 与 upstream 一致（避免 docs/scripts 引用断裂）
- **CLI argparse 结构**：flat（top-level 全局 flag、子命令各自重定义），与 upstream `hermes_cli/_parser.py:build_top_level_parser` 一致——**不用 `parents=[]`**

未来 cherry-pick upstream 修改时，主要操作是：

```bash
# 大部分修改可以直接 cherry-pick
git -C /path/to/hermes-agent format-patch HEAD~1 --stdout | git apply --3way

# 涉及 HERMES_HOME 字面量时一次性 sed
git apply --3way patch.diff
sed -i 's/HERMES_HOME/PHALANX_HOME/g; s/~\/\.hermes/~\/\.phalanx/g; s/HERMES_TIMEZONE/PHALANX_TIMEZONE/g' <(grep -rl HERMES_ phalanx/)
```

## 11. 演进时间线

phalanx 从空仓走到当前状态的所有 phase / wave 与对应 commit + 设计文档：

| Phase | 范围 | 关键 commits | 设计文档 |
|---|---|---|---|
| 2.0 | 项目骨架 + 方案 B env 隔离 + CI 工作流 | `ba41f00` `fdac96e` `32e24f1` `fd2ab63` | [`phase-2.0-skeleton.md`](phase-2.0-skeleton.md) |
| 2.1 | minimal AIAgent loop + 8 子命令 CLI + tool registry + echo + 35 测试 + flat argparse | `c76818d` `d326985` `3cf7e2f` `8236a32` `6660eb1` | [`phase-2.1-minimal-loop.md`](phase-2.1-minimal-loop.md) |
| 2.2 | 真实工具栈（file/todo/web/terminal）+ tool_result_storage 三层防御 + tool-exec 对齐上游 + tools schema/dry-run | `cbd8281` `7879dd6` | [`phase-2.2-tools.md`](phase-2.2-tools.md) |
| 2.3 | prompt 系统 + API retry + model metadata + usage pricing + trajectory + memory/compressor shim + prompt CLI | `36c933a` `2a2486c` `e3ab035` `5dad136` | [`phase-2.3-prompt-context.md`](phase-2.3-prompt-context.md) |
| 2.4 | 流式 + provider CLI + anthropic adapter + codex Responses API + `_make_api_call` 分发器 | `7bcae52` `33572f6` `62092e6` `e0eefa2` `bc809db` | [`phase-2.4-multi-provider.md`](phase-2.4-multi-provider.md) |
| 2.5 | `SessionDB` + `--resume` + session list/show/dump/delete CLI + logs 子命令 | `c42a4fe` `684154d` `cff4ad9` `6315549` `2aee4b5` `8605df3` | [`phase-2.5-sessions.md`](phase-2.5-sessions.md) |
| 2.6 | prompt_toolkit REPL + slash command registry + 11 个 handler + tips + 流式 patch_stdout | `c76ac2e` `b9523fd` `fb5c265` `b5a2f64` | [`phase-2.6-repl.md`](phase-2.6-repl.md) |

每份设计文档的 §0 都有当 phase 内 wave 级别的更细粒度 commit 表。

## 12. 常见操作速查

```bash
# 配置 + smoke
python -m hermes_cli doctor                       # 环境探测
python -m hermes_cli config show
python -m hermes_cli tools list

# 真实问答（需 ~/.phalanx/config.yaml + ~/.phalanx/.env）
python -m hermes_cli oneshot "用一句话说什么是闭包"

# 调试
python -m hermes_cli --debug oneshot "..."        # 全 debug 日志到 stderr
python -m hermes_cli tools run echo --args '{"text":"hi"}'   # 旁路 loop 直调工具

# 旁路入口
python run_agent.py --message "..." --model qwen2.5:1.5b \
    --base-url http://localhost:11434/v1 --api-key ollama

# 测试 + 构建
pytest tests/
ruff check .
python -m build       # → dist/phalanx-0.0.1-py3-none-any.whl + .tar.gz
```

## 13. 已知限制（Phase 2.7+ 解锁）

§2.6 wave 3 之后已经解锁的能力：流式响应（§2.4 wave 1）、anthropic / codex provider（§2.4 wave 2-6）、对话历史持久化与 `--resume`（§2.5）、prompt cache（§2.3 wave 2）、prompt_toolkit REPL 与斜杠命令（§2.6）。当前剩余的限制：

- **REPL stub 命令**：`/retry` `/undo` `/title` `/branch` `/compress` `/yolo` `/reasoning` `/personality` 注册了 def 但 handler 打 `not yet implemented`；用户可见但调用无效，详见 [`phase-2.6-repl.md`](phase-2.6-repl.md) §3.1
- **`@file:` / `@diff` / `@url:` reference**：上游 `SlashCommandCompleter` 里的附件路径补全完全没移
- **session title 系统**：`/save <title>` 路径走 stub；`set_session_title` 未实现，唯一索引冲突回退（"foo (2)"）也未做
- **memory / context_compressor**：`agent/memory_manager.py` 与 `agent/context_compressor.py` 是 shim，运行时 no-op；§2.7+ 移植
- **guardrails / steer / checkpoint / skill**：未引入；上游 `tool_guardrails` / `steer` / `checkpoint_manager` / `skills_*` 全推后续 phase
- **API key 仅 env/.env**：`config.yaml` 不读 API key；待 §2.7 credential pool
- **`cmd_tools_run --args`** 在 Windows PowerShell 下因 shell 引号剥离需用 `\"` 转义；`--args-file` / stdin 通道未实现
- **web_tools 的 LLM 摘要降级**：`agent/auxiliary_client.py` 是 89 行 shim（上游 3914 行），大网页回退到 truncated 原文不做 Gemini/OpenRouter 摘要——上游 client 移植后自动恢复
- **terminal_tool 后端单一**：仅支持 `env_type='local'`；docker / ssh / modal / daytona / vercel_sandbox 待后续 phase
- **delegate_task 未引入**：子 agent 派生还未实现，但 `iteration_budget` 接口已为共享预算预留（见 §4.1）
- **多模态 / 语音 / 浏览器工具**：`/image` `/paste` `/voice` `/browser` 等 §2.7+ 子系统全部未引入

剩余里程碑见 [`MIGRATION_PLAN.md`](MIGRATION_PLAN.md) §2.7 与各 phase 的"留给后续"小节。

## 14. §2.2 关键设计

§2.2 的非平凡决策（tool_result_storage 三层防御 / Windows ARG_MAX 适配 / TodoStore 会话级共享 / auxiliary_client 降级路径 / interrupt 提前引入 / file_state no-op shim）已抽到独立设计文档：见 [`phase-2.2-tools.md`](phase-2.2-tools.md)。
