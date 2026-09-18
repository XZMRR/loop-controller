# Loop Controller 最新 develop：Agent 驱动治理与 HTTP/gRPC 服务化分析

## 1. 分析基线

- 仓库：`https://github.com/XZMRR/loop-controller.git`
- 分支：`develop`
- 提交：`8c4d6ff03f2e6874ebcd82a22c72c6bf8a4e0565`
- 提交说明：`feat(v0.13.0-v0.19.0): agent-driven governance, adapters, HTTP/gRPC service boundary`
- 包版本：`0.19.0`
- 独立分析目录：`D:\Agent\loop-controller-latest-develop`
- 分析日期：2026-08-24

本报告依据该提交的实际源码，不把 README、开发日志中的声明自动视为已经成立的能力。本次未运行全量测试，只进行了源码核查、Git 洁净度检查和编辑器诊断检查。

## 2. 执行结论

最新版本已经完成了一次实质性架构重构：核心包不再拥有 Planner 或 Agent 主循环，而由外部 Agent 自己规划和选择工具，并在每次工具调用时主动进入 Loop Controller。项目新增了统一 `LoopController`、通用 Python `ToolGovernor`、LangChain/OpenAI Agents/AutoGen 适配器，以及独立 HTTP 和 gRPC/protobuf 边界。

因此，从软件职责和调用方向看，可以成立地描述为：

> Loop Controller 已从“由项目内部框架驱动 Agent 的 Python Runtime”重构为“由外部 Agent 驱动的工具调用治理控制平面”，并具备 Python、HTTP、gRPC 和 MCP 多种接入方式。

但不能进一步无条件表述为“已完成生产级、不可绕过、可信身份、多实例安全的独立治理服务”。当前仍存在：

1. gRPC CLI 没有启动 `Controller/Runtime/MCPGateway` 生命周期，真实 allow 执行链可能失败；
2. HTTP 的 API key 可选，gRPC 无认证和 TLS，主体身份主要由客户端自报；
3. 审批恢复和审批状态查询缺少 Agent/User/Task 对象级授权；
4. JSONL 状态依赖单进程、单事件循环，不能安全多 worker 或水平扩展；
5. Agent 若可直连真实 MCP Server 或持有真实工具凭证，仍可绕过治理；
6. MCP Proxy 仍直接调用 Runtime/Checkpoint，没有完全收敛到统一 Controller；
7. Proxy 路径没有复用 Controller 的完整审计埋点。

最准确的阶段定性是：

> **Agent 驱动治理架构已经成立，HTTP/gRPC 服务边界已经建立；生产级可信身份、强制出口、事务状态和协议一致性仍需补齐。**

## 3. 重构前后对比

### 3.1 旧模式

```text
Loop Controller Runtime
→ 内部 Planner / LLMPlanner
→ 生成 ActionProposal
→ Checkpoint
→ MCPGateway
→ 真实工具
```

在此模式中，项目既管理 Agent 主循环，又执行治理。治理能力与内部 Python Runtime、Planner 和演示 Agent 绑定，外部框架接入不是核心编程模型。

### 3.2 最新模式

```text
任意 Agent / Harness
→ Python ToolGovernor / Framework Adapter / HTTP / gRPC / MCP Proxy
→ LoopController 或 Checkpoint
→ R1 分类
→ R2 策略、权限、预算、风险、审批、Decision
→ MCPGateway
→ 真实 MCP Server
→ R3 审计
```

核心变化不是简单增加 Web API，而是发生了职责倒置：

- Agent 负责规划、主循环和选择工具；
- Loop Controller 不再替 Agent 思考；
- Agent 每次调用工具时申报 `agent_id`、`user_id`、工具、参数和上下文；
- 治理层返回 allow、deny、require_approval、blocked 或 error；
- 对 allow/modify，真实工具由治理层内部经 Checkpoint 转发执行，而不是只给 Agent 一个建议性 verdict。

这比“只返回 allow/deny、Agent 自觉遵守”的 PDP 模式更强，因为 Controller 路径保留了执行代理能力。但其强制性仍取决于真实凭证和网络出口是否只掌握在治理服务手中。

## 4. v0.13.0–v0.19.0 演进

