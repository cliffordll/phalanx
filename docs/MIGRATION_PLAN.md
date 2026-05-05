# Hermes → Phalanx 移植方案

> 源：`D:\opendemo\claudedemo\hermes-agent`（hermes-agent v0.12.0，约 1379 个 .py 文件）
> 目标：`D:\opendemo\claudedemo\phalanx`（当前为空，仅 `.git`）
> **核心约束**：
> 1. 保持原文件名、函数名、类名不变（不重命名 `run_agent.py` / `AIAgent` / `agent/` / `tools/` / `hermes_cli/` / `hermes_*.py` 等）
> 2. **CLI 与最小 loop 同期产出**，作为后续每期功能的调试入口；每期都要有对应的 CLI 子命令暴露当期功能

## 0. 设计原则

1. **核心 loop + CLI 并行优先**：先把 `AIAgent.run_conversation()` 跑通最小闭环，**同时**配套最小可用的 `cli.py` / `hermes_cli/main.py`，让每期产物都能从命令行直接调试，不依赖手写测试脚本。
2. **每期 CLI 同步扩展**：每加一个功能模块（工具、prompt、provider、session、…），CLI 就增加对应的 debug 子命令——`phalanx tools list`、`phalanx prompt show`、`phalanx provider test`、`phalanx session dump` 等。CLI 是贯穿全程的"测量仪"。
3. **路径与符号原样保留**：移植时复制源码路径与命名（含 `hermes_*` 前缀），phalanx 内部仍以 hermes 包名/类名自识别。这能让后续从 hermes 上游补丁直接 cherry-pick，无需改 import。
4. **按需裁剪而非整体复制**：`run_agent.py` 14123 行、`agent/` 32K 行、`tools/` 53K 行，整体抄过来会被多 provider、checkpoint、steer、guardrail、credential pool 等耦合逻辑拽出整条仓库。每个文件都"按需取最小子集"。
5. **每期可运行**：每个 Phase 结束时 phalanx 必须能 `python` 起来、CLI 命令能跑通、对应 smoke test 通过；不允许"半成品中转态"。
6. **删注释中的内部上下文**：源码内大量注释引用 hermes 内部 issue 编号、过往事故、第三方供应商特例（Moonshot/MiniMax/DashScope 等），与 phalanx 无关，可裁剪。**注释内容**可改/可删，**标识符（文件名/类名/函数名）**严格保留。

---

## 1. 现状对照

### 1.1 hermes-agent 顶层布局（保留的目录与文件）

```
hermes-agent/
├── run_agent.py            # AIAgent 主体，14123 行 ← 核心 loop
├── cli.py                  # 12043 行交互式 CLI ← 调试入口
├── hermes_constants.py     # 路径/平台常量（get_hermes_home 等）
├── hermes_state.py         # SessionDB 持久化（2248 行）
├── hermes_logging.py       # 日志/会话上下文
├── hermes_time.py          # 时区辅助
├── utils.py                # atomic_json_write / base_url_hostname 等小工具
├── model_tools.py          # 模型工具相关辅助
├── batch_runner.py         # 批量数据生成
├── mini_swe_runner.py      # SWE bench runner
├── mcp_serve.py            # MCP server 入口
├── rl_cli.py               # RL CLI 入口
├── trajectory_compressor.py
├── toolsets.py / toolset_distributions.py
├── agent/                  # 50 文件，约 31951 行 ← 核心依赖
├── tools/                  # 72 文件，约 53517 行
├── hermes_cli/             # ~70 文件 CLI 子命令
├── plugins/  skills/  optional-skills/
├── gateway/  web/  ui-tui/  tui_gateway/
├── cron/  environments/  acp_adapter/  acp_registry/
├── plans/  docs/  scripts/  packaging/  docker/  nix/
└── tests/  tinker-atropos/  website/
```

### 1.2 phalanx 目标布局（首阶段）

phalanx 顶层与上面对齐，但**只填入当期 Phase 涉及的文件**，其余目录暂空。

### 1.3 核心 loop 位置（关键参考）

