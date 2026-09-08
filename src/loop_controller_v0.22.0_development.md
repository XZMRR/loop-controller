# v0.22.0 开发文档：Secret Broker 与 HTTP 工具热更新

## 1. 目标

v0.21.0 把 HTTP Executor 跑通，但 HTTP 工具凭证仍依赖 `${ENV_NAME}` 环境变量引用，配置与 secret 混合，且改配置必须重启进程。v0.22.0 的目标是：

1. **Secret Broker**：把 secret 从配置文件/环境变量中剥离出来，统一由 `SecretBroker` 管理，支持文件后端，未来可替换为 Vault/KMS/etd；
2. **HTTP 工具配置热更新**：HTTP 工具规格与 secret 支持运行时热更新，无需重启进程；
3. **多租户隔离基础**：引入 `tenant_id` 概念，Secret Broker 按 tenant 命名空间隔离，Runtime 支持按 tenant 路由配置。

> **一句话目标**：企业可以在不重启 Loop Controller 的情况下，安全地更新 HTTP 工具配置与凭证，并为未来多租户部署打下命名空间基础。

v0.22.0 只做以上三件事，不贪大求全。

## 2. 背景与动机

### 2.1 环境变量引用的局限

v0.21.0 用 `${ENV_NAME}` 把 secret 放在环境变量中，带来几个问题：

- **运维繁琐**：每新增一个 HTTP 工具就要改一次启动脚本/容器 env；
- **轮转困难**：API Key 到期轮转需要重启进程；
- **审计面大**：环境变量可能被 `/proc/*/environ`、容器镜像层、CI log 泄露；
- **多租户困难**：同一进程服务多个租户时，环境变量无法按租户隔离。

### 2.2 为什么需要热更新

企业生产中的 HTTP API 经常变动：

- API Key 定期轮转（3-6 个月）；
- 第三方 API 升级、base_url 变更；
- 新增/下线内部 REST 工具。

如果每次变动都要重启 Loop Controller，会中断所有 Agent 的治理链路， unacceptable。

### 2.3 多租户前提

v0.22.0 不实现完整多租户（权限隔离、数据隔离、计费拆分），但：

- 在数据模型上预留 `tenant_id`；
- Secret Broker 按 `tenant_id` 命名空间隔离 secret；
- Runtime 加载配置时区分 `global` / `tenant` 两个层级。

## 3. 设计原则

1. **Secret 不落地配置文件**：HTTP 工具配置中只保留 `secret_ref` / `secret_key`，真实 secret 由 Secret Broker 在运行期注入。
2. **Fail-closed**：Secret Broker 找不到 secret 时拒绝调用；配置热更新失败时保留旧配置继续运行，并告警。
3. **最小权限**：Secret Broker 的 file backend 支持按 secret 文件权限控制；读取后只在内存中保留必要时间。
4. **向后兼容**：v0.21.0 的 `${ENV_NAME}` 引用继续支持，但标记为 deprecated；MCP 工具不受影响。
5. **热更新只对 HTTP 工具**：MCP 工具配置涉及子进程生命周期，仍需要重启；HTTP 工具是声明式配置，天然适合热更新。
6. **单进程 asyncio 假设不变**：热更新走主事件循环，不引入多线程锁。

## 4. 新增/修改文件

| 文件 | 操作 | 说明 |
|---|---|---|
| `src/loop_controller/secrets/__init__.py` | 新增 | Secret Broker 包导出 |
| `src/loop_controller/secrets/models.py` | 新增 | `SecretRef`、`SecretValue`、`SecretScope` |
| `src/loop_controller/secrets/broker.py` | 新增 | `SecretBroker` Protocol |
| `src/loop_controller/secrets/file_backend.py` | 新增 | `FileSecretBackend`：从 `secrets/` 目录按 tenant/secret_name.jsonl 或 .enc 读取 |
| `src/loop_controller/secrets/memory_backend.py` | 新增 | `MemorySecretBackend`：内存后端，供测试使用 |
| `src/loop_controller/executors/http_models.py` | 修改 | `HTTPAuthConfig` 支持 `secret_ref` 替代 `token/password`；渲染时通过 Secret Broker 解析 |
| `src/loop_controller/executors/http_executor.py` | 修改 | execute 时传入 Secret Broker，动态替换 secret 占位符 |
| `src/loop_controller/infra/hot_reload.py` | 新增 | 热更新调度器：`watchdog` 监控文件变化 + 定时轮询 fallback |
| `src/loop_controller/infra/config_loader.py` | 修改 | 加载 `config/http_tools.yaml` 和 `config/secrets.yaml`；支持 tenant 层级 |
| `src/loop_controller/runtime.py` | 修改 | 创建 Secret Broker；为 HTTPExecutor 注入；启动热更新调度器 |
| `src/loop_controller/models.py` | 修改（可能） | `Agent` / `Task` 可选 `tenant_id` |
| `config/secrets.yaml` | 新增 | Secret Broker 后端配置 |
| `config/http_tools.yaml` | 新增 | HTTP 工具独立配置文件（v0.21 复用 mcp_servers.yaml，v0.22 拆分） |
| `secrets/` | 新增目录 | file backend 默认 secret 目录 |
| `tests/test_secret_broker.py` | 新增 | Secret Broker 单元测试 |
| `tests/test_hot_reload.py` | 新增 | 热更新测试 |
| `tests/test_http_executor.py` | 修改 | 增加 secret broker 集成测试 |
| `src/KNOWN_LIMITATIONS.md` | 修改 | 更新 v0.22.0 边界 |
| `src/development_log.md` | 修改 | 追加 v0.22.0 记录 |
| `src/loop_controller_v0.22.0_development.md` | 新增 | 本文档 |

