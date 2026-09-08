# v0.25.0 开发文档：Harness 作为生产级执行后端

## 1. 目标

v0.24.0 已完成架构收敛，Loop Controller 明确定位为 **工具调用治理控制平面**，内部只保留 MCP / HTTP 协议型工具代理，高危工具通过 MCP 包装示例和 Harness 示例接入。

v0.25.0 的目标是把 Harness 从示例提升为 **生产级可插拔执行后端**：

> **一句话目标**：让 Loop Controller 能够把任意工具调用安全地路由到外部 Harness 执行，Loop Controller 只做治理决策，Harness 负责实际执行与隔离。

v0.25.0 只做以下三件事：

1. **定义 Loop Controller ↔ Harness 通信协议**：gRPC/HTTP 执行协议，包含身份、上下文、工具参数、结果、错误码；
2. **实现 HarnessExecutor**：Loop Controller 内部新增执行器，将指定工具调用转发给 Harness；
3. **提供生产级 Harness 参考实现**：支持子进程和 Docker 两种 Harness 后端，具备超时、输出限制、环境隔离和网络白名单能力。

v0.25.0 **不做**：

- 不新增 Shell / Browser / SQL 内置执行器（已删除）；
- 不把 Harness 做成 Loop Controller 的子进程组件（Harness 是独立进程/容器）；
- 不做完整的多租户隔离、全局吊销、KMS 集成（v0.26.0）。

---

## 2. 背景与动机

### 2.1 为什么需要 Harness

Loop Controller 已经能治理 MCP / HTTP 工具的调用入口，但企业还有大量工具和能力无法被简单包装成远程协议：

- 内部脚本/CLI（Shell、Python、kubectl、awscli）；
- 浏览器自动化（Playwright、Selenium）；
- 数据库直连（JDBC/ODBC/各语言驱动）；
- 需要访问本地文件、GPU、内部网络的能力。

这些工具的共性是：**调用入口不标准，但调用行为可以被隔离和控制**。Harness 就是负责“隔离和控制”的执行环境，Loop Controller 负责“判断能不能调”。

### 2.2 Harness 与 Loop Controller 的关系

```text
Agent
  ↓
Loop Controller（身份、策略、审批、审计）
  ↓
HarnessExecutor
  ↓
Harness 进程/容器（实际执行工具）
  ↓
真实工具或系统能力
```

Loop Controller 只回答三个问题：

1. 谁在调用？（身份）
2. 能不能调用？（策略/风险/审批）
3. 调用后发生了什么？（审计）

Harness 回答：

1. 如何安全地执行？（子进程/容器/沙箱）
2. 如何限制副作用？（网络、文件、环境变量）
3. 如何返回结果？（输出大小、超时、错误码）

### 2.3 与 v0.24.0 示例的衔接

v0.24.0 提供了 `examples/contrib/harness/` 示例。v0.25.0 把这些示例升级为正式组件：

- 示例中的通信协议变成内部协议；
- `docker_harness.py` 升级为生产可用的 Docker Harness backend；
- 新增 `SubprocessHarnessBackend` 用于开发和测试；
- 新增 `HarnessExecutor` 作为 Loop Controller 到 Harness 的桥接。

---

## 3. 设计原则

1. **Loop Controller 不执行工具**：只把执行请求转发给 Harness，Harness 内部执行。保持控制平面与执行平面分离。
2. **Harness 是独立进程**：可以是本地子进程、Docker 容器、K8s sidecar、远程 VM，不依赖 Loop Controller 主进程。
3. **协议轻量**：Loop Controller ↔ Harness 的通信协议只包含必要字段（身份、工具名、参数、上下文），不暴露 Loop Controller 内部状态。
4. **fail-closed**：Harness 不可达、身份校验失败、输出超限、超时均返回受控错误。
5. **依赖可选**：Docker SDK 作为可选 extra `[container]`，不安装时 Docker Harness 不可用，但子进程 Harness 仍可用。

---

## 4. 新增/修改文件

