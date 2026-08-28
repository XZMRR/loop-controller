# v0.27.0 开发文档：Harness 生产闭环

> **基线**：v0.26.1（提交 `200bfcc`）。
>
> **版本定位**：本版本继续完善 v0.25.0 引入的 Harness 执行出口，补齐远程 HTTP Harness 的认证、并发控制、健康管理、启动校验、风险接线和参考服务安全契约，使其从“可用桥接”收敛为“可部署、可监控、可验证的生产执行后端”。

---

## 1. 目标

v0.25.0 已建立以下主链路：

```text
Agent
  ↓ MCP / HTTP / gRPC / SDK
Loop Controller
  ├─ 身份
  ├─ Profile / Policy / Risk
  ├─ 审批
  ├─ 预算 / 吊销
  └─ 审计 / 证据
  ↓
HarnessExecutor
  ↓ HTTP/JSON
外部 Harness
  ↓
Shell / SQL / Browser / CLI / 内部脚本
```

但当前 Harness 仍存在生产闭环缺口：

- `max_concurrent_calls` 只是配置字段，没有实际限流；
- 远程 Harness 仅支持客户端发送静态 API Key，参考服务没有认证；
- 工具引用不存在的 backend 时，错误延迟到执行期；
- `default_risk` 没有稳定进入风险分类；
- 健康检查只用于子进程启动，远程 Harness 缺少可观测状态；
- `allowed_hosts`、`allowed_paths`、`env_whitelist` 只是协议字段，参考实现没有形成可验证的强制契约；
- 输出在进程结束后截断，不能限制执行过程中的内存占用；
- Docker 只有配置模型和示例，没有形成明确、诚实的产品边界。

### 1.1 一句话目标

> **让远程 HTTP Harness 成为 Loop Controller 中可认证、可限流、可探活、配置可校验、风险可治理、失败语义明确的正式生产执行出口。**

### 1.2 本版本必须完成

1. Harness 配置启动期完整校验；
2. 每个 backend 的真实并发限制与过载错误；
3. 远程 HTTP Harness 双向身份边界：API Key 认证 + 可选 TLS/mTLS 客户端配置；
4. 请求防重放：时间戳、nonce、请求签名及有限时间窗；
5. 健康检查、运行状态和可观测指标；
6. `default_risk` 接入统一风险分类；
7. 参考 Harness 对超时、输出、工具白名单和声明的沙箱能力执行 fail-closed；
8. Harness 协议版本与错误码稳定化；
9. 完整单元、集成和负向安全测试；
10. 版本号、边界文档和配置示例同步更新。

### 1.3 本版本明确不做

- 不在 Loop Controller 核心中新增 Shell、SQL、Browser 或 Docker 内置执行器；
- 不把 Loop Controller 做成容器编排器；
- 不承诺子进程 Harness 是安全沙箱；
- 不实现 Kubernetes、Firecracker、gVisor 或 VM 调度；
- 不实现 Harness gRPC 协议；
- 不做 Harness 配置热更新；
- 不做跨节点分布式并发配额；
- 不做完整多租户隔离；
- 不做 KMS/HSM、远程证据锚点或完整远程审计仓库；
- 不做 UI 控制台、计费系统或新的策略语言。

---

## 2. 核心架构决策

### 2.1 Loop Controller 仍是控制平面

Loop Controller 负责：

- 认证 Agent；
- 检查 Profile、Policy、Risk、Approval、Budget、Revocation；
- 选择 Harness backend；
- 对 Harness 请求签名；
- 限制发往 backend 的并发；
- 记录调用与后端健康状态；
- 将受控结果返回 Agent。

Loop Controller 不负责：

- 解释 Shell 命令；
- 连接数据库；
- 启动浏览器；
- 挂载生产目录；
- 创建生产容器或网络策略；
- 替 Harness 判断某条系统调用是否安全。

### 2.2 生产主路径固定为远程 HTTP Harness

```text
Loop Controller
  ↓ HTTPS（推荐 mTLS）+ 请求签名
独立 Harness Service
  ↓
部署层隔离（容器 / K8s / VM / 专用主机）
  ↓
真实工具
```

`SubprocessBackendConfig` 继续保留，但只服务于开发和集成测试。

