# v0.45.0：会话与对话上下文状态 SQLite 化

**Status**: 已完成
**Target Version**: `0.45.0`
**Goal**: 将 Python 工具治理层中会话（`JsonlSessionBackend`）与对话上下文（`JsonlConversationStore`）状态迁入统一 `StateDatabase`（SQLite WAL）事实源，与 v0.34.0 的 Decision / RiskState、v0.43.0 的 budget / reservation / authority / task、v0.44.0 的 alert / report 对齐。继续沿用既有约束：**不引入 PostgreSQL、外部消息队列或分布式共识**。

---

## 1. 版本定位

v0.44.0 完成后，Python 工具治理层中除审批（AES-256-GCM 敏感字段加密）、审计（哈希链物理追加顺序）外的状态已全部 SQLite 化。会话与对话上下文是 v0.44.0 §5「明确不做」中仅剩的低频、非关键路径状态，且不涉及加密、不依赖物理追加顺序，是 v0.45.0 最合理的迁移对象。

| 状态存储 | 当前实现 | 状态模型 | SQLite 迁移价值 |
| --- | --- | --- | --- |
| 会话 | `JsonlSessionBackend` | 单 JSONL 追加 + 启动重放 + (user,agent)→session 内存索引 | 主键 upsert、`get_or_create_active` 跨进程原子化 |
| 对话上下文 | `JsonlConversationStore` | 单 JSONL 追加 + 启动重放 + 每 session FIFO 保留 | 按 session 索引、LIMIT 取最近 N 条 |

---

## 2. 核心原则

1. **统一事实源**：SQLite `StateDatabase` 是唯一事实源；`InMemorySessionBackend` 保留作测试兼容实现，`JsonlSessionBackend` / `JsonlConversationStore` 保留作 `.jsonl` 路径下的兼容后端。
2. **按路径扩展名选择后端**：沿用 `runtime._is_sqlite_path()` 的 `.db/.sqlite/.sqlite3` 判定，将 `session_manager` 后端与 `conversation_store` 纳入后端选择。
3. **语义不回归**：`SessionBackend` / `ConversationStore` Protocol 的方法签名与失败语义保持一致；`Session` 的生命周期（`touch` / `close` / `get_or_create_active`）与 `ConversationStore` 的 FIFO 保留语义不回归。
4. **最小侵入**：新增 `SqliteSessionBackend` / `SqliteConversationStore` 门面 + `StateDatabase` 方法，不改动 `SessionManager` / `Runtime` 的消费逻辑。
5. **fail-closed**：SQLite 操作失败抛出 `StateDatabaseError`；会话门面统一映射为 `ValueError`（对齐内存/JSONL 后端的域错误契约），对话门面沿用底层异常直抛。

---

## 3. 设计决策

### 3.1 `StateDatabase` Schema 扩展

在 `SCHEMA` 中新增两张表（幂等 `CREATE TABLE IF NOT EXISTS`）：

```sql
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_task_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_sessions_user_agent ON sessions(user_id, agent_id);

CREATE TABLE IF NOT EXISTS conversations (
    message_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conversations_session ON conversations(session_id);
```

- `sessions.active` 用 `INTEGER`（`0/1`）表达布尔终态；`Session` 模型仍以 `active: bool` 对客。
- `conversations.role` 落 `"user"` / `"agent"` 字符串（`ConversationRole` 是 `Literal`）。
- 会话与对话分别沿用「当前活跃会话」的插入顺序语义：`sessions` 以 `rowid DESC` 取最新活跃会话（对齐 JSONL 追加后最后一条 `active` 记录获胜）；`conversations` 以 `rowid` 表达插入顺序。

### 3.2 `StateDatabase` 新增方法

沿用「每方法新建连接 + 显式事务」与 `sqlite3.Row` 行工厂。所有方法以 JSON 序列化后的 dict（`model_dump(mode="json")` 格式）为边界，由门面负责模型转换。

会话（`check-then-act` 方法显式 `_immediate`）：

```python
def save_session(self, session: dict) -> None
    # 拒绝已关闭 session 的重新激活；否则 INSERT ... ON CONFLICT(session_id) DO UPDATE

def get_session(self, session_id: str) -> dict | None

def get_active_session(self, user_id: str, agent_id: str) -> dict | None
    # WHERE user_id=? AND agent_id=? AND active=1 ORDER BY rowid DESC LIMIT 1

def touch_session(self, session_id: str, last_task_at: str, *, user_id=None, agent_id=None) -> dict
    # 校验存在 / 活跃 / 绑定一致后 UPDATE last_task_at

def close_session(self, session_id: str) -> dict
    # 存在且活跃时置 active=0；幂等返回

def get_or_create_active_session(self, user_id, agent_id, now_iso, timeout_seconds, new_session) -> dict
    # 原子查询并复用或创建
```

对话：

```python
def save_message(self, message: dict) -> None

def list_messages(self, session_id: str, limit: int) -> list[dict]
    # 按插入顺序返回最近 limit 条（FIFO 保留语义）
```

### 3.3 门面实现

新增 `infra/sqlite_session_backend.py` → `SqliteSessionBackend` 与 `infra/sqlite_conversation_store.py` → `SqliteConversationStore`，遵循既有模式：`__init__(db: StateDatabase)` + `from_path` 类方法。

- `SqliteSessionBackend` 实现 `SessionBackend` Protocol：`get_active` / `get_by_id` / `put` / `touch` / `close` / `get_or_create_active`，`model_dump` → 委托 → `Session.from_dict` 回构；`StateDatabaseError` 映射为 `ValueError`。
- `SqliteConversationStore` 实现 `ConversationStore` Protocol：`append_message` / `get_context`；`get_context` 用 `list_messages` 取最近 `max_messages_per_session` 条并回构 `ConversationContext`（`updated_at` 取末条 `created_at`，空则当前 UTC）。