### v0.13.0：建立 Agent 驱动入口

新增 `LoopController`：

- `evaluate()`：只判定；
- `evaluate_and_execute()`：判定并执行；
- `resume_after_approval()`：审批后恢复；
- `build_controller()`：从配置构建控制器。

同时加入 LangChain/LangGraph 适配器。Agent 开始拥有主循环，治理层按工具调用介入。

### v0.13.1–v0.14.0：从核心彻底移除 Planner

- `Runtime` 移除 Planner 字段；
- Planner/LLMPlanner 先迁到示例兼容层，随后从核心包删除；
- 测试改为直接驱动 `LoopController`；
- 增加两段式 evaluate + execute；
- 补齐审批恢复审计。

这是判断架构重构真实成立的关键证据：不是给旧 Planner Runtime 套一层 API，而是把 Agent 编排职责移出了治理核心。

### v0.15.0–v0.16.0：框架适配与通用 SDK

新增：

- OpenAI Agents SDK Adapter；
- AutoGen Adapter；
- 通用 `ToolGovernor`；
- 裸 Python Agent 示例。

三个框架 Adapter 都复用 `ToolGovernor`，说明框架只是边缘适配器，治理核心不再依赖某一种 Agent 框架。

### v0.17.0：HTTP 服务化

新增 Starlette/ASGI 服务，提供：

- `POST /v1/govern/tool-call`；
- `POST /v1/govern/resume-after-approval`；
- `GET /health`。

HTTP lifespan 会启动和关闭 Controller，因此 HTTP 服务可以脱离 Agent 进程独立运行。

### v0.18.0：异步审批与可观测性

新增：

- 长轮询审批等待；
- Prometheus 指标；
- trace ID；
- 结构化日志；
- 管理面 pending approval 和 audit 查询。

这一步把治理从单次同步函数调用推进到持续运行的服务模型。

### v0.19.0：SSE 与 gRPC/protobuf

新增：

- SSE 实时审批通道；
- `governance.proto`；
- gRPC Server；
- Python gRPC Client；
- 工具调用、审批恢复、审批等待、健康检查、待审批和审计 RPC。

这提供了跨进程和潜在跨语言接口，但 gRPC 当前仍存在服务生命周期与认证缺口。

## 5. 最新模块边界

### 5.1 `LoopController`

它是最新 Python 和网络服务路径的治理编排入口，负责：

1. 查找 Agent；
2. 创建或恢复 Task/Session；
3. 构造 ActionProposal；
4. 执行 R1 风险分类；
5. 调用 R2 Checkpoint；
6. 写 propose/evaluate 审计；
7. 对审批请求持久化并暂停；
8. 对 allow/modify 调用 `Checkpoint.forward()`；
9. 写 execute 审计；
10. 审批后恢复原请求。

### 5.2 `ToolGovernor`

它是框架无关的 Python SDK 抽象，构造时固定 Agent/User，并将每次工具调用转发给 Controller。LangChain、OpenAI Agents 和 AutoGen 适配器都在其上实现。

这说明项目目前不是“为每个 Agent 框架复制一套治理逻辑”，而是：

```text
框架 Adapter → ToolGovernor → LoopController
```

### 5.3 `Checkpoint`

Checkpoint 仍是实时授权核心，承担：

- 身份存在性和 Proposal 一致性；
- Capability Profile 默认拒绝；
- 工具参数和调用次数；
- 组合风险；
- Session Risk；
- OPA/Rego；
- Budget/Reservation；
- Decision TTL、max_uses、call_id 防重放；
- 审批请求与最终化；
- Authority Token；
- 执行前参数绑定；
- 调用 MCPGateway。

服务化没有绕开原来的安全内核，而是把它放到新的 Controller 和网络边界后面。

### 5.4 `MCPGateway`

Gateway 是治理后的真实工具出口，负责启动真实 MCP Server、维护工具映射并转发调用。它本身是“哑代理”，不承担治理判断。

正确强制链是：

```text
Agent → Controller → Checkpoint.forward → MCPGateway → Tool
```

但 `Runtime.gateway` 仍可被可信进程内代码直接调用，因此进程内插件边界和部署隔离仍然重要。

