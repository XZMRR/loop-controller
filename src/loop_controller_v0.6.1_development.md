# Loop Controller v0.6.1 开发指南：BudgetReservation 状态机

> **状态**：已完成（238 tests passed，ruff 干净，mypy 仅余 2 个预存在 PyYAML stub 错误）。
> **完成提交**：`feat(v0.6.1): BudgetReservation state machine`。
>
> 本文件保留为设计记录，具体实现细节以 `src/development_log.md` v0.6.1 章节为准。
>
> **目标**：把当前分散在 `Checkpoint.evaluate()` / `forward()` 中的预算预留/返还逻辑，抽象为显式的 `BudgetReservation` 状态机，消除二次预留、过期不释放、无法查询 pending 等隐患。

---

## 1. 背景与目标

v0.3.0 起预算控制通过 `BudgetLedger.check_and_reserve()` / `commit()` / `refund()` 三方法实现。v0.6.0 把 `BudgetLedger` 持久化到 JSONL，但预算的**生命周期状态**仍然是隐式的：

```python
# evaluate
self._budget_ledger.check_and_reserve(task_id, cost)

# deny / require_approval / invalid
self._refund_for(proposal)

# forward success
self._budget_ledger.commit(task_id, cost)

# forward exception
self._budget_ledger.refund(task_id, cost)
```

隐患：

1. **没有 reservation 实体**：无法回答"这个 task 现在还占着多少预算"；
2. **返还逻辑分散**：deny、require_approval、modify 复核失败、异常都手动 refund；
3. **require_approval 路径二次预留**：当前实现先 refund，resume 时再重新 reserve，一旦 resume 前 Ledger 状态变化可能失败；
4. **审批超时不会自动释放**：require_approval 过期后，若前期没有 refund（未来改保留预算时）会泄漏；
5. **modify 复核失败 refund 与异常 refund 路径不统一**。

v0.6.1 目标：

1. 引入显式 `BudgetReservation` 对象；
2. 由 Checkpoint 统一维护状态流转；
3. `require_approval` 路径保持预算预留，审批通过后直接 commit，无需二次 reserve；
4. 提供 `Checkpoint.get_pending_reservations(task_id)` 查询接口；
5. 不破坏 `BudgetLedger` Protocol 方法签名，不破坏现有测试。

---

## 2. 范围与边界

### 2.1 纳入 v0.6.1

| # | 功能 | 优先级 |
|---|---|---|
| 1 | `BudgetReservation` 模型 | P0 |
| 2 | `ReservationStore` Protocol + `InMemoryReservationStore` | P0 |
| 3 | `JsonlReservationStore`（可选，v0.6.1 争取实现） | P1 |
| 4 | `Checkpoint` 集成：evaluate / forward / resume 状态流转 | P0 |
| 5 | `Checkpoint.get_pending_reservations(task_id)` 查询接口 | P0 |
| 6 | `Runtime.resume_task()` 不再二次 reserve | P0 |
| 7 | 测试：状态流转、require_approval 不二次预留、查询接口 | P0 |

### 2.2 明确不纳入

- `BudgetLedger` Protocol 不新增方法；
- 不改动 `ActionProposal` / `Decision` 模型；
- 不引入异步过期扫描器（过期态由 forward/resume 时被动检查）；
- 不改动 CLI。

---

## 3. 设计

### 3.1 BudgetReservation 模型

```python
class BudgetReservation(BaseModel):
    reservation_id: str
    task_id: str
    call_id: str
    tool_name: str
    cost: BudgetCost
    state: Literal[
        "pending",           # 已预留，等待执行或审批
        "pending_approval",  # 已预留，等待 R0 审批
        "committed",         # 执行成功，已确认消耗
        "refunded",          # 未执行，已返还
        "expired",           # 审批/决策过期，已失效
    ]
    created_at: datetime = Field(default_factory=_utc_now)
    expires_at: datetime | None = None  # 可执行截止时间
```

### 3.2 ReservationStore Protocol

```python
class ReservationStore(Protocol):
    def save(self, reservation: BudgetReservation) -> None: ...
    def get(self, reservation_id: str) -> BudgetReservation | None: ...
    def get_by_call_id(self, call_id: str) -> BudgetReservation | None: ...
    def list_by_task(self, task_id: str) -> list[BudgetReservation]: ...
```

`InMemoryReservationStore`：默认，进程重启丢失，适合测试。
`JsonlReservationStore`：append-only JSONL，保存状态变更事件。

### 3.3 Checkpoint 状态流转

```
                check_and_reserve 成功
                            │
                            ▼
                    ┌───────────────┐
        ┌──────────│    pending    │──────────┐
        │          └───────────────┘          │
        │  deny/invalid                require_approval
        │        │                           │
        ▼        ▼                           ▼
   ┌────────┐  ┌────────────┐        ┌─────────────────┐
   │refunded│  │  refunded  │        │ pending_approval │
   └────────┘  └────────────┘        └─────────────────┘
        ▲                                        │
        │        ┌──────────────┐                │ approve
        │        │              │                ▼
        └────────│  committed   │◄────────── pending
                 │              │         (finalize 后)
                 └──────────────┘                │
                                                  │ forward success
                                                  ▼
                                            ┌──────────┐
                                            │ committed│
                                            └──────────┘
```

### 3.4 evaluate 改动

```python
# 步骤 4：预算
if hasattr(self._budget_ledger, "set_budget"):
    self._budget_ledger.set_budget(task.task_id, profile.max_budget_token)
if not self._budget_ledger.check_and_reserve(task.task_id, self._cost_for(proposal)):
    return self._deny(proposal, "budget exceeded", now, policy_version)

# 新建 reservation
reservation = self._create_reservation(proposal, now)
self._reservation_store.save(reservation)
```

