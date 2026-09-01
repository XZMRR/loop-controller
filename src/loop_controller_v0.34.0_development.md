# v0.34.0 工具治理层跑稳（二）：执行器与审计耐久性

> 一句话目标：**把当前基于 JSONL 追加 + 内存索引的决策/风险/审计存储升级为可长期运行、可多进程安全访问、可观察的耐久性基础设施，并同步强化 Harness 执行器的资源隔离、热更新与幂等能力，使 Loop Controller 从“功能正确”进入“生产可长期运行”。**
>
> 范围限定：本版本聚焦 Python 工具治理层内部的执行器与审计耐久性；Go 交互治理内核、多租户 SaaS、KMS/HSM 等进入 v0.35.0/v0.36.0。

- 状态：**开发中/部分完成**
- 前置版本：v0.33.0 工具治理层健壮性加固（一）：SDK 与 API 入口安全
- 版本性质：耐久性/生产化第二阶段
- 核心范围：DecisionStore/RiskStateStore SQLite 化、AuditStore 性能与只读快照、Harness 执行器资源隔离与热更新、远程取消与幂等基础
- 当前进度：
  - ✅ `StateDatabase` 统一 SQLite 后端与 Schema 已完成；
  - ✅ `SqliteDecisionStore` / `SqliteRiskStateStore` 已实现并迁移 JSONL 数据；
  - ✅ `AuditIndex` 与审计写入一致性已实现；
  - ✅ `IsolatedSubprocessHarnessBackend` 资源限制（Linux/macOS）与输出大小截断已实现；
  - ✅ `HarnessExecutor.update_specs()` 平滑热更新已实现（backend 增删改、drain、回滚）；
  - ✅ `HotReloader` 已接入 `harness_tools.yaml` 监控；
  - ✅ Harness HTTP 远程取消协议路径 `/harness/v2/cancel` 与 `_HTTPHarnessClient.cancel_call()` 已实现；
  - ✅ `HarnessExecutor` 按 `call_id` 的幂等缓存与在途调用跟踪已实现；
  - ✅ 新增 `tests/test_harness_hot_reload.py` 与 `tests/test_isolated_subprocess_harness.py` 扩展测试，单元测试全绿；
  - ⬜ 集成测试需有效 OPA 二进制后运行。
- 验证目标：
  - ✅ `pytest tests/ -m "not integration" -q` 全绿且覆盖新增耐久性路径；
  - ⬜ `pytest tests/integration -m integration -q` 保持 22 passed；
  - ✅ `python -m ruff check src tests` 通过；
  - ✅ 新增 Harness 资源限制/取消/幂等测试。

---

## 1. 背景

v0.33.0 把 Agent SDK、MCP Proxy、HTTP REST API 和配置校验的安全与稳定性漏洞堵住，使接入面达到可生产基线。但底层决策、风险、审计数据仍基于 JSONL 追加 + 进程内内存索引：

- 启动时必须重放整个 JSONL 才能恢复 `_finalized_decisions`、`_risk_sessions`、审计索引；
- 多进程同时写入同一文件无原子语义，依赖 `portalocker` 跨进程锁但仍存在读-改-写窗口；
- `JsonlAuditStore.list_recent()`、`verify_chain()` 在大文件下退化明显；
- Harness 执行器缺少资源隔离、后端配置不支持热更新，超时后无法取消远端动作，也缺少跨实例幂等保证。

v0.34.0 要解决的不是新增功能，而是让现有三条接入线（`@governed`、MCP Proxy、HTTP REST API）背后的执行与审计链路真正耐跑。

---

## 2. 当前问题清单（本次版本处理）

### P0-1：决策与风险状态存储的耐久性不足

| 编号 | 问题 | 文件位置 | 严重度 |
|---|---|---|---|
| ST-H1 | `JsonlDecisionStore` 启动重放为 O(n)，决策记录随时间线性增长；`is_consumed` 检查也退化为 O(n) | `src/loop_controller/infra/decision_store.py` | 高 |
| ST-H2 | `RiskStateStore` 基于单进程内存 + JSONL 追加，多进程/多 worker 共享文件时无原子更新 | `src/loop_controller/infra/risk_state_store.py` | 高 |
| ST-H3 | 决策/风险/审计均使用独立 JSONL 文件，缺少统一事务边界 | 多处 | 高 |
| ST-M1 | `DecisionStore` 的 `_finalized_decisions` 在进程重启后重放恢复，但运行期 `is_consumed` 为内存集合 | `src/loop_controller/infra/decision_store.py` | 中 |
| ST-M2 | `RiskStateStore` 未提供只读快照，策略评估时可能读到正在写入的中间状态 | `src/loop_controller/infra/risk_state_store.py` | 中 |