| 位置 | 内容 |
|---|---|
| `run_agent.py:271` | `class IterationBudget` — 迭代预算计数器（独立可用） |
| `run_agent.py:873` | `class AIAgent` |
| `run_agent.py:896-959` | `AIAgent.__init__`，参数 60+ 个（多数可在 Phase 1 删除） |
| `run_agent.py:10382` | `def run_conversation` 入口 |
| `run_agent.py:10752` | `while (api_call_count < self.max_iterations and self.iteration_budget.remaining > 0)` ← **主循环** |
| `run_agent.py:13892` | `def chat`（一次性发消息便利方法） |
| `run_agent.py:13907` | `def main`（`run_agent.py` 直接 CLI 入口，带 fire） |

### 1.4 主循环的直接 import（决定 Phase 1 必须先就绪的依赖）

```text
hermes_constants.get_hermes_home
hermes_cli.env_loader.load_hermes_dotenv
hermes_cli.timeouts.*
hermes_cli.config.cfg_get
model_tools.*
tools.terminal_tool.{cleanup_vm, get_active_env, is_persistent_env, ...}
tools.tool_result_storage.{maybe_persist_tool_result, enforce_turn_budget}
tools.interrupt.set_interrupt
tools.browser_tool.cleanup_browser
agent.memory_manager.{StreamingContextScrubber, build_memory_context_block, sanitize_context}
agent.retry_utils.jittered_backoff
agent.error_classifier.{classify_api_error, FailoverReason}
agent.prompt_builder.*
agent.model_metadata.*
agent.context_compressor.ContextCompressor
agent.subdirectory_hints.SubdirectoryHintTracker
agent.prompt_caching.apply_anthropic_cache_control
agent.usage_pricing.{estimate_usage_cost, normalize_usage}
agent.codex_responses_adapter.*
agent.display.*
agent.tool_guardrails.*
agent.trajectory.*
utils.{atomic_json_write, base_url_host_matches, base_url_hostname, env_var_enabled, normalize_proxy_url}
```

> 这串 import 多数可在 Phase 1 用"裁剪后的最小子集"或桩函数顶替。

### 1.5 CLI 入口与实现文件对照

hermes 在 `pyproject.toml:132-135` 注册了**两个 console_script 入口**：

```toml
[project.scripts]
hermes       = "hermes_cli.main:main"   # 用户主入口
hermes-agent = "run_agent:main"          # 直调 AIAgent 的旁路
hermes-acp   = "acp_adapter.entry:main"  # ACP 协议（Phase 7+）
```

`cli.py` **不是注册入口**，但它有 `if __name__ == "__main__": fire.Fire(main)`（`cli.py:12042`），所以也能 `python cli.py` 直跑（开发期常用）；同时它是 `hermes` 默认 / `chat` 子命令的**实现**——`hermes_cli/main.py:1313 from cli import main as cli_main` 委托给它。

| 文件 | 角色 | 调用路径 | Phase 1 覆盖度 |
|---|---|---|---|
| `hermes_cli/main.py`（10439 行） | **注册入口**（`hermes`）：argparse 子命令分发器 | `hermes <subcmd>` → 直接路由<br>`hermes` / `hermes chat` → 委托给 `cli.py:main` | 留 `oneshot` / `chat` / `tools` / `config` / `doctor` / `version` 子命令骨架 |
| `cli.py`（12043 行） | **实现文件**（非注册入口）：富交互式 REPL（prompt_toolkit + TUI + 流式渲染） | 被 `hermes_cli/main.py` 委托；也可 `python cli.py` 直跑 | 仅留薄壳（朴素 `input()`），prompt_toolkit / TUI 推到 Phase 6 |
| `run_agent.py:13907 def main` | **注册入口**（`hermes-agent`）：直调 AIAgent，最薄一层 | 完全旁路，不经过 `hermes_cli/main.py`；用于 smoke / 调试 | 整体保留（裁剪参数） |

---

## 2. 分期计划

> **CLI 始终是当期产物的调试面板**——每期都明确列出"本期新增的 CLI 子命令"。

### 2.0 Phase 0 — 项目骨架（半天）

