# Loop Controller v0.5.0 开发文档：MCP Proxy / 外来 Agent 接入

> **文档定位**：v0.4.0 的下一个迭代版本。核心目标是把 Loop Controller 的治理能力从"内部 Runtime 调用"扩展到"外部 Agent 的 MCP 工具调用"，使未安装 Loop Controller SDK 的第三方 Agent 也能被 R2/R3 治理。
>
> **版本**：v0.5.0
> **状态**：详细设计，可直接进入开发
> **最后更新**：2026-08-19

---

## 1. 版本核心跃迁

v0.4.0 之前，Loop Controller 只能治理**自己 Runtime 内运行的 Agent**。外部 Agent 若想调用工具，必须自己实现治理逻辑，或直接绕过治理。

v0.5.0 把 Loop Controller 同时暴露为一个 **MCP Server**，外部 Agent 作为 MCP Client 连接后，所有 tool call 都经过 Loop Controller 的 `Checkpoint` 治理，再转发到真实的 MCP Server。外部 Agent 侧无需修改业务逻辑，只需把 MCP endpoint 指向 Loop Controller。

### 1.1 典型使用场景

| 场景 | 说明 |
|---|---|
| 外部 LLM Agent | 另一个基于 LLM 的 Agent，通过 MCP 调用 `send_email`、`read_file` 等工具 |
| 低代码平台 | 用户拖拽配置的工作流节点，通过 MCP 调用工具 |
| 多 Agent 系统 | 多个子 Agent 共享同一个 Loop Controller Proxy，统一风控 |

### 1.2 关键设计约束

- **外部 Agent 不感知治理存在**：看到的工具列表和调用方式与直接连真实 MCP Server 一致；
- **每次 tool call 独立治理**：外部 Agent 没有 Planner，因此一次 MCP tool call = 一个 `ActionProposal`；
- **异步审批暂不暴露**：MCP tool call 是同步请求-响应；v0.5.0 中 `require_approval` 直接返回 deny 并附带审批指引；
- **Session 跨调用生效**：同一外部 Agent 连接内的多次 tool call 共享同一个 Session，v0.4.0 的跨 Task 风险累计生效。

---

## 2. 架构设计

### 2.1 总体数据流

```
外部 Agent (MCP Client)
    │
    │ stdio / SSE
    ▼
┌─────────────────────────────────────┐
│  Loop Controller MCP Proxy Server   │  ← v0.5.0 新增
│  - initialize                         │
│  - tools/list                         │
│  - tools/call                         │
└─────────────────────────────────────┘
    │
    │ 构建 ActionProposal
    ▼
┌─────────────────────────────────────┐
│  SessionManager.get_or_create_session │
│  Runtime.create_task                  │
│  Checkpoint.evaluate()                │
│  Checkpoint.forward()                 │
└─────────────────────────────────────┘
    │
    │ 转发到真实 MCP server
    ▼
┌─────────────────────────────────────┐
│  MCPGateway                           │
│  真实 MCP tools                       │
└─────────────────────────────────────┘
```

### 2.2 与现有组件的关系

| 现有组件 | v0.5.0 中使用方式 |
|---|---|
| `Runtime` | 复用其 `create_task`、Session/Risk 管理、Audit 存储 |
| `Checkpoint` | 复用 `evaluate()` 和 `forward()`，不调用 `run_task` |
| `MCPGateway` | 复用真实工具调用能力 |
| `ConfigLoader` | 复用现有配置加载 |
| `Planner` | **不使用**。外部 Agent 自己决定调用什么工具 |
| `AsyncApprovalManager` | **v0.5.0 不暴露**。`require_approval` 直接 deny |

---

## 3. 核心抽象

### 3.1 ProxyIdentity：外部 Agent 身份

外部 Agent 不是 `agents.yaml` 里预定义的 Agent，但 Proxy 启动时必须把它映射到一个内部 Agent。

```python
@dataclass
class ProxyIdentity:
    agent_id: str
    user_id: str
    session_id: str | None  # 可选，复用已有 Session
```

映射来源（按优先级）：
1. CLI 参数 `--agent-id`、`--user-id`、`--session-id`；
2. SSE 模式 HTTP header `x-loop-controller-agent-id`、`x-loop-controller-user-id`；
3. MCP initialize 时的 `clientInfo.name`（fallback）。

### 3.2 ProxySession：Proxy 侧会话