| 文件 | 操作 | 说明 |
|---|---|---|
| `src/loop_controller/executors/harness_executor.py` | 新增 | Loop Controller 侧转发执行器 |
| `src/loop_controller/executors/harness_models.py` | 新增 | Harness 工具配置与后端配置模型 |
| `src/loop_controller/executors/harness_protocol.py` | 新增 | Loop Controller ↔ Harness 通信协议定义 |
| `examples/contrib/harness/harness_server.py` | 新增 | 参考 Harness 服务器（HTTP/gRPC 入口） |
| `examples/contrib/harness/subprocess_backend.py` | 新增 | 子进程执行后端 |
| `examples/contrib/harness/docker_backend.py` | 重写 | Docker 容器执行后端（由 `docker_harness.py` 升级） |
| `examples/contrib/harness/runner.py` | 修改 | 支持启动子进程/Docker Harness |
| `src/loop_controller/infra/config_loader.py` | 修改 | 加载 `config/harness_tools.yaml` |
| `src/loop_controller/runtime.py` | 修改 | 注册 HarnessExecutor，支持按工具名路由到 Harness |
| `config/harness_tools.yaml` | 新增 | Harness 工具与后端配置示例 |
| `tests/test_harness_executor.py` | 新增 | HarnessExecutor 单元/集成测试 |
| `tests/test_harness_subprocess.py` | 新增 | 子进程 Harness 测试 |
| `tests/test_harness_docker.py` | 新增 | Docker Harness 测试（可选，CI 或本地有 Docker 时运行） |
| `src/KNOWN_LIMITATIONS.md` | 修改 | 更新 Harness 边界 |
| `src/development_log.md` | 修改 | 追加 v0.25.0 记录 |
| `src/loop_controller_v0.25.0_development.md` | 新增 | 本文档 |

---

## 5. 配置模型

### 5.1 `config/harness_tools.yaml`

```yaml
backends:
  local_subprocess:
    type: subprocess
    # 子进程 Harness 的二进制或 Python 模块入口
    command: ["python", "-m", "examples.contrib.harness.harness_server"]
    env:
      LOOP_CONTROLLER_HARNESS_MODE: subprocess
    max_concurrent_calls: 10

  docker_linux:
    type: docker
    image: loop-controller/harness:latest
    network_mode: none
    env:
      LOOP_CONTROLLER_HARNESS_MODE: docker
    mounts:
      - source: /var/run/secrets
        target: /secrets
        read_only: true
    max_concurrent_calls: 5

tools:
  run_shell_script:
    harness: local_subprocess
    description: 在受控 Harness 中执行白名单 Shell 命令
    input_schema:
      type: object
      properties:
        command: {type: string}
      required: [command]
    default_risk: critical
    cost_per_call: 100
    sandbox:
      timeout_seconds: 30
      max_output_bytes: 131072
      allowed_hosts: []
      allowed_paths: ["/tmp"]

  query_database:
    harness: docker_linux
    description: 在隔离容器中执行只读 SQL
    input_schema:
      type: object
      properties:
        sql: {type: string}
      required: [sql]
    default_risk: high
    cost_per_call: 100
    sandbox:
      timeout_seconds: 30
      max_output_bytes: 65536
```

### 5.2 Harness 工具规范

```python
class HarnessToolSpec(BaseModel):
    tool_name: str
    harness: str  # backend key
    description: str
    input_schema: dict[str, Any]
    default_risk: RiskLevel = RiskLevel.critical
    cost_per_call: int = 100
    sandbox: HarnessSandboxConfig | None = None

class HarnessBackendConfig(BaseModel):
    name: str
    type: Literal["subprocess", "docker", "grpc", "http"]
    # 类型特定字段...
    max_concurrent_calls: int = 10
```

---

## 6. 关键设计决策

### 6.1 Loop Controller ↔ Harness 协议

采用轻量级 HTTP/JSON 协议（复用现有 HTTP 服务基础设施），未来可扩展为 gRPC。

请求：

```json
POST /harness/v1/execute
{
  "tool": "run_shell_script",
  "arguments": {"command": "kubectl get pods"},
  "context": {
    "call_id": "...",
    "task_id": "...",
    "agent_id": "...",
    "user_id": "...",
    "tenant_id": "..."
  },
  "sandbox": {
    "timeout_seconds": 30,
    "max_output_bytes": 131072,
    "allowed_hosts": [],
    "allowed_paths": ["/tmp"]
  }
}
```

响应：