**产物**
- `phalanx/pyproject.toml`：核心依赖（`openai`、`anthropic`、`httpx[socks]`、`tenacity`、`pydantic`、`python-dotenv`、`pyyaml`、`jinja2`、`rich`、`requests`、**`fire`**——`run_agent.py` 与 `cli.py` 都要用），暂不引入 edge-tts / croniter / faster-whisper / fal-client / firecrawl / exa-py / prompt_toolkit（Phase 6 再加）。
- `phalanx/MANIFEST.in`、`phalanx/.gitignore`（从 hermes 拷贝）。
- 目录占位：`agent/`、`tools/`、`hermes_cli/`、`tests/`、`docs/`。
- 顶层文件（自包含、几乎无外部依赖）：
  - `hermes_constants.py`（**完整移植**，~310 行）
  - `hermes_logging.py`（**完整移植**）
  - `hermes_time.py`（**完整移植**）
  - `utils.py`（**完整移植**：纯函数小工具）
- 初始化 `agent/__init__.py`、`tools/__init__.py`、`hermes_cli/__init__.py`。

**CLI 产物**：无（仅基础设施期）。

**验收**
```bash
cd phalanx && python -c "from hermes_constants import get_hermes_home; print(get_hermes_home())"
```

---

### 2.1 Phase 1 — 最小 loop + 最小 CLI（**合并为本期重点，2–3 天**）

**目标**：一条命令跑通"用户消息 → chat completions API → 模型返回 tool_call → dispatch 一个内置工具 → 模型返回最终消息 → 退出循环"，并且 CLI 已经具备 oneshot / 工具直调 / 配置查看的最小调试面板。

#### 2.1.1 最小 loop 产物

- `run_agent.py`（**裁剪移植**，目标 ~800–1500 行）：
  - 保留：
    - `class IterationBudget`（原 line 271，独立类）
    - `class AIAgent`
    - `AIAgent.__init__` 仅保留参数：`base_url`、`api_key`、`model`、`max_iterations`、`tool_delay`、`enabled_toolsets`、`disabled_toolsets`、`session_id`、`verbose_logging`、`quiet_mode`、`max_tokens`
    - `AIAgent.run_conversation` 主循环骨架（`while` 那段，line 10752 起）
    - `AIAgent.chat`（line 13892 便利方法）
    - 模块级 `_load_openai_cls` / `_OpenAIProxy`（保持 lazy-import，跟测试 patch 兼容）
    - `def main`（line 13907 fire CLI 入口，作为 Phase 1 的"最薄 CLI"立即可用）
  - **删除**：fallback runtime / credential_pool / ACP / 多 provider adapter（仅留 chat_completions）/ checkpoint / steer / skill 注入 / memory prefetch / prompt_caching / context_compressor / tool_guardrails / trajectory 持久化 / surrogate-unicode sanitize / 各类回调（保 None 占位）/ 流式（Phase 4 再加）。
- `agent/retry_utils.py`（**完整移植**，独立无依赖）
- `agent/error_classifier.py`（**裁剪移植**：保留 `classify_api_error`、`FailoverReason`，删非通用 provider 特例）
- `agent/__init__.py`、`tools/__init__.py`、`hermes_cli/__init__.py`（沿用原 docstring）

#### 2.1.2 最小 CLI 产物（**Phase 1 同步上线**）

- `hermes_cli/_parser.py`（**裁剪移植**：参数解析骨架）
- `hermes_cli/env_loader.py`（**裁剪**：`load_hermes_dotenv` 函数名保留，内部用 `python-dotenv.load_dotenv` 顶替；移除复杂搜索路径逻辑）
- `hermes_cli/config.py`（**裁剪**：保留 `cfg_get`、`cfg_set` 最小实现，YAML/JSON 配置文件读写）
- `hermes_cli/timeouts.py`（**完整移植**）
- `hermes_cli/banner.py`、`hermes_cli/colors.py`、`hermes_cli/cli_output.py`（**完整移植**：纯输出辅助）
- `hermes_cli/main.py`（**重度裁剪**至 ~300 行）：仅注册以下子命令
- `cli.py`（**重度裁剪**至 ~200 行薄壳）：作为顶层 fire 入口，把命令委托给 `hermes_cli.main`

