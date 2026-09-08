# v0.48.0：委托审批持久化与可靠恢复派发

**Status**: 已完成  
**Target Version**: `0.48.0`  
**Goal**: 将 IIGE 的 `require_approval` 从“拒绝式结果”升级为可持久化、可审计、可过期、只能消费一次并能可靠恢复派发的 Agent 委托审批状态机。

---

## 1. 版本定位

v0.47.0 已完成可信递归委托、scope 和预算逐层衰减、期限收缩、取消传播及幂等链路审计。本版本聚焦高风险委托的人机协同授权，不扩展新的 Agent 调度或工具执行能力。

当前 `require_approval` 等同于 `allowed=false`：不会创建审批记录、Task 或可恢复派发。v0.48.0 必须将其建模为独立审批对象，审批通过后重新校验动态安全条件，并以单次原子消费创建 Task 和持久化派发任务。

## 2. 核心不变量

1. `require_approval` 与 `deny` 是不同业务结果；前者返回持久化审批对象。
2. 审批快照绑定 request/decision、可信谱系、有效参数、scope、预算与期限。
3. 审批身份来自认证 principal，不接受请求体自报 approver。
4. `approve/reject/expire/cancel/consume` 使用 SQLite CAS；并发操作只能有一个获胜。
5. 一张审批最多兑换一个 Task；重复消费返回同一 Task，不重复预留预算或派发。
6. 批准不等于立即执行；消费前必须重新验证身份、父任务、吊销、策略、scope、预算和期限。
7. 审批后的策略只能保持或收紧权限，不能扩大审批快照。
8. 审批消费、父预算预留、Task 创建、token 持久化及 dispatch outbox 必须同事务提交。
9. 派发采用至少一次投递加目标端幂等，不能把网络成功响应当作唯一事实。
10. 审批已消费后取消应进入 Task 取消流程，不得改写审批历史。

## 3. 实施范围

### P48-01 委托审批事实源

新增 SQLite `delegation_approvals`，状态为：

```text
pending → approved → consumed
       ├→ rejected
       ├→ expired
       └→ cancelled
approved ├→ expired
         └→ cancelled
```

保存：

- `approval_id`、唯一 `request_id`、唯一 `decision_id`；
- source/target/session 和 root/parent/depth；
- 加密或受保护的 effective args 及完整 request hash；
- effective scope、allow_redelegation、预算和 task deadline；
- approval expiry、认证 approver、reason、时间戳、task_id 和 CAS version。

相同 request ID 与相同 hash 返回已有审批；相同 ID 不同 hash fail-closed。

### P48-02 审批 API 与对象级授权

新增：

```text
GET  /a2a/v1/delegation-approvals/{approval_id}
POST /a2a/v1/delegation-approvals/{approval_id}/approve
POST /a2a/v1/delegation-approvals/{approval_id}/reject
POST /a2a/v1/delegation-approvals/{approval_id}/cancel
```

- 发起方可查询和取消自己的待审批委托；
- 审批与拒绝要求独立审批 principal/credential；
- 所有操作记录认证 principal；
- 缺失身份返回 401，无对象权限返回 403。

### P48-03 批准后重新评估和原子消费

批准后由恢复协调器重新调用 IIGE，并重新检查：

- source/target 身份和 Agent Card；
- 父任务状态、再委托许可、谱系、环路与深度；
- 动态 trust/revocation/profile/OPA；
- scope 不得超过审批快照；
- 父预算与 deadline 仍有效。

重新评估结果：

- `allow/modify`：只允许收紧，进入原子消费；
- `deny`：不创建 Task，审批保持已决并记录恢复失败；
- 再次 `require_approval`：不得递归创建新审批，保持待处理并给出明确原因。

原子消费事务包括审批 CAS、预算预留、Task/token/event、dispatch outbox 和审计 outbox。

### P48-04 可靠派发 Outbox

新增委托派发 outbox：

- 稳定 `delivery_id=delegation-dispatch:{approval_id}`；
- lease + claim token fencing；
- 指数退避重试；
- ack/fail 检查 claim token 与受影响行数；
- 目标 entrypoint 以 delivery/request/token 绑定实现幂等；
- 已取消或过期 Task 不再派发。

### P48-05 过期、防重放与审计

- 定时扫描 `pending/approved` 且已过期审批并逐条 CAS 为 `expired`；
- 审批期限与 Task deadline 分离，消费时同时检查；
- approve/reject/cancel/consume 重复相同请求幂等，不同内容冲突；
- 审批审计记录 created/approved/rejected/expired/cancelled/consumed/dispatch retry/delivered；
- 不记录明文敏感参数，只记录摘要和经过掩码的必要元数据。

## 4. 明确不做

- OIDC/JWKS、多租户 RBAC 和跨组织审批联邦；
- 多人会签、法定人数、审批链编排；
- 审批 UI；
- 动态 Agent 调度、消息队列或 DAG 工作流；
- PostgreSQL 和多节点共识；
- 普通工具审批存储迁移。

## 5. 完成定义

1. `require_approval` 返回 202 和稳定审批 ID，而不是普通拒绝；
2. 重启后审批仍可查询、审批并恢复；
3. 非审批 principal 无法批准/拒绝，其他 Agent 无法读取或取消；
4. approve/reject/expire/cancel 并发时只有一个状态迁移成功；
5. 批准后策略、父任务、scope、预算或期限变化会在恢复前被重新发现；
6. 一张审批并发消费只创建一个 Task、一次预算预留和一条逻辑派发；
7. 远端成功而本地 ack 丢失时可幂等重投；
8. 审批过期、防重放和取消语义经过专项测试；
9. OpenAPI、contract fixture、Go/Python DTO 与审计字段一致；
10. Go build/vet/test、Python 全量测试和 `git diff --check` 全部通过；
11. 活动版本统一为 `0.48.0`。

## 6. 验证记录

- 审批事实源：SQLite `delegation_approvals`、CAS 状态机、request hash 幂等与过期扫描已实现。
- 审批 API：查询、批准、拒绝、取消已实现；initiator 对象级授权与独立 approver Bearer/principal 已接入。
- 恢复执行：批准后重新调用 IIGE，重新校验 Agent、谱系、scope、预算和期限；只允许保持或收紧审批快照。
- 原子消费：审批消费、父预算预留、Task、delegation token、Task event、dispatch outbox 和 audit outbox 在同一事务提交。
- 可靠派发：稳定 delivery ID、lease、claim-token fencing、指数退避及目标端幂等已实现；lifecycle outbox 同步加固 fencing。
- 审批安全语义：approve/reject/cancel/expire 并发 CAS、防重放、已消费审批取消转 Task 取消均已覆盖测试。
- 审计与契约：审批 created/approved/rejected/cancelled/expired/consumed 进入可靠审计 outbox；明文参数不进入公开 DTO 或审计；OpenAPI、contract fixture 和 Go/Python DTO 已统一。
- 版本：所有活动代码、配置、包、OpenAPI、fixture 和测试统一为 `0.48.0`。
- `go build ./...`、`go vet ./...`、`go test ./...`：全部通过。
- `pytest -q`：932 passed, 4 skipped, 1 warning。
- `git diff --check`：通过；VS Code diagnostics：无诊断。
