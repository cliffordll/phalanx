# Phalanx 架构说明（current state）

> 本文描述**当前已实现**的代码结构与运行时数据流；与之配对的**前瞻规划**见 [`MIGRATION_PLAN.md`](MIGRATION_PLAN.md)。
> 当前进度：Phase 2.2 全部 5 wave 完成（file/todo/web/result-storage 工具齐备），可用 Ollama / OpenAI 兼容端点跑完整工具链。已注册 9 个工具：`echo` `read_file` `write_file` `patch` `search_files` `terminal` `todo` `web_search` `web_extract`。

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
                                        │   plain-text REPL or oneshot  │
                                        └────────────┬──────────────────┘
                                                     │  AIAgent(...)
                                                     ▼
   ┌──── per-turn loop ────────────────────────────────────────────────────┐
   │                                                                       │
   │  ┌────────────────┐    1. build api_kwargs   ┌─────────────────────┐  │
   │  │ AIAgent        │ ────────────────────────►│  OpenAI lazy proxy  │  │
   │  │ run_conversa-  │                           │  (chat completions)│  │
   │  │ tion()         │ ◄──── 2. response ───────│                     │  │
   │  └──────┬─────────┘                           └─────────────────────┘  │
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
├── cli.py                          交互式 REPL 实现（被 hermes_cli/main 委托）
├── hermes_constants.py             路径/平台常量（PHALANX_HOME 等）
├── hermes_logging.py               日志工厂（session_tag、redact、rotation）
├── hermes_time.py                  时区辅助（PHALANX_TIMEZONE）
├── utils.py                        atomic_*_write、env helpers、URL 解析
│
├── agent/                          AIAgent 内部支持模块
│   ├── __init__.py
│   ├── retry_utils.py              jittered_backoff
│   ├── error_classifier.py         classify_api_error / FailoverReason
│   ├── file_safety.py              get_read_block_error（§2.2 wave 1）
│   ├── redact.py                   redact_sensitive_text（§2.2 wave 1）
│   └── auxiliary_client.py         §2.2 wave 4 shim（4 个符号，3914→91 行）
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
│   └── tool_result_storage.py      maybe_persist_tool_result + enforce_turn_budget
│
├── hermes_cli/                     CLI 入口与子命令
│   ├── __init__.py                 __version__ + Windows utf-8 setup
│   ├── __main__.py                 python -m hermes_cli 入口
│   ├── main.py                     argparse 分发器，8 个子命令
│   ├── _parser.py                  argparse 辅助（来自 upstream）
│   ├── env_loader.py               load_hermes_dotenv：~/.phalanx/.env + 项目 .env
│   ├── config.py                   cfg_get / cfg_set / load_config / save_config
│   ├── timeouts.py                 get_provider_request_timeout 等
│   ├── banner.py                   ASCII art / 版本横幅
│   ├── colors.py                   ANSI 色码常量
│   └── cli_output.py               print 包装、密码读取
│
├── tests/                          pytest 套件
│   ├── __init__.py
│   ├── conftest.py                 StubClient + stub_openai fixture + reset_echo_call_count
│   ├── test_minimal_loop.py        18 个用例覆盖 IterationBudget / __init__ / run_conversation / 工具序列化
│   ├── test_cli_oneshot.py         10 个用例覆盖 version/doctor/config/oneshot/--debug
│   └── test_cli_tools.py            7 个用例覆盖 tools list/run/错误分支
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
| `run_agent.py` | `AIAgent` orchestrator、`IterationBudget`、`OpenAI` lazy proxy、`def main` 旁路 CLI | ~700 | upstream 14123 行裁剪 |
| `cli.py` | 富 REPL 实现（Phase 1 用朴素 `input()`，Phase 6 上 prompt_toolkit） | ~216 | upstream 12043 行裁剪 |
| `hermes_cli/main.py` | argparse 顶层分发；与上游一致的 flat 结构（global flag 在 top-level，子命令各自重定义需要的 flag） | ~440 | upstream 10439 行裁剪 |
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
| `agent/auxiliary_client.py` | LLM 摘要客户端（OpenRouter Gemini 等）；shim 让 web_tools 走"返回原文"降级路径 | 91 | phalanx minimal shim（upstream 3914 行） |
| `agent/retry_utils.py` | `jittered_backoff(attempt)` | 57 | verbatim |
| `agent/error_classifier.py` | `classify_api_error(error, ...)` + `FailoverReason` enum | 1000 | verbatim |
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
         response = self._call_chat_completions(messages, tools)
         # ↳ POST http://localhost:11434/v1/chat/completions
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