后续路径：

- `deny` / `invalid verdict`：调用 `self._refund_reservation(reservation)` -> state=refunded；
- `require_approval`：调用 `self._approve_reservation(reservation, now + _APPROVAL_DELTA)` -> state=pending_approval；
- `allow/modify`：保持 `pending`。

### 3.5 forward 改动

```python
# 校验：找到 pending / pending_approval reservation
reservation = self._reservation_store.get_by_call_id(proposal.call_id)
if reservation is None or reservation.state not in ("pending", "pending_approval"):
    raise CheckpointError("找不到有效预算预留")

# modify 复核失败
if modify_recheck_fails:
    self._refund_reservation(reservation)
    return self._blocked(...)

# 执行异常
try:
    result = await self._gateway.call_tool(...)
except Exception:
    self._refund_reservation(reservation)
    raise

# 执行成功
self._commit_reservation(reservation)
```

### 3.6 resume_task 改动

当前：

```python
if decision.verdict in ("allow", "modify") and not runtime.checkpoint.reserve_for_execution(...):
    raise CheckpointError(...)
```

改为：

```python
if decision.verdict in ("allow", "modify"):
    reservation = runtime.checkpoint.get_pending_reservation(proposal.call_id)
    if reservation is None:
        # 兼容旧数据：若找不到 reservation，回退到重新预留
        if not runtime.checkpoint.reserve_for_execution(task.task_id, proposal):
            raise CheckpointError(...)
```

### 3.7 finalize_after_approval 改动

审批通过（approve）：将对应 `pending_approval` reservation 转为 `pending`，供 forward 提交；
审批拒绝（deny）：将 reservation 转为 `refunded` 并调用 BudgetLedger.refund。

### 3.8 查询接口

```python
def get_pending_reservations(self, task_id: str) -> list[BudgetReservation]:
    return [
        r for r in self._reservation_store.list_by_task(task_id)
        if r.state in ("pending", "pending_approval")
    ]

def get_pending_reservation(self, call_id: str) -> BudgetReservation | None:
    r = self._reservation_store.get_by_call_id(call_id)
    if r is not None and r.state in ("pending", "pending_approval"):
        return r
    return None
```

---

## 4. 接口变更

### 4.1 新增

- `loop_controller.models.BudgetReservation`
- `loop_controller.infra.reservation_store.ReservationStore` Protocol
- `loop_controller.infra.reservation_store.InMemoryReservationStore`
- `loop_controller.infra.reservation_store.JsonlReservationStore`
- `Checkpoint` 参数 `reservation_store: ReservationStore | None = None`
- `Checkpoint.get_pending_reservations(task_id)`
- `Checkpoint.get_pending_reservation(call_id)`

### 4.2 修改

- `Checkpoint.__init__`：新增 `reservation_store` 参数；
- `Checkpoint.evaluate`：创建 reservation 并按路径流转；
- `Checkpoint.forward`：通过 reservation 提交/返还；
- `Checkpoint.finalize_after_approval`：审批通过后 reservation 转 pending，拒绝后 refund；
- `runtime.py::resume_task`：优先复用现有 reservation；
- `runtime.py::build_runtime`：注入 `JsonlReservationStore`（如实现）。

### 4.3 不修改

- `BudgetLedger` Protocol 方法签名；
- `Decision` / `ApprovalRequest` 模型；
- CLI 行为。

---

## 5. 实现顺序

1. 新增 `BudgetReservation` 模型；
2. 新增 `ReservationStore` Protocol + `InMemoryReservationStore`；
3. Checkpoint 集成 InMemoryReservationStore（默认），实现状态流转；
4. 更新 `resume_task`；
5. 跑测试，确保 231 tests 全部通过；
6. （可选）实现 `JsonlReservationStore`；
7. （可选）`build_runtime` 注入持久化 store；
8. 新增 BudgetReservation 单元测试；
9. 更新 `development_log.md`。

---

## 6. 测试策略

### 6.1 BudgetReservation 状态机单元测试

- `test_pending_to_committed`：allow -> forward success -> committed；
- `test_pending_to_refunded_on_deny`：deny -> refunded；
- `test_pending_to_pending_approval`：require_approval -> pending_approval；
- `test_pending_approval_to_committed`：approve -> forward -> committed；
- `test_pending_approval_to_refunded_on_deny`：deny approval -> refunded；
- `test_modify_recheck_failed_refunded`：modify + 复核失败 -> refunded；
- `test_forward_exception_refunded`：forward 抛异常 -> refunded；
- `test_get_pending_reservations`：查询接口正确；
- `test_resume_no_double_reserve`：resume 不重复预留。

### 6.2 集成测试

- `test_e2e_approve_path_event_sequence` 等现有审批路径不应被破坏；
- `test_proxy_retry_approved_executes` 等 Proxy 重试路径预算状态正确。

---

## 7. 验收标准

- `pytest tests/` 全部通过（至少 231 个）；
- `ruff check src tests` 干净；
- `mypy src` 无新增错误；
- `require_approval` 路径不再二次 reserve；
- `Checkpoint.get_pending_reservations()` 可查询当前 pending reservation；
- `BudgetLedger` Protocol 不变。

---

## 8. 风险与回退

| 风险 | 缓解 |
|---|---|
| 状态流转遗漏某个分支导致预算泄漏 | 所有 evaluate/forward/finalize 路径都显式 transition；新增单元测试覆盖 |
| resume_task 改动影响旧审批数据 | 保留 fallback：找不到 reservation 时回退到 reserve_for_execution |
| JsonlReservationStore 复杂度高 | 可先只实现 InMemory，保证行为正确后再加持久化 |
