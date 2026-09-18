# v0.54.0：可靠多 Agent 调度与执行编排基线

> **Status**: 开发完成（支持环境发布门禁待执行）
> **Planning Scope**: `v0.54.0`
> **Implementation Baseline**: `v0.53.0`
> **Target Version**: `v0.54.0`
> **Delivery Version**: `v0.54.0`
> **依赖说明**: 复用 v0.53 的 strict workload identity、DelegatedSubject、tenant/target binding、受保护执行出口、ExecutionReceipt、能力协商与审计关联；调度层不得削弱或绕过这些安全不变量。

---

## 1. 版本定位

历史路线图将 v0.54 定义为“多 Agent 平台化”，候选内容包括 Agent Card 能力路由、并行子任务与 DAG、worker 集群与任务队列、预算感知调度、失败重试和组织级 federation。结合 v0.53 实际代码重新核查后，本版本冻结为 **可靠多 Agent 调度**，不一次性实现完整平台或跨组织 federation。

v0.35—v0.39 已提供 Go A2A Kernel、Agent Card、registry、静态/HTTP discovery、Task 状态机、SSE、delegation、预算与 Python bridge；v0.42 已持久化 registry/router/discovery；v0.49 已提供具备 lease、claim token 和 ACK fencing 的审批 dispatch outbox；v0.53 已将 A2A target execution 纳入 strict 安全闭环。这些能力是 v0.54 的实现基线，不重写已正确工作的治理平面。

当前缺口不是“能否创建一个跨 Agent Task”，而是：

- router 仍依赖调用方指定目标 Agent，没有基于能力、健康、容量和安全约束选择候选；
- registry 未区分稳定 spec 与高频 status，缺少 revision、TTL、draining、负载和容量；
- execution handle 与 goroutine 保存在 API 进程内存，进程退出后不能可靠接管；
- Task execution lease 没有 epoch/fencing，过期 worker 仍可能提交旧结果；
- 普通 delegation、审批后 dispatch 和直接 execution 的持久化语义不一致；
- discovery 缺少 source ownership、持续刷新、部分失败和陈旧记录语义；
- SSE 有持久化回放基础，但缺少稳定序号、retention、cursor expiry 和客户端重连合同；
- Go model、Python bridge、OpenAPI 与历史 proto 存在合同漂移。

版本规则：

- v0.54 新增 wire 字段、assignment、attempt/fence、DAG 与调度 API，统一按 `0.54.0` 版本化；
- OpenAPI/JSON contract 是 wire authority；历史 proto 若不恢复生成与合同测试，则明确归档，不再声称 Go model 镜像 proto；
- v0.53 peer 不得静默接收 v0.54 调度字段。滚动升级必须使用显式双版本窗口或先升级服务端、后启用 v0.54 capability；
- `scheduler_fencing_v1`、`durable_assignment_v1` 等能力必须由实际实现和字段语义证明，不能只由版本号推断；
- v0.53 支持环境发布门禁仍是生产 strict 前置条件，不因开始 v0.54 而视为自动完成。

---

## 2. 目标与非目标

### 2.1 目标

1. 根据 tenant、trust domain、required capabilities、协议与安全能力、健康、容量和调度约束确定性选择目标 Agent。
2. 将 Task assignment、dispatch、worker claim、执行续租、结果提交和预算结算建立为持久化闭环。
3. 使用 execution attempt 与单调 fencing token 阻止过期 worker、重复 dispatch 和取消竞态提交旧结果。
4. 支持有界 DAG：依赖满足后调度、失败传播、取消传播、并行宽度和聚合终态可验证。
5. 实现预算、deadline、重试成本和容量感知的 admission、retry 与 failover。
6. 将 Agent spec/status、discovery source ownership、heartbeat/TTL/draining 建立为可并发更新的数据模型。
7. 提供可断线续传的 Task 事件协议以及 queue、lease、attempt、lag、retry、dead-letter 可观测性。
8. 保持 v0.53 strict 身份、tenant、delegation、credential、protected egress 与 receipt 保证不降级。

### 2.2 非目标

