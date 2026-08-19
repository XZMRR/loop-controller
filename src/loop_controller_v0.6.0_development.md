# Loop Controller v0.6.0 开发指南：持久化基础设施（TaskStore + BudgetLedger）

> **状态**：已完成（226 tests passed，ruff/mypy 干净，仅余 2 个预存在 PyYAML stub 错误）。
> **完成提交**：`feat(v0.6.0): persistent TaskStore and BudgetLedger`。
>
> 本文件保留为设计记录，具体实现细节以 `src/development_log.md` v0.6.0 章节为准。
>
> **目标**：消除 v0.5.1 已知的 Proxy 重启重试失败问题，并让预算状态在生产环境可持久化。

---

## 1. 背景与目标

v0.5.1 让 MCP Proxy 支持审批后重试，但实现时把 Task 缓存在 Proxy 进程内存中。一旦 Proxy 进程重启，重试会因为找不到原始 Task 而失败。

同时，当前 `InMemoryBudgetLedger` 在进程重启后也会丢失所有 `reserved` / `committed` 记录，导致预算控制失效。

v0.6.0 的核心目标：

1. 引入 `TaskStore` 持久化 `Task` 对象，支持 Proxy 重启后恢复；
2. 引入 `JsonlBudgetLedger` 持久化预算事件，支持重启后重放恢复；
3. 让 `Runtime` 默认使用持久化实现；
4. 保持现有 API 不变，行为与 v0.5.1 兼容。

---

## 2. 范围与边界

### 2.1 纳入 v0.6.0

| # | 功能 | 优先级 |
|---|---|---|
| 1 | `TaskStore` Protocol + `JsonlTaskStore` | P0 |
| 2 | `Runtime.create_task()` 持久化 Task | P0 |
| 3 | `Runtime.get_task()` 从 Store 读取 Task | P0 |
| 4 | `BudgetLedger` Protocol 扩展 + `JsonlBudgetLedger` | P0 |
| 5 | `build_runtime()` 默认使用 `JsonlBudgetLedger` + `JsonlTaskStore` | P0 |
| 6 | `ProxyServer` 重试时通过 `Runtime.get_task()` 恢复 Task | P0 |
| 7 | 启动时重放恢复与损坏 fail-closed | P1 |
| 8 | 测试覆盖：持久化、重启恢复、Proxy 重试跨进程恢复 | P0 |

### 2.2 明确不纳入

- `BudgetReservation` 状态机：只保证持久化，不引入新的状态对象；
- Task 状态机扩展：Task 仍然只有创建，没有执行状态；
- 多机共享存储：仍只支持单机 append-only JSONL；
- 历史 Task 清理：不实现过期删除。

---

## 3. 设计

### 3.1 Task 模型

当前 `Task` 已经是 Pydantic v2 `frozen=True` 模型，可直接序列化。在 `Task` 中新增 `status` 字段表示生命周期：

```python
class Task(BaseModel):
    task_id: str
    session_id: str
    user_id: str
    agent_id: str
    description: str
    status: Literal["created", "completed"] = "created"  # v0.6.0 新增
    created_at: datetime = Field(default_factory=_utc_now)
    completed_at: datetime | None = None  # v0.6.0 新增
```

> 注：当前 `Task` 没有 status 字段。新增字段是向后兼容的，默认值为 `created`。

### 3.2 TaskStore Protocol

```python
class TaskStore(Protocol):
    def save(self, task: Task) -> None: ...
    def get(self, task_id: str) -> Task | None: ...
    def complete(self, task_id: str) -> None: ...
```

`JsonlTaskStore` 行为：

- `save(task)`：append 一行 `{"type": "task", "task_id": "...", ...}`；
- `get(task_id)`：从文件尾部向前读，找到最后一个 `type == "task"` 且 `task_id` 匹配的行；
- `complete(task_id)`：append 一行 `{"type": "task_complete", "task_id": "...", "completed_at": "..."}`；
- 损坏 fail-closed：遇到非法 JSON 直接抛 `TaskStoreError`。

### 3.3 Runtime 集成

在 `Runtime` 中新增：

```python
task_store: TaskStore
```

`create_task()` 在构造 Task 后立即 `task_store.save(task)`。

`Runtime.get_task(task_id: str) -> Task | None` 委托给 `task_store.get(task_id)`。

`run_task()` 在任务结束时调用 `task_store.complete(task.task_id)`。

### 3.4 JsonlBudgetLedger

事件类型：

- `{"type": "reserve", "task_id": "...", "token_count": 100, "timestamp": "..."}`
- `{"type": "commit", "task_id": "...", "token_count": 100, "timestamp": "..."}`
- `{"type": "refund", "task_id": "...", "token_count": 100, "timestamp": "..."}`
- `{"type": "set_budget", "task_id": "...", "max_budget_token": 10000, "timestamp": "..."}`

启动时重放所有事件恢复内存状态：

```python
self._reserved: dict[str, int] = defaultdict(int)
self._committed: dict[str, int] = defaultdict(int)
self._max: dict[str, int] = {}
```

实现注意：

- `refund` 使 `reserved` 可能低于 0 时clamp为 0；
- `commit` / `refund` 的 token_count 来自 `BudgetCost`；
- 文件损坏 fail-closed。

### 3.5 BudgetLedger Protocol 调整

当前 `BudgetLedger` 在 `loop_controller.checkpoint` 中定义为 Protocol。保持 Protocol 方法不变：

