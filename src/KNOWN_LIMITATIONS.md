# 已知局限（Known Limitations）

> 本文件列出 Loop Controller v0.33.0 **明确声明的能力边界**。每一条都是设计决策的结果，不是缺陷；但使用者必须据此判断当前版本是否适用于自己的场景。**不得在对外材料中声称本版本具备下列未实现的能力。**

---

## v0.20.0 边界声明

### V20-1. 接入形态收敛为三种官方形态（v0.32.0 刷新）

v0.32.0 核心包仅维护三种官方接入形态，所有其他方式已移除或降级为示例；v0.33.0 对这三条接入线做了健壮性加固：

| 形态 | 定位 | 说明 |
|---|---|---|
| `@governed` + `GovernanceRuntime` | **主路线**：Python Agent 主动接入 | 单 Agent 工具调用、审批、审计、错误恢复的首选路径；支持同步/异步函数与统一工具注册表 Hook。v0.33.0 将 `GovernanceRuntime` 上下文改为 `ContextVar`，内部引用改为 `PrivateAttr`，注册表替换改为两阶段原子操作。 |
| MCP Proxy | 强制约束/网关入口 | 外部不可控 Agent（Cursor、Claude Desktop 等）通过标准 MCP Client 接入，必须走网关。v0.33.0 增加 SSE 并发上限、请求体/限流、admin profile 白名单、错误脱敏与 mTLS fallback 加固。 |
| HTTP REST API | 跨语言调用入口 | 为 Go / Node / Java 等外部 Agent 或管理系统提供稳定 REST 调用面。v0.33.0 增加请求体大小限制、可配置限流、CORS 来源校验、全局异常处理、API Key 安全比较与 Query 参数校验。 |

已移除的接入方式：

- **FastAPI 集成**：`src/loop_controller/integrations/fastapi.py` 已删除；Loop Controller 的 HTTP 面由自有 Starlette/HTTP REST 服务提供，不再依赖框架级适配。
- **gRPC 服务**：`src/loop_controller/grpc_server.py`、`grpc_client.py` 已删除；CLI `grpc-server` 子命令已移除。后续 Agent 间横向交互治理由独立 Go 内核通过 A2A 协议负责，不再在 Python 工具治理层暴露 gRPC。
- **LangChain 集成模块**：`src/loop_controller/integrations/langchain.py` 已删除，降级为 `examples/integrations/langchain_example.py` 示例应用，展示如何用 `@governed` 包装 LangChain 工具。

`Python SDK / ToolGovernor` 是主路线，不再只是“内部开发/可信 Agent”用途；
`Framework Adapters`（LangChain / OpenAI Agents / AutoGen）全部移出核心包，仅保留在
`examples/integrations/` 作为接入示例。

### V20-2. 仅 MCP / HTTP 协议型工具由 Loop Controller 内部代理执行

v0.20.0 起，Loop Controller 内部只保留两类执行器：

- `MCPExecutor`：把调用转发给外部 MCP Server；
- `HTTPExecutor`：把调用转发给 REST API。

Shell、SQL、浏览器、文件系统、本地函数等操作系统级或应用级能力，
**不由 Loop Controller 进程自己执行**。企业应通过以下方式接入治理：

1. **包装成 MCP Server**：见 `examples/contrib/mcp_wrappers/` 下的
   `shell_mcp_server.py`、`sql_mcp_server.py`、`browser_mcp_server.py`；
2. **接入 Harness**：Loop Controller 做治理决策， HarnessExecutor 将调用转发给外部 Harness 进程/容器执行，见 `examples/contrib/harness/`。

v0.27.0 将远程 HTTP Harness 收敛为生产主路径，支持认证、防重放、进程内并发门控、健康检查和 TLS/mTLS 客户端配置。子进程仍仅用于开发/测试；Docker 配置在加载期被拒绝，Loop Controller 不直接调用 Docker daemon。

`LocalFunctionExecutor`（v0.23.0）保留，但仅定位为“不方便包装成 MCP 时的可选辅助”，
不是核心架构方向。