#### 2.1.3 Phase 1 暴露的 CLI 子命令

```bash
# 一次性问答（最常用调试入口）
python -m hermes_cli oneshot "你好"
python -m hermes_cli --debug --model gpt-4o-mini oneshot "用 echo 工具回显 hi"

# 朴素交互（input() 即可，prompt_toolkit Phase 6 再上）
python -m hermes_cli chat

# 工具调试（不进 loop，直接 dispatch）
python -m hermes_cli tools list
python -m hermes_cli tools run echo --args '{"text": "hi"}'

# 配置查看
python -m hermes_cli config show
python -m hermes_cli config get base_url

# 版本 / 健康检查
python -m hermes_cli version
python -m hermes_cli doctor      # 检查 API key、网络、模型可达性

# 直接调底层（绕过 hermes_cli 路由）
python run_agent.py --message "..." --model gpt-4o-mini
```

`--debug` 全局开关：打印每次 API 请求/响应概要、tool dispatch 名字+参数+耗时、当前 `IterationBudget.remaining`、最终消息条数。这个开关贯穿后续所有 Phase。

**位置敏感（与 hermes 上游一致）**：`--debug` / `--quiet` / `--model` / `--base-url` / `--api-key` / `--provider` / `--resume` 是 **top-level flag**，必须放在**子命令名称之前**：

```bash
python -m hermes_cli --debug --model X oneshot "..."   # ✓ 正确
python -m hermes_cli oneshot --debug --model X "..."   # ✗ argparse 报 unrecognized
```

子命令自身的 flag（`--system` / `--max-iterations` / `--max-tokens` / `--dump-messages` / `--stream` / `--args` / `--verbose`）则放在子命令名之后。upstream `hermes_cli/_parser.py` 的 `build_top_level_parser()` 走 flat 结构，不用 `parents=[]`，phalanx 与之保持一致以便上游 cherry-pick。

#### 2.1.4 内置工具

- `tools/registry.py`（**裁剪**至最小 API：`register/dispatch/list_tools`）
- 一个可用工具用于 smoke test：复用 hermes 现有的 `tools/todo_tool.py` 的最简子集（保持文件名），或从 `tools/file_tools.py` 抽 `read_file` 一个函数

#### 2.1.5 测试

- `tests/test_minimal_loop.py`：mock `openai.OpenAI`，断言 tool_call 闭环
- `tests/test_cli_oneshot.py`：subprocess 调 `python -m hermes_cli oneshot`，断言退出码与输出
- `tests/test_cli_tools.py`：断言 `tools list` / `tools run` 行为

#### 2.1.6 验收

```bash
pytest tests/                                                    # 全过
python -m hermes_cli doctor                                      # 健康检查通过
python -m hermes_cli tools list                                  # 至少列出 1 个工具
python -m hermes_cli tools run <name> --args '{...}'             # 直调成功
python -m hermes_cli --debug oneshot "用 <name> 工具完成 X"      # 闭环跑通，debug 输出可读
python run_agent.py --message "echo X"                           # 底层入口也可用
```

---

### 2.2 Phase 2 — 真实工具落地 + 工具 CLI 完善（2–3 天）

按使用频率移植，每个工具单独裁剪。**文件名保留 hermes 原名**。

| 顺序 | 源文件 | 移植策略 |
|---|---|---|
| 1 | `tools/file_tools.py` `tools/file_operations.py` | 保留 `read/write/edit/glob/grep` 等核心；删 `file_state` 跨 agent 注册表（Phase 7+ 再上） |
| 2 | `tools/todo_tool.py` | 较轻量，整体移植 |
| 3 | `tools/terminal_tool.py` | **最复杂**：仅移植"一次性子进程执行 + 超时"的最小子集；持久化 VM、env passthrough、process_registry、shell_hooks 全部 Phase 7+ |
| 4 | `tools/path_security.py` `tools/binary_extensions.py` | 纯工具函数，整体移植 |
| 5 | `tools/web_tools.py` | 仅保留基于 `httpx` 的 webfetch + 一个简单 search；删 exa/firecrawl/parallel-web 重依赖 |
| 6 | `tools/interrupt.py` | 整体移植 |
| 7 | `tools/tool_output_limits.py` `tools/tool_result_storage.py` | 整体移植 |

