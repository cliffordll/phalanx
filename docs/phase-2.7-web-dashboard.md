# Phase 2.7 设计文档 — Web Dashboard MVP（FastAPI + React SPA）

> 配套阅读：
> - [`MIGRATION_PLAN.md`](MIGRATION_PLAN.md) §2.7 — 计划层面的源文件清单与策略
> - [`ARCHITECTURE.md`](ARCHITECTURE.md) — 全局视图
> - [`phase-2.5-sessions.md`](phase-2.5-sessions.md) — SessionDB（Sessions / Logs / Analytics 后端的数据源）
> - [`phase-2.6-repl.md`](phase-2.6-repl.md) — REPL（仍是日常对话主入口；MVP 不抢戏）

本文记录 **§2.7 六个 wave 把 phalanx 的"已落地子系统"接上一个浏览器 console 的过程**——FastAPI 后端 + Vite/React/TS/Tailwind SPA + token 鉴权 + 单 `hermes web` CLI 起服务。MVP 出 6 个 page（Status / Sessions / Logs / Config / Env / Analytics），覆盖 phalanx 当前能跑的所有子系统；Chat / Cron / Skills / Plugins / Profiles 等依赖未移植子系统的 page 在 NavBar 直接隐藏，等 §2.8 各对应期回填。

## 0. 范围与 wave 划分

| Wave | 内容 | 估行 |
|---|---|---|
| 1 | 后端骨架：`hermes_cli/web_server.py` FastAPI app + token 鉴权中间件（`X-Hermes-Session-Token` HMAC 比较）+ Host header 防 DNS rebinding + CORS 仅 localhost + `/api/status` 端点 + `cmd_web(--port=9119, --no-open, --token)` CLI；`fastapi` `uvicorn[standard]` 加进 `pyproject.toml`；`web_dist/` 不存在时返 "Frontend not built" 占位 | ~600 |
| 2 | 后端 read-side：`/api/sessions` (list / messages / delete) + `/api/logs` (file / lines / level / component) + `/api/analytics/usage`（usage 从 SessionDB messages 表 group-by 算 token / cost）；模块复用 `hermes_state.SessionDB` 和 `agent/usage_pricing.py` | ~400 |
| 3 | 后端 write-side：`/api/config` (GET/PUT) + `/api/config/raw` (YAML) + `/api/config/schema`（自动从 default config 推断字段类型）+ `/api/env` (GET/PUT/DELETE) + `/api/env/reveal`（token 二次校验 + rate limit 5/30s） | ~350 |
| 4 | 前端骨架：`web/` 子树（Vite + React 19 + TS + Tailwind v4 + shadcn 风格自卷组件）+ `App.tsx` 路由 + `lib/api.ts`（裁剪到 phalanx 已实现端点）+ StatusPage + SessionsPage（list / resume / delete / 改 title）；Vite dev proxy 到 `127.0.0.1:9119` | ~1400 |
| 5 | 前端 LogsPage + ConfigPage（dynamic schema-driven 编辑器）+ EnvPage（key 列表 + reveal 弹窗）+ AnalyticsPage（按天 token / cost 折线图）；缺 backing 的 page 在 NavBar **不渲染** | ~1500 |
| 6 | 打包：`npm run build` → `hermes_cli/web_dist/`；`pyproject.toml` `[tool.setuptools.package-data]` 含 `web_dist/**/*`；`MANIFEST.in` 同步；`.github/workflows/ci.yml` 加 frontend build step（`actions/setup-node` + `npm ci` + `npm run build` 在 Python build 之前）；wheel 装完后 `hermes web` 验证 SPA serve 工作 | ~150 |

§2.7 落地后：

- `pip install -e .` + `cd web && npm install && npm run build` 一次构建后，`hermes web` 一行命令起服务、自动开浏览器、扫到 `~/.phalanx/state.db` 列出所有 session
- 用户能从浏览器删 session、改 title、看历史消息；能编辑 `~/.phalanx/config.yaml` 不用手写 YAML；能在 EnvPage 维护 API key 而不暴露明文（reveal 走 token 二次校验）
- CI 每次 push 自动跑 frontend build + backend pytest；wheel 里携带 `web_dist/`，`pip install phalanx` 装完即可用