一个 MCP 连接对应一个 ProxySession。同一连接内的所有 tool call 共享一个 Loop Controller `Session`。

```python
@dataclass
class ProxySession:
    proxy_session_id: str
    identity: ProxyIdentity
    runtime: Runtime
    loop_session_id: str
    created_at: datetime
```

### 3.3 工具列表透传

Proxy Server 的工具列表直接来自 `MCPGateway.list_tools()`，转换时：
- 保留 `name`、`description`、`inputSchema`；
- 不暴露 Loop Controller 内部元数据；
- 工具名使用 Loop Controller 内部 canonical name（与真实 MCP server 的映射由 `tool_mapping` 处理）。

---

## 4. 组件详细设计

### 4.1 `loop_controller/proxy_server.py`

使用 `mcp.server.Server` 实现 MCP Server。

```python
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.server.sse import SseServerTransport

class LoopControllerProxyServer:
    def __init__(self, runtime: Runtime, identity: ProxyIdentity) -> None: ...

    def build_mcp_server(self) -> Server:
        server = Server("loop-controller-proxy")

        @server.list_tools()
        async def list_tools() -> list[types.Tool]: ...

        @server.call_tool()
        async def call_tool(name: str, arguments: dict) -> list[types.TextContent]: ...

        return server
```

### 4.2 `tools/list` 实现

```python
@server.list_tools()
async def list_tools() -> list[types.Tool]:
    tools = await self.runtime.gateway.list_tools()
    return [
        types.Tool(
            name=tool.canonical_name,
            description=tool.description,
            inputSchema=tool.input_schema,
        )
        for tool in tools
    ]
```

### 4.3 `tools/call` 实现

```python
@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    agent = self.runtime.checkpoint._identity.get_agent(self.identity.agent_id)
    if agent is None:
        return _error_result(f"unknown agent_id: {self.identity.agent_id}")

    task, session = self.runtime.create_task(
        user_id=self.identity.user_id,
        agent_id=agent.agent_id,
        description=f"proxy call: {name}",
        session_id=self.identity.session_id or self._proxy_session.loop_session_id,
    )

    proposal = ActionProposal(
        task_id=task.task_id,
        call_id=uuid.uuid4().hex,
        agent_id=agent.agent_id,
        tool_name=name,
        arguments=dict(arguments),
        task_context="",
    )

    try:
        decision = await self.runtime.checkpoint.evaluate(task, agent, proposal)
    except Exception as exc:
        return _error_result(f"governance evaluation failed: {exc}")

    if decision.verdict == "require_approval":
        return _error_result(
            f"BLOCKED: requires human approval (decision_id={decision.decision_id}). "
            "Approve via 'lc approvals approve <decision_id>' and retry."
        )

    if decision.verdict == "deny":
        return _error_result(f"DENIED: {decision.reason}")

    try:
        result = await self.runtime.checkpoint.forward(
            proposal, decision, session_id=session.session_id
        )
    except Exception as exc:
        return _error_result(f"execution failed: {exc}")

    if result.status == "success":
        return [types.TextContent(type="text", text=str(result.content))]
    return _error_result(str(result.content))
```

### 4.4 错误响应格式

所有治理拦截和执行失败都通过 MCP `TextContent` 返回可读文本。v0.5.0 不引入自定义 content type，保持简单。

```python
def _error_result(message: str) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=f"[loop-controller] {message}")]
```

---

## 5. CLI 入口

新增 `lc proxy` 子命令，复用现有 `build_runtime()`。

```bash
# stdio 模式（默认）
lc proxy --config-dir ./config --agent-id external_agent_001 --user-id alice

# SSE 模式
lc proxy --config-dir ./config --agent-id external_agent_001 --user-id alice --transport sse --port 8080

# 复用已有 Session
lc proxy --config-dir ./config --agent-id external_agent_001 --user-id alice --session-id s-xxx
```

CLI 参数：

| 参数 | 说明 |
|---|---|
| `--config-dir` | 配置目录，默认 `./config` |
| `--agent-id` | 映射到 `agents.yaml` 中的 agent |
| `--user-id` | 外部 Agent 代表的用户 |
| `--session-id` | 可选，复用已有 Session |
| `--transport` | `stdio`（默认）或 `sse` |
| `--port` | SSE 模式端口，默认 `8080` |
| `--host` | SSE 模式 host，默认 `127.0.0.1` |

