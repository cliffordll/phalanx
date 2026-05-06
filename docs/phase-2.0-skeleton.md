# Phase 2.0 设计文档 — 项目骨架与方案 B 隔离

> 配套阅读：
> - [`MIGRATION_PLAN.md`](MIGRATION_PLAN.md) §2.0 — 计划层面的产物清单与验收
> - [`ARCHITECTURE.md`](ARCHITECTURE.md) — 全局视图
> - [`guides/build-vs-setup-py.md`](guides/build-vs-setup-py.md) — 打包工具链选型
> - [`guides/ci-cd.md`](guides/ci-cd.md) — CI 工作流逐步骤说明

本文记录 **§2.0 三个 wave 的项目骨架搭建、`PHALANX_*` 方案 B 命名空间隔离、与上游 cherry-pick 兼容契约的最早形态**——把"空仓 + 一份 `.git`"推到"4 个 verbatim 顶层模块 + pyproject + CI 全绿"的最小可复用底座。

## 0. 范围与 wave 划分

| Wave | 内容 | 提交 |
|---|---|---|
| 1 | `pyproject.toml` 写最小依赖与两个 `console_script` 入口；`MANIFEST.in` / `.gitignore` 拷贝；目录占位 `agent/` `tools/` `hermes_cli/` `tests/`；4 个顶层 verbatim 模块 `hermes_constants.py` / `hermes_logging.py` / `hermes_time.py` / `utils.py`；`docs/MIGRATION_PLAN.md` + `docs/guides/build-vs-setup-py.md` | `ba41f00` |
| 2 | 方案 B：`HERMES_HOME` / `HERMES_TIMEZONE` / `HERMES_OPTIONAL_SKILLS` 全部改为 `PHALANX_*`；默认数据目录 `~/.hermes` → `~/.phalanx`；函数名 `get_hermes_home` 等保留不动 | `fdac96e` |
| 3 | `.github/workflows/ci.yml` 单 job 五步骤（lint / import smoke / pytest / build / twine check）；ruff 配置加进 `pyproject.toml`；`docs/guides/ci-cd.md` 写完；后续微调把 `workflow_dispatch` trigger 删掉 | `32e24f1` `fd2ab63` |

§2.0 落地后 `python -m build` 出 `phalanx-0.0.1.tar.gz` + `.whl`；`python -c "from hermes_constants import get_hermes_home; print(get_hermes_home())"` 打 `~/.phalanx`；CI 在 push 时跑过 lint/import/build——但**还没有 loop、没有 CLI 命令**，所有功能从 §2.1 开始。

## 1. 设计约束

phalanx 同时受**两条对立约束**夹住，§2.0 的每个决定都得同时满足：

1. **标识符严格保留**——文件名 / 类名 / 函数名 / 包名（`agent/` `tools/` `hermes_cli/`）跟 hermes-agent 上游一字不改。**理由**：上游每周还在更新，我们要能 `git apply --3way` 直接落补丁，不允许引入"phalanx 改名 → 每次 cherry-pick 都得手工 sed"的成本。
2. **运行时数据隔离**——若用户机器同时装了上游的 hermes-agent，phalanx **不能**读写上游的 `~/.hermes/` 配置 / sessions / .env，否则两边互相污染。

直接的冲突点：上游用 `HERMES_HOME` env 变量定位数据目录，函数叫 `get_hermes_home()`。如果 phalanx 沿用同一个变量名，两边运行时混在同一棵目录树里——违反约束 2；如果改函数名，每次 cherry-pick 都断 import——违反约束 1。

**方案 B**：env 变量名 / 用户数据路径换成 `PHALANX_*` / `~/.phalanx`，**函数名保留 `get_hermes_home` 等**——拆开 "用户面" 和 "代码面"，约束 1/2 各自满足。

详见 §3。

## 2. 顶层骨架：4 个 verbatim 模块

### 2.1 选型理由

Wave 1 选了 4 个**完全没有跨模块依赖、只用 stdlib + 极少第三方**的顶层模块作为最早的 verbatim 抄录目标：

| 文件 | 行数 | 第三方依赖 | 职责 |
|---|---|---|---|
| `hermes_constants.py` | 345 | 无（stdlib） | `get_hermes_home` / `get_config_path` / `is_termux` / `is_wsl` / `is_container` |
| `hermes_logging.py` | 389 | 无（stdlib） | `setup_logging` / `set_session_context` / 会话上下文 LogRecord 工厂 |
| `hermes_time.py` | 104 | 无（stdlib） | `now()` 时区感知 datetime |
| `utils.py` | 297 | `pyyaml` | `atomic_json_write` / `atomic_yaml_write` / `base_url_hostname` / env helpers |

