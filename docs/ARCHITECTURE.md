# Phalanx 架构说明（current state）

> 本文描述**当前已实现**的代码结构与运行时数据流；与之配对的**前瞻规划**见 [`MIGRATION_PLAN.md`](MIGRATION_PLAN.md)。
> 当前进度：Phase 1 完整收尾（§2.1.1–§2.1.5），可用 Ollama / OpenAI 兼容端点跑通完整 tool-calling loop。

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
   │  │ tools.registry │ ── registry.dispatch ──► │  echo_tool / ...    │  │
   │  │ singleton      │                           │  (handlers)         │  │
   │  └────────────────┘                          └─────────────────────┘  │
   │                                                                       │
   │  4. append tool result, loop again until no tool_calls or budget end  │
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
│   └── error_classifier.py         classify_api_error / FailoverReason
│
├── tools/                          工具系统
│   ├── __init__.py                 静态 import echo_tool 触发自注册
│   ├── registry.py                 ToolRegistry 单例 + ToolEntry + tool_error/tool_result
│   └── echo_tool.py                Phase 1 smoke 工具（phalanx 专属，§2.2 后可删）
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
| `tools/registry.py` | `ToolRegistry` 单例、`ToolEntry`、register/dispatch/`get_definitions`/`get_all_tool_names`/`get_schema`/`get_toolset_for_tool` | ~553 | near-verbatim from upstream（2 处 lazy fallback 替换 budget_config / model_tools） |
| `tools/echo_tool.py` | smoke 工具，自注册 `echo` | ~83 | phalanx 专属 |
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

## 5. 工具系统

### 注册（Phase 1：静态）

`tools/__init__.py` 在 import 时**显式 import** 每个内置工具模块：

```python
# tools/__init__.py
from tools import registry           # 暴露子模块
from tools import echo_tool          # 触发 echo_tool 顶层 registry.register(...)
```

每个工具文件顶层调一次 `registry.register(name, toolset, schema, handler, ...)`。**不再有别处主动加载工具**——只要 `import tools` 跑过，单例就装满。

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

### Phase 1 缺什么

- 异步 handler：`registry.dispatch` 已支持 `is_async=True`，但 Phase 1 没有 model_tools 的桥，回退到 `asyncio.run`（不支持嵌套循环场景）
- `discover_builtin_tools()`：upstream 用 AST 自动扫 `tools/*.py` 是否含 `registry.register(...)` 顶层调用。Phase 1 用静态 import 替代——更明确，但加新工具要改 `__init__.py`

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

## 12. 已知限制（Phase 2.2 即将解锁）

- 工具集合极小（只有 `echo`）；真正能用的 file_tools / terminal / web_tools 需 §2.2
- 不支持流式响应；`stream_callback` 参数已签收但 Phase 1 不实现（§2.4）
- 不支持 anthropic / bedrock / codex / gemini provider；只走 OpenAI 兼容 chat completions（§2.4）
- 不持久化对话历史；REPL 重启后即清空（§2.5）
- 没有 prompt 缓存 / context compression / memory manager（§2.3 / §2.7）
- API key 仍只能从 env / .env 取，config.yaml 不读（待 §2.7 credential pool）
- `cmd_tools_run --args` 在 Windows PowerShell 下因 shell 引号剥离需用 `\"` 转义；`--args-file` / stdin 通道未实现

剩余里程碑见 [`MIGRATION_PLAN.md`](MIGRATION_PLAN.md) §2.2–§2.7。
