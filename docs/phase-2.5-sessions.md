# Phase 2.5 设计文档 — 会话持久化 + Session/Logs CLI

> 配套阅读：
> - [`MIGRATION_PLAN.md`](MIGRATION_PLAN.md) §2.5 — 计划层面的源文件清单与策略
> - [`ARCHITECTURE.md`](ARCHITECTURE.md) — 全局视图
> - [`run-loop-vs-upstream.md`](run-loop-vs-upstream.md) — phalanx 主循环与上游的逐节点差异
> - [`phase-2.4-multi-provider.md`](phase-2.4-multi-provider.md) — 上一阶段的多 provider 设计

本文记录 **§2.5 五个 wave 的存储 schema、消息编码、主循环 hook 点、CLI 暴露面与 stub 模式**——把上游 `hermes_state.py` 的 SessionDB 装进 phalanx，让 `run_conversation` 每轮落库；附 `hermes_cli/logs.py` 的轻量移植。

## 0. 范围与 wave 划分

| Wave | 内容 | 提交 |
|---|---|---|
| 1 | `hermes_state.py` 骨架 — schema + 连接 + `_execute_write` jitter 重试 + `_init_schema`（含 `_reconcile_columns` / FTS5）+ 9 个核心 CRUD 方法（`create_session` / `end_session` / `reopen_session` / `ensure_session` / `get_session` / `update_system_prompt` / `update_token_counts` / `append_message` / `get_messages`）+ `_encode_content` / `_decode_content` | `c42a4fe` |
| 2 | `run_agent.py` 主循环接 SessionDB — `_ensure_db_session` + `_persist_messages_to_db` flush 点 + `AIAgent.__init__` 接 `_session_db` / `_session_db_created` / `_last_flushed_db_idx` + DB 故障 try/except 不打断对话 | `684154d` |
| 3 | Resume 路径 — `resolve_session_id`（前缀解析）+ `resolve_resume_session_id`（compression 链跟进）+ `get_messages_as_conversation` + `_session_lineage_root_to_tip` + `_is_duplicate_replayed_user_message` + 主循环 `--resume <id>` 接入 | `cff4ad9` |
| 4 | `session list/show/dump/delete` 子命令 — `list_sessions_rich`（裁剪：去掉 compression 投影 + `order_by_last_active` CTE）+ `_get_session_rich_row` + `delete_session`（orphan child）+ CLI 渲染（preview / last_active / token 总量 / JSONL dump / `--yes` 守卫） | `6315549` |
| 5 | `hermes_cli/logs.py` 移植 — `tail_log` / `_read_tail` / `_read_last_n_lines` / `_follow_log` / `list_logs` + `--session` / `--component` / `--since` / `--level` 过滤 + `logs` 子命令 | `2aee4b5` |

§2.5 落地后 phalanx 单进程 CLI 即可创建 / 列举 / 续聊 / 导出会话；gateway 多进程并发下的 WAL contention 路径（`_try_wal_checkpoint`）一并就位。

## 1. 存储 schema

### 1.1 数据库位置与连接

```
~/.hermes/state.db            # DEFAULT_DB_PATH = get_hermes_home() / "state.db"
journal_mode = WAL            # 多 reader + 单 writer
foreign_keys = ON
isolation_level = None        # 应用层自管理事务（BEGIN IMMEDIATE）
timeout = 1.0s                # SQLite 内置 busy handler 短超时
```

phalanx 沿用 `hermes_constants.get_hermes_home()`（即 `~/.hermes/`），不另起 `~/.phalanx/`——为后续与 hermes 上游补丁互操作。`DEFAULT_DB_PATH` 仅作模块级常量留作 monkeypatch 锚点；实际路径在 `SessionDB.__init__` 里**懒解析**（`db_path or get_hermes_home() / "state.db"`），让测试的 `PHALANX_HOME` env override 在模块 import 之后还能即时生效。

### 1.2 三张主表 + 两张 FTS5 虚拟表

