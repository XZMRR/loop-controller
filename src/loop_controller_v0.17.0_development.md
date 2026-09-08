# v0.17.0 开发文档：Loop Controller HTTP 服务化

## 1. 目标

v0.16.0 已经把 Python 层的工具调用治理抽象为 `ToolGovernor`，所有框架适配器都复用它。

v0.17.0 的目标是把 Loop Controller 从“被 import 的 Python 库”升级为**一个可独立运行的 HTTP 服务**，让任何语言、任何部署方式的 Agent 都能通过网络调用治理。

范围**最小化**：只暴露两个核心治理 endpoint + health check，不追求生产级完整服务。

## 2. 设计原则

1. **最小可用**：只做 `/v1/govern/tool-call` 和 `/v1/govern/resume-after-approval`。
2. **向后兼容**：现有 `ToolGovernor`、适配器、示例全部保留不变。
3. **复用现有抽象**：服务内部直接调用 `ToolGovernor.call(...)` 和 `LoopController.resume_after_approval(...)`。
4. **轻量依赖**：使用 Starlette + uvicorn，不引入 FastAPI 等重型框架。
5. **可选依赖**：`server` 相关依赖放入 `[project.optional-dependencies]`，核心包保持轻量。

## 3. 依赖配置

在 `pyproject.toml` 增加：

```toml
[project.optional-dependencies]
server = [
    "starlette>=0.40",
    "uvicorn>=0.30",
]
```

测试使用 `starlette.testclient.TestClient`，无需启动真实端口。

## 4. 新增文件

| 文件 | 说明 |
|---|---|
| `src/loop_controller/server.py` | HTTP 服务实现：路由、认证、请求处理 |
| `src/loop_controller/server_models.py` | Pydantic 请求/响应模型 |
| `examples/http_agent_demo.py` | 通过 HTTP 调用 Loop Controller 的 Agent 示例 |
| `tests/test_server.py` | HTTP 服务单元测试 |

## 5. 修改文件

| 文件 | 说明 |
|---|---|
| `pyproject.toml` | 增加 `[server]` 可选依赖 |
| `src/loop_controller/cli.py` | 增加 `lc server` 子命令 |
| `src/development_log.md` | 追加 v0.17.0 记录 |
| `src/loop_controller_v0.17.0_development.md` | 本文档 |

## 6. HTTP API

### 6.1 POST /v1/govern/tool-call

请求体：

```json
{
  "agent_id": "researcher_001",
  "user_id": "alice",
  "tool_name": "send_email",
  "arguments": {
    "to": "zhang@company.com",
    "subject": "摘要",
    "body": "请查收"
  },
  "task_context": "发送调研摘要",
  "session_id": "session-001",
  "task_id": "task-001"
}
```

响应体：

```json
{
  "status": "allow",
  "result": "email sent"
}
```

或：

```json
{
  "status": "require_approval",
  "result": "[requires approval] request_id=...",
  "request_id": "..."
}
```

### 6.2 POST /v1/govern/resume-after-approval

请求体：

```json
{
  "request_id": "req-..."
}
```

响应体：

```json
{
  "status": "allow",
  "result": "email sent"
}
```

### 6.3 GET /health

响应体：

```json
{"status": "ok"}
```

## 7. 认证

最小实现：通过 `X-API-Key` header 或 `Authorization: Bearer <token>` 做简单 API key 校验。

- 默认从环境变量 `LOOP_CONTROLLER_API_KEY` 读取 key
- 未设置时允许所有请求（开发模式），但打印警告
- 401 返回统一错误格式

## 8. 服务启动方式

### 8.1 CLI

```bash
lc server --config ./config --port 8080 --opa-url http://127.0.0.1:8181
```

### 8.2 编程方式

```python
from loop_controller.server import build_app
from loop_controller.controller import build_controller
from loop_controller.infra.config_loader import ConfigLoader

config = ConfigLoader().load("config")
controller = await build_controller(config)
app = build_app(controller, api_key="optional-key")
```

## 9. 测试策略

1. 使用 `starlette.testclient.TestClient` 对 ASGI app 做同步调用。
2. Mock `LoopController`：
   - `evaluate_and_execute` 返回预定义 `GovernanceResult`
   - `resume_after_approval` 返回预定义结果
3. 测试覆盖：
   - `/health` 返回 ok
   - `/v1/govern/tool-call` 正确转发参数
   - `/v1/govern/resume-after-approval` 正确转发 request_id
   - API key 认证生效
   - 缺少参数返回 422
   - 未安装 server 依赖时导入报错友好

## 10. 验收标准

- [x] `src/loop_controller/server.py` 存在且 ruff/mypy 通过
- [x] `src/loop_controller/server_models.py` 存在且 ruff/mypy 通过
- [x] `lc server` CLI 命令可用
- [x] `examples/http_agent_demo.py` 可运行
- [x] `tests/test_server.py` 覆盖核心路径
- [x] `pytest -W error::DeprecationWarning tests/` 全部通过
- [x] `ruff check src tests examples` 通过
- [x] `mypy src` 无新增错误
- [x] `development_log.md` 追加 v0.17.0 记录

## 11. 后续铺垫

v0.17.0 完成后，Python 工具治理内核成为一个独立 HTTP 服务。未来 Go 交互治理内核可以直接通过 HTTP 调用它：

```go
resp, _ := http.Post("http://localhost:8080/v1/govern/tool-call", ...)
```

v0.18.0 可以在之上增加：
- SSE/WebSocket 审批事件推送
- 配置/策略管理 API
- Prometheus metrics endpoint
