# v0.43.0：Python 工具治理层核心状态 SQLite 化

**Status**: 完成
**Target Version**: `0.43.0`
**Goal**: 消除 Python 工具治理层中预算、预留、动态权限与任务生命周期四类核心状态对 JSONL 追加 + 内存投影 + 文件锁的依赖，将其迁入统一 `StateDatabase`（SQLite WAL）事实源，与 v0.34.0 已落地的 Decision / RiskState 对齐。继续沿用既有约束：**不引入 PostgreSQL、外部消息队列或分布式共识**。

---

## 1. 版本定位

v0.42.0 完成了 Go 侧 `registry` / `router` / `discovery` 的共享 SQLite 持久化，Agent 注册信息与路由消息已跨实例可见。但 Python 工具治理层（R1/R2/R3 控制面）仍有大量状态存储停留在 JSONL：

| 状态存储 | 当前实现 | 状态模型 | SQLite 迁移价值 |
| --- | --- | --- | --- |
| 预算账本 | `JsonlBudgetLedger` | 事件日志 + 内存 `_max/_reserved/_committed` 投影 | `check_and_reserve` 需要跨进程原子 CAS |
| 预算预留 | `JsonlReservationStore` | 状态机 + 每次 `get/list` 全量重放 | 终态校验 + 按 call_id/task_id 索引查询 |
| 动态权限令牌 | `JsonlAuthorityStore` | 状态机 + 能力交集 + 原子消费/返还 | `validate_and_consume` / `refund_if_unchanged` 需要原子条件更新 |
| 任务生命周期 | `JsonlTaskStore` | 每次 `get` 反向扫描全文件 | O(1) 主键查询 |

v0.43.0 将上述四类核心状态迁入统一 SQLite，其余状态（审批、会话、对话、告警、审计）因各自约束暂不迁移（见 §5 明确不做）。

---

## 2. 核心原则

1. **统一事实源**：SQLite `StateDatabase` 是唯一事实源；内存实现（`InMemory*`）保留作测试与无存储场景的兼容实现，JSONL 实现（`Jsonl*`）保留作 `.jsonl` 路径下的兼容后端。
2. **按路径扩展名选择后端**：沿用 `runtime._is_sqlite_path()` 的 `.db/.sqlite/.sqlite3` 判定，扩展现有 `decision` / `risk_state` 的后端选择到四类核心状态。
3. **语义不回归**：`BudgetLedger` / `ReservationStore` / `AuthorityStore` / `TaskStore` 四个 Protocol 的方法签名与状态机转移语义保持一致；`checkpointer.evaluate()` 依赖的 `set_budget` 能力必须保留。
4. **最小侵入**：新增 `Sqlite*Store` 门面 + `StateDatabase` 方法，不改动 `Checkpoint` / `EarnedAuthorityManager` / `Runtime` 的消费逻辑。
5. **fail-closed**：SQLite 操作失败抛出 `StateDatabaseError`，门面统一映射为对应领域错误（`BudgetLedgerError` / `ReservationStoreError` / `AuthorityStoreError` / `TaskStoreError`）。

---

## 3. 设计决策

### 3.1 `StateDatabase` Schema 扩展

在 `src/loop_controller/infra/state_db.py` 的 `SCHEMA` 中新增四张表（幂等 `CREATE TABLE IF NOT EXISTS`）：