### V20-3. 同进程 SDK/Adapter 的阻断是合作式而非强制式

`@governed` 与 `Framework Adapter` 与 Agent 同进程运行，Agent 代码理论上可以
绕过装饰器直接调用底层库或访问本地资源。因此这些形态提供的是**合作式治理**——
只要 Agent 调用被装饰的工具，Loop Controller 就能完整执行 R1/R2/R3 链路、审批等待与错误恢复；
但它不承诺对恶意/错误代码的进程级强制隔离。

企业级强治理必须对不可控 Agent 使用 **MCP Proxy** 或 **HTTP REST API** 等网络边界，
并配合凭证/网络隔离。

### V20-4. 身份认证处于基础实现阶段

v0.20.0 已实现 JWT / mTLS / 静态 token 三种身份 Provider：

- **静态 token** 仅用于开发/测试；
- **JWT** 支持 RS256 和 JWKS；
- **mTLS** 支持证书 CN/SAN 映射。

但以下能力尚未实现：
- 身份动态申领/注册；
- 多租户身份隔离；
- 与外部 IDP 的 OAuth2 / OIDC 集成。

v0.26.0 已提供全局吊销列表与 Kill Switch；其多进程一致性边界见 V26-1。

### V20-5. stdio 模式 MCP Proxy 仅用于开发/测试

`stdio` 模式下的 `MCP Proxy` 依赖环境变量 `LOOP_CONTROLLER_IDENTITY_TOKEN`，
Agent 进程可能读取到该 token，因此**不用于生产环境**。
生产环境必须使用 SSE + mTLS。

v0.23.1 已移除 `lc proxy --identity-token` 命令行参数，避免敏感 token 进入 shell history / 进程列表；token 仅能从环境变量读取。

### V20-6. 配置不支持热更新

`identity.yaml`、`entrypoints.yaml` 等配置在进程启动时加载，运行期修改需重启。

---

## v0.23.0 边界声明

### V23-1. 本地函数沙箱为粗粒度可选辅助

v0.23.0 已实现 `LocalFunctionExecutor`，通过子进程 + stdin/stdout JSON 调用本地 Python 函数，并提供超时、`open()` 路径白名单、环境变量白名单等最小沙箱能力。
v0.23.1 修复后，子进程不再继承完整环境变量，仅保留系统必要变量 + 白名单变量 + `PYTHONPATH`，并额外 hook 了 `os.open`。

v0.24.0 按架构审计收敛后，`LocalFunctionExecutor` 被重新定位为
**“不方便包装成 MCP 时的可选辅助”**，不是核心执行器扩展方向。
Loop Controller 未来不会把 Shell / SQL / Browser 等高危能力做成内置执行器。

该隔离仍是**粗粒度**的：

- 子进程与主进程共享文件系统、网络、CPU/内存等底层资源；
- 路径白名单基于字符串前缀匹配，复杂路径或编码绕过仍可能生效；
- 未做 CPU/内存/cgroup、seccomp、chroot、容器级隔离。

高危函数应部署到容器/VM 或作为独立 MCP Server，由部署层提供真正隔离。

### V23-2. 本地函数参数与返回结果须 JSON 可序列化

`LocalFunctionExecutor` 通过 JSON 在父子进程间传递参数与结果，因此函数入参和返回值必须能被 `json.dumps` / `json.loads` 处理；Python 对象、二进制数据、datetime、自定义类型不会被特殊处理。

### V23-3. 本地函数配置不支持运行期热更新

v0.23.0 的 `config/local_functions.yaml` 在进程启动时一次性加载。函数实现文件变更后，新的子进程 runner 会重新导入最新代码，但工具注册（`tool_name → LocalFunctionSpec`）的增删改需要重启主进程；v0.24+ 可考虑统一纳入 `HotReloader`。

### V23-4. Windows 子进程需要继承系统环境变量