`DockerBackendConfig` 不再作为 Loop Controller 直接创建容器的承诺。生产 Docker/Kubernetes 应由部署层启动一个 HTTP Harness Service，Loop Controller 仍使用 `HTTPBackendConfig` 调用。

### 2.3 安全保证分层

| 层 | 责任 |
|---|---|
| Loop Controller | 调用者身份、策略、审批、吊销、预算、请求签名、并发控制、审计 |
| Harness Service | 验证来源、防重放、工具路由、参数校验、执行超时、输出上限 |
| 部署环境 | 文件系统、网络、进程、CPU、内存、容器/VM 隔离 |
| 真实工具 | 最小权限凭证、业务授权、幂等和事务语义 |

任何一层都不得把其他层的“配置字段”描述成自身已经实现的安全保证。

---

## 3. 计划修改文件

| 文件 | 操作 | 说明 |
|---|---|---|
| `src/loop_controller/executors/harness_models.py` | 修改 | 扩展认证、TLS、健康、并发等待和协议版本配置 |
| `src/loop_controller/executors/harness_protocol.py` | 修改 | 增加协议版本、签名头语义、健康模型和稳定错误码 |
| `src/loop_controller/executors/harness_executor.py` | 重构 | 并发门控、认证签名、健康状态、启动校验和错误净化 |
| `src/loop_controller/infra/config_loader.py` | 修改 | Harness 配置交叉校验和 fail-fast |
| `src/loop_controller/runtime.py` | 修改 | 启动/停止顺序、健康检查与可观测状态接入 |
| `src/loop_controller/classifier.py` 或现有风险入口 | 修改 | `default_risk` 进入统一风险计算；以实际代码入口为准，不新增平行分类器 |
| `src/loop_controller/server.py` | 可选修改 | 暴露只读 Harness 健康状态；复用现有 admin 鉴权 |
| `src/loop_controller/metrics.py` | 修改 | 增加 Harness 调用、过载、延迟和健康指标；若现有指标模块路径不同则修改实际文件 |
| `config/harness_tools.yaml` | 修改 | 提供远程 HTTP 生产配置模板 |
| `examples/contrib/harness/harness_server.py` | 重构 | 认证、防重放、协议校验和严格输出限制参考实现 |
| `tests/test_harness_executor.py` | 扩展 | 配置、并发、认证、健康和错误语义测试 |
| `tests/test_harness_subprocess.py` | 扩展 | 生命周期和开发边界测试 |
| `tests/test_harness_security.py` | 新增 | 签名、防重放、认证失败和敏感信息脱敏测试 |
| `tests/test_harness_server.py` | 新增 | 参考服务的协议与沙箱契约测试 |
| `src/KNOWN_LIMITATIONS.md` | 修改 | 更新生产保证与未实现边界 |
| `src/development_log.md` | 修改 | 记录 v0.27.0 |
| `README.md`、`src/README.md` | 修改 | 更新版本与 Harness 部署说明 |
| `pyproject.toml` | 修改 | 版本升级到 `0.27.0` |

原则：优先修改现有文件；只有安全测试职责无法清晰放入现有测试文件时，才新增测试文件。

---

## 4. 配置模型

### 4.1 推荐配置

```yaml
backends:
  production_harness:
    type: http
    base_url: https://harness.internal.example
    timeout_seconds: 30
    max_concurrent_calls: 20
    acquire_timeout_seconds: 2

    auth:
      type: hmac_sha256
      key_env: HARNESS_SIGNING_KEY
      key_id: lc-harness-2026-01
      max_clock_skew_seconds: 60

    tls:
      verify: true
      ca_file: /etc/loop-controller/harness-ca.pem
      client_cert_file: /etc/loop-controller/client.pem
      client_key_file: /etc/loop-controller/client-key.pem

    health:
      enabled: true
      path: /health
      startup_required: true
      interval_seconds: 15
      timeout_seconds: 3
      unhealthy_threshold: 3

tools:
  production_shell:
    harness: production_harness
    description: 在企业受控 Harness 中执行预注册运维动作
    default_risk: critical
    cost_per_call: 100
    secret_refs:
      - PROD_OPERATIONS_TOKEN
    input_schema:
      type: object
      properties:
        action:
          type: string
          enum: [list_pods, inspect_deployment]
      required: [action]
      additionalProperties: false
    sandbox:
      timeout_seconds: 30
      max_output_bytes: 65536
      allowed_hosts: []
      allowed_paths: []
      env_whitelist: []
```

