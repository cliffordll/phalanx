# Phase 2.1 设计文档 — 最小 loop + 最小 CLI

> 配套阅读：
> - [`MIGRATION_PLAN.md`](MIGRATION_PLAN.md) §2.1 — 计划层面的产物清单与验收
> - [`ARCHITECTURE.md`](ARCHITECTURE.md) — 全局视图
> - [`phase-2.0-skeleton.md`](phase-2.0-skeleton.md) — 骨架与方案 B 隔离
> - [`phase-2.2-tools.md`](phase-2.2-tools.md) — 真实工具落地（紧接 §2.1）

本文记录 **§2.1 五个 wave 的核心 loop 裁剪、IterationBudget 设计、ToolRegistry 静态导入、八个调试子命令、flat argparse 决策与 35 个用例的测试基建**——把 §2.0 的"4 个 verbatim 模块 + pyproject + CI 全绿"推到一条可走通的 `oneshot` / `chat` / `tools run` / `doctor` 调试链。

## 0. 范围与 wave 划分

| Wave | 内容 | 提交 |
|---|---|---|
| 1 | `run_agent.py` 14123 → 698 行：保留 `AIAgent` / `IterationBudget` / `_OpenAIProxy` lazy / `_SafeWriter` / `run_conversation` / `chat` / `def main`；裁掉所有 multi-provider / streaming / cache / compression / guardrails / checkpoint / steer。`agent/retry_utils.py` (57) + `agent/error_classifier.py` (1000) 整体移植 | `c76818d` |
| 2 | 最小 CLI 外壳：`hermes_cli/main.py` 10439 → 447（argparse 分发器，8 子命令），`cli.py` 12043 → 216（朴素 `input()` REPL，接受所有上游 phase-pending kwargs）。`hermes_cli/{_parser,timeouts,colors,cli_output,env_loader,banner,__init__,__main__}` 全部移植；`hermes_cli/config.py` 4831 → 206 | `d326985` |
| 3 | `tools/registry.py` 537 行 near-verbatim（保留 `ToolRegistry` 单例 + `ToolEntry` + 七个 public 方法）；新增 `tools/echo_tool.py` smoke 工具；`tools/__init__.py` 静态 import 触发自注册；`run_agent.py` / `hermes_cli/main.py` 切到上游 `get_definitions`/`get_all_tool_names` 接口 | `3cf7e2f` |
| 4 | 35 个 pytest 用例：`tests/conftest.py`（`StubClient` + `stub_openai` + `reset_echo_call_count`），`test_minimal_loop.py` 18，`test_cli_oneshot.py` 10，`test_cli_tools.py` 7。CI pytest step 不再走 exit-5 容忍 | `8236a32` |
| 5 | argparse flat 修正：撤销 `parents=[]` 让全局 flag 跟上游一致只挂 top-level；副作用：`hermes oneshot --debug "..."` 报错（跟上游同样），正确写法 `hermes --debug oneshot "..."`；`test_doctor_flags_missing_model` 改用 `monkeypatch PHALANX_HOME` 隔离主机 config | `6660eb1` |

§2.1 落地后：35 个测试 ~1.2s 全过；`python -m hermes_cli oneshot "..."` 跑通完整闭环（system → user → assistant+tool_call → tool → assistant）；`hermes doctor` 探测 env / paths / API key 全绿；CI 不再依赖 pytest 空跑容忍。

## 1. AIAgent 裁剪：14123 → 698

### 1.1 保留 vs 删除矩阵

`run_agent.py` 上游 14123 行覆盖了 hermes-agent 整个核心。Wave 1 必须**强行收敛**到 700 行以内才能在剩余 phase 里逐项加回——否则就成了"整体抄过来再删"，跟"按需移植"的核心原则相悖。决策矩阵：