### 5.5 MCP Proxy

MCP Proxy 继续为标准 MCP Client 提供接入：

```text
外部 Agent → MCP Client → Loop Controller Proxy → Checkpoint → MCPGateway
```

它是真实外部 Agent 接入能力，不是内部 Planner。但它仍直接使用 Runtime/Checkpoint，而不是调用新的 Controller 或远程 HTTP/gRPC 服务，因此目前更像一条并行治理入口，而不是完全“薄化”的协议 Adapter。

## 6. HTTP 服务判断

HTTP 服务边界是真实存在且生命周期基本完整的：

- 独立 CLI：`lc server`；
- Starlette/ASGI；
- Uvicorn；
- Controller 在 lifespan 中启动和关闭；
- 工具调用和审批恢复均汇入同一个 Controller；
- 支持 long-polling、SSE、metrics、健康检查和管理查询。

因此 HTTP 可以被称为独立治理服务，而不只是 Python 库中的一个函数。

但当前认证模型较弱：

- `LOOP_CONTROLLER_API_KEY` 未设置时默认允许；
- 一个共享 API key 同时覆盖 Agent 调用、审批和审计管理面；
- `agent_id/user_id` 仍由请求 Body 声明；
- 没有 OIDC、OAuth2、mTLS、workload identity 或凭证到 Agent 的固定映射。

所以它解决了跨进程接入，不等于解决可信 Agent 身份。

## 7. gRPC 服务判断

### 已实现

proto 定义了：

- `EvaluateToolCall`；
- `ResumeAfterApproval`；
- `WaitForApproval`；
- `GetHealth`；
- `ListPendingApprovals`；
- `QueryAuditEvents`。

Servicer 和 Python Client 均已存在，调用会进入同一个 Controller。协议层可以支持其他语言根据 proto 生成 Client。

### 关键实现缺口

`lc grpc-server` 当前只执行：

```text
build_controller → serve
```

但没有执行：

```text
controller.start() → runtime.start() → gateway.start()
```

因此 gRPC 服务可以接收请求并做治理判定，但 allow 后尝试调用真实 MCP 工具时，Gateway 可能尚未启动。现有 gRPC 测试使用 Mock Controller/Runtime，未覆盖真实服务生命周期。

此外：

- 服务端使用 `add_insecure_port()`；
- Client 使用 insecure channel；
- 无认证 interceptor；
- 无 mTLS；
- `agent_id/user_id` 来自请求字段；
- gRPC 管理和审计 RPC 无独立角色授权。

所以应把 v0.19.0 描述为“gRPC 服务边界和协议契约已建立”，不应描述为“生产级 gRPC 治理服务已经完备”。

## 8. 身份与权限评估

当前身份仍主要是静态治理身份：

- Agent 在 YAML 注册；
- Agent 固定绑定 Profile 和 Owner；
- Checkpoint 校验 Agent 是否存在；
- Session/Task 持有 Agent/User 字段。

但外部入口没有证明调用者确实拥有所声明 Agent 身份。需要区分：

```text
agent_id 在注册表中存在
≠
调用者经过密码学认证且被绑定到该 agent_id
```

建议后续把认证主体放入服务上下文，由服务端从 token、证书或 workload identity 映射 Agent，不再接受 Body/proto 任意覆盖。

## 9. 策略、审批和 Decision 评估

### 已成立能力

- Capability Profile 默认拒绝；
- OPA 故障 fail-closed；
- deny 优先；
- 参数范围和组合风险；
- Session 风险累计和连续拒绝熔断；
- 审批请求持久化；
- 审批人职责分离；
- 原工具和参数绑定；
- Decision TTL、max_uses；
- `call_id` 防重放；
- 审批后由治理层恢复执行。

### 主要缺口

1. `resume_after_approval(request_id)` 没有验证当前网络调用者属于原 Agent/User/Task；
2. MCP 审批状态工具只凭 Decision ID 查询；
3. HTTP/gRPC wait 接口在审批完成后会触发真实执行，表面查询接口具有副作用；
4. CLI 审批人身份只是命令行字符串，不绑定 SSO/OS/证书主体；
5. ApprovalStore 的检查和写入不是多进程原子操作；
6. 审批持久化记录包含原始工具参数，可能集中保存敏感内容。

