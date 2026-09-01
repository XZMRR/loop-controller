# v0.35.0 交互治理层启动（一）：Go 内核骨架与 A2A 最小协议

> 一句话目标：**在 Python 工具治理层（R1→R2→R3）之外，新增一个独立的 Go 交互治理内核，负责 Agent 与 Agent 之间的横向交互治理（A2A），输出分层职责文档并在 v0.35.0 内完成最小可运行骨架。**
>
> 范围限定：本版本只搭骨架、定协议、跑通桥接；完整 A2A 会话状态机、跨 Agent 审批委托、分布式发现等进入 v0.36.0/v0.37.0。

- 状态：**开发中/骨架已完成**
- 前置版本：v0.34.0 执行器与审计耐久性
- 版本性质：新增治理层 / 跨 Agent 交互治理第一阶段
- 核心范围：
  - ✅ 输出 Python 工具治理层与 Go 交互治理层的分层职责文档；
  - ✅ 定义 Loop Controller A2A 最小协议（Agent Card、Task、Message、Part）；
  - ✅ 搭建 Go 交互治理内核最小可运行骨架（Agent Registry、Task Manager、Message Router、Delegation Manager）；
  - ✅ 实现 Python R2 与 Go 内核的最小 HTTP/JSON 桥接；
  - ✅ 新增 Go 单元测试与 Python 桥接测试。
- 验证目标：
  - ✅ `go test ./...` 在 `go/` 目录下全绿；
  - ✅ `pytest tests/test_go_kernel_bridge.py -q` 通过；
  - ✅ `pytest tests/ -m "not integration" -q` 保持全绿（735 passed）；
  - ✅ `python -m ruff check src tests` 通过。

---

## 1. 背景

v0.33.0/v0.34.0 把 Python 工具治理层跑稳：单 Agent 的每次工具调用都经过「申报 → 策略判定 → 审批 → 执行前复查 → 授权转发 → 审计」的闭环。但现实中的企业 Agent 系统往往不是单 Agent 在跑，而是多 Agent 协作：

- 一个「规划 Agent」把子任务委托给「执行 Agent」；
- 一个「审核 Agent」在「执行 Agent」调用高危工具前要求二次确认；
- 多个「领域 Agent」通过 A2A 协议发现彼此并协同完成复杂任务。

这些 Agent 之间的横向交互如果缺少治理，会出现新的风险：

- Agent A 未经治理直接把用户上下文泄露给 Agent B；
- Agent B 替 Agent A 执行高危动作，但审计链断在 Agent 边界；
- 委托任务完成后，权限、预算、会话状态没有闭环回收。

因此需要在现有 Python 工具治理层之上，新增一个专门负责 **Agent 间交互治理** 的层。这个层需要：

1. **高性能、低延迟**：Agent 间消息路由是高频路径，Go 比 Python 更适合做这件事；
2. **强类型、易部署**：Go 静态编译，适合作为独立 sidecar；
3. **与 Python 层解耦**：Go 内核只负责 Agent 间交互治理，不替代工具调用治理；
4. **可扩展**：未来支持 A2A、ANP、MCP 等跨 Agent 协议。

---

## 2. 分层职责

```
┌─────────────────────────────────────────────────────────────┐
│                    Agent A  (R1)                           │
│         规划 → 决定调用工具 / 委托给其他 Agent                │
└──────────────┬────────────────────────────┬─────────────────┘
               │                            │
               │ 1. 工具调用治理              │ 2. Agent 间交互治理
               │    (Python R2)             │    (Go 内核)
               ▼                            ▼
┌────────────────────────┐      ┌─────────────────────────────┐
│   Python 工具治理层      │      │   Go 交互治理内核             │
│  - LoopController        │      │  - Agent Registry             │
│  - Checkpoint            │      │  - Task Manager               │
│  - PolicyEngine (OPA)    │      │  - Message Router               │
│  - ExecutorRegistry      │      │  - Delegation Manager           │
│  - AuditStore (R3)       │      │                               │
└──────────────┬───────────┘      └──────────────┬──────────────┘
               │                                  │
               ▼                                  ▼
        工具执行 (MCP/HTTP/Harness)         其他 Agent / A2A 网络
```