```sql
-- 预算账本：物化状态（非事件日志），task_id 唯一
CREATE TABLE IF NOT EXISTS budget_ledger (
    task_id TEXT PRIMARY KEY,
    max_budget_token INTEGER NOT NULL DEFAULT 1000000,
    reserved INTEGER NOT NULL DEFAULT 0,
    committed INTEGER NOT NULL DEFAULT 0
);

-- 预算预留：取代 v0.34.0 遗留且未使用的 reservations 表
CREATE TABLE IF NOT EXISTS budget_reservations (
    reservation_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    cost_token_count INTEGER NOT NULL DEFAULT 0,
    cost_payment_amount REAL NOT NULL DEFAULT 0,
    cost_currency TEXT NOT NULL DEFAULT 'USD',
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_budget_reservations_call_id ON budget_reservations(call_id);
CREATE INDEX IF NOT EXISTS idx_budget_reservations_task_id ON budget_reservations(task_id);
CREATE INDEX IF NOT EXISTS idx_budget_reservations_state ON budget_reservations(state);

-- 动态权限令牌：能力列表与两层预算均扁平化，便于条件更新
CREATE TABLE IF NOT EXISTS authority_tokens (
    token_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    granted_capabilities_json TEXT NOT NULL DEFAULT '[]',
    budget_token_count INTEGER NOT NULL DEFAULT 0,
    budget_payment_amount REAL NOT NULL DEFAULT 0,
    budget_currency TEXT NOT NULL DEFAULT 'USD',
    remaining_token_count INTEGER NOT NULL DEFAULT 0,
    remaining_payment_amount REAL NOT NULL DEFAULT 0,
    remaining_currency TEXT NOT NULL DEFAULT 'USD',
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    audit_record_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_authority_tokens_task ON authority_tokens(task_id);

-- 任务生命周期：task_id 唯一，status/completed_at 直接落列
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    description TEXT NOT NULL,
    tenant_id TEXT,
    status TEXT NOT NULL DEFAULT 'created',
    created_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_session ON tasks(session_id);
```

**关于遗留 `reservations` 表**：v0.34.0 的 `reservations` 表（`request_id/decision_id/amount/currency/status/...`）从未被任何方法读写，且与 `BudgetReservation` 模型字段不匹配。本版本**不删除**该表（避免破坏既有数据库文件的迁移兼容），仅新增语义正确的 `budget_reservations` 表并废弃旧表。

**关于预算账本的物化建模**：`JsonlBudgetLedger` 是事件日志（`set_budget/reserve/commit/refund` 四类事件重放得到 `_max/_reserved/_committed`）。SQLite 化时改用**物化状态表**（`max_budget_token/reserved/committed` 三列直接存储当前值），理由：

- `check_and_reserve` / `commit` / `refund` 可表达为单条原子 `UPDATE`，天然满足跨进程并发安全；
- 无需启动重放，`set_budget` 为幂等 upsert，`check_and_reserve` 通过 `WHERE reserved + committed + cost <= max_budget_token` 实现 CAS；
- 启动孤儿预留告警改为扫描 `reserved > 0` 的行，替代事件重放。

### 3.2 `StateDatabase` 新增方法

沿用「每方法新建连接 + 显式事务」与 `sqlite3.Row` 行工厂。所有方法以 JSON 序列化后的 dict（`model_dump(mode="json")` 格式）为边界，由门面负责模型转换。

**Budget**

```python
def set_budget(self, task_id: str, max_budget_token: int) -> None
    # INSERT ... ON CONFLICT(task_id) DO UPDATE SET max_budget_token = excluded.max_budget_token

def check_and_reserve(self, task_id: str, token_count: int, default_max_budget_token: int) -> bool
    # 1) INSERT OR IGNORE 确保行存在（默认 max）
    # 2) UPDATE budget_ledger SET reserved = reserved + ?
    #    WHERE task_id = ? AND reserved + committed + ? <= max_budget_token
    # 返回 rowcount > 0

def commit_budget(self, task_id: str, token_count: int) -> None
    # UPDATE budget_ledger SET reserved = reserved - ?, committed = committed + ? WHERE task_id = ?

def refund_budget(self, task_id: str, token_count: int) -> None
    # UPDATE budget_ledger SET reserved = MAX(0, reserved - ?) WHERE task_id = ?

def iter_reserved_budget(self) -> list[tuple[str, int]]
    # SELECT task_id, reserved FROM budget_ledger WHERE reserved > 0
```

**Reservation**

```python
def save_reservation(self, reservation: dict) -> None
    # 事务内：读取现有 state；若已终态则相等返回、否则抛 StateDatabaseError；
    # 非终态走 INSERT ... ON CONFLICT(reservation_id) DO UPDATE

def get_reservation(self, reservation_id: str) -> dict | None
def get_reservation_by_call_id(self, call_id: str) -> dict | None
def list_reservations_by_task(self, task_id: str) -> list[dict]
def list_all_reservations(self) -> list[dict]
```

**Authority**

