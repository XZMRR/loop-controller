# v0.44.0：告警与审计报告状态 SQLite 化

**Status**: 完成
**Target Version**: `0.44.0`
**Goal**: 将 Python 工具治理层中告警与审计报告状态（`JsonlAlertStore` 的 `AuditAlert` / `AuditReport`）迁入统一 `StateDatabase`（SQLite WAL）事实源，与 v0.34.0 的 Decision / RiskState、v0.43.0 的 budget / reservation / authority / task 对齐。继续沿用既有约束：**不引入 PostgreSQL、外部消息队列或分布式共识**。

---

## 1. 版本定位

v0.43.0 已完成预算、预留、动态权限与任务生命周期四类核心状态的 SQLite 化。告警存储（`JsonlAlertStore`）是 v0.43.0 §5「明确不做」中唯一不涉及敏感字段加密、也不依赖物理追加顺序的状态，且它是 `SqliteBudgetLedger` / `HarnessExecutor` / `AuditAnalyzer` / `JsonlApprovalStore` / `JsonlAuditStore` / `HTTPAnchorBackend` 的共同依赖，当前仍为 JSONL 追加 + 启动重放。

v0.44.0 将其迁入统一 SQLite，使 Python 工具治理层除审批（AES-256-GCM 敏感字段）、会话/对话（低频）、审计（哈希链物理追加顺序）外的状态全部落在同一 `StateDatabase` 事实源。

| 状态存储 | 当前实现 | 状态模型 | SQLite 迁移价值 |
| --- | --- | --- | --- |
| 告警 | `JsonlAlertStore` | 单 JSONL + `type` 区分 alert/report + 启动重放 | 按 session/task 索引查询、主键 upsert |
| 审计报告 | `JsonlAlertStore` | 同上 | `get_report` O(1) 主键查询 |

---

## 2. 核心原则

1. **统一事实源**：SQLite `StateDatabase` 是唯一事实源；`InMemoryAlertStore` 保留作测试兼容实现，`JsonlAlertStore` 保留作 `.jsonl` 路径下的兼容后端。
2. **按路径扩展名选择后端**：沿用 `runtime._is_sqlite_path()` 的 `.db/.sqlite/.sqlite3` 判定，将 `alert_store` 纳入后端选择。
3. **语义不回归**：`AlertStore` Protocol 的方法签名与覆盖语义保持一致；`save_alert` / `save_report` 对同主键为幂等覆盖（对齐 JSONL 字典覆盖），`list_*` 支持 `session_id` / `task_id` 可选过滤。
4. **最小侵入**：新增 `SqliteAlertStore` 门面 + `StateDatabase` 方法，不改动 `HarnessExecutor` / `AuditAnalyzer` / `JsonlApprovalStore` / `JsonlAuditStore` / `HTTPAnchorBackend` 的消费逻辑。
5. **fail-closed**：SQLite 操作失败抛出 `StateDatabaseError`，门面统一映射为 `AlertStoreError`。

---

## 3. 设计决策

### 3.1 `StateDatabase` Schema 扩展

在 `SCHEMA` 中新增两张表（幂等 `CREATE TABLE IF NOT EXISTS`）：

```sql
CREATE TABLE IF NOT EXISTS alerts (
    alert_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    task_id TEXT,
    rule_id TEXT NOT NULL,
    severity TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_session ON alerts(session_id);
CREATE INDEX IF NOT EXISTS idx_alerts_task ON alerts(task_id);

CREATE TABLE IF NOT EXISTS reports (
    report_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    task_id TEXT,
    generated_at TEXT NOT NULL,
    summary TEXT NOT NULL,
    alert_ids_json TEXT NOT NULL DEFAULT '[]',
    event_count INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_reports_session ON reports(session_id);
CREATE INDEX IF NOT EXISTS idx_reports_task ON reports(task_id);
```

- `evidence` / `alert_ids` 为 `list[str]`，`metadata` 为 `dict[str, Any]`，均以 JSON 序列化落列。
- `task_id` 可空，索引允许 `NULL` 匹配（`WHERE task_id IS NULL`）。
- alert 与 report 拆为两张表，避免 JSONL 中依赖 `type` 字段区分的重放逻辑；主键 `alert_id` / `report_id` 天然保证覆盖语义。

### 3.2 `StateDatabase` 新增方法

沿用「每方法新建连接 + 显式事务」与 `sqlite3.Row` 行工厂。所有方法以 JSON 序列化后的 dict（`model_dump(mode="json")` 格式）为边界，由门面负责模型转换。

```python
def save_alert(self, alert: dict) -> None
    # INSERT ... ON CONFLICT(alert_id) DO UPDATE（幂等覆盖）

def list_alerts(self, session_id: str | None, task_id: str | None) -> list[dict]
    # SELECT ... WHERE (session_id IS NULL OR session_id = ?) AND (task_id IS NULL OR task_id = ?)

def save_report(self, report: dict) -> None
    # INSERT ... ON CONFLICT(report_id) DO UPDATE（幂等覆盖）

def get_report(self, report_id: str) -> dict | None

def list_reports(self, session_id: str | None, task_id: str | None) -> list[dict]
```

### 3.3 门面实现