### P0-2：审计存储性能与观察性不足

| 编号 | 问题 | 文件位置 | 严重度 |
|---|---|---|---|
| AU-H1 | `JsonlAuditStore.verify_chain()` 全量读取审计文件，大文件下极慢 | `src/loop_controller/infra/audit_store.py` | 高 |
| AU-H2 | `list_recent` / 按 trace 查询需要线性扫描 | `src/loop_controller/infra/audit_store.py` | 高 |
| AU-H3 | 审计写入与证据写入不在同一事务中，可能产生审计-证据不一致 | `src/loop_controller/infra/audit_store.py`, `evidence.py` | 高 |
| AU-M1 | 缺少只读快照，长查询阻塞写入 | `src/loop_controller/infra/audit_store.py` | 中 |
| AU-M2 | 审计事件分级掩码在 `model_dump()` 中重复计算 | `src/loop_controller/infra/audit_store.py` | 中 |

### P0-3：Harness 执行器生产化不足

| 编号 | 问题 | 文件位置 | 严重度 |
|---|---|---|---|
| HS-H1 | 子进程/容器 Harness 无 CPU/内存/输出上限，存在资源耗尽风险 | `examples/contrib/harness/`, `harness_executor.py` | 高 |
| HS-H2 | `harness_tools.yaml` 仅在启动时加载，后端配置变更需重启 | `src/loop_controller/infra/config_loader.py`, `harness_executor.py` | 高 |
| HS-H3 | HTTP/Harness 调用超时后无法取消远端动作，结果未知 | `src/loop_controller/executors/harness_executor.py` | 高 |
| HS-H4 | 缺少 `call_id` 幂等保证，远端重复执行无法识别 | `src/loop_controller/executors/harness_executor.py` | 高 |
| HS-M1 | Docker backend 不保证容器退出与资源清理 | `examples/contrib/harness/docker_backend.py` | 中 |
| HS-M2 | `effective_sandbox` 校验为字符串比较，容易被绕过 | `src/loop_controller/executors/harness_executor.py` | 中 |

### P0-4：配置与工程化

| 编号 | 问题 | 文件位置 | 严重度 |
|---|---|---|---|
| CFG-H1 | `_check_dirs_writable` 已覆盖路径，但未校验数据库目录 | `src/loop_controller/infra/config_loader.py` | 高 |
| CFG-H2 | SQLite 升级后需要迁移路径与回退开关 | `src/loop_controller/infra/config_loader.py` | 高 |
| CI-H1 | 耐久性测试缺少大文件/多进程/long-running 用例 | `tests/` | 高 |

---

## 3. 设计原则

1. **默认安全**：新存储必须 fail-closed，数据库不可用时拒绝启动或降级到已知安全模式。
2. **向后兼容**：现有 JSONL 文件必须能无缝迁移到 SQLite，或保留 JSONL 作为只读归档。
3. **最小侵入**：上层接口 `DecisionStore`、`RiskStateStore`、`AuditStore` 协议不变，仅后端实现升级。
4. **可观察**：所有耐久性操作必须暴露 Prometheus 指标（延迟、错误率、队列深度）。
5. **资源边界**：Harness 子进程/容器必须带 CPU、内存、输出大小、网络隔离边界。

---

## 4. 详细设计

### 4.1 DecisionStore / RiskStateStore SQLite 化

#### 4.1.1 统一 SQLite 数据库

引入 `src/loop_controller/infra/state_db.py`，作为 Decision、Risk、Approval、Reservation 等状态表的统一 SQLite 后端：

```python
class StateDatabase:
    """统一状态数据库：Decision / Risk / Approval / Reservation."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._pool: aiosqlite.Pool | None = None  # 或 sqlite3 + 线程池

    async def init_schema(self) -> None:
        async with self._write() as conn:
            await conn.executescript(SCHEMA)
            await conn.execute("PRAGMA journal_mode=WAL;")
            await conn.execute("PRAGMA foreign_keys=ON;")

    @asynccontextmanager
    async def _write(self):
        ...

    @asynccontextmanager
    async def _read(self):
        ...
```