#### 2.2.1 Phase 2 新增 CLI 子命令

```bash
python -m hermes_cli tools list --verbose                         # 显示 schema/描述
python -m hermes_cli tools run read_file --args '{"path":"..."}'
python -m hermes_cli tools run terminal --args '{"cmd":"ls"}'
python -m hermes_cli tools run web_fetch --args '{"url":"..."}'
python -m hermes_cli tools schema <name>                          # 单独 dump JSON Schema
python -m hermes_cli tools dry-run <name> --args '...'            # 仅校验参数不真执行
```

**验收**：每个工具配 `tests/test_<tool>.py`；端到端 `python -m hermes_cli oneshot "用 read_file 工具读 README"` 跑通。

---

### 2.3 Phase 3 — Prompt 与上下文 + Prompt CLI（2 天）

| 源文件 | 策略 |
|---|---|
| `agent/prompt_builder.py`（56385 行） | 裁剪：保留 `build_environment_hints`、`load_soul_md`、基础 `build_system_prompt`；删 skills 注入、context_files 注入、enforcement guidance 表（Phase 7 再加） |
| `agent/model_metadata.py`（61668 行） | 裁剪：仅保留你当前要用的 5–10 个模型条目（OpenAI/Anthropic 主线），数百行模型表全删 |
| `agent/display.py` | 裁剪：保留 `print_*` 基本输出；删富 UI 渲染 |
| `agent/usage_pricing.py` | 完整移植（721 行，独立） |
| `agent/trajectory.py` | 完整移植（仅 56 行） |
| `agent/memory_manager.py` | **不移植本期**：`build_memory_context_block` 写空字符串 stub，函数名保留 |
| `agent/context_compressor.py` | **不移植本期**：用"消息超过 N 条就丢最早 user/assistant 对"的回退策略；保留类名 `ContextCompressor` 作为薄壳 |
| `agent/subdirectory_hints.py` | 完整移植 |
| `agent/prompt_caching.py` | 完整移植 |

#### 2.3.1 Phase 3 新增 CLI 子命令

```bash
python -m hermes_cli prompt show                          # dump 当前完整 system prompt（含环境提示）
python -m hermes_cli prompt show --raw                    # 不带工具描述拼接的原 SOUL.md
python -m hermes_cli model list                           # 列出 model_metadata 中的模型条目
python -m hermes_cli model info gpt-4o-mini               # 单模型上下文窗口/价格/能力
python -m hermes_cli oneshot --dump-messages "..."        # 调用结束后打印完整 messages 数组（含 system）
python -m hermes_cli pricing estimate --tokens 12000      # 用 usage_pricing 算估价
```

---

### 2.4 Phase 4 — 多 provider 适配 + Provider CLI（按需，1–3 天/个）

每个 adapter 独立大文件，**只移植日常用的一个**，其余推迟。

| 源文件 | 行数 | 何时移植 |
|---|---|---|
| `agent/anthropic_adapter.py` | ~84K | 用 Claude API 时立刻移 |
| `agent/bedrock_adapter.py` | ~50K | 用 AWS Bedrock 时再移 |
| `agent/codex_responses_adapter.py` | ~46K | 用 OpenAI Responses API / xAI 时再移 |
| `agent/gemini_native_adapter.py` `agent/gemini_cloudcode_adapter.py` | ~70K | 用 Gemini 时再移 |
| `agent/auxiliary_client.py` | 174K | 极复杂，含子 agent / 鉴权流；推迟到 Phase 7 |

移植 adapter 时，`run_agent.py` 中 `__init__` 的 provider 自动检测分支（line 1043–1072）按需补回。

**本期同时引入流式（`stream_callback`）路径**——CLI 用它做实时 token 显示。

#### 2.4.1 Phase 4 新增 CLI 子命令