```sql
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,                 -- session_id (uuid4 字符串)
    source TEXT NOT NULL,                -- 'cli' / 'tui' / 'gateway' / 'oneshot'
    user_id TEXT,                        -- 多用户场景；phalanx 单用户暂不用
    model TEXT,
    model_config TEXT,                   -- JSON: 创建时的 model 配置快照
    system_prompt TEXT,                  -- 完整组装好的 system prompt
    parent_session_id TEXT,              -- compression / branch 父引用
    started_at REAL NOT NULL,
    ended_at REAL,
    end_reason TEXT,                     -- 'completed' / 'compression' / 'branched' / ...
    message_count INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    reasoning_tokens INTEGER DEFAULT 0,
    billing_provider TEXT,               -- 'openai' / 'anthropic' / 'codex'
    billing_base_url TEXT,
    billing_mode TEXT,
    estimated_cost_usd REAL,
    actual_cost_usd REAL,
    cost_status TEXT,                    -- pricing 子系统填
    cost_source TEXT,
    pricing_version TEXT,
    title TEXT,                          -- 用户可命名（暂不暴露）
    api_call_count INTEGER DEFAULT 0,
    FOREIGN KEY (parent_session_id) REFERENCES sessions(id)
);

CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,                  -- 'system' / 'user' / 'assistant' / 'tool'
    content TEXT,                        -- 字符串 / JSON 编码（见 §2.2）
    tool_call_id TEXT,                   -- assistant tool_call 的 id（tool 角色用）
    tool_calls TEXT,                     -- JSON: assistant 的 tool_calls 列表
    tool_name TEXT,                      -- tool 角色对应的工具名
    timestamp REAL NOT NULL,
    token_count INTEGER,
    finish_reason TEXT,                  -- 仅 assistant
    reasoning TEXT,                      -- 仅 assistant，部分 provider
    reasoning_content TEXT,              -- 同上
    reasoning_details TEXT,              -- JSON
    codex_reasoning_items TEXT,          -- JSON: codex/responses 专属
    codex_message_items TEXT             -- JSON: 同上
);

CREATE TABLE state_meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE schema_version (version INTEGER NOT NULL);

-- 索引
CREATE INDEX idx_sessions_source ON sessions(source);
CREATE INDEX idx_sessions_parent ON sessions(parent_session_id);
CREATE INDEX idx_sessions_started ON sessions(started_at DESC);
CREATE INDEX idx_messages_session ON messages(session_id, timestamp);
CREATE UNIQUE INDEX idx_sessions_title_unique ON sessions(title) WHERE title IS NOT NULL;

-- FTS5：全文检索（unicode61 分词）+ 三字符切片（CJK / 子串）
CREATE VIRTUAL TABLE messages_fts USING fts5(content);
CREATE VIRTUAL TABLE messages_fts_trigram USING fts5(content, tokenize='trigram');
-- 两组 INSERT/DELETE/UPDATE trigger 把 messages 行同步进两张 FTS 表；
-- 索引内容是 content || tool_name || tool_calls 的拼接（带 COALESCE 容空）。
```

`SCHEMA_VERSION = 11`。phalanx 全量抄过来，schema 版本号保持一致，避免与 hermes 同时使用同一份 `state.db` 时分歧。

### 1.3 连接管理与写竞争

| 机制 | 实现 | 为什么需要 |
|---|---|---|
| WAL 模式 | `PRAGMA journal_mode=WAL` | 读不阻塞写；gateway / CLI / worktree agent 多进程共用 |
| 短 timeout + 应用层重试 | `timeout=1.0` + `_execute_write` 手写 `BEGIN IMMEDIATE` 事务，最多 15 次 jitter 20-150ms 重试 | 多 writer 高并发下 SQLite 内置 busy handler 的确定性退避会形成 convoy；随机抖动打散 |
| BEGIN IMMEDIATE | 事务起点就拿写锁，不等到 commit | 锁竞争立刻浮出来，便于重试；避免死锁 |
| 周期 PASSIVE checkpoint | 每 50 次成功写一次 `_try_wal_checkpoint()` | 防 WAL 文件无限增长；best-effort 永不阻塞 |
| `_reconcile_columns` 自愈 | 启动时拿 `PRAGMA table_info` 与 `SCHEMA_SQL` 对比，缺啥就 `ALTER TABLE ADD COLUMN` | 列添加 = 改 SQL 字符串，不需要写 migration；老库自动跟上 |

