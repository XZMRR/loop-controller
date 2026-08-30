# v0.32.0 Agent 接入体验优化与 Harness 后端完善

> 一句话目标：**让愿意接入 Loop Controller 的 Agent 以最低成本获得完整治理，补齐 MCP Proxy 的运维与审计能力，并完成 v0.31.0 声明但未实现的 Docker / Isolated Subprocess Harness backend。**
>
> 范围限定：本版本只服务“主动接入”的 Agent；不配合 Agent 的进程外约束、运行时沙箱、SaaS 控制台均不在本版本范围内。

- 状态：**已完成**
- 前置版本：v0.31.0 外部工具执行沙箱（Harness）
- 版本性质：接入层体验与后端完善
- 核心范围：ToolGovernor SDK 友好化、MCP Proxy 运维工具、Harness backend 完善、接入示例
- 验证目标：pytest 全绿 / ruff 通过 / mypy 通过 / 提供至少 3 个 Agent 接入示例

---

## 1. 背景

v0.31.0 已经把 Harness 变成治理入口上的默认执行模式：任何经过 ToolGovernor / MCP Proxy / HTTP / gRPC 进入 Loop Controller 的敏感工具调用，默认走 Harness 沙箱。

但 v0.31.0 留下几个明显问题：

1. **接入成本高**：ToolGovernor SDK 要求 Agent 在每个调用点改成 `tool_governor.call(...)`，对复杂 Agent（如 Trae、LangChain Agent、FastAPI 服务）改造量大；
2. **MCP Proxy 接入隐性成本高**：需要把内置工具、Skill 工具、子 Agent 工具都包装成 MCP server，工程量大；
3. **MCP Proxy 缺少运维能力**：没有 admin/审计类 MCP tool，运维人员无法通过 MCP 入口查看后端状态、审批记录、证据链；
4. **Harness backend 不完整**：配置模型已声明 Docker / Isolated Subprocess backend，但运行时只支持 `http` / `subprocess`；
5. **缺少接入示例**：没有针对常见 Agent 框架（LangChain、FastAPI、函数式 Agent）的示例，新用户不知道从何开始。

v0.32.0 不新增治理规则，而是**把“接入 Loop Controller”这件事从工程挑战变成基础设施配置**。

---

## 2. 当前问题清单

### P0-1：ToolGovernor SDK 要求改造每个调用点

当前使用方式：

```python
# Agent 原来
result = write_file(path, content)

# 改造后
result = await tool_governor.call("write_file", {"path": path, "content": content})
```

问题：

- 调用点分散，复杂 Agent 可能有几十个；
- Trae 这类 IDE-Agent 改造风险高，容易改坏；
- Agent 开发者需要手动处理参数打包/解包、async/sync 边界。

### P0-2：MCP Proxy 缺少运维与审计工具

当前 MCP Proxy 只提供正向工具转发能力，缺少：

- 查询 Harness backend 健康状态；
- 对 backend drain/reset；
- 查询最近审批、decision 状态；
- 查询 evidence / audit 摘要；
- kill switch / revocation 管理。

这导致一旦 Agent 通过 MCP Proxy 接入，运维人员必须同时维护 HTTP / gRPC admin 入口，增加了运营复杂度。

### P0-3：Docker / Isolated Subprocess backend 未实现

v0.31.0 配置模型 `HarnessBackendConfig` 已声明：

- `docker` backend；
- `isolated_subprocess` backend。

但 `HarnessExecutor._build_backend()` 只实现了 `http` 和 `subprocess`。

风险：

- `subprocess` backend 隔离性弱，只能用于开发测试；
- 没有 Docker backend，生产环境必须依赖外部 HTTP Harness 服务；
- 配置模型与实际能力不一致，用户会被误导。

### P0-4：没有面向常见 Agent 框架的接入示例

常见 Agent 框架：

- LangChain / LangGraph；
- FastAPI 服务；
- 函数式工具 Agent；
- 已有统一 `tool_registry` 的自定义 Agent。

缺少示例导致：

