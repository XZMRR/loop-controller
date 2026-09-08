# v0.24.0 开发文档：架构收敛 — MCP 包装 + Harness 接入 + 加密 Secret 后端

## 1. 目标

v0.23.2 已完成 v0.23.1 残留问题的收尾。v0.24.0 的核心任务是根据
`src/audit_report.md` 的架构审查结论，**把 Loop Controller 从“工具运行时”收敛回“治理控制平面”**：

> **一句话目标**：删除 Shell/SQL 内置执行器规划，改为提供高危工具的 MCP 包装示例与 Harness 接入规范，并完成 EncryptedFileSecretBackend。

v0.24.0 做以下三件事：

1. **明确架构边界**：Loop Controller 内部只保留 MCP / HTTP 协议型工具代理；
2. **提供替代接入方式**：新增 `examples/contrib/mcp_wrappers/` 与 `examples/contrib/harness/`；
3. **完成加密 Secret 后端**：新增 `EncryptedFileSecretBackend`，secret 落盘 AES-256-GCM 加密。

v0.24.0 **不做**：

- 不再新增 `ShellExecutor`、`BrowserExecutor`、`SQLExecutor` 等内置执行器；
- 不再把容器隔离后端做进 Loop Controller 内部执行器（容器隔离留给 Harness）。

## 2. 背景与动机

### 2.1 审计报告的核心结论

Loop Controller 的正确边界是统一的治理闭环：

```text
身份认证 → 工具目录 → 风险分类 → 策略判定 → 审批 → 执行器路由 → 审计
```

它只应回答三个问题：

1. **谁**在调用？（身份）
2. **能不能**调用？（策略/风险/审批）
3. **调用后发生了什么？**（审计/预算/吊销）

具体工具怎么跑，不应由 Loop Controller 内部实现。

| 能力 | 属于 Loop Controller | 不属于 Loop Controller |
|---|---|---|
| 身份认证 | ✅ | ❌ |
| 策略引擎 | ✅ | ❌ |
| 审批工作流 | ✅ | ❌ |
| 审计链 | ✅ | ❌ |
| MCP 协议代理 | ✅ | ❌ |
| HTTP API 代理 | ✅ | ❌ |
| 执行 Python 函数 | ⚠️ 可选辅助 | ❌ 不应成为核心 |
| 执行 Shell 命令 | ❌ | ✅ 应由工具侧或 Harness 处理 |
| 执行 SQL | ❌ | ✅ 应由数据库工具或 Harness 处理 |
| 运行浏览器 | ❌ | ✅ 应由浏览器工具或 Harness 处理 |

### 2.2 替代方案

对于 Shell / SQL / Browser，推荐两种接入方式：

**方式 1：MCP 包装示例**

把工具能力包装成独立的 MCP Server：

```text
examples/contrib/mcp_wrappers/shell_mcp_server.py
examples/contrib/mcp_wrappers/sql_mcp_server.py
examples/contrib/mcp_wrappers/browser_mcp_server.py
```

这些 server 作为独立进程运行，Loop Controller 通过 `MCPGateway` 转发调用。
它们自身可跑在容器/沙箱中，避免把执行能力带入 Loop Controller 进程。

**方式 2：Harness 接入**

Agent 和工具进程跑在受控沙箱/容器中：

```text
examples/contrib/harness/harness_sdk.py
examples/contrib/harness/docker_harness.py
examples/contrib/harness/Dockerfile
examples/contrib/harness/runner.py
```

Harness 拦截调用并交给 Loop Controller 决策；
决策允许后，Harness 在隔离环境中执行。

### 2.3 LocalFunctionExecutor 的重新定位

`LocalFunctionExecutor`（v0.23.0）保留，但需重新定位：

- 仅作为 **“不方便包装成 MCP 时的可选辅助”**；
- 不作为核心架构方向；
- 后续优先推荐企业把函数包装成 MCP Server 或 HTTP API；
- 如果内部执行，应逐步迁移到可选 Harness/容器后端，而不是 Loop Controller 本进程。

## 3. 新增/修改文件