```bash
python -m hermes_cli provider list                        # 当前已注册 adapter
python -m hermes_cli provider test anthropic              # 发一条 ping 消息验证连通
python -m hermes_cli --provider anthropic oneshot --stream "..."   # 强制流式
python -m hermes_cli oneshot --no-stream "..."            # 强制非流式（对比调试）
python -m hermes_cli model switch claude-3-5-sonnet       # 写入配置切默认模型
```

---

### 2.5 Phase 5 — 会话与持久化 + Session CLI（1–2 天）

| 源文件 | 策略 |
|---|---|
| `hermes_state.py` | **完整移植**（2248 行，相对独立的 SessionDB） |
| `agent/trajectory.py` | Phase 3 已移 |
| `run_agent.py` 中 `_ensure_db_session`、trajectory 写入分支 | 重新启用（Phase 1 时被裁掉） |
| `hermes_cli/logs.py` | **裁剪移植**（与 SessionDB 配套） |

#### 2.5.1 Phase 5 新增 CLI 子命令

```bash
python -m hermes_cli session list                         # 列出最近 N 个会话
python -m hermes_cli session show <id>                    # 渲染会话轮次
python -m hermes_cli session dump <id>                    # 导出原始 JSONL
python -m hermes_cli session resume <id>                  # 在已有会话上继续 chat
python -m hermes_cli session delete <id>
python -m hermes_cli logs --session <id>                  # 按会话过滤日志
python -m hermes_cli logs --tail 50
python -m hermes_cli --resume <id> oneshot "..."          # 短链路恢复
```

---

### 2.6 Phase 6 — 交互式 REPL 与 CLI 完善（1–2 天）

把 Phase 1 的 `input()` 朴素交互升级为 hermes 风格的 prompt_toolkit REPL：

| 源文件 | 策略 |
|---|---|
| `cli.py`（12043 行） | 进一步移植：prompt_toolkit 历史/补全/多行输入、`/` 斜杠命令、`patch_stdout` 流式渲染 |
| `hermes_cli/main.py` | 完善子命令路由 |
| `hermes_cli/banner.py` `hermes_cli/cli_output.py` `hermes_cli/colors.py` | 已在 Phase 1 移植，本期补能力 |
| `hermes_cli/completion.py` | **新移植**：tab 补全 |
| `hermes_cli/commands.py` | **裁剪移植**：`/` 斜杠命令处理（`/help`、`/clear`、`/save`、`/model` 等基础几个） |
| `hermes_cli/tips.py` | 完整移植（轻量） |

依赖：本期才把 `prompt_toolkit` 加入 `pyproject.toml`。

#### 2.6.1 Phase 6 新增 CLI 能力

```bash
python -m hermes_cli                                       # 默认进入 REPL
> /help
> /clear
> /save mytopic
> /model gpt-4o
> /tools                                                   # REPL 内列工具
> /debug on                                                # REPL 内切 debug
> 你好<Enter>                                              # 普通对话，流式输出
```

---

### 2.7 Phase 7+ — 后续按需扩展

每一项作为独立 feature 分支完成。每项都要随 CLI 子命令一起出（如 `phalanx skills list`、`phalanx kanban add`、`phalanx mcp connect` 等），保持"每期可调试"的节奏。

