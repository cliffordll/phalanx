# Phase 2.2 设计文档 — 真实工具栈落地与上游对齐

> 配套阅读：
> - [`MIGRATION_PLAN.md`](MIGRATION_PLAN.md) §2.2 — 计划层面的产物清单与验收
> - [`ARCHITECTURE.md`](ARCHITECTURE.md) — 全局视图（§3 模块矩阵 + §5 工具系统）
> - [`phase-2.1-minimal-loop.md`](phase-2.1-minimal-loop.md) — `tools/registry.py` 立起单例的前期工作
> - [`run-loop-vs-upstream.md`](run-loop-vs-upstream.md) — wave 6 配套：phalanx 主循环 vs 上游 3400 行的差距盘点

本文记录 **§2.2 五个 wave 把 echo 一根毛升级成 9 个真实工具的过程**——file/todo/terminal/web 工具落地 + tool_result_storage 三层防御 + 工具调用子系统按上游签名对齐。`echo` 之外新增 8 个工具，模型从此能读文件、跑命令、抓网页、维护待办列表。

## 0. 范围与 wave 划分

| Wave | 内容 | 提交 |
|---|---|---|
| 1 | file 工具栈：`file_tools` (1143) + `file_operations` (1287) + `path_security` (43) + `binary_extensions` (42) + `fuzzy_match` (704) + `tool_output_limits` (92) verbatim；`terminal_tool` 320 行 minimal port（仅 `LocalTerminalEnv`）；`file_state` 73 行 no-op shim；`agent/file_safety` (111) + `agent/redact` (394) verbatim | `cbd8281` |
| 2 | `todo_tool` 277 行 verbatim；`AIAgent` 持有 `TodoStore` 实例 + `dispatch` 透传 `store=`；CLI `tools run todo` 用 ephemeral store 让 smoke 可跑 | `cbd8281` |
| 4 | web 工具栈：`web_tools` (2153) + `url_safety` (231) + `website_policy` (282) + `managed_tool_gateway` (167) + `tool_backend_helpers` (144) + `debug_helpers` (105) verbatim；`agent/auxiliary_client` 89 行 shim（保留 4 个符号签名）；`interrupt` 98 行提前移植（解 wave 4 的 8 处 lazy import） | `cbd8281` |
| 5 | `tool_result_storage` 226 + `budget_config` 52 verbatim；`maybe_persist_tool_result` / `enforce_turn_budget` 接进 `run_conversation`；registry 内 64KB inline fallback 永久跳过；`LocalTerminalEnv.execute` 加 Windows 16KB 命令行 spill 适配 | `cbd8281` |
| 6 | tool-exec 子系统对齐上游：`_execute_tool_calls` / `_execute_tool_calls_sequential` / `_execute_tool_calls_concurrent` / `_invoke_tool` / `_should_parallelize_tool_batch` 按上游签名重排；CLI 加 `tools schema <name>` / `tools dry-run <name>`；`jsonschema` 列依赖；新增 `docs/run-loop-vs-upstream.md` 盘 phalanx vs 上游差距 | `7879dd6` |

§2.2 落地后已注册工具：`echo` `read_file` `write_file` `patch` `search_files` `terminal` `todo` `web_search` `web_extract`（9 个）；§2.2 不存在 wave 3——原计划 wave 3 是单独的 terminal_tool wave，最终跟 file 工具栈一起塞进 wave 1。

## 1. Wave 1：文件 / 终端工具栈

### 1.1 6 个 verbatim 模块的耦合

```text
file_tools.py             (1143)  read_file / write_file / patch / search_files 入口
file_operations.py        (1287)  ShellFileOperations: sed/head/tail/grep/rg 后端
path_security.py             43   敏感系统路径黑名单
binary_extensions.py         42   二进制扩展名集合
fuzzy_match.py              704   patch 用的 9 策略模糊匹配
tool_output_limits.py        92   max_lines / max_line_length 配置入口
```

这 6 个文件互相紧耦合：`file_tools.read_file` 调 `file_operations.ShellFileOperations.read`，后者调 `tool_output_limits.get_max_lines` 决定截断点；`file_tools.patch` 调 `fuzzy_match.fuzzy_find_and_replace`；`write_file` / `read_file` 都过 `path_security.is_sensitive_path` 黑名单。任何一个抄漏 / 改名都会让 import 链断。

策略：**整组打包 verbatim**——不挑挑拣拣，全抄进来再说。后续期想减重时再单独审 fuzzy_match 是不是真的需要 9 策略。

### 1.2 terminal_tool 为什么 minimal-port

上游 `tools/terminal_tool.py` 约 3000 行覆盖：