- 用户不知道选哪种接入方式；
- 不清楚如何最小化改造；
- 不知道生产环境推荐配置。

### P1-1：接入方式选择没有指导

Agent 开发者面对 ToolGovernor SDK、MCP Proxy、HTTP、gRPC 四种入口，不知道：

- 哪种适合自己的 Agent；
- 每种方式的改造成本；
- 生产环境推荐组合。

---

## 3. 设计原则

### 3.1 接入成本最低优先

- 优先让 Agent 在**工具定义处**完成治理接入，而不是改造每个调用点；
- 优先支持“一行代码 hook 整个工具注册表”的方式；
- 只有当工具调用已经自然经过某个入口时，才推荐该入口（如 MCP tool 用 MCP Proxy，HTTP API 用 HTTP 入口）。

### 3.2 不新增接入方式，强化现有方式

v0.32.0 不引入新的接入协议，而是让现有接入方式更易用：

- ToolGovernor SDK：通过装饰器和注册表 Hook 降低改造量；
- MCP Proxy：补齐 admin/审计工具；
- HTTP / gRPC：保持现状，作为多语言和已有 HTTP 链路的选项。

### 3.3 MCP Proxy 是可选补充，不是默认推荐

- 只有当 Agent 的工具栈已经是 MCP 形态时，才推荐 MCP Proxy；
- 不要把复杂 Agent 的内置工具、Skill 工具、子 Agent 工具强制包装成 MCP server；
- 文档中明确说明各种接入方式的适用场景。

### 3.4 后端完善是生产前提

- 必须实现 Docker backend，让 Harness 能在生产环境单机部署；
- 必须实现 Isolated Subprocess backend，作为跨平台开发和 CI 兜底；
- `subprocess` backend 明确标注为“仅开发测试”。

### 3.5 示例即文档

- 每个接入方式至少有一个可运行的最小示例；
- 示例覆盖函数式 Agent、LangChain、FastAPI；
- 示例同时展示开发和生产配置差异。

---

## 4. 总体架构

```text
Agent 代码
    │
    ├─ 工具函数定义处：@governed 装饰器
    │
    ├─ Agent 启动时：GovernanceRuntime.hook_tool_registry(agent.tool_registry)
    │
    └─ 自然走 MCP 的工具：MCP Proxy
                │
                ▼
    ┌─────────────────────────────┐
    │     Loop Controller Runtime    │
    │  ┌─────────────────────────┐  │
    │  │   ToolGovernor / MCP    │  │
    │  │   Proxy / HTTP / gRPC   │  │
    │  └─────────────────────────┘  │
    │              │               │
    │              ▼               │
    │  ┌─────────────────────────┐  │
    │  │   ExecutionModeResolver │  │
    │  │   (harness_required)    │  │
    │  └─────────────────────────┘  │
    │              │               │
    │              ▼               │
    │  ┌─────────────────────────┐  │
    │  │   HarnessExecutor       │  │
    │  │   http / docker /       │  │
    │  │   isolated_subprocess   │  │
    │  └─────────────────────────┘  │
    │              │               │
    │              ▼               │
    │  ┌─────────────────────────┐  │
    │  │   Audit / Evidence      │  │
    │  └─────────────────────────┘  │
    └─────────────────────────────┘
```

---

## 5. 详细设计

### 5.1 `@governed` 装饰器

新增装饰器 `loop_controller.governed`：

```python
from loop_controller import governed

@governed(
    tool_name="write_file",           # 可选，默认用函数名
    mode="harness_required",          # 可选，默认从全局策略读取
    budget_unit="file_write",         # 可选
)
def write_file(path: str, content: str) -> dict:
    ...
```

行为：

- 保持原函数签名不变；
- 调用时自动把调用路由到 Loop Controller；
- 等待 Loop Controller 审批通过后执行；
- 返回原始返回值。

支持同步和异步函数：

```python
@governed(tool_name="fetch_url")
async def fetch_url(url: str) -> str:
    ...
```

实现要点：