- **memory & context**：`agent/memory_manager.py`、`agent/context_compressor.py`、`agent/context_engine.py`、`agent/context_references.py`
- **guardrails**：`agent/tool_guardrails.py`、`tools/skills_guard.py`、`tools/tirith_security.py`、`tools/website_policy.py`
- **credentials**：`agent/credential_pool.py`、`agent/credential_sources.py`、`agent/google_oauth.py`、`hermes_cli/auth*.py`
- **skills 系统**：`skills/`、`optional-skills/`、`agent/skill_*.py`、`tools/skill*.py`、`tools/skills_hub.py`、`hermes_cli/skills_*.py`
- **gateway / 平台桥接**：`gateway/`、`hermes_cli/gateway.py`、`hermes_cli/slack_cli.py`、`hermes_cli/dingtalk_auth.py`、`hermes_cli/copilot_auth.py`
- **web dashboard**：`web/`、`hermes_cli/web_server.py`
- **kanban**：`tools/kanban_tools.py`、`hermes_cli/kanban*.py`
- **cron**：`cron/`、`tools/cronjob_tools.py`、`hermes_cli/cron.py`
- **MCP**：`tools/mcp_tool.py`、`tools/mcp_oauth*.py`、`mcp_serve.py`、`hermes_cli/mcp_config.py`
- **ACP**：`acp_adapter/`、`acp_registry/`、`agent/copilot_acp_client.py`
- **browser**：`tools/browser_*.py`、`tools/browser_providers/`
- **voice**：`tools/tts_tool.py`、`tools/transcription_tools.py`、`tools/voice_mode.py`、`tools/neutts_synth.py`、`hermes_cli/voice.py`
- **checkpoints**：`tools/checkpoint_manager.py`
- **delegate / 子 agent**：`tools/delegate_tool.py`、`agent/auxiliary_client.py`、`agent/curator.py`
- **RL / training**：`tinker-atropos/`、`rl_cli.py`、`tools/rl_training_tool.py`
- **batch / dataset gen**：`batch_runner.py`、`mini_swe_runner.py`、`datagen-config-examples/`
- **plugins**：`plugins/`、`hermes_cli/plugins.py`、`hermes_cli/plugins_cmd.py`

---

## 3. 验收门 / 每期产物清单

| Phase | 关键产物 | CLI 验收 |
|---|---|---|
| 0 | 骨架 + `hermes_constants.py` + `hermes_logging.py` + `utils.py` | `python -c "from hermes_constants import get_hermes_home"` |
| **1** | 裁剪版 `run_agent.py` + 最小 CLI（`hermes_cli/main.py` + `cli.py` 薄壳）+ `tools/registry.py` + 1 个工具 | `python -m hermes_cli doctor` ✓<br>`python -m hermes_cli tools list` ✓<br>`python -m hermes_cli --debug oneshot "..."` ✓<br>`pytest tests/` 全过 |
| 2 | 4–7 个核心工具移植完成 | `python -m hermes_cli tools run <each> --args ...` 全成功<br>`python -m hermes_cli oneshot "用 <tool> 完成多步任务"` |
| 3 | prompt_builder/model_metadata/display/usage_pricing/trajectory（裁剪） | `python -m hermes_cli prompt show` ✓<br>`python -m hermes_cli model list` ✓<br>`python -m hermes_cli oneshot --dump-messages "..."` |
| 4 | 至少 1 个 adapter + 流式 | `python -m hermes_cli provider test <name>` ✓<br>`python -m hermes_cli --provider <name> oneshot --stream "..."` |
| 5 | `hermes_state.py` + trajectory 写盘 + 会话恢复 | `python -m hermes_cli session list` ✓<br>`python -m hermes_cli session resume <id>` ✓ |
| 6 | prompt_toolkit REPL + 斜杠命令 | `python -m hermes_cli` 进入 REPL，`/help` `/model` `/tools` 全部可用 |
| 7+ | 按需 | 各模块独立 CLI 子命令 + smoke |

---

## 4. 风险与应对

| 风险 | 应对 |
|---|---|
| Phase 1 范围扩大（loop+CLI 合并），单期工时膨胀 | CLI 部分严控范围：仅 `oneshot` / `chat`(朴素 input) / `tools list/run` / `config show/get` / `doctor` / `version` 6 个子命令；prompt_toolkit、`/` 斜杠、history 等推到 Phase 6 |
| `run_agent.py` 内部交叉引用过深，裁剪后语法/逻辑断裂 | 用 ruff/mypy/`python -c "import run_agent"` 即时验证；每删一段就跑一次 import + CLI smoke |
| `agent/*` 之间相互依赖（如 `prompt_builder` 引用 `model_metadata` 的常量） | Phase 3 一次性把这几个文件按"最小可用子集"一起裁剪 |
| 多 provider 分支删干净后，未来加回来需重新对照 hermes 上游 | 在 phalanx 内保留 `docs/HERMES_UPSTREAM_MAP.md` 记录"哪些行号属于哪个 provider"，方便回填 |
| hermes 上游持续演进，phalanx 跟不上 | 因为标识符全部保持原名，可用 `git diff` 对照 hermes 仓库 cherry-pick；建议每月做一次同步 |
| `~/.phalanx/` 目录与系统中已有 hermes 安装冲突 | 已采用方案 B 完全隔离 env 变量（`PHALANX_HOME` / `PHALANX_TIMEZONE` / `PHALANX_OPTIONAL_SKILLS`）；hermes 的 `HERMES_HOME` 不再被 phalanx 识别，二者环境变量互不干扰。CLI `doctor` 子命令打印当前生效路径以便核对 |
| Windows 路径 / PowerShell 兼容（当前环境） | Phase 1 起就在 Windows 上跑 CLI 测试，不要拖到 Phase 6 |
| CLI debug 输出过多影响主流程性能/可读 | `--debug` 仅写 stderr 或单独 `~/.phalanx/logs/debug.log`；默认 stdout 仍然干净 |