## 5. Secret Broker 设计

### 5.1 Secret 模型

```python
class SecretScope(str, Enum):
    GLOBAL = "global"
    TENANT = "tenant"

class SecretValue(BaseModel):
    value: str
    scope: SecretScope = SecretScope.GLOBAL
    tenant_id: str | None = None
    version: str = "1"
    expires_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

class SecretRef(BaseModel):
    name: str                    # secret 在 backend 中的名字
    key: str | None = None      # 若 secret value 是 JSON，取其中某个字段
    version: str | None = None  # 指定版本，None 取最新
    tenant_id: str | None = None
```

### 5.2 SecretBroker Protocol

```python
class SecretBroker(Protocol):
    async def get(self, ref: SecretRef) -> SecretValue | None: ...
    async def list(self, scope: SecretScope, tenant_id: str | None = None) -> list[str]: ...
    async def reload(self) -> None: ...
```

### 5.3 FileSecretBackend

默认 backend：

- `secrets/global/{name}.json`：全局 secret；
- `secrets/tenants/{tenant_id}/{name}.json`：租户级 secret。

JSON 文件格式：

```json
{
  "value": "sk-...",
  "version": "1",
  "expires_at": "2026-12-31T23:59:59Z",
  "metadata": {"rotation_by": "admin"}
}
```

若 `value` 是 JSON 对象，则 `SecretRef.key` 可指定取其中字段，例如：

```json
{
  "value": {"api_key": "ak-...", "api_secret": "as-..."},
  "version": "2"
}
```

`SecretRef(name="jira", key="api_key")` 得到 `ak-...`。

### 5.4 加密（v0.22 最小实现）

v0.22.0 file backend 默认存储明文 secret（文件系统权限保护）。可选支持 base64 编码但不提供真正加密；真正加密由 v0.23+ 的 KMS 插件负责。这样避免 v0.22 引入加密依赖和密钥管理复杂度。

## 6. HTTP 工具与 Secret Broker 集成

### 6.1 配置形式

`config/http_tools.yaml`：

```yaml
tools:
  create_jira_ticket:
    base_url: "https://company.atlassian.net"
    method: POST
    path: "/rest/api/3/issue"
    headers:
      Content-Type: "application/json"
    auth:
      type: bearer_token
      secret_ref:
        name: jira_api_token
    body_template:
      fields:
        summary: "{summary}"
    response_mapping:
      success_status: [200, 201]
      extract:
        key: "$.key"
```

`config/secrets.yaml`：

```yaml
backend:
  type: file
  base_path: "./secrets"
hot_reload:
  enabled: true
  poll_interval_seconds: 30
```

`secrets/global/jira_api_token.json`：

```json
{
  "value": "ATATT3x...",
  "version": "3",
  "metadata": {"rotated_at": "2026-08-25"}
}
```

### 6.2 渲染流程

`HTTPToolSpec.build_request()` 增加 `secret_broker` 参数：

1. 先渲染普通模板；
2. 若 `auth` 含 `secret_ref`，调用 `broker.get(ref)`；
3. 若 secret 不存在或过期，抛 `SecretNotFoundError`；
4. 把 secret value 注入 header / query / body；
5. 返回最终请求。

### 6.3 向后兼容

若 `auth.token` 或 `auth.password` 已经是 `${ENV}` 解析后的字符串，继续生效。`secret_ref` 与直接值同时存在时，`secret_ref` 优先。

## 7. 热更新设计

### 7.1 热更新范围

| 资源 | 是否热更新 | 说明 |
|---|---|---|
| `config/http_tools.yaml` | 是 | 重新解析并替换 `HTTPExecutor._tool_specs` |
| `secrets/` 下文件 | 是 | 重新加载对应 secret |
| `config/mcp_servers.yaml` | 否 | 涉及子进程生命周期，仍要重启 |
| `config/profiles.yaml` | 否 | 权限策略变更建议重启，避免运行时权限漂移 |
| `config/policies/*.rego` | 否 | 策略热加载超出本版本范围 |