- `LocalTerminalEnv`（subprocess + bash）
- `DockerTerminalEnv`（docker exec）
- `SshTerminalEnv`（paramiko）
- `ModalTerminalEnv` / `DaytonaTerminalEnv` / `VercelSandboxEnv`
- `process_registry`（持久 VM、process 复用、env passthrough）
- `shell_hooks`（pre/post hook 钩子）

phalanx wave 1 只要"一次性子进程执行 + 超时"——其他 backend 全推到 §2.7+。最终落到 320 行：

- `LocalTerminalEnv.execute(command, timeout, ...)`：subprocess 启 bash 跑命令、捕 stdout/stderr、超时 kill
- module-level globals：`_active_environments` / `_env_lock` / `_creation_locks` ——给 `file_tools.get_or_create_file_ops` 用
- `cleanup_vm` / `get_active_env` / `is_persistent_env` 等 stub 函数：上游 `run_agent.py` 卸载阶段会调，不能让 import 断

**不在 320 行里的**：docker / ssh / modal / process_registry / shell_hooks。后续 phase 接入时把对应 backend 类塞进同一个文件即可。

### 1.3 LocalTerminalEnv 的 Windows ARG_MAX 适配（wave 5 反哺 wave 1）

这是 wave 5 落地后**才需要的**适配，但代码改动落在 wave 1 写的 `terminal_tool.py` 里——所以记在 wave 1 这里。

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

### 1.4 file_state 是一个 no-op shim

上游 `tools/file_state.py` 332 行实现 cross-tool 读写时间戳：跨工具 / 跨子 agent 检测"另一个工具刚改过这个文件，你的 read 已过时"，并对并发写串行化。phalanx wave 1 用 73 行的 shim 替代——所有函数返回安全默认值（无追踪 / 无 staleness 警告 / 无锁）。

shim 严格保留**所有公开符号**：`FileStateRegistry` / `get_registry` / `record_read` / `note_write` / `check_stale` / `lock_path` / `writes_since` / `known_reads`。`file_tools.py` 和 `file_operations.py` 的 import 不需要任何修改，未来直接用上游文件覆盖即可。

代价：单 agent 单线程下 file 工具的并发 / staleness 警告暂时缺失。§2.7+ 引入子 agent 后必须移植真实版本——届时只换文件，不改 import。

### 1.5 import 顺序约束

`tools/__init__.py`：

```python
from tools import registry        # 暴露子模块（不触发 register）
from tools import echo_tool       # phase 2.1
from tools import terminal_tool   # 必须在 file_tools 之前
from tools import file_tools      # 用到 terminal_tool 的 module-level globals
from tools import todo_tool       # phase 2.2 wave 2
from tools import web_tools       # phase 2.2 wave 4
```

`file_tools.get_or_create_file_ops` 调 `terminal_tool._active_environments` / `_env_lock` 等 module-level globals——必须先 import 才能拿到。其他工具间无顺序依赖。

## 2. Wave 2：TodoStore 的会话级共享

`tools/todo_tool.py` 的 `TodoStore` 是一个 in-memory 列表，**实例由 AIAgent 持有，跨工具调用共享**。这意味着：

```text
turn 1：todo write → TodoStore = [task1, task2, task3]
turn 2：read_file → 不影响 TodoStore
turn 3：todo read  → 拿回 [task1, task2, task3]   ✓
```

实现上靠 `registry.dispatch(name, args, **kwargs)` 透传 `store=self._todo_store`，handler 收到 `**kw` 后 `kw.get("store")` 取出。这是工具系统里**第一个**需要 per-session 状态的工具，也是为什么 `dispatch` 签名长成 `(name, args, **kwargs)` 而不是 `(name, args)`——为后续这种带状态的工具留接口。

CLI `tools run todo` 路径不经过 AIAgent，所以 `hermes_cli/main.py:cmd_tools_run` 检测到 `args.name == "todo"` 时会**临时 new 一个 TodoStore**，让 smoke test 能跑（每次进程重启即清空，符合预期）。这条特判让 todo 成为 CLI 直调时**唯一被特殊处理**的工具——后续若加更多带 per-session 状态的工具，要么集中给 `cmd_tools_run` 一个 `_BUILD_EPHEMERAL_STORE` 注册表，要么把"工具状态注入"抽出共用 helper。§2.2 阶段只一个 todo，特判不是问题。

## 3. Wave 4：web 工具栈

### 3.1 移植清单

```text
web_tools.py              (2153)  web_search / web_extract / web_crawl 入口（多 backend）
url_safety.py              231    IP/SSRF 黑白名单
website_policy.py          282    域名级访问策略
managed_tool_gateway.py    167    Nous gateway 路由
tool_backend_helpers.py    144    managed_nous_tools_enabled / prefers_gateway 等开关
debug_helpers.py           105    DebugSession 调试日志
interrupt.py                98    per-thread 中断信号（提前引入，§3.3）
```

