# Loop Controller

企业级 AI Agent 治理层（v0.36.1）。基于 R0-R3 分层治理模型，让 Agent 的每一次工具调用都经过"申报 → 吊销检查 → 策略判定 → 审批 → 执行前复查 → 授权转发 → 审计"的完整闭环。

**v0.33.0 战略方向**：在 v0.32.0 接入方式收敛的基础上，本版本聚焦**Python 工具治理层的健壮性加固**：堵住 Agent SDK、MCP Proxy、HTTP REST API 与配置校验中当前最危险的安全、稳定与正确性漏洞，使 `@governed` 主路线和网络接入面达到可生产部署基线。HTTP REST API 与 MCP Proxy 继续作为**网关/强制约束层**保留，用于外部不可控 Agent 或跨语言接入；FastAPI 与 gRPC 接入已从核心包移除，LangChain 集成降级为 `examples/integrations/` 可选示例。

v0.28.0 为审计/证据链引入外部可信锚点；v0.29.0 修复人工审批跨进程闭环失效与预算/决策状态泄漏；v0.32.0 重点完善 Agent 主动接入体验，并通过 22 个集成测试覆盖 `@governed`、hook 注册表、审批流、审批后自动重试、多步骤工作流、MCP Proxy、LangChain 等真实场景；v0.33.0 进一步补齐 SDK 并发安全、API 入口防御、错误响应脱敏、admin 权限隔离与 CI 分层验证。

**核心命题**：R1（Agent）不持有任何外部工具的执行通道；R2 Checkpoint 作为工具调用治理控制平面，是所有经治理工具调用的**唯一授权出口**。

## 特性

- **默认拒绝**：未在 CapabilityProfile 中明确允许的工具与参数，一律拒绝；
- **策略即代码**：OPA / Rego v1 实时判定，轻量、确定性、断网可用，fail-closed；
- **异步人工审批 CLI**：高风险动作触发 `needs_approval` 暂停态，审批人通过 `lc approvals list/approve/deny` 写入结果，任务 `resume_task` 后继续；deny 永远优先于 require_approval；
- **权限组合分析**：静态规则表检测"A 权限 + B 权限 = C 风险"的组合（如读取知识库后外发邮件）；
- **防重放授权**：Decision 单次使用、限期有效、跨重启持久化；
- **可检测篡改的审计**：JSONL 全量日志 + 默认 HMAC-SHA256 哈希链 + seal 记录 + event/seal key 域分离 + 参数分级掩码；
- **会话级风险记忆**：同一 session 内的异常动作会累积风险分，高 session risk 自动将 allow/modify 升级为 require_approval；
- **动态会话上下文**：`ConversationContext` 保存当前 Task 的用户/Agent 多轮消息，`build_governance_context` 确定性拼装进 R2 input，让策略看到完整意图；
- **ask_user 暂停态**：Planner 可返回 `UserQuestion`，`run_task` 返回 `needs_user_input`，外部补充输入后 `resume_task` 继续执行；
- **预算控制**：按工具计费的 token 预算，超支即拒。
- **可信身份控制平面**（v0.20.0）：Agent 身份由 JWT / mTLS / 静态 token 验证，`agent_id` 从凭证推导，不可伪造；
- **可插拔执行器抽象**（v0.20.0）：`ExecutorRegistry` + `ToolExecutor` 让 MCP / HTTP 协议型工具可统一接入；
- **远程 HTTP Harness 生产出口**（v0.27.0）：`HarnessExecutor` 支持 HMAC/API Key、timestamp + nonce 防重放、TLS/可选 mTLS 客户端、每后端进程内并发门控、健康检查与启动校验；子进程仅供开发/测试，Docker/Kubernetes 由部署层运行独立 HTTP Harness Service；
- **全局吊销与 Kill Switch**（v0.26.1）：可按 agent、user、tool、secret 阻断调用；可信 Secret 依赖来自执行器当前配置，所有执行路径在最终执行边界复查，阻断会释放未提交预算并写审计；
- **本地签名证据链**（v0.26.1）：审计事件可写入 HMAC-SHA256 或 Ed25519 签名的链式 JSONL 证据；异步写入保持单进程有序，并通过审计—证据交叉校验和签名本地 checkpoint 检测单边丢失与相对回退；
- **外部可信锚点**（v0.28.0）：checkpoint 成功后向远程锚点服务发布当前链状态，获取 Ed25519 签名的 receipt；启动时交叉验证远程最新锚点与本地证据/审计；冲突/回滚时进入写阻断并告警；提供 Admin 端点用于 verify / publish / bootstrap；
- **审批与状态恢复闭环**（v0.29.0）：审批结果通过增量 `refresh()` 对运行中的 Runtime 可见，CLI 与 Admin 端点复用统一校验；审批记录不可覆盖；`portalocker` 跨进程锁保护追加写；启动期清扫过期预算预留；Decision 状态机与 `finalized` 持久化防止重复消费。
- **Agent 主动接入体验**（v0.32.0）：`@governed` 装饰器支持同步/异步函数、`hook_tool_registry` 批量治理注册表、保留参数 `_loop_controller_session_id/task_id/task_context` 透传治理上下文；`require_approval` 支持阻塞等待审批后自动重试并返回执行结果。
- **网络级治理入口**：HTTP REST API、MCP Proxy 作为网关/强制约束层保留；gRPC 与 FastAPI 集成已移除核心包。
- **API 入口防御性中间件**（v0.33.0）：HTTP 服务增加请求体大小限制、可配置限流、CORS 来源校验与全局异常处理器；MCP Proxy 增加 SSE 并发上限、请求体限制、限流与错误响应脱敏，避免内部信息泄露与 DoS。
- **SDK 并发安全与 fail-closed**（v0.33.0）：`GovernanceRuntime` 改用 `ContextVar` 隔离运行时上下文；`GovernanceResult` 内部引用改为 `PrivateAttr`，避免深拷贝与序列化泄露；`hook_tool_registry` 两阶段原子替换，失败回滚；`wait_for_approval` 超时后清理审批请求。
- **admin 工具权限隔离**（v0.33.0）：kill_switch、revoke 等 admin 工具仅在调用 agent 的 `profile_id` 属于 `entrypoints.admin.agent_profiles` 白名单时才允许执行，默认关闭。
- **配置校验 fail-closed**（v0.33.0）：`_check_dirs_writable` 覆盖所有持久化路径；配置加载的 `ValidationError` 统一包装为 `ConfigValidationError`；OPA fixture 通过子进程端口探测消除端口抢占 TOCTOU。
- **CI 分层**（v0.33.0）：单元测试与集成测试拆分为独立 job，integration job 限定 `pytest tests/integration -m integration` 并加 30 分钟超时。