**为什么先抄这 4 个**：

1. 它们是 §2.1 起所有上层模块的**叶子依赖**——`run_agent.py` / `cli.py` / `hermes_cli/main.py` 都会直接 `from hermes_constants import ...`。先把叶子立住，后面任何一期都不需要再回头改这层。
2. 跨模块依赖为 0，导入图就是一行——出问题排查面极小。
3. 上游这 4 个文件相对**稳定**（不是 14000 行的 `run_agent.py` 那种月度大改类），verbatim 抄进来未来 cherry-pick 成本低。

### 2.2 verbatim 的边界

"verbatim" 指**字节级保留**，包括：

- 函数 / 类 / 变量名（`get_hermes_home` / `_profile_fallback_warned` / `display_hermes_home`）
- import 语句的顺序
- 注释里对上游 issue 编号的引用（`https://github.com/NousResearch/hermes-agent/issues/18594`）——这些注释**不删**，方便未来读上游 issue tracker 对照
- docstring 整段

**唯一改动**：`HERMES_HOME` / `HERMES_TIMEZONE` / `HERMES_OPTIONAL_SKILLS` 三个 env 变量字面量改成 `PHALANX_*`（Wave 2 的工作）。docstring 里同步改。

这条铁律贯穿后续所有 phase——"verbatim 模块" 三个字在 `MIGRATION_PLAN.md` 模块矩阵的"上游对照"列里反复出现，每次都同一含义。

## 3. 方案 B：env 命名空间隔离

### 3.1 三个被改的 env 变量

| 上游 | phalanx |
|---|---|
| `HERMES_HOME` | `PHALANX_HOME` |
| `HERMES_TIMEZONE` | `PHALANX_TIMEZONE` |
| `HERMES_OPTIONAL_SKILLS` | `PHALANX_OPTIONAL_SKILLS` |

被改的共同特征：**指向"用户数据归属"的变量**——目录、时区、skills 来源。这些一旦混用，两边运行时就会互相覆盖（典型场景：phalanx 写 session 落到 `~/.hermes/state.db`，下次开 hermes-agent 拿到一个未知 schema 的 session 表，启动报错）。

### 3.2 没改的 env 变量

```text
HERMES_QUIET                # 运行时控制：suppress logs
HERMES_INFERENCE_MODEL      # 运行时控制：默认推理模型
HERMES_ACCEPT_HOOKS         # 运行时控制：是否运行 user hooks
OPENAI_API_KEY / OPENAI_BASE_URL  # 上游标准 env，跨工具通用
```

这些都是**运行时行为开关**，不写盘——两边即使同时设置也不会损坏对方数据。Phase 2.0 完全没碰，全留给后续期 verbatim 移植上游代码。

### 3.3 为什么不顺手把函数也改名

`hermes_constants.py` 顶层有 30+ 个调用点会 `from hermes_constants import get_hermes_home`，分布在后续 phase 才会落地的 `run_agent.py` / `agent/*.py` / `tools/*.py` / `hermes_cli/*.py` 里。如果改成 `get_phalanx_home`：

- 上游补丁里凡是改这些调用点的都得手工搜替——cherry-pick 成本变 O(N)
- 上游函数 docstring 里互相引用（"see also `display_hermes_home`"），改名后这些指针全断

所以：函数名 / 类名一律保留，只改字符串字面量。这条决策直接产生方案 B 的核心特征——**代码层面看仍然是 hermes，运行时数据归属切到 phalanx**。

未来 cherry-pick 上游修改时绝大多数 patch 直接 `git apply` 就能落；遇到改 `HERMES_HOME` 字面量的（罕见，因为上游内部叫这名是稳定的）一次性 sed 即可：

```bash
git apply --3way patch.diff
sed -i 's/HERMES_HOME/PHALANX_HOME/g; s/~\/\.hermes/~\/\.phalanx/g; s/HERMES_TIMEZONE/PHALANX_TIMEZONE/g' \
    $(grep -rl HERMES_ phalanx/)
```

### 3.4 默认路径 `~/.phalanx`

`get_hermes_home()` 在 `PHALANX_HOME` 未设时返 `Path.home() / ".phalanx"`。这是**唯一一处会落到磁盘的目录默认值**——所有衍生路径（`get_config_path` / `get_skills_dir` / `get_env_file_path` 等）都从它派生。

后续期会陆续往 `~/.phalanx/` 下铺：