## 1. 上游对照与裁剪策略

### 1.1 后端：4049 → ~1350 行

上游 `hermes_cli/web_server.py` 4049 行包含：

```text
core infra              ~600 行   FastAPI / token / Host / CORS / SPA mount
sessions / search       ~250 行   list / messages / delete / FTS5 search
logs                    ~150 行   file / lines / level / component 过滤
config / schema         ~600 行   推断 + read / write / raw YAML + denormalize
env                     ~200 行   GET / PUT / DELETE / reveal + rate limit
model                   ~700 行   info / options / auxiliary / set + provider 探测
analytics               ~300 行   usage / models 端点（聚合 SessionDB usage）
chat (PTY)              ~400 行   WebSocket /api/pty + 流式渲染（dashboard --tui）
cron                    ~200 行   jobs CRUD
profiles                ~200 行   list / create / rename / delete / soul / setup
skills / toolsets       ~150 行   GET / toggle
OAuth providers         ~250 行   start / submit / poll / cancel
gateway / update        ~150 行   restart / update / action status
themes                  ~100 行   default / preset 列表
plugins (dashboard)     ~300 行   manifest / hub / install / rescan
```

phalanx MVP **保留**（约 1350 行）：

```text
core infra              ~600    照抄
sessions                ~200    保留 list / messages / delete；删 search（依赖 SQLite FTS5 索引未建）
logs                    ~150    照抄
config / schema         ~250    照抄（denormalize 逻辑保留）
env                     ~200    照抄
analytics               ~150    保留 usage 聚合，删 models 详情（依赖 model registry 未移植）
```

phalanx MVP **完全不移**（推到 §2.8）：

```text
model (info/options/...)  ← 依赖 model_registry 子系统；MVP 用 GET /api/status 露当前 model
chat (PTY WebSocket)      ← 流式 SSE / WebSocket 增量逻辑非小，且 REPL 已是主入口
cron jobs                 ← 依赖 cron 子系统
profiles                  ← 依赖 profile 系统
skills / toolsets         ← 依赖 skills 子系统
OAuth providers           ← 依赖 credentials 子系统
gateway / update / themes ← 依赖 gateway 子系统 + 主题系统
dashboard plugins         ← 依赖 plugin 子系统
```

### 1.2 前端：6800 → ~2900 行

上游 `web/` 12 个 page 共 ~6800 行；phalanx MVP 出 6 个：

| page | 上游行数 | phalanx 估行 | 状态 |
|---|---|---|---|
| StatusPage | ~150 | ~120 | wave 4 |
| SessionsPage | 852 | ~700 | wave 4（删 FTS5 search 输入框） |
| LogsPage | 233 | 200 | wave 5 |
| ConfigPage | 614 | 500 | wave 5（保留 schema 驱动编辑器） |
| EnvPage | 872 | 700 | wave 5（保留 reveal 弹窗） |
| AnalyticsPage | 543 | 400 | wave 5（usage 折线图） |
| **不实装的** | | | |
| ChatPage | 834 | — | §2.8 wave 7 |
| CronPage | 352 | — | §2.8 cron 期 |
| ModelsPage | 817 | — | §2.8 model_registry 期 |
| PluginsPage | 581 | — | §2.8 plugins 期 |
| ProfilesPage | 444 | — | §2.8 profiles 期 |
| SkillsPage | 562 | — | §2.8 skills 期 |
| DocsPage | 61 | — | §2.8（trivial 但 MVP 不需要） |

NavBar / `App.tsx` 路由 hard-code 6 条；缺 backing 的 page 在 import / route / NavBar 三处都不出现。**好处**：用户看不到一堆灰色 "not yet available" 的悬念；**代价**：每次 §2.8 一期落地都要回到 `App.tsx` 加一条路由 + 一行 NavBar——可接受。