- 不实现跨组织公网 federation、全局 registry 联邦或多集群共识；
- 不实现 OAuth/OIDC 动态 Agent 自助注册；
- 不实现通用 BPMN/工作流引擎、任意循环图、补偿 DSL 或人工流程设计器；
- 不承诺 exactly-once 外部副作用，只承诺 durable at-least-once dispatch、fenced result commit 和幂等键传播；
- 不引入 Kafka、Redis、Consul、etcd 或外部数据库作为强制依赖；首期复用 SQLite WAL 与现有 outbox；
- 不在本版本实现跨租户成本计费、拆账或财务结算；预算仍是治理资源上限；
- 不将 Agent 自报 load/health、普通 header、payload 或 process instance 视为可信身份；
- 不重做 v0.52 RBAC、v0.53 Secret/receipt/Protected MCP 模型或内置容器编排器。

---

## 3. 威胁模型与强制不变量

调度平面新增以下威胁：恶意调用方伪造 capability 或目标；未授权主体覆盖 Agent entrypoint；discovery provider 删除其他来源记录；多个 scheduler 超卖同一容量；worker lease 过期后继续执行或提交结果；重试产生重复副作用；取消与启动竞争；跨 tenant 候选泄漏；伪造 DAG 依赖绕过授权、预算或深度；慢 SSE 消费者耗尽资源。

强制不变量：

1. **先过滤安全边界，再评分**：tenant、trust domain、workload、协议、required security capabilities 任一不匹配的 Agent 不得进入候选集。
2. **选择与容量预留原子关联**：不能先读取 load 再异步占位；assignment 必须以事务/CAS reservation 防止超卖。
3. **每次 claim/接管递增 fence**：renew、result、receipt、预算结算、取消确认和 outbox ACK 必须匹配当前 owner、attempt 与 fence。
4. **过期 worker 无写权限**：旧 fence 的结果即使内容和 receipt 合法，也不得改变 Task 或 DAG 状态。
5. **取消优先建立持久化事实**：执行启动必须在可见、可取消的 assignment claim 之后；不得存在“Task 已 cancelled 但未登记 handle 的执行刚启动”窗口。
6. **调度不得扩大委托权限**：有效工具、capability、deadline、depth 和预算是父链、token、profile、tenant policy 与 kernel hard limit 的交集。
7. **DAG 节点逐个治理**：父图获准不代表所有节点自动获准；每个远程委托与工具副作用仍经过既有治理与 strict executor。
8. **重试不等于安全重放**：只有 failure class、deadline、预算、幂等能力和 side-effect policy 均允许时才重试。
9. **注册更新有归属和 revision**：一个 discovery source 只能更新/删除自己管理的记录；API 与其他 provider 记录不可被越权覆盖。
10. **不宣称 exactly-once**：没有下游幂等协议时，网络不确定性必须进入 `outcome_unknown`，不能盲目换 Agent 重试。
11. **tenant 全链一致**：registry、assignment、Task、DAG、token、worker、receipt 与审计 tenant 必须一致。
12. **v0.53 strict 不变量保持有效**：不得回退 interaction token、明文 HTTP、local/default executor、stdio/旧 SSE、Secret global fallback 或未验证 receipt。

---

## 4. 核心模型

### 4.1 AgentSpec 与 AgentStatus

v0.54 将当前单一 Agent Card 分为稳定声明和运行状态。

`AgentSpec` 至少包含：

```text
tenant_id
agent_id
name
description
entrypoint
capabilities
trust_domain
supported_protocol_versions
supported_security_capabilities
expected_workload_id
labels
priority
weight
max_concurrency
schedulable
source_type
source_id
generation
resource_version
created_at
updated_at
```

`AgentStatus` 至少包含：

```text
tenant_id
agent_id
observed_generation
health
current_load
available_capacity
draining
last_seen_at
expires_at
status_revision
reported_by
```

规则：

- `AgentSpec` 的 entrypoint、workload 和 capability 只能由具备 registry 管理权限的主体或可信 discovery source 更新；
- heartbeat 只能更新允许的 status 字段，不得改变 spec；
- `resource_version` 用于 spec CAS，`status_revision` 用于 status CAS；
- 首期保持 `agent_id` 全局唯一，tenant 是强制归属字段；不在本版迁移为 tenant-scoped 主键，避免 token、Task 和外键同时破坏；
- `current_load` 是调度提示，不是身份事实；最终容量以数据库 reservation 为准；
- expired、unhealthy、draining、unschedulable Agent 不接收新 assignment，已运行任务按策略完成、取消或接管。

