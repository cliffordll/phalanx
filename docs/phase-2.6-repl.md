# Phase 2.6 设计文档 — 交互式 REPL（prompt_toolkit + 斜杠命令）

> 配套阅读：
> - [`MIGRATION_PLAN.md`](MIGRATION_PLAN.md) §2.6 — 计划层面的源文件清单与策略
> - [`ARCHITECTURE.md`](ARCHITECTURE.md) — 全局视图
> - [`phase-2.5-sessions.md`](phase-2.5-sessions.md) — SessionDB / `--resume` 落地，REPL `/save` 与 `/resume` 的依赖

本文记录 **§2.6 三个 wave 的依赖引入、`cli.py` REPL 状态机、斜杠命令注册与分发、流式输出回显**——把 Phase 1 的 `input()` 朴素 loop 升级到 hermes 风格的 prompt_toolkit 交互。

## 0. 范围与 wave 划分

| Wave | 内容 | 提交 |
|---|---|---|
| 1 | `pyproject.toml` 加 `prompt_toolkit` 依赖；`cli.py` `_run_repl` 重写为 `PromptSession` + `FileHistory(~/.hermes/cli_history)` + multiline（Alt+Enter）+ `AutoSuggestFromHistory`；保留 `input()` 退路在 prompt_toolkit 不可用时。Banner 用现有 `hermes_cli/banner.py` 渲染 | `c76ac2e` |
| 2 | 移植 `hermes_cli/commands.py` 的 `CommandDef` + 裁剪后的 `COMMAND_REGISTRY`（只留 phalanx 当前能跑的 ~10 个命令）+ `resolve_command` + `SlashCommandCompleter`（基础版，去掉 skill / file-path / `@reference` 补全）；接进 `PromptSession.completer`；REPL 入口检测 `/<cmd>` 触发 dispatcher | `b9523fd` |
| 3 | 斜杠命令 handler 实装：`/help` / `/clear` / `/new` / `/history` / `/quit` / `/exit` / `/model` / `/tools` / `/debug` / `/save <title>` / `/resume <id>`；`patch_stdout` 包装流式输出让 token delta 不破坏 prompt 行；`hermes_cli/tips.py` 完整移植 + REPL 启动随机展示一条 | `fb5c265` |

§2.6 落地后 `python -m hermes_cli` 默认进 REPL，跟 hermes 上游"长得很像但功能子集"——history 持久化、tab 补全斜杠命令、流式渲染、几个核心管理命令；剩余的 `/branch` / `/compress` / `/skin` / `/personality` / `/yolo` / `/worktree` / `/checkpoint` / `@file:` reference 等大头留给 §2.7+。

## 1. 依赖与启动路径

### 1.1 prompt_toolkit 引入

`pyproject.toml` 增加：
```toml
"prompt_toolkit>=3.0.50,<4",
```

引入位置：仅 `cli.py` + `hermes_cli/commands.py` 在用，其它子命令（`oneshot` / `tools` / `session` / `logs`）均不依赖。CLI 子命令从 `hermes_cli/main.py` 直接调，**不会触发 prompt_toolkit import**——`hermes oneshot` / `hermes session list` / `hermes version` 启动时间不受影响。

`cli.py` 在最顶层做 lazy import + try/except 兜底：

```python
try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
    from prompt_toolkit.patch_stdout import patch_stdout
    _PT_AVAILABLE = True
except ImportError:
    _PT_AVAILABLE = False
```

`_run_repl()` 入口检测 `_PT_AVAILABLE`：True 走 prompt_toolkit 路径，False 退到 Phase 1 的 `input()` loop（保留兼容性 + 文档里写明这种降级模式）。

### 1.2 REPL 入口路径

```
python -m hermes_cli                  # → main.py 默认走 chat 子命令
hermes chat                            # → cmd_chat → from cli import main as cli_main
                                       # → cli.main() → _run_repl()
python cli.py                          # → fire.Fire(main) → main() → _run_repl()
```

三条路最终都汇到 `cli._run_repl(agent)`。`hermes_cli/main.py:cmd_chat` 已存在不动；wave 1 只换 `_run_repl` 内部实现。

## 2. REPL 状态机

### 2.1 `PromptSession` 配置