| 上游能力 | 处理 | 原因 |
|---|---|---|
| `class AIAgent` 主结构 | 保留 | 主入口，不能动 |
| `class IterationBudget` | 保留（独立类，§2 详述） | 子 agent 共享预算的接口必须早期立 |
| `_OpenAIProxy` lazy import | 保留 | 让 mock 测试能 monkeypatch `run_agent.OpenAI` |
| `_SafeWriter` / `_install_safe_stdio` | 保留 | Windows console 偶发 UnicodeEncodeError 防崩 |
| `run_conversation` 主循环 | **重写**（不是裁剪） | 上游主循环 ~1500 行涵盖多 provider 分支，phalanx 主循环 ~80 行只走 chat completions |
| `chat()` 便利方法 | 保留 | 一行调 `run_conversation` 的 sugar |
| `def main` (`fire.Fire`) | 保留 | `hermes-agent` console_script 入口 |
| Multi-provider 路由（anthropic / codex / responses）| 删 | §2.4 |
| Streaming（`stream=True` + delta 渲染）| 删 | §2.4 |
| Prompt caching / context compression / memory | 删 | §2.3 |
| Guardrails / steer / checkpoint / skill 注入 | 删 | §2.7+ |
| Trajectory 持久化 / SessionDB | 删 | §2.5 |
| Surrogate-unicode sanitize / 各类 callback | 删 | §2.7+ |

### 1.2 `__init__` 参数从 60+ 收到 12

上游 `AIAgent.__init__` 签名 60+ 参数。§2.1 收到这 12 个：

```text
base_url, api_key, model, max_iterations, tool_delay,
enabled_toolsets, disabled_toolsets, session_id,
verbose_logging, quiet_mode, max_tokens, iteration_budget
```

**取舍标准**：跑通 chat completions + tool dispatch 闭环必需的，留；其余一律默认值 / 不接收。后续 phase 加回参数时，在签名里 append（不重排），保持向后兼容。

### 1.3 公开符号严格对齐

mock 测试 / 上游 cherry-pick 都依赖**模块顶层符号名**对齐。Wave 1 的 `run_agent.py` 顶层 export 跟上游一致：

```text
AIAgent / IterationBudget / OpenAI / _OpenAIProxy / _SafeWriter /
_install_safe_stdio / _load_openai_cls / main
```

测试代码 `monkeypatch.setattr("run_agent.OpenAI", StubClient)`、`patch("run_agent._SafeWriter")` 等无需修改即可工作。这条契约后续每个 phase 都不准破——任何对外暴露的 symbol 必须保留同名。

## 2. IterationBudget — 跨 agent 的执行预算

### 2.1 数据结构

```python
class IterationBudget:
    max_total: int              # 上限（默认 90）
    _used: int                  # 已用计数
    _lock: threading.Lock       # 跨线程同步

    consume() -> bool           # 抢一个名额，True=允许，False=已满
    refund() -> None            # 退还（出错重试时偶尔用）
    used / remaining            # 只读属性
```

每跑一轮 `chat.completions` 就 `consume()` 一次；用完则主循环退出，`stop_reason="budget_exhausted"`。一轮 = 一次模型 HTTP + 0~N 次工具调用，整体只扣 1 格。

### 2.2 为什么单独抽出来当一个类

§2.1 阶段 phalanx 还没有子 agent，看上去 `IterationBudget` 完全可以用一个 `int` 计数器代替。但 wave 1 坚持以独立类形态落地，三个原因：

**1. 防止失控的工具循环烧爆 API**。模型偶尔陷入"调 search → 看不懂 → 再调一次"的死循环。`max_iterations=90` 是硬天花板，触发后状态机进 `budget_exhausted` 分支，给用户返回部分结果而不是无限等待。

**2. 可继承的全局额度**（关键设计）。`__init__` 提供 `iteration_budget: Optional[IterationBudget]` 参数：未来 §2.7+ 引入 `delegate_task` 工具，主 agent 派生子 agent 干独立活时，**把父级实例直接传过去**，所有 agent 共享同一个计数器：