```json
{
  "status": "success",
  "content": "...",
  "error_code": null,
  "metadata": {"elapsed_ms": 123}
}
```

或：

```json
{
  "status": "error",
  "content": null,
  "error_code": "harness_sandbox_violation",
  "metadata": {"detail": "file access outside /tmp"}
}
```

### 6.2 身份与认证

- Harness 启动时持有由 Loop Controller 颁发的短期 token 或客户端证书；
- 每个执行请求中，Loop Controller 在 header 中携带调用者身份；
- Harness 验证请求来源，拒绝未授权调用；
- 未来版本支持 Harness 主动向 Loop Controller 拉取策略缓存。

### 6.3 HarnessExecutor 在 Loop Controller 内部的位置

`HarnessExecutor` 实现 `ToolExecutor` 协议，注册到 `ExecutorRegistry`。对 Loop Controller 核心而言，它和 `MCPExecutor`、`HTTPExecutor` 没有区别。

```python
class HarnessExecutor(ToolExecutor):
    async def execute(self, *, tool_name, arguments, context) -> ToolResult:
        backend = self._resolve_backend(tool_name)
        return await backend.execute(tool_name, arguments, context, sandbox)
```

### 6.4 Harness 后端抽象

```python
class HarnessBackend(Protocol):
    async def execute(
        self,
        tool_name: str,
        arguments: dict,
        context: ExecutionContext,
        sandbox: HarnessSandboxConfig,
    ) -> ToolResult: ...
```

实现：

- `SubprocessHarnessBackend`：启动本地 harness_server 子进程，通过 HTTP 调用；
- `DockerHarnessBackend`：通过 Docker SDK 启动容器，通过 HTTP 调用；
- `HTTPHarnessBackend`：连接远程已运行的 Harness 服务。

### 6.5 生命周期管理

- 子进程 Harness：Loop Controller 启动时拉起，关闭时终止；
- Docker Harness：每次调用启动一个容器，执行完成后销毁；
- 远程 HTTP Harness：只建立连接，不管理生命周期。

v0.25.0 优先实现 **子进程** 和 **远程 HTTP**，Docker 每次启动容器作为可选优化。

---

## 7. 风险与缓解

| 风险 | 缓解 |
|---|---|
| Harness 被绕过，Agent 直连真实工具 | Agent 必须运行在受控网络中，真实工具凭证不暴露给 Agent；Harness 网络白名单限制 |
| Harness 本身成为攻击面 | Harness 不暴露公网，只接受来自 Loop Controller 的请求；最小权限运行 |
| Docker 启动开销大 | 子进程 Harness 用于开发；Docker 用于生产；未来支持池化 |
| 子进程 Harness 与 Loop Controller 共享资源 | 仅用于开发/测试；生产必须使用 Docker 或远程 Harness |
| 输出过大导致 OOM | Harness 边读边截断，超过 `max_output_bytes` 立即返回错误 |
| 工具调用超时无法终止 | Harness 严格设置 `timeout_seconds`，超时时 kill 进程/容器 |

---

## 8. 验收标准

- `pytest tests/test_harness_executor.py`：HarnessExecutor 路由和协议转换测试通过；
- `pytest tests/test_harness_subprocess.py`：子进程 Harness 执行、超时、输出限制测试通过；
- `pytest tests/test_harness_docker.py`：Docker Harness 测试在有 Docker 环境下通过（可选标记）；
- `pytest tests/`：整体无回归，至少保持 v0.24.0 的通过数量；
- `ruff check src tests examples`：通过；
- `mypy src`：通过；
- `examples/contrib/harness/` 可在本地直接运行子进程 Harness；
- `KNOWN_LIMITATIONS.md` 和 `README.md` 更新 Harness 使用说明。

---

## 9. 最终目标

v0.25.0 完成后，Loop Controller 的工具调用路径应当是：

```text
Agent
  ↓
Loop Controller（身份/策略/审批/审计）
  ↓
  ├─ MCP 工具 → MCPExecutor → MCP Server
  ├─ HTTP 工具 → HTTPExecutor → API Endpoint
  └─ 高危/非标准工具 → HarnessExecutor → Harness（子进程/Docker/远程）
```

Loop Controller 仍然是统一的治理控制平面，Harness 成为可插拔的执行与隔离层。
