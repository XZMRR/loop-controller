# v0.38.0：独立 Agent Interaction Governance 平面

**Status**: 规划中  
**Target Version**: `0.38.0`  
**Goal**: 将 Agent 交互治理（委托授权、Agent 信任、交互审计）从 Python R2 工具治理平面中独立出来，建立专用的 Agent Interaction Governance Engine（IIGE），与 Go A2A Kernel 形成清晰的治理-执行边界。

---

## 1. 背景与动机

v0.37.0 已实现单实例可靠的 A2A 闭环：

- OpenAPI/JSON Schema 作为权威协议
- Go SQLite 状态层持久化 Task/Message/Event/Idempotency
- 异步 entrypoint、Task 状态机、取消与结果查询
- `ActionKind.DELEGATION` 与 Python R2 委托授权接口
- Go Kernel 调用 Python R2 进行委托授权

但 v0.37.0 的委托授权仍然复用 Python R2 工具治理平面：委托请求以 `action_kind="delegation"` 的形式进入 R2，与本地工具调用共用同一套 OPA 策略、Profile 和审计路径。这带来了以下问题：

1. **治理语义混杂**：工具策略关心参数、资源、敏感路径；委托策略关心目标 Agent 可信性、Agent 能力、跨 Agent 预算、委托深度。两者强行放在同一 Rego 包中，策略难以维护。
2. **Profile 语义不清**：`profiles.yaml` 同时描述“Agent 能用什么工具”和“Agent 能委托给谁”，字段会越发膨胀。
3. **审计视角不独立**：委托授权事件与工具执行事件混在一起，无法独立分析 Agent 交互链路。
4. **演进受限**：未来若要支持 Agent 组信任、委托链、跨 Agent 预算、连接认证等，R2 工具模型无法自然承载。

v0.38.0 的目标是把 Agent 交互治理提升为与工具治理并列的独立平面，同时保留现有的 `@governed` / MCP / HTTP 三套入口不变。

---

## 2. 架构目标

### 2.1 双治理平面 + 执行骨架

```text
┌─────────────────────────────────────────────────────────────┐
│                     用户接入层（入口不变）                      │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐ │
│  │ @governed   │  │ MCP Proxy   │  │ HTTP REST           │ │
│  │ Python SDK  │  │ MCP Server  │  │ /r2/v1/*            │ │
│  └──────┬──────┘  └──────┬──────┘  └──────────┬────────────┘ │
│         │                │                    │             │
│         └────────────────┴────────────────────┘             │
│                          │                                  │
│                   分发层（按 action_kind）                    │
│                          │                                  │
│          ┌───────────────┴───────────────┐                  │
│          ▼                               ▼                  │
│  ┌───────────────┐               ┌──────────────────┐          │
│  │ Tool          │               │ Agent            │          │
│  │ Governance    │               │ Interaction      │          │
│  │ Plane (R2)    │               │ Governance       │          │
│  │               │               │ Plane (IIGE)     │          │
│  │ - 工具白名单  │               │ - Agent 信任     │          │
│  │ - 参数规则    │               │ - 委托授权       │          │
│  │ - 本地/MCP    │               │ - 跨 Agent 预算  │          │
│  │   /HTTP 执行  │               │ - 交互审计       │          │
│  └───────┬───────┘               └────────┬─────────┘          │
│          │                                 │                   │
│          ▼                                 ▼                   │
│  ┌───────────────┐               ┌──────────────────┐          │
│  │ Python        │               │ Go A2A Kernel    │          │
│  │ 执行器        │               │ - Agent Registry │          │
│  │               │               │ - Task 状态机    │          │
│  │               │               │ - A2A entrypoint │          │
│  └───────────────┘               └──────────────────┘          │
└─────────────────────────────────────────────────────────────┘
```

### 2.2 关键设计原则

1. **入口不变**：用户仍然通过 `@governed`、MCP Proxy、HTTP 发起请求，不引入新的“第四种接入方式”。
2. **平面独立**：委托请求的授权、策略、审计由 IIGE 负责，不再进入 R2 工具治理平面。
3. **执行分离**：IIGE 只做授权决策，实际的 Task 创建、路由、状态机由 Go A2A Kernel 执行。
4. **向后兼容**：v0.37.0 的 `/r2/v1/delegations/authorize` 接口保留，但内部转发到 IIGE。
5. **统一审计**：工具治理审计与交互治理审计各自独立，但共享底层存储（R3 SQLite）。

---

## 3. 核心概念

### 3.1 InteractionProfile

描述一个 Agent 在交互平面上的能力和限制，与 Tool Profile 分离。

