# v0.24.0 开发文档：执行器扩展与生产级隔离

## 1. 目标

v0.23.1 已完成审计修复， governance 核心链路（MCP / HTTP / Local Function）趋于稳定。
v0.24.0 的目标是把剩余常见工具形态纳入同一套治理闭环，并提升生产部署时的隔离能力。

> **一句话目标**：新增 Shell、Browser、SQL 直连三类执行器，并补齐可选的容器级隔离与 Secret 加密后端，让 Loop Controller 覆盖企业最常见的工具调用形态。

v0.24.0 只做以下四件事：

1. **ShellExecutor**：让 Agent 在受控条件下调用本地命令/脚本，默认禁止，需显式白名单；
2. **BrowserExecutor**：提供 headless 浏览器页面访问/截图/DOM 提取能力，用于公开网页治理；
3. **SQLExecutor**：在声明的数据源上执行只读/写 SQL，与 sqlite_server MCP 形成互补；
4. **生产级隔离与 Secret 加密**：为本地函数/Shell 提供可选容器执行后端；Secret Broker 支持加密文件后端（KMS/HashiCorp Vault 预留接口）。

## 2. 背景与动机

### 2.1 为什么需要更多执行器

- **Shell**：大量 legacy 工具只有 CLI 接口；Agent 需要通过脚本与系统/容器/云平台交互。
- **Browser**：Agent 需要读取网页、填写表单、截图验证；HTTP Executor 只能做请求/响应，无法执行 JS、获取渲染后 DOM。
- **SQL**：企业内部数据库/数据仓库是高频工具；把 SQL 工具直接接入治理可避免为每个表都包装 MCP Server。

### 2.2 为什么需要生产级隔离

v0.23.0 的本地函数沙箱是子进程隔离，仍共享文件系统/网络/内存：

- Shell 脚本可能读取任意文件、发起网络请求、修改系统状态；
- 浏览器和 SQL 工具会引入新的依赖（playwright、数据库驱动），增加供应链风险；
- 多租户场景下，不同 Agent 的函数/Shell 调用应互不干扰。

v0.24.0 在保留子进程回退的同时，提供可选容器执行后端，作为生产部署的强隔离手段。

### 2.3 与现有执行器的关系

| 执行器 | 隔离级别 | 适用场景 |
|---|---|---|
| MCPExecutor | 子进程（外部 MCP Server） | 官方/第三方 MCP 工具 |
| HTTPExecutor | 网络边界 | REST API 工具 |
| LocalFunctionExecutor | 子进程 / 可选容器 | 企业内部 Python 函数 |
| **ShellExecutor** | 子进程 / 可选容器 | CLI/脚本工具 |
| **BrowserExecutor** | 子进程 / 可选容器 | 网页访问、截图、DOM 提取 |
| **SQLExecutor** | 数据库连接池 | 数据库查询/写入 |

## 3. 设计原则

1. **默认禁止，显式授权**：Shell / Browser / SQL 属于高危能力，默认 `default_risk=critical`，需在 Profile 中显式 `allowed: true` 并配置数据源/命令白名单。
2. **fail-closed**：命令/URL/数据库不在白名单、连接失败、输出超限、执行超时均返回错误 `ToolResult`。
3. **统一接入 `ExecutorRegistry`**：三类新执行器实现 `ToolExecutor`，`Checkpoint` 无需感知类型。
4. **向后兼容**：不启用相关配置时，v0.24.0 行为与 v0.23.1 完全一致。
5. **依赖可选**：playwright、数据库驱动、docker SDK 作为可选 extras，不安装时相关执行器 gracefully 不可用。

## 4. 新增/修改文件

| 文件 | 操作 | 说明 |
|---|---|---|
| `src/loop_controller/executors/shell_executor.py` | 新增 | `ShellExecutor`，命令白名单 + 超时 + 输出限制 |
| `src/loop_controller/executors/shell_models.py` | 新增 | `ShellToolSpec`、`ShellCommandConfig` |
| `src/loop_controller/executors/browser_executor.py` | 新增 | `BrowserExecutor`，基于 playwright |
| `src/loop_controller/executors/browser_models.py` | 新增 | `BrowserToolSpec`、`BrowserActionConfig` |
| `src/loop_controller/executors/sql_executor.py` | 新增 | `SQLExecutor`，只读/写分离 |
| `src/loop_controller/executors/sql_models.py` | 新增 | `SQLToolSpec`、`DataSourceConfig` |
| `src/loop_controller/executors/base.py` | 修改 | 如有必要扩展 `ToolExecutor` 协议 |
| `src/loop_controller/executors/__init__.py` | 修改 | 导出新增执行器与模型 |
| `src/loop_controller/infra/config_loader.py` | 修改 | 加载 `config/shell_tools.yaml`、`config/browser_tools.yaml`、`config/sql_tools.yaml` |
| `src/loop_controller/runtime.py` | 修改 | 构造并注册新执行器 |
| `src/loop_controller/secrets/encrypted_backend.py` | 新增 | 加密文件 Secret 后端（KMS/Vault 接口预留） |
| `src/loop_controller/executors/container_backend.py` | 新增 | 可选容器隔离后端接口 |
| `src/loop_controller/executors/local_function_executor.py` | 修改 | 支持容器后端回退 |
| `src/loop_controller/executors/shell_executor.py` | 修改 | 支持容器后端回退 |
| `config/shell_tools.yaml` | 新增 | 示例 Shell 工具配置 |
| `config/browser_tools.yaml` | 新增 | 示例 Browser 工具配置 |
| `config/sql_tools.yaml` | 新增 | 示例 SQL 工具配置 |
| `tests/test_shell_executor.py` | 新增 | Shell 执行器测试 |
| `tests/test_browser_executor.py` | 新增 | Browser 执行器测试 |
| `tests/test_sql_executor.py` | 新增 | SQL 执行器测试 |
| `tests/test_encrypted_secret_backend.py` | 新增 | 加密 Secret 后端测试 |
| `src/KNOWN_LIMITATIONS.md` | 修改 | 更新 v0.24.0 边界 |
| `src/development_log.md` | 修改 | 追加 v0.24.0 记录 |
| `src/loop_controller_v0.24.0_development.md` | 新增 | 本文档 |