### 2.1 Python 工具治理层（不变）

继续负责：

- 单 Agent 的工具调用决策（allow / deny / modify / require_approval）；
- 工具执行前的预算、吊销、审批、风险状态检查；
- 工具执行与审计记录。

**不进入 Go 内核**。

### 2.2 Go 交互治理内核（新增）

负责：

- **Agent 注册与发现**：维护 Agent Card（能力、入口、身份、信任域）；
- **任务管理**：创建、查询、结束 Agent 间交互 Task；
- **消息路由**：在已注册 Agent 之间路由 A2A Message；
- **委托治理**：记录 Agent A → Agent B 的委托关系，确保权限不越界；
- **与 Python R2 桥接**：当 Agent 需要把动作委托给另一个 Agent 时，Python 层把委托请求发给 Go 内核，Go 内核返回是否允许、目标 Agent 入口、治理上下文。

**不执行具体工具**，只决定「能不能交互、跟谁交互、交互边界是什么」。

---

## 3. v0.35.0 范围

### 3.1 纳入本版本

| 编号 | 内容 | 文件位置 |
|---|---|---|
| A2A-1 | 输出分层职责与 A2A 最小协议文档 | `src/loop_controller_v0.35.0_development.md` |
| A2A-2 | 定义 A2A 最小协议 proto / JSON 模型 | `proto/loop_controller/a2a/v1/a2a.proto` |
| A2A-3 | 搭建 Go 模块骨架 | `go/` |
| A2A-4 | 实现 Agent Registry | `go/internal/registry/` |
| A2A-5 | 实现 Task Manager | `go/internal/task/` |
| A2A-6 | 实现 Message Router | `go/internal/router/` |
| A2A-7 | 实现 Delegation Manager | `go/internal/delegation/` |
| A2A-8 | 实现 Go HTTP/JSON API | `go/cmd/kernel/main.go` |
| A2A-9 | 实现 Python 桥接客户端 | `src/loop_controller/go_kernel_bridge.py` |
| A2A-10 | 新增 Go 单元测试 | `go/internal/*/*_test.go` |
| A2A-11 | 新增 Python 桥接测试 | `tests/test_go_kernel_bridge.py` |

### 3.2 明确不纳入

| 编号 | 内容 | 原因 |
|---|---|---|
| A2A-N1 | 完整 A2A Agent Cards 自动发现 | v0.36.0 实现 |
| A2A-N2 | 跨 Agent 审批委托 UI / CLI | v0.36.0 实现 |
| A2A-N3 | 分布式成员发现与共识 | v0.37.0+ 实现 |
| A2A-N4 | gRPC 传输层 | v0.36.0 评估是否引入；v0.35.0 先用 HTTP/JSON 降低依赖 |
| A2A-N5 | 与现有 `@governed` 装饰器深度融合 | v0.36.0 实现；v0.35.0 只提供桥接 API |

---

## 4. A2A 最小协议

参考 Google A2A 协议概念，但做最小子集：

### 4.1 Agent Card

```json
{
  "agent_id": "executor-agent-01",
  "name": "执行 Agent",
  "description": "负责调用外部工具的 Agent",
  "entrypoint": {
    "type": "http",
    "url": "http://executor-agent:8080/a2a"
  },
  "capabilities": ["delegate_execution"],
  "trust_domain": "loop-controller.local",
  "version": "0.35.0"
}
```

### 4.2 Task

```json
{
  "task_id": "task-uuid",
  "session_id": "session-uuid",
  "initiator_agent_id": "planner-agent-01",
  "target_agent_id": "executor-agent-01",
  "status": "pending",
  "created_at": "2026-08-31T12:00:00Z",
  "updated_at": "2026-08-31T12:00:00Z"
}
```

### 4.3 Message / Part

```json
{
  "message_id": "msg-uuid",
  "task_id": "task-uuid",
  "from_agent_id": "planner-agent-01",
  "to_agent_id": "executor-agent-01",
  "role": "user",
  "parts": [
    {
      "type": "text",
      "text": "请帮用户查询本月销售额"
    }
  ],
  "timestamp": "2026-08-31T12:00:00Z"
}
```

### 4.4 Python 桥接请求