数据表设计：

- `decisions(request_id PRIMARY KEY, serialized JSON, expires_at, finalized BOOL, consumed_at, call_id UNIQUE)`
- `risk_state(session_id PRIMARY KEY, user_id, agent_id, score REAL, events JSON, updated_at)`
- `reservations(request_id PRIMARY KEY, amount, currency, status, expires_at)`
- `approvals(request_id PRIMARY KEY, ...)`（与现有 `JsonlApprovalStore` 共存或逐步替换）

#### 4.1.2 `DecisionStore` 升级

保留 `DecisionStore` 协议，新增 `SqliteDecisionStore` 实现：

```python
class SqliteDecisionStore:
    def __init__(self, db: StateDatabase) -> None:
        self._db = db

    async def get(self, request_id: str) -> Decision | None:
        ...

    async def save(self, decision: Decision) -> None:
        ...

    async def is_consumed(self, request_id: str, call_id: str) -> bool:
        """O(1) 索引查询。"""
        ...

    async def mark_consumed(self, request_id: str, call_id: str) -> None:
        ...
```

启动期：
- 若配置 `state.db_path` 存在且为 SQLite，直接使用；
- 若只有 JSONL 归档，执行一次性迁移：`JsonlDecisionStore.migrate_to(db)`；
- 若两者都不存在，新建 SQLite。

#### 4.1.3 `RiskStateStore` 升级

同样保留协议，新增 `SqliteRiskStateStore`：

```python
class SqliteRiskStateStore:
    async def get(self, session_id: str) -> RiskSnapshot:
        ...

    async def merge(self, session_id: str, event: RiskEvent) -> RiskSnapshot:
        ...
```

支持只读快照：策略评估前 `snapshot = await risk_store.snapshot(session_id)`，避免写入干扰。

#### 4.1.4 事务边界

Checkpoint 的 `forward()` 流程：

```python
async with state_db.transaction():
    decision = await policy_engine.evaluate(...)
    await decision_store.save(decision)
    await risk_store.merge(session_id, event)
    await budget_ledger.reserve(...)
```

任一环节失败整体回滚，避免决策已存但预算未预留的状态。

### 4.2 AuditStore 性能与一致性改造

#### 4.2.1 SQLite 审计索引表

保留 JSONL 证据文件作为追加日志（符合不可篡改要求），但将关键索引写入 SQLite：

```python
class AuditIndex:
    """审计事件索引：加速 list_recent / trace / verify_chain。"""

    async def init_schema(self):
        await conn.executescript("""
            CREATE TABLE audit_events(
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT UNIQUE,
                timestamp REAL,
                trace_id TEXT,
                request_id TEXT,
                action TEXT,
                level TEXT,
                offset_bytes INTEGER,
                length INTEGER,
                prev_hash TEXT,
                hash TEXT
            );
            CREATE INDEX idx_audit_trace ON audit_events(trace_id);
            CREATE INDEX idx_audit_request ON audit_events(request_id);
            CREATE INDEX idx_audit_time ON audit_events(timestamp);
        """)
```

#### 4.2.2 写入一致性

审计事件写入时：
1. 在 WAL 事务中先写入 JSONL 文件；
2. 成功后插入 `audit_events` 索引表；
3. 若索引插入失败，记录为 `degraded` 状态，但 JSONL 仍保留（审计完整性优先）。

证据写入同步化：
- `AuditStore.append_async` 与 `EvidenceChain.append` 在同一线程/事务边界内顺序执行，避免审计-证据分叉。

#### 4.2.3 只读快照与 verify_chain 优化

```python
async def verify_chain(self, *, up_to: int | None = None) -> VerifyResult:
    """使用索引表快速校验链；up_to 限制只校验最近 N 条。"""
    ...

async def list_recent(self, *, limit: int = 100, before: float | None = None) -> list[AuditEvent]:
    """O(log n) 索引查询 + 顺序读取 JSONL 片段。"""
    ...
```

### 4.3 Harness 执行器生产化

#### 4.3.1 资源隔离