---

## 5. 操作守则

1. **每个 Phase 必须保持 phalanx 可运行 + CLI 子命令可用 + 测试通过**，不允许并行多个 Phase。
2. **裁剪只删不改**：尽量只删除整段代码（多 provider 分支、回调、复杂 flag），不要重写函数体。能保留原行就保留原行——这样将来从 hermes 同步上游补丁时冲突最少。
3. **CLI 子命令同步追加**：每加一个底层模块，必须在同一 PR 里给 CLI 加对应的 debug 子命令。没有 CLI 入口的功能视为"未交付"。
4. **注释裁剪**：内部 issue 编号 / 事故引用可删；公共算法说明保留。
5. **License**：`hermes-agent` 是 MIT。在 phalanx 根目录放 `LICENSE`，顶部保留原作者 (Nous Research) 的版权声明 + 你的修改条款。
6. **环境变量改名为 `PHALANX_*`（方案 B）**：`HERMES_HOME` → `PHALANX_HOME`、`HERMES_TIMEZONE` → `PHALANX_TIMEZONE`、`HERMES_OPTIONAL_SKILLS` → `PHALANX_OPTIONAL_SKILLS`。原因：与系统中已装的 hermes-agent 完全隔离，避免共用同一 env 变量时数据互相污染。**函数名 `get_hermes_home` 等保持不变**（标识符约束）；只改 env 变量字面量与默认路径 `~/.hermes` → `~/.phalanx`。从 hermes 上游 cherry-pick 时，每批做一次 `sed -i 's/HERMES_HOME/PHALANX_HOME/g'`（含其他 `HERMES_*` env 变量同步处理）。
7. **不新增"包名前缀"**：不要把 `agent/` 包成 `phalanx/agent/`——保持原顶层布局，让 `from agent.retry_utils import jittered_backoff` 这种 import 在 phalanx 内仍然有效。

---

## 6. 立即启动

下一步建议直接进 **Phase 0 + Phase 1**，预计交付：

**基础设施（Phase 0）**
- `phalanx/pyproject.toml`、`phalanx/hermes_constants.py`、`phalanx/hermes_logging.py`、`phalanx/hermes_time.py`、`phalanx/utils.py`

**最小 loop（Phase 1）**
- `phalanx/run_agent.py`：裁剪版 `IterationBudget` + `AIAgent.__init__` + `AIAgent.run_conversation` 主循环 + `AIAgent.chat` + `def main`，目标 800–1500 行
- `phalanx/agent/retry_utils.py`、`phalanx/agent/error_classifier.py`（裁剪）
- `phalanx/tools/registry.py`（最小 API）+ 一个可用工具

**最小 CLI（Phase 1 同期）**
- `phalanx/cli.py`：~200 行薄壳
- `phalanx/hermes_cli/main.py`：~300 行子命令分发
- `phalanx/hermes_cli/_parser.py` `env_loader.py` `config.py` `timeouts.py` `banner.py` `colors.py` `cli_output.py`：裁剪移植
- 暴露：`oneshot` / `chat` / `tools list` / `tools run` / `config show` / `config get` / `doctor` / `version` 共 8 个子命令，并支持 `--debug` 全局开关

**测试**
- `tests/test_minimal_loop.py`、`tests/test_cli_oneshot.py`、`tests/test_cli_tools.py` 全过

确认后开工。