```python
def save_authority_token(self, token: dict, event_type: str) -> None
    # token_created 已存在且不相等 -> 抛错；其余 upsert（ON CONFLICT DO UPDATE）

def create_authority_token_if_available(self, token: dict, now: datetime) -> bool
    # 事务内：读取同 task 活跃 token，能力交集非空则返回 False；否则 INSERT，返回 True

def get_authority_token(self, token_id: str) -> dict | None
def list_all_authority_tokens(self) -> list[dict]
def list_active_authority_tokens(self, now: datetime) -> list[dict]
def list_authority_tokens_by_task(self, task_id: str) -> list[dict]

def consume_authority_token(
    self, token_id: str, cost_token_count: int, now: datetime, task_id: str, agent_id: str
) -> dict | None
    # UPDATE authority_tokens SET remaining_token_count = remaining_token_count - ?
    # WHERE token_id = ? AND task_id = ? AND agent_id = ?
    #   AND revoked_at IS NULL AND expires_at > ? AND remaining_token_count >= ?
    # rowcount == 1 时回读并返回，否则 None

def refund_authority_token(
    self, token_id: str, expected_remaining: int, cost_token_count: int
) -> dict | None
    # UPDATE authority_tokens SET remaining_token_count = remaining_token_count + ?
    # WHERE token_id = ? AND remaining_token_count = ? AND revoked_at IS NULL
    #   AND remaining_token_count + ? <= budget_token_count
    #   （budget_token_count 直接读列值，避免传入过期上限）
    # rowcount == 1 时回读并返回，否则 None
```

**Task**

```python
def save_task(self, task: dict) -> None
    # INSERT ... ON CONFLICT(task_id) DO UPDATE（幂等覆盖）

def get_task(self, task_id: str) -> dict | None
    # completed 状态返回 None（对齐 JsonlTaskStore.get 对 task_complete 的反向扫描语义）

def complete_task(self, task_id: str) -> None
    # UPDATE tasks SET status = 'completed', completed_at = ? WHERE task_id = ?
```

### 3.3 门面实现

新增四个门面文件，遵循 `SqliteDecisionStore` / `SqliteRiskStateStore` 既有模式：`__init__(db: StateDatabase)` + `from_path` 类方法，方法内将 `StateDatabaseError` 映射为对应领域错误。

**`infra/sqlite_budget_ledger.py` → `SqliteBudgetLedger(BudgetLedger)`**

- 构造参数：`db: StateDatabase`、`default_max_budget_token: int = 1_000_000`、`alert_store: AlertStore | None = None`。
- 构造时调用 `iter_reserved_budget()`，对 `reserved > 0` 的任务复用 `_emit_orphan_alert` 语义（与 `JsonlBudgetLedger` 启动告警一致）。
- `set_budget` / `check_and_reserve` / `commit` / `refund` 分别委托 `StateDatabase` 对应方法；`check_and_reserve` 传入 `default_max_budget_token`。
- 保留 `set_budget` 方法（`checkpointer.evaluate()` 的 `hasattr` 探测依赖它）。
- 不保留 `write_blocked`：SQLite 失败是确定性的（无「写入结果不确定」的尾部场景），通过抛 `BudgetLedgerError` fail-closed。

**`infra/sqlite_reservation_store.py` → `SqliteReservationStore(ReservationStore)`**

- `save`：先将 `BudgetReservation.model_dump(mode="json")` 交给 `StateDatabase.save_reservation` 的终态守卫；终态相等时幂等返回。
- `get` / `get_by_call_id` / `list_by_task` / `list_all` 委托 `StateDatabase` 并 `BudgetReservation.model_validate` 回构。

**`infra/sqlite_authority_store.py` → `SqliteAuthorityStore(AuthorityStore)`**

- `create_if_capabilities_available`：委托 `create_authority_token_if_available`（事务内完成能力交集检查 + 插入）。
- `validate_and_consume`：委托 `consume_authority_token`（原子消费）。
- `refund_if_unchanged`：先 `get_authority_token` 做全量相等校验与预算上限校验，再委托 `refund_authority_token`（以 `remaining_token_count` 为 CAS 条件）。
- `save`：委托 `save_authority_token`，处理 `token_revoked` / `token_expired`（生产调用）及 `token_created` 幂等。
- `get` / `list_all` / `list_active` / `list_by_task`：委托并 `AuthorityToken.model_validate` 回构；`list_active` 以传入 `now` 过滤。