- 装饰器内部使用 `GovernanceRuntime.current()` 获取当前运行时；
- 通过反射获取函数签名和参数名，自动打包参数；
- 返回结果自动解包为原始返回类型；
- 如果 Loop Controller 拒绝，抛出 `GovernanceDeniedError`。

### 5.2 工具注册表 Hook

新增 `GovernanceRuntime.hook_tool_registry(registry)`：

```python
from loop_controller import GovernanceRuntime

rt = GovernanceRuntime.from_config("loop-controller.yaml")
rt.hook_tool_registry(agent.tool_registry)
```

假设 Agent 已有统一工具注册表：

```python
class ToolRegistry:
    def register(self, name: str, fn: Callable): ...
    def get(self, name: str) -> Callable: ...
```

`hook_tool_registry` 行为：

- 遍历注册表中所有工具；
- 为每个工具自动应用 `@governed`；
- 替换原 callable 为治理后的 callable；
- 支持可选的排除列表和策略覆盖。

适用场景：

- Trae 类已有统一工具注册表的 Agent；
- LangChain `BaseToolkit` / `Tool` 注册表；
- 自定义 Agent 框架。

### 5.3 Agent 启动器

新增 `loop_controller.launch_agent`：

```python
from loop_controller import launch_agent

launch_agent(
    agent_module="my_agent.main:run",
    config="loop-controller.yaml",
    workspace="/tmp/agent-001",
)
```

行为：

- 读取 Loop Controller 配置；
- 启动 Agent 进程/线程；
- 注入治理上下文；
- 可选：限制 Agent 工作目录（不强制）。

这不是运行时沙箱，只是方便 Agent 在治理上下文中启动。

### 5.4 LangChain / LangGraph 集成

提供 `loop_controller.integrations.langchain`：

```python
from loop_controller.integrations.langchain import govern_langchain_tools

govern_langchain_tools(
    tools=tools,
    runtime=rt,
)
```

行为：

- 把 LangChain `BaseTool` 列表中的每个 tool 包装成治理版本；
- 保持 LangChain 的调用约定（`_run` / `_arun`）；
- 支持 `StructuredTool`、`AgentTool` 等常见类型。

### 5.5 FastAPI 集成

提供 `loop_controller.integrations.fastapi`：

```python
from fastapi import FastAPI
from loop_controller.integrations.fastapi import GovernedFastAPI

app = FastAPI()
governed_app = GovernedFastAPI(app, runtime=rt)
```

或在路由层面：

```python
from loop_controller.integrations.fastapi import governed_route

@app.post("/run-tool")
@governed_route(tool_name="run_tool")
async def run_tool(req: ToolRequest):
    ...
```

### 5.6 MCP Proxy admin / 审计工具

为 MCP Proxy 增加以下 MCP tool：

| MCP Tool | 功能 |
|---|---|
| `harness_backend_status` | 查询所有 backend 状态 |
| `harness_backend_drain` | drain 指定 backend |
| `harness_backend_reset` | reset 指定 backend |
| `list_recent_decisions` | 查询最近 decision |
| `get_decision_status` | 查询某个 decision 的状态 |
| `list_recent_audit_events` | 查询最近审计事件（元数据，不含敏感参数） |
| `get_evidence_summary` | 查询 evidence 摘要 |
| `trigger_kill_switch` | 触发 kill switch |
| `revoke_decision` | 吊销某个 decision |

这些工具本身也要走 Loop Controller 治理，需要 admin 权限审批。

实现要点：

- 在 `mcp_gateway.py` 中增加 `AdminToolRegistry`；
- 复用现有 `server.py` admin 端点的内部方法；
- 返回数据做脱敏，不暴露敏感参数和完整结果。

### 5.7 Docker backend 实现

新增 `src/loop_controller/executors/docker_harness_backend.py`：

```python
class DockerHarnessBackend:
    def __init__(self, config: DockerBackendConfig): ...

    async def execute(self, request: HarnessExecuteRequest) -> HarnessExecuteResponse:
        # 1. 创建一次性容器
        # 2. 挂载 allowed_paths 为只读/读写
        # 3. network_mode 默认 none
        # 4. 运行 harness runner
        # 5. 读取 stdout 作为 HarnessExecuteResponse
        # 6. 清理容器
```