`LocalFunctionExecutor` 在 Windows 上启动子进程时，必须保留 `PATH` / `SYSTEMROOT` / `APPDATA` / `USERPROFILE` 等系统环境变量，Python 才能正确加载用户 site-packages；v0.23.1 的实现在所有场景下仅保留这组系统必要变量 + 白名单变量 + `PYTHONPATH`。

### V23-5. mcp 依赖暂时固定在 1.x

v0.23.1 将 `mcp` 依赖固定为 `<2.0`，因为 mcp 2.0 对服务端 API 做了破坏性变更，现有 `mcp_servers/sqlite_server.py` 与 mocks 需要较大改造；未来升级时会同步迁移到 `MCPServer` API。`email_server.py` 已做兼容性预处理。

---

## v0.22.0 边界声明

### V22-1. 仅 HTTP 工具与 Secret 支持热更新

v0.22.0 的热更新仅覆盖 `config/http_tools.yaml` 与 `secrets/` 下的 secret 文件。
`config/mcp_servers.yaml`、`config/profiles.yaml`、`config/policies/*.rego` 等变更建议重启进程，避免运行时权限漂移或子进程生命周期混乱。

### V22-2. Secret 文件后端：明文为默认，加密后端已提供

v0.22.0 `FileSecretBackend` 默认以明文 JSON 存储 secret，依赖文件系统权限与进程隔离保护。

v0.24.0 新增 `EncryptedFileSecretBackend`，使用 AES-256-GCM 加密落盘，
密钥从环境变量 `LC_SECRET_ENCRYPTION_KEY` 读取（32 字节，hex/base64）。
可在 `config/secrets.yaml` 中设置 `backend.type=encrypted_file` 启用。

生产部署应优先使用加密后端，并确保 secret 目录的 ACL 严格受限。
KMS / Vault / etcd 等外部密钥/存储集成仍计划在后续版本。

### V22-3. 多租户仅为命名空间预留

v0.22.0 在 `Agent` / `Task` / `ExecutionContext` / `SecretRef` 中预留 `tenant_id`，
`SecretBroker` 按 tenant 命名空间隔离 secret，但尚未实现完整的多租户权限隔离、数据隔离、计费拆分或跨租户访问控制。

### V22-4. Secret Broker 权限检查在 Windows 上跳过 POSIX 位

`FileSecretBackend` 的 world-readable 拒绝逻辑基于 Unix `st_mode`；Windows 上该检查不生效，需通过 NTFS ACL 与运行账户权限控制。

---

## 安全相关局限

### L1. 审计哈希链对"最后一行"的删改需依赖 seal 记录

哈希链中，第 N 行的完整性由第 N+1 行的 `prev_hash` 承诺。文件末尾的最后一行没有后继，删除或篡改它无法被 `verify_chain()` 直接检出；但若此前写过 seal 记录，则删除 seal 之后的事件会破坏 seal 的 `chain_hash` 校验，删除 seal 之前的事件会破坏 seal 的 `prev_hash` 链接。

- **当前缓解**：`JsonlAuditStore.seal()` 可手动或周期性调用；启用 HMAC-SHA256 时 seal 记录还受 `seal_key` 域分离签名保护；审计事件与 seal 记录均带 `key_id` 用于密钥轮换识别；
- **当前缓解**：v0.26.0 可选启用本地签名证据链，逐条签名并绑定前序哈希；原审计链仍建议定期写 seal；
- **生产路径**：本地证据文件仍需配合 WORM/对象锁定与独立备份。

### L2. 若显式回退 `sha256` 模式，低熵参数可被字典攻击

审计日志默认 `hash_algo=hmac-sha256`（由 `ConfigLoader` 自动配置）。若部署方显式设置 `LOOP_CONTROLLER_AUDIT_HASH_ALGO=sha256` 或直接用 `JsonlAuditStore(..., hash_algo="sha256")` 回退，则 `args_hash = SHA-256(canonical_json(arguments))`，对已知邮箱、常见文件名等低熵参数，攻击者可彩虹表反推。