## 2. 后端核心模式

### 2.1 Token 鉴权

```python
import secrets, hmac
from fastapi import Request, HTTPException

_SESSION_TOKEN = secrets.token_urlsafe(32)        # 进程启动时一次性生成
_SESSION_HEADER = "X-Hermes-Session-Token"

def _has_valid_token(req: Request) -> bool:
    # 优先专用 header（避免被反代的 Authorization 干扰）
    h = req.headers.get(_SESSION_HEADER, "")
    if h and hmac.compare_digest(h.encode(), _SESSION_TOKEN.encode()):
        return True
    # 兼容 Bearer
    auth = req.headers.get("authorization", "")
    return hmac.compare_digest(auth.encode(), f"Bearer {_SESSION_TOKEN}".encode())

@app.middleware("http")
async def auth_middleware(req: Request, call_next):
    if req.url.path.startswith("/api/") and req.url.path not in _PUBLIC_API_PATHS:
        if not _has_valid_token(req):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return await call_next(req)
```

**设计要点**：

- **进程启动一次性生成**：token 死在进程退出时，`hermes web` 重启后 token 失效 → 浏览器要刷新页面（让 SPA 重读注入的新 token）
- **header 而非 cookie**：避免 CSRF——浏览器不会自动发非 same-origin 的 custom header
- **HMAC 比较**：`hmac.compare_digest` 防止 timing attack（比 `==` 抵抗时序侧信道）
- **`--token <hex>` flag**：CI 测试场景需要固定 token；用户传值后跳过 `secrets.token_urlsafe`，用提供的字符串

### 2.2 SPA mount + token 注入

```python
@application.get("/{full_path:path}")
async def serve_spa(full_path: str):
    file_path = WEB_DIST / full_path
    # 防 path traversal（%2e%2e/）
    if (full_path
            and file_path.resolve().is_relative_to(WEB_DIST.resolve())
            and file_path.exists() and file_path.is_file()):
        return FileResponse(file_path)
    return _serve_index()    # client-side routing fallback

def _serve_index():
    html = (WEB_DIST / "index.html").read_text()
    script = f'<script>window.__HERMES_SESSION_TOKEN__="{_SESSION_TOKEN}";</script>'
    html = html.replace("</head>", f"{script}</head>", 1)
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})
```

**为什么用 `</head>` 字符串替换而不是模板引擎**：纯静态 SPA，绝大多数 byte 都不变；避免引入 jinja2 在 web_server.py 里只为这一处用。`Cache-Control: no-store` 防止浏览器缓存住旧 token 后服务重启失配。

### 2.3 Host header 防 DNS rebinding

```python
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

@app.middleware("http")
async def host_header_middleware(request: Request, call_next):
    host = request.headers.get("host", "").split(":")[0].lower()
    if _BOUND_HOST in _LOOPBACK_HOSTS and host not in _LOOPBACK_HOSTS:
        return JSONResponse({"error": "Invalid Host"}, status_code=400)
    return await call_next(request)
```

DNS rebinding 攻击：受害者浏览器访问 `evil.test`（解析到 attacker），TTL 翻转后 `evil.test` 解析到 `127.0.0.1`，攻击 SPA 误以为同源。Host header 校验把"我们 bind 的 interface"写死，绕不开。CVE 参考 GHSA-ppp5-vxwm-4cf7（上游记录）。

### 2.4 `/api/status` 字段集合（phalanx 版）

```python
@app.get("/api/status")
async def get_status():
    return {
        "version": __version__,
        "phalanx_home": str(get_hermes_home()),
        "config_path": str(get_config_path()),
        "model": cfg_get(load_config(), "model.default"),
        "base_url": cfg_get(load_config(), "model.base_url"),
        "provider": _infer_provider(...),
        "active_session": _last_session_id_or_none(),
        "session_count": SessionDB().count_sessions(),
        "tools": registry.get_all_tool_names(),
    }
```