### 4.2 建议模型

```python
class HarnessAuthConfig(BaseModel):
    type: Literal["none", "api_key", "hmac_sha256"] = "none"
    key_env: str | None = None
    key_id: str | None = None
    max_clock_skew_seconds: int = 60


class HarnessTLSConfig(BaseModel):
    verify: bool = True
    ca_file: str | None = None
    client_cert_file: str | None = None
    client_key_file: str | None = None


class HarnessHealthConfig(BaseModel):
    enabled: bool = True
    path: str = "/health"
    startup_required: bool = True
    interval_seconds: float = 15.0
    timeout_seconds: float = 3.0
    unhealthy_threshold: int = 3


class HTTPBackendConfig(BaseModel):
    name: str
    type: Literal["http"] = "http"
    base_url: str
    timeout_seconds: float = 30.0
    max_concurrent_calls: int = 10
    acquire_timeout_seconds: float = 2.0
    auth: HarnessAuthConfig = HarnessAuthConfig()
    tls: HarnessTLSConfig = HarnessTLSConfig()
    health: HarnessHealthConfig = HarnessHealthConfig()
```

### 4.3 向后兼容

现有 `api_key_env` 可在 v0.27.0 继续接受，但加载后必须归一化为：

```yaml
auth:
  type: api_key
  key_env: 原 api_key_env
```

不得同时配置 `api_key_env` 和 `auth`。文档标记 `api_key_env` 为 deprecated，计划在后续 breaking version 删除。

### 4.4 启动期校验

加载配置后必须一次性验证：

1. 每个 tool 的 `harness` 引用真实存在；
2. 每个 backend 名称唯一；
3. 生产 HTTP backend 的 URL scheme 必须是 HTTPS；仅 `localhost`/`127.0.0.1` 开发配置可显式允许 HTTP；
4. `auth.type != none` 时所需环境变量存在且非空；
5. mTLS 证书和私钥必须成对配置；
6. TLS 文件存在且可读；
7. `input_schema` 至少是合法 JSON Schema 对象；
8. `max_concurrent_calls >= 1`；
9. tool 和 backend 的 Secret 引用进入统一吊销检查；
10. 不支持的 `DockerBackendConfig` 不得在运行期才抛 `NotImplementedError`。

Docker 配置采用以下二选一方案，开发 Agent 必须选择并在实现中保持一致：

- **推荐**：从正式配置联合类型移除 `DockerBackendConfig`，仅保留示例；
- 或者：配置加载时明确拒绝，并返回稳定配置错误。

不得实现 Loop Controller 直接调用 Docker daemon。

---

## 5. Harness 协议 v1 稳定化

### 5.1 请求体

继续使用：

```http
POST /harness/v1/execute
Content-Type: application/json
X-Harness-Protocol-Version: 1
X-Harness-Key-Id: lc-harness-2026-01
X-Harness-Timestamp: 1787898000
X-Harness-Nonce: <随机值>
X-Harness-Signature: <base64 hmac>
```

请求体继续使用 `HarnessExecuteRequest`，但必须增加显式 `protocol_version: "1"`，或由固定请求头承载。只能选一种权威来源，禁止两处不一致。

### 5.2 Canonical 签名载荷

HMAC-SHA256 签名必须覆盖：

```text
HTTP method
request path
protocol version
key id
timestamp
nonce
sha256(raw request body)
```

建议 canonical 形式：

```text
POST\n/harness/v1/execute\n1\n{key_id}\n{timestamp}\n{nonce}\n{body_sha256}
```

```python
signature = base64.b64encode(
    hmac.new(key, canonical.encode("utf-8"), hashlib.sha256).digest()
).decode("ascii")
```

要求：

- Loop Controller 使用最终发送的原始 body 计算哈希；
- Harness 使用收到的原始 body 验证，不能重新序列化 JSON 后验证；
- 签名比较使用 `hmac.compare_digest()`；
- 不在日志、错误响应或 metadata 中输出 key、签名完整值或原始认证头。

### 5.3 防重放

参考 Harness 必须：

1. 拒绝超出 `max_clock_skew_seconds` 的 timestamp；
2. 在时间窗内缓存已使用的 `(key_id, nonce)`；
3. 重复 nonce 返回 `harness_replay_detected`；
4. nonce 缓存必须有上限和过期清理；
5. 多副本 Harness 的跨实例防重放不在本版本保证范围，生产部署需共享 nonce store 或使用流量粘滞；必须写入边界文档。

