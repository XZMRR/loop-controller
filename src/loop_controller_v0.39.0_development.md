# v0.39.0：Agent Interaction Governance 协议与运行闭环收敛

**Status**: 开发中  
**Target Version**: `0.39.0`  
**Goal**: 在 v0.38.0 双治理平面基础上，统一 Python、Go、OpenAPI 与运行配置的交互协议，完成 Go Kernel → IIGE 授权、结构化参数、身份绑定、审计和兼容迁移闭环。

---

## 1. 版本定位

v0.38.0 已建立独立 IIGE 的模型、配置、Rego package 和 HTTP 授权入口，但仓库仍存在协议与运行实现漂移：

- Python Bridge 使用 `0.38.0`，Go Kernel 和契约仍使用 `0.37.0`；
- Python IIGE 使用结构化 `arguments`，Go 委托模型仍发送 `arguments_json`；
- Go 授权客户端仍固定调用旧 `/r2/v1/delegations/authorize`；
- 授权回调缺少可验证的 Agent 服务身份；
- Python 与 Go 尚未共同消费同一份 v0.39.0 权威契约；
- interaction 决策已进入审计链，但缺少独立索引和完整专项测试。

v0.39.0 是收敛版本，不增加分布式、多租户、Agent 联邦等新领域能力。

---

## 2. 架构边界

### 2.1 双治理平面保持不变

- `action_kind=tool_call`：只进入 Tool Governance Plane（R2）。
- `action_kind=delegation`：只进入 Agent Interaction Governance Plane（IIGE）。
- IIGE 只负责授权决策，不创建 Task、不执行工具、不签发执行 token。
- Go A2A Kernel 负责 Agent Card、Task 状态机、SSE、取消、查询和 token 签发。

### 2.2 统一入口

保留现有开发者入口：

- `@governed`
- MCP Proxy
- HTTP

入口层必须显式携带 `action_kind`。旧 `__target_agent_id` 隐式控制字段只作为兼容机制，不再作为新协议。

---

## 3. 权威协议

### 3.1 协议版本

v0.39.0 的 A2A/IIGE HTTP JSON 协议统一为：

```text
0.39.0
```

兼容规则：

- 只允许严格 `major.minor.patch`；
- patch 漂移可兼容；
- major/minor 不一致必须 fail-closed；
- 缺失或格式非法必须 fail-closed；
- 请求和响应都必须验证协议版本。

Agent Card 的实现版本与线协议版本语义分离；协议兼容不得依赖 Agent Card `version` 字段。

### 3.2 委托请求

A2A Kernel 入站请求继续使用 `initiator_agent_id`：

```json
{
  "protocol_version": "0.39.0",
  "request_id": "req-001",
  "initiator_agent_id": "agent-a",
  "target_agent_id": "agent-b",
  "tool_name": "analyze_sales",
  "arguments": {"region": "APAC"},
  "session_id": "session-001",
  "task_id": "task-001",
  "risk_level": "high"
}
```

Kernel → IIGE 边界必须显式转换为 `source_agent_id`：

```json
{
  "protocol_version": "0.39.0",
  "request_id": "req-001",
  "source_agent_id": "agent-a",
  "target_agent_id": "agent-b",
  "tool_name": "analyze_sales",
  "arguments": {"region": "APAC"},
  "session_id": "session-001",
  "task_id": "task-001",
  "risk_level": "high"
}
```

`arguments_json` 禁止继续作为正式字段，避免双重编码和策略读取空参数。

### 3.3 授权路径

正式路径：

```text
POST /interaction/v1/delegations/authorize
```

兼容路径：

```text
POST /r2/v1/delegations/authorize
```

Go 客户端必须优先访问正式路径。只有正式路径返回 `404` 时才允许回退旧路径；网络错误、认证失败、5xx 或非法响应不得回退为放行。

---

## 4. 身份与安全