```yaml
# config/interaction_profiles.yaml
interaction_profiles:
  - profile_id: sales_delegator_v1
    agent_id: agent-a
    capabilities:
      delegate_execution: true
      receive_delegation: true
    delegation_policy:
      allowed_target_agents:
        - agent-b
        - agent-c
      denied_target_agents:
        - external-untrusted-agent
      max_delegation_depth: 2
      require_approval_for:
        - external_communication
      budget:
        max_tasks_per_hour: 100
        max_external_delegations_per_day: 10
```

### 3.2 AgentTrust

描述 Agent 之间的信任关系，类似证书链或白名单。

```yaml
# config/agent_trust.yaml
agent_trust:
  - source_agent_id: agent-a
    target_agent_id: agent-b
    trust_level: full    # full / limited / none
    expires_at: "2027-01-01T00:00:00Z"
    conditions:
      same_domain: true
      mfa_required: false
```

### 3.3 DelegationPolicy

描述某个工具/能力是否允许被委托，以及委托条件。

```yaml
# config/delegation_policies.yaml
delegation_policies:
  - tool_name: analyze_sales
    action_kind: delegation
    allowed: true
    target_agent_id: agent-b
    allowed_args:
      region: ["APAC", "EMEA", "NA"]
```

### 3.4 InteractionProposal

IIGE 的输入模型，类似 R2 的 ActionProposal，但语义面向交互。

```python
class InteractionProposal(BaseModel):
    interaction_id: str
    session_id: str | None
    task_id: str | None
    source_agent_id: str
    target_agent_id: str
    tool_name: str | None
    arguments: dict[str, Any]
    action_kind: Literal["delegation"]
    risk_level: str
    risk_tags: list[str]
    interaction_context: str | None   # 类似于 task_context
```

---

## 4. 接口契约

### 4.1 Python IIGE 内部接口

```python
class InteractionGovernanceEngine:
    async def evaluate(self, proposal: InteractionProposal) -> InteractionDecision:
        """
        评估委托/交互请求，返回：
        - allowed / blocked / require_approval / modify
        - target_entrypoint
        - delegation_token
        - decision_id
        - reason
        - policy_hits
        """
```

### 4.2 HTTP API

新增交互治理专用接口：

```text
POST /interaction/v1/delegations/authorize
POST /interaction/v1/delegations/{decision_id}/approve
GET  /interaction/v1/delegations/{decision_id}
POST /interaction/v1/agent-trust/verify
GET  /interaction/v1/profiles/{agent_id}
```

保留兼容接口：

```text
POST /r2/v1/delegations/authorize  → 内部转发到 /interaction/v1/delegations/authorize
```

### 4.3 Go Kernel → IIGE

Go Kernel 在创建委托 Task 前调用 Python IIGE：

```go
POST /interaction/v1/delegations/authorize
{
  "protocol_version": "0.38.0",
  "source_agent_id": "agent-a",
  "target_agent_id": "agent-b",
  "tool_name": "analyze_sales",
  "arguments": {"region": "APAC"},
  "session_id": "...",
  "task_id": "..."
}
```

返回：

```json
{
  "protocol_version": "0.38.0",
  "decision_id": "...",
  "allowed": true,
  "verdict": "allowed",
  "target_entrypoint": {"type": "http", "url": "..."},
  "delegation_token": "...",
  "reason": "target agent trusted and within budget"
}
```

---

## 5. 任务清单（IG-01 ~ IG-12）

### IG-01: 完成 IIGE 架构设计文档

- 明确 Tool Governance vs Interaction Governance 边界
- 定义 InteractionProfile、AgentTrust、DelegationPolicy、InteractionProposal 模型
- 更新本开发文档为可实施规格

### IG-02: 设计并注册 OpenAPI/JSON Schema v0.38.0

- 新增 `/interaction/v1/*` 路径
- 定义 `InteractionProposal`、`InteractionDecision`、`AgentTrust` schema
- 保持 `/r2/v1/delegations/authorize` 兼容

### IG-03: 实现 `loop_controller/interaction/` 包

- `models.py`: InteractionProposal, InteractionDecision, InteractionResult
- `engine.py`: InteractionGovernanceEngine
- `checkpoint.py`: InteractionCheckpoint（类似 R2 的 Checkpoint，但面向交互）
- `policy_engine.py`: InteractionPolicyEngine（OPA 交互策略）
- `config.py`: 加载 interaction_profiles.yaml / agent_trust.yaml / delegation_policies.yaml

### IG-04: 实现交互治理 Rego 策略包

- 新建 `policies/interaction/default.rego`
- 规则覆盖：目标 Agent 注册、AgentTrust、能力匹配、委托深度、跨 Agent 预算
- 默认 deny（fail-closed）