```python
session = PromptSession(
    history=FileHistory(get_hermes_home() / "cli_history"),
    auto_suggest=AutoSuggestFromHistory(),     # 灰色 ghost text
    multiline=Condition(_should_multiline),    # Alt+Enter 触发
    completer=SlashCommandCompleter(...),       # wave 2 接入
    complete_while_typing=True,
    key_bindings=_build_keybindings(),         # Alt+Enter 插入换行
    erase_when_done=False,
    bottom_toolbar=None,                        # status bar 留给后续
)
```

**multiline 触发条件**：当用户当前行以 `\` 结尾或显式按 `Alt+Enter` 时进入多行；空行单独按 Enter 提交。Phase 2.6 简化为只支持 `Alt+Enter` 显式换行——上游的"5 行以上自动转 attachment"等启发式留给后续。

**FileHistory 路径**：`~/.hermes/cli_history`（与 hermes 上游同位置，方便共用）。

### 2.2 主循环

```python
def _run_repl(agent):
    print(_build_banner(agent))
    print(random.choice(TIPS))                      # wave 3
    history: List[Dict] = []
    while True:
        try:
            with patch_stdout():                    # wave 3
                line = session.prompt("> ")
        except (EOFError, KeyboardInterrupt):
            return 0
        line = line.strip()
        if not line:
            continue
        if line.startswith("/"):
            rc = _dispatch_slash(agent, line, state)
            if rc == "exit":
                return 0
            continue
        # Plain message — run a turn.
        try:
            history = _run_turn(agent, line, history)
        except Exception as exc:
            print_error(f"[error] {exc}")
