# v0.31.0 外部工具执行沙箱（Harness）

> 一句话目标：**把 Loop Controller 从“相信 Agent 会自觉走 MCP/HTTP 受控执行器”升级为“默认不信任治理入口上的工具调用，敏感工具必须通过外部 Harness 在受限沙箱中执行，网络、文件、命令出口默认全部 deny”。**
>
> 范围限定：本版本只约束通过 ToolGovernor / MCP Proxy / HTTP / gRPC 进入 Loop Controller 的工具调用；Agent 进程内部绕过这些入口的代码仍不在本版本治理范围内。

- 状态：**已实现**
- 实际实现版本：v0.31.0
- 前置版本：v0.30.0 持久化一致性与崩溃恢复加固
- 版本性质：执行面安全边界升级
- 核心范围：Harness 默认化、沙箱策略、执行模式路由、Harness 协议 v2、证据回传、健康熔断
- 验证结果：pytest 730 passed / ruff passed / mypy passed / git diff --check passed

---

## 1. 背景

v0.28.0–v0.30.0 主要解决“治理判定正确且持久化可靠”，但**实际副作用发生在哪、治理入口以内的调用能否被强制走沙箱**，仍然依赖执行器自身的实现。

当前架构下：

- MCP 工具通过 `MCPGateway` 拉起 stdio 子进程；
- HTTP 工具通过受控 `httpx` 客户端直连；
- 本地函数通过 `LocalFunctionExecutor` 启动独立 Python 子进程；
- Harness 工具通过 `HarnessExecutor` 转发到外部 Harness 服务。

v0.31.0 首先解决“治理入口以内”的问题：只要调用经过 Loop Controller 的 ToolGovernor / MCP Proxy / HTTP / gRPC，默认必须走 Harness。

**本版本未解决、留到后续版本的问题**：

1. **Agent 不配合时无法约束**：如果 Agent 自己写了一个本地函数或 HTTP 调用，绕过 `ToolGovernor.call()`，Loop Controller 没有任何外部机制限制它访问网络、文件系统或启动子进程。这需要把 Agent 本身放进受限进程/容器/透明代理中，属于 v0.32.0 或单独版本的范围。
2. **Harness 是可选特性**：`config/harness_tools.yaml` 默认全部注释，敏感工具仍然落在 MCP/HTTP/Local 执行器上。
3. **沙箱参数只有声明、没有强制证据**：`HarnessSandboxConfig` 的 `allowed_hosts`、`allowed_paths` 只是传给 Harness 的“建议”，Harness 不强制、Loop Controller 也不校验 Harness 是否真正执行了隔离。
4. **执行结果不可审计**：Harness 执行了什么、尝试访问了哪些文件/网络、是否越界，当前没有结构化证据回传。
5. **Harness 健康与熔断未接入执行门控**：后端不可用时，调用会失败，但没有统一的 fail-closed 策略。

v0.31.0 不新增治理规则，而是**把执行面本身变成可强制隔离的边界**。

---

## 2. 当前问题清单

### P0-1：治理入口上的敏感工具默认仍在主进程或半隔离子进程中执行

MCP server、HTTP client、本地函数 runner 虽然不在主事件循环里运行，但仍与 Loop Controller 同一用户/网络命名空间，可以访问主机的文件、环境变量、网络。

风险：Agent 通过治理入口调用本地函数读取 `~/.aws/credentials`，再构造 HTTP 请求外发。

注意：本版本只解决“调用已经经过 ToolGovernor / MCP Proxy 等入口”的情况；Agent 进程内部绕过治理入口的代码需要后续版本对 Agent 进程本身做约束。

### P0-2：Harness 是“可选增强”，不是“默认执行模式”

`harness_tools.yaml` 默认全部注释。 production 中如果管理员忘记配置 Harness，shell、write_file、fetch_url 等敏感工具会落到普通执行器。

风险：配置遗漏 = 安全边界消失。

