# Loop Controller v0.8.0 开发指南：持久化 BudgetReservation 存储

> **状态**：已完成（248 tests passed，ruff 干净，mypy 仅余 2 个预存在 PyYAML stub 错误）。
> **完成提交**：`feat(v0.8.0): persistent JsonlReservationStore`。
>
> 本文件保留为设计记录，具体实现细节以 `src/development_log.md` v0.8.0 章节为准。
>
> **目标**：把 v0.6.1 中仍为内存实现的 `ReservationStore` 升级为持久化 JSONL 实现，让 BudgetReservation 状态机在 Runtime/Proxy 重启后可恢复。

---

## 1. 背景与目标

v0.6.1 引入了 `BudgetReservation` 状态机，由 `Checkpoint` 统一维护预算预留的生命周期。但当时只实现了 `InMemoryReservationStore`，所有 reservation 状态保存在进程内存中。

这导致：

1. Runtime/Proxy 进程重启后，pending/pending_approval 的 reservation 丢失；
2. 虽然 `JsonlBudgetLedger` 能恢复预算余额，但无法恢复"这笔预算对应的是哪一次调用、处于什么状态"；
3. 跨 Runtime 重试时，若 reservation 不存在，`forward()` 会退回到现场预留，可能重复占用预算；
4. 无法查询历史 reservation 轨迹。

v0.8.0 的目标：实现 `JsonlReservationStore`，让 reservation 状态和预算事件一样持久化到 append-only JSONL。

---

## 2. 范围与边界

### 2.1 纳入 v0.8.0

| # | 功能 | 优先级 |
|---|---|---|
| 1 | `JsonlReservationStore` 实现 | P0 |
| 2 | `AppConfig.reservation_store_path` + 环境变量 | P0 |
| 3 | `build_runtime()` 默认注入 `JsonlReservationStore` | P0 |
| 4 | `Checkpoint` 从 `Runtime` 接收 `reservation_store` | P0 |
| 5 | 启动时重放恢复 reservation 状态 | P0 |
| 6 | 损坏文件 fail-closed | P1 |
| 7 | 测试覆盖：持久化、重启恢复、跨 Runtime 一致性 | P0 |

### 2.2 明确不纳入

- 多 worker 并发原子性（仍单进程 asyncio 假设）；
- 将其他 Store（DecisionStore、ApprovalStore、RiskStateStore）也持久化；
- R3 审计分析；
- R1 小模型。

---

## 3. 设计

### 3.1 存储格式

JSONL，每行一个事件：

```json
{"type": "reservation_created", "reservation_id": "res_xxx", "task_id": "task_xxx", "call_id": "call_xxx", "tool_name": "send_email", "cost": {"token_count": 100}, "state": "pending", "created_at": "...", "expires_at": "..."}
{"type": "reservation_transitioned", "reservation_id": "res_xxx", "state": "pending_approval", "expires_at": "...", "timestamp": "..."}
{"type": "reservation_transitioned", "reservation_id": "res_xxx", "state": "committed", "timestamp": "..."}
{"type": "reservation_transitioned", "reservation_id": "res_xxx", "state": "refunded", "timestamp": "..."}
{"type": "reservation_transitioned", "reservation_id": "res_xxx", "state": "expired", "timestamp": "..."}
```

### 3.2 恢复逻辑

启动时重放所有事件：

- 遇到 `reservation_created`：建立 reservation 对象；
- 遇到 `reservation_transitioned`：更新对应 reservation 的 `state` 和 `expires_at`；
- 最终保留每个 `reservation_id` 的最新状态。

内存索引：

- `_by_id: dict[str, BudgetReservation]`
- `_by_call_id: dict[str, str]`
- `_by_task: dict[str, set[str]]`

### 3.3 JsonlReservationStore 类

```python
@dataclass
class JsonlReservationStore:
    path: PathLike

    def __post_init__(self) -> None:
        self._path = Path(str(self.path))
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._by_id: dict[str, BudgetReservation] = {}
        self._by_call_id: dict[str, str] = {}
        self._by_task: dict[str, set[str]] = {}
        self._replay()
```