### 4.2 SchedulingRequest 与候选决策

```text
SchedulingRequest
  request_id
  tenant_id
  task_id
  dag_id / node_id
  required_agent_capabilities
  required_security_capabilities
  required_tools
  trust_domain
  deadline
  budget_envelope
  affinity / anti_affinity
  excluded_agent_ids
  retry_context
```

调度顺序固定为：

1. tenant 与授权范围；
2. trust domain、协议版本、workload 和 security capability；
3. required Agent capability/tool；
4. health、TTL、draining、schedulable；
5. deadline、预算与可用容量 admission；
6. affinity/anti-affinity；
7. failure penalty、load、priority、weight 评分；
8. 以稳定字段做确定性 tie-break；
9. 事务内创建 assignment 并预留容量。

调度输出 `SchedulingDecision`，包含候选摘要、过滤 reason codes、选中 Agent、算法版本和非敏感 score breakdown。不能记录 Secret、token、完整业务参数或证书。

### 4.3 Assignment、Attempt 与 Fence

```text
TaskAssignment
  assignment_id
  tenant_id
  task_id
  agent_id
  state
  attempt
  execution_fence
  lease_owner
  lease_expires_at
  not_before
  deadline
  delivery_id
  idempotency_key
  failure_class
  created_at / updated_at
```

状态建议与公开 Task status 正交：

```text
queued → claimed → dispatched → executing → settled
             ↘ retry_wait → queued
             ↘ dead_letter
             ↘ cancelled
```

公开 Task status 尽量保持 v0.53 enum；scheduler 阶段通过 `assignment_state` 表达，避免立即重建 SQLite `CHECK`。所有 assignment 转换使用 expected state/revision/fence CAS。

### 4.4 有界 DAG

```text
TaskGraph
  dag_id
  tenant_id
  root_task_id
  status
  max_parallelism
  deadline
  budget_envelope
  revision

TaskNode
  node_id
  dag_id
  task_id
  dependencies
  failure_policy
  retry_policy
  budget_limit
  status
```

首期只支持创建时冻结的有向无环图：

- 服务端执行 cycle detection；
- 节点数、边数、深度和并行宽度有硬上限；
- 依赖全部成功才可 admission；
- `fail_fast`、`continue_independent` 两种失败策略；
- 取消根图时按持久化 descendants/edges 传播；
- 节点预算预留和释放必须与父图 envelope 原子关联；
- 不支持运行中任意增删节点、循环、动态 fan-out DSL 和自动补偿事务。

### 4.5 FailureClass 与 RetryPolicy

固定分类至少包括：

```text
pre_dispatch_transient
pre_dispatch_permanent
sent_unacknowledged
remote_rejected
remote_timeout
receipt_invalid
security_violation
cancelled
budget_exhausted
deadline_exceeded
```

仅 `pre_dispatch_transient` 默认允许安全重试。`sent_unacknowledged` 必须进入 `outcome_unknown`，除非目标支持稳定 delivery/idempotency key 和可认证状态查询。`receipt_invalid`、`security_violation` 禁止换 Agent 自动重试，并产生高严重度审计/告警。

RetryPolicy 包含 `max_attempts`、指数退避、jitter、`retryable_failure_classes`、每次尝试预算上限与总 deadline。调度器必须在 claim 前重新检查剩余预算和 deadline。

---

## 5. 持久化队列与 Worker

v0.54 将 API handler 中的内存执行管理迁移为 durable worker loop：

```text
API / Delegation / DAG Controller
  → SQLite transaction: Task + Event + Assignment + Outbox + Budget Reservation
  → Worker claim(lease_owner, attempt, fence)
  → strict target executor(dispatch delivery_id + fence)
  → renew lease
  → verify ExecutionReceipt
  → fenced terminal commit + budget settlement + lifecycle/audit outbox
```

要求：