```

**state 字典**保存 REPL 之间持久的小状态：当前 session_id（用于 `/save` 命名）、debug flag（`/debug on`/`off`）、最近一次 model（`/model` 切换后下一次 turn 生效）。不进 SessionDB——纯进程内。

### 2.3 Ctrl+C 语义

- **Single Ctrl+C，agent idle**：清空当前输入行（prompt_toolkit 默认行为）
- **Single Ctrl+C，agent busy**：调 `agent.request_interrupt()`，agent 在下一轮 tool-call 边界停下；REPL 仍在
- **Double Ctrl+C 内 2 秒**：`raise KeyboardInterrupt` → `_run_repl` 返回 0 退出

state 字典里存 `last_ctrl_c_ts: float` 实现双击检测。Wave 3 配套 patch_stdout 一起做。

## 3. 斜杠命令子系统

### 3.1 上游 53 个命令的取舍

上游 `commands.py` `COMMAND_REGISTRY` 列了 53 个 `CommandDef`。Phase 2.6 只保留 phalanx 当前能跑的：

**保留（wave 3 实装）**：
- Session：`/new` `/clear` `/history` `/save` `/resume` `/quit`/`/exit`
- Configuration：`/model` `/debug`
- Tools：`/tools`（list/disable/enable）
- Info：`/help`

**保留 def 但 stub handler**（让 tab 补全列出来，调用时打 "not yet implemented"）：
- `/retry` `/undo` `/title` `/branch` `/compress` `/yolo` `/reasoning` `/personality`

**完全不移**（依赖 §2.7+ 子系统）：
- `/rollback` `/snapshot` `/checkpoint`（依赖 checkpoint 系统）
- `/skin` `/indicator`（依赖 skin/theme 系统）
- `/voice`（依赖 TTS）
- `/cron` `/skills` `/curator` `/kanban` `/browser`（独立大模块）
- `/background` `/queue` `/steer` `/agents` `/goal`（依赖 BG task runner）
- `/usage` `/insights` `/copy` `/paste` `/image`（其它依赖）
- gateway-only：`/sethome` `/approve` `/deny` `/restart` `/commands` `/update`

数量：**保留 ~10 个 + stub ~8 个 = 注册 ~18 个**，剩 35 个上游命令在 phalanx 当前直接屏蔽。

### 3.2 `CommandDef` + 注册表

裁剪后的 `hermes_cli/commands.py` 只保留：
- `CommandDef` dataclass（**verbatim**，所有字段都用得上）
- `COMMAND_REGISTRY: list[CommandDef]`（裁剪后约 18 项）
- `_build_command_lookup` / `resolve_command`
- `COMMANDS` / `COMMANDS_BY_CATEGORY` / `SUBCOMMANDS` 三个派生 dict（`/help` 用）

去掉的：`telegram_*` / `discord_*` / `slack_*` / `_iter_plugin_command_entries` / `gateway_*`（gateway / messaging-platform 全套，~700 行）。

### 3.3 `SlashCommandCompleter`

基础版，只补全：
1. **斜杠命令本身**：用户输入 `/` → 列所有 `COMMANDS.keys()`；`/m` → 过滤 `/model` `/...`
2. **子命令**：`/tools <space>` 后列 `SUBCOMMANDS["/tools"]`（`list` / `disable` / `enable`）；`/model <space>` 后不补全（model 名千变万化）

**不做**（留给后续）：
- `@file:` / `@folder:` / `@diff` / `@url:` 等 `@reference` 补全
- 文件系统路径补全（`./foo/bar` 走 fs glob）
- skill 命令补全（依赖 skill 系统）
- 异步 model 列表补全（依赖 `provider list` 拿模型列表）

### 3.4 Dispatch 表

Wave 3 在 `cli.py` 内开 `_SLASH_HANDLERS: dict[str, Callable]` 映射：

```python
_SLASH_HANDLERS = {
    "help":    _cmd_help,
    "clear":   _cmd_clear,
    "new":     _cmd_new,
    "history": _cmd_history,
    "save":    _cmd_save,
    "resume":  _cmd_resume,
    "quit":    _cmd_exit,
    "exit":    _cmd_exit,
    "model":   _cmd_model,
    "tools":   _cmd_tools,
    "debug":   _cmd_debug,
}
```

`_dispatch_slash(agent, line, state)`：
1. 拆 `/<cmd> <args>`，alias 走 `resolve_command(name).name` 归一
2. handler 缺失 → 打 stub `"/<cmd> not yet implemented in phalanx (Phase 2.7+)"`
3. handler 返回 `"exit"` 字符串 → 主循环退出
4. 其它返回值或异常 → 当前轮处理，不退出

每个 handler 签名 `(agent, args: str, state: dict) -> Optional[str]`，没必要做装饰器注册——dispatch 表就一个文件 11 行。

### 3.5 关键 handler 行为表

| 命令 | 实现概要 |
|---|---|
| `/help` | 按 `COMMANDS_BY_CATEGORY` 分组打印 `name — description`；保留命令同时列 stub 命令但加 `[stub]` 标记 |
| `/clear` | 清屏（`os.system("cls" / "clear")`）+ 重置 history 列表 + 新 `agent.session_id`，等同于 `/new` 但加屏幕清理 |
| `/new` | 不清屏，仅重置 history + 新 session_id（旧 session 仍在 DB 里） |
| `/history` | 打印当前进程内 `history` 列表（每条 `role: content[:80]`）；不查 DB |
| `/quit` `/exit` | 返 `"exit"` 让主循环退出 |
| `/save <title>` | 调 `agent._session_db.set_session_title(...)`；§2.6 不做 title 系统所以暂打 `not yet implemented`（升级到 stub） |
| `/resume <id>` | `resolve_session_id` → `get_messages_as_conversation` → 替换当前 `history`；session_id 也跟着切；`reopen_session` 让 end_session 不变 no-op |
| `/model <name>` | `agent.model = name`；下一轮 turn 生效。无参数时打当前 model |
| `/tools list` | 复用 `cmd_tools_list` 同款渲染，REPL 内调 |
| `/tools disable <name>` / `/tools enable <name>` | 改 `agent.disabled_toolsets` 列表 + 调用 `agent._tool_schemas_cache = None` 失效缓存 |
| `/debug on` / `/debug off` | 切 `agent.verbose_logging`；root logger level 跟着改 |

`/save` 里 `set_session_title` 暂没移植（§3.4 wave 4 留给后续 title 子系统），所以会落到 stub 路径——但命令仍然出现在 `/help` 里且 tab 补全可见。

## 4. 流式输出与 `patch_stdout`

### 4.1 问题

prompt_toolkit 的 `PromptSession.prompt()` 持有 stdout 控制权——`print()` 直接喂 stdout 会被 prompt 行盖掉，或者把 prompt 行打飞。`patch_stdout()` context manager 把 `print()` 重定向到 prompt_toolkit 的渲染管线，让输出在 prompt 行之上。

### 4.2 `_run_turn` 流式

```python
def _run_turn(agent, line, history):
    def stream_callback(delta: str):
        sys.stdout.write(delta)
        sys.stdout.flush()
    with patch_stdout():
        result = agent.run_conversation(
            line,
            conversation_history=history,
            stream_callback=stream_callback,
        )
    sys.stdout.write("\n")
    return result["messages"]