- **当前缓解**：`ConfigLoader.load()` 默认 `hmac-sha256`；`JsonlAuditStore` 默认仍保留 `sha256` 以便验证旧文件；HMAC key 从环境变量 `LOOP_CONTROLLER_AUDIT_HMAC_KEY` 读取、event key 与 seal key 做域分离；
- **升级触发条件**：任何涉及真实 PII 的部署必须使用 HMAC-SHA256（即保持默认）。

### L3. 防重放与会话风险状态依赖单进程 asyncio 假设

`DecisionStore` 的"检查 + 记账"原子性与 `RiskStateStore` 的 JSONL 追加均建立在**单进程、单事件循环、无并行写入** 的运行时假设上。

- **当前缓解**：假设已在架构文档 §6.6 / v1.2 §3.2 显式声明；
- **生产路径**：多 worker / 多进程部署前，必须将 DecisionStore 与 RiskStateStore 一起升级为同一套原子语义（SQLite `INSERT OR FAIL` 或分布式锁）。**违反此前提的部署不在安全承诺范围内。**

### L4. 策略明文存储

Rego 策略文件为明文，仅依赖文件系统权限保护。恶意 Agent 若能读取策略文件，可探测完整控制逻辑。

- **当前缓解**：建议 `chmod 600 policies/`；
- **生产路径**：加密存储 + HSM/TEE/密钥代理受信解密（post-MVP）。

### L5. token 预算是估算值，非真实计量

预算按 `mcp_servers.yaml` 中各工具的 `cost_per_call` 固定估算值计费，不含工具返回内容进入 LLM context 的实际消耗。

- **当前缓解**：调用次数上限（`max_calls_per_task`）独立生效，提供硬兜底；
- **生产路径**：LLM usage 上报 + 工具结果长度折算（post-MVP）。

---

## 功能边界（设计内缺失，非缺陷）

| # | 边界 | 说明 |
|---|---|---|
| F1 | ~~审批为配置打桩~~ 已实现异步审批 CLI | v0.3.0 Iteration 5 用 `AsyncApprovalManager` + `JsonlApprovalStore` + `lc approvals list/approve/deny` 替换 `ConfigR0Delegate`；审批人通过 CLI 写入结果，任务 `resume_task` 后继续 |
| F2 | 无 Agent 间交互治理 | 只治理 `tool_call`；多 Agent 委托、inter_agent 均未实现 |
| F3 | 无 Earned Authority | 权限固定，无任务后临时提权；`fixed_ceiling` 保留为空 |
| F4 | ~~LLMPlanner 未实现~~ 已实现（T3.5） | 默认仍关闭（`config/llm_planner.yaml`），开启后由 LLM 动态规划；密钥仅来自环境变量，失败不重试 |
| F5 | 权限组合规则为静态 YAML | 无图分析/能力代数；规则需人工维护 |
| F6 | 审计全量记录无采样 | 高负载场景需自行评估日志量 |
| F7 | 财务支付预算未启用 | `payment_amount` 恒为 0 |
| F8 | ~~多轮对话上下文未进入 R2~~ 已实现 | v0.3.0 Iteration 4 通过 `ConversationContext` + `build_governance_context` 让当前 Task 的最近用户/Agent 消息进入 R2；跨 Task 同 session 消息暂未混入 |
| F9 | ~~外部 Agent 直接接入尚不支持~~ 已实现 MCP Proxy | v0.5.0 起 `LoopControllerProxyServer` 通过 stdio/SSE 把 Loop Controller 暴露为 MCP Server；v0.5.1 完成 `require_approval` 结构化响应与审批后重试 |
| F10 | ~~SSE/HTTP MCP transport 未支持~~ 已实现 | v0.5.0 起同时支持 stdio 与 SSE transport |
| F11 | ~~MCP Proxy 审批重试依赖 Proxy 进程存活~~ 已解决 | v0.6.0 引入 `JsonlTaskStore`，`Runtime.get_task()` 可从持久化存储恢复 Task，新 Runtime 读取同一数据目录即可完成审批后重试 |
| F12 | ~~HTTP / 本地函数 / 浏览器 / Shell 执行器未实现~~ HTTP 与本地函数已部分实现 | v0.21.0 实现 `HTTPExecutor`；v0.23.0 实现 `LocalFunctionExecutor`；浏览器 / Shell 仍待后续 |
| F13 | Framework Adapters 已移出核心包 | 仅作为 `examples/contrib/adapters/` 示例保留 |