### IG-05: 从 R2 迁移 `DelegationAuthorizer`

- 将 `src/loop_controller/delegation.py` 的授权逻辑迁移到 IIGE
- R2 不再处理 `action_kind="delegation"`
- 保留 R2 对 delegation 的 fail-closed（未知 action_kind 直接 deny）

### IG-06: 更新 `@governed` 路由

- `@governed` 装饰器根据配置或显式参数判断是 `tool_call` 还是 `delegation`
- `delegation` 请求进入 IIGE
- `tool_call` 请求继续进入 R2

### IG-07: 更新 MCP Proxy 路由

- MCP Proxy 识别委托意图（通过 tool 配置里的 `target_agent_id` + `action_kind="delegation"`）
- 委托请求转发到 IIGE
- 普通工具请求继续转发到 R2

### IG-08: 保留 v0.37.0 兼容接口

- `/r2/v1/delegations/authorize` 继续工作
- 内部实现为调用 IIGE
- 在 v0.38.0 中标记为 deprecated，v0.39.0 可考虑移除

### IG-09: 更新 Go Kernel 授权客户端

- `go/internal/delegation/r2_authorizer.go` 重命名为 `interaction_authorizer.go`
- 请求路径改为 `/interaction/v1/delegations/authorize`
- 保留 `/r2/v1/delegations/authorize` 回退（兼容模式）
- 协议版本 bump 到 `0.38.0`

### IG-10: 交互审计独立存储

- 审计表 `interaction_audit_events` 与 `tool_audit_events` 分离
- 记录字段：interaction_id、source_agent_id、target_agent_id、verdict、policy_hits、target_entrypoint
- 与 R3 审计共享底层 SQLite，但逻辑表分离

### IG-11: 全量测试

- Python：IIGE 单元测试、`test_interaction_authorizer.py`、集成测试
- Go：`go test ./...`
- 协议：`tests/test_a2a_contract.py` 切换到 v0.38.0 fixture
- 集成：`pytest tests/ -m integration`

### IG-12: 发布与文档

- 更新 `src/loop_controller_v0.38.0_development.md` 状态为“已发布”
- 更新 `README.md`、`KNOWN_LIMITATIONS.md`、版本字符串
- 创建 annotated tag `v0.38.0`
- 全量验证：ruff、mypy、pytest、go test、build、wheel smoke

---

## 6. 与 v0.37.0 的兼容性

| v0.37.0 行为 | v0.38.0 行为 | 兼容策略 |
|-------------|-------------|---------|
| `action_kind="delegation"` 进 R2 | 进 IIGE | `/r2/v1/delegations/authorize` 内部转发 |
| `DelegationAuthorizer` 在 `src/loop_controller/delegation.py` | 迁移到 `loop_controller/interaction/` | 旧模块保留 deprecated wrapper |
| Rego 包 `policies/default.rego` 处理 delegation | `policies/interaction/default.rego` 处理 delegation | 旧包对 delegation fail-closed |
| Go 调 `/r2/v1/delegations/authorize` | 优先调 `/interaction/v1/delegations/authorize` | 新路径失败时回退旧路径 |

---

## 7. 完成定义（Definition of Done）

1. `src/loop_controller_v0.38.0_development.md` 定稿并通过 review。
2. `loop_controller/interaction/` 包实现完整，覆盖 IG-03 ~ IG-05。
3. `@governed` 与 MCP Proxy 委托请求正确路由到 IIGE。
4. Go Kernel 优先调用 `/interaction/v1/delegations/authorize`。
5. 默认 Rego 策略对未授权的委托目标 fail-closed。
6. 交互审计与工具审计逻辑分离。
7. 全部 Python/Go/协议/集成测试通过。
8. `uv build` 成功，wheel 安装 smoke 通过。
9. `git diff --check` 无空白/格式问题。
10. README/KNOWN_LIMITATIONS 版本一致性更新。
11. 创建 annotated tag `v0.38.0` 并指向 develop HEAD。
12. 从干净 clone 完成完整复验（可选，建议执行）。

---

## 8. 风险与注意事项

1. **范围控制**：v0.38.0 只做治理平面拆分，不扩展新的 A2A 能力（如委托链、拍卖、Agent 组）。
2. **不要破坏 v0.37.0 测试**：所有 v0.37.0 的委托集成测试在兼容模式下必须继续通过。
3. **协议版本**：A2A 协议版本 bump 到 `0.38.0`，major/minor 不一致时 fail-closed。
4. **配置迁移**：现有 `profiles.yaml` 中涉及 delegation 的字段需要迁移到新的 `interaction_profiles.yaml` / `delegation_policies.yaml`。