跟上游差异：删 `gateway_health` / `update_available` / `oauth_providers` / `themes` 等依赖未移植子系统的字段；加 `phalanx_home` / `config_path` 让前端能直接显示路径。

## 3. 前端栈与组织

### 3.1 为什么完整照抄上游栈

候选方案：

1. ✓ Vite + React 19 + TS + Tailwind v4 + shadcn 风格自卷组件（上游）
2. SvelteKit / SolidStart（更轻）
3. HTMX + Alpine（无构建）
4. 原生 fetch + vanilla JS（最轻但维护差）

选 1。**理由**：

- **cherry-pick 友好**：上游每发一个 page 改动，phalanx 复制改动文件即生效；改栈意味着所有 page 重写
- **shadcn 风格自卷组件**而非 npm 装 shadcn-cli——零运行时依赖，组件源码就在 `web/src/components/ui/`，跨版本稳定
- **Tailwind v4** 单 CSS 文件 + `@import "tailwindcss"` 极简，不要 PostCSS 配置；vite 插件直接处理
- **代价**：CI 多一条 setup-node + `npm ci` 链路，约 15-20 秒；wheel 携带 `web_dist/` 后 ~3 MB（gzip 后 ~600KB）

### 3.2 `web/src/lib/api.ts` 裁剪原则

上游 api.ts 784 行——基本是 `api = { ... }` 大对象，每个端点一个方法。phalanx 裁到 ~250 行：

```typescript
export const api = {
  // Status
  getStatus: () => fetchJSON<StatusResponse>("/api/status"),

  // Sessions
  getSessions: (limit=20, offset=0) =>
    fetchJSON<PaginatedSessions>(`/api/sessions?limit=${limit}&offset=${offset}`),
  getSessionMessages: (id: string) =>
    fetchJSON<SessionMessagesResponse>(`/api/sessions/${id}/messages`),
  deleteSession: (id: string) =>
    fetchJSON<OK>(`/api/sessions/${id}`, { method: "DELETE" }),
  setSessionTitle: (id: string, title: string) =>
    fetchJSON<OK>(`/api/sessions/${id}/title`, {
      method: "PUT", body: JSON.stringify({ title }) }),

  // Logs
  getLogs: (params: LogParams) => ...,

  // Analytics
  getAnalytics: (days: number) =>
    fetchJSON<AnalyticsResponse>(`/api/analytics/usage?days=${days}`),

  // Config / Env (config / env / reveal)
  getConfig / saveConfig / getConfigRaw / saveConfigRaw / getSchema /
  getEnvVars / setEnvVar / deleteEnvVar / revealEnvVar
};
```

**删的方法**：cron / profiles / skills / toolsets / OAuth / gateway / hermes-update / dashboard-plugins / model_options / chat。

`fetchJSON` helper 跟上游一致——header 自动注入 token。

### 3.3 SessionsPage 的删除策略

上游 `SessionsPage.tsx` 852 行包含 FTS5 search 输入框 + 高亮 + 分页 + 删除二次确认。phalanx 保留：

- list（分页）
- 删除（弹窗二次确认 → DELETE 端点）
- 改 title（双击 cell 直编辑 → PUT title 端点）
- "Resume in REPL" 按钮——复制 `hermes --resume <id> chat` 命令到剪贴板（不是 web 内 chat，因为 ChatPage 推后）

去掉：

- FTS5 search 输入框（依赖 §2.8 SessionDB FTS5 索引）
- "Open in browser tab" → ChatPage（依赖 §2.8 ChatPage）

### 3.4 ConfigPage 的 schema 驱动编辑

上游 ConfigPage 走"后端推断 schema 字段类型 → 前端按字段类型渲染对应 input"模式（boolean → switch / int → number input / enum → select / string → text）。Phalanx 保留这一套——`/api/config/schema` 端点跟上游一致地把 default config 跑一遍 `_infer_type`。

好处：phalanx 后续每加一个 config 字段（§2.4 加了 `provider`、§2.5 加了 `session.flush_interval` 等），ConfigPage 自动渲染出来——零前端改动。

