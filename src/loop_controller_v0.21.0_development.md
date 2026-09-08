# v0.21.0 开发文档：HTTP Executor —— 原生 REST API 工具治理

## 1. 目标

v0.20.0 把 Loop Controller 收敛为 MCP-only 控制平面，并建立了 `ToolExecutor` / `ExecutorRegistry` 执行器抽象。v0.21.0 的目标是在不破坏 MCP 治理能力的前提下，让 Loop Controller **直接调用 REST API 工具**，无需先把每个 REST API 包装成 MCP Server。

> **一句话目标**：HTTP Executor 接入后，企业可以把内部/外部的 REST API 当作 Loop Controller 原生工具治理，复用同一套身份、策略、审批、审计、预算、风险链路。

v0.21.0 只做一件事：

1. **HTTP Executor 与工具配置模型**：实现 `HTTPExecutor`，让 `tool_mapping` 支持 `type: http`。

## 2. 背景与动机

### 2.1 为什么需要 HTTP Executor

企业里大量系统只有 REST API，例如：

- 内部 CRM / ERP / OA / HR 系统
- 第三方 SaaS（Slack、Jira、GitHub、企业微信、飞书）
- 云厂商 API（AWS、Azure、阿里云）
- 自建微服务

v0.20.0 之前，想让 Agent 调用这些 API，必须：

1. 写一段 Python/Node 代码把 REST API 包装成 MCP Server；
2. 在 `config/mcp_servers.yaml` 注册该 MCP Server；
3. 在 `tool_mapping` 中映射工具名。

这带来三个问题：

- **接入成本高**：每个 REST API 都要写、部署、运维一个 MCP Server；
- **治理链路碎片化**：API 的认证、限流、错误处理、版本管理散落在无数个 MCP Server 里；
- **无法统一管理凭证**：API Key、Client Secret 等秘密分散在 MCP Server 的环境变量中，企业安全团队难以审计。

HTTP Executor 让 Loop Controller **自己就是 REST API 的调用方**，统一收口认证、审计、策略、审批。

### 2.2 与 MCP-only 边界的关系

v0.20.0 的 MCP-only 是**阶段性边界**，不是架构边界。`ToolExecutor` 抽象正是为了解耦“治理控制平面”与“工具执行协议”。HTTP Executor 的引入验证了这一抽象的扩展性：新增一种执行器，不需要修改 `Checkpoint.forward()`、策略引擎、审批流、审计流、预算流。

## 3. 设计原则

1. **零侵入治理链路**：HTTP Executor 复用 v0.20.0 的 `ToolExecutor` 协议；`Checkpoint`、`LoopController`、策略、审批、审计、预算均不感知执行器类型。
2. **配置即工具**：HTTP 工具通过 YAML 声明式配置注册，不强制要求写代码。
3. **默认高风险**：HTTP 工具默认风险等级为 `high`，默认触发更严格治理（建议 require_approval）。
4. **Fail-closed 安全**：
   - 默认禁止访问本地/内网地址（SSRF 防护）；
   - 必须显式 allowlist 才能访问外部域名；
   - 请求/响应敏感字段自动脱敏；
   - 超时、重试、连接数必须受控。
5. **凭证不落地配置**：API Key / Secret 通过环境变量引用或（v0.22 的）Secret Broker 注入，配置文件中只保留引用名。
6. **向后兼容**：现有 MCP 工具配置与行为不变；HTTP 工具是新增 `type` 的可选能力。
7. **可观测**：每个 HTTP 调用记录方法、URL（脱敏）、状态码、耗时、响应大小；失败按 `ToolResult.error_code` 分类。

## 4. 新增/修改文件

