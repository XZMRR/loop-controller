# v0.29.0 审批与状态恢复闭环

> 一句话目标：**修复人工审批跨进程闭环失效与预算/决策状态泄漏，让"审批 → 恢复执行"在任何部署形态下都可靠闭环，且不再因崩溃或放弃而永久占用预算。**

- 状态：开发方案（待代码 agent 实现）
- 前置：v0.28.0 已发布（`e87eb9e`）
- 对应问题来源：全项目健壮性审查（3 个 P0 + 关联 P1）

---

## 1. 背景与问题清单

v0.28.0 之后对全项目做了健壮性审查，发现三个 P0 直接破坏**审批核心链路**与**预算硬约束**，另有若干关联 P1。本版本只做"审批与状态恢复"主题，其余健壮性主题（持久化加固、超时控制）分别归入后续版本。

### P0-1：CLI 审批结果对运行中的 Runtime 进程不可见（审批闭环失效）

- `JsonlApprovalStore._load()` 仅在 `__init__` 执行一次（`src/loop_controller/infra/approval_store.py:67-72`），之后从不重读文件；
- CLI（独立进程）执行 `lc approvals approve <decision_id>` 时，`record_response` 只更新 **CLI 进程自己的内存字典**并 append 到 JSONL（`approval_store.py:124-127`、`src/loop_controller/cli.py:241`）；
- Runtime 进程的 `AsyncApprovalManager.check()` → `store.get_record()` 读内存（`src/loop_controller/approval_manager.py:29-31`），**永远看不到 CLI 写入的结果**；
- `resume_after_approval` 因此永远返回 `require_approval`（"approval not yet decided"，`src/loop_controller/controller.py:438-447`），`wait_for_approval` 长轮询/SSE 也永远 pending（`src/loop_controller/server.py`）；
- `ApprovalWatcher.notify()` 全项目无调用方（`src/loop_controller/approval_watcher.py:32-38`），同进程也无法唤醒等待者；
- 现有测试全部在同一进程内直接调 `store.record_response`，未暴露此问题。

> 影响：人工审批在真实跨进程部署下**完全不生效**，直到 Runtime 重启重放文件。

### P0-2：过期预算预留永不清理，预算被永久占用

- `Checkpoint._expire_reservation()` 已实现但**无任何调用方**（`src/loop_controller/checkpoint.py:359-361`）；
- `evaluate()` 创建 `pending` reservation（`expires_at = now + 5min`），Agent 放弃执行/崩溃/超时后 reservation 永远停留 pending；
- `get_pending_reservation()` / `get_pending_reservations()` 只看 `state`，不检查 `expires_at`（`checkpoint.py:363-376`）；
- 进程重启后 `JsonlReservationStore._replay` 恢复 pending、`JsonlBudgetLedger._replay` 恢复 reserved 计数，**泄漏跨重启持续**；
- `Runtime.start()` 无任何恢复/清扫逻辑（`src/loop_controller/runtime.py:176-190`）。

> 影响：预算上限形同虚设；审批超时的 `pending_approval` 预留同样永久占用。

### P0-3：重复 approve/deny 无原子性，审批结果可被静默翻转

- `cli.py:213-241` 先 `get_record` 检查再 `record_response`，两个 CLI 进程可同时通过检查各自写盘；
- `record_response` 允许**覆盖同一 decision_id 的旧结果**（"覆盖同一 decision_id 的旧结果"注释，`approval_store.py:124-127`）——先 deny 后 approve 会静默翻转决定；
- `_load` 重放时后写的 response 覆盖先写的（`approval_store.py:94-99`），审计上出现矛盾记录；
- CLI 与 Runtime 双进程 append 同一 JSONL 无任何文件锁，Windows 上多行写不保证原子。

### 关联 P1（一并修复）