```python
sub = AIAgent(..., iteration_budget=parent.iteration_budget)
```

否则一个主 agent + 5 个子 agent 各自 new 一个 `IterationBudget(90)` 等于把 cap 抬到 540 轮，硬天花板形同虚设。`threading.Lock` 也是为这种并发场景准备的——子 agent 极有可能跨线程跑。

**3. 重试不重复扣费**。API 失败重试时（`retry_utils.jittered_backoff`），一次成功响应只扣一格；`refund()` 让"被取消的 turn"能把名额退回。一个简单 `int` 没这种粒度。

### 2.3 与 `max_iterations` 的关系

- `self.max_iterations`：本次 AIAgent 实例自己的硬上限。
- `self.iteration_budget`：可跨实例共享的预算池。
- 主循环条件 `&&`：两个都要满足。子 agent 既受自己 `max_iterations` 限制，也受继承下来的父预算限制——谁先到先停。

### 2.4 §2.1 阶段的实际行为

phalanx 还没移植 `delegate_task`，每个 `AIAgent` 都自己 new 一个 `IterationBudget`，等价于一个简单的 90 轮上限。但接口已经留好——后续接入子 agent 派生时，传 `iteration_budget=parent.iteration_budget` 就能跑共享预算，零结构变更。

返回值 `iterations_used` 会向调用方透出实际烧了几轮，便于成本观测。

## 3. ToolRegistry：静态 import + 自注册

### 3.1 上游的"AST 自动发现"为什么不抄

上游 `tools/registry.py` 顶部有一个 `discover_builtin_tools()`：扫 `tools/*.py`，AST parse 每个文件找顶层 `registry.register(...)` 调用，按发现顺序触发 import。优势是加新工具不用改 `__init__.py`；代价是：

- 启动时多一次 AST 全扫描（~50ms）
- 错误诊断难——某个工具 import 失败时，错误栈里看不到是哪个 `__init__.py` 拉的
- 测试隔离难——`tests/` 里想 stub 某个工具，得先抑制 discover 路径

§2.1 选了**静态 import**：

```python
# tools/__init__.py
from tools import registry            # 暴露子模块（不触发 register）
from tools import echo_tool           # 顶层 registry.register("echo", ...)
```

更明确，启动时间不受影响，新增工具时改一行 `__init__.py`——可接受成本。等 §2.7+ 引入 plugin / skill 系统后再回头评估是否切回 AST discover。

### 3.2 ToolRegistry 单例

```python
# tools/registry.py
class ToolRegistry:
    _entries: Dict[str, ToolEntry]
    _toolset_aliases: Dict[str, str]
    _check_fn_cache: Dict[Callable, Tuple[float, bool]]

    def register(name, toolset, schema, handler, check_fn=None, ...) -> None
    def dispatch(name, args_dict, **kwargs) -> str
    def get_definitions(toolset_filter=None, *, quiet=False) -> List[Dict]
    def get_all_tool_names() -> List[str]
    def get_schema(name) -> Dict
    def get_toolset_for_tool(name) -> str
    def register_toolset_alias(alias, target) -> None

# 模块级单例
registry = ToolRegistry()
```

这 7 个 public 方法是上游 `cli.py` / `run_agent.py` / `hermes_cli/main.py` 全部依赖的接口。Wave 3 切表面时把 `run_agent.py` 中临时用过的 `list_schemas` / `list_tools` shim 全删，改用上游正名——后续 cherry-pick registry 修改自动落地。

### 3.3 两处 lazy fallback

phalanx wave 3 时 `tools/budget_config.py` 和 `model_tools.py` 都还没移植（前者 §2.2 wave 5，后者 §2.7+），但 `registry.py` 内部要用：