### P0-3：执行模式没有 fail-closed 的兜底

`ExecutorRegistry.get_executor()` 的逻辑是：先找已注册执行器，再回退 default。没有任何逻辑判断“这个工具是否允许在本地执行”。

风险：新增敏感工具时，如果不显式绑定 Harness，默认会在本地执行。

### P0-4：Harness 协议没有约束回执和越界证据

当前 `HarnessExecuteResponse` 只返回 `status / content / error_code / metadata`，没有：

- 实际生效的沙箱参数；
- 被拦截的网络/文件访问尝试；
- 子进程退出码、运行时长、资源使用；
- Harness 对请求的签名回执。

风险：Harness 声称成功，但私下越界访问；Loop Controller 无法审计。

### P0-5：Harness 健康异常不阻断执行面

`_BackendState.status` 会变为 `unhealthy`，但 `forward()` 在调用前没有统一检查。某些路径可能继续调用已不可用的后端，导致请求 hang 或返回不可信结果。

### P1-1：Docker Harness 后端未实现

`HarnessBackendConfig` 已声明 `DockerBackendConfig`，但 `HarnessExecutor._build_backend()` 只实现了 `subprocess` 和 `http`。

### P1-2：Harness 认证 nonce 池是进程内内存，重启后丢失

参考 `harness_server.py` 的 `_NonceStore` 只存在于单个 Harness 进程内存。Harness 重启后会接受旧重放请求。生产环境需要持久化或分布式 nonce 存储（外部 Redis / 数据库）。

### P1-3：管理员缺少 Harness 状态观测接口

当前只有代码内 `backend_statuses()`，没有 HTTP / gRPC / CLI 接口暴露给运维。

---

## 3. 设计原则

### 3.1 治理入口上的执行面默认不信任

- 任何经过 ToolGovernor / MCP Proxy / HTTP / gRPC 入口、且可能产生副作用的工具调用，默认必须通过 Harness 执行；
- 只有显式声明为 `trusted_local` 的工具才允许在 Loop Controller 进程内或半隔离子进程中执行；
- `trusted_local` 审批由 admin 显式配置，并写入审计；
- Agent 进程内部自发执行的代码不在本版本约束范围内。

### 3.2 沙箱策略由 Loop Controller 决定、Harness 证明

- `HarnessSandboxConfig` 由 Loop Controller 根据 Profile + Rego + 工具规格生成；
- Harness 必须按配置执行，并在响应中证明“实际生效的约束”；
- 如果 Harness 返回的 `effective_sandbox` 与请求不一致，Loop Controller 拒绝结果并告警。

### 3.3 网络/文件/命令出口默认 deny

- `network_policy`: `deny_all`（默认），可选 `allow_list` / `loopback_only`；
- `file_policy`: `deny_all`（默认），可选 `read_only_list` / `read_write_list`；
- `process_policy`: 除显式允许列表外，禁止启动新进程。

### 3.4 越界即证据

- Harness 必须记录每一次被拦截或允许的出口访问；
- 回传给 Loop Controller 后写入 Audit + Evidence；
- 严重越界直接升级为 alert 并可能 kill-switch。

### 3.5 后端不可用时 fail-closed

- 所有 Harness 调用前检查后端健康；
- `unhealthy` 且 `fail_closed_when_unhealthy=true` 时，返回 `harness_backend_unavailable` 并阻止执行；
- 健康恢复后自动恢复。

---

## 4. 总体架构

```text
Agent / ToolGovernor.call()
           │
           ▼
LoopController.evaluate_and_execute()
           │
           ▼
   ExecutionModeResolver   ←── config + Profile + Rego
           │
           ├─ trusted_local ──► Local / MCP / HTTP executor（显式白名单）
           │
           └─ harness ────────► HarnessExecutor ──► HTTP / Subprocess / Docker backend
                          │
                          ▼
                   Harness Sandbox
                          │
                          ▼
              ToolResult + EvidenceRecord
                          │
                          ▼
                    Audit / Evidence / Alert
```

