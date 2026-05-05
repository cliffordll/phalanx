# Phalanx CI/CD 指南

> 配套文件：`.github/workflows/ci.yml`、`pyproject.toml` 的 `[tool.ruff]` 段
> 当前阶段：CI 已就绪（Layer 1 + 2），CD（自动发布）未启用

## 1. 是什么 & 为什么

CI（Continuous Integration）**= 每次 push / PR 自动跑一组检查**，保证主分支始终是绿色（lint 干净、能构建、测试通过）。phalanx 选 GitHub Actions：

- 仓库已在 GitHub，零配置
- 公开仓库免费、无月度分钟数上限
- YAML 工作流声明式、可读、版本化在仓库内

CD（Continuous Deployment）= 自动发布。phalanx 现在**不**做 CD：项目还在 Phase 0/1，没必要每次 push 都发版。等稳定到要上 PyPI 再加。

## 2. 三层模型与 phalanx 当前覆盖

| 层 | 内容 | phalanx 状态 |
|---|---|---|
| Layer 1 — CI 基础 | lint + 构建 + import 冒烟 | ✅ 已上 |
| Layer 2 — CI 测试 | pytest 单元测试 | ✅ 工作流已就位，pytest 步骤当前因没有测试用例而 exit 5；CI 会容忍直到 Phase 1 写出第一批测试 |
| Layer 3 — CD 发布 | tag 触发 → twine upload PyPI | ❌ 未启用，时机未到 |

## 3. 触发条件

```yaml
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]
  workflow_dispatch:
```

- **push to main**：每次合并/直推 main 都跑
- **PR to main**：开 PR 立刻跑，PR 内每次新 commit 也重跑
- **workflow_dispatch**：在 GitHub 仓库的 Actions tab 里手动点 "Run workflow" 触发，便于排障或重跑失败的 build

## 4. 并发取消机制

```yaml
concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true
```

同一分支连续 push 时，旧 run 立即被取消。**避免**"在一个 PR 上连推 3 次，CI 同时跑 3 份"造成 minutes 浪费与排队拥堵。

## 5. 工作流步骤逐行解析

### Step 1 — Checkout

```yaml
- uses: actions/checkout@v4
```

把仓库代码 clone 到 runner 的当前目录。`@v4` 是当前最新的稳定大版本——固定 major version，subminor 自动跟进 bugfix。

### Step 2 — Set up Python

```yaml
- uses: actions/setup-python@v5
  with:
    python-version: "3.11"
    cache: pip
    cache-dependency-path: pyproject.toml
```

- 装 Python 3.11
- 启用 pip 缓存：以 `pyproject.toml` 的 hash 为 key，下次同 hash 直接复用 wheel，节省 ~30s 安装时间
- 矩阵化的入口在这里——加 `strategy.matrix.python-version: ["3.11", "3.12", "3.13"]` 即可

### Step 3 — 安装依赖

```yaml
- run: |
    python -m pip install --upgrade pip
    pip install -e ".[dev]"
    pip install build twine
```

- `pip install -e ".[dev]"`：editable 安装本项目 + 运行时依赖（openai、anthropic、httpx…）+ dev extras（ruff、pytest、pytest-asyncio）
- `pip install build twine`：CI-only 工具，没必要混进项目运行时依赖

### Step 4 — Lint

```yaml
- run: ruff check .
```

ruff 配置在 `pyproject.toml`：

```toml
[tool.ruff]
line-length = 120
target-version = "py311"

[tool.ruff.lint]
select = ["F", "E9"]   # 仅检查"真 bug"（pyflakes + 语法错误）
exclude = ["build", "dist", ".venv", "__pycache__"]
```

**早期阶段保守选规则**——只抓未定义变量、未使用 import、语法错。等代码沉淀后再加 `I`（import 排序）、`B`（bugbear 反模式）等。

### Step 5 — Import smoke

```yaml
- run: |
    python -c "import hermes_constants, hermes_logging, hermes_time, utils"
    python -c "import agent, tools, hermes_cli"
    python -c "from hermes_constants import get_hermes_home; print('PHALANX_HOME default ->', get_hermes_home())"
```

测两件事：
1. **顶层模块能 import** —— 如果裁剪 `run_agent.py` 时漏删了某个 import，这里立刻红
2. **`get_hermes_home()` 默认指向 `~/.phalanx`** —— 验证方案 B（env 变量隔离）持续有效

### Step 6 — Tests（早期阶段宽容）

```yaml
- run: |
    python -m pytest tests/ || ec=$?
    ec=${ec:-0}
    if [ "$ec" = "5" ]; then
      echo "::notice ::No tests collected yet — accepted during early phases."
      exit 0
    fi
    exit "$ec"
```

`pytest` 退出码语义：