- `~/.phalanx/.env`（§2.0 已支持，env_loader 会读）
- `~/.phalanx/config.yaml`（§2.1 引入）
- `~/.phalanx/state.db`（§2.5 引入 SessionDB）
- `~/.phalanx/cli_history`（§2.6 引入 prompt_toolkit history）

§2.0 不主动建任何文件——只把"路径解析逻辑"立起来。

## 4. 打包：双 console_script + py-modules + packages

### 4.1 为什么有两个入口

```toml
[project.scripts]
hermes       = "hermes_cli.main:main"
hermes-agent = "run_agent:main"
```

跟上游一对一对齐：

- `hermes`——用户主入口，走 argparse 子命令分发器（§2.1 才会有真实子命令）
- `hermes-agent`——旁路入口，用 `fire` 直 fire 进 `AIAgent`，bypass argparse；调试时常用

**为什么不用 `phalanx` / `phalanx-agent`**：相同的"上游 cherry-pick 友好"约束。上游 README / scripts / CI 里大量引用 `hermes ...` 命令；改名等于把这些文档全部腐化掉。包名（`name = "phalanx"`）已经告诉用户这是哪个产品，console_script 是命令名而不是包名——保留 `hermes` 不会引发任何混淆。

### 4.2 `py-modules` vs `packages.find`

```toml
[tool.setuptools]
py-modules = [
    "hermes_constants",
    "hermes_logging",
    "hermes_time",
    "utils",
]

[tool.setuptools.packages.find]
include = ["agent", "agent.*", "tools", "tools.*", "hermes_cli", "hermes_cli.*"]
```

setuptools 区分两种顶层 Python 实体：

- **module**——单文件 `.py`，没有 `__init__.py`。`py-modules` 列表里逐个写名字。
- **package**——目录 + `__init__.py`，可以含子包。`packages.find` 用 glob 自动扫。

phalanx 顶层四个模块（constants/logging/time/utils）是**单文件**，所以走 `py-modules`；`agent/` `tools/` `hermes_cli/` 是带 `__init__.py` 的**包**，走 `packages.find`。混合使用是 setuptools 的标准做法，两组 include 各自起作用，互不冲突。

### 4.3 Phase 0 引入的依赖

Wave 1 的 `pyproject.toml` 一次性写齐了 §2.1 起所有期会用到的依赖（除了 `prompt_toolkit` 推到 §2.6）：

```toml
dependencies = [
    "pyyaml>=6.0.2,<7",          # utils.atomic_yaml_write / hermes_logging
    "openai>=2.21.0,<3",         # §2.1 chat completions
    "anthropic>=0.39.0,<1",      # §2.4 anthropic provider
    "httpx[socks]>=0.28.1,<1",   # §2.2 web tools
    "tenacity>=9.1.4,<10",       # §2.1 retry
    "pydantic>=2.12.5,<3",       # 各 schema
    "python-dotenv>=1.2.1,<2",   # §2.1 env_loader
    "jinja2>=3.1.5,<4",          # §2.3 prompt template
    "rich>=14.3.3,<15",          # CLI 输出
    "requests>=2.33.0,<3",       # 工具内零散 HTTP
    "fire>=0.7.1,<1",            # run_agent.py / cli.py 旁路
    "jsonschema>=4.20,<5",       # §2.2 tools dry-run
]
```

**为什么一次性写齐**：避免每个 phase 都改 `pyproject.toml` 触发 wheel 重建 / `pip install -e .` 重装。dev 体验上一次安装吃齐所有依赖最安静。代价：早期空仓 `pip install` 比上游慢 20-30 秒——可接受。

`prompt_toolkit` 例外：它只 `cli.py` 一处用，且明确推到 §2.6。Wave 1 不引入避免误导（让别人以为 §2.0 就要做 REPL）。

### 4.4 验收：`python -m build`

Wave 1 通过的标志是：

```bash
python -m build       # → dist/phalanx-0.0.1-py3-none-any.whl + .tar.gz
twine check dist/*    # 元数据合规
```

详细的 build vs setup.py 选型对照见 [`guides/build-vs-setup-py.md`](guides/build-vs-setup-py.md)。

## 5. CI：单 job 五步骤

### 5.1 工作流形态

`.github/workflows/ci.yml` 走"扁平单 job"路线，五个 step 串行：

```yaml
on:
    push:        { branches: [main] }
    pull_request: { branches: [main] }

jobs:
    ci:
        runs-on: ubuntu-latest
        steps:
            - Checkout
            - Set up Python (3.11, cache=pip)
            - Install (-e ".[dev]" + build + twine)
            - Lint (ruff check .)
            - Import smoke (4 个 verbatim 模块 + 3 个空 package + get_hermes_home 默认值)
            - Run tests (pytest, exit-5 容忍)
            - Build sdist + wheel
            - Verify metadata (twine check)
```