方法：

- `save(reservation)`：append `reservation_created` 或 `reservation_transitioned` 事件；
- `get(reservation_id)` -> `BudgetReservation | None`；
- `get_by_call_id(call_id)` -> `BudgetReservation | None`；
- `list_by_task(task_id)` -> `list[BudgetReservation]`。

### 3.4 Checkpoint 使用 Runtime 的 reservation_store

当前 `Checkpoint` 在 `__init__` 中默认创建 `InMemoryReservationStore`。生产环境下，`build_runtime()` 需要把 `JsonlReservationStore` 传给 `Checkpoint`。

方案：

- `Runtime` 新增 `reservation_store: ReservationStore` 字段；
- `build_runtime()` 创建 `JsonlReservationStore` 并注入；
- `Checkpoint.__init__` 接收 `reservation_store`，`Runtime` 构造时传入；
- 测试默认仍用 `InMemoryReservationStore`（通过 `Runtime` 默认值）。

### 3.5 Config 扩展

```yaml
reservation_store_path: "data/reservations.jsonl"
```

环境变量覆盖：`LOOP_CONTROLLER_RESERVATION_STORE_PATH`。

---

## 4. 接口变更

### 4.1 新增

- `JsonlReservationStore`（`src/loop_controller/infra/reservation_store.py`）
- `AppConfig.reservation_store_path`
- `Runtime.reservation_store: ReservationStore`

### 4.2 修改

- `Runtime` 新增 `reservation_store` 字段，并传给 `Checkpoint`；
- `build_runtime()` 创建 `JsonlReservationStore`；
- `ConfigLoader` 读取 `reservation_store_path`；
- `Checkpoint.__init__` 保留默认 `InMemoryReservationStore`，但优先使用传入值。

### 4.3 不修改

- `BudgetLedger` Protocol；
- `ReservationStore` Protocol；
- `BudgetReservation` 模型；
- `Checkpoint.evaluate/forward/finalize_after_approval` 签名。

---

## 5. 实现顺序

1. 扩展 `AppConfig`；
2. `ConfigLoader` 读取环境变量；
3. 实现 `JsonlReservationStore`；
4. `Runtime` 新增 `reservation_store` 字段；
5. `build_runtime()` 注入持久化 store；
6. 确保 `Checkpoint` 从 `Runtime` 接收 store；
7. 新增/更新测试；
8. 更新 `development_log.md`。

---

## 6. 测试策略

### 6.1 JsonlReservationStore 单元测试

- `test_save_and_get`：保存 reservation 后按 id/按 call_id 读取；
- `test_list_by_task`：按 task 过滤；
- `test_transition_overwrite`：多次 save 返回最新状态；
- `test_persistence_across_reconstruction`：重建 store 对象后状态恢复；
- `test_corrupted_file_fail_closed`：损坏文件抛 `ReservationStoreError`；
- `test_datetime_roundtrip`：datetime 字段正确序列化/反序列化。

### 6.2 Runtime 集成测试

- 使用 `JsonlReservationStore` 的 Runtime，`create_task` + evaluate 后 reservation 持久化；
- 新建 Runtime 读取同一文件，能查到 pending reservation。

### 6.3 回归测试

- 所有已有 checkpoint / runtime / proxy / e2e 测试通过，验证未破坏既有行为。

---

## 7. 验收标准

- `pytest tests/` 全部通过（至少 242 个）；
- `ruff check src tests` 干净；
- `mypy src` 无新增错误；
- `JsonlReservationStore` 持久化 reservation 状态；
- Runtime 重启后能恢复 pending/pending_approval reservation；
- 损坏文件 fail-closed。

---

## 8. 风险与回退

| 风险 | 缓解 |
|---|---|
| JSONL 写入性能 | 单机 MVP，先保证正确性 |
| 与 InMemoryReservationStore 行为不一致 | 两套实现共享同一 Protocol，单元测试覆盖两套 |
| 多进程写冲突 | v0.8.0 仍单进程假设，文档中明确声明 |
| Checkpoint 默认仍用内存版 | 生产由 build_runtime 注入，测试默认不受影响 |
