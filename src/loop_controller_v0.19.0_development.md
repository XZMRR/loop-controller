# v0.19.0 开发文档：实时审批通道 + gRPC 边界

## 1. 目标

v0.18.0 已经让 Loop Controller 工具治理服务具备 HTTP 接口、long-polling 审批等待、Prometheus 指标和结构化日志。v0.19.0 的目标是：

1. **实时审批通道**：用 SSE（Server-Sent Events）替代/补充 long-polling，让 Agent 在审批完成时立即收到推送。
2. **gRPC 服务边界**：把 Python 工具治理服务封装成标准 gRPC 服务，为将来 Go 交互治理内核提供高性能、强类型的调用边界。

两者共用同一套底层事件通知机制。

## 2. 设计原则

1. **不破坏现有 API**：`/v1/wait-for-approval` long-polling 保留，新增 `/v1/wait-for-approval/sse`。
2. **HTTP 与 gRPC 共存**：gRPC 不是替代 HTTP，而是面向内部服务间调用的补充边界。
3. **单一事件源**：ApprovalStore 仍是权威状态源；SSE/gRPC 只做通知，不额外持久化。
4. **可选依赖**：`grpcio` 与 `grpcio-tools` 放入新的 `[grpc]` 可选依赖，不污染核心包与 HTTP-only 部署。
5. **复用现有治理语义**：gRPC message 直接映射到现有 Pydantic 模型字段，不发明新语义。

## 3. 新增/修改文件

| 文件 | 操作 | 说明 |
|---|---|---|
| `proto/loop_controller/v1/governance.proto` | 新增 | gRPC service 与 message 定义 |
| `src/loop_controller/grpc_server.py` | 新增 | gRPC servicer 实现 |
| `src/loop_controller/grpc_client.py` | 新增 | 可选的 Python gRPC 客户端封装 |
| `src/loop_controller/server.py` | 修改 | 新增 SSE endpoint `/v1/wait-for-approval/sse` |
| `src/loop_controller/approval_watcher.py` | 新增 | 基于 asyncio 的 ApprovalStore 变更通知器 |
| `src/loop_controller/cli.py` | 修改 | 新增 `lc grpc-server` 子命令 |
| `pyproject.toml` | 修改 | 新增 `[grpc]` 可选依赖与 dev 依赖 `grpcio-tools` |
| `examples/grpc_agent_demo.py` | 新增 | 通过 gRPC 调用治理服务的 Agent 示例 |
| `examples/sse_agent_demo.py` | 新增 | 通过 SSE 等待审批的 Agent 示例 |
| `tests/test_grpc_server.py` | 新增 | gRPC 服务单元测试 |
| `tests/test_sse.py` | 新增 | SSE endpoint 测试 |
| `tests/test_approval_watcher.py` | 新增 | ApprovalWatcher 测试 |
| `src/development_log.md` | 修改 | 追加 v0.19.0 记录 |
| `src/loop_controller_v0.19.0_development.md` | 新增 | 本文档 |

## 4. 实时审批通道（SSE）

### 4.1 API

```
GET /v1/wait-for-approval/sse?request_id=req-123&max_wait=60
```

响应头：

```
Content-Type: text/event-stream
Cache-Control: no-cache
Connection: keep-alive
X-Trace-ID: <trace_id>
```

事件：

```text
event: pending
data: {"request_id": "req-123", "status": "pending"}

event: result
data: {"request_id": "req-123", "status": "allow", "result": "email sent"}
```

行为：

- 立即发送一次 `pending` 心跳。
- 当审批完成时，发送 `result` 事件并关闭连接。
- 超时未审批则发送 `pending` 事件并关闭连接。

### 4.2 实现思路

新建 `ApprovalWatcher`：

```python
class ApprovalWatcher:
    async def wait(self, request_id: str, timeout: float) -> GovernanceResult | None: ...
    def notify(self, request_id: str) -> None: ...
```

- 内部维护 `dict[str, list[asyncio.Event]]`。
- `wait()` 注册 Event，超时或被 notify 唤醒后查询 ApprovalStore 并返回。
- `resume_after_approval` / `approve` / `deny` 路径在状态变更后调用 `watcher.notify(request_id)`。

SSE handler 调用 `watcher.wait()`，结果返回时 yield SSE event。

## 5. gRPC 服务边界

### 5.1 Proto 定义（`proto/loop_controller/v1/governance.proto`）