## 4. 关键决策点

### 4.1 缺 backing 的页面：完全隐藏 vs 灰色 stub

选**完全隐藏**（NavBar 不出现，路由不注册）。

理由：phalanx MVP 应该看起来"功能完整一致"——用户打开 Sessions 能正常工作，体验干净。如果 NavBar 一半都是 "not yet available"，会让人怀疑成熟度。代价：每次 §2.8 一期落地都要回到 `App.tsx` 加一条路由 + 一行 NavBar——这是**正向的** marker，提醒该期落地后 dashboard 也得更新。

### 4.2 Chat page 是否进 MVP

**不进**。两个原因：

- 工作量大（上游 834 行，含 SSE 流式、tool call 渲染、cancel）
- 日常调试已有 REPL，web Chat 是锦上添花

§2.8 单独开一期（"web dashboard Chat page"）做。届时已落地 §2.7 的基础设施（token / api / 路由 / SessionsPage），加 ChatPage 是纯增量。

### 4.3 多用户 / 多 worker

**单用户 + 单 worker**——`uvicorn --workers 1` 写死。

token 是进程内 module-level 单例，多 worker 各 fork 一份会失配；session 是单 SQLite，并发写也最好串行。phalanx 当前用例（个人开发者本地起一个 console）单 worker 够用。

未来 §2.8 接入 gateway / 多用户场景时，token 改成 SQLite 持久化 + 跨 worker 共享。

### 4.4 端口与浏览器

- 默认端口 `9119`（跟上游一致——上游约定俗成）
- 启动时 `webbrowser.open(url)` 默认开浏览器；`--no-open` flag 跳过（CI / SSH）
- 如 9119 已被占用：报错 + 提示用户传 `--port`，不自动找其他端口（避免静默落到错误端口让用户找不到）

## 5. CLI 暴露面

```bash
hermes web                                    # :9119, 自动开浏览器
hermes web --port 8080                        # 自定义端口
hermes web --no-open                          # 不开浏览器（CI / SSH）
hermes web --token deadbeef...                # 复用固定 token（CI 测试）
hermes web --bind 127.0.0.1                   # 默认就是 127.0.0.1，留 flag 备 0.0.0.0 用
```

`hermes_cli/main.py:cmd_web(args)` 大致：

```python
def cmd_web(args):
    from hermes_cli.web_server import start_server
    start_server(
        host=args.bind,
        port=args.port,
        token=args.token,           # None → secrets.token_urlsafe(32)
        open_browser=not args.no_open,
    )
```

`start_server` 内部 `uvicorn.run(app, host=..., port=..., log_level="info")`。

## 6. 测试策略

### 6.1 后端测试（wave 1-3）

```text
tests/test_web_server.py    # 约 30 用例，按 phase 分组
  - status 端点（version / paths / session count）
  - 鉴权（无 token 401、错 token 401、对 token 200）
  - sessions list / messages / delete
  - logs file / lines / level / component
  - analytics usage 聚合（SessionDB 用 fixtures 准备 N 行 usage）
  - config GET / PUT / raw / schema 推断
  - env GET / PUT / DELETE / reveal + rate limit
  - host header rebinding 拒绝
  - SPA fallback 命中 index.html
```

用 FastAPI 的 `TestClient`——同步、不开真 socket、跑 ~1s。token 测试用 `_SESSION_TOKEN` module-level 直接读。

### 6.2 前端测试

**MVP 不引入前端单元测试**。理由：

- 前端 page 多数是"读 api / 渲染"——逻辑简单
- 引入 jest/vitest + DOM testing library 工具链开销大
- E2E（Playwright）覆盖更值——但 §2.7 还不上 E2E，等 §2.8 多 page 后再开

替代方案：**Vite build pass = smoke**。CI 跑 `npm run build` 就能抓 TS 编译错和 import 链断；运行时 bug 靠手工跑 `hermes web` 验证。

### 6.3 集成测试