### 3.4 `runtime.build_runtime()` 后端选择

将硬编码 JSONL 后端替换为按扩展名选择：

```python
if _is_sqlite_path(config.session_path):
    session_manager = SessionManager(backend=SqliteSessionBackend(_state_db_for(config.session_path)))
else:
    session_manager = SessionManager(backend=JsonlSessionBackend(config.session_path))

if _is_sqlite_path(config.conversation_path):
    conversation_store = SqliteConversationStore(
        _state_db_for(config.conversation_path),
        max_messages_per_session=config.conversation_max_messages_per_session,
    )
else:
    conversation_store = JsonlConversationStore(
        config.conversation_path,
        max_messages_per_session=config.conversation_max_messages_per_session,
    )
```

- `Runtime.conversation_store` 字段类型由 `JsonlConversationStore` 放宽为 `ConversationStore` Protocol。
- `PersistenceProbe` 目标列表已包含 `session` / `conversation`（`critical=False`），SQLite 目标会降级为目录可写探测（v0.43.0 已实现），无需额外改动。

### 3.5 迁移、备份与回滚

- **新库默认初始化**：`StateDatabase.__init__` 的 `init_schema()` 幂等建表，无需显式迁移。
- **JSONL → SQLite 迁移**：不提供自动迁移脚本；已有 `.jsonl` 数据继续由 JSONL 后端读取，切换 SQLite 需通过配置改路径为新 `.db` 文件。
- **备份/回滚**：复用 `close_wal()`；配置路径改回 `.jsonl` 即回到旧后端。

---

## 4. 实施阶段

### P45-01 扩展 `StateDatabase` schema 与 session/conversation 方法

- `state_db.py`：`SCHEMA` 新增 `sessions` / `conversations` 两表及索引；新增会话 6 个方法与对话 2 个方法。

### P45-02 实现 `SqliteSessionBackend` 门面

- 新增 `infra/sqlite_session_backend.py`。

### P45-03 实现 `SqliteConversationStore` 门面

- 新增 `infra/sqlite_conversation_store.py`。

### P45-04 `runtime.build_runtime()` 接入后端选择

- 按 `_is_sqlite_path()` 为 `session_manager` 后端与 `conversation_store` 选择 SQLite 或 JSONL 后端。

### P45-05 测试

- 新增 `tests/test_sqlite_session_backend.py`：get_or_create 复用/超时新建、touch 校验（不存在/已结束/绑定不一致）、close 终态、put 拒绝重开、重启持久化、datetime 往返。
- 新增 `tests/test_sqlite_conversation_store.py`：empty_context / append_and_retrieve / session_isolation / fifo_eviction / persistence_and_replay / datetime 往返。

### P45-06 版本统一 0.45.0

- Go `CurrentProtocolVersion`、contract fixture、OpenAPI、Python `CURRENT_PROTOCOL_VERSION`、`pyproject.toml`、`uv.lock` 及 config/tests 中的硬编码版本由 `0.44.0` 统一为 `0.45.0`；生成 `contract/a2a_v0.45.0.json` / `openapi/a2a_v0.45.0.yaml`，删除旧 `v0.44.0` 文件。

---

## 5. 明确不做

- PostgreSQL 或其他共享数据库驱动。
- 外部消息队列 / Kafka / NATS。
- 分布式共识（Raft/Paxos）。
- **审批存储**（`JsonlApprovalStore`）：涉及 AES-256-GCM + AAD 敏感字段加密，暂不迁移。
- **审计存储**（`JsonlAuditStore`）：哈希链要求物理追加顺序，继续使用 JSONL + 既有 `AuditIndex` SQLite 查询加速。
- JSONL → SQLite 的自动数据搬迁工具。

---

## 6. 完成定义

v0.45.0 只有同时满足以下条件才能完成：

1. 会话与对话上下文在配置为 `.db/.sqlite/.sqlite3` 路径时使用 SQLite 后端，在 `.jsonl` 路径时保持 JSONL 后端。
2. `SessionBackend` 六方法的失败语义与 JSONL/内存后端不回归（复用/超时新建、touch 校验、close 终态、put 拒绝重开）。
3. `ConversationStore` 的 FIFO 保留与重放语义不回归（`get_context` 返回最近 N 条）。
4. 版本统一为 `0.45.0`（Go / Python / OpenAPI / contract fixture / config）。
5. `go build ./...`、`go vet ./...`、`go test ./internal/...`、`pytest -q`、`git diff --check` 全部通过。

---

## 7. 验证记录

- 会话与对话上下文在 `.db/.sqlite/.sqlite3` 路径使用 SQLite 后端，`.jsonl` 路径保持 JSONL 后端（`runtime.build_runtime()` 按 `_is_sqlite_path()` 选择）。
- `SqliteSessionBackend` 六方法与 `SqliteConversationStore` 的 FIFO/重放语义通过新增 `tests/test_sqlite_session_backend.py`、`tests/test_sqlite_conversation_store.py` 覆盖。
- 版本统一：Go `CurrentProtocolVersion`、Python `CURRENT_PROTOCOL_VERSION`、`pyproject.toml`、`uv.lock`、OpenAPI、contract fixture 与 config/tests 硬编码均同步为 `0.45.0`；`contract/a2a_v0.45.0.json`、`openapi/a2a_v0.45.0.yaml` 已生成，旧 `v0.44.0` 文件已删除。
- `go build ./...`、`go vet ./...`、`go test ./internal/...` 全部通过。
- `pytest -q`：921 passed, 4 skipped。
- `git diff --check`：通过。