- 普通 delegation、审批后 delegation 和 DAG node dispatch 统一走 durable assignment/outbox；
- worker 数量、poll interval、lease duration、renew interval 和 per-tenant/per-agent concurrency 可配置；
- `RenewExecutionLease` 必须检查 `RowsAffected()`，0 行即丢失 lease；
- result commit 必须匹配 assignment、attempt、owner、fence 和当前状态；
- recovery 扫描 expired lease，按 failure class 转 `retry_wait` 或 `outcome_unknown`；
- dispatch payload 向支持 v0.54 的 target 传递 `assignment_id`、`delivery_id`、`attempt`、`execution_fence`；
- target 必须按 delivery ID 幂等，并拒绝低于已见 fence 的请求；不支持该能力的 target 只能按受限 compatibility policy 调度；
- dead-letter 不是 terminal success，必须可查询、可审计并需要显式 replay 权限；
- worker graceful shutdown 停止 claim、新 lease 续租到安全点，并对未确认执行保留真实不确定状态。

不再以 API 进程内 `map[taskID]Handle` 作为可靠执行事实。内存 handle 只可作为当前 worker 的取消优化，数据库 assignment/fence 才是权威。

---

## 6. Registry、Discovery 与安全入口

Registry API 必须区分：

- create：不存在才成功；
- update：要求 expected `resource_version`；
- heartbeat/status：只能修改 status；
- drain：停止新 assignment；
- delete：先 tombstone/drain，确认无活动 assignment 后清理。

所有 mutation 接入 v0.52 RBAC 和 v0.53 workload authentication；禁止匿名注册或覆盖 entrypoint。entrypoint 必须通过 scheme、host/CIDR、DNS rebinding 与 metadata/loopback/private network policy；strict 远端只允许 HTTPS+mTLS，并绑定 expected workload/certificate policy。

Discovery 要求：

- 每条记录保存 `source_type/source_id/external_revision`；
- provider 只能管理自身记录；
- provider 同步使用 staging + transaction，失败不得留下半同步状态；
- 一个 provider 暂时失败时保留旧记录并标为 stale，不立即删除；
- 支持周期 refresh、ETag/If-None-Match、响应大小和 Agent 数量上限；
- 多来源同 Agent 冲突按显式优先级或冲突错误处理，不采用“最后写入覆盖”；
- TTL 到期先标 unavailable，再按 retention 清理；
- HTTP discovery strict 下要求 HTTPS、可信 peer、响应 schema 与可选签名验证。

---

## 7. SSE、查询与可观测性

Task events 增加显式 per-task monotonic sequence 和 schema version，不再把 SQLite `rowid` 作为长期协议语义。SSE `id` 是 opaque cursor，合同定义：

- 支持 `Last-Event-ID`；
- cursor 已过 retention 返回稳定 `event_cursor_expired`（HTTP 410）；
- 定期发送 heartbeat comment 和 `retry` 提示；
- 慢消费者超过 event/byte/time lag 上限后断开，不阻塞 publisher；
- Python bridge 保存最后完整事件 ID，断线后带 cursor 自动重连；
- retention/compaction 不删除 terminal Task 的最小可审计摘要；
- write error、client cancel 和 server shutdown 都正确释放 subscriber。

新增指标至少覆盖：

```text
scheduler_candidates_total
scheduler_no_candidate_total
assignments_queued
assignment_claim_latency
execution_attempts_total
lease_renew_failures_total
fence_rejections_total
retry_total
outcome_unknown_total
dead_letter_total
agent_available_capacity
discovery_stale_agents
sse_subscribers
sse_cursor_expired_total
```

指标标签禁止使用 task_id、user_id、Secret 或高基数业务参数；tenant 标签默认关闭或受控聚合。health/readiness 分别报告 scheduler store、worker、queue lag、expired lease recovery、discovery freshness 和 strict target capability。

---

## 8. 数据库迁移与协议

v0.54 在增加 assignment、agent status、event sequence 和幂等 lease 前，引入正式 `schema_migrations`：

- 每个 migration 有单调版本、名称和 checksum；
- migration 在事务和进程级互斥下执行；
- 支持表重建迁移，不再仅依赖 `ALTER TABLE ADD COLUMN`；
- 多实例启动时只有一个实例迁移，其余等待或 fail-closed；
- 迁移失败不得启动 worker；
- 回滚策略优先使用向前兼容旧二进制，不承诺自动 destructive downgrade。