### 7.2 实现

`HotReloader`：

- 使用 `watchdog`（如果已安装）监控 `config/http_tools.yaml` 和 `secrets/`；
- 若 `watchdog` 不可用，退化为 `asyncio` 轮询（默认 30s）；
- 检测到变化后调用 `ConfigLoader.reload_http_tools()` 和 `SecretBroker.reload()`；
- 更新 `HTTPExecutor._tool_specs` 时保持旧引用不中断正在执行的请求；
- 更新失败时记录 `audit_alert`，保留旧配置。

### 7.3 安全

- 热更新不修改 MCP gateway、OPA policy、审计存储；
- secret 文件权限校验：若文件权限过宽（如 `o+r`），`FileSecretBackend` 拒绝加载并告警；
- 不支持删除运行中工具：只能新增或修改；删除工具需重启。

## 8. 多租户隔离基础

### 8.1 模型层

- `Agent.tenant_id: str | None = None`
- `Task.tenant_id: str | None = None`
- `SecretRef.tenant_id: str | None = None`
- `SecretValue.tenant_id: str | None = None`

### 8.2 Secret 查找顺序

`SecretBroker.get(ref)`：

1. 若 `ref.tenant_id` 指定，优先查 `secrets/tenants/{tenant_id}/{name}.json`；
2. 未命中则查 `secrets/global/{name}.json`；
3. 若 `ref.tenant_id` 未指定，只查 `global`。

### 8.3 Runtime 按 tenant 路由

v0.22.0 不实现 tenant 级完整隔离，但：

- `build_runtime()` 接收可选 `tenant_id` 参数；
- `HTTPExecutor` 执行时，从 `context.tenant_id` 或 `task.tenant_id` 推导 secret scope；
- 不同 tenant 的 HTTP 工具规格可以分别放在 `config/tenants/{tenant_id}/http_tools.yaml`。

## 9. 验收标准

v0.22.0 完成时应满足：

1. ✅ `pytest tests/` 全部通过；
2. ✅ `ruff check src tests examples` 无错误；
3. ✅ `mypy src` 无新增错误；
4. ✅ `HTTPAuthConfig` 支持 `secret_ref`；
5. ✅ `FileSecretBackend` 能从 `secrets/global/{name}.json` 读取 secret；
6. ✅ `SecretBroker` 支持 tenant 命名空间查找顺序；
7. ✅ `HTTPExecutor.execute()` 在运行时通过 Secret Broker 注入 secret；
8. ✅ secret 缺失/过期时返回 `http_auth_error` 并阻止调用；
9. ✅ 支持 `${ENV}` 与 `secret_ref` 两种模式并存，`secret_ref` 优先；
10. ✅ `config/http_tools.yaml` 支持运行时热更新；
11. ✅ 热更新失败时保留旧配置并记录告警；
12. ✅ `Agent` / `Task` 模型包含可选 `tenant_id`；
13. ✅ 现有 MCP 工具不受影响；
14. ✅ 更新 `src/KNOWN_LIMITATIONS.md` 与 `src/development_log.md`。

## 10. 不做的事

| 不做 | 原因 |
|---|---|
| MCP 工具热更新 | 涉及子进程生命周期，v0.22 不做 |
| Profile / Policy 热更新 | 权限变更应显式重启，避免运行时漂移 |
| Secret 真正加密/KMS 集成 | v0.23+；v0.22 用文件权限保护 |
| 完整多租户权限/数据隔离 | v0.22 只预留 tenant_id 与 secret 命名空间 |
| Web UI for Secret 管理 | 远期 |

## 11. 风险与回退

| 风险 | 缓解措施 |
|---|---|
| 热更新导致配置错误 | 失败保留旧配置；单元测试覆盖非法 YAML、缺失 secret |
| Secret 文件权限过宽 | `FileSecretBackend` 启动/加载时校验并拒绝 `o+r` |
| Secret Broker 成为单点瓶颈 | backend 只读、无锁；内存缓存 + reload 触发更新 |
| tenant 查找顺序出错 | 单元测试覆盖 global / tenant / fallback |
| 热更新与正在执行请求竞态 | 原子替换 `HTTPExecutor._tool_specs` 引用，旧请求不受影响 |

## 12. 后续版本预告

| 版本 | 目标 |
|---|---|
| v0.23.0 | Sandboxed Local Function Executor（本地函数沙箱执行） |
| v0.24.0+ | KMS/Vault Secret Backend、HTTP 缓存、请求签名插件 |