完整演进计划见方案文档 §9.3 post-MVP 路线图。

---

## v0.21.0 边界声明

### V21-1. HTTP Executor 仅支持 REST API 文本/JSON

v0.21.0 的 `HTTPExecutor` 直接调用 REST API，但仅处理 text/json 响应；二进制、文件流、GraphQL、WebSocket 不在范围内。

### V21-2. 凭证通过环境变量引用

HTTP 工具的 API Key / Secret 通过 `${ENV_NAME}` 引用解析，配置文件本身不存真实 secret。v0.22 将引入 Secret Broker 支持热更新与更细粒度访问控制。

### V21-3. DNS 反解析 SSRF 防护默认关闭

`HTTPSecurityPolicy.require_dns_resolution` 默认 `false`；开启后会把域名解析到 IP 再检查是否在私有段。生产环境推荐开启，但可能增加延迟。

### V21-4. 响应体与超时大小限制为硬编码默认值

默认响应体上限 5 MB、超时 30 秒。v0.22 将支持按工具配置。

### V21-5. HTTP 工具风险等级默认提升一级

`RuleBasedClassifier` 仍按工具名/参数判断；HTTP 工具在 `LoopController._evaluate_proposal()` 中统一提升一级风险。未来会把风险计算下沉到工具元数据。

---

## v0.25.0 边界声明

### V25-1. Harness 是独立执行平面，默认不启用

v0.25.0 实现 `HarnessExecutor`，通过 `config/harness_tools.yaml` 将工具调用转发给外部 Harness 进程/容器执行。Loop Controller 仍然是治理控制平面，不直接执行 Shell / SQL / Browser 等副作用。

`config/harness_tools.yaml` 默认全部注释，启动时不会自动拉起 Harness 后端；用户显式启用后才生效，避免无 Harness 环境启动失败。

### V25-2. 子进程 Harness 仅用于开发/测试

`SubprocessBackendConfig` 允许 Loop Controller 启动本地子进程作为 Harness。该模式与 Loop Controller 主进程共享文件系统、网络、CPU/内存 等底层资源，**不用于生产环境**。

生产环境应由部署层在容器、Kubernetes、VM 或专用主机中运行独立 HTTPS HTTP Harness Service，并由部署层提供真正隔离。

### V25-3. Loop Controller 不提供 Docker/Kubernetes 编排

`DockerBackendConfig` 类型和 `examples/contrib/harness/docker_backend.py` 示例仍在代码库中，但 v0.27.0 配置加载器会在启动期拒绝 `type: docker`，不会把它注册到 `HarnessExecutor`。生产容器或 Kubernetes 工作负载必须由部署层启动为独立 HTTP Harness Service。

### V25-4. Harness 工具风险等级由配置提供下限

Harness 工具的 `default_risk` 默认为 `critical`；v0.27.0 将它接入统一 `RuleBasedClassifier`，R1 结果取规则风险与工具默认风险的较高者。它不是最终授权：R2 的 Profile、OPA/Rego、审批和组合规则仍保留最终裁决权。

### V25-5. Harness 后端不支持运行期热更新

`config/harness_tools.yaml` 在进程启动时一次性加载；后端配置、工具注册、沙箱参数的增删改需要重启主进程。未来可考虑统一纳入 `HotReloader`，但当前版本未实现。

---

## v0.26.0 边界声明

### V26-1. 吊销状态按单进程部署设计

`config/revocation.yaml` 支持轮询热更新，HTTP/gRPC 管理操作也会同步写回文件；但多个进程或节点之间没有分布式锁、版本仲裁或一致性广播。生产部署多副本前需接入共享的强一致存储与通知机制。

### V26-2. 签名证据链与尾状态 checkpoint 仅提供本地文件后端

