# 已知局限（Known Limitations）

> 本文件列出 Loop Controller v0.3.0-iter5 / v0.20.0 **明确声明的能力边界**。每一条都是设计决策的结果，不是缺陷；但使用者必须据此判断当前版本是否适用于自己的场景。**不得在对外材料中声称本版本具备下列未实现的能力。**

---

## v0.20.0 边界声明

### V20-1. 接入形态收敛为三种官方形态

v0.20.0 核心包仅维护三种官方接入形态：

| 形态 | 定位 |
|---|---|
| HTTP 服务 | 生产主推入口 |
| gRPC 服务 | 生产主推入口（内部服务间） |
| MCP Proxy | 兼容入口（外部标准 MCP Client） |

`Python SDK / ToolGovernor` 保留在核心包，但仅作为**内部开发/可信 Agent** 使用；
`Framework Adapters`（LangChain / OpenAI Agents / AutoGen）已移出核心包，仅保留在
`examples/contrib/adapters/` 作为迁移示例。

### V20-2. 仅支持 MCP 工具执行

v0.20.0 只实现 `MCPExecutor`。所有真实工具调用必须通过 MCP Server 包装。
HTTP API、本地函数、Shell、浏览器、数据库直连等工具执行能力尚未实现，
计划通过 `ExecutorRegistry` + `ToolExecutor` 抽象在后续版本扩展。

### V20-3. 同进程 SDK/Adapter 不提供工具级实时阻断

`Python SDK` 和 `Framework Adapter` 与 Agent 同进程运行，Agent 代码理论上可以
绕过治理、直接调用底层库或访问本地资源。因此这些形态**不承诺工具级实时阻断**。
企业级强治理必须使用 HTTP / gRPC / MCP Proxy 等网络边界，并配合凭证/网络隔离。

### V20-4. 身份认证处于基础实现阶段

v0.20.0 已实现 JWT / mTLS / 静态 token 三种身份 Provider：

- **静态 token** 仅用于开发/测试；
- **JWT** 支持 RS256 和 JWKS；
- **mTLS** 支持证书 CN/SAN 映射。

但以下能力尚未实现：
- 身份动态申领/注册；
- 吊销列表（Revocation List）；
- 多租户身份隔离；
- 与外部 IDP 的 OAuth2 / OIDC 集成。

### V20-5. stdio 模式 MCP Proxy 仅用于开发/测试

`stdio` 模式下的 `MCP Proxy` 依赖环境变量 `LOOP_CONTROLLER_IDENTITY_TOKEN`，
Agent 进程可能读取到该 token，因此**不用于生产环境**。
生产环境必须使用 SSE + mTLS。

v0.23.1 已移除 `lc proxy --identity-token` 命令行参数，避免敏感 token 进入 shell history / 进程列表；token 仅能从环境变量读取。

### V20-6. 配置不支持热更新

`identity.yaml`、`entrypoints.yaml` 等配置在进程启动时加载，运行期修改需重启。

---

## v0.23.0 边界声明

### V23-1. 本地函数沙箱为粗粒度子进程隔离

v0.23.0 已实现 `LocalFunctionExecutor`，通过子进程 + stdin/stdout JSON 调用本地 Python 函数，并提供超时、`open()` 路径白名单、环境变量白名单等最小沙箱能力。
v0.23.1 修复后，子进程不再继承完整环境变量，仅保留系统必要变量 + 白名单变量 + `PYTHONPATH`，并额外 hook 了 `os.open`。
但该隔离仍是**粗粒度**的：

- 子进程与主进程共享文件系统、网络、CPU/内存等底层资源；
- 路径白名单基于字符串前缀匹配，复杂路径或编码绕过仍可能生效；
- 未做 CPU/内存/cgroup、seccomp、chroot、容器级隔离。

高危函数应部署到容器/VM，由部署层提供真正隔离。

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

### V22-2. Secret 文件后端默认明文存储

v0.22.0 `FileSecretBackend` 默认以明文 JSON 存储 secret，依赖文件系统权限与进程隔离保护；
真正加密/KMS/Vault 集成计划在 v0.24+。生产部署应确保 secret 目录的 ACL 严格受限。

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
- **生产路径**：定期写 seal 记录 + WORM 存储 + 签名日志（post-MVP）。

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

## 环境备注

- **Windows 开发机**：关闭 MCP stdio 子进程时会打 anyio cancel-scope 的 WARNING 日志（mcp SDK 2.x 已知行为），不影响主链路；CI（Linux）下不应出现，出现即说明容错逻辑误吞了正常路径。
- **CI 的 e2e 测试**：`tests/test_e2e_research_agent.py` 仍使用 FakeGateway 以快速回归；`tests/test_e2e_real_mcp.py` 使用 `build_runtime()` + 真实 `MCPGateway` + 本地 `email_mock` server，作为发布前真实组件 gate。