```python
try:
    from tools.budget_config import DEFAULT_RESULT_SIZE_CHARS
except ImportError:
    DEFAULT_RESULT_SIZE_CHARS = 64 * 1024   # inline fallback

try:
    from model_tools import _run_async
except ImportError:
    _run_async = lambda coro: asyncio.run(coro)   # asyncio.run fallback
```

两条 try 都"优先走上游符号、缺失才用 inline 替身"。后续 phase 一旦把对应模块抄进来，分支自动切到上游路径——**registry.py 本体不用改一行**。这是把"按需移植"和"verbatim 兼容"两条原则同时落到代码层面的具体办法。

注：§2.2 wave 5 引入 `tools/budget_config.py` 后，registry 内的 64KB inline 分支就被永久跳过——但保留代码不删，方便未来 cherry-pick 上游修改时该位置仍能编译。

### 3.4 `check_fn` TTL=30s 缓存

`get_definitions` 内部对每个 entry 调 `check_fn()` 判断"工具当前是否可用"（如 ping docker / 检查 playwright 安装）。每次 `run_conversation` 起头都要 build 一次 schema list，频繁调用 IO 探测会拖慢首响应。

`registry._check_fn_cached(check_fn)` 用 `{check_fn: (timestamp, bool)}` 字典缓存 30 秒。回收策略：进入 `get_definitions` 时打一次时间戳 → 跟 cache 里时间戳比较 → 过期重算。

§2.1 阶段唯一注册的工具 `echo` 的 `check_echo_requirements` 永远返 True，缓存对它无意义。但接口立起来——§2.2 web/terminal 工具一上就立刻起作用。

## 4. echo smoke tool 的角色

`tools/echo_tool.py` 83 行，做三件事：

1. **回显 `text`**——返回输入字符串，可选 `uppercase=True` 转大写
2. **per-process call counter**——module-level `_CALL_COUNT` 累加，每次调用 +1
3. **自注册到单例**——文件末尾 `registry.register("echo", ...)`

存在的全部目的：让"loop + tool dispatch 闭环"这件事**可被独立验证**——

- `tests/test_minimal_loop.py::test_loop_dispatches_real_echo_tool` 用真 registry + 真 echo handler 跑一次 mock-OpenAI 闭环，断言 `_CALL_COUNT >= 1` 证明 dispatch 真的发生了。
- `python -m hermes_cli tools run echo --args '{"text":"hi"}'` 直接调工具不进 loop，证明 registry 表面 OK。
- `python -m hermes_cli oneshot "用 echo 工具回显 hi"` 跑完整链路。

**为什么不直接用上游某个工具**：上游 `read_file` 1143 行 + `file_operations` 1287 行 + 依赖 `terminal_tool` + `binary_extensions` + `path_security`——抄进来违反 wave 3 的"最小化"原则。echo 是 phalanx 自创的极简工具，不在上游存在，但它**永远不会被删**——是后续每期回归测试的最稳定 smoke 点。

`reset_echo_call_count` autouse fixture（conftest.py）每个测试前清掉 `_CALL_COUNT`，避免用例间污染。

## 5. CLI 表面：8 子命令 + flat argparse

### 5.1 `hermes_cli/main.py` 的 8 子命令

```bash
hermes oneshot <message>      # 单 turn 问答
hermes chat                    # 朴素 input() REPL（§2.6 升级到 prompt_toolkit）
hermes tools list              # 列已注册工具
hermes tools run <name>        # 直调工具，绕开 loop
hermes config show             # dump ~/.phalanx/config.yaml
hermes config get <dot.key>    # 取一个配置项
hermes version                 # 打 phalanx 版本
hermes doctor                  # 检查 env / paths / API key
```

**为什么这 8 个**：每个都对应"调试某个层"——loop（oneshot/chat）/ tool 表面（tools list/run）/ 配置（config show/get）/ 版本与环境（version/doctor）。少一个调试链就断一段，多一个就跑题（§2.1 不做 session/logs/skills）。