**为什么单独抽出来当一个类**：

1. **防止失控的工具循环烧爆 API**。模型偶尔陷入"调 search → 看不懂 → 再调一次"的死循环。`max_iterations=90` 是硬天花板，触发后状态机进 `budget_exhausted` 分支，给用户返回部分结果而不是无限等待。

2. **可继承的全局额度**（关键设计）。`__init__` 提供 `iteration_budget: Optional[IterationBudget]` 参数：未来 §2.4+ 引入 `delegate_task` 工具，主 agent 派生子 agent 干独立活时，**把父级实例直接传过去**，所有 agent 共享同一个计数器：

   ```python
   sub = AIAgent(..., iteration_budget=parent.iteration_budget)
   ```

   否则一个主 agent + 5 个子 agent 各自 new 一个 `IterationBudget(90)` 等于把 cap 抬到 540 轮，硬天花板形同虚设。`threading.Lock` 也是为这种并发场景准备的。

3. **重试不重复扣费**。API 失败重试时（`run_agent.py:493`），看 `iteration_budget.remaining == 0` 决定要不要继续重试，必要时 `refund()`。一次成功响应只扣一格。

**与 `max_iterations` 的关系**：

- `self.max_iterations`：本次 AIAgent 实例自己的硬上限。
- `self.iteration_budget`：可跨实例共享的预算池。
- 主循环条件 `&&`：两个都要满足。子 agent 既受自己 `max_iterations` 限制，也受继承下来的父预算限制——谁先到先停。

**当前阶段的实际行为**：phalanx 还没移植 `delegate_task`，每个 `AIAgent` 都自己 new 一个 `IterationBudget`，等价于一个简单的 90 轮上限。但接口已经留好——后续接入子 agent 派生时，传 `iteration_budget=parent.iteration_budget` 就能跑共享预算，零结构变更。

返回值 `iterations_used`（`run_agent.py:652`）会向调用方透出实际烧了几轮，便于成本观测。

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
   --debug   --quiet   --model   --base-url   --api-key

subcommands:
   oneshot     <message>     [--message …] [--system …] [--max-iterations N] [--max-tokens N]
   chat                       [--system …] [--max-iterations N]
   tools list                 [--verbose]
   tools run   <name>         --args '<json>'
   config show
   config get  <dot.key>
   version
   doctor
```

**位置敏感**：因为 upstream 不用 `parents=[]` 给子命令注入 global flag，phalanx 也保持 flat。结果：

```bash
hermes --debug oneshot "..."        ✓
hermes oneshot --debug "..."        ✗  argparse: unrecognized argument
```

`hermes_cli/main.py:cmd_chat` 通过 `from cli import main as cli_main` 委托 `cli.py`，复刻 upstream `hermes_cli/main.py:1313` 的调用模式。

## 8. 三个入口

| 入口 | 文件 | 调用模式 | 用途 |
|---|---|---|---|
| `hermes` | `hermes_cli/main.py:main` | `pyproject.toml` 注册的 console_script | 用户主入口（argparse） |
| `hermes-agent` | `run_agent.py:main` | `pyproject.toml` 注册的 console_script | 旁路：直接 fire 进 AIAgent，bypass argparse |
| `cli.py` | 非注册入口 | 被 `hermes_cli/main.py:cmd_chat` 委托；也能 `python cli.py` 直跑 | 交互式 REPL 实现 |

## 9. 测试策略

| 文件 | 用例数 | 关键 fixture / 模式 |
|---|---|---|
| `tests/conftest.py` | （fixture 库） | `stub_openai(responses) → StubClient`：monkeypatch `run_agent.OpenAI` 为返回固定 response 的工厂；`reset_echo_call_count` autouse 清隔离 |
| `tests/test_minimal_loop.py` | 18 | IterationBudget 单元、AIAgent 构造、run_conversation 全闭环（mock 模型 + 真 registry + 真 echo handler） |
| `tests/test_cli_oneshot.py` | 10 | argparse 入口、doctor、config show/get、oneshot 单 turn / 双 turn / 缺 message / `--debug` 输出 |
| `tests/test_cli_tools.py` | 7 | tools list 默认/verbose、tools run 标准/uppercase/JSON 错/非 object/未知工具 |

跑法：

```bash
pytest tests/                     # 全过 1.2s
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