**`infra/sqlite_task_store.py` → `SqliteTaskStore(TaskStore)`**

- `save` / `get` / `complete` 委托 `StateDatabase`；`get` 对 `status == "completed"` 返回 `None`（对齐 JSONL 语义）。

### 3.4 `runtime.build_runtime()` 后端选择

在现有 `_state_db_for()` / `_is_sqlite_path()` 基础上，将四类核心状态接入后端选择：

```python
budget_ledger: BudgetLedger
if _is_sqlite_path(config.budget_ledger_path):
    budget_ledger = SqliteBudgetLedger(
        _state_db_for(config.budget_ledger_path), alert_store=alert_store
    )
else:
    budget_ledger = JsonlBudgetLedger(config.budget_ledger_path, alert_store=alert_store)

task_store: TaskStore
if _is_sqlite_path(config.task_store_path):
    task_store = SqliteTaskStore(_state_db_for(config.task_store_path))
else:
    task_store = JsonlTaskStore(config.task_store_path)

reservation_store: ReservationStore
if _is_sqlite_path(config.reservation_store_path):
    reservation_store = SqliteReservationStore(_state_db_for(config.reservation_store_path))
else:
    reservation_store = JsonlReservationStore(config.reservation_store_path)

authority_store: AuthorityStore
if _is_sqlite_path(config.authority_log_path):
    authority_store = SqliteAuthorityStore(_state_db_for(config.authority_log_path))
else:
    authority_store = JsonlAuthorityStore(config.authority_log_path)
```

- `alert_store` 需在 `budget_ledger` 之前构造（现有代码已满足：`alert_store` 在 `budget_ledger` 之前创建）。
- 其余 store（`session` / `conversation` / `approval` / `alert` / `audit`）维持 JSONL 不变。
- `authority_manager` 改为注入上面选择的 `authority_store`。

### 3.5 `PersistenceProbe` 路径探测调整

SQLite 后端无需 JSONL 尾部修复与文件锁探测。`PersistenceProbe` 仅对 `.jsonl` 路径执行 `DurableJsonlFile` 事务探测；对 `.db/.sqlite/.sqlite3` 路径降级为**目录可写探测**（`path.parent.mkdir` + 可读可写权限检查），避免把 SQLite 文件当 JSONL 解析。

- 新增 `PersistenceTarget.sqlite: bool`（默认 `False`）；`_is_sqlite_path` 命中时置 `True`。
- `_probe_target` 对 `sqlite=True` 的目标跳过 `DurableJsonlFile.transaction()`，仅做父目录创建 + `os.access` 可写检查。
- 目标列表仍保留 `budget` / `reservation` / `authority` / `task` 四类路径，无论后端类型。

### 3.6 迁移、备份与回滚

- **新库默认初始化**：`StateDatabase.__init__` 的 `init_schema()` 幂等建表，无需显式迁移。
- **JSONL → SQLite 迁移**：不提供自动迁移脚本；已有 `.jsonl` 数据继续由 JSONL 后端读取，切换 SQLite 需通过配置改路径为新 `.db` 文件。原因：四类状态均为运行期治理状态，历史数据无需跨后端搬迁，且避免引入一次性迁移工具。
- **备份**：复用 `StateDatabase.close_wal()`（`PRAGMA wal_checkpoint(TRUNCATE)`）在备份前落 WAL。
- **回滚**：配置路径改回 `.jsonl` 即回到旧后端；SQLite 表为增量新增，不删除任何既有数据。

---

## 4. 实施阶段

### P43-01 扩展 `StateDatabase` schema 与预算/预留方法

- `state_db.py`：`SCHEMA` 新增 `budget_ledger` / `budget_reservations` / `authority_tokens` / `tasks` 四表及索引。
- 新增 Budget 与 Reservation 的 `StateDatabase` 方法（见 §3.2）。

### P43-02 扩展 `StateDatabase` 权限/任务方法

- 新增 Authority 与 Task 的 `StateDatabase` 方法（见 §3.2）。

