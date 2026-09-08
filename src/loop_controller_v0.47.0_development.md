# v0.47.0：可信递归委托、权限与预算衰减

**Status**: 已完成  
**Target Version**: `0.47.0`  
**Goal**: 在 v0.46.0 可信单跳 A2A 闭环上，完成 `A → B → C` 递归委托的可信父子链、再委托授权、权限范围衰减、父预算原子预留、期限收缩、取消传播与链路审计。

---

## 1. 版本定位

v0.46.0 已完成单跳委托的控制面身份绑定、对象级任务授权、Python/Go 授权契约、目标 R2 服务认证和可配置自动 accept/start。本版本不新增工具执行器，重点是让子 Agent 继续委托时仍满足：每一跳重新授权、链路事实由 Kernel 推导、权限和预算只能收缩、期限不能延长、取消和审计可以沿整棵任务树传播。

## 2. 安全不变量

1. `child.initiator_agent_id == parent.target_agent_id`。
2. `child.root_task_id`、`parent_task_id`、`delegation_depth` 由 Kernel 从持久化父任务推导，调用方不能自报覆盖。
3. 父任务必须有效、未处于终态，并明确授予 `allow_redelegation`。
4. 目标 Agent 不能重复出现在祖先 Agent 链中，委托深度不能超过可信策略上限。
5. 子级有效 scope 必须是父级有效 scope、发起 Agent 权限、目标 Agent 能力、请求 scope 和企业策略的交集。
6. 子任务预算必须从父任务剩余额度原子预留，任何并发路径都不能超额分配。
7. `child.deadline <= parent.deadline`；授权、派发、accept、start 和执行前均需拒绝已过期任务。
8. scope、预算、期限和谱系必须绑定任务及委托 token，并在目标工具 R2 再次强制验证。
9. 父任务取消必须触达所有非终态后代；无法确认正在执行任务的副作用时使用 `outcome_unknown`。
10. 生命周期 outbox 使用稳定事件 ID，实现接收端幂等审计。

## 3. 实施范围

### P47-01 可信父子链与再委托证明

- 扩展 Python Bridge、Controller、OpenAPI 和 contract，使子 Agent 可提交 `parent_task_id`、子预算和期限请求。
- Kernel 在 IIGE 授权前读取父任务，推导 root、parent、depth 和祖先链。
- 验证 source/parent target 连续性、父任务状态、父 token/任务再委托授权及祖先环路。
- 移除可绕过谱系校验的已有 task 分支；已有 task 必须与当前请求完整绑定。
- 目标 entrypoint 落库时保留可信 task 谱系。
- 深度上限以确定性硬上限和 IIGE profile 上限共同收紧。

### P47-02 权限范围逐层衰减

- 委托请求、Task、token 和执行上下文携带 `allowed_tools`、`allowed_capabilities` 与 `allow_redelegation`。
- 每一跳计算父 scope 与当前请求/Agent 能力的交集，不允许 `modify` 扩权。
- 目标 Python R2 同时检查目标 Agent Profile 与委托 scope。
- scope 外工具调用 fail-closed，并生成可关联审计。

### P47-03 父预算原子预留和结算

- 子任务创建与父任务预算预留在同一 SQLite 事务提交。
- 多个并发子任务不得超过父任务剩余预算。
- 创建或派发失败释放预留；明确终态按幂等规则结算或退款；`outcome_unknown` 不提前退款。
- 明确预算字段语义：额度信封、预留额、实际消费额分别建模，避免把成功任务误当作全额退款。

### P47-04 期限、取消传播与审计

- 子期限由父期限与请求期限取更早值；执行 context 使用该 deadline。
- 根/父任务取消遍历后代：未运行任务直接取消，运行中任务调用实际 cancel，无法确认则置 `outcome_unknown`。
- 每个状态变化继续通过事务 outbox 发送，使用稳定 event ID 在 Python 审计接收端去重。
- 审计记录 root/parent/current task、depth、source/target、effective scope、预算、期限和 decision ID。

## 4. 明确不做

- 委托 `require_approval` 的持久化审批与恢复派发；
- JWT/OIDC/JWKS 和跨组织身份联邦；
- 动态 Agent 发现、竞价、负载均衡或消息队列调度；
- DAG 并行编排和补偿事务；
- PostgreSQL 或多节点共识；
- 新的工具执行器。

## 5. 完成定义

必须通过以下端到端场景：

1. `A → B → C` 正常委托，可信 root/parent/depth 在两端持久化并进入审计；
2. 未获再委托权限、伪造 parent/root/depth、source 与 parent target 不一致时拒绝；
3. `C → A/B` 环路和超过最大深度时拒绝；
4. 子 Agent 请求父级未下放工具或能力时被裁剪/拒绝，目标 R2 拒绝 scope 外工具；
5. 并发子任务预算预留不超额，创建/派发失败可幂等回退，`outcome_unknown` 不退款；
6. 子期限不晚于父期限，过期任务不能启动执行；
7. 根任务取消向所有后代传播，运行中且无法确认者进入 `outcome_unknown`；
8. 重复 lifecycle 通知不会产生重复审计事件；
9. Go build/vet/test、Python 全量测试和 `git diff --check` 全部通过；
10. 活动协议、包、配置和测试版本统一为 `0.47.0`。

## 6. 验证记录

- P47-01：正式 Python/Controller/OpenAPI 父任务入口、授权前可信谱系推导、IIGE 可信深度、父 token 再委托证明、祖先环路检查、目标侧谱系持久化及既有 Task 完整绑定已完成。
- P47-02：工具与能力 scope 已写入 Task/token/entrypoint；每一跳与父 scope 取交集，目标 Python R2 对缺失或越界 scope fail-closed。
- P47-03：子任务创建与父预算预留使用同一 SQLite 事务；并发超额被拒绝，明确未派发失败退款，实际消费幂等结算，`outcome_unknown` 保留预留。
- P47-04：期限逐层收缩并用于 accept/start/执行 context；父任务取消遍历后代并调用真实取消路径；不确定副作用进入 `outcome_unknown`；生命周期审计使用稳定事件 ID 并保留谱系、scope、预算和期限元数据。
- 契约与版本：Go、Python、包、配置、OpenAPI、contract fixture 及测试均统一为 `0.47.0`，canonical roundtrip 覆盖新增谱系、预算和期限字段。
- `go build ./...`、`go vet ./...`、`go test ./...`：全部通过。
- `pytest -q`：931 passed, 4 skipped, 1 warning。
- `git diff --check`：通过；VS Code diagnostics：无诊断。
