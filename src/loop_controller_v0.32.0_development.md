# v0.32.0 接入方式收敛与审批后自动重试

> 一句话目标：**把 Loop Controller 的接入面收敛到三种工具接入方式，确认 `@governed` 主动接入为主路线；补齐 `@governed` 审批后自动重试能力，使 Agent 在 `require_approval` 后能够阻塞等待审批结果并继续执行。**
>
> 范围限定：本版本只完善"主动接入"与"网关接入"两条线；Agent 交互治理（A2A/Go 内核）不在本版本范围内。

- 状态：**已完成**
- 前置版本：v0.31.0 外部工具执行沙箱（Harness）
- 版本性质：接入层收敛与主路线体验收尾
- 核心范围：接入方式收敛、`@governed` 审批后自动重试、集成测试补强
- 验证目标：`pytest tests/integration -m integration -v` 全绿、`pytest tests -m "not integration" -q` 无新增失败、ruff 通过

**v0.32.0 验证结果：**

- `pytest tests/integration -m integration -q`：**22 passed**（当前环境已安装 `langchain_core`；未安装时为 21 passed, 1 skipped）。
  - `@governed` 端到端、hook 注册表、审批流、**审批后自动重试**、审计、多步骤工作流、session 隔离、本地函数异常/超时；
  - MCP Proxy 工具发现/执行、require_approval、deny、参数错误、approval_status、admin status；
  - LangChain 单 tool / 多步工作流 / require_approval（作为 `@governed` 的示例应用）。
- `pytest tests -m "not integration" -q`：**738 passed, 4 skipped, 22 deselected**。
- `python -m ruff check src tests`：**All checks passed**。

---

## 1. 背景

v0.31.0 已经把 Harness 变成治理入口上的默认执行模式。v0.32.0 前期完成了 `@governed` 装饰器、MCP Proxy、LangChain/FastAPI 示例、Harness backend 等能力。

但随着接入方式增多，项目出现两个必须解决的问题：

1. **接入面过于发散**：当前存在 `@governed`、MCP Proxy、HTTP、gRPC、FastAPI、LangChain 六种接入形态，导致用户选择困难、文档边界不清、维护成本高。
2. **`@governed` 主路线未收尾**：`require_approval` 时只返回 `GovernanceResult`，Agent 需要手动处理审批恢复，没有内置的阻塞等待/自动重试机制，真实 Agent 难以使用。

v0.32.0 后半段的目标是：

- **收敛接入方式**：只保留 `@governed`（主路线）、MCP Proxy（网关）、HTTP REST API（跨语言）。
- **移除冗余**：删除 FastAPI 集成、删除 gRPC 服务、将 LangChain 集成降级为可选示例。
- **补齐主路线体验**：实现 `@governed` 审批后阻塞等待/自动重试，让 Agent 代码在 `require_approval` 后像普通函数一样继续执行。

---

## 2. 当前问题清单

### P0-1：接入方式过多，边界不清

当前接入方式：

- `@governed`：Python 主动接入
- MCP Proxy：标准 MCP Client 网关
- HTTP REST API：跨语言接入
- gRPC：可选跨语言接入
- FastAPI 集成：HTTP 路由装饰器
- LangChain 集成：框架适配

问题：

- 用户不知道选哪个；
- 文档需要维护六种方式；
- FastAPI/gRPC 与 HTTP/MCP 功能重叠；
- 与后续 Agent 交互治理（A2A/Go 内核）的边界不清。

### P0-2：`@governed` 审批恢复不完整

当前行为：

```python
@governed(tool_name="send_email")
async def send_email(...):
    ...

result = await send_email(...)
if result.status == "require_approval":
    # Agent 需要手动轮询 approval_status，然后在某处重新调用
```

问题：

- Agent 代码需要分叉处理 `allow` 和 `require_approval`；
- 审批通过后 Agent 需要主动重试；
- 没有统一的等待/恢复抽象。

### P1-1：FastAPI 集成价值低

`GovernedFastAPI` 当前基本是空壳，`governed_route` 把 HTTP body 整体当工具参数，与真实 FastAPI 用法冲突。

### P1-2：gRPC 服务维护成本高