- Go Kernel 调用 IIGE 时必须发送可验证的服务身份。
- Bearer token 中的 Agent 身份必须绑定请求的 `source_agent_id`。
- 身份与 source 不一致返回 `403`。
- 缺失身份、签名无效或过期返回 `401`。
- 共享 API Key 不能单独证明 source Agent 身份，不作为正式委托授权身份方案。
- OPA 不可达、响应异常、未知 verdict、协议不兼容均默认拒绝。
- 审计不得记录 Bearer token、delegation token 或未脱敏敏感参数。

---

## 5. 实施任务

### P39-01 权威契约

- 新建 v0.39.0 OpenAPI 与 contract fixture。
- Python 和 Go 契约测试共同读取同一 fixture。
- `arguments` 必须是 JSON object。
- 增加缺失、非法、patch 漂移和 minor 不兼容用例。

### P39-02 Python Bridge

- 协议版本提升到 `0.39.0`。
- `DelegationRequest.to_dict()` 输出结构化 `arguments`。
- 校验 Go 委托响应协议版本。
- 缺失版本 fail-closed。

### P39-03 Go 模型与 Kernel

- `DelegationRequest.Arguments` 改为结构化 JSON object。
- Kernel 协议版本提升到 `0.39.0`。
- 严格拒绝旧 `arguments_json`。
- 保证嵌套对象、数组、Unicode 参数无损传递。

### P39-04 Go → IIGE 授权客户端

- 正式命名改为 Interaction Authorizer；旧类型可暂时保留兼容别名。
- A2A `initiator_agent_id` 映射为 IIGE `source_agent_id`。
- 优先访问新路径，404 时回退旧路径。
- 支持 Bearer service token。
- 校验响应协议版本并限制响应体大小。

### P39-05 入口路由

- HTTP delegation 只调用 IIGE。
- `@governed` 与 MCP Proxy 对 delegation 显式路由 IIGE。
- 旧 DelegationAuthorizer 降级为 IIGE compatibility wrapper。

### P39-06 Interaction 审计

- 保持现有 JSONL 哈希链作为证据源。
- SQLite 增加 `interaction_audit_events` 独立索引。
- 可按 interaction、source、target、verdict 查询。
- 记录 allow、deny、modify、require_approval 和协议拒绝。

### P39-07 测试闭环

- IIGE 单元测试。
- 新旧 HTTP 路由测试。
- Go Interaction Authorizer 测试。
- Python ↔ Go 共用契约测试。
- 启用真实 Go → Python 授权回调的端到端测试。

---

## 6. 兼容策略

- v0.39.0 不删除 `/r2/v1/delegations/authorize`。
- 旧路由内部与新路由使用同一 IIGE 实现。
- 旧路由返回弃用信号并记录使用情况。
- `arguments_json` 不提供静默转换；调用方必须迁移到 `arguments`。
- 旧协议 major/minor 不兼容时拒绝，不通过兼容层绕过。

---

## 7. 明确不做

- PostgreSQL 或消息队列后端；
- 多实例强一致；
- 跨信任域 Agent 联邦；
- Agent 拍卖、Agent Group；
- 完整多租户隔离；
- OAuth2/OIDC 动态 Agent 注册；
- KMS/HSM；
- 配置热更新。

---

## 8. 完成定义

v0.39.0 只有同时满足以下条件才能完成：

1. Python、Go、OpenAPI、contract fixture 和配置使用统一 `0.39.0` 协议。
2. 委托参数全链路使用结构化 `arguments`，不存在正式 `arguments_json` 写路径。
3. Go Kernel 优先调用 IIGE 新路径，只有 404 才兼容回退旧路径。
4. Go → IIGE 请求携带可验证身份，source identity 绑定测试通过。
5. delegation 不进入 R2 Tool Checkpoint。
6. 请求与响应协议版本均 fail-closed 校验。
7. interaction 审计可独立查询且哈希链验证通过。
8. Python 单元/集成测试、Go race test、OPA check、lint、类型检查全部通过。
9. wheel 安装 smoke 与 Python-Go-OPA 端到端委托验证通过。