```text
tests/test_web_integration.py    # 约 5 用例
  - hermes web --no-open --port 0 启服务（port=0 OS 分配端口）
  - urllib 拿 token 后调 /api/status 200
  - SPA 路径下没 web_dist/ 时返 "Frontend not built"
  - port 占用时报 OSError（不静默换端口）
```

## 7. 打包：让 wheel 自带 web_dist

### 7.1 `pyproject.toml`

```toml
[tool.setuptools.package-data]
hermes_cli = ["web_dist/**/*"]    # 含 index.html / assets/ 全部
```

构建时序：

1. `cd web && npm ci && npm run build` → 输出到 `web/dist/`，Vite 配置 `outDir: '../hermes_cli/web_dist'`
2. `python -m build` 或 `pip wheel .` → setuptools 把 `hermes_cli/web_dist/` 整树塞进 wheel

**约束**：`web_dist/` 必须**在 setuptools 跑前就存在**——所以 CI 顺序得是 `npm build` 先于 `python -m build`。本地开发用 `pip install -e .` 不需要 `web_dist`（dev 起 `npm run dev` 走 Vite 开发 server，proxy 到后端）。

### 7.2 `MANIFEST.in`

sdist 也要包含 `web_dist/`（用户 `pip install phalanx-X.Y.tar.gz` 时不应该再要求装 npm）：

```
recursive-include hermes_cli/web_dist *
```

### 7.3 `.gitignore`

`hermes_cli/web_dist/` 是 build 产物——必须在 .gitignore，不进 git。如果不 ignore，每次 `npm build` 后 `git status` 一片红。

### 7.4 CI

```yaml
- uses: actions/setup-node@v5
  with:
    node-version: "20"
    cache: npm
    cache-dependency-path: web/package-lock.json
- run: cd web && npm ci && npm run build
# ↑ 上面这步必须在 python -m build 之前
- run: python -m build
- run: twine check dist/*
```

cache 命中后 `npm ci` 通常 < 10s；`npm run build` ~15s。CI 总时长增加 ~30s。

## 8. Stub / 降级

### 8.1 没 web_dist 时

`hermes web` 仍然能起（后端 API 仍 work），但访问 `/` 返：

```json
{ "error": "Frontend not built. Run: cd web && npm run build" }
```

便于 dev 模式（用户先起后端、再单独 `cd web && npm run dev` 走 vite proxy）。

### 8.2 token 丢了

浏览器开着、服务重启 → SPA 持有的旧 token 失配 → 调 API 报 401 → 用户**刷新页面**重读注入的新 token。前端可以加一条 `if (status === 401) location.reload()` 的全局拦截器让这个无感——MVP 简单点直接让用户手动刷。

## 9. 留给后续

§2.7 立的基础设施会被后续 page 复用：

- token / 鉴权中间件 → §2.8 加 ChatPage / OAuth 时直接挂新端点上
- `lib/api.ts` 大对象 → 加端点就是加 method
- shadcn 组件库 → 新 page 复用已有 Card / Button / Input / Dialog
- ConfigPage 的 schema 驱动编辑器 → §2.8 任何加 config 字段的子系统自动有 UI
- frontend build CI step → 不动

后续按"代价 / 收益"排序：

1. **ChatPage**（~800 行 + `/api/chat/stream` SSE）— web 内对话，跟 REPL 体验对齐
2. **`@reference` 文件附件**（依赖 §2.8 memory & context）—  web 上更直观
3. **CronPage / SkillsPage / ProfilesPage**（依赖各自子系统落地）
4. **PluginsPage**（hub + install + rescan，依赖 §2.8 plugins 系统）
5. **OAuth 弹窗**（依赖 credentials 子系统）
6. **ModelsPage 详情**（依赖 model_registry）
7. **多用户 / SSO**（依赖 gateway 子系统）
8. **theme / skin 系统**（独立 wave，纯前端）

§2.7 之后的演进对照见 [`MIGRATION_PLAN.md`](MIGRATION_PLAN.md) §2.8。