配置示例：

```yaml
backends:
  docker_harness:
    type: docker
    image: loop-controller/harness-runner:latest
    network_mode: none
    mounts:
      - source: /data/output
        target: /data/output
        read_only: false
    max_concurrent_calls: 5
    acquire_timeout_seconds: 2
```

### 5.8 Isolated Subprocess backend 实现

新增 `src/loop_controller/executors/isolated_subprocess_harness.py`：

```python
class IsolatedSubprocessHarnessBackend:
    def __init__(self, config: IsolatedSubprocessBackendConfig): ...

    async def execute(self, request: HarnessExecuteRequest) -> HarnessExecuteResponse:
        # 1. 启动一个受限 Python 子进程
        # 2. 子进程只加载白名单 builtins
        # 3. 通过 IPC 传递请求
        # 4. 子进程在受限环境中执行工具代码
        # 5. 返回 HarnessExecuteResponse
```

限制：

- 优先支持 Linux / Windows / macOS 通用子进程隔离；
- 不保证完整容器级隔离；
- 用于开发、CI 和低敏感生产场景。

### 5.9 接入方式选择指南

在文档中明确：

| Agent 形态 | 推荐接入方式 | 原因 |
|---|---|---|
| Python 函数式 Agent | `@governed` 装饰器 | 改动最小 |
| 已有统一工具注册表 | `hook_tool_registry` | 一行代码接入 |
| LangChain / LangGraph | `govern_langchain_tools` | 框架原生集成 |
| FastAPI 服务 | `governed_route` 或 `GovernedFastAPI` | HTTP 调用链自然经过入口 |
| 工具栈已是 MCP | MCP Proxy | 无需改工具形态 |
| 多语言 Agent | HTTP / gRPC | 跨语言 |

---

## 6. 配置变更

### 6.1 新增 `config/agent_sdk.yaml`（可选）

```yaml
agent_sdk:
  auto_hook_registries: true
  default_mode: harness_required
  decorator_enabled: true

integrations:
  langchain:
    enabled: true
  fastapi:
    enabled: true
```

### 6.2 `config/harness_tools.yaml` 补充 backend 示例

```yaml
backends:
  docker_harness:
    type: docker
    image: loop-controller/harness-runner:latest
    network_mode: none

  isolated_subprocess:
    type: isolated_subprocess
    python_path: .venv/bin/python
    max_concurrent_calls: 3
```

---

## 7. 接口变更

### 7.1 新增

- `loop_controller.governed` 装饰器
- `GovernanceRuntime.hook_tool_registry()`
- `loop_controller.launch_agent()`
- `loop_controller.integrations.langchain.govern_langchain_tools()`
- `loop_controller.integrations.fastapi.GovernedFastAPI`
- `loop_controller.integrations.fastapi.governed_route()`
- `DockerHarnessBackend`
- `IsolatedSubprocessHarnessBackend`
- MCP Proxy admin tools：`harness_backend_status`、`harness_backend_drain`、`harness_backend_reset`、`list_recent_decisions`、`get_decision_status`、`list_recent_audit_events`、`get_evidence_summary`、`trigger_kill_switch`、`revoke_decision`

### 7.2 修改

- `HarnessExecutor._build_backend()` 支持 `docker` 和 `isolated_subprocess`
- `mcp_gateway.py` 增加 `AdminToolRegistry`
- `Runtime._build_executor_registry()` 默认启用 Docker / Isolated Subprocess backend

---

## 8. 测试计划

### 8.1 单元测试

- `tests/test_governed_decorator.py`
  - 同步函数装饰后调用走 Loop Controller；
  - 异步函数装饰后调用走 Loop Controller；
  - 参数自动打包/解包正确；
  - Loop Controller 拒绝时抛出正确异常。

- `tests/test_hook_tool_registry.py`
  - 注册表中所有工具被自动治理；
  - 排除列表生效；
  - 原始调用点无需修改。