| 编号 | 问题 | 位置 |
|---|---|---|
| R1 | `forward()` 异常路径（decision 过期 / use_decision 失败）不 refund 已查到的 reservation | `checkpoint.py:778-800` |
| R2 | `_finalized_decisions` 是内存态，重启丢失："finalize 成功但 use_decision 落盘前崩溃"窗口可导致重复执行 | `checkpoint.py:251, 616-671` |
| R3 | `_commit_reservation` 不校验当前状态，可对已 refunded/committed 的 reservation 再次 commit | `checkpoint.py:346-349` |
| R4 | `finalize_after_approval` 在 deny-comment 校验（L646-647）之前就 `add(decision_id)`（L640），提前"烧掉"决策 | `checkpoint.py:638-647` |
| R5 | 重复 resume 第二次返回 `status="deny"`（`CheckpointError` 统一转 deny），Agent 误判为"审批被拒" | `controller.py:502-513` |
| R6 | CLI 审批不校验 Decision 过期，过期请求可被审批且永远显示为待审批 | `cli.py:207-241` |
| R7 | `record.verdict` 非 "deny" 一律按 approve 处理（checkpoint 与 controller 两侧逻辑不一致） | `checkpoint.py:645` / `controller.py:472-477` |
| R8 | budget.refund 与 reservation 状态流转分两次落盘，崩溃窗口可致重复 commit | `checkpoint.py:331-337` |
| R9 | modify 复核只比较参数键集合，不比较参数值 | `checkpoint.py:805-807` |

---

## 2. 设计总览

```text
审批闭环（P0-1）
  CLI / Server Admin ──写入──> JsonlApprovalStore（JSONL）
        ▲                            │ 增量 refresh
        │ record_response             ▼
        │                     Runtime 内存合并新行
        │                            │
        └── notify（同进程）          ├─ resume_after_approval 可见
                                      └─ wait_for_approval 轮询可见

预算闭环（P0-2）
  Runtime.start() ──> recover_stale_reservations()
        ├─ 扫描过期 pending / pending_approval
        ├─ refund + expired + 审计事件
        └─ 孤儿 reserve 告警（不 fail）

原子性（P0-3 / R3 / R8）
  record_response：已存在结果则拒绝覆盖（幂等重复除外）
  重放：保留第一条 response
  写路径：跨进程文件锁
  _commit_reservation：校验状态机合法转移
```

---

## 3. 详细设计

### 3.1 审批跨进程可见性（P0-1）

#### 3.1.1 `JsonlApprovalStore` 增量 refresh

新增方法（线程安全，`threading.Lock` 保护）：

```python
def refresh(self) -> None:
    """增量读取自上次位置以来新增的行，合并进内存。"""
```

实现要点：

- 记录已读字节偏移 `self._read_offset`（或已读行数），`refresh` 从偏移处读剩余行；
- 新行解析规则与 `_load` 一致；`request` 以 `decision_id` 去重覆盖（原语义保留），`response` 已存在则**忽略新行**（与 3.3 的"保留第一条"一致）；
- 文件被外部截断/重建（偏移超过文件大小）时重置偏移为 0 并全量重放；
- 半行/损坏行处理：末行不完整忽略并 WARNING（与 3.3 损坏策略一致），中间行损坏告警并跳过（不 fail，审批存储的可用性优先；但必须记录告警审计事件，避免 fail-open 无感）。

#### 3.1.2 调用点接线

- `AsyncApprovalManager.check()` / `get_request()` / `get_request_by_id()` / `get_decision()` 调用前先 `self._store.refresh()`；
- `ApprovalStore` 协议增加 `refresh()` 方法签名；`InMemoryApprovalStore` 空实现；
- `server.py` 的 `wait_for_approval`（1 秒轮询，`server.py:386-403`）与 SSE 路径（`server.py:409-462`）无需改动——它们最终走 `controller.resume_after_approval` → `approval_manager.check()`，refresh 后即可感知新结果；
- `resume_after_approval`（`controller.py:404-447`）同样无需改动。

#### 3.1.3 `ApprovalWatcher` 接线

- 新增同进程通知路径：Server 增加 Admin 审批 API（见 3.1.4），在写入 record 后调用 `watcher.notify(request_id)`；
- 跨进程 CLI 无法调用 notify，依赖轮询 refresh（可接受：当前 wait 端点本就是 1 秒轮询语义）；
- 明确：**本版本不做服务端主动推送**，保持"Agent 主动轮询/重试"的既有语义，只是把"永远看不到结果"修成"下次轮询就能看到"。

#### 3.1.4 Server Admin 审批 API（可选，推荐实现）

新增两个 Admin 端点（与现有 anchor/revocation admin 端点同一鉴权与审计模式）：

