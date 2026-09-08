# v0.46.0：可信单跳 A2A 委托闭环

**Status**: 已完成  
**Target Version**: `0.46.0`  
**Goal**: 在 v0.45.0 已完成状态存储收口的基础上，加固主 Agent 到目标 Agent 的单跳委托主链，建立一致的授权协议、可信控制面身份、任务对象级授权、目标工具治理服务认证及可配置的目标任务自动接收/启动能力。递归委托、权限与预算逐层衰减、委托审批恢复留待后续版本。

---

## 1. 版本定位

v0.45.0 已完成会话与对话上下文 SQLite 化，Python 工具治理层具备统一单机状态事实源。A2A 委托已经拥有 Agent Card、IIGE、任务状态机、委托令牌、防重放、HTTP entrypoint 与目标侧工具治理等组件，但真实组合链路仍存在四项关键缺口：

1. Go 控制面请求身份与 `initiator_agent_id` 未建立可信绑定，任务查询、SSE 和取消缺少对象级授权；
2. Python IIGE 与 Go authorizer 的响应 DTO 不一致，`modify` 的有效参数未稳定进入 token 摘要和目标派发；
3. Go 目标执行器调用 Python R2 时缺少 Bearer 工作负载凭证；
4. 目标 entrypoint 创建任务后依赖外部调用 `accept/start`，缺少可配置的单跳自动执行路径。

v0.46.0 聚焦上述单跳闭环，不扩展新的工具执行器，也不将子 Agent 当作 Harness 工具执行。

---

## 2. 核心原则

1. **身份不可自报**：启用控制面认证时，可信 Bearer principal 必须绑定固定 initiator；请求体不能冒用其他 Agent。
2. **对象级授权**：任务查询、SSE 和取消仅允许任务发起方访问，不把 `task_id` 当成授权凭证。
3. **契约单一语义**：授权响应明确表达 `allow / deny / modify / require_approval`，Go 严格解码 Python 返回结构。
4. **有效参数唯一**：`modify` 通过二次复核后，`effective_args` 必须用于参数摘要、委托 token、entrypoint payload 和目标执行。
5. **下游认证不降级**：Go Kernel 调用 Python 工具治理 API 时携带独立 Bearer token；不得以关闭认证维持生产可用性。
6. **自动执行可配置**：目标任务可配置自动 accept 或自动 accept+start，默认行为保持显式生命周期兼容。
7. **生产 fail-closed**：非 development 模式要求控制面认证配置完整；凭证缺失、身份不匹配、协议异常一律拒绝。

---

## 3. 设计与实现

### P46-01 A2A 控制面身份与对象级授权

Go Kernel 新增控制面认证配置：

- `LC_A2A_CONTROL_TOKEN` / `-control-token`
- `LC_A2A_CONTROL_INITIATOR` / `-control-initiator`

两项必须成对配置。非 development 模式缺失时拒绝启动；development 模式允许关闭认证以兼容本地嵌入和既有测试。

启用后：

- 创建任务和发起委托时，请求 `initiator_agent_id` 必须与认证 principal 绑定的 Agent 一致；
- 查询任务、订阅 SSE 和取消任务时，任务的 `initiator_agent_id` 必须与认证 principal 一致；
- Bearer token 使用常量时间比较；缺失或错误返回 `401`，主体不匹配返回 `403`。

本阶段不改变 Agent Registry 管理接口和普通 message 路由的授权模型；其管理面身份与多 principal token 映射留待后续版本。

### P46-02 IIGE / Go 委托授权契约一致化

`DelegationResponse` 统一增加：

- `verdict`
- `original_args`
- `modified_args`
- `effective_args`

授权响应约束：

- `allow`：`allowed=true`；
- `modify`：`allowed=true` 且 `effective_args` 必须是 JSON object；
- `deny` / `require_approval`：`allowed=false`；
- 未知 verdict、字段类型错误或协议版本不兼容时 fail-closed。

Go Delegator 以 `effective_args` 作为唯一执行参数：

```text
IIGE effective_args
  → arguments SHA-256
  → delegation token
  → entrypoint task payload
  → target R2 tool-call
```

OpenAPI schema、contract fixture、Python bridge 和双方测试同步更新。

### P46-03 目标 R2 工作负载认证

Go `execution.HTTPExecutor` 新增 `BearerToken`，调用 `/v1/govern/tool-call` 时发送：