- `tests/test_docker_harness_backend.py`（本地有 Docker 时运行）
  - 成功启动一次性容器；
  - `network_mode=none` 生效；
  - 只读挂载生效；
  - 返回 `HarnessExecuteResponse`。

- `tests/test_isolated_subprocess_harness.py`
  - 成功执行 `echo`；
  - 访问 `allowed_paths` 外文件被拒绝；
  - 超时返回 `harness_timeout`。

### 8.2 集成测试

- `tests/test_langchain_integration.py`
  - LangChain tool 被治理后，Agent 调用走 Harness；
  - 返回结果格式正确。

- `tests/test_fastapi_integration.py`
  - FastAPI 路由被治理后，HTTP 调用走 Harness；
  - 审批拒绝返回 403/合适状态码。

- `tests/test_mcp_proxy_admin_tools.py`
  - MCP Proxy admin tools 可查询 backend 状态；
  - drain/reset 生效；
  - audit 查询返回脱敏数据。

### 8.3 端到端测试

- 函数式 Agent 接入示例可完整跑通；
- LangChain Agent 接入示例可完整跑通；
- FastAPI 服务接入示例可完整跑通；
- Docker backend 在生产配置下可运行。

---

## 9. 验收标准

1. `python -m pytest -q` 全部通过；
2. `python -m ruff check src tests` 通过；
3. `python -m mypy src/loop_controller` 通过；
4. `@governed` 装饰器支持 sync/async 函数；
5. `hook_tool_registry` 能治理注册表中所有工具，调用点无需修改；
6. LangChain 和 FastAPI 集成测试通过；
7. Docker backend 在有 Docker 环境下可运行；
8. Isolated Subprocess backend 跨平台可运行；
9. MCP Proxy admin tools 至少实现 `harness_backend_status`、`harness_backend_drain`、`list_recent_audit_events`；
10. 提供 3 个可运行的 Agent 接入示例；
11. 接入方式选择文档清晰，明确各种方式的适用场景。

---

## 10. 非目标

- **不配合 Agent 的进程外约束**：Agent 必须主动接入，本版本不解决 Agent 进程内部绕过治理入口的问题；
- **运行时沙箱 / 应用级沙箱启动器**：`launch_agent` 只是方便启动，不提供强隔离；真正的运行时沙箱放到 v0.36.0 或企业版；
- **SaaS 控制台 / 多租户**：控制平面仍部署在客户本地；
- **新增接入协议**：不新增第四种接入方式；
- **强制把所有工具改成 MCP**：MCP Proxy 只是可选项；
- **内核级 / 系统调用级拦截**：不属于开源核心范围。

---

## 11. 风险与回退

| 风险 | 缓解 |
|---|---|
| `@governed` 装饰器改变原函数签名或类型提示 | 充分单元测试 + mypy 测试；保留原函数 `__signature__` |
| `hook_tool_registry` 与 Agent 自己的装饰器冲突 | 支持排除列表；提供详细调试日志 |
| Docker backend 在 Windows/macOS 上体验不一致 | 用 Isolated Subprocess backend 作为跨平台兜底；文档明确说明 |
| MCP Proxy admin tools 泄露敏感信息 | 返回数据脱敏；admin tools 自身也要审批 |
| LangChain / FastAPI 版本兼容性问题 | 集成测试覆盖主流版本；文档说明支持版本 |

---

## 12. 备注

- 本版本重点是“接入体验”，不是“治理能力增强”。v0.31.0 已经把治理能力（Harness 默认化、沙箱校验、证据回传、健康熔断）做扎实了，v0.32.0 让这些能力更容易被 Agent 使用；
- MCP Proxy 在本版本补齐 admin tools 后，可以作为 MCP-native Agent 的完整接入方案；
- 接入示例要同时展示“最小改造”和“生产配置”，帮助用户快速判断 ROI；
- v0.32.0 完成后，应该能够回答潜在客户的核心问题：“我的 Agent 要改多少代码才能用 Loop Controller？”