新增 `infra/sqlite_alert_store.py` → `SqliteAlertStore`，遵循 `SqliteDecisionStore` / `SqliteTaskStore` 既有模式：`__init__(db: StateDatabase)` + `from_path` 类方法，方法内将 `StateDatabaseError` 映射为 `AlertStoreError`。

- `save_alert` / `save_report`：`model_dump(mode="json")` 后委托 `StateDatabase`。
- `list_alerts` / `list_reports`：委托并 `AuditAlert` / `AuditReport` `model_validate` 回构。
- `get_report`：委托并回构；缺失返回 `None`。

### 3.4 `runtime.build_runtime()` 后端选择

将 `alert_store = JsonlAlertStore(config.alert_store_path)` 替换为按扩展名选择：

```python
alert_store: AlertStore
if _is_sqlite_path(config.alert_store_path):
    alert_store = SqliteAlertStore(_state_db_for(config.alert_store_path))
else:
    alert_store = JsonlAlertStore(config.alert_store_path)
```

- `alert_store` 需在 `budget_ledger` 之前构造（现有代码已满足）。
- 下游 `harness_executor` / `budget_ledger` / `evidence_anchor` / `audit_store` / `approval_store` / `audit_analyzer` 均消费 `AlertStore` 协议，不感知后端差异。
- `PersistenceProbe` 目标列表中 `alert` 路径已存在，且 SQLite 目标会降级为目录可写探测（v0.43.0 已实现），无需额外改动。

### 3.5 迁移、备份与回滚

- **新库默认初始化**：`StateDatabase.__init__` 的 `init_schema()` 幂等建表，无需显式迁移。
- **JSONL → SQLite 迁移**：不提供自动迁移脚本；已有 `.jsonl` 数据继续由 JSONL 后端读取，切换 SQLite 需通过配置改路径为新 `.db` 文件。
- **备份/回滚**：复用 `close_wal()`；配置路径改回 `.jsonl` 即回到旧后端。

---

## 4. 实施阶段

### P44-01 扩展 `StateDatabase` schema 与告警/报告方法

- `state_db.py`：`SCHEMA` 新增 `alerts` / `reports` 两表及索引；新增 `save_alert` / `list_alerts` / `save_report` / `get_report` / `list_reports` 方法。

### P44-02 实现 `SqliteAlertStore` 门面

- 新增 `infra/sqlite_alert_store.py`。

### P44-03 `runtime.build_runtime()` 接入后端选择

- 按 `_is_sqlite_path()` 为 `alert_store` 选择 SQLite 或 JSONL 后端。

### P44-04 测试

- 新增 `tests/test_sqlite_alert_store.py`：save/list alert（含 session/task 过滤）/ save/get report / list report / 覆盖幂等 / 重启持久化 / datetime 往返。

### P44-05 版本统一 0.44.0

- Go `CurrentProtocolVersion`、contract fixture、OpenAPI、Python `CURRENT_PROTOCOL_VERSION`、`pyproject.toml`、`uv.lock` 及 config/tests 中的硬编码版本由 `0.43.0` 统一为 `0.44.0`。

---

## 5. 明确不做

- PostgreSQL 或其他共享数据库驱动。
- 外部消息队列 / Kafka / NATS。
- 分布式共识（Raft/Paxos）。
- **审批存储**（`JsonlApprovalStore`）：涉及 AES-256-GCM + AAD 敏感字段加密，暂不迁移。
- **会话/对话存储**（`JsonlSessionBackend` / `JsonlConversationStore`）：低频、非关键路径，暂不迁移。
- **审计存储**（`JsonlAuditStore`）：哈希链要求物理追加顺序，继续使用 JSONL + 既有 `AuditIndex` SQLite 查询加速。
- JSONL → SQLite 的自动数据搬迁工具。

---

## 6. 完成定义

v0.44.0 只有同时满足以下条件才能完成：

1. 告警与报告在配置为 `.db/.sqlite/.sqlite3` 路径时使用 SQLite 后端，在 `.jsonl` 路径时保持 JSONL 后端。
2. `save_alert` / `save_report` 对同主键为幂等覆盖，`list_*` 的 `session_id` / `task_id` 过滤语义与 JSONL 后端不回归。
3. `get_report` 对缺失项返回 `None`。
4. 版本统一为 `0.44.0`（Go / Python / OpenAPI / contract fixture / config）。
5. `go build ./...`、`go vet ./...`、`go test ./internal/...`、`pytest -q`、`git diff --check` 全部通过。

---

## 7. 验证记录

- `go build ./...`：通过。
- `go vet ./...`：通过。
- `go test -count=1 ./internal/...`：通过。
- `pytest -q`：905 passed, 4 skipped。
- `git diff --check`：通过。
- 新增 `tests/test_sqlite_alert_store.py`：9 个用例全部通过（save/list alert、session/task 过滤、幂等覆盖、save/get report、缺失返回 None、report 过滤、重启持久化、datetime 往返）。
- 版本统一确认：Go `CurrentProtocolVersion`、Python `CURRENT_PROTOCOL_VERSION`、`pyproject.toml`、`uv.lock`、OpenAPI、contract fixture、config 与 Go/Python 测试硬编码均已同步为 `0.44.0`；`contract/a2a_v0.44.0.json` 与 `openapi/a2a_v0.44.0.yaml` 已生成，旧 `v0.43.0` 文件已删除。