### 5.2 cli.py 的"接受 + 忽略" pending kwargs

`hermes_cli/main.py:cmd_chat` 委托给 `cli.py:main`，符合上游模式。但上游 `cli.py:main` 签名带 30+ 参数（`provider=` `toolsets=` `image=` `resume=` `worktree=` `checkpoints=` ...）。Wave 2 的 `cli.py:main` 全部接受，**用 `logging.info("ignored, arrives in Phase X")` 静默跳过**：

```python
def main(message=None, provider=None, toolsets=None, image=None,
         resume=None, worktree=None, checkpoints=None, ...):
    if provider is not None:
        logger.info("--provider ignored, arrives in Phase 2.4")
    if image is not None:
        logger.info("--image ignored, arrives in Phase 2.7+ (multimodal)")
    ...
```

这样后续 phase 在 `hermes_cli/main.py:cmd_chat(args)` 处加一个 `cli_main(provider=args.provider, ...)` 时，**`cli.py` 无需任何改动**——签名已经长好了。是"phase 1 写好接口、后续 phase 填实"的典型实现。

### 5.3 flat argparse：为什么撤销 `parents=[]`

Wave 2 第一版用 `parents=[common_parser]` 给每个子命令都注入 `--debug` / `--quiet` 等全局 flag，让 `hermes oneshot --debug "..."` 也能跑。直觉上更友好。

Wave 5（`6660eb1`）把这个改回扁平结构——全局 flag 只挂 top-level，子命令需要哪个就**重新声明**：

```python
parser = argparse.ArgumentParser(...)
parser.add_argument("--debug", ...)               # top-level
parser.add_argument("--model", ...)               # top-level

sub = parser.add_subparsers(dest="cmd")
chat_p = sub.add_parser("chat")
chat_p.add_argument("-m", "--model", ...)         # 重新声明，跟 top-level 是两个独立 dest
```

**理由**：上游 `hermes_cli/_parser.py:build_top_level_parser()` 就是这种 flat 结构。phalanx 跟着上游走能让未来 cherry-pick argparse 修改时**完全不用改 parents 链**。代价是 UX 略不友好——`hermes oneshot --debug "..."` 报 unrecognized arguments，正确写法 `hermes --debug oneshot "..."`。

```bash
hermes --debug oneshot "..."        ✓
hermes oneshot --debug "..."        ✗  argparse: unrecognized argument
```

这条决策跟 §2.0 方案 B 是同性质的——**"上游 cherry-pick 友好"压倒"phalanx 自己更顺手"**。

### 5.4 `_Flags` 单例 + lazy 配置链

CLI flag → env var → config.yaml 三层覆盖在 `hermes_cli/main.py:_build_agent()` 里实现：

```python
model = _Flags.model
     or os.environ.get('PHALANX_MODEL')
     or os.environ.get('OPENAI_MODEL')
     or cfg_get(cfg, 'model', 'default')
```

`_Flags` 是个简单 module-level 容器类，`main()` 解析完 argv 后写入。子命令读取它而不是穿层传 `args`——避免每个 `cmd_*` 函数都得知道完整 argparse 结构。

## 6. 测试基建：StubClient + 35 用例

### 6.1 conftest 的 StubClient

`tests/conftest.py` 提供一组**协议级 stub**模拟 OpenAI SDK 的返回结构：

```python
@dataclass
class FakeFunction:    name: str; arguments: str
@dataclass
class FakeToolCall:    id: str; function: FakeFunction; type: str = "function"
@dataclass
class FakeMessage:     role: str; content: str | None; tool_calls: list | None
@dataclass
class FakeChoice:      message: FakeMessage; index: int = 0; finish_reason: str = "stop"
@dataclass
class FakeResponse:    choices: list[FakeChoice]; ...

class StubClient:
    """Pretends to be openai.OpenAI(); returns pre-queued FakeResponses."""
    def __init__(self, responses: list[FakeResponse]):
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
    def _create(self, **kw): return self._queue.pop(0)
```