v0.26.1 支持 HMAC-SHA256 与 Ed25519，并通过审计—证据交叉校验和签名本地 checkpoint 检测记录修改、插入、断链、单边丢失与相对尾部回退。但拥有主机权限的攻击者仍可同时删除审计、证据和 checkpoint；该场景无法与新部署区分。远程对象存储、WORM/对象锁定、外部可信锚点、KMS/HSM 和密钥轮换尚未实现，生产环境需自行提供独立备份和文件权限保护。

### V26-3. 证据链失败不阻断主审计写入

签名或证据后端写入失败时，原 JSONL 审计记录仍会保留，并产生 critical 告警。这避免证据后端故障导致审计事件丢失，但该事件不会自动补写到证据链；审计—证据一致性验证会将状态标记为 `degraded`，需要运维监控并处理告警。

### V26-4. 多租户字段不等于完整隔离

吊销与证据模型保留 `tenant_id`，本地证据按租户分文件保存，但当前版本不提供完整的租户鉴权、资源隔离和跨租户访问控制。

## v0.26.1 边界声明

### V261-1. 本地异步有序写入仍是单进程语义

`append_async()` 避免阻塞事件循环，并在单个 `JsonlAuditStore` 实例内保证序号和哈希链有序；它不提供多进程、多 worker 或多节点对同一 JSONL 文件的安全并发。违反该部署前提不在完整性承诺范围内。

### V261-2. gRPC 管理授权是简单 allowlist，不是完整 RBAC

`entrypoints.grpc.admin_agent_ids` 只按已认证身份的精确 `agent_id` 授权 Admin RPC；省略或空列表时默认全部拒绝。本版本不提供角色继承、细粒度管理权限或多租户管理员域。HTTP Admin API 继续使用现有 API key，两套认证体系尚未统一。

### V261-3. 吊销时间必须显式携带时区

`revoked_at` 与 `expires_at` 的 HTTP、gRPC、YAML 输入必须是带时区的 ISO 8601 时间；合法值会转为 UTC，无时区值会被拒绝。吊销热更新校验失败时保留旧内存快照，不会用无效配置覆盖现有保护。

### V261-4. 证据验证失败采用降级而非拒绝启动

启动时审计—证据—checkpoint 校验失败会产生告警，并通过健康检查暴露 `evidence_status=degraded`；为保留主审计可用性，服务仍会启动。对证据完整性要求必须 fail-stop 的部署，需要在外部编排或健康检查 gate 中拒绝接流量。

## v0.27.0 边界声明

### V27-1. 生产保证止于独立 HTTP Harness 的控制与协议边界

Loop Controller 可对远程 HTTP Harness 做启动校验、HMAC/API Key 认证、TLS/可选 mTLS 客户端配置、每后端并发门控、健康检查、Secret 吊销和风险下限接线，但不执行真实工具，也不提供文件系统、网络、进程、CPU 或内存隔离。参考 Harness 只用于演示协议、认证、参数校验、超时、输出上限和 fail-closed 语义，不是生产沙箱。

### V27-2. 防重放、并发门控和健康状态是单实例语义

nonce 缓存位于参考 Harness 进程内；`asyncio.Semaphore` 与健康状态位于 Loop Controller 进程内。多副本部署没有共享 nonce store、分布式并发配额或状态一致性。生产多副本必须自行接入共享 nonce/配额/状态设施，或接受每实例独立的边界。

### V27-3. 超时和取消不等于远端执行已取消

HTTP 请求发送后超时或连接中断时，远端工具可能已经执行；本版本返回不确定结果且不自动重试。调用方取消本地协程只释放本地并发槽位，不会取消远端动作。本版本没有远程取消端点，也不提供跨实例或长期 `call_id` 幂等保证。

### V27-4. 沙箱字段不是控制平面的隔离保证

参考 Harness 对非空 `allowed_hosts`/`allowed_paths` 返回 `harness_sandbox_unsupported`，不会静默忽略；`env_whitelist` 只允许服务端预配置环境变量。真正的网络和文件系统策略仍须由部署环境强制执行。参考 shell 工具只是预注册命令 allowlist 示例，不应直接作为生产运维执行器。