`web_tools.py` 一个文件 2153 行——里面同时管 Firecrawl / Tavily / Exa / Parallel 四个 search backend、`web_extract` 的 markdown 转换、page-level LLM 摘要、SSRF 防护、`@reference` 解析。phalanx 全部 verbatim——**不动它，除了让 import 跑通**。

### 3.2 auxiliary_client 的"返回原文"降级路径

上游 `agent/auxiliary_client.py` 3914 行——是一个独立的 LLM 客户端，专门用来对 web_extract 抓回的网页做 markdown 摘要。phalanx wave 4 不想跟上游 OpenRouter / 凭据池一起搬，于是写了 89 行 shim：

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

未来移植真实 `auxiliary_client.py` 时，**只换文件，不改任何 web_tools 代码**——这就是为什么 shim 严格保留 4 个符号的签名（`async_call_llm` / `extract_content_or_reasoning` / `get_async_text_auxiliary_client` / `get_auxiliary_extra_body`）。这跟 §1.4 的 `file_state` shim 是同一招——给上游某个超大文件留一个"占位但能跑"的薄壳，等到真实文件移植时无缝替换。

### 3.3 interrupt 的提前引入

按原计划 `tools/interrupt.py` 应在 wave 5 引入，但 wave 4 的 `web_tools.py` 在 8 处用了 `from tools.interrupt import is_interrupted`（lazy import）。两个选择：

1. 给 8 处都打 `try/except ImportError` 补丁（破坏 verbatim 原则）
2. 直接把 98 行 `interrupt.py` 提前 verbatim 复制过来

选了 2。这意味着 wave 5 的实质内容只剩 `tool_result_storage` + `budget_config`。

`interrupt` 提供按线程隔离的中断信号（`set_interrupt(active, thread_id)` / `is_interrupted()`），背后是 `set[int]` + `threading.Lock`。当前没有调用方 `set_interrupt(True)`——所以 `is_interrupted()` 永远返回 False，不影响功能。预留给后续 Ctrl+C 处理 / gateway 多 session 模式。

## 4. Wave 5：tool_result_storage 三层防御

### 4.1 问题：单工具就能撑爆 context

工具返回大输出（一次 search 命中 1000 行、一次 read_file 50KB）会迅速吃掉模型 context。一个 turn 里 5 个工具调用 + 每个 50KB = 250KB——直接顶到 GPT-4 / Claude context 上限的尾部，下一轮模型就忘了 system prompt。

`tools/tool_result_storage.py` 用三层防御逐级拦截：

| 层 | 入口 | 触发条件 | 行为 |
|---|---|---|---|
| 1 — per-tool cap | 工具自己 | 工具内部判断（如 search 截到 100 条） | 工具返回前先截断；唯一工具作者能控的层 |
| 2 — per-result persist | `maybe_persist_tool_result` | 单结果 > `registry.get_max_result_size(tool_name)`（默认 100 KB） | 通过 `env.execute()` 把全文写入沙箱 `/tmp/hermes-results/{tool_use_id}.txt`，模型只看到 1.5 KB preview + `<persisted-output>` 块带文件路径，可用 `read_file` 按需读片段 |
| 3 — per-turn budget | `enforce_turn_budget` | 一轮所有工具结果之和 > 200 KB | 按已用字符降序，把最大的未持久化结果继续 spill 到沙箱，直到 aggregate 落到 budget 以下 |

接入位置在 `run_agent.py:_dispatch_tool_call` 之后逐工具包一层（layer 2），整轮 for 循环结束后调一次 `enforce_turn_budget`（layer 3）。layer 1 在每个工具的 handler 内部各自处理。

### 4.2 PINNED_THRESHOLDS：避免 persist 循环

`PINNED_THRESHOLDS` 把 `read_file` 的阈值钉死为 `inf`——避免"persist→read→persist"无限循环：

1. 模型调 `web_extract` 拿到 200KB 网页 → layer 2 spill 到沙箱，preview 给 1.5KB
2. 模型读 preview 看到 `<persisted-output> path=/tmp/.../abc.txt`，调 `read_file path=/tmp/.../abc.txt`
3. 如果 `read_file` 也按 100KB 阈值 spill，模型又拿到一个 preview——永远拿不到原文

钉死 `read_file` 阈值后，模型一旦决定主动读 persisted 文件，**就让它读完**。其他工具该 spill 的 spill。

### 4.3 budget_config 替换 registry 内 inline fallback

§2.1 wave 3 时 `tools/registry.py` 内有一段 inline fallback：