## 架构

```
User → R1 Agent（规划 + 轻量分类器自检）
         │  ActionProposal（动作申报）
         ▼
R2 Checkpoint（身份校验 → 吊销检查 → 防重放 → Profile → 预算 → 组合规则 → OPA/Rego）
         │  Decision: allow / deny / modify / require_approval
         ▼
ExecutorRegistry ──→ MCPExecutor ──→ MCPGateway ──→ MCP Servers
                                  ├─→ HTTP Executor（v0.21+）
                                  ├─→ LocalFunctionExecutor（v0.23+，可选辅助）
                                  └─→ HarnessExecutor（v0.27）──HTTPS/HMAC──→ 独立 Harness Service

Shell / SQL / Browser 等高危工具通过外部 MCP Server 或独立 Harness Service 接入，
不在 Loop Controller 进程内执行。容器/Kubernetes/VM 提供真实隔离；subprocess Harness
仅供开发/集成测试。见 examples/contrib/mcp_wrappers/ 与 examples/contrib/harness/。

R3 AuditStore：异步全量记录 + 哈希链 + 可选本地签名证据链 + 分级掩码（只读，无指令下发权）
R0 AsyncApprovalManager：异步审批请求持久化，审批人通过 `lc` CLI 写入结果
```

## 接入形态（v0.33.0）

| 形态 | 定位 | 方向 | 成熟度 | 说明 |
|---|---|---|---|---|
| **Python SDK `@governed`** | **主路线 / 推荐** | 主动接入 | 高 | 装饰同步/异步函数，调用自动提交 Loop Controller；支持 `hook_tool_registry` 批量治理；`require_approval` 可阻塞等待审批后自动重试 |
| **HTTP REST API** | 生产入口 / 管理面 | 网关/强制约束 | 高 | Agent 通过 REST 调用，支持 JWT/API Key；含完整 admin 审批/审计接口 |
| **MCP Proxy** | 兼容入口 | 网关/强制约束 | 高 | 对标准 MCP Client 透明，支持 stdio/SSE、mTLS、审批恢复 |
| **LangChain 集成** | 可选示例 | 主动接入示例 | 中 | 已移出核心包，见 `examples/integrations/langchain_example.py`；依赖可选 `langchain_core` |

**已移除**：

- **FastAPI 集成**：`GovernedFastAPI` / `governed_route` 已移除，HTTP REST API 覆盖同样场景。
- **gRPC 服务**：`grpc_server` / `grpc_client` / `lc grpc-server` 已移除，HTTP REST API 覆盖同样场景。