新增/重点修改模块：

| 模块 | 路径 | 职责 |
|---|---|---|
| 执行模式解析器 | `src/loop_controller/execution_mode.py` | 根据工具、Profile、Rego、全局策略决定走本地还是 Harness |
| Harness 协议 v2 | `src/loop_controller/executors/harness_protocol.py` | 扩展请求/响应，增加约束回执、越界证据、资源使用 |
| 内置隔离子进程后端 | `src/loop_controller/executors/isolated_subprocess_harness.py` | 不依赖外部服务，直接用受限子进程跑任意工具 |
| Docker 后端 | `src/loop_controller/executors/docker_harness_backend.py` | 启动一次性容器执行 |
| Harness 策略校验器 | `src/loop_controller/executors/harness_policy_validator.py` | 校验响应中的 `effective_sandbox` 与请求一致 |
| 证据捕获 | `src/loop_controller/audit/harness_evidence.py` | 把 Harness 越界证据写入 Audit/Evidence |
| 管理接口 | `src/loop_controller/server.py` / `grpc_server.py` | 暴露 Harness 后端状态、强制刷新、手动熔断 |
| 配置 | `config/harness_tools.yaml` / `config/execution_policy.yaml` | 默认 deny、trusted_local 白名单 |

---

## 5. 详细设计

### 5.1 执行模式解析器（ExecutionModeResolver）

新增类 `ExecutionModeResolver`，在 `Controller.evaluate()` 或 `Checkpoint.forward()` 阶段调用。

决策逻辑（按优先级从高到低）：

1. **Kill Switch / Revocation**：命中直接 deny，不走任何执行器。
2. **工具级配置**：
   - 如果 `execution_policy.yaml` 中该工具 `mode = "trusted_local"` → 允许本地执行；
   - 如果 `mode = "harness_required"` → 必须 Harness；
   - 如果 `mode = "harness_preferred"` → 有健康 Harness 则走 Harness，否则本地执行。
3. **风险等级兜底**：
   - `default_risk` 为 `critical` / `high` 的工具，默认 `harness_required`；
   - `medium` / `low` 默认 `harness_preferred`。
4. **全局开关**：
   - `harness.fail_closed_when_unhealthy = true` 时，没有健康 Harness 则拒绝执行。

返回：

```python
class ExecutionMode(Enum):
    TRUSTED_LOCAL = "trusted_local"
    HARNESS = "harness"
    DENY = "deny"
```

如果返回 `DENY`，`Checkpoint.forward()` 直接返回 `GovernanceResult(status="deny", error_code="execution_mode_denied")`。

### 5.2 Harness 协议 v2

#### 5.2.1 请求扩展

```python
class HarnessSandbox(BaseModel):
    timeout_seconds: float
    max_output_bytes: int
    network_policy: Literal["deny_all", "allow_list", "loopback_only"]
    allowed_hosts: list[str]
    file_policy: Literal["deny_all", "read_only_list", "read_write_list"]
    allowed_paths: list[str]
    readonly_paths: list[str]
    env_whitelist: list[str]
    process_policy: Literal["deny_all", "allow_list"]
    allowed_commands: list[str]
    evidence_capture: Literal["none", "stdout", "all"]
    resource_limits: ResourceLimits | None = None
```

#### 5.2.2 响应扩展

```python
class HarnessExecuteResponse(BaseModel):
    status: Literal["success", "error"]
    content: Any | None = None
    error_code: HarnessErrorCode | None = None
    effective_sandbox: HarnessSandbox | None = None
    evidence: HarnessEvidence | None = None
    metadata: dict[str, Any]
```

```python
class HarnessEvidence(BaseModel):
    started_at: datetime
    finished_at: datetime
    exit_code: int | None = None
    max_memory_bytes: int | None = None
    cpu_milliseconds: int | None = None
    network_attempts: list[NetworkAttempt]
    file_attempts: list[FileAttempt]
    stdout_sha256: str | None = None
    stderr_sha256: str | None = None
```