### 5.4 稳定错误码

至少固定以下错误码：

| 错误码 | 含义 | 是否可重试 |
|---|---|---|
| `harness_backend_unavailable` | backend 不健康或不可达 | 是 |
| `harness_overloaded` | 并发槽位获取超时 | 是 |
| `harness_request_timeout` | Loop Controller 到 Harness 请求超时 | 视工具幂等性 |
| `harness_auth_required` | 缺少认证 | 否 |
| `harness_auth_failed` | 认证失败 | 否 |
| `harness_replay_detected` | nonce 重放 | 否 |
| `harness_protocol_unsupported` | 协议版本不支持 | 否 |
| `harness_invalid_request` | 请求模型或 schema 非法 | 否 |
| `harness_tool_not_found` | Harness 未注册工具 | 否 |
| `harness_sandbox_violation` | 执行请求违反沙箱规则 | 否 |
| `harness_timeout` | Harness 内真实工具执行超时 | 视工具幂等性 |
| `harness_output_limit_exceeded` | 输出超过限制并已终止 | 否 |
| `harness_invalid_response` | Harness 返回非法响应 | 否 |

Loop Controller 不能把底层 URL、证书路径、原始异常、响应 body 或 Secret 暴露给 Agent；详细信息只写受控内部日志和审计 metadata。

---

## 6. 并发控制与过载保护

### 6.1 每 backend 独立门控

每个 backend 创建独立 `asyncio.Semaphore(max_concurrent_calls)`：

```text
Tool A ─┐
Tool B ─┼→ backend production_harness → semaphore(20)
Tool C ─┘
```

多个工具共享同一 backend 时，必须共享同一个 semaphore，不能按 tool 各建一把。

### 6.2 获取语义

```python
try:
    await asyncio.wait_for(semaphore.acquire(), timeout=acquire_timeout_seconds)
except TimeoutError:
    return ToolResult(error_code="harness_overloaded", ...)
```

释放必须放在 `finally` 中，覆盖：

- 成功；
- HTTP 错误；
- 解析失败；
- 取消；
- 超时；
- 未预期异常。

### 6.3 不做的事情

- 不做跨进程全局并发配额；
- 不做优先级队列；
- 不做自动扩容；
- 不做无限等待；
- 不在 v0.27.0 自动重试真实工具调用。

默认不自动重试，是因为 Shell/SQL/Browser 等工具可能存在不可逆副作用；网络超时不能证明远端没有执行。

---

## 7. 健康检查与运行状态

### 7.1 backend 状态

```python
class HarnessBackendStatus(BaseModel):
    name: str
    status: Literal["unknown", "healthy", "degraded", "unhealthy"]
    checked_at: datetime | None
    consecutive_failures: int
    last_error_code: str | None
```

不得记录原始敏感异常。

### 7.2 启动行为

- `startup_required: true`：启动时健康检查失败，Runtime 启动失败；
- `startup_required: false`：Runtime 可启动，但 backend 标记 `unhealthy`，调用立即返回 `harness_backend_unavailable`；
- 默认不允许首次工具调用隐式启动远程 backend；Runtime 必须显式管理生命周期。

### 7.3 运行期行为

后台探活只更新状态，不自动重放工具调用。

建议状态转换：

```text
unknown → healthy
unknown → unhealthy
healthy --连续失败达到阈值→ unhealthy
unhealthy --一次成功→ healthy
单次业务请求失败 → 指标/告警，不直接等同健康检查失败
```

不得在探活失败时取消已经发出的真实工具调用。

### 7.4 运维可见性

复用现有 admin 鉴权，提供只读状态：

```http
GET /v1/admin/harness/backends
```

响应只包含：

- backend 名称；
- 类型；
-健康状态；
- 最近检查时间；
- 连续失败次数；
- 净化后的错误码；
- 当前 in-flight 数量与配置并发上限。

不得返回 URL 中的凭证、API Key、证书私钥路径或环境变量值。

---

## 8. 风险、策略与工具元数据接线

### 8.1 `default_risk` 必须成为真实输入

当前 `HarnessToolSpec.default_risk` 不能继续只是未使用字段。