gRPC 是可选依赖，管理接口未与 HTTP REST 对齐，HTTP 已覆盖同样场景。

### P1-3：LangChain 集成不应作为独立模块

LangChain 集成只是 `@governed` 在 LangChain 工具上的一种应用，不应与 `@governed` 并列。

---

## 3. 设计原则

### 3.1 接入方式收敛为三种

| 接入方式 | 面向 Agent | 定位 |
|---|---|---|
| `@governed` | Python 自研 Agent | **主路线：主动接入** |
| MCP Proxy | 标准 MCP Client（Cursor、Claude Desktop） | 网关/强制约束 |
| HTTP REST API | 跨语言 Agent / 遗留系统 | 通用协议接入 |

其他方式：

- FastAPI 集成：移除；HTTP REST API 已覆盖服务端接入。
- gRPC 服务：移除；HTTP 已覆盖跨语言接入。
- LangChain 集成：降级为 `examples/` 或 `tests/integration/` 中的可选示例，不作为核心包维护。

### 3.2 `@governed` 是主路线，必须能跑通完整审批流

Agent 调用被治理函数时，应该像调用普通函数一样简单：

```python
result = await send_email(...)  # require_approval 时自动等待审批并返回执行结果
```

### 3.3 不破坏现有稳定能力

MCP Proxy、HTTP REST API、Harness backend 保持可用；清理工作通过删除模块和依赖完成，不改动核心治理逻辑。

---

## 4. 总体架构

```text
Agent 代码
    │
    ├─ Python 自研 Agent：@governed 装饰器 / hook_tool_registry
    │
    ├─ 标准 MCP Client Agent：MCP Proxy
    │
    └─ 跨语言 Agent：HTTP REST API
                │
                ▼
    ┌─────────────────────────────┐
    │     Loop Controller Runtime    │
    │  ┌─────────────────────────┐  │
    │  │   ToolGovernor / MCP    │  │
    │  │   Proxy / HTTP          │  │
    │  └─────────────────────────┘  │
    │              │               │
    │              ▼               │
    │  ┌─────────────────────────┐  │
    │  │   Checkpoint / R2       │  │
    │  │   OPA / Rego 策略判定    │  │
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

### 5.1 接入方式收敛

#### 5.1.1 移除 FastAPI 集成

- 删除 `src/loop_controller/integrations/fastapi.py`
- 删除 `tests/integrations/test_fastapi.py`（如有）
- 从 `pyproject.toml` 移除 `fastapi` 相关可选依赖
- 从 `src/loop_controller/integrations/__init__.py` 移除导出
- 从 README/开发文档/KNOWN_LIMITATIONS 移除 FastAPI 接入说明

#### 5.1.2 移除 gRPC 服务

- 删除 `src/loop_controller/grpc_server.py`
- 删除 `src/loop_controller/grpc_client.py`
- 从 `cli.py` 移除 `grpc-server` 子命令
- 从 `pyproject.toml` 移除 `grpcio`、`grpcio-tools` 等依赖
- 删除 gRPC 相关测试
- 更新 README/开发文档

#### 5.1.3 LangChain 集成降级为可选示例

- 将 `src/loop_controller/integrations/langchain.py` 移动到 `examples/integrations/langchain_example.py`
- 保留 `tests/integration/test_langchain_agent.py` 作为集成测试，但标记为可选依赖
- 从 `src/loop_controller/integrations/__init__.py` 移除导出
- 文档中说明：LangChain 用户可参考示例，但推荐方式仍然是把 LangChain tool 用 `@governed` 包装

#### 5.1.4 接入方式选择指南（收敛后）

| Agent 形态 | 推荐接入方式 |
|---|---|
| Python 函数式 Agent / 自研 Agent | `@governed` 装饰器 |
| 已有统一工具注册表 | `hook_tool_registry` |
| 标准 MCP Client（Cursor、Claude Desktop、Windsurf） | MCP Proxy |
| 跨语言 Agent / 遗留系统 | HTTP REST API |
| LangChain Agent | 参考 `examples/integrations/langchain_example.py` |

### 5.2 `@governed` 审批后自动重试

#### 5.2.1 设计目标

Agent 调用 `@governed` 函数时：

- `allow`：立即返回执行结果（当前已实现）
- `require_approval`：默认阻塞等待审批，审批通过后自动重试并返回执行结果
- `deny` / `blocked` / `error`：抛出 `GovernanceDeniedError`（当前已实现）

#### 5.2.2 新增 API

在 `GovernanceResult` 上增加等待方法：

```python
class GovernanceResult:
    ...

    async def wait_for_approval(
        self,
        *,
        timeout: float | None = 60.0,
        poll_interval: float = 1.0,
    ) -> "GovernanceResult":
        """阻塞等待审批完成，返回最终执行结果或 raise GovernanceDeniedError。"""

    async def retry_after_approval(
        self,
        *,
        timeout: float | None = 60.0,
        poll_interval: float = 1.0,
    ) -> Any:
        """等待审批通过后，使用原 Decision 重试执行并返回执行结果。"""