| 文件 | 操作 | 说明 |
|---|---|---|
| `src/loop_controller/executors/http_executor.py` | 新增 | `HTTPExecutor` 实现 `ToolExecutor` |
| `src/loop_controller/executors/http_client.py` | 新增 | 受控 `httpx.AsyncClient` 封装：SSRF、超时、限域 |
| `src/loop_controller/executors/http_models.py` | 新增 | `HTTPRequestTemplate`、`HTTPResponseMapping`、`HTTPAuthConfig`、`HTTPToolSpec` 模型 |
| `src/loop_controller/executors/http_security.py` | 新增 | SSRF 防护、域名 allowlist、URL 校验 |
| `src/loop_controller/executors/__init__.py` | 修改 | 导出 `HTTPExecutor`、`HTTPClient`、HTTP 模型 |
| `src/loop_controller/infra/config_loader.py` | 修改 | 解析 `tool_mapping` 中 `type: http` 条目；构造 `HTTPToolSpec` |
| `src/loop_controller/runtime.py` | 修改 | 为每个 HTTP 工具注册 `HTTPExecutor`；支持多执行器并存 |
| `src/loop_controller/tool_governor.py` | 修改（可能） | 风险打分默认对 HTTP 工具提升一级 |
| `src/loop_controller/mcp_gateway.py` | 不修改 | MCP 能力保持现状 |
| `config/mcp_servers.yaml` | 不修改 | 现有 MCP 配置保持兼容 |
| `config/tools.yaml` / `config/http_tools.yaml`（可选） | 新增 | HTTP 工具专用配置文件（先复用 `mcp_servers.yaml` 的 `tool_mapping` 扩展） |
| `tests/test_http_executor.py` | 新增 | HTTP Executor 单元测试 |
| `tests/test_http_security.py` | 新增 | SSRF / allowlist 测试 |
| `tests/test_executor_registry.py` | 修改 | 增加多执行器并存测试 |
| `src/loop_controller_v0.21.0_development.md` | 新增 | 本文档 |
| `src/KNOWN_LIMITATIONS.md` | 修改 | 更新 v0.21.0 边界说明 |
| `src/development_log.md` | 修改 | 追加 v0.21.0 开发记录 |

> 配置位置选择：v0.21.0 先扩展 `config/mcp_servers.yaml` 的 `tool_mapping`，增加 `type: http` 字段。v0.22 再考虑拆分为 `config/tools.yaml` 统一 MCP/HTTP/Local 工具。

## 5. HTTP 工具配置模型

### 5.1 `tool_mapping` 扩展

`tool_mapping` 中每个条目当前是：

```yaml
read_file: {server: filesystem, mcp_name: read_text_file, cost_per_call: 500}
```

v0.21.0 扩展为两种形态：

```yaml
tool_mapping:
  # MCP 工具（v0.20.0 已有）
  read_file:
    type: mcp
    server: filesystem
    mcp_name: read_text_file
    cost_per_call: 500

  # HTTP 工具（v0.21.0 新增）
  create_jira_ticket:
    type: http
    cost_per_call: 1000
    base_url: "https://company.atlassian.net"
    method: POST
    path: "/rest/api/3/issue"
    headers:
      Content-Type: "application/json"
      Accept: "application/json"
    auth:
      type: bearer_token
      token: "${JIRA_API_TOKEN}"   # 环境变量引用，v0.22 支持 secret broker
    body_template:
      fields:
        project:
          key: "{project_key}"
        summary: "{summary}"
        description: "{description}"
    response_mapping:
      status_code: 201
      extract:
        issue_key: "$.key"
        issue_url: "$.self"
    default_risk: high
    allowed_hosts:
      - "company.atlassian.net"
```