要求：

1. 工具注册时将 `default_risk` 放入统一 Tool 元数据或现有分类器可查询的 registry；
2. R1 分类结果不得低于该默认风险；
3. 现有规则根据参数判定出更高风险时取更高值；
4. R2 Profile/OPA/组合策略仍保留最终裁决权；
5. 不专门为 Harness 建立第二套风险分类器。

风险合并语义：

```text
final_r1_risk = max(rule_based_risk, tool_default_risk)
```

风险顺序必须复用现有 `RiskLevel` 定义，不在 Harness 模块复制排序。

### 8.2 输入 schema

Loop Controller 应在发往 Harness 前复用统一工具参数校验。如果当前主链路尚未统一执行 JSON Schema，本版本只要求：

- 配置启动时检查 schema 结构合法；
- 参考 Harness 对工具参数做自身边界校验；
- 不在 HarnessExecutor 内临时实现一套与其他执行器不同的 schema 引擎。

---

## 9. 参考 Harness 的安全契约

参考服务不是生产沙箱，但必须成为协议、安全和失败语义的可信示例。

### 9.1 必须实现

- HMAC/API Key 认证；
- timestamp + nonce 防重放；
- 协议版本检查；
- 明确工具注册表；
- 工具级参数校验；
- 超时后终止并等待子进程退出；
- 输出达到上限时主动终止，不是执行完成后切片；
- stdout/stderr 合计使用统一上限；
- 输出超限返回 `harness_output_limit_exceeded`；
- 请求与错误日志脱敏；
- `/health` 不泄露配置和 Secret。

### 9.2 沙箱字段的诚实语义

对 `allowed_hosts`、`allowed_paths`、`env_whitelist`：

- 参考实现能严格执行的字段必须执行；
- 无法执行时，配置非空必须 fail-closed 返回 `harness_sandbox_unsupported`；
- 禁止静默忽略；
- 真正的网络和文件系统隔离仍由容器/K8s/VM 部署层保证。

### 9.3 Shell 示例限制

Shell 参考工具只能接受动作 ID 或命令 + 独立参数数组，禁止：

- `shell=True`；
- `bash -c` / `sh -c`；
- 用户提供完整命令行字符串后自行 split；
- 管道、重定向、命令替换；
- 继承全部宿主环境变量。

子进程环境必须从空白或最小基础环境构造，仅加入 `env_whitelist` 允许且服务端预配置的变量。客户端不得通过参数指定任意环境变量值。

---

## 10. TLS 与认证边界

### 10.1 TLS

生产远程 Harness 默认要求：

- HTTPS；
- `verify=True`；
- 支持企业 CA；
- 可选 mTLS client certificate；
- 禁止通过配置关闭生产 HTTPS 校验。

本地开发可使用 HTTP，但必须满足：

- host 是 loopback；
- 配置显式标记开发用途；
- KNOWN_LIMITATIONS 中说明没有传输保护。

### 10.2 密钥读取和吊销

- 认证密钥仍通过环境变量读取，不把密钥写进 YAML；
- `key_env` 必须由 `secret_refs_for()` 返回，纳入 v0.26 Secret 吊销；
- 运行期 Secret 轮换不是本版本目标，变更后需要重启；
- 日志和审计只能记录 `key_id` / secret ref 名称，不能记录值。

### 10.3 API Key 与 HMAC

- `api_key` 仅为向后兼容；
- 新生产配置推荐 `hmac_sha256`；
- mTLS 用于传输和服务身份，HMAC 用于请求完整性与应用层来源证明；
- 本版本不实现 OAuth2、JWT 颁发或 PKI 自动化。

---

## 11. 审计与指标

### 11.1 审计 metadata

Harness 调用审计可增加：

- `harness_backend`；
- `harness_protocol_version`；
- `harness_key_id`；
- `harness_status`；
- `harness_error_code`；
- `harness_elapsed_ms`；
- `harness_queue_wait_ms`。

禁止记录：

- API Key/HMAC key；
- 完整签名；
- mTLS 私钥路径内容；
- 未净化的异常；
- 工具返回中的敏感原始凭证。

### 11.2 指标

至少增加：

```text
loop_controller_harness_calls_total{backend,tool,status,error_code}
loop_controller_harness_call_duration_seconds{backend,tool}
loop_controller_harness_queue_wait_seconds{backend}
loop_controller_harness_in_flight{backend}
loop_controller_harness_overloaded_total{backend}
loop_controller_harness_health{backend}
```

