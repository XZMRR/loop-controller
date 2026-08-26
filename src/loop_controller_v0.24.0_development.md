# v0.24.0 开发文档：ShellExecutor 与 SQLExecutor

## 1. 目标

v0.23.1 已完成审计修复，governance 核心链路（MCP / HTTP / Local Function）趋于稳定。
v0.24.0 的目标是把企业 Agent 最高频的两类工具纳入同一套治理闭环：**命令行脚本** 与 **SQL 查询**。

> **一句话目标**：新增 `ShellExecutor` 与 `SQLExecutor`，用“命令模板 + 参数白名单”和“参数化查询 + 只读角色”替换脆弱的正则白名单，覆盖企业 CLI 与数据库工具。

v0.24.0 只做以下两件事：

1. **ShellExecutor**：让 Agent 在受控条件下调用本地命令/脚本，默认 `default_risk=critical`；
2. **SQLExecutor**：在声明的数据源上执行参数化 SQL，只读/写分离，敏感密码通过 Secret Broker 注入。

v0.24.0 **不做**：

- BrowserExecutor（延后至 v0.25.0）；
- Docker 容器隔离后端（延后至 v0.26.0）；
- EncryptedFileSecretBackend（延后至 v0.26.0）。

## 2. 背景与动机

### 2.1 为什么先做 Shell + SQL

| 工具类型 | 企业场景 | 治理难点 |
|---|---|---|
| Shell | kubectl、awscli、legacy CLI、内部脚本 | 命令注入、参数拼接、任意重定向 |
| SQL | 内部数据库、数据仓库、分析平台 | SQL 注入、越权读写、敏感字段 |

Shell 和 SQL 都是“命令/查询字符串 + 参数”的形态，治理模型高度相似：

- 先由管理员声明**固定模板**；
- 运行时严格校验**参数值**来自白名单或安全类型；
- 拒绝任何试图拼接、转义、注释的行为。

把这两类放在同一版本，可以复用模板校验、参数白名单、超时、输出限制等基础设施。

### 2.2 为什么不先做 Browser

BrowserExecutor 需要 Playwright 依赖，二进制体积大，且涉及截图 DOM 信息泄露等独立安全问题。
单独放到 v0.25.0，可以让 v0.24.0 的测试和交付边界更清晰。

### 2.3 与现有执行器的关系

| 执行器 | 隔离级别 | 适用场景 |
|---|---|---|
| MCPExecutor | 子进程（外部 MCP Server） | 官方/第三方 MCP 工具 |
| HTTPExecutor | 网络边界 | REST API 工具 |
| LocalFunctionExecutor | 子进程 | 企业内部 Python 函数 |
| **ShellExecutor** | 子进程 | CLI/脚本工具 |
| **SQLExecutor** | 数据库连接池 | 数据库查询/写入 |

## 3. 设计原则

1. **默认禁止，显式授权**：Shell / SQL 默认 `default_risk=critical`，需在 Capability Profile 中显式 `allowed: true` 并配置数据源/命令模板。
2. **模板 + 参数白名单，而非正则**：Shell 使用 `command_template` 与 `allowed_args`；SQL 使用参数化查询与 `allowed_patterns`（仅作为二次校验）。
3. **fail-closed**：命令/数据源不存在、参数不合法、连接失败、输出超限、执行超时均返回错误 `ToolResult`。
4. **统一接入 `ExecutorRegistry`**：两类新执行器实现 `ToolExecutor`，`Checkpoint` 无需感知类型。
5. **向后兼容**：不启用相关配置时，v0.24.0 行为与 v0.23.1 完全一致。
6. **依赖可选**：数据库驱动作为 `[sql]` extra，未安装时 `SQLExecutor` 优雅不可用；ShellExecutor 仅依赖标准库。

## 4. 新增/修改文件