### 5.2 配置字段说明

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `type` | `mcp` / `http` | 否，默认 `mcp` | 工具类型 |
| `cost_per_call` | int | 否，默认 0 | 调用成本，进入预算治理 |
| `base_url` | str | HTTP 必填 | 根 URL，末尾无 `/` |
| `method` | str | HTTP 必填 | `GET` / `POST` / `PUT` / `PATCH` / `DELETE` |
| `path` | str | HTTP 必填 | 路径模板，支持 `{arg}` 占位符 |
| `query_template` | dict | 否 | 查询参数模板 |
| `headers` | dict | 否 | 固定 header；值支持 `{arg}` 占位符 |
| `body_template` | dict / str | 否 | 请求体模板；dict 会被 JSON 序列化；str 按原样发送 |
| `auth` | dict | 否 | 认证配置 |
| `response_mapping` | dict | 否 | 响应映射与校验 |
| `default_risk` | `low/medium/high/critical` | 否，默认 `high` | 默认风险等级 |
| `allowed_hosts` | list[str] | 推荐 | 允许访问的主机白名单；未设置时使用 `base_url` 的主机 |
| `timeout_seconds` | float | 否，默认 30 | 单次请求超时 |
| `retry` | dict | 否 | 重试策略（次数、退避） |

### 5.3 认证模型 `HTTPAuthConfig`

```python
class HTTPAuthConfig(BaseModel):
    type: Literal[
        "none",
        "bearer_token",
        "api_key_header",
        "api_key_query",
        "basic",
        "mtls",
    ] = "none"

    # bearer_token / api_key_header / api_key_query
    token: str | None = None          # 支持 ${ENV} 引用
    key_name: str | None = None       # header/query 名

    # basic
    username: str | None = None
    password: str | None = None       # 支持 ${ENV} 引用

    # mtls：引用 entrypoints / identity 中已配置的证书
    cert_ref: str | None = None
```

### 5.4 响应映射 `HTTPResponseMapping`

```python
class HTTPResponseMapping(BaseModel):
    # 把哪些 HTTP status_code 视为成功
    success_status: list[int] = Field(default_factory=lambda: [200, 201, 202, 204])

    # 从 JSON 响应中提取字段，作为 ToolResult.content 的一部分
    extract: dict[str, str] = Field(default_factory=dict)
    # 示例：{"issue_key": "$.key", "issue_url": "$.self"}

    # 把响应体直接作为 content 的字段名
    raw_body_field: str | None = None

    # 错误码映射：status_code -> error_code
    error_codes: dict[int, str] = Field(default_factory=dict)
```

## 6. HTTPExecutor 实现要点

### 6.1 类签名

```python
class HTTPExecutor(ToolExecutor):
    def __init__(
        self,
        http_client: HTTPClient,
        tool_specs: dict[str, HTTPToolSpec],
    ) -> None:
        ...

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: ExecutionContext,
    ) -> ToolResult:
        ...

    async def list_tools(self, profile: CapabilityProfile) -> list[Tool]:
        ...
```

### 6.2 执行流程

1. **查规格**：按 `tool_name` 取 `HTTPToolSpec`；找不到抛 `ExecutorRegistryError`。
2. **渲染模板**：
   - 用 Jinja2 或简单字符串替换把 `arguments` 填入 `path`、`query_template`、`headers`、`body_template`；
   - 缺失参数必须显式报错（fail-fast），避免部分参数导致不可预期请求。
3. **安全校验**：
   - 解析最终 URL；
   - 校验 host 在 `allowed_hosts` 中；
   - SSRF 检查（见第 7 节）。
4. **认证注入**：根据 `auth.type` 注入 header / query / basic / mtls。
5. **发送请求**：通过 `HTTPClient` 发送，受控超时。
6. **处理响应**：
   - 状态码在 `success_status` 内：按 `response_mapping.extract` 提取字段，构造 `ToolResult(content=...)`；
   - 状态码不在成功列表：按 `error_codes` 映射或通用 `http_error`，构造 `ToolResult(status="error")`。
7. **审计摘要**：记录 method、host、path、status_code、elapsed_ms、response_size；header/body 敏感字段脱敏。

### 6.3 与 MCPExecutor 并存

`Runtime` 组装时：

```python
mcp_executor = MCPExecutor(gateway)
http_executor = HTTPClient(...)  # 全局受控 client
http_executor = HTTPExecutor(http_client, config.http_tool_specs)

executor_registry = ExecutorRegistry()
for canonical_name, entry in config.tool_mapping.items():
    if entry.type == "http":
        executor_registry.register(canonical_name, http_executor)
    else:
        executor_registry.register(canonical_name, mcp_executor)
```