#### 5.2.3 认证增强

- 保留 `hmac_sha256`；
- `canonical` 字符串增加 `sandbox` 字段的规范化摘要，防止 Harness 擅自放宽沙箱；
- nonce 支持两种模式：
  - 单进程：内存 `_NonceStore`（默认）；
  - 生产：可选 `redis_url` / `etcd` 后端共享 nonce 存储（P1）。

### 5.3 沙箱策略回执校验

新增 `HarnessPolicyValidator`：

1. 比较请求 `sandbox` 与响应 `effective_sandbox`；
2. 如果响应缺少 `effective_sandbox`，视为降级错误；
3. 如果 `allowed_hosts`、`allowed_paths` 比请求更宽松，拒绝结果并写入 `harness_sandbox_policy_violation` alert；
4. 如果响应包含越界 `network_attempts` / `file_attempts`，按严重程度写入 audit + alert。

校验失败的处理：

- 不把结果返回给 Agent；
- 返回 `GovernanceResult(status="error", error_code="harness_sandbox_violation")`；
- 写入 audit event：`action=execution`, `status=harness_violation`。

### 5.4 内置隔离子进程后端（Isolated Subprocess Harness）

新增 `_IsolatedSubprocessHarnessBackend`：

- 不依赖 Docker / Kubernetes / 外部服务；
- 启动一个独立 Python 子进程，运行 `loop_controller.executors.isolated_runner`；
- 子进程仅加载白名单模块，禁用 `socket`、`os.system`、`subprocess` 等敏感 builtins（通过 `RestrictedPython` 或 import 钩子）；
- 文件访问通过 `allowed_paths` 过滤；
- 网络访问通过 allowed_hosts 代理或禁止；
- 执行完后返回 `HarnessExecuteResponse`。

限制：

- 平台：优先支持 Linux / Windows / macOS 的通用子进程隔离，不保证完整容器级隔离；
- 用于开发、CI 和低敏感生产场景；
- 高敏感场景仍推荐 Docker / 远程 Harness。

### 5.5 Docker 后端实现

新增 `_DockerHarnessBackend`：

- 使用 Docker SDK 或 CLI 启动一次性容器；
- `network_mode` 默认 `"none"`；
- 通过 `--mount` 只读挂载 `allowed_paths`；
- 容器镜像内预装 `loop-controller-harness-runner`；
- 调用完成后读取容器 stdout 作为 `HarnessExecuteResponse`。

配置示例：

```yaml
backends:
  docker_harness:
    type: docker
    image: loop-controller/harness:latest
    network_mode: none
    mounts:
      - source: /data/output
        target: /data/output
        read_only: false
    max_concurrent_calls: 5
    acquire_timeout_seconds: 2
```

### 5.6 执行入口改造

`Checkpoint.forward()` 改造：

```python
mode = self._execution_mode_resolver.resolve(proposal)
if mode == ExecutionMode.DENY:
    return GovernanceResult(status="deny", error_code="execution_mode_denied")

if mode == ExecutionMode.HARNESS:
    # 检查是否有健康的 Harness 后端
    if not self._harness_executor.has_healthy_backend():
        if self._config.harness_fail_closed_when_unhealthy:
            return GovernanceResult(status="error", error_code="harness_backend_unavailable")
        # 否则 fallback 到 trusted_local（仅当工具本身允许）
```

`Runtime._build_executor_registry()` 改造：

- 保留 MCP / HTTP / Local 注册；
- 新增 `harness_fallback` 注册：对于没有显式注册但命中 `harness_required` 的工具，统一路由到 `HarnessExecutor`；
- 推荐把 `HarnessExecutor` 设为 default executor，让未知工具全部走 Harness。

### 5.7 审计与证据

每次 Harness 执行无论成功失败都写入 Audit：

