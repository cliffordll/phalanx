# `python -m build` vs `python setup.py sdist`

> 适用范围：Python 项目打包工具链选择
> 一句话结论：**两者不等同，前者是当前推荐方式，后者已被 PyPA 弃用**

## 1. 历史背景

| 时期 | 主流方式 | 标准 |
|---|---|---|
| ~2018 之前 | `python setup.py sdist/bdist_wheel/install` | distutils + setuptools，无规范，全靠 `setup.py` 这个 Python 脚本声明 |
| 2018+ | `pip install`（背后用 PEP 517 build backend） | [PEP 517](https://peps.python.org/pep-0517/) / [PEP 518](https://peps.python.org/pep-0518/)：用 `pyproject.toml` 声明 build backend，前端工具与后端解耦 |
| 当前 | `python -m build` | 由 PyPA 维护的 `build` 包（`pip install build`），是 PEP 517 的标准前端 |

**关键转变**：`setup.py` 从"可执行配置脚本"变成"可选的兼容层"。新项目应只用 `pyproject.toml`，不需要 `setup.py`。

## 2. 核心差异对比

| 维度 | `python -m build` | `python setup.py sdist` |
|---|---|---|
| 调用前提 | 安装 `build` 包（`pip install build`） | 项目根目录必须有 `setup.py` 文件 |
| 配置来源 | 读 `pyproject.toml` 的 `[build-system]` | 直接执行 `setup.py` |
| 后端可选 | 任意 PEP 517 backend（setuptools / hatchling / flit / poetry-core / maturin / pdm-backend …） | 硬绑死 setuptools |
| 默认产物 | **sdist + wheel** 都生成（`.tar.gz` + `.whl`） | **只生成 sdist**（`.tar.gz`） |
| 限定单产物 | `--sdist` 或 `--wheel` 标志 | 改用 `bdist_wheel` 子命令出 wheel |
| 构建隔离 | 默认临时干净 venv 中执行（PEP 517 build isolation），依赖只取自 `[build-system].requires` | 用当前 Python 环境，污染敏感 |
| 可重复性 | 高（隔离 + 锁定 backend 版本） | 低（看本机环境状态） |
| 维护状态 | 当前推荐 | `setuptools >= 58` 起触发 `SetuptoolsDeprecationWarning`，PyPA 明确警告将来移除 |

## 3. 产物对比

两者都能生成 sdist，但结构有微妙差别：

```
phalanx-0.0.1.tar.gz  (python -m build 产物)
└── phalanx-0.0.1/
    ├── pyproject.toml
    ├── setup.cfg              ← setuptools backend 自动生成的兼容层
    ├── README.md
    ├── PKG-INFO
    ├── MANIFEST.in
    ├── phalanx.egg-info/
    └── <源码文件...>
```

`python -m build` 还会**额外**生成 wheel（`.whl`）——这是默认行为，等同于 `python setup.py sdist bdist_wheel` 两步合并。

## 4. 为什么 setup.py 调用方式已弃用

引用 [setuptools 官方文档](https://setuptools.pypa.io/en/latest/deprecated/commands.html)：

> Setuptools no longer supports the direct invocation of `setup.py` for building or installing packages.

弃用理由：
1. **运行 `setup.py` 等于执行任意 Python 代码**，没有标准化的依赖声明，安全审计困难
2. **依赖污染**：`setup.py` 的 import 链使用当前环境，容易出现"在我机器上能打包"问题
3. **后端锁定**：无法替换为非 setuptools 的 backend
4. **幂等性差**：副作用（写文件、改 sys.path）难以追踪

## 5. phalanx 的实测结果

phalanx 当前**没有 `setup.py`**，所以：

```bash
$ python setup.py sdist
error: can't open file 'setup.py': [Errno 2] No such file or directory
```

只能走 `python -m build`：

```bash
$ python -m build
...
Successfully built phalanx-0.0.1.tar.gz and phalanx-0.0.1-py3-none-any.whl

$ ls dist/
phalanx-0.0.1.tar.gz           16.2 KB
phalanx-0.0.1-py3-none-any.whl 16.9 KB
```

wheel 的关键 metadata：

```ini
# entry_points.txt
[console_scripts]
hermes = hermes_cli.main:main
hermes-agent = run_agent:main

# top_level.txt
agent
hermes_cli
hermes_constants
hermes_logging
hermes_time
tools
utils
```

注意：entry points 指向的目标模块（`hermes_cli/main.py`、`run_agent.py`）在 Phase 0 时**还不存在**——打包阶段不校验 entry points 目标，只把元数据写进 wheel；运行 `hermes` 命令时才会触发 `ModuleNotFoundError`。这符合 Phase 0 预期，Phase 1 落地这两个文件后即可正常调用。

## 6. 常用命令速查

```bash
# 同时构建 sdist + wheel（默认）
python -m build

# 只构建 wheel（迭代调试时常用，更快）
python -m build --wheel

# 只构建 sdist
python -m build --sdist

# 跳过隔离环境（调试 build backend 本身时偶尔需要）
python -m build --no-isolation

# 检查产物的 metadata 完整性
pip install twine
twine check dist/*
```

## 7. phalanx 的选择

phalanx 完全用 `python -m build`，原因：

- 项目无 `setup.py`，没有迁移负担
- 未来如果要发 PyPI，`twine upload dist/*` 与 `python -m build` 配合最规范
- `pyproject.toml` 已声明 setuptools 为 backend，需要时也可以换 hatchling 等更现代的 backend，无需改打包流程

## 参考

- [PEP 517 — A build-system independent format for source trees](https://peps.python.org/pep-0517/)
- [PEP 518 — Specifying Minimum Build System Requirements](https://peps.python.org/pep-0518/)
- [PyPA Packaging Guide — Building distributions](https://packaging.python.org/en/latest/tutorials/packaging-projects/)
- [setuptools — deprecated commands](https://setuptools.pypa.io/en/latest/deprecated/commands.html)
- [`build` 项目主页](https://github.com/pypa/build)