`ExecutorRegistry` 已支持按工具名分发，因此 `Checkpoint.forward()` 无需改动。

## 7. 安全与 SSRF 防护

### 7.1 默认禁止访问的地址

`HTTPSecurityPolicy` 默认拒绝：

- IPv4/IPv6 回环地址（`127.0.0.0/8`、`::1`）
- 私有地址段（`10.0.0.0/8`、`172.16.0.0/12`、`192.168.0.0/16`）
- 链路本地地址（`169.254.0.0/16`、`fe80::/10`）
- URL 中出现 `localhost`
- 未解析到公共 IP 的域名（可选开关，推荐生产开启）

### 7.2 域名白名单

每个 HTTP 工具必须声明 `allowed_hosts`。`HTTPSecurityPolicy.is_allowed(url)` 检查：

1. 域名（或 IP）是否在白名单；
2. 最终解析的 IP 是否不在内网段；
3. 重定向后的地址是否仍满足 1/2（通过 `HTTPClient` 拦截重定向）。

### 7.3 URL 渲染限制

- `path` 中 `{arg}` 只能替换为 URL-safe 字符串；
- query/body 中不支持自由嵌套对象模板，防止通过参数注入改变目标主机；
- `base_url` 不允许包含 `{arg}`。

### 7.4 请求边界

- 默认 `timeout_seconds=30`，最大允许 300；
- 默认禁止重定向，或最多 3 次；
- 默认每个工具并发连接数限制；
- 请求体默认最大 1MB，响应体默认最大 5MB。

## 8. 凭证管理

### 8.1 环境变量引用（v0.21.0 最小实现）

配置文件中敏感值使用 `${ENV_NAME}` 语法：

```yaml
auth:
  type: bearer_token
  token: "${JIRA_API_TOKEN}"
```

`HTTPToolSpec` 加载时解析 `${...}`，运行时从环境变量读取。注意：

- 未解析的引用拒绝启动（fail-closed）；
- 解析后的真实 secret 不进入 `model_dump()` / 日志 / 审计；
- 单元测试使用 `monkeypatch.setenv`。

### 8.2 Secret Broker 预留接口（v0.22）

`HTTPAuthConfig` 设计时预留：

```python
secret_ref: str | None = None   # 指向 Secret Broker 的 secret ID
```

v0.21.0 不实现 Secret Broker，但保留字段名，避免 v0.22 破坏性改动。

## 9. 响应映射与错误处理

### 9.1 成功响应

```yaml
response_mapping:
  success_status: [200, 201]
  extract:
    issue_key: "$.key"
    issue_url: "$.self"
```

返回：

```json
{
  "status": "success",
  "issue_key": "PROJ-123",
  "issue_url": "https://..."
}
```

### 9.2 错误响应

HTTP status 400/401/403/404/500/502/503 等映射为：

```python
ToolResult(
    call_id=context.call_id,
    task_id=context.task_id,
    tool_name=tool_name,
    status="error",
    content=response_text[:500],
    error_code="http_unauthorized",  # 或 http_forbidden, http_not_found 等
)
```

### 9.3 超时与网络错误

- `httpx.TimeoutException` -> `error_code="http_timeout"`
- `httpx.ConnectError` -> `error_code="http_connect_error"`
- DNS 解析失败 -> `error_code="http_dns_error"`
- SSRF 被拦截 -> `error_code="http_security_blocked"`

## 10. 审计与可观测性

每个 HTTP 工具调用产生审计事件 `action="execute"`，payload 至少包含：

```json
{
  "executor": "http",
  "tool_name": "create_jira_ticket",
  "method": "POST",
  "host": "company.atlassian.net",
  "path": "/rest/api/3/issue",
  "status_code": 201,
  "elapsed_ms": 245,
  "request_size": 128,
  "response_size": 512,
  "error_code": null,
  "auth_type": "bearer_token"
}
```