**方向说明**：
- **主动接入**：Agent 主动调用 SDK 或装饰器，把每次工具调用提交给 Loop Controller。这是我们认定的主路线，控制权在 Agent 侧，错误语义最清晰。
- **网关/强制约束**：Loop Controller 作为网络代理或网关，对不感知治理的外部 Agent 做强制拦截。用于不可控 Agent、跨语言、遗留系统接入。

## 快速开始

> 以下命令均在项目根目录（本文件上级目录）执行。

**依赖**：Python ≥ 3.12、OPA ≥ 1.0、Node.js ≥ 20（filesystem MCP server）。

```bash
# 1. 安装
pip install -e ".[dev]"        # 或 uv sync

# 2. 准备数据目录
mkdir -p /data/kb /data/output
echo "# AI 合规 checklist" > /data/kb/ai_compliance_checklist.md

# 3. 配置审计 HMAC key（32 字节随机熵，hex 或 base64；生产环境应从密钥管理注入）
export LOOP_CONTROLLER_AUDIT_HMAC_KEY=$(openssl rand -hex 32)
# 可选：配置 key_id，用于未来密钥轮换识别（默认为 "default"）
export LOOP_CONTROLLER_AUDIT_KEY_ID="default"
# config/evidence.yaml 默认启用 Ed25519；配置 32 字节私钥（base64）
export LOOP_CONTROLLER_EVIDENCE_PRIVATE_KEY=$(openssl rand -base64 32)

# 4. 启动 OPA sidecar
opa run --server --addr localhost:8181 policies/

# 5. 跑端到端示例（研究助手：搜索 → 读知识库 → 写摘要 → 暂停待审批）
python examples/research_agent.py

# 示例会在 send_email 前暂停并返回 needs_approval；另开终端审批后继续：
# lc approvals list --config-dir config
# lc approvals approve <decision_id> --approver zhang_manager --comment "同意发送"
# （然后调用方用 resume_task 继续执行）

# 6. 跑测试
LOOP_CONTROLLER_AUDIT_HMAC_KEY=$LOOP_CONTROLLER_AUDIT_HMAC_KEY pytest tests/ -v

# 7. 校验审计链完整性
python -c "import os; from loop_controller.infra.config_loader import ConfigLoader; \
           from loop_controller.infra.audit_store import JsonlAuditStore; \
           cfg = ConfigLoader().load('config'); \
           key = ConfigLoader.resolve_audit_key(cfg); \
           print(JsonlAuditStore(cfg.audit_log_path, hash_algo='hmac-sha256', hmac_key=key).verify_chain())"

# 8. 启动 HTTP 服务（生产入口）
# lc server --config-dir config --port 8080
#
# 调用示例（需先在 config/identity.yaml 配置静态 token 或 JWT）：
# curl -H "Authorization: Bearer <token>" \
#      -H "Content-Type: application/json" \
#      -d '{"tool_name":"web_search","arguments":{"query":"AI"}}' \
#      http://localhost:8080/v1/govern/tool-call
```

会话上下文持久化路径默认是 `./data/conversations.jsonl`，审批请求/结果持久化路径默认是 `./data/approvals.jsonl`，可在 `config/` 下新增 `conversation.yaml` / `approval_store.yaml`（或环境变量 `LOOP_CONTROLLER_CONVERSATION_PATH` / `LOOP_CONTROLLER_APPROVAL_STORE_PATH`）覆盖；Planner 通过 `UserQuestion` 请求用户补充后，外部调用方写入 `runtime.add_user_message(...)` 并调用 `resume_task` 继续。

## 配置

治理行为由 `config/` 下的文件定义；除吊销列表和已注明的热更新配置外，修改后需重启进程：

| 文件 | 作用 |
|---|---|
| `agents.yaml` | Agent 身份、Profile 绑定、外部身份元数据 |
| `profiles.yaml` | CapabilityProfile：工具白名单、参数白名单、调用上限、预算 |
| `mcp_servers.yaml` | MCP server 连接、工具映射、`cost_per_call` |
| `permission_rules.yaml` | 权限组合规则（deny / require_approval） |
| `masking_rules.yaml` | 审计/审批的分级掩码规则 |
| `approval.yaml` | 审批人默认与规则（用于确定 escalation_target） |
| `identity.yaml` | 身份 Provider 配置（static / jwt / mtls） |
| `entrypoints.yaml` | HTTP/MCP Proxy 入口认证方式与开关 |
| `harness_tools.yaml` | Harness 后端与工具配置（v0.27.0，默认注释；生产模板为 HTTPS + HMAC + 健康检查） |
| `revocation.yaml` | 全局吊销列表与 Kill Switch（v0.26.0，支持热更新） |
| `evidence.yaml` | 本地签名证据链后端与锚点配置（v0.28.0） |
| `policies/default.rego` | 主策略（Rego v1） |

## 已知局限

**本项目当前为 v0.33.0，存在明确声明的能力边界**，使用前必读 [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md)。要点：