phalanx 全量保留这套——单进程 CLI 用不上多 writer 重试，但保留代码不增成本，将来跑 gateway 直接复用。

## 2. 数据格式转换

### 2.1 OpenAI message → DB row

run loop 永远拿 OpenAI 格式 messages。`append_message` 入参 = OpenAI message 字段子集，DB 行 = `messages` 表的列：

| OpenAI message 字段 | DB 列 | 编码 |
|---|---|---|
| `role` | `role` | 直存 |
| `content`（str） | `content` | 直存 |
| `content`（list，多模态 parts） | `content` | `\x00json:` 前缀 + `json.dumps(...)` |
| `tool_calls`（list of dict / SDK 对象） | `tool_calls` | `json.dumps(tool_calls)` |
| `tool_call_id` | `tool_call_id` | 直存 |
| `tool_name` | `tool_name` | 直存（仅 tool 角色） |
| `finish_reason` | `finish_reason` | 直存（仅 assistant） |
| `reasoning` / `reasoning_content` | 同名列 | 字符串直存 |
| `reasoning_details` | `reasoning_details` | `json.dumps(...)` |
| `codex_reasoning_items` / `codex_message_items` | 同名列 | `json.dumps(...)` |

**多模态 sentinel**：`_CONTENT_JSON_PREFIX = "\x00json:"`。NUL 字节在 OpenAI / Anthropic 文本里都不可能合法出现，作为标志位区分"原文 str"和"序列化后的 list/dict"。

### 2.2 DB row → OpenAI message（resume / 列表渲染共用）

| 入口 | 用途 | 还原细节 |
|---|---|---|
| `get_messages(session_id)` | dump / show 子命令 | 列名原样保留为 dict 键；JSON 字段 `json.loads` 失败 → 降级原字符串 + warn |
| `get_messages_as_conversation(session_id, include_ancestors=False)` | resume 重放 | 只取 OpenAI/Anthropic/Codex 必要字段；`user`/`assistant` content 过 `agent.memory_manager.sanitize_context`；`assistant` 还附带 reasoning / codex 字段；`include_ancestors=True` 时拼 lineage（compression 链）+ `_is_duplicate_replayed_user_message` 去重 |

phalanx **不直接复用上游的 `sanitize_context`**——上游在 `agent/memory_manager.py`。要么 §2.5 wave 3 顺手把 `sanitize_context` 单函数搬过来（~30 行），要么先用 identity 函数占位，等 §2.7 memory 子系统真正落地再补。**当前选项**：占位（`def sanitize_context(s): return s`），TODO 注释。

### 2.3 token / 成本计数

`update_token_counts` 两种语义：
- `absolute=False`（CLI 默认）：增量累加（每次 API call 后 +Δ）
- `absolute=True`（gateway）：直接 set（cached agent 自己在内存里累加）

phalanx CLI 路径走 `absolute=False`，每轮 `_make_api_call` 拿到 `response.usage` 后调用一次。pricing 字段（`estimated_cost_usd` / `cost_status` 等）由 §2.3 已有的 `agent/pricing.py` 计算，append 给 `update_token_counts`。

## 3. SessionDB 方法取舍

上游 53 个方法分四档：