```http
Authorization: Bearer <LC_EXECUTOR_TOKEN>
```

配置优先级：

1. `-executor-token`
2. `LC_EXECUTOR_TOKEN`
3. `LC_INTERACTION_TOKEN`

独立 executor token 优先；回退 interaction token 仅用于单服务部署兼容。

### P46-04 目标任务自动接收与启动

新增：

- `LC_TARGET_AUTO_ACCEPT` / `-target-auto-accept`
- `LC_TARGET_AUTO_START` / `-target-auto-start`

语义：

- `auto_accept=true`：entrypoint 创建成功后自动 `pending → accepted`；
- `auto_start=true`：隐含自动 accept，并继续 `accepted → running`，启动配置的 `TargetExecutor`；
- 自动接收或启动失败时返回明确错误，不伪造成功状态；
- 未启用时保留显式 `/accept`、`/start` 生命周期。

---

## 4. 测试范围

### Go

- 控制面缺失/错误 Bearer 返回 401；
- initiator 冒用返回 403；
- 发起方可查询、订阅和取消自有任务；其他主体被拒绝；
- 生产模式控制面配置缺失或不成对时拒绝启动；
- Python 授权响应严格解析四种 verdict；
- `modify` 的有效参数用于 token 摘要和 entrypoint 派发；
- Target HTTPExecutor 携带 Bearer token；
- 自动 accept 和自动 start 的状态迁移及执行行为。

### Python

- IIGE endpoint 返回 `verdict` 和 `effective_args`；
- modify 响应暴露 original/modified/effective 三组参数；
- Go bridge 对新增字段进行往返解析；
- 授权与执行异常继续 fail-closed。

---

## 5. 明确不做

- 递归委托 `A → B → C` 的可信父子链推导；
- 委托深度、环路检测和根任务级取消传播；
- 委托能力 scope、权限逐层衰减与预算逐层预留；
- `require_approval` 持久化、人工批准与批准后恢复派发；
- 多 principal / JWT / OIDC 控制面身份系统；
- Agent Registry 管理面的所有权证明与细粒度 RBAC；
- 消息队列、分布式 worker、负载均衡或跨信任域联邦；
- 新增 MCP、HTTP、LocalFunction、Harness 之外的工具执行器。

---

## 6. 完成定义

v0.46.0 只有同时满足以下条件才能完成：

1. 非 development Go Kernel 未配置控制面 token 与 initiator 时拒绝启动；
2. 委托主体不能通过请求体冒用其他 Agent；
3. 任务查询、SSE 和取消具备发起方对象级授权；
4. Python IIGE 与 Go authorizer 的真实响应可以严格互操作；
5. `modify` 后最终执行参数与 token 绑定参数完全一致；
6. 目标 R2 请求在认证开启时可以使用配置的 Bearer token；
7. 目标任务可以按配置自动 accept/start，并正确回写终态；
8. Go build/vet/test、Python 全量测试和 `git diff --check` 全部通过；
9. Go、Python、OpenAPI、contract fixture、config 和测试版本统一为 `0.46.0`。

---

## 7. 验证记录

- P46-01：控制面 Bearer、initiator 绑定及任务查询/SSE/取消对象级授权已实现并通过 Go 测试；非 development 模式要求控制 token 与 initiator 成对配置。
- P46-02：Python IIGE、Go authorizer、Python bridge、OpenAPI 与 contract fixture 已统一 `verdict` 及 original/modified/effective args；`effective_args` 已用于 token 参数摘要与 entrypoint 派发。
- P46-03：目标 `HTTPExecutor` 已支持 Bearer 工作负载凭证，配置优先级为显式 executor token、`LC_EXECUTOR_TOKEN`、interaction token。
- P46-04：目标 entrypoint 已支持可配置的自动 accept 和自动 accept+start；未启用时保留显式生命周期接口。
- 版本统一：Go、Python、`pyproject.toml`、`uv.lock`、OpenAPI、contract fixture、config 与测试均为 `0.46.0`；生成 `contract/a2a_v0.46.0.json` 与 `openapi/a2a_v0.46.0.yaml`，移除旧 v0.45.0 活动契约文件。
- `go build ./...`、`go vet ./...`、`go test ./...`：全部通过。
- `pytest -q`：922 passed, 4 skipped, 1 warning。
- `git diff --check`：通过；VS Code diagnostics：无诊断。