| 文件 | 操作 | 说明 |
|---|---|---|
| `src/loop_controller/executors/shell_executor.py` | 删除 | 不再内置 Shell 执行器 |
| `src/loop_controller/executors/shell_models.py` | 删除 | 同上 |
| `src/loop_controller/executors/sql_executor.py` | 删除 | 不再内置 SQL 执行器 |
| `src/loop_controller/executors/sql_models.py` | 删除 | 同上 |
| `src/loop_controller/executors/__init__.py` | 修改 | 移除 Shell/SQL 导出 |
| `src/loop_controller/infra/config_loader.py` | 修改 | 移除 `shell_tools.yaml` / `sql_tools.yaml` 加载 |
| `src/loop_controller/runtime.py` | 修改 | 移除 Shell/SQL 执行器构造与注册 |
| `config/shell_tools.yaml` | 删除 | 不再需要的配置 |
| `config/sql_tools.yaml` | 删除 | 同上 |
| `tests/test_shell_executor.py` | 删除 | 同上 |
| `tests/test_sql_executor.py` | 删除 | 同上 |
| `examples/contrib/mcp_wrappers/shell_mcp_server.py` | 新增 | Shell MCP 包装示例 |
| `examples/contrib/mcp_wrappers/sql_mcp_server.py` | 新增 | SQL MCP 包装示例 |
| `examples/contrib/mcp_wrappers/browser_mcp_server.py` | 新增 | Browser MCP 包装示例（占位） |
| `examples/contrib/harness/harness_sdk.py` | 新增 | 最小 Harness SDK 示例 |
| `examples/contrib/harness/docker_harness.py` | 新增 | 容器化 Harness 示例 |
| `examples/contrib/harness/Dockerfile` | 新增 | Harness 容器镜像 |
| `examples/contrib/harness/runner.py` | 新增 | 容器内工具执行器 |
| `src/loop_controller/secrets/encrypted_file_backend.py` | 新增 | AES-256-GCM 加密 Secret 后端 |
| `src/loop_controller/secrets/__init__.py` | 修改 | 导出 `EncryptedFileSecretBackend` |
| `src/loop_controller/runtime.py` | 修改 | 支持 `secrets.backend.type=encrypted_file` |
| `tests/test_encrypted_secret_backend.py` | 新增 | 加密后端测试 |
| `src/KNOWN_LIMITATIONS.md` | 修改 | 更新 v0.24.0 边界声明 |
| `src/README.md` | 修改 | 更新架构定位 |
| `src/development_log.md` | 修改 | 追加 v0.24.0 记录 |
| `pyproject.toml` / `uv.lock` | 修改 | 增加 `cryptography` 依赖 |

## 4. EncryptedFileSecretBackend

### 4.1 文件格式

与 `FileSecretBackend` 目录结构相同，但 JSON 文件中的 `value` 字段是密文：

```json
{
  "value": "base64(nonce || ciphertext || tag)",
  "encrypted": true,
  "version": "1",
  "expires_at": "2026-12-31T23:59:59Z"
}
```

未加密文件仍兼容，按 `encrypted=false` 处理。

### 4.2 密钥来源

- 环境变量 `LC_SECRET_ENCRYPTION_KEY`（默认）；
- 或在 `config/secrets.yaml` 中指定 `backend.key_env`；
- 32 字节，支持 hex（64 字符）或 base64 编码。

### 4.3 启用方式

```yaml
# config/secrets.yaml
backend:
  type: encrypted_file
  base_path: ./secrets
  key_env: LC_SECRET_ENCRYPTION_KEY
```

### 4.4 加密工具

`EncryptedFileSecretBackend.encrypt(plaintext, key)` 静态方法可供管理脚本使用。

## 5. 关键设计决策

1. **Loop Controller 不再实现非协议型工具执行器**：Shell / SQL / Browser 通过 MCP 包装或 Harness 接入；
2. **MCP 包装示例独立运行**：它们不依赖 Loop Controller 进程，可部署在容器/沙箱中；
3. **Harness 示例演示“治理决策 + 隔离执行”的分层**：Harness 先调用 `/v1/govern/tool-call`，获得 allow 后再在本地或 Docker 中执行；
4. **LocalFunctionExecutor 降级为可选辅助**：保留但不再扩展；
5. **EncryptedFileSecretBackend 继承 FileSecretBackend**：复用目录扫描、过期校验、权限校验，仅覆写解析逻辑；
6. **fail-closed**：加密后端缺少密钥、密钥长度错误、密文损坏均抛 `SecretError`，启动拒绝加载该 secret。

## 6. 风险与回退

| 风险 | 缓解 |
|---|---|
| 高危工具执行脱离治理 | Harness 示例强制先调 `/v1/govern/tool-call`；MCP 包装 server 由 Loop Controller 的 `MCPGateway` 转发，自然经过 R2 |
| MCP 包装 server 本身被绕过 | 部署在独立容器/沙箱中，限制网络与文件系统 |
| 加密密钥泄露 | 密钥只从环境变量读取，不落盘；定期轮换 |
| 加密后端性能 | 仅启动时加载/热更新时解密，运行期缓存明文 value |

## 7. 验收标准

- `pytest tests/`：全量通过，无回归；
- `ruff check src tests examples`：通过；
- `mypy src`：通过；
- 新增 `EncryptedFileSecretBackend` 测试覆盖加密解密、密钥缺失、密钥长度、明文兼容；
- 文档更新：`src/KNOWN_LIMITATIONS.md`、`src/README.md`、`src/development_log.md` 反映 v0.24.0 方向调整。
