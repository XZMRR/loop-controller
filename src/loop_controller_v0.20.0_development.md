# v0.20.0 开发文档：面向 MCP 工具的可信身份控制平面与执行器抽象基座

## 1. 目标

v0.19.0 已经让 Loop Controller 具备 HTTP 服务、gRPC 服务、MCP Proxy、SSE 实时审批通道和完整的策略/审批/审计闭环。v0.20.0 的目标是：

> 在 MCP 工具场景下，建立“可信身份控制平面”与“可插拔执行器抽象基座”，把项目从“五种接入形态的实验项目”收敛为可生产部署的治理控制平面。

v0.20.0 只做三件事：

1. **接入形态收敛**：把五种形态收敛为三种官方形态，Framework Adapters 移出核心包。
2. **身份认证**：让 `agent_id` 不再是请求体里的字符串，而是来自可验证的身份凭证。
3. **执行层抽象**：把 `MCPGateway` 抽象为 `ToolExecutor`，v0.20.0 只实现 `MCPExecutor`，但为后续 HTTP Executor、Sandboxed Local Function Executor 预留接口。

### 1.1 关于长期定位

v0.20.0 的 **MCP-only** 是**阶段性边界**，不是项目终极目标。Loop Controller 的长期定位是 **“企业 Agent 动作治理控制平面”**，覆盖 MCP 工具、HTTP API、本地函数、Shell、文件系统、浏览器等所有可能产生副作用的 Agent 动作。`ToolExecutor` / `ExecutorRegistry` 抽象正是为了让后续执行器类型能无缝接入，而不需要返工 `Checkpoint` / 策略 / 审批 / 审计等核心链路。

## 2. 设计原则

1. **MCP-only 是阶段性边界**：v0.20.0 只实现 MCP 工具执行，但 `ToolExecutor` 抽象为 HTTP / 本地函数 / 沙箱执行器预留接口。
2. **控制平面 > 网关**：HTTP/gRPC/MCP Proxy 是部署形态；Loop Controller 的本质是“Agent 动作治理控制平面”，不只是请求网关。
3. **身份优先**：所有生产入口默认要求身份认证，未认证请求被拒绝。
4. **向后兼容**：现有策略、审批、审计、预算、风险状态核心逻辑不变；执行层抽象后行为与 v0.19.0 一致。
5. **为未来留接口**：`ExecutorRegistry` + `ToolExecutor` 抽象让 v0.21.0 的 HTTP Executor 可以零侵入接入。
6. **治理强度分层**：明确区分“协作式拦截”“代理执行”“垄断出口”三种治理强度，不夸大弱形态的安全承诺。

## 3. 新增/修改文件