| 文件 | 操作 | 说明 |
|---|---|---|
| `src/loop_controller/executors/shell_executor.py` | 新增 | `ShellExecutor`，命令模板 + 参数白名单 |
| `src/loop_controller/executors/shell_models.py` | 新增 | `ShellToolSpec`、`ShellCommandConfig` |
| `src/loop_controller/executors/sql_executor.py` | 新增 | `SQLExecutor`，参数化查询 + 只读/写分离 |
| `src/loop_controller/executors/sql_models.py` | 新增 | `SQLToolSpec`、`DataSourceConfig` |
| `src/loop_controller/executors/base.py` | 可能修改 | 如有必要扩展 `ToolExecutor` 协议 |
| `src/loop_controller/executors/__init__.py` | 修改 | 导出新增执行器与模型 |
| `src/loop_controller/infra/config_loader.py` | 修改 | 加载 `config/shell_tools.yaml`、`config/sql_tools.yaml` |
| `src/loop_controller/runtime.py` | 修改 | 构造并注册新执行器 |
| `config/shell_tools.yaml` | 新增 | 示例 Shell 工具配置 |
| `config/sql_tools.yaml` | 新增 | 示例 SQL 工具配置 |
| `tests/test_shell_executor.py` | 新增 | Shell 执行器测试（含注入负向用例） |
| `tests/test_sql_executor.py` | 新增 | SQL 执行器测试（含注入负向用例） |
| `src/KNOWN_LIMITATIONS.md` | 修改 | 更新 v0.24.0 边界 |
| `src/development_log.md` | 修改 | 追加 v0.24.0 记录 |
| `src/loop_controller_v0.24.0_development.md` | 修改 | 本文档 |

## 5. 配置模型

### 5.1 `config/shell_tools.yaml`

```yaml
tools:
  kubectl_get:
    description: 在固定命名空间下执行 kubectl get
    command_template: ["kubectl", "get", "{resource}", "-n", "{namespace}"]
    allowed_args:
      resource: ["pods", "services", "deployments"]
      namespace: ["default", "staging"]
    default_risk: critical
    cost_per_call: 100
    sandbox:
      timeout_seconds: 30
      max_output_bytes: 131072
      env_whitelist: ["KUBECONFIG"]
```

### 5.2 Python 模型：Shell

```python
class ShellCommandConfig(BaseModel):
    timeout_seconds: float = Field(default=30.0, ge=0.1, le=300.0)
    max_output_bytes: int = Field(default=64 * 1024, ge=1024)
    env_whitelist: list[str] = Field(default_factory=list)

class ShellToolSpec(BaseModel):
    tool_name: str
    description: str = ""
    command_template: list[str]
    allowed_args: dict[str, list[str]] = Field(default_factory=dict)
    # 额外元字符黑名单，默认禁止常见 shell 注入字符
    forbidden_chars: list[str] = Field(default_factory=lambda: [";", "|", "&", "`", "$", "(", ")", ">", "<", "\\"])
    default_risk: RiskLevel = "critical"
    cost_per_call: int = 0
    sandbox: ShellCommandConfig = Field(default_factory=ShellCommandConfig)
```

### 5.3 `config/sql_tools.yaml`

```yaml
data_sources:
  company_db:
    driver: postgresql
    host: db.company.com
    port: 5432
    database: analytics
    read_only_user: analytics_ro
    secret_ref: {name: db_password}

tools:
  query_analytics:
    data_source: company_db
    description: 只读查询 analytics 数据库
    read_only: true
    parameterize: true
    allowed_patterns:
      - ^SELECT\s+.*$
    forbidden_patterns:
      - ";"
      - "--"
    default_risk: high
    cost_per_call: 100
```

### 5.4 Python 模型：SQL

```python
class DataSourceConfig(BaseModel):
    name: str
    driver: str  # postgresql | mysql | sqlite 等
    host: str | None = None
    port: int | None = None
    database: str | None = None
    read_only_user: str | None = None
    write_user: str | None = None
    secret_ref: SecretRef | None = None

