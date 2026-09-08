# v0.18.0 开发文档：事件驱动审批 + 可观测性

## 1. 目标

v0.17.0 已经把 Loop Controller 工具治理内核服务化。v0.18.0 的目标是让这个服务**可运维、可集成**，解决当前两个主要问题：

1. **审批恢复是轮询模式**：Agent 拿到 `require_approval` 后只能反复调用 `resume_after_approval`。
2. **缺少可观测性**：服务运行后没有 metrics、没有结构化日志、没有管理视图。

v0.18.0 解决这两个问题，同时保持**最小可用**原则。

## 2. 设计原则

1. **不破坏现有 API**：`/v1/govern/tool-call` 和 `/v1/govern/resume-after-approval` 保持行为不变。
2. **long-polling 优先于 SSE/WebSocket**：实现简单、兼容现有 CLI 审批、测试容易。SSE 留到后续版本。
3. **可选依赖**：metrics 和 logging 库放入 `[server]` 可选依赖，不污染核心包。
4. **复用现有存储**：wait-for-approval 通过轮询 `ApprovalStore` 实现，无需额外消息队列。

## 3. 新增/修改文件

| 文件 | 操作 | 说明 |
|---|---|---|
| `src/loop_controller/server.py` | 修改 | 新增 wait-for-approval、metrics、admin 路由 |
| `src/loop_controller/server_models.py` | 修改 | 新增 wait/admin 相关模型 |
| `src/loop_controller/metrics.py` | 新增 | Prometheus metrics 封装 |
| `src/loop_controller/logging_config.py` | 新增 | 结构化日志与 trace_id 上下文 |
| `pyproject.toml` | 修改 | `[server]` 增加 `prometheus-client` |
| `examples/http_agent_event_demo.py` | 新增 | 事件驱动审批 Agent 示例 |
| `tests/test_server.py` | 修改 | 新增 wait/metrics/admin 测试 |
| `src/development_log.md` | 修改 | 追加 v0.18.0 记录 |

## 4. 新增 HTTP API

### 4.1 GET /v1/wait-for-approval

long-polling 等待审批结果。

查询参数：

```
request_id=req-123
max_wait=30  # 最大等待秒数，默认 30
```

行为：

- 如果审批已完成，立即返回结果。
- 如果审批未完成，轮询 ApprovalStore，最长等待 `max_wait` 秒。
- 超时后返回 `{"status": "pending", "request_id": "..."}`。

响应：

```json
{
  "status": "allow",
  "result": "email sent",
  "request_id": "req-123"
}
```

或 pending：

```json
{
  "status": "pending",
  "request_id": "req-123"
}
```

### 4.2 GET /metrics

Prometheus 指标。

暴露指标：

- `loop_controller_requests_total`：总请求数（按 endpoint、status 分）
- `loop_controller_request_duration_seconds`：请求处理耗时直方图
- `loop_controller_tool_calls_total`：工具调用数（按 tool_name、status 分）
- `loop_controller_approval_pending_total`：当前待审批请求数

### 4.3 GET /health（增强）

响应：

```json
{
  "status": "ok",
  "opa_reachable": true,
  "gateway_ready": true,
  "uptime_seconds": 123
}
```

### 4.4 GET /v1/admin/approvals/pending

列出待审批请求。

响应：

```json
{
  "approvals": [
    {
      "request_id": "...",
      "decision_id": "...",
      "tool_name": "send_email",
      "requester_id": "alice",
      "reason": "..."
    }
  ]
}
```

### 4.5 GET /v1/admin/audit

查询审计事件。

查询参数：

```
session_id=...
task_id=...
limit=100
```

## 5. 结构化日志

每个请求分配 `trace_id`，通过 `X-Trace-ID` header 返回。

日志字段：

```json
{
  "timestamp": "2026-08-22T12:00:00Z",
  "level": "INFO",
  "trace_id": "trace-123",
  "method": "POST",
  "path": "/v1/govern/tool-call",
  "agent_id": "researcher_001",
  "tool_name": "send_email",
  "status": "require_approval",
  "request_id": "req-123",
  "duration_ms": 45
}
```

## 6. 事件驱动审批示例流程

```python
# Agent 调用 tool-call，拿到 require_approval
result = await client.post("/v1/govern/tool-call", json={...})
# result: {status: "require_approval", request_id: "req-123"}

# Agent 等待审批完成
resp = await client.get("/v1/wait-for-approval", params={"request_id": "req-123", "max_wait": 60})
# 审批人批准后，返回 {status: "allow", result: "email sent"}
```

## 7. 测试策略

1. wait-for-approval：
   - mock controller + mock approval store
   - 测试立即返回、等待后返回、超时返回 pending
2. metrics：
   - 调用 endpoint 后检查 `loop_controller_requests_total` 增长
3. health/admin：
   - 测试响应格式
4. trace_id：
   - 测试响应头包含 X-Trace-ID

## 8. 验收标准

- [x] `/v1/wait-for-approval` long-polling 工作正常
- [x] `/metrics` 暴露 Prometheus 指标
- [x] `/health` 返回增强信息
- [x] `/v1/admin/approvals/pending` 和 `/v1/admin/audit` 可查询
- [x] 请求日志包含 trace_id
- [x] 新增示例可运行
- [x] `pytest -W error::DeprecationWarning tests/` 全部通过
- [x] `ruff check src tests examples` 通过
- [x] `mypy src` 无新增错误
- [x] `development_log.md` 追加 v0.18.0 记录

## 9. 后续铺垫

v0.18.0 完成后，工具治理服务具备：
- 事件驱动审批恢复
- 可观测性基础
- 管理 API

v0.19.0 可以在此基础上：
- 把 long-polling 升级为 SSE/WebSocket
- 增加 gRPC 接口
- 增加策略热更新 API