## 10. 风险、动态权限和审计评估

### 风险

Session Risk 会累计风险分、标签、拒绝和审批次数，并进入 OPA。连续拒绝可熔断 Session。

缺口是审批通过/拒绝恢复链没有调用 RiskState 的相应事件更新，导致审批计数和连续拒绝状态可能与真实审批历史不一致。

### Earned Authority

v0.11.0 已实现 AuthorityRequest/AuthorityToken、能力范围、有效期、预算和撤销。但当前没有发现完整 HTTP、gRPC、MCP 或 CLI 生产申请入口；`user_confirmation` 仍只是布尔值，不是可信用户确认凭证。

所以当前更准确的描述是“动态权限内核已实现，但外部可信申请闭环尚未接通”。

### R3 审计

Controller 路径会记录 propose、evaluate、approve/deny、approval_consumed、execute，并使用 HMAC JSONL 哈希链。v0.12.0 还加入异步规则审计分析与告警。

缺口包括：

- MCP Proxy 没有复用 Controller 的完整审计链；
- gRPC 审计查询没有身份授权；
- HTTP 审计管理面只依赖共享 API key；
- seal 有实现但没有生产周期调用；
- 多 writer 会破坏 seq 和 hash chain；
- 工具副作用和 execute 审计不是同一事务。

## 11. 持久化和并发边界

当前生产 Runtime 大量使用 append-only JSONL：

- Task；
- Session；
- Conversation；
- Approval；
- Decision；
- Risk；
- Budget；
- Reservation；
- Authority；
- Audit。

这适合单机 MVP、重启恢复和可解释演示，但不适合多 worker 或水平扩展。核心问题是：

- 内存索引不跨进程同步；
- 没有文件锁或数据库唯一约束；
- `call_id` 检查与记录不是跨进程原子操作；
- Decision 消费不是跨进程原子操作；
- 审批决定可能竞争写入；
- Budget/Reservation/Authority 不在同一事务；
- Audit seq/hash 无单 writer 保证；
- Decision 消费、真实副作用和审计无法实现 exactly-once。

服务化后这一问题比 Python 库阶段更重要，因为 Uvicorn/gRPC 很容易被部署成多个 worker 或多个实例。

## 12. 强制治理与旁路判断

项目当前有两种语义：

### 强模式

Controller 收到请求后，只有 allow/modify 才由 Checkpoint 内部调用 Gateway 执行工具。Agent 不直接拿到真实工具句柄。

### 弱点

如果 Agent 仍能：

- 直连上游 MCP Server；
- 自己启动同一个 MCP Server；
- 持有真实数据库、文件系统或网络凭证；
- 在同进程中直接调用 `Runtime.gateway`；

则它可以绕过治理。

生产强制性必须由部署共同保证：

```text
Agent 只能访问 Loop Controller
Loop Controller 才持有真实工具凭证
上游 MCP Server 只接受 Controller 网络身份
OS、网络和 Secret 管理阻止旁路
```

HTTP/gRPC 只解决“Agent 如何调用治理”，不会自动解决“Agent 是否还能绕过治理”。

## 13. 与既有备案和产品方向讨论的对应

最新版本已经解决或推进：

- 不再把治理与内部 Planner 绑定；
- 支持多种 Agent 框架；
- 提供独立 HTTP/gRPC 服务边界；
- Agent 主动申报工具动作；
- 服务统一持有策略、审批、风险、Decision 和审计状态；
- 支持异步审批等待和恢复；
- proto 为跨语言接入提供契约基础。

仍未解决：

- 密码学 Agent Identity；
- Memory Governance；
- A2A 委托链和权限传播；
- 企业 IAM 和审批人认证；
- 运营级 Kill Switch；
- 多租户和多实例事务状态；
- 统一协议 Adapter 内核；
- 不可绕过的基础设施出口。

因此，最新版本证明了此前推荐路线的可行性，但尚未完成“Agent 主体全生命周期治理”。目前仍聚焦“Agent 工具调用 Runtime 治理”。

## 14. 建议优先级

### P0：先补安全和正确性闭环