- `POST /v1/admin/approvals/{decision_id}/approve`，body 可选 `{"approver": "...", "comment": "..."}`；
- `POST /v1/admin/approvals/{decision_id}/deny`，body 必填 `{"approver": "...", "comment": "..."}`；
- 行为：校验审批人存在/不是请求者/不是执行 Agent（复用 `cli.py:218-224` 的校验逻辑）→ `store.record_response` → `watcher.notify(request_id)` → 写 admin 审计事件；
- 校验逻辑与 CLI 抽取为共享函数，避免双份实现漂移（新增 `approval_service.py` 或复用现有模块）。

### 3.2 预算预留清扫（P0-2）

#### 3.2.1 `Runtime.start()` 增加恢复清扫

新增 `recover_stale_reservations()`（在 `start()` 中、锚点验证通过且未 write_blocked 后调用）：

```text
for reservation in reservation_store.list_all():
    if reservation.state not in ("pending", "pending_approval"):
        continue
    if reservation.expires_at >= now:
        continue
    budget_ledger.refund(reservation.task_id, reservation.cost)   # 释放预留
    transition(reservation, "expired")                            # 标记过期
    audit: action=reservation_expired, metadata={reservation_id, call_id, state, cost}
```

- 无论 `pending`（Agent 放弃/崩溃）还是 `pending_approval`（审批超时）都 refund——因为对应 Decision 已过期，不可能再执行；
- 幂等：`refund` 前检查 reservation 当前状态，已 refunded/expired 跳过；
- `JsonlReservationStore` 新增 `list_all()`（若没有）供扫描。

#### 3.2.2 查询与 forward 的过期校验

- `get_pending_reservation(call_id)`：增加 `reservation.expires_at >= now` 条件，过期返回 None；
- `get_pending_reservations(task_id)`：过滤过期；
- `forward()`：查到的 reservation 若过期，按"未预留"处理——不 commit、不 refund，返回 `CheckpointError("reservation expired")` 并继续走正常 fail 路径（R1 修复后该路径会 refund 兜底）。

#### 3.2.3 孤儿 reserve 告警（不 fail）

`JsonlBudgetLedger._replay` 结束时，统计未闭环的 reserve（无对应 commit/refund）：

- 仅产生告警事件（alert_store），不阻断启动；
- 因为孤儿 reserve 可能对应"reservation 落盘前崩溃"（`checkpoint.py:440-445` 的窗口），本版本不自动补账，交由告警人工处理。

### 3.3 审批原子性与防覆盖（P0-3）

#### 3.3.1 `record_response` 拒绝覆盖

```python
def record_response(self, record: ApprovalRecord) -> None:
    existing = self._responses.get(record.decision_id)
    if existing is not None and existing != record:
        raise ApprovalStoreError(
            f"decision {record.decision_id} 已有审批结果，不允许覆盖")
    self._responses[record.decision_id] = record
    self._append(_serialize_record(record))
```

- 相同内容重复写入（幂等重试）允许；
- 新增 `ApprovalStoreError` 异常类型（或复用现有），CLI 捕获后友好提示。

#### 3.3.2 重放保留第一条

- `_load` / `refresh` 中 `response` 行：`self._responses.setdefault(decision_id, response)`（已有则忽略），与 3.3.1 的拒绝覆盖语义一致；
- 这样即使历史文件里有矛盾记录，重放也取第一条（第一次审批决定为准）。

#### 3.3.3 跨进程写锁

- `_append` 写入时加跨进程文件锁（Windows `msvcrt.locking` / POSIX `fcntl.flock`）；
- 依赖选择：推荐引入 `portalocker`（跨平台、纯 Python、无额外二进制），`pyproject.toml` 增加依赖；
- 若不愿引入新依赖，退化为"同锁文件重写整文件"方案（审批文件量小，读-改-写 + 锁可接受），文档优先推荐 `portalocker`；
- 锁粒度：仅包裹 `open("a") + write + flush` 段；读取方（refresh）不加锁，靠"末行不完整忽略"容错。

### 3.4 重启恢复补全（R1 / R2 / R8）

#### 3.4.1 `_finalized_decisions` 持久化（R2）

- `finalize_after_approval` 在所有校验通过后、返回前，追加一条 `finalized` 记录到 decision log：