`HarnessToolSpec` 增加 `sandbox` 字段 v3：

```yaml
sandbox:
  max_cpu_ms: 1000
  max_memory_mb: 128
  max_output_bytes: 65536
  network_mode: none
```

`IsolatedSubprocessBackend`：
- 使用 `resource.setrlimit` 限制子进程 CPU/内存（Linux/macOS）；
- Windows 使用 `joblib` 或 `subprocess` + `CreateJobObject` 限制；
- 输出缓冲区大小限制，超过截断。

`DockerBackend`：
- `--cpus`、`--memory`、`--network none`、`--read-only`、`--rm`；
- 强制 `--init` 处理僵尸进程。

#### 4.3.2 后端热更新

将 `HarnessBackendConfig` 纳入 `HotReloader`（已存在但 Harness 未接入）：

```python
class HarnessExecutor:
    async def reload_backends(self, configs: list[HarnessBackendConfig]) -> None:
        # 平滑替换：新后端先 health check，旧后端 drain 后移除
        ...
```

#### 4.3.3 远程取消与幂等

协议 v3 扩展：

```python
class HarnessRequestV3:
    call_id: str          # 幂等键
    cancel_token: str     # 取消凭证
    idempotency_key: str  # 客户端生成的跨实例幂等键
```

参考 Harness 实现：
- `POST /v1/execute/{call_id}/cancel`：取消在途请求；
- `GET /v1/execute/{call_id}/status`：幂等查询结果；
- 已完成/已取消的请求返回缓存结果，避免重复执行。

`HarnessExecutor` 侧：
- 发起请求时记录 `call_id` 到 `StateDatabase.inflight_calls`；
- 超时时尝试 `POST /v1/execute/{call_id}/cancel`；
- 失败后通过 `GET /v1/execute/{call_id}/status` 消解不确定结果。

### 4.4 配置加载与迁移

#### 4.4.1 配置扩展

`config/state.yaml`：

```yaml
state:
  db_path: ./data/state.db
  migrate_from_jsonl: true
  audit:
    index_db_path: ./data/audit_index.db
    wal_enabled: true
```

#### 4.4.2 启动校验扩展

`_check_dirs_writable` 覆盖 `state.db_path`、`audit_index_db_path` 父目录。

#### 4.4.3 迁移开关

```python
if config.state.migrate_from_jsonl:
    await JsonlMigrator.run(decision_jsonl, risk_jsonl, approval_jsonl, db)
```

---

## 5. 配置变更

### 5.1 新增 `config/state.yaml`

```yaml
state:
  db_path: ./data/state.db
  migrate_from_jsonl: true
  audit:
    index_db_path: ./data/audit_index.db
    wal_enabled: true
```

### 5.2 扩展 `config/harness_tools.yaml` sandbox 字段

```yaml
harness_tools:
  send_email:
    backends:
      - name: email_harness
        type: http
        url: https://harness.example.com
    sandbox:
      max_cpu_ms: 1000
      max_memory_mb: 128
      max_output_bytes: 65536
      network_mode: none
```

---

## 6. 接口变更

### 6.1 新增

- `StateDatabase`：统一 SQLite 状态数据库；
- `SqliteDecisionStore` / `SqliteRiskStateStore`；
- `AuditIndex`：审计事件 SQLite 索引；
- `HarnessRequestV3`：远程取消与幂等键；
- `HarnessExecutor.reload_backends()`；
- `config/state.yaml`。

### 6.2 修改

- `DecisionStore` / `RiskStateStore` 协议增加 `async` 方法（现有实现同步改异步，或保留同步兼容层）；
- `AuditStore.list_recent` / `verify_chain` 改为 `async` 以使用 SQLite 索引；
- `HarnessBackendConfig` 增加 `sandbox` v3 字段；
- `_check_dirs_writable` 覆盖 SQLite 路径。

### 6.3 保留

- JSONL 审计文件仍作为追加日志与证据链的物理载体；
- 成功响应格式不变；
- `@governed` / MCP Proxy / HTTP REST API 语义不变。

---

## 7. 测试计划

### 7.1 单元测试

- `tests/test_state_db.py`（新建）
  - `test_decision_store_persists_and_recovers`
  - `test_decision_store_is_consumed_atomic`
  - `test_risk_state_merge_atomic`
  - `test_state_db_transaction_rollback`
  - `test_jsonl_migration_idempotent`