| exit code | 含义 | CI 行为 |
|---|---|---|
| 0 | 全部测试通过 | ✅ pass |
| 1 | 至少一个测试失败 | ❌ fail（直接 `exit 1`） |
| 2 | 测试执行被中断 | ❌ fail |
| **5** | **没收集到任何测试** | ✅ **容忍**（Phase 0/1 期间） |

这个容忍机制**等 Phase 1 写出第一批测试就自动失效**——因为只要有 1 个测试存在，pytest 就不再返回 5；要么 0（全过）要么 1（有失败），逻辑无需改。

### Step 7 — Build

```yaml
- run: |
    python -m build
    ls -la dist/
```

PEP 517 标准构建，产出 sdist (`.tar.gz`) + wheel (`.whl`)。详细原理见同目录 [`build-vs-setup-py.md`](build-vs-setup-py.md)。

### Step 8 — Twine check

```yaml
- run: twine check dist/*
```

校验产物的 metadata：版本号合法、README 能渲染（PyPI 的 long_description）、license 字段格式正确等。**这一步现在是预演**，等真要发 PyPI 就直接换成 `twine upload`。

## 6. 本地复现 CI

每一步都能在本机一行一行复现，调试不需要等 GHA 跑：

```bash
# 装 CI 同款工具（pyproject.toml 已声明 dev extras）
pip install -e ".[dev]"
pip install build twine

# 1. lint
ruff check .

# 2. import smoke
python -c "import hermes_constants, hermes_logging, hermes_time, utils"
python -c "import agent, tools, hermes_cli"

# 3. tests
python -m pytest tests/         # exit 5 时本地手动判断

# 4. build
python -m build

# 5. metadata check
twine check dist/*
```

## 7. 查看结果

- **每次 push/PR**：仓库主页顶端 commit 旁边出现 ✓ / ✗ 图标，点开看
- **Actions tab**：https://github.com/cliffordll/phalanx/actions 列出所有 run，可重跑、查看 step 输出
- **PR 页面**：底部 "Checks" 区域显示该 PR 的 CI 状态，红牌时直接拦截 merge（如果你在 Settings → Branches 里给 `main` 加了 protection rule）

## 8. 扩展规划

### 8.1 等 Phase 1 落地后

- pytest 步骤自动生效，无需改 workflow
- 可以加 `pytest --cov=.` + 上传 coverage 到 codecov

### 8.2 矩阵化（Phase 3-4 之前推荐）

```yaml
jobs:
  ci:
    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-latest, windows-latest, macos-latest]
        python-version: ["3.11", "3.12", "3.13"]
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
      ...
```

- 公开仓库无月度分钟限制，矩阵化"代价仅是排队时间"
- 私有仓库时记得算账：`9 = 3*3` 个 job，比单 job 贵 9 倍

### 8.3 上 CD（项目稳定后）

新建 `.github/workflows/release.yml`：

```yaml
on:
  push:
    tags: ["v*"]            # 仅 vX.Y.Z tag 触发
jobs:
  publish:
    runs-on: ubuntu-latest
    permissions:
      id-token: write       # PyPI 推荐 trusted publishing，无需 token
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11" }
      - run: |
          pip install build
          python -m build
      - uses: pypa/gh-action-pypi-publish@release/v1
```

加上 GitHub Release 自动生成（`actions/create-release`）便能"打 tag → 自动发包 + 发 release notes"。

## 9. 常见问题

| 现象 | 排查 |
|---|---|
| ruff 报错但本地 ruff 通过 | CI 跟本地 ruff 版本不同。把 ruff 锁定版本（`ruff==<X.Y>`）写进 dev extras |
| pip install 慢/超时 | 检查缓存命中：Action log 里搜 "Cache restored from key"。没命中时检查 `cache-dependency-path` 是否指对 |
| `twine check` 报 README 渲染失败 | `pyproject.toml` 的 `readme = "README.md"` 文件含 PyPI 不支持的 Markdown 扩展（如 GitHub-only emoji 短代码）。本地用 `python -m readme_renderer README.md` 复现 |
| 工作流文件改动没生效 | workflow 必须 commit 到默认分支（main）才会被识别。在 PR 分支里改 workflow，PR run 用的是 PR 分支版本，但 push 后才生效 |
| `unable to access ... SSL_ERROR_SYSCALL`（本地 push 时偶现） | 网络瞬态。重试 / 切代理 / 用 SSH remote 而不是 HTTPS |

## 10. 参考

- [GitHub Actions docs](https://docs.github.com/actions)
- [`actions/setup-python`](https://github.com/actions/setup-python)
- [`actions/checkout`](https://github.com/actions/checkout)
- [Ruff configuration](https://docs.astral.sh/ruff/configuration/)
- [pytest exit codes](https://docs.pytest.org/en/stable/reference/exit-codes.html)
- [PyPA — trusted publishing](https://docs.pypi.org/trusted-publishers/)