## 5. 配置模型

### 5.1 `config/shell_tools.yaml`

```yaml
tools:
  run_kubectl:
    command: kubectl
    description: 在显式命名空间下执行 kubectl 命令
    input_schema:
      type: object
      properties:
        namespace: {type: string}
        args: {type: string}
      required: [args]
    allowed_patterns:
      - ^kubectl\s+get\s+.*$
      - ^kubectl\s+describe\s+.*$
    default_risk: critical
    cost_per_call: 100
    sandbox:
      timeout_seconds: 30
      max_output_bytes: 131072
      env_whitelist: ["KUBECONFIG"]
```

### 5.2 `config/browser_tools.yaml`

```yaml
tools:
  fetch_page_content:
    description: 获取渲染后网页正文
    input_schema:
      type: object
      properties:
        url: {type: string, format: uri}
      required: [url]
    allowed_hosts: ["*.example.com", "docs.openai.com"]
    action: extract_text
    default_risk: high
    cost_per_call: 50
```

### 5.3 `config/sql_tools.yaml`

```yaml
data_sources:
  company_db:
    driver: postgresql
    host: db.company.com
    port: 5432
    database: analytics
    secret_ref: {name: db_password}

tools:
  query_analytics:
    data_source: company_db
    description: 只读查询 analytics 数据库
    input_schema:
      type: object
      properties:
        sql: {type: string}
      required: [sql]
    read_only: true
    allowed_patterns:
      - ^SELECT\s+.*$
    default_risk: high
    cost_per_call: 100
```

## 6. 关键设计决策

### 6.1 Shell 白名单语义

- `allowed_patterns` 为正则表达式列表；命令字符串必须完整匹配其中一条；
- 默认拒绝交互式 shell、重定向到文件系统敏感路径、`sudo` 等；
- 支持 `${ENV}` 解析，但环境变量必须在 `env_whitelist` 中声明。

### 6.2 Browser 隔离

- 每个调用启动独立浏览器上下文（context），不共享 cookie/storage；
- 限制可访问域名；支持代理、截图尺寸、超时；
- 返回 DOM 文本或截图 Base64，不返回原始 HTML 以降低信息泄露。

### 6.3 SQL 只读/写分离

- `read_only=true` 时执行前校验 SQL 必须以 `SELECT`/`WITH` 开头；
- `read_only=false` 需通过审批策略（默认 `critical` 风险）；
- 连接字符串中的密码通过 Secret Broker 注入，配置文件中不保留明文。

### 6.4 容器后端（可选）

- 定义 `ContainerBackend` 协议：`run(image, command, env, mounts) -> ToolResult`；
- 提供 `DockerContainerBackend`（依赖 docker SDK）；
- `LocalFunctionExecutor` 和 `ShellExecutor` 默认使用子进程，配置 `container_image` 后切换为容器；
- 容器镜像内置 Python、playwright、数据库驱动等执行时依赖。

### 6.5 加密 Secret 后端

- `EncryptedFileSecretBackend` 继承 `FileSecretBackend`；
- 支持通过环境变量 `LOOP_CONTROLLER_SECRET_KEY` 或 KMS 接口加解密；
- Vault/KMS 实现先提供接口与 mock 测试，真实集成放在 v0.24.x 或 v0.25.0。

## 7. 风险与回退

| 风险 | 缓解 |
|---|---|
| Shell 工具破坏系统 | 默认禁用 + 命令正则白名单 + 超时 + 可选容器 |
| Browser 引入大依赖 | 作为 `[browser]` extra；未安装时相关工具不可用 |
| SQL 注入 | SQL 白名单 + 参数化查询；写操作默认需审批 |
| 容器后端不可用 | 自动回退到子进程隔离 |
| Secret 加密密钥泄露 | 密钥仅通过环境变量注入，不落地配置文件 |

## 8. 验收标准

- `pytest tests/`：新增执行器与加密后端测试全部通过，整体无回归；
- `ruff check src tests examples`：通过；
- `mypy src`：通过；
- 新增工具能在示例配置中注册、 governance 链路中按风险等级正常审批/执行；
- 未安装 `[browser]`/`[sql]`/`[container]` extras 时，相关工具优雅失败并给出清晰错误码。