| 文件 | 操作 | 说明 |
|---|---|---|
| `src/loop_controller/executors/__init__.py` | 新增 | Executor 包入口 |
| `src/loop_controller/executors/base.py` | 新增 | `ToolExecutor` / `ExecutionContext` / `ExecutorRegistry` 抽象 |
| `src/loop_controller/executors/mcp_executor.py` | 新增 | `MCPExecutor`，复用现有 `MCPGateway` 能力 |
| `src/loop_controller/mcp_gateway.py` | 修改 | 核心转发逻辑不变，作为 `MCPExecutor` 内部实现 |
| `src/loop_controller/checkpoint.py` | 修改 | `forward()` 改为从 `ExecutorRegistry` 路由 |
| `src/loop_controller/runtime.py` | 修改 | 注入 `ExecutorRegistry`，默认只注册 `MCPExecutor` |
| `src/loop_controller/identity/__init__.py` | 新增 | 身份包入口 |
| `src/loop_controller/identity/models.py` | 新增 | `AgentIdentity` / `IdentityCredential` / `IdentityIssueRequest` 模型 |
| `src/loop_controller/identity/provider.py` | 新增 | `IdentityProvider` 协议 |
| `src/loop_controller/identity/static.py` | 新增 | `ConfigIdentityProvider`，静态 token（仅开发/测试） |
| `src/loop_controller/identity/jwt.py` | 新增 | `JWTIdentityProvider`，生产用 JWT 验证 |
| `src/loop_controller/identity/mtls.py` | 新增 | `MTLSIdentityProvider`，生产用 mTLS 证书身份映射 |
| `src/loop_controller/server.py` | 修改 | 从 `Authorization` header 提取并验证身份；请求体 `agent_id` 只作校验 |
| `src/loop_controller/grpc_server.py` | 修改 | 支持 mTLS，从客户端证书提取身份 |
| `src/loop_controller/proxy_server.py` | 修改 | 支持 `--identity-token` / `--identity-cert` |
| `src/loop_controller/cli.py` | 修改 | `lc proxy` 新增身份参数；`lc server` / `lc grpc-server` 身份配置 |
| `src/loop_controller/__init__.py` | 修改 | 移除 Framework Adapter 导出 |
| `src/loop_controller/adapters/` | 移动 | 移到 `examples/contrib/adapters/`，不再属于核心包 |
| `config/agents.yaml` | 修改 | 增加 `identity` 字段（issuer / subject / public_key） |
| `config/identity.yaml` | 新增 | 身份 Provider 配置 |
| `config/entrypoints.yaml` | 新增 | HTTP/gRPC/MCP Proxy 入口认证方式配置 |
| `pyproject.toml` | 修改 | 移除 `[langchain]`、`[openai-agents]`、`[autogen]` 可选依赖 |
| `src/KNOWN_LIMITATIONS.md` | 修改 | 增加 v0.20.0 边界说明 |
| `src/README.md` | 修改 | 更新为三种官方形态 |
| `tests/test_identity.py` | 新增 | JWT / static provider 测试 |
| `tests/test_executor_registry.py` | 新增 | Executor 路由测试 |
| `tests/test_server_auth.py` | 新增 | HTTP 入口身份认证测试 |
| `tests/test_grpc_mtls.py` | 新增 | gRPC mTLS 身份提取测试 |
| `src/development_log.md` | 修改 | 追加 v0.20.0 记录 |
| `src/loop_controller_v0.20.0_development.md` | 新增 | 本文档 |

## 4. 接入形态收敛

### 4.1 三种官方形态

v0.20.0 之后，核心包只支持三种接入方式：

| 形态 | 定位 | 生产可用性 |
|---|---|---|
| **HTTP 服务** | 生产主推入口 | 是 |
| **gRPC 服务** | 生产主推入口（内部服务间） | 是 |
| **MCP Proxy** | 兼容入口（外部 MCP Client） | 是 |
| **Python SDK / ToolGovernor** | 内部开发/可信 Agent | 协作式，需声明弱边界 |
| **Framework Adapters** | 示例/迁移工具 | 移出核心包 |

### 4.2 Framework Adapters 移出核心包

- 将 `src/loop_controller/adapters/` 整体移动到 `examples/contrib/adapters/`；
- 保留 LangChain / OpenAI Agents / AutoGen 三个 adapter 作为示例代码；
- 不再在 `pyproject.toml` 注册为 `[langchain]`、`[openai-agents]`、`[autogen]` 可选依赖；
- `src/loop_controller/__init__.py` 不再导出 adapter 相关符号；
- 文档明确：Adapter 是“开发便利示例”，不是企业级强治理形态。

### 4.3 Python SDK 定位

`LoopController` / `ToolGovernor` 保留在核心包，但文档中标记为：

> 适用于企业内部可信 Python Agent 或开发调试。由于与 Agent 同进程，无法防止 Agent 进程绕过治理，不提供工具级实时阻断承诺。

### 4.4 治理强度分层