如果现有指标系统限制高基数标签，不得把 `agent_id`、`user_id`、`call_id` 放入指标标签。

---

## 12. 失败语义

### 12.1 Fail-closed 场景

以下情况禁止发送真实执行请求：

- 工具引用不存在的 backend；
- backend 配置无效；
- Secret 已吊销；
- backend 已明确 unhealthy；
- 并发槽位获取超时；
- 认证密钥缺失；
- TLS 配置无效；
- 工具参数或协议版本非法。

### 12.2 不确定执行结果

HTTP 请求超时或连接在发送后中断时，Loop Controller 不知道远端是否已执行。

因此必须：

- 返回稳定的 `harness_request_timeout` 或 `harness_request_error`；
- metadata 标记 `execution_outcome: unknown`；
- 不自动重试；
- 审计记录不确定状态；
- 由具体工具通过业务幂等键解决重复执行问题。

`call_id` 必须原样传给 Harness，参考 Harness 可在本地时间窗内拒绝重复 `call_id`，但跨实例、长期幂等不在本版本保证范围。

### 12.3 取消语义

调用方取消协程时：

- 必须释放 Loop Controller 并发槽位；
- 不得声称远端执行已取消；
- 如协议尚无取消端点，应审计为 `execution_outcome: unknown`；
- 本版本不新增远程取消协议。

---

## 13. 测试计划

### 13.1 配置测试

- tool 引用不存在 backend 时启动失败；
- 重复 backend 名称失败；
- 非 loopback HTTP 生产配置失败；
- TLS cert/key 不成对失败；
- auth 环境变量缺失失败；
- `api_key_env` 向新 auth 模型兼容转换；
- 不支持 Docker 配置在启动期稳定失败；
- backend auth secret ref 进入吊销列表。

### 13.2 并发测试

- 同一 backend 的多个工具共享并发上限；
- 不同 backend 各自独立；
- 获取槽位超时返回 `harness_overloaded`；
- 成功、异常、取消后均释放槽位；
- in-flight 指标准确；
- 不存在超过配置值的真实并发请求。

### 13.3 认证与防重放测试

- 正确 HMAC 通过；
- body、path、timestamp、nonce、key_id 任一被修改均失败；
- 缺少认证失败；
- 错误 key 失败；
- 过期 timestamp 失败；
- nonce 重放失败；
- 签名比较不泄露 Secret；
- 原始 JSON body 签名不会因服务端重新序列化造成歧义。

### 13.4 健康测试

- `startup_required=true` 且不可达时启动失败；
- 非强制 backend 不可达时 Runtime 启动但状态 unhealthy；
- 连续失败达到阈值后状态转换；
- 恢复后转回 healthy；
- unhealthy backend 调用不发送网络请求；
- admin 状态端点不泄露配置 Secret。

### 13.5 风险测试

- `default_risk=critical` 的未知工具名也不会被 R1 降为 low；
- 参数规则判定更高风险时取更高值；
- MCP/HTTP 工具原有分类行为不回归；
- R2 仍能按 Profile/Policy 阻止 Harness 工具。

### 13.6 参考服务测试

- 未认证请求拒绝；
- 不支持协议版本拒绝；
- 未注册工具拒绝；
- 参数 schema 非法拒绝；
- shell allowlist 生效；
- timeout 终止并回收子进程；
- stdout/stderr 合计超过上限时主动终止；
- 不支持的沙箱字段非空时 fail-closed；
- 环境变量只来自服务端允许列表；
- `/health` 不泄露 Secret。

### 13.7 回归测试

必须继续通过：

- MCPExecutor；
- HTTPExecutor；
- Harness Secret 吊销；
- 审批恢复和最终执行前吊销；
- Audit/Evidence/checkpoint；
- HTTP/gRPC Admin；
- v0.26.1 全部测试。

---

## 14. 验收标准

### P0：发布阻断项