幂等记录增加 owner、lease expiry、attempt/fence 和 tenant/principal/operation scope。同 key 同 hash 回放原始 status/body；同 key 不同 hash 返回 409；过期 processing lease 可 CAS 接管；complete 必须验证 owner/fence 并检查影响行数。

v0.54 OpenAPI/contract 必须覆盖：

- Agent spec/status、CAS header/field、heartbeat/drain；
- scheduling request/decision；
- DAG create/query/cancel；
- assignment claim/renew/result/fence；
- `Idempotency-Key`、`Last-Event-ID`、cursor expiry；
- auth、tenant、workload 和 capability 字段；
- 所有成功和错误 HTTP status/body；
- `additionalProperties: false` 与 Go `DisallowUnknownFields()` 一致。

修复现有漂移：Agent Card `protocol_version/tenant_id`、register 200/201 body、list envelope、delegation 403 body、Python `approval_id`、GoKernelBridge control auth/mTLS。合同测试必须校验真实 handler request/response，而不只做 model round-trip。

---

## 9. 实施任务

### P54-01：合同与关键可靠性缺陷冻结

- [x] 冻结 AgentSpec/AgentStatus、SchedulingRequest/Decision、Assignment/Attempt/Fence、TaskGraph/Node 和 FailureClass。
- [x] 修复 execution start/cancel 竞态、lease renew 未检查影响行数和 descendants 查询列不一致。
- [x] 冻结 at-least-once、outcome_unknown、fenced result commit 与非 exactly-once 边界。
- [x] 建立 v0.53→v0.54 wire/schema 兼容矩阵。

### P54-02：正式 SQLite migration 与幂等 lease

- [x] 实现 schema_migrations、checksum、迁移互斥和失败门禁。
- [x] 新增 agent status、assignment、attempt/fence、event sequence 与必要索引。
- [x] 幂等记录增加 tenant/principal scope、owner、lease、attempt/fence 和完成 CAS。
- [x] 覆盖旧 v0.53 数据库升级、多实例并发启动和失败恢复测试。

### P54-03：Agent Registry 与 Discovery

- [x] spec/status 分离，支持 resource version、heartbeat、TTL、draining 和容量。
- [x] registry create/update/status/drain/delete 接入 RBAC、tenant 与 workload auth。
- [x] discovery 增加 source ownership、事务同步、周期刷新、stale/TTL 和冲突策略。
- [x] HTTP discovery 增加 HTTPS/mTLS、SSRF、防超大响应和 schema/signature 门禁。

### P54-04：能力路由与容量预留

- [x] 实现安全过滤、capability/tool 匹配、health/capacity admission 和确定性评分。
- [x] 在事务内完成目标选择与容量 reservation，防止多 scheduler 超卖。
- [x] 记录可审计 SchedulingDecision 和稳定 reason codes。
- [x] 覆盖无候选、并发竞争、draining、stale、tenant/trust/capability mismatch。

### P54-05：Durable Assignment Queue 与 Worker

- [x] 普通 delegation、approval dispatch 和 DAG node 统一写 durable assignment/outbox。
- [x] production main 启动 worker/dispatcher、恢复 expired lease 并支持优雅关闭。
- [x] claim/renew/result/cancel/settlement 全部校验 attempt、owner 与 fence。
- [x] target executor 传递 delivery ID/fence，并验证 v0.53 strict receipt 后提交终态。

### P54-06：有界 DAG

- [x] 实现 graph/node/edge 持久化、cycle detection、硬上限和幂等创建。
- [x] 依赖完成后原子 admission，遵守 max parallelism、deadline 和预算 reservation。
- [x] 支持 fail_fast、continue_independent、根取消和终态聚合。
- [x] 每个节点分别执行 delegation/tool governance，不继承未经验证的 allow。

### P54-07：预算感知 Retry、Failover 与 Dead-letter

- [x] 实现 FailureClass、RetryPolicy、指数退避、jitter、not_before 和最大尝试次数。
- [x] 重试前复核预算、deadline、delegation scope、Agent health 和幂等能力。
- [x] sent_unacknowledged 默认 outcome_unknown；security/receipt failure 禁止自动重试。
- [x] dead-letter 查询/replay 接入 RBAC、tenant、审计和新的 fence。

### P54-08：可靠 SSE 与查询