v0.20.0 的三种官方形态对应 [`src/answer.md`](file:///c:/Users/26343/Desktop/loop-controller/src/answer.md) 中的三种治理权获取方式：

| 形态 | 治理方式 | 强度 | Agent 妥协 | 可承诺 |
|---|---|---|---|---|
| **Python SDK / ToolGovernor** | 协作式拦截 | 弱 | Agent 改代码调用 SDK | 仅审计，不承诺阻断 |
| **HTTP/gRPC 服务 + MCP Proxy** | 代理执行 | 中~强 | Agent 改配置/改 endpoint，交出凭证 | 可承诺“所有经网关的工具调用被治理” |
| **Agent Harness / Runtime（远期）** | 垄断出口 | 最强 | Agent 跑在企业沙箱中 | 可承诺“Agent 无法绕过治理” |

v0.20.0 做到第二层（代理执行）对 MCP/HTTP 工具的强治理；第三层（垄断出口）需要 v0.23+ 的 Agent Harness / Runtime。

## 5. 身份认证

### 5.1 核心模型

```python
class AgentIdentity(BaseModel):
    agent_id: str
    user_id: str
    harness_id: str | None = None
    profile_id: str
    issued_at: datetime
    expires_at: datetime

class IdentityCredential(BaseModel):
    token: str | None = None          # JWT / static token
    cert_cn: str | None = None        # mTLS 证书 CN
    cert_sans: list[str] = []         # mTLS 证书 SAN

class IdentityProvider(Protocol):
    async def verify(self, credential: IdentityCredential) -> AgentIdentity | None: ...
    async def issue(self, request: IdentityIssueRequest) -> AgentIdentity | None: ...
```

### 5.2 Provider 实现

#### 5.2.1 `ConfigIdentityProvider`（静态模式，开发/测试用）

- 使用本地静态 token 表（`config/identity.yaml` 中 `static.allowed_tokens`）；
- 验证请求中的 `token` 是否在允许列表中，并映射到 `agents.yaml` 中的 Agent；
- **仅用于开发和测试环境，生产环境必须切换为 JWT / mTLS。**

#### 5.2.2 `JWTIdentityProvider`（生产用）

- 验证 JWT 签名（来自 `config/identity.yaml` 的 `issuer` / `jwks_url` 或 `public_key`）；
- 检查 `iss`、`aud`、`exp`、`iat`；
- 从 JWT claims 提取 `agent_id`、`user_id`、`harness_id`；
- 映射到 `agents.yaml` 中的 `profile_id` 和 `owner_id`。

### 5.3 各入口身份认证

#### 5.3.1 HTTP 服务

- 默认要求 `Authorization: Bearer <jwt>`；
- 从 JWT 提取 `agent_id` / `user_id`，请求体中的 `agent_id` / `user_id` 只作一致性校验；
- Admin API（`lc approvals` 相关）保留全局 API key 用于运维；
- 未认证请求返回 401。

#### 5.3.2 gRPC 服务

- 强制启用 TLS；
- 生产部署启用 mTLS；
- 从客户端证书 CN/SAN 提取证书身份；
- 通过 `config/identity.yaml` 中的 `cert_subject_template` 或 `cert_mappings` 把证书身份映射为内部 `agent_id` / `harness_id`；
- 请求体中的 `agent_id` 只作一致性校验。

#### 5.3.3 MCP Proxy

**SSE 模式（生产推荐）**：

- `lc proxy` 新增 `--identity-cert` / `--identity-key` 参数；
- 使用 mTLS，Loop Controller 侧验证客户端证书，从 CN/SAN 提取 `agent_id`；
- 身份凭证由部署平台/Harness 注入到 Proxy 进程，Agent 进程不可读取。

**stdio 模式（仅开发/测试）**：

- `lc proxy` 新增 `--identity-token` 参数；
- 启动时从环境变量 `LOOP_CONTROLLER_IDENTITY_TOKEN` 读取并验证 token；
- **限制**：stdio MCP Client 通常与 Agent 同进程或近距离部署，Agent 可能读取到环境变量中的 token；
- 因此 **stdio 模式仅用于开发/测试，生产环境必须改用 SSE + mTLS**。

**通用要求**：

- 启动时验证身份凭证，无效则拒绝启动；
- 运行期间不再接受身份变更。

### 5.4 配置示例

```yaml
# config/identity.yaml
identity:
  provider: jwt
  jwt:
    issuer: https://auth.company.com
    audience: loop-controller
    jwks_url: https://auth.company.com/.well-known/jwks.json
    claim_mappings:
      agent_id: agent_id
      user_id: user_id
      harness_id: harness_id
  # 静态 token 模式（仅开发/测试）
  static:
    allowed_tokens:
      - token: dev-token-researcher-001
        agent_id: researcher_001
        user_id: alice
```

```yaml
# config/agents.yaml
agents:
  - agent_id: researcher_001
    name: Research Assistant
    profile_id: research_assistant_v1
    owner_id: zhang_manager
    identity:
      issuer: https://auth.company.com
      subject: agent://research-assistant/prod-001
      # mTLS 场景下，证书 CN/SAN 匹配规则
      cert_subject_template: "agent-researcher-prod-{harness_id}"
```

### 5.5 证书身份映射

gRPC / MCP Proxy SSE 的 mTLS 场景下，证书 CN/SAN 通常不是直接等于内部 `agent_id`。v0.20.0 支持两种映射方式：

#### 方式一：模板映射（推荐）

在 `agents.yaml` 中配置 `cert_subject_template`：

```yaml
agents:
  - agent_id: researcher_001
    identity:
      cert_subject_template: "agent-researcher-{harness_id}"
```

运行时：

- 从证书 CN 提取 `agent-researcher-prod-001`；
- 匹配模板，得到 `agent_id = researcher_001`，`harness_id = prod-001`。

#### 方式二：显式映射表

在 `config/identity.yaml` 中配置 `cert_mappings`：

```yaml
identity:
  mtls:
    cert_mappings:
      - subject: "CN=agent-researcher-prod-001,O=company"
        agent_id: researcher_001
        harness_id: prod-001
```

**原则**：内部 `agent_id` 永远由 Loop Controller 侧的配置和映射规则决定，不由客户端声明。

## 6. 执行层抽象

### 6.1 `ToolExecutor` 协议

```python
class ExecutionContext(BaseModel):
    call_id: str
    task_id: str
    agent_id: str
    user_id: str
    session_id: str | None = None

class ToolExecutor(Protocol):
    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: ExecutionContext,
    ) -> ToolResult: ...

    async def list_tools(self, profile: CapabilityProfile) -> list[Tool]: ...
```

### 6.2 `ExecutorRegistry`

```python
class ExecutorRegistry:
    def register(self, tool_name: str, executor: ToolExecutor) -> None: ...
    def get_executor(self, tool_name: str) -> ToolExecutor: ...
```

v0.20.0 中，`config/mcp_servers.yaml` 解析后，每个 `tool_mapping` 条目对应的 `canonical_name` 注册到 `ExecutorRegistry`，统一指向 `MCPExecutor`。

### 6.3 `MCPExecutor`

- 把现有 `MCPGateway` 包装成 `ToolExecutor` 实现；
- 保留 stdio / SSE MCP server 生命周期管理；
- `execute()` 内部调用原 `MCPGateway.call_tool()`；
- `list_tools()` 内部调用原 `MCPGateway.list_tools()`。

### 6.4 `Checkpoint.forward()` 改造

```python
# 改造前
result = await self._gateway.call_tool(tool_name, args, call_id, task_id)

# 改造后
executor = self._executor_registry.get_executor(tool_name)
result = await executor.execute(
    tool_name=tool_name,
    arguments=effective_args,
    context=ExecutionContext(
        call_id=proposal.call_id,
        task_id=proposal.task_id,
        agent_id=proposal.agent_id,
        user_id=task.user_id,
        session_id=session_id,
    ),
)
```

v0.20.0 行为完全不变，但内部结构为后续扩展打开口子。

### 6.5 为什么现在只保留 MCPExecutor

- 当前所有工具都是 MCP；
- HTTP Executor 需要新的凭证管理、URL 模板、响应映射，超出 v0.20.0 范围；
- 本地函数 / 沙箱执行器需要运行时隔离基础设施，属于远期工作。

## 7. Agent 配置管理

### 7.1 配置分层

v0.20.0 的 Agent 配置管理限定在**网关层**：

| 配置 | 文件 | 管理者 | 作用 |
|---|---|---|---|
| Agent 身份 | `config/agents.yaml` | 企业管理员 | 声明 agent_id / profile / owner / 身份 issuer |
| 身份 Provider | `config/identity.yaml` | 平台运维 | 配置 JWT / mTLS 验证方式 |
| 入口认证 | `config/entrypoints.yaml` | 平台运维 | 配置 HTTP/gRPC/MCP Proxy 的认证方式 |
| 岗位权限 | `config/profiles.yaml` | 安全团队 | 定义工具白名单、预算、审批阈值 |
| 工具后端 | `config/mcp_servers.yaml` | 平台运维 | 注册 MCP Server 和工具映射 |
| 审批规则 | `config/approval.yaml` | 企业管理员 | 定义 escalation target |
| 策略 | `policies/default.rego` | 安全团队 | Rego 策略 |

### 7.2 入口认证配置

```yaml
# config/entrypoints.yaml
entrypoints:
  http:
    auth: jwt
    require_auth: true
  grpc:
    auth: mtls
    require_auth: true
  mcp_proxy_stdio:
    auth: static_token
  mcp_proxy_sse:
    auth: mtls
```

## 8. 不做的事

v0.20.0 明确排除，避免范围膨胀：

| 不做 | 原因 |
|---|---|
| HTTP Executor | v0.21.0 通过 Executor 抽象自然扩展 |
| 本地函数 / 沙箱执行器 | 需要运行时基础设施，v0.22+ |
| 新 Framework Adapter | 已移出核心 |
| 浏览器 / Shell / 文件 Gateway | 属于 Agent Harness 层，远期 |
| Agent 运行时环境配置 | 属于 Harness 层，远期 |
| 配置热更新 | 当前代码需重启加载，v0.21+ 再评估 |
| Web UI | 远期 |

## 9. 验收标准

v0.20.0 完成时应满足：

1. ✅ `pytest tests/` 全部通过；
2. ✅ `ruff check src tests` 无错误；
3. ✅ `mypy src` 无新增错误；
4. ✅ 核心包不再导出 Framework Adapter；
5. ✅ HTTP 服务默认要求 JWT 认证，未认证请求返回 401；
6. ✅ gRPC 服务支持 mTLS，能从证书提取 agent_id；
7. ✅ MCP Proxy 支持 `--identity-token` 启动参数；
8. ✅ `Checkpoint.forward()` 通过 `ExecutorRegistry` 路由，且行为与 v0.19.0 一致；
9. ✅ `MCPExecutor` 是 `ExecutorRegistry` 中唯一注册的 executor；
10. ✅ 文档更新为三种官方形态；
11. ✅ gRPC mTLS 支持证书身份到 `agent_id` / `harness_id` 映射；
12. ✅ MCP Proxy SSE 模式支持 mTLS；stdio 模式明确标记为开发/测试；
13. ✅ `src/KNOWN_LIMITATIONS.md` 增加 v0.20.0 边界声明。

## 10. 关键决策

1. **Framework Adapters 移出核心**：降低形态复杂度，避免用户误以为 Adapter 是强治理形态。
2. **身份认证不是可选项**：生产入口默认要求认证，开发环境可降级为 static 模式。
3. **agent_id 从凭证推导**：请求体中的 `agent_id` 不再作为权威来源，只做一致性校验。
4. **证书身份需要映射层**：mTLS CN/SAN 不等于内部 `agent_id`，必须通过 `cert_subject_template` 或 `cert_mappings` 映射。
5. **Executor 抽象但不扩展**：v0.20.0 只做接口抽象，不引入新执行器类型，保证行为稳定。
6. **MCP-only 是短期边界**：文档诚实声明当前只支持 MCP 工具，非 MCP 工具需要后续版本。
7. **stdio 模式仅用于开发/测试**：生产环境 MCP Proxy 必须使用 SSE + mTLS，防止 Agent 进程读取身份 token。

## 11. 后续版本预告

| 版本 | 目标 |
|---|---|
| v0.21.0 | HTTP Executor：让 Loop Controller 能直接调用 REST API 工具，无需先包装成 MCP Server |
| v0.22.0 | 配置热更新、多租户隔离、Secret Broker 基础 |
| v0.23.0+ | Sandboxed Local Function Executor、Agent Harness / Runtime 探索 |
