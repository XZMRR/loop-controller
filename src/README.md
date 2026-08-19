# Loop Controller

企业级 AI Agent 治理层（v0.4.0）。基于 R0-R3 分层治理模型，让 Agent 的每一次工具调用都经过"申报 → 策略判定 → 审批 → 授权转发 → 审计"的完整闭环；v0.3.0 引入异步人工审批 CLI 与动态会话上下文，v0.4.0 实现跨 Task Session 风险状态持久化与连续拒绝熔断。

**核心命题**：R1（Agent）不持有任何外部工具的执行通道；R2 Checkpoint 作为 MCP Client Policy Gateway，是所有工具调用的**唯一授权出口**。

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

## 架构

```
User → R1 Agent（规划 + 轻量分类器自检）
         │  ActionProposal（动作申报）
         ▼
R2 Checkpoint（身份校验 → 防重放 → Profile → 预算 → 组合规则 → OPA/Rego）
         │  Decision: allow / deny / modify / require_approval
         ▼
MCPGateway ──→ MCP Servers（filesystem / email mock / ...）
         
R3 AuditStore：异步全量记录 + 哈希链 + 分级掩码（只读，无指令下发权）
R0 AsyncApprovalManager：异步审批请求持久化，审批人通过 `lc` CLI 写入结果
```

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

# 4. 启动 OPA sidecar
opa run --server --addr localhost:8181 policies/

# 5. 跑端到端示例（研究助手：搜索 → 读知识库 → 写摘要 → 暂停待审批）
python examples/research_agent_example.py

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
```

会话上下文持久化路径默认是 `./data/conversations.jsonl`，审批请求/结果持久化路径默认是 `./data/approvals.jsonl`，可在 `config/` 下新增 `conversation.yaml` / `approval_store.yaml`（或环境变量 `LOOP_CONTROLLER_CONVERSATION_PATH` / `LOOP_CONTROLLER_APPROVAL_STORE_PATH`）覆盖；Planner 通过 `UserQuestion` 请求用户补充后，外部调用方写入 `runtime.add_user_message(...)` 并调用 `resume_task` 继续。

## 配置

所有治理行为由 `config/` 下的文件定义，改配置 = 重启进程：

| 文件 | 作用 |
|---|---|
| `agents.yaml` / `users.yaml` | Agent 身份与 Profile 绑定、用户名录 |
| `profiles.yaml` | CapabilityProfile：工具白名单、参数白名单、调用上限、预算 |
| `mcp_servers.yaml` | MCP server 连接、工具映射、`cost_per_call` |
| `permission_rules.yaml` | 权限组合规则（deny / require_approval） |
| `masking_rules.yaml` | 审计/审批的分级掩码规则 |
| `approval.yaml` | 审批人默认与规则（用于确定 escalation_target） |
| `policies/default.rego` | 主策略（Rego v1） |

## 已知局限

**本项目当前为 v0.3.0-iteration5，存在明确声明的能力边界**，使用前必读 [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md)。要点：审计哈希链需配合 seal/WORM、HMAC-SHA256 为默认、单进程 asyncio 假设、外部 Agent 直接接入尚不支持。

## 文档

### 当前有效

- `Loop_Controller_下一阶段开发方案_v0.3.0.md`——v0.3.0 开发方案与 Iteration 4/5 验收标准
- `loop_controller_v0.4.0_development.md`——v0.4.0 跨 Task Session 风险状态持久化方案
- `loop_controller_v0.5.0_development.md`——v0.5.0 MCP Proxy / 外来 Agent 接入方案
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
- `history/discussion_summary_for_planning_agent.md`——代码/规划 agent 讨论摘要

## 许可与边界

Loop Controller 采用 Open-Core 模式：本仓库为开源工程层（R1/R2/R3 框架、策略引擎、审计、权限控制）。意图控制接口、官方策略库等商业组件不在本仓库。
