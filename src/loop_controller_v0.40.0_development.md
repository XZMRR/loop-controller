# v0.40.0：Agent 委托执行生命周期闭环

**Status**: 完成  
**Target Version**: `0.40.0`  
**Goal**: 在 v0.39.0 已完成的 IIGE 授权闭环之上，建立可持久化、可恢复、可取消、可审计的 Agent 委托执行生命周期。

---

## 1. 版本定位

v0.38.0 将 Agent Interaction Governance 从工具治理中独立出来，v0.39.0 统一了 Python、Go、OPA、HTTP、审计与三入口协议。当前链路已能判断“是否允许委托”，但授权后的执行生命周期仍不完整：

- Go Task 状态迁移缺少正式 `accepted → running` API；
- 状态校验与 SQLite 更新不是原子操作；
- `outcome_unknown` 被错误视为不可恢复终态；
- Task 持久化失败可能产生幽灵任务；
- Task 状态与事件写入尚未形成事务一致性；
- SSE 实际发送裸 Task，与 TaskEvent 契约不一致；
- 委托 token 尚未绑定参数摘要且缺少重放防护；
- 发起端取消没有传播到目标 Agent；
- 授权成功后尚未真正派发到目标 entrypoint。

v0.40.0 聚焦单节点与 HTTP Agent 场景下的执行正确性，不引入消息队列、多实例共识或跨域 Agent 联邦。

---

## 2. 核心原则

1. **授权与执行分离**：IIGE 继续只负责授权，Go Kernel 负责执行生命周期。
2. **持久化优先**：Task 未成功持久化时不得签发 token 或返回委托成功。
3. **原子状态迁移**：所有状态更新必须使用 expected-status CAS，禁止先读后无条件更新。
4. **事件可恢复**：状态事件以 SQLite 为事实源，SSE 是可重放投影。
5. **执行强绑定**：token 必须绑定 source、target、tool、task 和参数摘要。
6. **不确定性显式化**：网络结果未知时进入 `outcome_unknown`，不得伪报 completed、failed 或 cancelled。
7. **默认拒绝**：身份、token、状态、协议或持久化异常均 fail-closed。

---

## 3. 权威生命周期

```text
pending
  → accepted
  → cancelled

accepted
  → running
  → cancelled

running
  → completed
  → failed
  → cancelled
  → outcome_unknown

outcome_unknown
  → completed
  → failed
  → cancelled
```

严格最终态只有：

```text
completed | failed | cancelled
```

`outcome_unknown` 表示结果暂时不确定，允许迟到结果回补，不得设置为不可变最终态。

---

## 4. 实施阶段

### P40-01 状态正确性

- 增加 `POST /a2a/v1/entrypoint/tasks/{id}/start`。
- SQLite 状态更新改为 compare-and-set。
- 并发迁移冲突返回 `409 invalid_status_transition`。
- 修正 `outcome_unknown` 的终态和 `completed_at` 语义。
- 新增可靠 Task 创建接口；持久化失败不得继续委托。

### P40-02 状态与事件一致性

- 状态迁移和 TaskEvent append 在同一 SQLite transaction 中提交。
- publisher 只负责提交后的本机 fan-out。
- 所有事件持久化失败必须向调用方返回错误。

### P40-03 SSE 契约

- SSE 发送完整 `TaskEvent`，不再发送裸 Task。
- 使用 `id`、`event`、`data` 三类 SSE 字段。
- 支持 `Last-Event-ID` 重放。
- 查询不存在的 Task 返回 404，不建立空连接。

### P40-04 真实派发

- 新增 `EntrypointClient`。
- 授权和 token 签发后 POST 目标 Agent entrypoint。
- 明确区分发送失败与响应未知。
- 内存 Router 不再承担 Task 派发语义。

### P40-05 取消闭环

- Python `GoKernelBridge` 增加 `cancel_task()`。
- 发起 Kernel 将取消请求转发到目标 entrypoint。
- 目标端连接实际执行句柄。
- 取消结果未知时进入 `outcome_unknown`。

### P40-06 Token 与幂等

- token 增加 `jti`、`iat`、`iss`、`aud` 和 `arguments_sha256`。
- 校验 token header、过期边界和参数摘要。
- entrypoint accept/start/get/results/cancel 使用 task-scoped token。
- 接入现有 IdempotencyStore；同 request ID 同请求返回原结果，不同请求返回 409。
- 非开发模式禁止默认 token secret。

### P40-07 Interaction 执行审计

- 授权、派发、接受、运行、完成、失败、取消和结果未知形成同一 interaction 时间线。
- 审计记录 root/parent interaction ID、Task ID 和 decision ID。
- 不记录原始 delegation token。

---

## 5. API 增量

```text
POST /a2a/v1/entrypoint/tasks/{id}/start
POST /a2a/v1/tasks/{id}/cancel
GET  /a2a/v1/tasks/{id}
GET  /a2a/v1/tasks/{id}/stream
```

后续状态变更接口必须携带 task-scoped delegation token；具体认证头和迁移兼容方式在 P40-06 固化。

---

## 6. 明确不做

- 多实例线性一致；
- PostgreSQL 或外部消息队列；
- 跨信任域 Agent 联邦；
- OAuth2 动态 Agent 注册；
- 分布式工作流编排；
- 自动补偿业务副作用；
- 通用 Agent 执行沙箱重构。

---

## 7. 完成定义

v0.40.0 只有同时满足以下条件才能完成：

1. 正式 HTTP 链路可完成 pending → accepted → running → completed/failed。
2. 并发状态迁移无法绕过状态机。
3. Task 创建失败时不签 token、不返回成功。
4. `outcome_unknown` 可被迟到结果修正且不提前写 completed_at。
5. 状态和事件持久化不存在可观察的不一致窗口。
6. SSE 与 OpenAPI TaskEvent 契约一致并支持断线重放。
7. 委托请求真实到达目标 entrypoint。
8. 取消可传播，无法确认时进入 outcome_unknown。
9. token 绑定参数摘要并具备单次/幂等防重放语义。
10. Python、Go、OpenAPI、contract fixture 版本统一为 `0.40.0`。
11. Python 全量测试、Go 全量与 race 测试、Ruff、Mypy、OPA check 全部通过。

---

## 8. 验证记录

本地（Windows）完成以下验证：

- `go build ./...` / `go vet ./...`：通过。
- `go test ./... -count=1`：全部通过（12 个包）。
- `go test -race ./...`：全部通过（12 个包，MinGW-w64 gcc 16.1.0，CGO 已启用）。
- `opa check --strict policies`：通过（无输出）。
- `uv run pytest -q`：`870 passed, 4 skipped`。
- `uv run ruff check .`：`All checks passed!`。
- `uv run mypy src/loop_controller`：`Success: no issues found in 98 source files`。
- `uv build`：成功产出 `dist/loop_controller-0.40.0.tar.gz` 与 `dist/loop_controller-0.40.0-py3-none-any.whl`。
- `git diff --check`：通过。