请求体/响应体中的敏感字段（由 `masking_rules` 决定）脱敏。

## 11. 岗位说明书（Profile）集成

HTTP 工具在 `CapabilityProfile.tools` 中的权限配置与 MCP 工具完全一致：

```yaml
profiles:
  - profile_id: research_assistant_v1
    tools:
      create_jira_ticket:
        allowed: true
        require_approval: true
        allowed_args:
          project_key: ["PROJ"]
        max_calls_per_task: 5
```

`ToolGovernor` 对 HTTP 工具默认风险提升一级，例如：

- 原本 `low` 的 HTTP 工具按 `medium` 进入 R2；
- `medium` 按 `high`；
- `high` / `critical` 不变。

具体实现可在 `tool_governor.py` 中增加 `executor_type == "http"` 的判断。

## 12. 验收标准

v0.21.0 完成时应满足：

1. ✅ `pytest tests/` 全部通过；
2. ✅ `ruff check src tests` 无错误；
3. ✅ `mypy src` 无新增错误；
4. ✅ `config/mcp_servers.yaml` 的 `tool_mapping` 支持 `type: http`；
5. ✅ `HTTPExecutor` 实现 `ToolExecutor`，并通过 `ExecutorRegistry` 与 MCPExecutor 并存；
6. ✅ HTTP 工具支持 `GET/POST/PUT/PATCH/DELETE`；
7. ✅ HTTP 工具支持 `bearer_token`、`api_key_header`、`api_key_query`、`basic` 认证；
8. ✅ 配置文件中的 `${ENV}` 引用能在运行时被解析，未解析时拒绝启动；
9. ✅ 默认 SSRF 防护生效：禁止访问本地/内网地址；
10. ✅ `allowed_hosts` 白名单生效，未命中返回 `http_security_blocked`；
11. ✅ 响应映射支持 JSONPath 提取字段，非 2xx 按 error_code 分类；
12. ✅ HTTP 工具调用进入审计日志，敏感字段脱敏；
13. ✅ HTTP 工具默认风险等级提升一级；
14. ✅ 现有 MCP 工具行为与 v0.20.0 完全一致；
15. ✅ `src/KNOWN_LIMITATIONS.md` 与 `src/development_log.md` 更新。

## 13. 不做的事

| 不做 | 原因 |
|---|---|
| GraphQL/WebSocket 工具 | REST 先跑通，v0.22+ 再评估 |
| Secret Broker | v0.22.0 专门做 Secret 管理与热更新 |
| HTTP 工具动态发现（OpenAPI 导入） | 超出范围，可后续作为配置生成器 |
| 响应体二进制/文件下载 | v0.21 只处理 text/json；大文件流式后续做 |
| HTTP 缓存 | v0.22+ 评估 |
| 请求签名（AWS Signature 等） | v0.22+ 作为 auth plugin |
| 沙箱本地函数执行器 | v0.23+ |
| Web UI | 远期 |

## 14. 风险与回退

| 风险 | 缓解措施 |
|---|---|
| HTTP 工具引入 SSRF | 默认禁止内网访问 + 强制 `allowed_hosts` + DNS 解析校验 |
| 配置文件泄露 secret | 只支持 `${ENV}`，日志/审计脱敏；启动时未解析则失败 |
| HTTP 响应体过大拖垮服务 | 默认 5MB 限制，超时 30s |
| HTTP 工具破坏 MCP 工具行为 | ExecutorRegistry 按工具名分发；HTTP 与 MCP 独立测试 |
| 外部 API 变更导致工具失效 | 配置级版本管理；错误码分类便于告警 |

## 15. 后续版本预告

| 版本 | 目标 |
|---|---|
| v0.22.0 | Secret Broker、配置热更新、多租户隔离基础 |
| v0.23.0+ | Sandboxed Local Function Executor、Agent Harness / Runtime 探索 |
