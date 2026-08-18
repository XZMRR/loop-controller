# Loop Controller

企业级 AI Agent 治理层（v0.2.0 可信执行基线）。基于 R0-R3 分层治理模型，让 Agent 的每一次工具调用都经过"申报 → 策略判定 → 审批 → 授权转发 → 审计"的完整闭环；v0.2.0 额外引入会话级风险记忆与 HMAC 审计链。

**核心命题**：R1（Agent）不持有任何外部工具的执行通道；R2 Checkpoint 作为 MCP Client Policy Gateway，是所有工具调用的**唯一授权出口**。

## 特性

- **默认拒绝**：未在 CapabilityProfile 中明确允许的工具与参数，一律拒绝；
- **策略即代码**：OPA / Rego v1 实时判定，轻量、确定性、断网可用，fail-closed；
- **人工审批链路**：高风险动作路由 R0-delegate 审批，deny 永远优先于 require_approval；
- **权限组合分析**：静态规则表检测"A 权限 + B 权限 = C 风险"的组合（如读取知识库后外发邮件）；
- **防重放授权**：Decision 单次使用、限期有效、跨重启持久化；
- **可检测篡改的审计**：JSONL 全量日志 + 默认 HMAC-SHA256 哈希链 + seal 记录 + event/seal key 域分离 + 参数分级掩码；
- **会话级风险记忆**：同一 session 内的异常动作会累积风险分，高 session risk 自动将 allow/modify 升级为 require_approval；
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
R0-delegate：审批打桩（async 接口，配置化 approve/deny）
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

# 5. 跑端到端示例（研究助手：搜索 → 读知识库 → 写摘要 → 审批后发邮件）
python examples/research_agent_example.py

# 6. 跑测试
LOOP_CONTROLLER_AUDIT_HMAC_KEY=$LOOP_CONTROLLER_AUDIT_HMAC_KEY pytest tests/ -v

# 7. 校验审计链完整性
python -c "import os; from loop_controller.infra.config_loader import ConfigLoader; \
           from loop_controller.infra.audit_store import JsonlAuditStore; \
           cfg = ConfigLoader().load('config'); \
           key = ConfigLoader.resolve_audit_key(cfg); \
           print(JsonlAuditStore(cfg.audit_log_path, hash_algo='hmac-sha256', hmac_key=key).verify_chain())"
```

## 配置

所有治理行为由 `config/` 下的文件定义，改配置 = 重启进程：

| 文件 | 作用 |
|---|---|
| `agents.yaml` / `users.yaml` | Agent 身份与 Profile 绑定、用户名录 |
| `profiles.yaml` | CapabilityProfile：工具白名单、参数白名单、调用上限、预算 |
| `mcp_servers.yaml` | MCP server 连接、工具映射、`cost_per_call` |
| `permission_rules.yaml` | 权限组合规则（deny / require_approval） |
| `masking_rules.yaml` | 审计/审批的分级掩码规则 |
| `approval.yaml` | 审批人与打桩行为（approve/deny） |
| `policies/default.rego` | 主策略（Rego v1） |

## 已知局限

**本项目当前为 v0.2.0，存在明确声明的能力边界**，使用前必读 [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md)。要点：审计哈希链需配合 seal/WORM、HMAC-SHA256 为默认、多轮对话上下文尚未进入 R2、单进程 asyncio 假设、外部 Agent 直接接入尚不支持。

## 文档

- `Loop_Controller_MVP方案_纯工具调用_v1.1.md`——架构与接口的唯一权威依据
- `Loop_Controller_MVP开发指南_v1.0.md`——三迭代开发计划与踩坑清单
- `development_log.md`——开发记录与决策追溯
- `KNOWN_LIMITATIONS.md`——MVP 明确声明的能力边界
- `发布检查清单_v0.1.0.md`——v0.1.0 发布前手动 gate 与自动化回归清单
- `发布检查清单_v0.2.0.md`——v0.2.0 发布前手动 gate 与自动化回归清单

## 许可与边界

Loop Controller 采用 Open-Core 模式：本仓库为开源工程层（R1/R2/R3 框架、策略引擎、审计、权限控制）。意图控制接口、官方策略库等商业组件不在本仓库。