class SQLToolSpec(BaseModel):
    tool_name: str
    data_source: str
    description: str = ""
    read_only: bool = True
    parameterize: bool = True
    # allowed/forbidden patterns 作为二次语义校验，不替代参数化
    allowed_patterns: list[str] = Field(default_factory=list)
    forbidden_patterns: list[str] = Field(default_factory=list)
    default_risk: RiskLevel = "high"
    cost_per_call: int = 0
    timeout_seconds: float = Field(default=30.0, ge=1.0, le=300.0)
```

## 6. 关键设计决策

### 6.1 ShellExecutor 安全模型

1. **command_template 必为字符串列表**：禁止传入完整命令字符串，Agent 只能填充命名参数；
2. **allowed_args 严格枚举**：每个 `{placeholder}` 必须有对应的允许值列表；未声明的 placeholder 不允许出现；
3. **参数值字符黑名单**：默认拒绝 `; | & \` \` $ ( ) > < \\`；允许管理员额外配置；
4. **Env 变量白名单**：只有 `env_whitelist` 中的变量才会注入子进程；
5. **禁用 shell=True**：始终使用 `asyncio.create_subprocess_exec` 直接执行，不经过 shell；
6. **默认 timeout + 输出限制**：和 LocalFunctionExecutor 一样，超限时 kill 子进程。

### 6.2 SQLExecutor 安全模型

1. **参数化查询优先**：SQL 中的变量必须由占位符替换，禁止字符串拼接；
2. **只读连接隔离**：`read_only=true` 时使用只读数据库用户，且只接受 `SELECT / WITH` 开头；
3. **禁用分号和注释**：`forbidden_patterns` 默认包含 `;` 和 `--`；命中直接拒绝；
4. **写操作默认 critical**：`read_only=false` 的工具默认 `default_risk=critical`，强制审批；
5. **连接密码不落地**：通过 `SecretRef` 从 `SecretBroker` 注入；
6. **连接池隔离**：每个数据源独立连接池，不同 tenant 不共享连接。

### 6.3 错误码

| 场景 | Shell 错误码 | SQL 错误码 |
|---|---|---|
| 命令/SQL 模板不存在 | `shell_command_not_found` | `sql_tool_not_found` |
| 参数非法/不在白名单 | `shell_arg_not_allowed` | `sql_arg_not_allowed` |
| 注入字符命中 | `shell_injection_blocked` | `sql_injection_blocked` |
| 超时 | `shell_timeout` | `sql_timeout` |
| 输出过大 | `shell_output_too_large` | — |
| 连接失败 | — | `sql_connect_error` |
| 写操作被只读拦截 | — | `sql_read_only_violation` |

## 7. 风险与回退

| 风险 | 缓解 |
|---|---|
| Shell 命令注入 | command_template + allowed_args + 字符黑名单 + 禁用 shell=True |
| SQL 注入 | 强制参数化 + 只读连接 + 分号/注释黑名单 |
| 数据库凭据泄露 | Secret Broker 注入，配置文件只保留 secret_ref |
| 子进程输出 OOM | 复用 LocalFunctionExecutor 的 `_communicate_with_limit` 逻辑 |
| 数据库驱动未安装 | `[sql]` extra 可选；未安装时工具返回 `sql_driver_missing` |
| 多租户越权 | 每个 tenant 独立数据源命名空间；连接字符串按 tenant_id 解析 |

## 8. 验收标准

- `pytest tests/`：新增 Shell/SQL 测试通过，整体无回归；
- `ruff check src tests examples`：通过；
- `mypy src`：通过；
- 安全测试：Shell 命令注入、SQL 注入均有负向测试；
- 未安装 `[sql]` extra 时，SQL 工具优雅失败并返回清晰错误码；
- 新增工具能在示例配置中注册、治理链路中按风险等级正常审批/执行。