### P43-03 实现四个 SQLite 门面

- 新增 `sqlite_budget_ledger.py` / `sqlite_reservation_store.py` / `sqlite_authority_store.py` / `sqlite_task_store.py`。

### P43-04 `runtime.build_runtime()` 接入后端选择

- 按 `_is_sqlite_path()` 为 budget / task / reservation / authority 选择 SQLite 或 JSONL 后端。

### P43-05 `PersistenceProbe` 调整

- 支持 SQLite 目标降级为目录可写探测。

### P43-06 测试

- 新增 `tests/test_sqlite_budget_ledger.py`：set_budget / check_and_reserve（含超额拒绝）/ commit / refund / 重启持久化 / 多进程并发 reserve。
- 新增 `tests/test_sqlite_reservation_store.py`：save/get/get_by_call_id/list_by_task/list_all / 终态守卫 / 幂等。
- 新增 `tests/test_sqlite_authority_store.py`：create（能力交集冲突）/ validate_and_consume（余额不足拒绝）/ refund_if_unchanged（CAS）/ revoke / expire。
- 新增 `tests/test_sqlite_task_store.py`：save/get/complete / completed 返回 None / 重启持久化。
- 既有 `pytest -q` 全量通过（JSONL 后端测试不受影响）。

### P43-07 版本统一 0.43.0

- Go `CurrentProtocolVersion`、contract fixture、OpenAPI、Python `CURRENT_PROTOCOL_VERSION`、`pyproject.toml`、`uv.lock` 及 config/tests 中的硬编码版本由 `0.42.0` 统一为 `0.43.0`。

---

## 5. 明确不做

- PostgreSQL 或其他共享数据库驱动。
- 外部消息队列 / Kafka / NATS。
- 分布式共识（Raft/Paxos）。
- **审批存储**（`JsonlApprovalStore`）：涉及 AES-256-GCM + AAD 敏感字段加密与不可覆盖响应约束，暂不迁移。
- **会话/对话存储**（`JsonlSessionBackend` / `JsonlConversationStore`）：低频、非关键路径，暂不迁移。
- **告警存储**（`JsonlAlertStore`）：非核心状态，暂不迁移。
- **审计存储**（`JsonlAuditStore`）：哈希链要求物理追加顺序，不适合简单迁为普通 SQLite 表；继续使用 JSONL + 既有 `AuditIndex` SQLite 查询加速。
- JSONL → SQLite 的自动数据搬迁工具。
- 删除 v0.34.0 遗留的 `reservations` 表。

---

## 6. 完成定义

v0.43.0 只有同时满足以下条件才能完成：

1. budget / reservation / authority / task 四类状态在配置为 `.db/.sqlite/.sqlite3` 路径时使用 SQLite 后端，在 `.jsonl` 路径时保持 JSONL 后端。
2. `check_and_reserve` / `validate_and_consume` / `refund_if_unchanged` 具备跨进程原子性（单语句/事务级 CAS）。
3. Reservation 终态守卫、Authority 能力交集、Task completed 语义与 JSONL 后端不回归。
4. `checkpointer.evaluate()` 依赖的 `set_budget` 能力保留，治理链路端到端行为不变。
5. 版本统一为 `0.43.0`（Go / Python / OpenAPI / contract fixture / config）。
6. `go build ./...`、`go vet ./...`、`go test ./internal/...`、`pytest -q`、`git diff --check` 全部通过。

---

## 7. 验证记录

- `go build ./...`：通过。
- `go vet ./...`：通过。
- `go test ./internal/... -count=1`：通过（`store` 幂等并发测试首次偶发 SQLITE_BUSY，单测与全量重跑均通过，属既有 flaky，与本次改动无关）。
- `pytest -q`：896 passed, 4 skipped。
- `git diff --check`：通过。
- 版本统一确认：Go `CurrentProtocolVersion`、Python `CURRENT_PROTOCOL_VERSION`、`pyproject.toml`、`uv.lock`、OpenAPI、contract fixture、config 与 Python 测试硬编码均已同步为 `0.43.0`；`contract/a2a_v0.43.0.json` 与 `openapi/a2a_v0.43.0.yaml` 已生成，旧 `v0.42.0` 文件已删除。