### V27-5. 运维状态端点沿用现有 HTTP Admin 鉴权边界

`GET /v1/admin/harness/backends` 复用现有 HTTP Admin API key，返回净化后的类型、健康状态、检查时间、失败次数、错误码、in-flight 与并发上限；Harness 调用、耗时、排队、in-flight、过载和健康指标已接入 Prometheus。当前没有对应 gRPC Admin RPC，也没有统一的 HTTP/gRPC Admin RBAC。

### V27-6. 配置和密钥不支持运行期轮换

`harness_tools.yaml` 仍只在启动时加载；认证环境变量、TLS 文件、工具注册和 backend 参数变更后需要重启。旧 `api_key_env` 仅为兼容入口，已弃用且不能与新 `auth` 同时配置。

## v0.28.0 边界声明

### V28-1. 可信锚点是外部独立服务

Loop Controller 的 `HTTPAnchorBackend` 把本地审计/证据链的当前状态发布到外部可信锚点服务，并校验返回的 Ed25519 receipt。它只能验证 receipt 签名和链一致性，不能验证锚点服务进程、存储或管理员操作的可信性。锚点服务必须作为独立高可用系统部署，并自行保护其签名私钥与数据库。

### V28-2. 锚点不提供跨实例分布式一致性

锚点流 ID 与幂等键用于检测重复发布、同序号分叉和回滚，但 Loop Controller 与锚点服务之间仍是 HTTP 请求-响应。多 Loop Controller 实例同时写入同一 stream 时，依赖锚点服务端的原子 CAS 来裁决；Loop Controller 本地不做分布式锁或 leader 选举。

### V28-3. 不确定结果通过 `latest` 消解，不自动重试

PUT 超时、连接中断等不确定结果会通过 `GET /v1/anchors/{stream_id}/latest` 消解。若 latest 与本次发布一致则视为成功；若远端已被其他实例推进到更高序号，则报告冲突或回滚。本版本不会自动重试发布，也不保证远端动作已被执行或取消。

### V28-4. bootstrap 是破坏性管理操作

`POST /v1/admin/evidence/anchor/bootstrap` 会显式覆盖锚点服务当前流状态，仅应在灾难恢复或首次初始化时由管理员执行。误用可能导致远程锚点与本地状态不一致，且会写入 `anchor_bootstrap` 审计事件。

### V28-5. 本地证据文件仍是单进程追加写

即使接入了外部锚点，本地 `audit.jsonl` 与 evidence JSONL 仍然是单进程追加写文件。多 writer 并发访问同一文件不在本版本保证范围内；生产部署应保证每个 Loop Controller 实例使用独立的本地路径，或通过外部共享存储按部署约定协调。

### V28-6. 仅支持 Ed25519 receipt 验证

当前 receipt 签名算法固定为 Ed25519，不支持多签、threshold 签名或密钥轮换后的历史 receipt 兼容。服务公钥在启动时通过配置文件加载，运行期不支持轮换。

## 环境备注

- **Windows 开发机**：关闭 MCP stdio 子进程时会打 anyio cancel-scope 的 WARNING 日志（mcp SDK 2.x 已知行为），不影响主链路；CI（Linux）下不应出现，出现即说明容错逻辑误吞了正常路径。
- **CI 的 e2e 测试**：`tests/test_e2e_research_agent.py` 仍使用 FakeGateway 以快速回归；`tests/test_e2e_real_mcp.py` 使用 `build_runtime()` + 真实 `MCPGateway` + 本地 `email_mock` server，作为发布前真实组件 gate。

---

## v0.29.0 边界声明

### V29-1. 审批可见性仍为轮询/重试语义

v0.29.0 修复了跨进程审批结果不可见的问题：Runtime 在每次查询审批状态时通过 `JsonlApprovalStore.refresh()` 增量读取文件。但 Loop Controller **不主动向 Agent 推送**审批结果；Agent 必须继续按既有语义轮询 `resume_after_approval` 或等待 `wait_for_approval` 超时重试。服务内 `ApprovalWatcher.notify()` 仅用于唤醒同一进程内的等待者。