---

## 6. SSE 模式身份透传

SSE 模式下，外部 Agent 通过 HTTP 连接。Loop Controller 在 SSE 握手时读取 header：

```http
GET /sse HTTP/1.1
Host: 127.0.0.1:8080
x-loop-controller-agent-id: external_agent_001
x-loop-controller-user-id: alice
x-loop-controller-session-id: s-xxx
```

每个 SSE 连接独立创建一个 `ProxySession`。header 中的 `session_id` 用于复用已有 Session；为空则创建新 Session。

---

## 7. 与 v0.4.0 的 Session 机制协同

- Proxy 启动时调用 `runtime.create_task()`；
- 若未指定 `session_id`，`SessionManager` 按 `(user_id, agent_id)` 自动分配或复用活跃 Session；
- 同一连接内后续 tool call 复用同一个 `loop_session_id`；
- v0.4.0 的 `consecutive_deny_count` 和 `cumulative_risk_score` 因此会跨多次 tool call 累计。

---

## 8. 异步审批策略（v0.5.0 明确不暴露）

MCP `tools/call` 是同步请求-响应，无法像 `run_task` 那样返回 `needs_approval` 暂停态。v0.5.0 选择**直接 deny** 并返回审批指引。

未来可选增强（v0.5.1）：
- 长轮询：在 `call_tool` 内阻塞等待审批，最多 N 秒；
- 回调机制：外部 Agent 需提供回调 URL，审批后主动推送；
- MCP sampling：利用 MCP 协议的 sampling 能力请求人类确认。

---

## 9. 安全约束

| 约束 | 说明 |
|---|---|
| 默认只监听 localhost | SSE 模式默认 `127.0.0.1`，避免公网暴露 |
| 身份必须预注册 | `--agent-id` 必须存在于 `agents.yaml`，否则 tool call 失败 |
| Session 不可跨 user 复用 | `runtime.create_task()` 会校验 `task.user_id == session.user_id` |
| 真实 MCP server 不对外暴露 | 外部 Agent 只能通过 Proxy 访问工具 |
| 审计完整 | 每次 Proxy tool call 写 `task_start`、`propose`、`evaluate`、`execute`、`task_end` |

---

## 10. 目录结构变化

```text
src/loop_controller/
├── proxy_server.py         # 新增：MCP Server 实现
├── cli/
│   ├── __init__.py
│   ├── main.py             # 新增/修改：注册 `lc proxy`
│   └── approvals.py
```

---

## 11. 验收标准

| # | 验收项 | 通过条件 |
|---|---|---|
| P1 | stdio MCP Proxy 启动 | `lc proxy --agent-id xxx --user-id alice` 成功启动并响应 `initialize` |
| P2 | 工具列表透传 | 外部 MCP client 看到的工具列表与 `MCPGateway` 一致 |
| P3 | allow 工具调用 | 外部 Agent 调用低风险工具成功返回结果 |
| P4 | deny 工具调用 | 外部 Agent 调用越权工具返回 `DENIED` |
| P5 | require_approval 直接 deny | 外部 Agent 调用需审批工具返回 `BLOCKED: requires human approval` |
| P6 | Session 风险累计跨 tool call | 同一连接连续越权后，第三次调用被 Session 硬熔断直接 deny |
| P7 | SSE 模式身份透传 | 通过 HTTP header 指定 agent/user/session，工具调用走对应身份 |
| P8 | 审计日志完整 | `audit.jsonl` 中每个 Proxy tool call 包含 `task_start` 到 `task_end` |

---

## 12. 风险与约束声明

1. **同步调用限制**：v0.5.0 不实现异步审批长轮询，需要人工审批的工具会被直接拒绝；
2. **单连接 Session**：SSE 模式下每个 TCP 连接一个 Session，连接断开后 Session 保留但不再被该连接复用；
3. **工具 schema 透传**：v0.5.0 不做 schema 改写，若真实 MCP tool schema 包含敏感示例，会直接暴露给外部 Agent；
4. **性能**：每个 tool call 都创建一次 Task 并写审计，高频调用场景需后续优化。

---

## 13. 参考文档

- `docs/architecture/00_r0r3_architecture.md`
- `src/loop_controller_v0.4.0_development.md`
- `src/history/Loop_Controller方案_v1.2增补.md`
- `reports/develop_mvp_review_for_team.md`