- [x] event 使用显式 sequence/schema version，定义 retention 与 cursor expiry。
- [x] SSE 支持 heartbeat、慢消费者断开、write/cancel 清理和 Last-Event-ID。
- [x] Python bridge 保存 cursor、自动重连并避免重复应用事件。
- [x] Task/DAG/assignment 查询提供一致快照和 tenant 隔离。

### P54-09：API、Bridge、可观测性与 readiness

- [x] 同步 Python/Go/OpenAPI/contract 0.54.0，修复已知合同漂移。
- [x] GoKernelBridge 实际接入 control auth 与 mTLS，并正确解析 approval_id 和错误 envelope。
- [x] 增加 scheduler/queue/lease/fence/retry/discovery/SSE 指标与低基数告警。
- [x] strict readiness 对 store、worker、queue lag、recovery 和 target capability fail-closed。

### P54-10：安全、并发、发布与文档收口

- [x] 代码级 race/fault 测试覆盖多 scheduler claim、lease expiry/takeover、旧 fence、取消/启动竞态和同机多实例崩溃模型。
- [x] 代码级安全测试覆盖注册/entrypoint 劫持、SSRF、跨 tenant 候选、capability 约束和 DAG 权限扩大。
- [x] Python/Go 全量、mypy、Ruff、`go vet`/race、OpenAPI/contract 和 migration 自动化门禁已执行。
- [x] package/protocol、README、KNOWN_LIMITATIONS、配置注释、CI 和 strict 静态模板已收口，不宣称 exactly-once 或 federation。
- [ ] 在支持环境执行真实双独立 OS 进程或容器 worker/scheduler 崩溃接管并留存证据。
- [ ] 在支持环境执行 Protected MCP 完整 mTLS 拓扑、Go→Python strict、Docker/Kubernetes CNI/Secret 隔离和 external DeploymentProof 门禁。

---

## 10. 测试与发布门禁

### 10.1 并发与故障注入

- 多个 scheduler 同时竞争单一容量，只能有一个成功 reservation；
- worker claim 后崩溃，lease 到期由新 worker 以更高 fence 接管；旧 worker 的 renew/result/receipt/settlement 全部拒绝；
- cancel 与 dispatch/start 在每个可见时序交错，不能出现 cancelled 后新启动副作用；
- DB busy、事务回滚、进程终止、网络超时、HTTP 非 2xx、响应丢失分别产生正确 FailureClass；
- idempotency owner 崩溃后可接管，同 key 不同 payload 永远冲突；
- approval consume、Task、assignment、预算 reservation 与 outbox 保持原子。

### 10.2 路由与 DAG

- tenant/trust/workload/security capability 在评分前过滤；
- capacity、priority、weight、load 和 tie-break 在固定输入下结果确定；
- stale/unhealthy/draining Agent 不接收新任务；
- DAG cycle、超深、超宽、超节点数、跨 tenant edge 均拒绝；
- 节点依赖、并行宽度、失败传播、根取消和预算释放正确；
- 图或父 Task 不能扩大子节点 token、工具、capability、deadline、depth 和预算。

### 10.3 安全与协议

- 未授权 Agent register/update/heartbeat/drain/delete 全部拒绝并审计；
- entrypoint 与 discovery 覆盖 loopback、metadata、私网、DNS rebinding、明文 HTTP 和超大响应负向测试；
- v0.53 peer 不接受 v0.54 字段且不静默降级；双版本窗口只开放明确兼容路径；
- target 不支持 fence/idempotency 时不得获得可自动重试的副作用任务；
- receipt、workload、tenant、assignment、attempt、fence 或 result hash 任一错配不得 completed；
- OpenAPI validator 覆盖真实 handler 的 status、headers、request 和 response。

### 10.4 SSE 与迁移

- 断线后从最后完整 cursor 续传，不丢事件、不重复应用；
- cursor 过期返回 410，慢消费者被有界断开且不阻塞其他订阅者；
- v0.53 数据库升级后 Task、Agent、事件、预算和审计保持一致；
- 两个实例并发 migration 只有一个执行，checksum 不一致 fail-closed；
- migration 中途失败可安全重试，不启动 scheduler worker。

### 10.5 发布验证