### V29-2. 文件锁不覆盖多 worker 强一致并发

`JsonlApprovalStore._append` 使用 `portalocker` 对追加写加跨进程锁，可防止 CLI 与 Runtime 双进程写冲突。但锁粒度仅保护单条记录追加，不覆盖读-改-写、重放或跨多台机器的并发；多 worker 共享同一 `approvals.jsonl` 仍可能产生竞态。强一致多 writer 需使用外部数据库或分布式锁，归入后续版本。

### V29-3. 预算清扫不自动修复所有孤儿预留

`recover_stale_reservations()` 仅清理状态为 `pending` / `pending_approval` 且 `expires_at` 已过的预留。对于崩溃窗口中产生的其他孤儿 `reserve`（无对应 `commit/refund` 记录），启动期仅产生告警、不自动补账，需人工对账处理。

### V29-4. `DecisionAlreadyConsumed` 仅在同一 Loop Controller 实例内生效

`DecisionAlreadyConsumed` 由内存 `_finalized_decisions` 集合与 `JsonlDecisionStore` 的 `finalized` 记录共同保护。进程重启后 `finalized` 记录会被重放恢复，因此跨重启仍然有效；但在多 worker 场景下，单个实例的 finalized 集合同步仍依赖底层持久化存储，不额外提供分布式协调。

## v0.33.0 边界声明

### V33-1. 网关层安全加固为单进程内存实现

v0.33.0 为 HTTP REST API 与 MCP Proxy 增加了请求体大小限制、可配置限流、全局异常处理、错误响应脱敏、API Key 安全比较、admin 工具 profile 白名单、SSE 并发上限、mTLS fallback 加固与 CORS 来源校验。但这些机制当前均为单进程、内存实现：

- 限流计数器按进程独立，多副本部署时没有分布式共享配额；
- 请求体大小限制在应用层读取前生效，但仍依赖底层 HTTP server 的连接管理；
- SSE 并发上限为单进程 `asyncio.Semaphore`。

生产多副本部署需在负载均衡或 API Gateway 层补充全局限流与连接控制。

### V33-2. mTLS 身份提取依赖部署层 TLS termination

v0.33.0 的 `_resolve_sse_identity` 已禁止在 `client_ca_cert` 已配置但未成功提取 mTLS 身份时 fallback 到默认身份，从而触发 401。但该机制能获取的对端证书信息受 ASGI server 与部署层 TLS termination 实现限制；如果反向代理未正确转发客户端证书（且未配置 `trust_proxy_headers`），身份提取将失败。生产部署需确保 TLS termination 发生在 Loop Controller 进程内，或仅信任已验证的代理转发。

### V33-3. admin 工具白名单默认关闭

`entrypoints.admin.agent_profiles` 默认为空列表，MCP Proxy 中的 kill_switch、revoke 等 admin 工具默认全部拒绝。启用后，仅当调用 agent 的 `profile_id` 在白名单中时才允许执行。HTTP Admin API 仍使用 API Key 鉴权，两套 admin 权限模型尚未统一为单一 RBAC 体系。

### V33-4. 错误响应脱敏不覆盖 DEBUG 模式

v0.33.0 的所有网络入口错误响应统一返回固定错误码与文案，不携带原始异常信息。该行为在生产配置下生效；若显式开启调试模式或降低日志级别，内部堆栈仍可能进入日志文件。生产环境应确保日志与审计文件 ACL 严格受限。

### V33-5. OPA fixture 端口探测依赖 psutil

v0.33.0 通过 OPA 绑定 `127.0.0.1:0` 并由子进程网络连接表读取实际端口，消除了端口抢占 TOCTOU。该机制依赖 `psutil`；CI 与 dev 依赖已加入 `psutil>=7.0`。若运行集成测试的环境未安装 psutil，fixture 会回退到重试逻辑，但仍可能受端口抢占影响。