1. 修复 gRPC 生命周期：在服务启动/关闭时调用 `controller.start()/aclose()`；
2. HTTP 未配置认证材料时默认拒绝启动；
3. gRPC 引入 mTLS 或认证 interceptor，默认只监听 loopback；
4. 认证凭证固定映射 Agent/User，禁止请求字段覆盖主体；
5. 审批状态、等待和恢复加入 Agent/User/Task 对象级授权；
6. 将 wait/status 与 execute 分离，GET/查询 RPC 不触发副作用；
7. 修复审批结果到 Session Risk 的更新；
8. Proxy 路径统一写完整治理审计；
9. 明确禁止多 worker，并在启动时检测或文档化强约束。

### P1：统一架构和事务状态

1. MCP Proxy 改为薄 Adapter，统一调用 Controller 或远程治理服务；
2. Decision、Approval、Risk、Budget、Reservation、Authority 迁入事务数据库；
3. 使用唯一约束/CAS 保证 Decision 单次消费和 call_id 唯一；
4. 审计采用单 writer 或事务 outbox；
5. 管理面和数据面分离授权；
6. Authority 增加可信用户确认和正式申请入口；
7. 文件路径 canonicalization 和 fetch_url SSRF 防护。

### P2：企业治理能力

1. OIDC/OAuth2/mTLS/workload identity；
2. 多租户 Policy Pack 和策略发布治理；
3. A2A 身份互鉴、委托令牌和权限传播天花板；
4. Memory Object、生命周期、必要性和审计；
5. 全局/租户/Agent/Session/Tool Kill Switch；
6. 跨语言正式 SDK、版本兼容和 RFC；
7. 治理验证集与绕过测试矩阵。

## 15. 对外表述建议

### 可以说

> v0.19.0 已完成从内部 Planner 驱动 Runtime 到外部 Agent 驱动治理控制平面的核心重构。Agent 自己规划和运行，Loop Controller 通过 Python SDK、框架 Adapter、HTTP、gRPC 和 MCP 接收每次工具调用，在真实副作用发生前统一执行身份登记、权限、策略、审批、风险、Decision 和审计，并在授权后代理执行工具。

### 不宜说

- 已支持任意 Agent 零配置接入；
- 已具备不可伪造的 Agent 身份；
- 已可多 worker/集群生产部署；
- 所有协议入口均已完成生产级认证和生命周期；
- Agent 无法绕过治理；
- 已实现完整 A2A、Memory Governance 或企业 Kill Switch；
- 已经是生产级企业治理平台。

## 16. 最终架构判断

按严格判据逐项判断：

| 判据 | 结论 |
|---|---|
| 核心是否仍拥有 Planner | 否，v0.14.0 已从核心删除 |
| Agent 是否掌握主循环 | 是 |
| Agent 是否按调用主动申报动作 | 是 |
| 治理是否可脱离 Agent 进程运行 | HTTP 是；gRPC边界是，但生命周期有缺口 |
| 是否有 HTTP 服务 | 是 |
| 是否有 protobuf/gRPC | 是 |
| 是否有 Python SDK/Client | 是，ToolGovernor 和 gRPC Client |
| 是否支持多 Agent 框架 | 是，LangChain/OpenAI Agents/AutoGen/裸 Python |
| 是否跨语言 | proto 提供基础，正式多语言 SDK 尚未提供 |
| 是否由治理层代理真实执行 | Controller/MCP Proxy 路径是 |
| 是否只是建议式 verdict API | 否，支持治理后代理执行；但也有 evaluate 两段式接口 |
| 是否具备可信外部身份 | 否 |
| 是否具备多实例事务安全 | 否 |
| 是否天然不可绕过 | 否，依赖凭证和网络隔离 |
| 是否完成生产级独立服务 | 尚未完成 |

最终结论：

> 这不是给旧 Python Agent 框架简单套上 HTTP/gRPC 外壳，而是已经完成了核心控制关系的反转：Agent 成为调用方，Loop Controller 成为独立治理方。重构方向正确且已有真实代码支撑；下一阶段重点不应继续增加更多“入口”，而应优先补齐可信身份、gRPC 生命周期、审批对象级授权、事务状态、统一 Adapter 和不可绕过的执行出口。