- Python 全量测试、mypy、Ruff、Go 全量测试、`go vet`、`go test -race` 全部通过；
- 至少执行一次真实双 worker/双 scheduler 崩溃接管测试；
- 在支持环境执行 v0.53 strict mTLS、Protected MCP、Go→Python、容器/CNI 防绕过门禁；
- 保存 migration、调度决策、fence rejection、重试、dead-letter、DAG 和 SSE 证据；
- 没有支持环境证据时，不得把“静态/单进程测试通过”描述为生产可靠调度完成。

---

## 11. 迁移与回滚

建议迁移顺序：

1. 部署包含 migration runner 但 scheduler 未启用的 v0.54 服务端；
2. 升级数据库并验证旧 v0.53 Task/Agent/event 读取；
3. 启用 Agent spec/status 与 heartbeat，保持显式目标路由；
4. 启用 durable assignment worker，但暂不自动 failover；
5. 目标 Agent 全部支持 delivery ID/fence 后启用自动 retry/failover；
6. 启用 capability routing，再启用 DAG；
7. 完成支持环境 strict 和多实例故障门禁后冻结 Delivery Version。

回滚原则：

- 不做 destructive schema downgrade；优先回滚到能读取新增列的向前兼容二进制；
- 停止新 claim 后等待或 fencing 当前 worker，再切换 scheduler；
- 已分配 Task 保留 assignment/attempt/fence，不转换为无 fence 的 v0.53 执行；
- 无法确认下游是否执行的任务保持 `outcome_unknown`，不得回滚时自动重放；
- 回滚不得关闭 v0.53 strict workload、tenant、protected egress、Secret 或 receipt 门禁。

---

## 12. 总体 DoD

- [x] Agent spec/status、source ownership、revision、TTL、draining 与容量模型完成并有代码级并发验证。
- [x] 路由按 tenant/trust/capability/security/health/capacity 过滤并原子 reservation。
- [x] dispatch 使用 durable assignment/outbox，不依赖 API 进程内 handle 作为权威。
- [x] attempt/fence 覆盖 claim、renew、dispatch、result、receipt、cancel、settlement 与 ACK；代码级测试拒绝旧 worker 写入。
- [x] DAG 有界、无环、逐节点治理，依赖/失败/取消/预算/并行语义已自动化验证。
- [x] retry/failover 遵守 FailureClass、幂等能力、deadline 与预算，并保留 `outcome_unknown`。
- [x] discovery 持续刷新、部分失败、stale/TTL 和来源冲突具有确定语义。
- [x] SSE 可恢复，有 retention/cursor expiry/慢消费者边界，Python bridge 自动续传。
- [x] 正式 migration 和幂等 lease 通过多实例、失败恢复与重复启动的代码级测试。
- [x] registry/discovery/worker/DAG/dead-letter API 接入身份、RBAC、tenant 与审计。
- [x] Python、Go、OpenAPI、contract 和 package version 同步为 0.54.0，无已知合同漂移。
- [x] 代码级 full/race/fault/security/migration/protocol 门禁通过。
- [x] README 与 KNOWN_LIMITATIONS 准确说明 at-least-once、SQLite 范围、无 federation 和环境限制。
- [ ] 真实双独立 OS 进程或容器 worker/scheduler 崩溃接管通过并留存证据。
- [ ] v0.53 strict 的 Protected MCP 完整拓扑与 Go→Python strict 支持环境门禁通过并留存证据。
- [ ] Docker/Kubernetes CNI/NetworkPolicy、Secret 隔离及 external DeploymentProof 支持环境门禁通过并留存证据。

---

## 13. 预期边界声明

开发完成后至少保留以下诚实边界：

1. 首期 durable scheduler 基于 SQLite WAL，适合单区域、小到中等规模部署；不宣称跨区域共识或无限水平扩展。
2. 外部副作用只能达到 at-least-once + 幂等/fencing 协作保证；下游不支持幂等时不能承诺 exactly-once。
3. Agent load/health 是调度信号，不是可信安全身份；安全身份继续来自 v0.53 workload authentication。
4. DAG 是有界静态 DAG，不是通用工作流或补偿事务引擎。
5. federation、动态 Agent 自助注册、多集群 registry 与全局公平调度不在 v0.54 范围。
6. v0.53 支持环境 strict 门禁仍须在实际部署环境通过，v0.54 调度测试不能替代网络和 Secret 隔离证据。