- [ ] 远程 HTTP Harness 支持 HMAC 请求签名和防重放；
- [ ] 认证密钥进入 Secret 吊销检查；
- [ ] 每个 backend 的 `max_concurrent_calls` 真正生效；
- [ ] 所有 semaphore 路径无泄漏；
- [ ] backend 引用与认证/TLS 配置在启动期 fail-fast；
- [ ] 远程生产 URL 默认强制 HTTPS；
- [ ] backend 健康状态可查询且不泄露敏感配置；
- [ ] unhealthy 和 overloaded 时 fail-closed；
- [ ] `default_risk` 真实进入统一风险计算；
- [ ] 参考 Harness 验证认证、协议版本和 nonce；
- [ ] 输出超限时主动终止执行；
- [ ] 无法执行的沙箱限制不得静默忽略；
- [ ] 网络超时不自动重试，并标记执行结果不确定；
- [ ] 错误和审计信息完成脱敏；
- [ ] 全量 pytest、ruff、mypy 通过。

### P1：建议完成

- [ ] mTLS 客户端配置；
- [ ] 后台周期健康检查；
- [ ] Harness 专用指标；
- [ ] 只读 admin backend 状态端点；
- [ ] 旧 `api_key_env` 配置兼容和弃用提示。

如果 P1 中 mTLS 未完成，不得在发布文档中宣称“双向 TLS 身份认证已实现”；但 HMAC、防重放、并发控制、启动校验、健康状态和风险接线仍属于 P0，不得延期。

---

## 15. 推荐实施顺序

### 阶段 1：模型和配置校验

1. 扩展 Harness 配置模型；
2. 决定并落实 Docker 配置拒绝/移除策略；
3. 实现 backend/tool 交叉校验；
4. 归一化旧 `api_key_env`；
5. 增加配置负向测试。

### 阶段 2：并发和生命周期

1. 每 backend 建立共享状态对象；
2. 实现 semaphore 和获取超时；
3. 实现 in-flight 计数；
4. 修正 Runtime 显式 start/stop；
5. 增加取消和异常释放测试。

### 阶段 3：认证和协议

1. 固定 protocol version；
2. 实现 canonical body 签名；
3. 客户端发送 timestamp/nonce/signature；
4. 参考服务验证签名和重放；
5. 错误净化和 Secret 吊销接线。

### 阶段 4：健康和可观测性

1. 启动健康检查；
2. 周期探活和状态机；
3. admin 只读状态；
4. metrics；
5. 后端故障测试。

### 阶段 5：风险和参考执行安全

1. `default_risk` 接入现有分类器；
2. 参考 Harness 参数校验；
3. 主动输出限制；
4. 沙箱不支持时 fail-closed；
5. shell 环境最小化。

### 阶段 6：发布收尾

1. 全量回归；
2. 更新版本号；
3. 更新 README、KNOWN_LIMITATIONS、development_log；
4. 核对文档承诺与实际代码；
5. 不把未完成的 Docker/K8s/多租户/KMS 写成已交付能力。

---

## 16. 版本完成后的准确能力描述

v0.27.0 完成后，可以对外描述为：

> Loop Controller 支持将治理后的工具调用安全转发到独立 HTTP Harness，具备启动期配置校验、HMAC 请求认证、防重放、每后端并发控制、健康监测、风险元数据接线、Secret 吊销、稳定错误语义和完整审计。Harness 可由企业部署在容器、Kubernetes、VM 或专用主机中，实现 Shell、SQL、Browser、CLI 和内部脚本等非标准工具的受控执行。

不能描述为：

- Loop Controller 自身提供生产级 Shell/SQL/Browser 沙箱；
- Loop Controller 已提供 Docker/Kubernetes 编排；
- 所有 `allowed_hosts`/`allowed_paths` 都由控制平面强制执行；
- 子进程 Harness 构成生产隔离；
- 多副本 Harness 已具备全局防重放或分布式并发一致性；
- Harness 工具已不可绕过——强制治理仍依赖凭证隔离、网络出口控制和受控 Agent 运行环境。

---

## 17. v0.28+ 候选方向

以下能力后续独立规划，不提前承诺版本顺序：

1. 外部可信证据锚点 / 远程 WORM 证据；
2. KMS/HSM 签名和密钥轮换；
3. 多副本 Store 与分布式吊销；
4. 完整多租户隔离；
5. Harness 配置热更新；
6. 企业级 Harness SDK 或 Kubernetes 部署参考；
7. 统一 HTTP/gRPC Admin RBAC。

每个 minor 版本只选择一条主线，避免再次把控制平面扩张为万能执行运行时。