### 3.1 必移（核心 CRUD，wave 1）
- `__init__` / `_execute_write` / `_try_wal_checkpoint` / `close` / `_init_schema` / `_parse_schema_columns` / `_reconcile_columns`
- `_insert_session_row` / `create_session` / `end_session` / `reopen_session` / `ensure_session`
- `get_session`
- `update_system_prompt` / `update_token_counts`
- `_encode_content` / `_decode_content` / `append_message` / `replace_messages` / `get_messages`

### 3.2 必移（resume，wave 3）

| 方法 | 输入 → 输出 | 关键行为 |
|---|---|---|
| `resolve_session_id(s)` | `str` → `Optional[str]` | 先 `get_session(s)` 走精确匹配；miss 后用 `LIKE 's%' ESCAPE '\\'` + `LIMIT 2` 检测唯一前缀。**LIKE 元字符 `%` / `_` / `\` 在拼前先转义**，否则 `--resume "%"` 会匹配所有会话。≥2 命中或 0 命中都返回 `None`，CLI 据此报 "no matching session"。 |
| `resolve_resume_session_id(sid)` | `str` → `str` | 先看 `sid` 自己有没有 message 行——有就直接返回（绝大多数情况）。无则沿 `parent_session_id` 反向（实际是 `WHERE parent_session_id = current` 找子节点）走 compression 链，每跳挑 `started_at` 最新的 child；找到第一个有 message 的子节点即返回。**32 hop 上限** + `seen` 集合防环。phalanx 当前 compressor 是 stub，链一定空，函数永远走第一条 fast path——但接口留好，§2.7 启用 compression 时直接生效。 |
| `get_messages_as_conversation(sid, include_ancestors=False)` | `str` → `List[Dict]` | 把 DB 行翻译回 OpenAI message 形态：仅保留 `role`/`content`/`tool_call_id`/`tool_name`/`tool_calls` 等模型实际看到的字段，**剥掉 `id` / `timestamp` / `token_count` 等 db 元数据**。`user`/`assistant` 的 string content 走 `agent.memory_manager.sanitize_context` + `.strip()`。assistant 行额外恢复 `finish_reason` / `reasoning` / `reasoning_content` / `reasoning_details` / `codex_*_items`（缺一份就让 OpenRouter / Codex 多轮思考链断掉）。坏 JSON 降级到 `[]` / `None` + warn——单条腐败不让整段重放挂掉。`include_ancestors=True` 时拼整个 lineage 链，并对接缝处的重复 user prompt 调下面那个 helper 去重。 |
| `_session_lineage_root_to_tip(sid)` | `str` → `List[str]` | 沿 `parent_session_id` 一路向上找根，返回 `[root, ..., tip]` 顺序（保证回放顺序自然）。**100 hop 上限** + `seen` 集合：上游观察过用户手工乱接 `parent_session_id` 形成环，必须有兜底。 |
| `_is_duplicate_replayed_user_message(messages, msg)` | `(List[Dict], Dict)` → `bool` | 静态方法。仅当 `msg.role == "user"` 且 content 是非空字符串时才有可能 True。从 `messages` 末尾倒着扫：碰到内容相同的另一条 user → True（接缝重复）；碰到带 `content` 或 `tool_calls` 的 assistant → False（中间确有进展，是真新轮）。**lineage 接缝场景**：父 session 末轮是 "user: foo"，compression 后 child 重放也以 "user: foo" 开头——不去重就会双发同一句。 |

**主循环接入**（wave 3）：
- `cli_main(["--resume", "<id-or-prefix>", "oneshot", ...])` 顶层 flag
- `_build_agent` 无条件构造 `SessionDB`（让每次 CLI 都落库），`--resume` 时调 `resolve_session_id` → `resolve_resume_session_id` → `get_messages_as_conversation` 三连，再 `reopen_session` 让目标会话能继续记录 `end_session`
- 解析失败（无匹配 / 前缀歧义）走 `sys.exit(2)` + stderr 友好提示
- `cmd_oneshot` 把 history 透传给 `run_conversation(conversation_history=...)`；主循环里的 `_persist_messages_to_db` 自动跳过 `len(history)` 之前的索引，**不会重写历史**

### 3.3 必移（list/show/delete，wave 4）

| 方法 | 输入 → 输出 | 关键行为 |
|---|---|---|
| `list_sessions_rich(source=None, exclude_sources=None, limit=20, offset=0, include_children=False)` | filters → `List[Dict]` | `hermes session list` 的数据来源。在 `sessions.*` 之外多算两列：`preview`（首条 user message 前 60 字符 + `\n` / `\r` 扁平化为空格 + 超长加 `...`）、`last_active`（`MAX(messages.timestamp)`，无 message 回退到 `started_at`）。**裁剪**：去掉上游 `order_by_last_active=True` 的递归 CTE 分支（依赖 compression chain）+ `project_compression_tips` 投影；`started_at DESC` 简单分页。`include_children=False`（默认）排除 sub-agent run（parent 还活着时开的子 session）；branch session（`parent.end_reason='branched'` 之后开的）保留可见。 |
| `_get_session_rich_row(sid)` | `str` → `Optional[Dict]` | 单行版本，给 `session show` 用。同样附 `preview` + `last_active`，session 不存在返 `None`。 |
| `delete_session(sid)` | `str` → `bool` | 一个 transaction 内 `DELETE FROM messages` → `DELETE FROM sessions`。**子 session 走 orphan**：`UPDATE ... SET parent_session_id = NULL`，不级联删除——避免 compression chain 里删 ancestor 把 live tip 一起带走。返回是否真删了（不存在返 False）。 |

**CLI 接入**（wave 4，`hermes_cli/main.py`）：
- `cmd_session_list` — 默认表格视图 `ID(8) SOURCE MODEL MSGS LAST PREVIEW` 六列；`--json` 直出 dict 数组（管道 / jq 友好）；`--source` / `--limit` 透传给查询。
- `_format_session_age(ts)` — 把 epoch 时间戳渲成 `"3m ago"` / `"2h ago"` / `"4d ago"`，给表格的 `LAST` 列。
- `_resolve_session_target(target)` — 三个 cmd（show/dump/delete）共用：开 DB → `resolve_session_id(target)` 把前缀解析成全 id → 失败 `sys.exit(2)` + stderr 友好提示。
- `cmd_session_show` — 顶部 metadata（id / source / model / started / ended+reason / 计数 / token 总量 / preview），下面遍历 messages 以 `--- [n] role ---` 分隔；content 超 500 字符截断（完整内容走 `dump`）。
- `cmd_session_dump` — 每条 message 一行 JSON（JSONL 格式），保留所有 db 字段；脚本管道用。
- `cmd_session_delete` — 没带 `--yes` → dry-run（stderr 提示 + exit 1，DB 不动）；带 `--yes` 才真调 `delete_session`。

### 3.4 暂不移（gateway/compression 专属）
- Title 全套 6 个：`sanitize_title` / `set_session_title` / `get_session_title` / `get_session_by_title` / `resolve_session_by_title` / `get_next_title_in_lineage`——/save 命令依赖，§2.6 REPL 落地时再加
- `get_compression_tip`——依赖 §2.7 context_compressor 写入 `end_reason='compression'`，phalanx 当前 compressor 是 stub
- `prune_empty_ghost_sessions`——TUI ghost session 清理，phalanx 没 TUI

> 留 stub 占位还是不留？设计选择是**完全不移植**——`SessionDB` 只暴露 §3.1+§3.2+§3.3 的 ~25 个方法。gateway 等真要用时再 cherry-pick；当前不留半成品代码减少维护面。

## 4. 主循环 hook 点

phalanx `run_agent.py` 当前 (`62092e6` 之后) 已经有：
- `self.session_id = session_id or str(uuid.uuid4())` (line 612)
- `_set_session_log_context(self.session_id)` (line 1240)

**缺的**（wave 2 补回）：

### 4.1 `AIAgent.__init__` 新字段
```python
# Session DB persistence — None means "ephemeral, no DB writes".
# Created lazily by _ensure_db_session() so import-time failures
# (missing dir, locked file) don't block construction.
self._session_db: Optional[SessionDB] = None
self._session_db_created: bool = False
self._last_flushed_db_idx: int = 0
self._parent_session_id: Optional[str] = None
self._cached_system_prompt: Optional[str] = None
self.platform: str = platform or "cli"  # source 字段；CLI 默认 'cli'
```

DB instance 在 `__init__` 里就建（`SessionDB(db_path=...)`）但 `create_session` 推到首次 `run_conversation` 入口——同上游。

### 4.2 `_ensure_db_session()`（来自上游 line 2188）
首次 `run_conversation` 入口处调；`INSERT OR IGNORE` 语义所以重复调安全。失败只 warn 不抛。

### 4.3 `_persist_messages_to_db(messages, conversation_history)`（来自上游 line ~3750）
每轮 API call 后调（位置：`run_conversation` 主循环 `_execute_tool_calls` 之后、下一次 `_make_api_call` 之前）。逻辑：
1. 跳过已 flush 的索引（`self._last_flushed_db_idx`）
2. 对 `messages[flush_from:]` 每条算 `tool_calls_data`，调 `append_message`
3. 推进 `self._last_flushed_db_idx`
4. try/except 包整段——DB 故障**绝不**阻断对话；warn 后继续

### 4.4 `_call_chat_completions` 收尾处更新 token 计数
拿 `response.usage`（OpenAI 格式）→ 调 `update_token_counts(...)`。三个 provider helper 都加一遍——`SimpleNamespace` shape 已统一，但 `response.usage` 字段名各异：
- OpenAI: `prompt_tokens` / `completion_tokens` / `prompt_tokens_details.cached_tokens`
- Anthropic: `input_tokens` / `output_tokens` / `cache_read_input_tokens` / `cache_creation_input_tokens`
- Codex Responses: `input_tokens` / `output_tokens` / `output_tokens_details.reasoning_tokens`

**约定**：在 `_*_response_to_openai_shape` 收口时把 usage 也归一化到 `SimpleNamespace(input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens)`，主循环只调一次 `update_token_counts`。

### 4.5 主循环对照表（更新 `run-loop-vs-upstream.md` §"持久化"段）

| 上游 | phalanx (§2.5 后) | 备注 |
|---|---|---|
| `_ensure_db_session()` 首次入口 | 同 | 无差异 |
| `_persist_messages_to_db()` 每 API 周期 | 同 | flush 索引 + try/except 不抛 |
| `_get_messages_up_to_last_assistant()` 回滚 | wave 3 加（`/retry` / 异常恢复用） | |
| `replace_messages()` 用于 `/retry`、`/undo`、`/compress` | 移植但不接 CLI（§2.6 REPL 用） | |

## 5. CLI 暴露面

### 5.1 session 子命令
```bash
hermes session list [--limit N] [--source SRC] [--json]
hermes session show <id_or_prefix>           # 渲染轮次（pretty）
hermes session dump <id_or_prefix>           # 原始 JSONL（管道友好）
hermes session resume <id_or_prefix>         # 进入 chat，预加载历史
hermes session delete <id_or_prefix> [--yes]
```

`<id_or_prefix>` 走 `resolve_session_id`——CLI 用户只敲前 8 字符即可。

### 5.2 顶层 `--resume` 短链路
```bash
hermes --resume <id> oneshot "..."   # 复用 session_id + 历史，单轮叠加
```
顶层 flag → `AIAgent(session_id=resolved_id, conversation_history=loaded_msgs)`。`run_conversation` 收到非空 `conversation_history` 就跳过 system 重组。

### 5.3 logs 子命令（wave 5）
```bash
hermes logs                          # 默认 agent.log，最后 50 行
hermes logs -f                       # follow（轮询 0.3s）
hermes logs errors -n 100
hermes logs --level WARNING
hermes logs --session <id>           # 子串匹配 [sess_xxx] tag
hermes logs --component tools
hermes logs --since 1h
hermes logs list                     # 列出 ~/.hermes/logs/ 下所有 *.log
```

logs CLI **不依赖 SessionDB**——纯文本文件 tail/filter。session_id 过滤靠 `hermes_logging.set_session_context()` 在每条日志行注入的 `[sess_xxx]` 标签做子串匹配（这套已在 phalanx 现有 `_set_session_log_context` 路径里就位）。`hermes_cli/logs.py` 几乎逐字 verbatim 移自上游，无 agent 依赖。

**logs.py 函数表**：

| 函数 | 输入 → 输出 | 关键行为 |
|---|---|---|
| `tail_log(name, num_lines, follow, level, session, since, component)` | flags → exit code | 顶层入口。校验 `name` 在 `LOG_FILES`（agent/errors/gateway）、`since` 能解析、`level` 在 `_LEVEL_ORDER`、`component` 在 `COMPONENT_PREFIXES`，任一失败 stderr 报错 + 返非零。Header 回显被激活的过滤集，方便用户确认 "正在看哪些行"。`follow=True` 时打印 header 后转 `_follow_log`，`Ctrl+C` 优雅退出。 |
| `_read_tail(path, n, has_filters, ...)` | Path + filters → `List[str]` | 有过滤时**多读 20 倍** raw lines（保底 2000）以保证过滤后还剩 N 条；无过滤直接 `_read_last_n_lines(path, n)`。 |
| `_read_last_n_lines(path, n)` | Path → `List[str]` | 文件 ≤1MB 全读；>1MB 走**倍增分块尾部反读**（8K → 16K → 32K → 64K 上限），合并跨 chunk 的半行；任何异常降级到全文件读。 |
| `_follow_log(path, ...)` | Path → never returns | `seek(0, 2)` 跳到文件尾，0.3 秒轮询 `readline()`；新行通过 `_matches_filters` 才打印 + flush stdout。 |
| `list_logs()` | – → exit code | 扫 `~/.hermes/logs/*.log`，每文件一行：name + size（B/KB/MB）+ 相对 mtime（"just now" / "Nm ago" / "Nh ago" / 旧的直出日期）。无目录 / 空目录给友好提示。 |
| `_parse_since(s)` | `"30s"`/`"5m"`/`"1h"`/`"2d"` → `datetime` | 正则 `^(\d+)\s*([smhd])$`，配合 `timedelta` 算 cutoff = `now - delta`。无效输入返 `None` 让 caller 决定如何报错。 |
| `_parse_line_timestamp(line)` | str → `Optional[datetime]` | `^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})` 匹配；无时间戳返 `None`（**不让 since 过滤误杀**——无法判定就放行）。 |
| `_extract_level(line)` | str → `Optional[str]` | ` (DEBUG\|INFO\|WARNING\|ERROR\|CRITICAL) ` 子串匹配。 |
| `_extract_logger_name(line)` | str → `Optional[str]` | level 后可选 `[sess_*]` tag，再下一个非空白 token 到 `:` 之前——既支持带 session tag 的行，也支持不带的。 |
| `_line_matches_component(line, prefixes)` | (str, prefixes) → bool | logger name `startswith(tuple(prefixes))`。 |
| `_matches_filters(line, *, min_level, session_filter, since, component_prefixes)` | str + 4 个 optional filter → bool | 四谓词依次跑：`since` → `level >= threshold` → `session_filter in line`（子串）→ component prefix。**任一不匹配返 False**；某谓词无法判定（无时间戳 / 无 level）则跳过该谓词。 |

**CLI 接入**（`hermes_cli/main.py` 的 `cmd_logs`）：thin delegate，特判 `name="list"` → `list_logs()`，其它把所有 flag 透传给 `tail_log()`。`--since 30m -f` 可叠用——header 显示 cutoff，follow 阶段同样过滤新行。

依赖项：
- `hermes_constants.get_hermes_home()` / `display_hermes_home()`（已移）
- `hermes_logging.COMPONENT_PREFIXES`（已存在；phalanx 默认五桶：`gateway` / `agent` / `tools` / `cli` / `cron`）

## 6. Stub 模式

跨进程 SQLite 文件本身就好测，但每个测试 fixture 用临时文件清理麻烦——上游用 `:memory:` 数据库 + monkeypatch DEFAULT_DB_PATH 模式：

```python
@pytest.fixture
def stub_session_db(tmp_path, monkeypatch):
    """Each test gets a fresh SQLite file; cleaned up by tmp_path teardown."""
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("hermes_state.DEFAULT_DB_PATH", db_path)
    db = SessionDB(db_path=db_path)
    yield db
    db.close()
```

**main 循环测试**用同一 fixture：
```python
def test_run_conversation_persists_messages(stub_session_db, stub_openai):
    stub_openai([
        FakeResponse([FakeChoice(message=FakeMessage(content="hi"))])
    ])
    agent = AIAgent(session_db=stub_session_db, ...)
    agent.run_conversation("hello")

    # 断言 session row + messages
    sess = stub_session_db.get_session(agent.session_id)
    assert sess["message_count"] >= 2
    msgs = stub_session_db.get_messages(agent.session_id)
    assert msgs[0]["role"] == "user"
    assert msgs[1]["role"] == "assistant"
```

**resume 测试**：
```python
def test_resume_restores_history(stub_session_db, stub_openai):
    # 第一轮跑完
    stub_openai([FakeResponse(...)])
    agent1 = AIAgent(session_db=stub_session_db, ...)
    agent1.run_conversation("first turn")

    # 第二轮 resume，stub 检查 messages 已含历史
    stub_openai([FakeResponse(...)])
    agent2 = AIAgent(
        session_db=stub_session_db,
        session_id=agent1.session_id,
        conversation_history=stub_session_db.get_messages_as_conversation(agent1.session_id),
    )
    agent2.run_conversation("second turn")

    # 断言 OpenAI stub 收到的 messages 含上一轮的两条
    assert any(m["content"] == "first turn" for m in stub_openai.calls[1]["messages"])
```

**logs 测试**：临时 `tmp_path / "logs" / "agent.log"`，monkeypatch `get_hermes_home`，写几行后 assert tail 输出。

## 7. 留给后续

按"代价 / 收益"排序：

1. **Title / `/save` / fork 子系统**（~600 行）——`set_session_title` + 唯一索引冲突回退 + `get_next_title_in_lineage` lineage 命名（"foo (2)"）；§2.6 REPL `/save` 一起做
2. **Compression-aware resume**——`resolve_resume_session_id` 的 compression-tip 跟进 + `list_sessions_rich.project_compression_tips`；要等 §2.7 真把 context_compressor 接上才有意义
3. **Sub-agent / branch session lineage**——`parent_session_id` 链 + branch 子树查询；gateway / multi-agent 才用
4. **Token / pricing 闭环**——`update_token_counts` 调用点已在 wave 2 接好，但精确 cost 需 §2.7 / §2.3 pricing 子系统补全 `cost_status='actual'` 支路
5. **FTS5 search CLI**——`hermes search "..."` 走 `messages_fts_trigram` MATCH；schema 已有，CLI 子命令暂不暴露
6. **Auto-prune ghost sessions**——`prune_empty_ghost_sessions` 在 startup 跑一次清 24 小时前的空会话；TUI 落地后才有 ghost session 概念

§2.5 全 5 wave 完成后，phalanx 已具备完整的会话 round-trip：创建 → 落库 → 列举 → 续聊 → 删除 + 日志检索。后续 §2.6 REPL 在此基础上加 `/save` / `/resume` 等斜杠命令。