```python
{"type": "finalized", "decision_id": ..., "finalized_at": ...}
```

- `JsonlDecisionStore` 扩展：新增 `record_finalized(decision_id)` + 重放时恢复 `_finalized_decisions` 集合；
- 这样"finalize 成功但 use_decision 落盘前崩溃"重启后，`_finalized_decisions` 已恢复，再次 resume 会被拒（`checkpoint.py:638-639`）；
- 决策顺序修正（R4）：`self._finalized_decisions.add(decision_id)` 移到**所有校验（含 deny-comment 校验）通过之后**。

#### 3.4.2 `forward()` 异常路径 refund（R1）

- `forward()` 中已查到的 reservation，在后续任何 `CheckpointError` 抛出前统一 `_refund_reservation(reservation)`：

```python
reservation = self.get_pending_reservation(proposal.call_id)
try:
    # 校验 + use_decision + 执行
    ...
except CheckpointError:
    if reservation is not None:
        self._refund_reservation(reservation)
    raise
```

- `_refund_reservation` 已幂等（`checkpoint.py:331-337` 检查 state），可安全重复调用。

#### 3.4.3 状态机合法转移（R3 / R8）

- `_commit_reservation`：校验 `current.state in ("pending", "pending_approval")`，否则抛 `CheckpointError`；
- `_transition_reservation`：定义合法转移表：

```text
pending          → pending_approval / committed / refunded / expired
pending_approval → pending / committed / refunded / expired
committed/refunded/expired → （终态，无转移）
```

非法转移抛 `CheckpointError`；
- 崩溃窗口（R8）：`_refund_reservation` 改为**先写 reservation 状态、后 budget.refund**。若崩溃在两步之间：reservation 已 refunded，重启清扫不会重复 commit；budget 未退还的额度由 3.2.3 的孤儿告警兜底（人工对账）。此顺序保证**不重复 commit**（安全侧优先）。

### 3.5 错误分类与边界修正（R5 / R6 / R7 / R9）

- **R5**：新增 `DecisionAlreadyConsumed` 异常（`checkpoint.py` 抛出），`controller.py` 捕获后返回 `status="error"` + `error_code="decision_already_consumed"`，与真实 deny 区分；MCP Proxy 重试路径同样处理；
- **R6**：CLI `approve/deny` 前检查 `request.original_decision.expires_at`，过期则拒绝并提示；`lc approvals list` 过滤过期请求（或标注 `[expired]`）；
- **R7**：`checkpoint.finalize_after_approval` 与 `controller.resume_after_approval` 两侧统一三态处理：`approve` / `deny` / 其他值抛 `CheckpointError`，不再"非 deny 即 approve"；
- **R9**：modify 复核改为 `canonical_json(decision.modified_args) == canonical_json(proposal.arguments)` 全量比较（复用 `utils/canonical.py`）。

---

## 4. 配置与接口变更汇总

| 变更 | 类型 | 说明 |
|---|---|---|
| `ApprovalStore` 协议 | 接口 | 新增 `refresh()` |
| `JsonlApprovalStore` | 实现 | 增量 refresh、拒绝覆盖、重放保留第一条、损坏行策略统一 |
| `ApprovalStoreError` | 新增 | 覆盖拒绝/写失败异常 |
| `JsonlDecisionStore` | 实现 | 新增 `record_finalized()` / 重放恢复 finalized 集合 |
| `Checkpoint` | 行为 | 状态机转移校验、forward 异常 refund、finalize 顺序修正、modify 全量比较 |
| `Runtime.start()` | 行为 | 新增预算清扫与孤儿告警 |
| `AsyncApprovalManager` | 行为 | 查询前 refresh |
| `Server Admin` | 新增端点 | `POST /v1/admin/approvals/{decision_id}/approve|deny` |
| `CLI` | 行为 | 审批前过期校验、覆盖拒绝提示 |
| `pyproject.toml` | 依赖 | 新增 `portalocker`（文件锁） |
| `models.py` | 模型 | `DecisionAlreadyConsumed` 异常（或放 checkpoint 模块） |

---

## 5. 测试计划

### 5.1 跨进程审批可见性（P0-1）