```json
{
  "action": "tool_execution",
  "tool_name": "production_shell",
  "execution_mode": "harness",
  "backend": "docker_harness",
  "status": "success",
  "effective_sandbox": {...},
  "evidence": {
    "network_attempts": [],
    "file_attempts": []
  }
}
```

如果开启 Evidence chain，把 `evidence` 对象的 hash 作为 evidence event 附加。

### 5.8 管理接口

新增 HTTP 端点：

- `GET /v1/admin/harness/status`：列出所有 Harness 后端状态、in_flight、consecutive_failures、last_error_code。
- `POST /v1/admin/harness/{name}/drain`：停止接收新请求，等待在途完成。
- `POST /v1/admin/harness/{name}/reset`：清空失败计数，重新健康检查。

gRPC 对应新增 RPC。

---

## 6. 配置变更

### 6.1 新增 `config/execution_policy.yaml`

```yaml
execution_policy:
  default_mode: harness_required          # harness_required / harness_preferred / trusted_local
  fail_closed_when_unhealthy: true
  allow_fallback_to_local: false

  tools:
    read_file:
      mode: harness_preferred
      sandbox:
        file_policy: read_only_list
    write_file:
      mode: harness_required
      sandbox:
        file_policy: read_write_list
    fetch_url:
      mode: harness_required
      sandbox:
        network_policy: allow_list
    production_shell:
      mode: harness_required
      sandbox:
        network_policy: deny_all
        file_policy: deny_all
        process_policy: allow_list
        allowed_commands: ["kubectl", "systemctl"]

  trusted_local_tools:
    - echo
    - get_current_time
```

### 6.2 `config/harness_tools.yaml` 改造

- 取消“全部注释”的默认配置；
- 提供 `default` Harness backend（subprocess 或 http）示例；
- 明确说明：生产必须使用 HTTP / Docker，subprocess 仅用于开发测试。

---

## 7. 接口变更

### 7.1 新增

- `ExecutionModeResolver`
- `ExecutionMode`
- `HarnessPolicyValidator`
- `IsolatedSubprocessHarnessBackend`
- `DockerHarnessBackend`
- `HarnessExecuteResponse.effective_sandbox`
- `HarnessExecuteResponse.evidence`
- `HarnessSandbox.network_policy`
- `HarnessSandbox.file_policy`
- `HarnessSandbox.process_policy`
- `HarnessSandbox.evidence_capture`
- `HarnessSandbox.resource_limits`
- `GET /v1/admin/harness/status`
- `POST /v1/admin/harness/{name}/drain`
- `POST /v1/admin/harness/{name}/reset`

### 7.2 修改

- `Checkpoint.forward()`：增加执行模式解析和健康检查；
- `HarnessExecutor`：支持 backend fallback、default executor 模式、policy validation；
- `HarnessBackendConfig` / `HarnessSandboxConfig`：扩展字段；
- `Runtime._build_executor_registry()`：支持 Harness 作为 default executor。

---

## 8. 测试计划

### 8.1 单元测试

- `tests/test_execution_mode.py`
  - 风险等级默认映射；
  - 工具级配置覆盖全局默认；
  - `fail_closed_when_unhealthy` 分支；
  - Kill Switch / Revocation 优先于执行模式。

- `tests/test_harness_policy_validator.py`
  - `effective_sandbox` 与请求一致 → 通过；
  - `effective_sandbox` 更宽松 → 拒绝并告警；
  - 缺少 `effective_sandbox` → 拒绝；
  - 越界 `network_attempts` → 写入 audit + alert。

- `tests/test_isolated_subprocess_harness.py`
  - 成功执行 `echo`；
  - 访问 `allowed_paths` 外文件被拒绝；
  - 访问未允许网络被拒绝；
  - 超时返回 `harness_timeout`。

- `tests/test_docker_harness_backend.py`（可选，需要 Docker 环境时运行）
  - 启动一次性容器执行；
  - `network_mode=none` 生效；
  - 只读挂载生效。