`stub_openai(responses)` 是 `pytest fixture`，monkeypatch `run_agent.OpenAI` 为 `StubClient` 工厂——所有走 `run_conversation` 的测试都不接外网，且能**精确控制每一轮**模型返回什么 tool_call。

### 6.2 35 用例分布

| 文件 | 用例 | 覆盖面 |
|---|---|---|
| `tests/test_minimal_loop.py` | 18 | `IterationBudget` consume/refund/clamp；`AIAgent.__init__` 默认值；`run_conversation` 无工具 / 完整 round-trip / 未知工具 / `max_iterations` 用尽；`_serialize_tool_calls` / `_parse_tool_arguments` 边界 |
| `tests/test_cli_oneshot.py` | 10 | `version` / `doctor` 通过 + 缺失分支；`config show/get`；`oneshot` 单 turn / 完整工具 round-trip / 缺 message / `--debug` |
| `tests/test_cli_tools.py` | 7 | `tools list` 默认 + `--verbose`；`tools run` valid / `uppercase=True` / 无效 JSON / 非 object JSON / 未知工具 |

跑全套 1.2 秒。`-q` 选项默认开（`pyproject.toml`），输出干净——CI 里能直接看出哪条挂。

### 6.3 `reset_echo_call_count` autouse

```python
@pytest.fixture(autouse=True)
def reset_echo_call_count():
    import tools.echo_tool as et
    et._CALL_COUNT = 0
    yield
```

echo 工具的 module-level counter 没被任何用例直接断言，但**没 reset 就出现幽灵故障**：testA 跑完 counter=2，testB 顺序跑时 echo 的内部断言 `assert count == 1` 挂掉。autouse 把它清干净——`tests/` 任何用例都不需要主动调。

### 6.4 wave 5 的 isolation 漏洞

`test_doctor_flags_missing_model` 原本写法：构造一个不带 model 字段的 fake config，断言 `doctor` 报"未配置 model"。Wave 5 之前一直绿——但 wave 5 时本机刚装好 Ollama，往 `~/.phalanx/config.yaml` 落了 `model.default: qwen2.5:1.5b`。再跑测试发现 `doctor` 居然过——因为 fake config 被 fixture 取代不掉**主机真实 config**，`load_config()` 默默回退到磁盘文件。

修复：`monkeypatch.setenv("PHALANX_HOME", str(tmp_path))`。`get_hermes_home()` 走 env 优先级，瞬间切到一个空 tmp 目录——主机 config 不再可见。

这是**方案 B env 隔离的副产品**：env 变量名设计良好的话，测试隔离 trivial。如果当初没把 `HERMES_HOME` 改名 `PHALANX_HOME`，主机已装的 hermes-agent config 还会跟测试串扰一次。§2.0 方案 B 的回报又多算一笔。

## 7. 留给后续

§2.1 立的所有契约会被后续每个 phase 复用：

- `AIAgent` 顶层符号对齐 → §2.4 multi-provider 时新增 `_call_chat_completions` / `_call_anthropic_messages` 等同名函数，不重排
- `IterationBudget` 接口 → §2.7+ 子 agent 派生时一行传参就接通
- `tools/__init__.py` 静态 import → §2.2 加四行 import；§2.3 加 todo；§2.4 不加（流式不引入新工具）
- `_Flags` 三层覆盖 → §2.4 加 `--provider`；§2.5 加 `--resume`；§2.6 不动
- `StubClient` 测试基建 → 后续每个新 provider 都派生一个 `StubAnthropicClient` / `StubCodexClient`，但 fixture 套路相同

§2.1 之后的演进对照见 [`MIGRATION_PLAN.md`](MIGRATION_PLAN.md) §2.2—§2.7 与各 phase 设计文档。