```python
try:
    from tools.budget_config import DEFAULT_RESULT_SIZE_CHARS
except ImportError:
    DEFAULT_RESULT_SIZE_CHARS = 64 * 1024   # inline
```

wave 5 引入 `budget_config.py` 后，`try` 分支永久得手——但代码不删，留着方便上游 cherry-pick。`DEFAULT_RESULT_SIZE_CHARS` 上游定义为 100KB，phalanx 跟齐——之前的 64KB inline fallback 历史归零。

## 5. Wave 6：tool-exec 子系统对齐上游

### 5.1 五个新方法的签名

wave 6 把 `_dispatch_tool_call` 单一函数拆成五个，每个签名跟上游一字不差：

```python
_execute_tool_calls(tool_calls, ...)              # 总入口
  ├── _should_parallelize_tool_batch(tool_calls)  # 决定走串行还是并行
  ├── _execute_tool_calls_sequential(...)         # 串行实现（phalanx 唯一走通的）
  ├── _execute_tool_calls_concurrent(...)         # 并行 stub，目前 fallback 到串行
  └── _invoke_tool(name, args, ...)               # 单个工具调用
```

**phalanx 实际行为**：`_should_parallelize_tool_batch` 永远返回 False；`_execute_tool_calls_concurrent` 直接调 `_execute_tool_calls_sequential`。**接口立起来不实现并行**——后续 cherry-pick 上游"工具并行执行"修改时，patch 里改的方法名 / 签名都对得上，无需做 phalanx 私有 sed。

`_invoke_tool` 内部按上游把"agent-level 工具"和"registry 工具"分开：

```python
if name == "todo":
    return self._invoke_todo_tool(args)              # 走 TodoStore
elif name in ("memory", "clarify", "delegate_task", "session_search"):
    pass                                              # placeholder，全部 §2.7+
else:
    return registry.dispatch(name, args, ...)        # 兜底到 registry
```

agent-level 工具的 placeholder 列表不删——上游每加一个就跟着加一行，phalanx 啥时候真用再实现。

### 5.2 schema / dry-run：CLI 调试增量

`hermes tools schema <name>`：

```bash
$ hermes tools schema read_file
{
  "type": "object",
  "properties": {
    "path": {"type": "string", "description": "..."},
    "max_lines": {"type": "integer", "default": 1000},
    ...
  },
  "required": ["path"]
}
```

直接打 registry 注册时挂的 JSON Schema——开发新 prompt / 调 `--args` 时省去翻源码。

`hermes tools dry-run <name> --args <json>`：

```bash
$ hermes tools dry-run read_file --args '{"path": "."}'
✓ args validate against read_file schema
$ hermes tools dry-run write_file --args '{"path": "x.txt", "content": "hi"}'
✓ args validate against write_file schema (handler not invoked, file not written)
```

走 `jsonschema.validate(args, schema)`——纯校验，不调 handler。**`dry-run write_file` 不创建文件**是 wave 6 测试明确断言的——避免有人误把 dry-run 当成"快速跑一下试试"。

`jsonschema>=4.20,<5` 这条依赖 wave 6 才显式列进 `pyproject.toml`——之前一直被 `openai` SDK 间接拉进来，但 wave 6 起 phalanx 自己直调它，必须列为顶层依赖。

### 5.3 wave 6 的副产物：run-loop-vs-upstream.md

wave 6 改 `run_conversation` 时发现：phalanx 当前主循环 ~120 行，上游 ~3400 行——差 28 倍。差距全在哪些 phase 补回？写了 [`run-loop-vs-upstream.md`](run-loop-vs-upstream.md) 把缺口列成清单——每条标 phase 归属（§2.3 / §2.4 / §2.5 / §2.7+），后续每期都可以从中挑相关项实现。

## 6. 留给后续

§2.2 立的工具系统契约会被后续 phase 复用：

- `dispatch(name, args, **kwargs)` 透传 → §2.3 加 `prompt_builder` 工具时透传 `prompt_engine=`；§2.5 加 session 工具时透传 `session_db=`
- shim 模式（`auxiliary_client` / `file_state`）→ 后续每个"上游某文件超大、phalanx 暂不要全部"的场景都用同样套路：保留所有 public 符号、返回安全默认
- tool-exec 上游签名 → §2.4 multi-provider 时 `_invoke_tool` 内部分支按 provider 加；§2.7+ 子 agent 加并行执行时填 `_execute_tool_calls_concurrent`
- `tool_result_storage` 三层防御 → 任何后续期新增大输出工具，自动受三层保护，不需要额外接入

§2.2 之后的演进对照见 [`MIGRATION_PLAN.md`](MIGRATION_PLAN.md) §2.3—§2.7 与各 phase 设计文档。