### 8.2 集成测试

- `tests/test_harness_protocol_v2.py`
  - 请求/响应往返序列化；
  - HMAC 签名覆盖 sandbox 字段；
  - 越界证据解析。

- `tests/test_server_harness_admin.py`
  - `GET /v1/admin/harness/status` 返回正确状态；
  - drain/reset 端点生效。

- `tests/test_checkpoint_harness_fail_closed.py`
  - Harness 后端 unhealthy 且 `fail_closed_when_unhealthy=true` 时执行被拒绝；
  - 恢复 healthy 后执行恢复。

### 8.3 端到端测试

- Agent 通过 ToolGovernor / MCP Proxy 调用敏感工具，默认路由到 Harness；
- Agent 通过 HTTP / gRPC 入口调用未声明 `trusted_local` 的敏感工具，默认路由到 Harness；
- Harness 响应中的越界网络访问写入 Alert；
- （本版本不测试）Agent 进程内部绕过 `ToolGovernor` 直接调用本地函数——该场景需要后续版本对 Agent 进程本身施加约束。

---

## 9. 验收标准

1. `python -m pytest -q` 全部通过；
2. `python -m ruff check src tests` 通过；
3. `python -m mypy src/loop_controller` 通过；
4. `config/execution_policy.yaml` 存在且 `default_mode = harness_required` 时，未显式声明 `trusted_local` 的工具全部走 Harness；
5. `HarnessExecuteResponse` 必须携带 `effective_sandbox`，否则 Loop Controller 拒绝结果；
6. Harness 后端 unhealthy 时，执行面 fail-closed（除非显式允许 fallback）；
7. 新增 `/v1/admin/harness/status` 端点可列出所有后端健康状态；
8. Docker 后端实现可运行（本地有 Docker 时）；
9. 文档说明：subprocess harness 仅用于开发，生产必须使用 HTTP 或 Docker Harness。

---

## 10. 非目标

- **不对 Agent 进程本身做强制约束**：如果 Agent 绕过 ToolGovernor / MCP Proxy / HTTP / gRPC 入口，在进程内部直接执行代码，本版本无法拦截。该目标放到后续版本（v0.32.0 或单独版本）。
- 不替代操作系统级安全边界（seccomp、AppArmor、SELinux、Windows 防火墙规则）；Harness 是在这些机制之上再加一层应用级约束。
- 不实现跨主机分布式 Harness 调度（放到 v0.32.0 多机持久化之后）。
- 不为 Agent 提供自行声明沙箱的能力；沙箱参数只能由 Loop Controller 根据配置和策略生成。
- 不在本版本解决 Harness nonce 跨进程持久化（P1，可借助外部 Redis）。

---

## 11. 风险与回退

| 风险 | 缓解 |
|---|---|
| Harness 后端性能开销导致吞吐下降 | 提供并发上限、连接池、backend 多实例；关键路径保留 `trusted_local` 白名单。 |
| Docker 在 Windows/macOS 体验不一致 | 内置 Isolated Subprocess Harness 作为跨平台兜底。 |
| 旧配置没有 `execution_policy.yaml` | 向后兼容：缺失时按当前行为运行，但默认 `default_mode = harness_preferred` 并告警建议升级。 |
| Harness 响应格式不兼容 v1 | 协议版本号升级为 `2`，v1 Harness 仍可通过 `harness_protocol_version` 协商拒绝或降级。 |

---

## 12. 备注

- 本版本重点是把 Harness 从“可选示例”变成“治理入口上的默认安全边界”，因此配置默认需要启用至少一个 backend；如果没有配置任何 backend，Runtime 启动时应进入 `write_blocked` 或 `execution_blocked` 状态。
- 本版本不解决 Agent 进程内部自发执行代码的约束，该缺口在 v0.32.0 或后续版本中通过进程/容器/透明代理级约束补齐。
- 修复时只补执行面隔离缺口，不要改动 v0.30.0 已加固的持久化语义。