- **接入方式已收敛**：v0.32.0 已移除 FastAPI 集成与 gRPC 服务，LangChain 集成降级为 `examples/integrations/langchain_example.py` 可选示例；核心包只保留 `@governed`（主路线）、HTTP REST API、MCP Proxy 三种工具接入方式。v0.33.0 对三条接入线都做了健壮性加固，但网关层的限流、请求体限制仍为单进程内存实现，未提供分布式限流。
- **Harness 默认不启用**：生产仅推荐独立 HTTPS HTTP Harness，参考服务和 subprocess 都不是生产沙箱。
- **单进程/单实例语义**：并发门控、防重放 nonce store、吊销状态与本地 JSONL 写入均未实现分布式。
- **主动接入**：`@governed` 已支持 `wait_for_approval=True` 阻塞等待审批后自动重试；默认行为保持返回 `GovernanceResult`，由调用方自行处理。
- **远程调用**：超时后的执行结果可能未知且不会自动重试；远程取消、跨实例长期幂等和分布式配额尚未实现。
- **Admin 鉴权**：HTTP Admin API 已升级为 API Key + `hmac.compare_digest` 安全比较；admin 工具（kill_switch、revoke 等）在 MCP Proxy 中已按 agent profile 白名单隔离。Harness 只读 Admin 状态端点仍复用现有 API key，未实现基于角色的细粒度授权。
- **审计与证据链**：签名本地 checkpoint 可通过外部可信锚点获得 Ed25519 receipt，但锚点服务本身是外部独立可信系统；KMS/HSM、远程证据存储、多签 receipt 和完整多租户隔离也尚未实现。
- **mTLS fallback 加固**：v0.33.0 已禁止在服务端要求客户端证书但未成功提取身份时 fallback 到默认身份；生产部署仍需正确配置 TLS termination 与 `client_ca_cert`。

## 文档

### 当前有效

- `loop_controller_v0.20.0_development.md`——v0.20.0 可信身份控制平面与执行器抽象基座
- `Loop_Controller_下一阶段开发方案_v0.3.0.md`——v0.3.0 开发方案与 Iteration 4/5 验收标准
- `loop_controller_v0.4.0_development.md`——v0.4.0 跨 Task Session 风险状态持久化方案
- `loop_controller_v0.5.0_development.md`——v0.5.0 MCP Proxy / 外来 Agent 接入方案
- `loop_controller_v0.25.0_development.md`——v0.25.0 Harness 作为生产级执行后端
- `loop_controller_v0.26.0_development.md`——v0.26.0 全局吊销与本地签名证据链
- `loop_controller_v0.26.1_development.md`——v0.26.1 吊销、Kill Switch 与证据链可靠性修复
- `loop_controller_v0.27.0_development.md`——v0.27.0 Harness 生产闭环
- `loop_controller_v0.28.0_development.md`——v0.28.0 可信锚点与审计链外部闭环
- `loop_controller_v0.29.0_development.md`——v0.29.0 审批与状态恢复闭环
- `loop_controller_v0.31.0_development.md`——v0.31.0 外部工具执行沙箱（Harness）
- `loop_controller_v0.32.0_development.md`——v0.32.0 Agent 接入体验优化与接入方式收敛
- `loop_controller_v0.33.0_development.md`——v0.33.0 工具治理层健壮性加固：SDK 与 API 入口安全（当前版本依据）
- `development_log.md`——开发记录与决策追溯
- `KNOWN_LIMITATIONS.md`——MVP 明确声明的能力边界
- `answer.md`——MVP 审查分析与修复状态追踪

### 历史归档

- `history/Loop_Controller_MVP方案_纯工具调用_v1.1.md`——v1.1 架构与接口方案
- `history/Loop_Controller_MVP开发指南_v1.0.md`——v1.0 三迭代开发计划
- `history/发布检查清单_v0.1.0.md`——v0.1.0 发布前 gate 清单
- `history/发布检查清单_v0.2.0.md`——v0.2.0 发布前 gate 清单
- `history/Loop_Controller方案_v1.2增补.md`——v1.2 能力增补方案
- `history/LLMPlanner设计补充_v1.0.md`——v1.0 LLMPlanner 设计补充
- `history/ask.md`——v0.3.0 前规划问题清单
- `history/Loop_Controller_MVP_LangChain_Agent_示例_v1.0.md`——LangChain Agent 示例
- `history/discussion_summary_for_planning_agent.md`——代码/规划 agent 讨论摘要

## 许可与边界

Loop Controller 采用 Open-Core 模式：本仓库为开源工程层（R1/R2/R3 框架、策略引擎、审计、权限控制）。意图控制接口、官方策略库等商业组件不在本仓库。