```

`patch_stdout()` 的整段会把 `sys.stdout.write` / `print` 都拦下来送进 prompt_toolkit；prompt 行不会被 token delta 破坏。

### 4.3 ANSI 颜色

stream callback 写的是裸 token——大多数模型不输出 ANSI 转义，所以不需要 `_PT_ANSI` 包装。如果将来要给 assistant 输出上色（例如 markdown 渲染），用 `prompt_toolkit.print_formatted_text(ANSI(text))` 而非 `print()`。Phase 2.6 不做。

## 5. CLI 暴露面变化

主要是默认进 REPL 的体验升级，**没新子命令**：

```bash
python -m hermes_cli                   # 默认进入 REPL（wave 1+）
hermes chat                             # 同上，显式调
python cli.py                           # 直跑（绕过 hermes_cli wrapper）

# REPL 内：
> /help                                 # 列保留命令 + stub
> /tools                                # = /tools list
> /tools list
> /model                                # 当前 model
> /model qwen2.5:1.5b
> /resume sess_aaaa
> /save my-feature-discussion
> /debug on
> /history
> /clear
> /quit
```

历史文件：`~/.hermes/cli_history`（首次启动自动建）。

## 6. Stub / 测试模式

prompt_toolkit 输入难以单元测试——上游用 `pytest-asyncio` + 假事件 loop，phalanx 不引入这条路。Phase 2.6 测试策略分两层：

### 6.1 业务逻辑层（覆盖率重点）
- `_dispatch_slash` 直接函数调用，绕 PromptSession——传字符串 + 假 agent + 假 state，断言 handler 返回值与 state 改动
- 每个 `_cmd_*` handler 单测
- `SlashCommandCompleter` 调 `get_completions(Document(...))` 直接断言 `Completion` 列表
- `resolve_command` / `CommandDef` lookup 单测

### 6.2 集成层（smoke）
`_run_repl` 的 prompt_toolkit 路径不直接覆盖，但留一个 monkeypatch 测试：把 `PromptSession` 替换成假对象，让 `.prompt()` 按队列返预设输入；驱动 `_run_repl` 跑几轮验证：
- 普通文本 → `agent.run_conversation` 被调
- `/quit` → 主循环干净退出
- DB 故障不破坏 REPL（已在 wave 2 测过，REPL 路径再过一遍）

`_PT_AVAILABLE=False` 的降级路径用第三种测试：monkeypatch 模块顶部 flag，让代码走 `input()` 路径，喂 `StringIO` stdin。

## 7. 留给后续

按"代价 / 收益"排序：

1. **Title / `/save` 子系统**（~600 行）— 真正实现 `set_session_title` + 唯一索引冲突回退 + lineage 命名（"foo (2)"）；用户记忆友好但非阻断
2. **`@file:` / `@diff` / `@url:` reference**（~400 行 + 文件 cache）— 上游 SlashCommandCompleter 里的 `_extract_path_word` / `_resolve_attachment_path` 路径；Phase 2.6 完全没做
3. **多模态附件 `/image` `/paste`**（依赖 vision provider，§2.4 已支持但 CLI 路径未通）
4. **Status bar / `/statusbar`**（prompt_toolkit `bottom_toolbar` 路径，~200 行）— 显示 model / token / context-fill / cost
5. **Skin / theme / `/skin` `/personality`**（依赖独立 skin 系统）
6. **Background tasks `/background` `/queue` `/steer`**（依赖 BG runner）
7. **Worktree / checkpoint `/rollback` `/snapshot`**（依赖 worktree + checkpoint 系统）
8. **Plugin / skill 命令**（依赖 plugin / skill 系统）
9. **Voice mode `/voice`**（依赖 TTS）

§2.6 全 3 wave 完成后，phalanx CLI 已经"长得像 hermes"——历史持久化、tab 补全、流式、最常用的 ~10 个命令——足够日常 REPL 使用；剩余的"丰富但非必需"功能跟着 §2.7+ 各子系统逐个回归。