## 11. 常见操作速查

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

## 12. 已知限制（Phase 2.3+ 解锁）

- 不支持流式响应；`stream_callback` 参数已签收但当前不实现（§2.4）
- 不支持 anthropic / bedrock / codex / gemini provider；只走 OpenAI 兼容 chat completions（§2.4）
- 不持久化对话历史；REPL 重启后即清空（§2.5）
- 没有 prompt 缓存 / context compression / memory manager（§2.3 / §2.7）
- API key 仍只能从 env / .env 取，config.yaml 不读（待 §2.7 credential pool）
- `cmd_tools_run --args` 在 Windows PowerShell 下因 shell 引号剥离需用 `\"` 转义；`--args-file` / stdin 通道未实现
- **web_tools 的 LLM 摘要降级**：`agent/auxiliary_client.py` 是 91 行 shim（上游 3914 行），导致大网页只回退到 truncated 原文，不做 Gemini/OpenRouter 摘要——上游 client 移植后自动恢复
- **terminal_tool 后端单一**：仅支持 `env_type='local'`；docker / ssh / modal / daytona / vercel_sandbox 待后续 phase
- **delegate_task 未引入**：子 agent 派生还未实现，但 `iteration_budget` 接口已为共享预算预留（见 §4.1）

剩余里程碑见 [`MIGRATION_PLAN.md`](MIGRATION_PLAN.md) §2.3–§2.7。

## 13. §2.2 关键设计

§2.2 不只是"把工具搬过来"，里面有几处非平凡的工程决策值得单独记录。

### 13.1 tool_result_storage 的三层防御

工具返回大输出（一次 search 命中 1000 行、一次 read_file 50KB）会迅速吃掉模型 context。`tools/tool_result_storage.py` 用三层防御逐级拦截：

| 层 | 入口 | 触发条件 | 行为 |
|---|---|---|---|
| 1 — per-tool cap | 工具自己 | 工具内部判断（如 search 截到 100 条） | 工具返回前先截断；唯一工具作者能控的层 |
| 2 — per-result persist | `maybe_persist_tool_result` | 单结果 > `registry.get_max_result_size(tool_name)`（默认 100 KB） | 通过 `env.execute()` 把全文写入沙箱 `/tmp/hermes-results/{tool_use_id}.txt`，模型只看到 1.5 KB preview + `<persisted-output>` 块带文件路径，可用 `read_file` 按需读片段 |
| 3 — per-turn budget | `enforce_turn_budget` | 一轮所有工具结果之和 > 200 KB | 按已用字符降序，把最大的未持久化结果继续 spill 到沙箱，直到 aggregate 落到 budget 以下 |

接入位置在 `run_agent.py:_dispatch_tool_call` 之后逐工具包一层（layer 2），整轮 for 循环结束后调一次 `enforce_turn_budget`（layer 3）。layer 1 在每个工具的 handler 内部各自处理。

PINNED_THRESHOLDS 把 `read_file` 的阈值钉死为 `inf`——避免"persist→read→persist"无限循环。

### 13.2 LocalTerminalEnv 的 Windows ARG_MAX 适配

`tool_result_storage._write_to_sandbox` 用 heredoc 把内容塞进单条 shell 命令：

```bash
mkdir -p /tmp/hermes-results && cat > /tmp/hermes-results/abc.txt << 'HERMES_PERSIST_EOF'
<整个工具输出，可能 200 KB>
HERMES_PERSIST_EOF
```

Linux ARG_MAX ≥ 2 MB，毫无压力。**Windows `CreateProcess` 命令行限制是 32 767 字符**——等于上游设计在 Windows 上对超过 16 KB 的结果直接退化到 inline truncate（preview 1.5 KB），让 Wave 5 在主开发平台失效。

`tools/terminal_tool.py:LocalTerminalEnv.execute` 加了透明适配：

```python
if os.name == "nt" and len(command) > _LONG_CMD_THRESHOLD:   # 16 KB
    # spill 到临时 .sh 文件，bash <file> 执行，finally 删掉
    fd, spill_path = tempfile.mkstemp(prefix="phalanx-cmd-", suffix=".sh")
    ...
    argv = [_BASH_PATH, spill_path]
```