```protobuf
syntax = "proto3";
package loop_controller.v1;

service ToolGovernance {
  rpc EvaluateToolCall(EvaluateToolCallRequest) returns (EvaluateToolCallResponse);
  rpc ResumeAfterApproval(ResumeAfterApprovalRequest) returns (EvaluateToolCallResponse);
  rpc WaitForApproval(WaitForApprovalRequest) returns (stream EvaluateToolCallResponse);
  rpc GetHealth(HealthRequest) returns (HealthResponse);
  rpc ListPendingApprovals(ListPendingApprovalsRequest) returns (ListPendingApprovalsResponse);
  rpc QueryAuditEvents(QueryAuditEventsRequest) returns (stream AuditEvent);
}

message EvaluateToolCallRequest {
  string agent_id = 1;
  string user_id = 2;
  string tool_name = 3;
  string arguments_json = 4;  // JSON object
  string task_context = 5;
  string session_id = 6;
  string task_id = 7;
}

message EvaluateToolCallResponse {
  string status = 1;        // allow / deny / require_approval / error / blocked
  string result = 2;
  string request_id = 3;    // require_approval 时返回
  string error_code = 4;
}

message ResumeAfterApprovalRequest {
  string request_id = 1;
}

message WaitForApprovalRequest {
  string request_id = 1;
  int32 max_wait_seconds = 2;
}

message HealthRequest {}

message HealthResponse {
  string status = 1;
  bool opa_reachable = 2;
  bool gateway_ready = 3;
  float uptime_seconds = 4;
}

message PendingApproval {
  string request_id = 1;
  string decision_id = 2;
  string tool_name = 3;
  string requester_id = 4;
  string reason = 5;
}

message ListPendingApprovalsRequest {}

message ListPendingApprovalsResponse {
  repeated PendingApproval approvals = 1;
}

message QueryAuditEventsRequest {
  string session_id = 1;
  string task_id = 2;
  int32 limit = 3;
}

message AuditEvent {
  string event_id = 1;
  string trace_id = 2;
  string session_id = 3;
  string action = 4;
  string actor_type = 5;
  string actor_id = 6;
  string target = 7;
  string decision = 8;
  string reason = 9;
  string timestamp = 10;
  string payload_json = 11;  // 其余字段 JSON 序列化后存放
}
```

### 5.2 生成代码

- 使用 `grpcio-tools` 生成：`python -m grpc_tools.protoc --proto_path=proto --python_out=src --grpc_python_out=src loop_controller/v1/governance.proto`
- 生成文件放入 `src/loop_controller/proto/v1/`。
- 该目录加入 `.gitignore` 或每次构建时生成；为简化 CI，选择将生成代码提交到仓库。

### 5.3 Servicer 实现

`src/loop_controller/grpc_server.py`：

```python
class ToolGovernanceServicer(governance_pb2_grpc.ToolGovernanceServicer):
    def __init__(self, controller: LoopController, watcher: ApprovalWatcher): ...
```

- `EvaluateToolCall`：解析 JSON arguments，调用 `controller.evaluate_and_execute()`，返回 proto response。
- `ResumeAfterApproval`：调用 `controller.resume_after_approval()`。
- `WaitForApproval`：server-streaming，先 yield pending，再等待 watcher，yield result。
- `GetHealth`：同 HTTP health。
- `ListPendingApprovals`：读取 ApprovalStore pending 列表。
- `QueryAuditEvents`：读取 AuditStore，按条件过滤并流式返回。

### 5.4 启动入口

`lc grpc-server --port 50051 --config ./config --api-key ...`

## 6. ApprovalWatcher 设计

```python
class ApprovalWatcher:
    def __init__(self) -> None: ...
    async def wait(self, request_id: str, timeout: float | None = None) -> None: ...
    def notify(self, request_id: str) -> None: ...
```

- 内部 `dict[str, asyncio.Event]`。
- `wait()` 创建或复用 Event，等待 notify 或超时。
- 线程安全：notify 可由 async 上下文调用；CLI 审批路径当前是 sync，需要改为 async 或把 notify 投递到事件循环。

集成点：

- `AsyncApprovalManager.approve()` / `deny()` 在写入记录后调用 `watcher.notify(request_id)`。
- `LoopController.resume_after_approval()` 不需要修改，因为 approve/deny 会触发 notify。

## 7. 依赖

`pyproject.toml`：

```toml
[project.optional-dependencies]
server = [
    "starlette>=0.40",
    "uvicorn>=0.30",
    "prometheus-client>=0.20",
]
grpc = [
    "grpcio>=1.68",
    "grpcio-tools>=1.68",
]
all-adapters = [
    "loop-controller[langchain,openai-agents,autogen,server,grpc]",
]

[dependency-groups]
dev = [
    ...,
    "grpcio-tools>=1.68",
]
```

## 8. 测试策略

### 8.1 ApprovalWatcher

- `test_wait_notified`：注册 wait，notify 后 wait 立即返回。
- `test_wait_timeout`：不 notify，超时返回 False/None。
- `test_multiple_waiters_same_request`：多个 waiter 同时被唤醒。

### 8.2 SSE

- 使用 `TestClient` 或 `httpx` 流式读取 SSE。
- mock controller + watcher，验证 event 格式与关闭行为。

### 8.3 gRPC

- 启动 in-process gRPC server（`grpc.aio.server` + `add_insecure_port("localhost:0")`）。
- 覆盖 EvaluateToolCall、WaitForApproval streaming、ListPendingApprovals。

## 9. 验收标准

- [x] `GET /v1/wait-for-approval/sse` 能推送 pending 与 result 事件
- [x] `ApprovalWatcher` 支持多 waiter 并发唤醒
- [x] gRPC `EvaluateToolCall` 返回正确结果
- [x] gRPC `WaitForApproval` server-streaming 工作正常
- [x] `lc grpc-server` CLI 子命令可启动服务
- [x] `pytest -W error::DeprecationWarning tests/` 全部通过
- [x] `ruff check src tests examples` 通过
- [x] `mypy src` 无新增错误
- [x] `development_log.md` 追加 v0.19.0 记录

## 10. 后续铺垫

v0.19.0 完成后：
- Python 工具治理服务同时暴露 HTTP（面向 Agent/外部）与 gRPC（面向内核）
- 审批恢复具备实时推送能力

v0.20.0 可以开始：
- 设计 Go 交互治理内核 MVP，通过 gRPC 调用 Python 工具治理服务
- 策略热更新 API
- 多副本部署下的 ApprovalWatcher 共享（Redis / 消息队列）