当 Python R2 判定一个动作需要委托给外部 Agent 时，向 Go 内核发送：

```json
{
  "request_id": "req-uuid",
  "initiator_agent_id": "planner-agent-01",
  "target_agent_id": "executor-agent-01",
  "tool_name": "query_sales",
  "arguments": {"month": "2026-08"},
  "session_id": "session-uuid",
  "task_id": "task-uuid",
  "risk_level": "critical"
}
```

Go 内核返回：

```json
{
  "allowed": true,
  "task_id": "task-uuid",
  "target_entrypoint": "http://executor-agent:8080/a2a",
  "delegation_token": "jwt-or-mac",
  "reason": "target agent trusted and capable"
}
```

---

## 5. Go 模块结构

```
go/
├── go.mod
├── cmd/
│   └── kernel/
│       └── main.go          # HTTP 服务入口
├── internal/
│   ├── registry/
│   │   ├── agent.go         # Agent Card 与注册表
│   │   └── registry_test.go
│   ├── task/
│   │   ├── task.go          # Task 生命周期
│   │   └── task_test.go
│   ├── router/
│   │   ├── router.go        # 消息路由
│   │   └── router_test.go
│   ├── delegation/
│   │   ├── delegation.go    # 委托决策
│   │   └── delegation_test.go
│   ├── api/
│   │   ├── handlers.go      # HTTP handlers
│   │   └── api_test.go
│   └── models/
│       └── models.go        # 共享 JSON 模型
└── README.md
```

---

## 6. Python 桥接层

`src/loop_controller/go_kernel_bridge.py` 提供：

```python
class GoKernelBridge:
    async def register_agent(self, card: AgentCard) -> bool: ...
    async def request_delegation(self, req: DelegationRequest) -> DelegationResponse: ...
    async def route_message(self, msg: A2AMessage) -> bool: ...
    async def query_task(self, task_id: str) -> A2ATask | None: ...
```

桥接层使用 `httpx.AsyncClient` 与 Go 内核通信，fail-closed：Go 内核不可用时返回 `allowed=False`。

---

## 7. 集成点

v0.35.0 不改动 Python R2 主流程，只在 `LoopController` / `Checkpoint` 暴露可选的桥接入口：

- `Runtime.go_kernel_bridge: GoKernelBridge | None`
- 当配置 `go_kernel.enabled=true` 时，`build_runtime()` 初始化桥接；
- 当 Agent 显式调用「委托」语义（通过未来 `@delegate_to` 装饰器或 admin API）时，桥接到 Go 内核；
- v0.35.0 的测试直接调用桥接 API，不改动现有 `@governed` 路径。

---

## 8. 测试计划

### 8.1 Go 单元测试

- `registry_test.go`：注册、查询、删除 Agent Card
- `task_test.go`：Task 创建、状态流转、查询
- `router_test.go`：消息路由到已注册 Agent
- `delegation_test.go`：委托允许/拒绝决策
- `api_test.go`：HTTP handler 集成测试

### 8.2 Python 测试

- `tests/test_go_kernel_bridge.py`：桥接客户端与 Go 内核进程的端到端测试
- 全量单元测试 `pytest tests/ -m "not integration" -q` 保持通过

---

## 9. 风险与回退

| 风险 | 缓解 |
|---|---|
| Go 代码引入新的构建依赖 | v0.35.0 使用标准库 + `httpx`，不引入外部 Go 库；CI 增加可选 `go test` job； |
| Python 侧 ruff/mypy 不识别 Go 目录 | 在 `pyproject.toml` 中排除 `go/` 目录； |
| Go 内核与 Python 版本绑定 | Go 内核独立进程，通过 HTTP/JSON 通信，不共享内存； |
| A2A 协议未来变更 | 本版本只定义最小字段集，预留扩展字段； |

---

## 10. 后续版本

- **v0.36.0**：完整 A2A 协议（Agent Cards 自动发现、Tasks 流式更新、Parts 完整类型）、gRPC 可选传输、与 `@governed` / MCP Proxy 的委托集成。
- **v0.37.0+**：分布式发现、跨 Agent 审批委托、多租户隔离、与外部 A2A 生态互操作。