```python
class BudgetLedger(Protocol):
    def set_budget(self, task_id: str, max_budget_token: int) -> None: ...
    def check_and_reserve(self, task_id: str, cost: BudgetCost) -> bool: ...
    def commit(self, task_id: str, cost: BudgetCost) -> None: ...
    def refund(self, task_id: str, cost: BudgetCost) -> None: ...
```

`JsonlBudgetLedger` 实现该 Protocol。

### 3.6 ProxyServer 重试跨进程恢复

当前 `_handle_retry` 在 Task 缓存丢失时直接返回错误：

```python
task = self._tasks.get(request.task_id)
if task is None:
    return self._error_result("original task not available...")
```

v0.6.0 改为：

```python
task = self._runtime.get_task(request.task_id)
if task is None:
    return self._error_result("original task not available...")
```

不再需要 ProxyServer 内部 `_tasks` 缓存。该缓存可以保留作为性能优化，但恢复逻辑应以 `Runtime.get_task()` 为主。

### 3.7 配置与路径

在 `AppConfig` 中新增：

```yaml
task_store_path: "data/tasks.jsonl"
budget_ledger_path: "data/budget.jsonl"
```

`build_runtime()` 默认路径：

- `task_store_path = project_root / "data" / "tasks.jsonl`
- `budget_ledger_path = project_root / "data" / "budget.jsonl`

环境变量覆盖：

- `LOOP_CONTROLLER_TASK_STORE_PATH`
- `LOOP_CONTROLLER_BUDGET_LEDGER_PATH`

---

## 4. 接口变更

### 4.1 新增

- `loop_controller.infra.task_store.TaskStore` Protocol
- `loop_controller.infra.task_store.JsonlTaskStore`
- `loop_controller.budget.JsonlBudgetLedger`
- `Runtime.task_store: TaskStore`
- `Runtime.get_task(task_id: str) -> Task | None`

### 4.2 修改

- `Task`：新增 `status`, `completed_at` 字段；
- `build_runtime()`：使用 `JsonlBudgetLedger` 和 `JsonlTaskStore`；
- `ProxyServer._handle_retry()`：优先使用 `Runtime.get_task()` 恢复 Task。

### 4.3 不修改

- `Checkpoint` 不感知 TaskStore；
- `BudgetLedger` Protocol 方法签名不变；
- `run_task` / `resume_task` 签名不变。

---

## 5. 实现顺序

1. 扩展 `Task` 模型（status / completed_at）；
2. 实现 `JsonlTaskStore` 与 `TaskStoreError`；
3. `Runtime` 增加 `task_store` 和 `get_task()`，`create_task()` 调用 `save()`；
4. `run_task()` 结束时调用 `task_store.complete()`；
5. 实现 `JsonlBudgetLedger`；
6. `build_runtime()` 切换为持久化实现；
7. `AppConfig` 增加 `task_store_path` / `budget_ledger_path`；
8. `ConfigLoader` 加载新配置；
9. `ProxyServer._handle_retry()` 改用 `Runtime.get_task()`；
10. 删除或保留 `_tasks` 缓存（可选）；
11. 写测试；
12. 更新 `development_log.md` 和 `KNOWN_LIMITATIONS.md`。

---

## 6. 测试策略

### 6.1 JsonlTaskStore 单元测试

- `test_save_and_get`：保存 Task 后能读到；
- `test_complete_updates_status`：complete 后读到 `status="completed"`；
- `test_latest_wins`：多次 save 同 task_id 返回最新；
- `test_corrupted_file_fail_closed`：损坏文件抛 `TaskStoreError`。

### 6.2 JsonlBudgetLedger 单元测试

- `test_reserve_commit_refund_replayed`：事件序列后余额正确；
- `test_persistence_across_reconstruction`：重建对象后状态恢复；
- `test_corrupted_file_fail_closed`：损坏文件抛异常。

### 6.3 Runtime 集成测试

- `test_create_task_persists`：create_task 后能从新 Runtime 读到；
- `test_run_task_completes_task`：run_task 后状态为 completed。

### 6.4 Proxy 跨进程恢复测试

- 构造 Runtime A，调用 send_email 触发 require_approval；
- 批准；
- 构造 Runtime B 使用同一 `task_store_path` / `approval_store_path` / `session_path` / `risk_state_path`；
- 用 Runtime B 启动新的 ProxyServer；
- 重试带 decision_id 的 send_email；
- 验证成功执行。

---

## 7. 验收标准

- `pytest tests/` 全部通过；
- `ruff check src tests` 干净；
- `mypy src` 无新增错误；
- Proxy 进程重启后，审批通过的重试仍能成功执行；
- 预算事件持久化，重启后余额状态恢复；
- `KNOWN_LIMITATIONS.md` 中 F11 标记为已解决。

---

## 8. 风险与回退

| 风险 | 缓解 |
|---|---|
| JSONL 写入性能 | 当前是单机 MVP，先保证正确性；性能优化延后 |
| 并发写损坏 | asyncio 单进程，不存在并发写；多进程场景为已知限制 |
| `Task` 新增字段影响旧测试 | 默认值保持兼容，冻结模型新增字段无破坏 |
| `InMemoryBudgetLedger` 被替换影响 mock | 保留 `InMemoryBudgetLedger`，测试仍可显式使用 |

---

## 9. 后续方向

v0.6.0 完成后：

- v0.6.1：`BudgetReservation` 状态机抽象，或 `ActionProposal.intent_tag`；
- v0.7.0：Proxy 内置 `loop_controller_approval_status` 查询工具；
- v0.8.0：MCP Proxy 审批结果 SSE 推送（可选）。