**concurrency**：`{workflow}-{ref}` 并发组 + `cancel-in-progress: true`——同一分支后续 push 自动取消上一次运行，省 minutes。

### 5.2 为什么不分 lint / test / build 多 job

**理由**：单 job 内五步骤共用一次 `pip install -e ".[dev]"`，安装时间约 25 秒；分多 job 每个都得装一次，总安装时间翻倍。在 §2.0 这个阶段（无矩阵 / 无并行收益），单 job 收益大于代价。

未来 phase 引入 Python 矩阵（3.11 / 3.12）或多 OS（Linux / Windows / macOS）时再拆——具体演进路径见 [`guides/ci-cd.md`](guides/ci-cd.md) §8.2 / §8.3。

### 5.3 ruff 选型保守

```toml
[tool.ruff.lint]
select = ["F", "E9"]
```

只开 **F**（pyflakes：未定义变量、未用 import、未到达代码）+ **E9**（语法 / 缩进错）。**没开** I（import 排序）/ B（bugbear）/ N（命名）/ UP（pyupgrade）等。

**理由**：phalanx 大量 verbatim 抄上游代码，上游不一定通过 I/B/N/UP——开了立刻一片红，要么改上游码（破坏 verbatim 约束）要么 `# noqa` 海量注释（污染 cherry-pick）。F + E9 抓的是"真 bug 不是风格"，对 verbatim 友好。

未来上游 ruff 配置稳定后，phalanx 的 ruff 选型会跟着上游同步——这是**约束 1 在 lint 层面的延伸**。

### 5.4 pytest exit-5 容忍

```bash
python -m pytest tests/ || ec=$?
ec=${ec:-0}
if [ "$ec" = "5" ]; then
    echo "::notice ::No tests collected yet"
    exit 0
fi
exit "$ec"
```

pytest 没收集到任何用例时退出码是 5——`§2.0` 阶段 `tests/` 是空目录，naive `pytest` 会以 exit 5 把 CI 染红。Wave 3 加这段把 5 转成 0，并通过 `::notice ::` 在 Actions UI 标一句"早期 phase 容忍空 tests"。

§2.1 落地后 `tests/` 至少有 35 个用例，exit-5 这条 if 永远走不到——但代码留着不删，未来若临时把所有 test 全 skip 掉也不至于把 CI 整红。

### 5.5 `workflow_dispatch` 的反复

Wave 3 第一次提交（`32e24f1`）写了：

```yaml
on:
    push: ...
    pull_request: ...
    workflow_dispatch:    # 手动触发
```

21 分钟后又删掉（`fd2ab63`）——理由：手动触发的几乎所有用例（重跑当前 main / 测试 workflow 改动）都能通过 GitHub UI 上的 **Re-run** 按钮或一次空 commit 替代，多一个入口反而增加误触概率。

> 注：§2.2（`cbd8281`）开发过程中又恢复了 `workflow_dispatch`，理由是 §2.2 多 wave 调试时确实需要"不写代码就重新跑一次"。这个反转跟 §2.0 无关——记录在此仅为时间线完整性。

## 6. Phase 2.0 不做什么

明确划线避免范围蔓延：

- **没有 loop**——`run_agent.py` / `AIAgent` / `IterationBudget` 全推到 §2.1
- **没有 CLI 命令**——`hermes_cli/main.py` 是空的（`__init__.py` 占位）；任何子命令推到 §2.1
- **没有工具**——`tools/__init__.py` 空；`registry.py` 推到 §2.1
- **没有 anthropic / streaming / sessions / REPL**——全部推到对应 phase
- **没有真实测试**——`tests/__init__.py` 占位，pytest 空跑

§2.0 唯一可执行的"功能"就是 `python -c "from hermes_constants import get_hermes_home; print(get_hermes_home())"`——用来证明骨架立起来了。

## 7. 留给后续

§2.0 立的契约会在所有后续 phase 复用，**不会被打破**：

- 标识符保留 + env 隔离的方案 B 模式 → §2.1 起每个 phase 都遵守
- 双 console_script 入口（`hermes` / `hermes-agent`）→ §2.1 才填 `hermes_cli.main:main` 与 `run_agent:main` 的真实代码
- 单 job CI 工作流 → §2.1+ 加测试时 step 数会涨，但 job 形态不变到 §2.7+ 才考虑拆

§2.0 之后的演进对照见 [`MIGRATION_PLAN.md`](MIGRATION_PLAN.md) §2.1—§2.7。