```

在 `@governed` 装饰器上增加参数：

```python
@governed(tool_name="send_email", wait_for_approval=True)
async def send_email(...):
    ...
```

- `wait_for_approval=True`：遇到 `require_approval` 时自动阻塞等待，审批通过后自动重试，最终返回执行结果
- `wait_for_approval=False`（默认或保持当前行为）：返回 `GovernanceResult`，由 Agent 自己处理

#### 5.2.3 实现要点

1. `GovernanceResult` 保存原始 `ActionProposal`、`Decision`、`tool_name`、`arguments` 等重试所需信息
2. `wait_for_approval` 轮询 `approval_service` 或 `ApprovalStore` 获取 `ApprovalRecord`
3. 审批通过后，使用原 Decision 的 `decision_id` 调用 `controller.execute_with_decision(decision_id, ...)` 或等效接口
4. 审批被拒或超时：抛出 `GovernanceDeniedError`
5. 在 `@governed` 装饰器中，根据 `wait_for_approval` 参数自动调用 `retry_after_approval`

#### 5.2.4 示例

```python
@governed(tool_name="send_email", wait_for_approval=True)
async def send_email(to: str, subject: str, body: str) -> dict[str, str]:
    return {"status": "sent"}

# 调用方像普通函数一样使用
result = await send_email("bob@company.com", "hi", "body")
# 如果触发审批，会自动等待审批通过并返回执行结果
```

### 5.3 `@governed` 保留治理参数

调用 `@governed` 函数时，可以通过以 `_loop_controller_` 为前缀的关键字参数传入治理上下文，这些参数**不会**被打包为工具参数，而是直接传给 `GovernanceRuntime.call()`：

| 参数 | 说明 |
|---|---|
| `_loop_controller_session_id` | 显式指定 session_id；需预先存在，否则 `create_task` 会报错 |
| `_loop_controller_task_id` | 显式指定 task_id；需预先存在 |
| `_loop_controller_task_context` | 覆盖本次调用的任务上下文 |

---

## 6. 配置变更

### 6.1 移除 FastAPI / gRPC 可选依赖

从 `pyproject.toml` 移除或标记为 deprecated：

```toml
# 移除
[project.optional-dependencies]
fastapi = ["fastapi>=0.100.0"]
grpc = ["grpcio>=1.60.0", "grpcio-tools>=1.60.0"]
```

保留：

```toml
[project.optional-dependencies]
langchain = ["langchain_core>=0.1.0"]  # 可选示例依赖
```

### 6.2 无需新增配置

审批后自动重试通过 API 参数控制，不引入新配置文件。

---

## 7. 接口变更

### 7.1 移除

- `loop_controller.integrations.fastapi.GovernedFastAPI`
- `loop_controller.integrations.fastapi.governed_route`
- `loop_controller.grpc_server`
- `loop_controller.grpc_client`
- CLI `grpc-server` 子命令

### 7.2 修改

- `loop_controller.integrations.langchain.govern_langchain_tools` 移动到 `examples/integrations/langchain_example.py`
- `GovernanceResult` 新增 `wait_for_approval()`、`retry_after_approval()` 方法
- `@governed` 装饰器新增 `wait_for_approval` 参数

### 7.3 保留

- `loop_controller.governed`
- `GovernanceRuntime.hook_tool_registry()`
- `loop_controller.launch_agent()`
- MCP Proxy admin tools
- HTTP REST API

---

## 8. 测试计划

### 8.1 单元测试

- `tests/test_agent_sdk.py`
  - `GovernanceResult.wait_for_approval()` 超时抛出 `GovernanceDeniedError`；
  - `GovernanceResult.retry_after_approval()` 审批通过后返回执行结果；
  - `@governed(wait_for_approval=True)` 自动等待审批并返回结果；
  - `@governed(wait_for_approval=False)` 保持当前行为，返回 `GovernanceResult`。

### 8.2 集成测试

- `tests/integration/test_functional_agent.py`
  - `@governed` 端到端调用真实 Loop Controller；
  - `allow` 时返回实际执行结果；
  - `wait_for_approval=True` 时触发审批、审批通过后自动返回执行结果；
  - 审计记录正确写入。

- `tests/integration/test_mcp_proxy.py`
  - MCP Proxy 端到端路径保持通过；
  - require_approval、deny、参数错误路径保持通过。

- `tests/integration/test_langchain_agent.py`
  - 作为可选依赖测试保留；
  - 未安装 `langchain_core` 时自动 skip。

### 8.3 清理验证

- 确认 `src/loop_controller/integrations/fastapi.py` 已删除；
- 确认 `src/loop_controller/grpc_server.py`、`grpc_client.py` 已删除；
- 确认 `pyproject.toml` 中 `fastapi`、`grpcio` 相关可选依赖已移除；
- 确认 `python -m loop_controller.cli grpc-server --help` 不再存在。

---

## 9. 验收标准

1. `pytest tests/integration -m integration -v` 通过（保留 21 passed；`langchain_core` 未安装时 20 passed, 1 skipped）；
2. `pytest tests -m "not integration" -q` 无新增失败；
3. `python -m ruff check src tests` 通过；
4. FastAPI 集成已从代码库和文档中移除；
5. gRPC 服务已从代码库和文档中移除；
6. LangChain 集成已移动到 `examples/integrations/`，不作为核心包导出；
7. `@governed(wait_for_approval=True)` 能够在触发审批后阻塞等待并返回执行结果；
8. `@governed` 对 `allow` / `deny` / `blocked` / `error` 的语义保持不变；
9. README 和开发文档中的接入形态表格已更新为三种接入方式；
10. KNOWN_LIMITATIONS 已更新，说明 FastAPI/gRPC 已移除、LangChain 为可选示例。

---

## 10. 非目标

- **新增接入方式**：本版本不新增 A2A、GraphQL、WebSocket 等接入方式；
- **Agent 交互治理（Go 内核）**：这是 v0.34+ 的方向，本版本只收敛工具接入面；
- **运行时沙箱 / 应用级沙箱启动器**：`launch_agent` 只是方便启动，不提供强隔离；
- **SaaS 控制台 / 多租户**：控制平面仍部署在客户本地；
- **审批 UI 完整实现**：`wait_for_approval` 通过轮询 `approval_service` / `ApprovalStore` 实现，不依赖前端。

---

## 11. 风险与回退

| 风险 | 缓解 |
|---|---|
| 移除 FastAPI/gRPC 影响已有用户 | 本版本先标记 deprecated 并在文档中说明；v0.33.0 正式移除 |
| `wait_for_approval` 默认行为改变破坏现有测试 | 默认保持 `wait_for_approval=False`，显式开启才阻塞 |
| 审批通过后重试时参数状态不一致 | 使用原始 `ActionProposal` 和 `Decision` 重试，不重新构造参数 |
| 轮询审批增加服务负载 | 支持可配置 `poll_interval`；长期可替换为事件驱动 |
| LangChain 示例移出核心包导致测试 skip | 在 CI 中保留可选依赖安装步骤 |

---

## 12. 备注

- 本版本后半段重点是"接入收敛"和"主路线收尾"；
- `@governed` 跑稳后，v0.33.0 可以专注扩展 `hook_tool_registry` 对真实框架工具注册表的支持；
- v0.34.0 及以后将进入 Agent 交互治理层（Go 内核 + A2A），与 Python 工具治理层分层协作。