- `tests/test_approval_store.py` 新增：
  - `test_refresh_reads_new_rows`：构造 store A，用第二个 store 实例（模拟 CLI 进程）写 response，A.refresh() 后可 `get_record`；
  - `test_refresh_ignores_existing_response`：同 decision_id 新行被忽略（保留第一条）；
  - `test_refresh_offset_after_truncation`：文件被重建后 refresh 全量重放；
  - `test_refresh_tail_partial_line_warning`：末行半行被忽略且不破坏后续 refresh。
- `tests/test_controller.py` 新增：
  - `test_resume_sees_external_response`：模拟双进程——直接操作文件写 response，`resume_after_approval` 能感知。
- `tests/test_server.py` 新增 Admin 审批 API 测试：approve/deny 成功、deny 缺 comment 拒绝、非审批人拒绝、watcher 被 notify。

### 5.2 预算清扫（P0-2）

- `tests/test_checkpoint.py` 新增：
  - `test_recover_stale_pending_reservation`：构造过期 pending，`recover_stale_reservations` 后 refund + expired + 审计事件；
  - `test_recover_stale_pending_approval`：过期 pending_approval 同样清理；
  - `test_get_pending_reservation_filters_expired`；
  - `test_forward_expired_reservation_no_commit`；
- `tests/test_budget.py`：重放孤儿 reserve 产生告警（不 fail）。

### 5.3 原子性与状态机（P0-3 / R3 / R4 / R8）

- `test_approval_store.py`：`test_record_response_rejects_overwrite`、`test_replay_keeps_first_response`；
- `test_checkpoint.py`：`test_commit_reservation_rejects_terminal_state`、`test_transition_illegal_state_rejected`、`test_finalize_adds_finalized_after_validation`（deny 无 comment 时不烧 decision）；
- `test_budget.py`：`test_refund_writes_reservation_state_first`（模拟崩溃窗口状态检查）。

### 5.4 重启恢复（R2 / R5）

- `test_decision_store.py`：`test_finalized_survives_restart`；
- `test_controller.py`：`test_resume_twice_returns_already_consumed`（第二次不再返回 deny）；
- `test_checkpoint.py`：`test_forward_refunds_on_decision_expired`（R1）。

### 5.5 边界修正（R6 / R7 / R9）

- `test_cli.py`：`test_approve_rejects_expired_decision`、`test_list_marks_expired`；
- `test_checkpoint.py`：`test_finalize_unknown_verdict_rejected`；
- `test_checkpoint.py`：`test_modify_review_compares_values`（键同值不同 → 复核失败）。

---

## 6. 验收标准

- `python -m pytest -q` 全部通过；
- `python -m ruff check src tests` 通过；
- `python -m mypy src/loop_controller` 通过；
- 跨进程审批闭环有端到端测试证明：CLI 写入 → Runtime 可见 → resume 成功；
- 预算清扫有测试证明：过期 reservation 释放额度并写审计；
- `record_response` 拒绝覆盖、重放保留第一条、CLI/Runtime 写路径有跨进程锁；
- `_finalized_decisions` 跨重启恢复，重复 resume 返回 `decision_already_consumed` 而非 deny。

---

## 7. 明确不做

- 完整多进程分布式锁 / SQLite 原子 Store（多 worker 共享 data 目录）——归入 v0.30.0 持久化加固；
- 全部 store 的 fsync 落盘改造——归入 v0.30.0；
- 服务端主动推送审批结果（SSE push）——本版本保持"Agent 轮询/重试可见"，只修复可见性；
- 审批消息通知（邮件/企微/Slack webhook）；
- Egress Gateway / Agent 出口强制控制——远期架构方向；
- Anchor outbox / 批量锚点发布。

---

## 8. 完成后的能力边界

v0.29.0 完成后，可以准确描述为：

> 人工审批结果可在运行中的服务中被感知（无需重启），审批结果不可被静默覆盖，预算预留不会因 Agent 放弃或审批超时而永久泄漏，重启后已消费的审批与决策状态保持一致。

仍不能描述为：

- 支持多 worker/多进程共享同一数据目录的强一致并发（v0.30.0）；
- 审批完成后服务端主动通知 Agent（仍为轮询/重试语义）；
- 断电不丢失审计/预算记录（fsync，v0.30.0）；
- Agent 出口被强制治理（Egress，远期）。