对 verbatim 的 `tool_result_storage.py` 完全透明——不需要修改 verbatim 上游代码就能在 Windows 跑通沙箱写入。Linux/macOS 路径走 `os.name != "nt"` 分支，零开销。

### 13.3 TodoStore 的会话级共享

`tools/todo_tool.py` 的 `TodoStore` 是一个 in-memory 列表，**实例由 AIAgent 持有，跨工具调用共享**。这意味着：

```
turn 1：todo write → TodoStore = [task1, task2, task3]
turn 2：read_file → 不影响 TodoStore
turn 3：todo read  → 拿回 [task1, task2, task3]   ✓
```

实现上靠 `registry.dispatch(name, args, **kwargs)` 透传 `store=self._todo_store`，handler 收到 `**kw` 后 `kw.get("store")` 取出。这是工具系统里**第一个**需要 per-session 状态的工具，也是为什么 `dispatch` 签名长成 `(name, args, **kwargs)` 而不是 `(name, args)`——为后续这种带状态的工具留接口。

CLI `tools run todo` 路径不经过 AIAgent，所以 `hermes_cli/main.py:cmd_tools_run` 检测到 `args.name == "todo"` 时会**临时 new 一个 TodoStore**，让 smoke test 能跑（每次进程重启即清空，符合预期）。

### 13.4 auxiliary_client 的"返回原文"降级路径

上游 `agent/auxiliary_client.py` 3914 行——是一个独立的 LLM 客户端，专门用来对 web_extract 抓回的网页做 markdown 摘要。phalanx Wave 4 不想跟上游 OpenRouter / 凭据池一起搬，于是写了 91 行 shim：

```python
def get_async_text_auxiliary_client(task: str = "", *, ...):
    return None, None    # ← 关键
```

`web_tools.py` 内部已经写了优雅降级路径：

```python
aux_client, effective_model, _ = _resolve_web_extract_auxiliary(model)
if aux_client is None or not effective_model:
    logger.warning("No auxiliary model available for web content processing")
    return None    # ← 调用方进入"返回 truncated 原文"分支
```

效果：超过 5000 字符的网页**不再生成 markdown 摘要**，而是直接 truncate 到原文 5000 字符。模型仍能看到内容，只是不那么紧凑。

未来移植真实 `auxiliary_client.py` 时，**只换文件，不改任何 web_tools 代码**——这就是为什么 shim 严格保留 4 个符号的签名（`async_call_llm` / `extract_content_or_reasoning` / `get_async_text_auxiliary_client` / `get_auxiliary_extra_body`）。

### 13.5 interrupt 的提前引入

按原计划 `tools/interrupt.py` 应在 Wave 5 引入，但 Wave 4 的 `web_tools.py` 在 8 处用了 `from tools.interrupt import is_interrupted`（lazy import）。两个选择：

1. 给 8 处都打 `try/except ImportError` 补丁（破坏 verbatim 原则）
2. 直接把 98 行 `interrupt.py` 提前 verbatim 复制过来

选了 2。这意味着 Wave 5 的实质内容只剩 `tool_result_storage` + `budget_config`。

`interrupt` 提供按线程隔离的中断信号（`set_interrupt(active, thread_id)` / `is_interrupted()`），背后是 `set[int]` + `threading.Lock`。当前没有调用方 `set_interrupt(True)`——所以 `is_interrupted()` 永远返回 False，不影响功能。预留给后续 Ctrl+C 处理 / gateway 多 session 模式。

### 13.6 file_state 是一个 no-op shim

上游 `tools/file_state.py` 332 行实现 cross-tool 读写时间戳：跨工具 / 跨子 agent 检测"另一个工具刚改过这个文件，你的 read 已过时"，并对并发写串行化。phalanx Wave 1 用 73 行的 shim 替代——所有函数返回安全默认值（无追踪 / 无 staleness 警告 / 无锁）。

shim 严格保留**所有公开符号**：`FileStateRegistry` / `get_registry` / `record_read` / `note_write` / `check_stale` / `lock_path` / `writes_since` / `known_reads`。`file_tools.py` 和 `file_operations.py` 的 import 不需要任何修改，未来直接用上游文件覆盖即可。

代价：单 agent 单线程下 file 工具的并发 / staleness 警告暂时缺失。Phase 7+ 引入子 agent 后必须移植真实版本。