- `tests/test_audit_index.py`（新建）
  - `test_list_recent_uses_index`
  - `test_verify_chain_fast_path`
  - `test_audit_write_degraded_when_index_fails`
- `tests/test_harness_executor.py` 扩展
  - `test_harness_v3_idempotency`
  - `test_harness_cancel_after_timeout`
  - `test_harness_resource_limits_applied`
- `tests/test_config_loader.py` 扩展
  - `test_state_db_dir_writable_check`
  - `test_jsonl_migration_switch`

### 7.2 集成测试

- 保持现有 22 个集成测试通过；
- 新增 `tests/integration/test_harness_long_running.py`：验证后端热更新、远程取消、幂等。

### 7.3 耐久性测试

- `tests/stress/test_audit_large_file.py`（可选，不纳入 CI 默认）：10 万条审计事件写入后 `verify_chain` / `list_recent` 性能基线。

### 7.4 CI 验证

- `pytest tests/ -m "not integration" -q` 全绿；
- `pytest tests/integration -m integration -q` 22 passed；
- `python -m ruff check src tests` 通过。

---

## 8. 验收标准

1. `DecisionStore` 默认使用 SQLite，启动重放不再依赖 JSONL 全量扫描；
2. `is_consumed` 与 `mark_consumed` 在 SQLite 事务中原子执行，多进程并发测试通过；
3. `RiskStateStore` 支持只读快照，策略评估不读到写入中间状态；
4. `AuditStore.list_recent` 与 `verify_chain` 在大文件下不再线性退化；
5. 审计事件写入与证据写入顺序一致，索引失败时进入 `degraded` 状态但不丢审计事件；
6. Harness 子进程/容器带 CPU/内存/输出大小限制；
7. `harness_tools.yaml` 变更后通过 Admin API 或文件热更新生效，无需重启；
8. HTTP/Harness 调用超时后可远程取消，取消失败时通过幂等查询消解不确定结果；
9. `call_id` 幂等键在重复调用时返回相同结果，不重复执行；
10. 现有 JSONL 数据可一键迁移到 SQLite，迁移幂等；
11. `_check_dirs_writable` 覆盖所有 SQLite 与 JSONL 持久化路径；
12. CI 保持 unit + integration 分层，全量测试通过。

---

## 9. 非目标

- **Go 交互治理内核**：v0.35.0 再启动 Agent 间交互治理设计；
- **多租户隔离**：`tenant_id` 字段保留，但完整租户隔离进入 v0.36.0；
- **KMS/HSM 集成**：证据密钥仍从环境变量/文件读取；
- **分布式 DecisionStore**：跨机器共享状态仍靠外部数据库，本版本先做单机 SQLite；
- **审计链远程存储**：本地 JSONL 仍是证据载体，远程 S3/OSS 进入 v0.36.0。

---

## 10. 风险与回退

| 风险 | 缓解 |
|---|---|
| SQLite 改造影响现有 `@governed` 同步调用路径 | 保留同步兼容层 `SyncDecisionStoreWrapper`，内部使用 asyncio 线程池调用 SQLite；|
| 迁移失败导致启动崩溃 | `migrate_from_jsonl` 默认 true，失败时抛出 `ConfigValidationError` 并保留原 JSONL 不动；|
| SQLite WAL 在某些文件系统上不支持 | 启动校验检测 `journal_mode=WAL` 是否设置成功，失败回退到 `DELETE` 模式并告警；|
| Harness 取消/幂等需要后端配合 | 仅对声明 `protocol_version >= 3` 的 backend 启用，旧 backend 保持 v2 行为；|
| 审计索引表与 JSONL 漂移 | 每次启动校验索引最后 N 条与 JSONL 实际内容一致，不一致时重建索引。|

---

## 11. 备注

- v0.34.0 是“工具治理层跑稳”第二阶段，核心是把内存/JSONL 状态升级到可耐久、可观察、可并发的 SQLite 后端；
- v0.35.0 将输出 Python 工具治理层与 Go 交互治理层的分层职责文档，并启动 A2A/Go 内核设计；
- 本版本结束后，Loop Controller 将具备单机多 worker 长期运行的基础能力。
