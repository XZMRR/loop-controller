# Loop Controller — 企业内部 Agent 工具调用治理基础设施

> **当前版本**：v0.55.0（管理治理台全面接入真实后端）
> **项目阶段**：核心治理层、A2A 调度内核与管理治理台均已完成代码级实现并通过全量自动化测试；真实双独立 OS 进程/容器崩溃接管，以及 Protected MCP 完整拓扑、Go→Python strict、Docker/Kubernetes CNI 与 Secret 隔离等支持环境发布门禁仍待执行。`compatibility` 为默认模式，`strict` 必须显式启用。
> **首选语言**：Python（Agent 生态最丰富，社区传播友好）
> **文档语言**：中文为主，代码与核心 API 文档以英文为主，便于国际化开源

---

## 1. 项目愿景

**Loop Controller** 是企业内部 Agent 的工具调用治理基础设施，其设计灵感来自企业内控（Internal Control）部门的运作方式。我们相信：

> 当 Agent 被赋予越来越多的自主权和工具访问能力时，它不应该被当作一个无限制调用的函数，而应该被当作一名需要被 **聘用、授权、监督、审计** 的数字员工。

更进一步，我们相信：

> **真正有效的治理，不是通过无意义的逐个确认来“审计损失”，而是通过清晰的制度、良好的运行环境和无处不在的保护机制，让 Agent “自然而然地做对”。**

Loop Controller 的核心使命是：

- 为企业内部各种 Agent（自研、LangChain、AutoGen、OpenAI Agents SDK 等）提供统一的 **工具调用治理层**；
- 将企业内控中的 "三道防线"、COSO 五要素、风险评估、控制活动、监督闭环等思想，转化为 Agent 框架中的 **一等设计原语**；
- **不替 Agent 思考，只在 Agent 调用工具时把关**：Agent 自己决定计划，Loop Controller 负责 R0 审批、R1 风险评估、R2 策略判定、R3 审计。

Loop Controller **不是** Agent 开发框架，也**不是**面向陌生 Agent 的开放网关；它是企业内部 Agent 的 **安全运行时**。

---

## 2. 为什么要做这个项目

当前 Agent 生态蓬勃发展，但大多数框架关注的是：

- 如何让 Agent 调用更多工具；
- 如何让多个 Agent 更高效地协作；
- 如何让 Agent 的输出更可靠。

这些都很重要，但企业真正关心的是：

- 这个 Agent 是谁？它能做什么？谁授权它这么做？
- 它的决策过程能否被理解和审计？
- 当它要执行高风险动作时，如何确保有人把关？
- 当它出错时，如何快速定位、止损、整改？

Loop Controller 试图回答这些问题。我们不是要取代 LangChain、CrewAI、OpenAI Agents SDK 等框架，而是要 **在它们之上或之间，提供一层面向治理的运行时控制平面**。

---

## 3. 核心隐喻：把 Agent 当人看

企业内控的核心隐喻是：人是组织风险的来源，也是风险控制的主体。但有效的内控不是让每个人每做一件事都打一次报告，而是：

1. **写好制度**：明确每个岗位能做什么、不能做什么；
2. **塑造环境**：让合规成为默认选项，让越权行为难以发生；
3. **关键把关**：只在真正重要或不确定的节点设置审批；
4. **持续监督**：通过日常监督和独立审计，发现问题并改进制度。

Loop Controller 用同样的逻辑管理 Agent，抽象为 **R0-R3 四层治理模型**：

| 角色 | 企业内控映射 | 主要职责 |
|------|-------------|---------|
| **R0 Governance** | 董事会/经营层/治理层 | 定风险偏好、批 Policy、接收审计报告、问责 |
| **R0-delegate** | 被授权的业务主管/安全员 | 实时审批例外请求，必要时升级到 R0 |
| **R1 Agent** | 业务部门/一线员工 | 接收任务、自检、生成动作申报、接收 R2 授权后的执行结果并返回 |
| **R2 Checkpoint** | 风控/合规/内控部 | 统一策略执行、验证申报、返回 allow/deny/modify/require_approval |
| **R3 Audit** | 内部审计/纪检监察 | 异步采集日志、哈希链完整性、按 trace 查询 |

因此，Loop Controller 不是另一个"审批工具"，而是一个 **Agent 组织的制度基础设施**。

---

## 4. 项目文档结构

```
src/
├── loop_controller/        # Python 治理层源码（R1/R2/R3、三入口、执行器、IIGE、Go 内核桥接）
├── KNOWN_LIMITATIONS.md                        # 明确声明的能力边界
└── 发布检查清单_v0.1.0.md                      # 发布前手动 gate 与回归清单
go/                         # Go A2A 交互治理内核（调度、委托、死信、SSE）
frontend/                   # Vue 3 管理治理台（src/ + docs/ 前端设计文档）
config/                     # 运行配置样例（开发占位凭据，生产必须替换）
docs/
├── architecture/           # 架构文档
│   ├── overview.md
│   ├── 00_r0r3_architecture.md
│   └── 05_mvp_core_abstractions.md
├── research/             # 前期调研报告
│   ├── 01_internal_control_research.md
│   ├── 02_agent_landscape_research.md
│   ├── 03_runtime_governance_landscape.md
│   └── 内控最小岗位结构抽象_v0.1.md
reports/                  # 汇报与研究报告
tests/                    # pytest 单元与集成测试
```

---

## 5. v0.54.0 可靠调度与安全边界摘要

v0.54.0 提供可靠多 Agent 调度：Agent spec/status 与容量路由、durable assignment/outbox、lease/attempt/fence、预算感知 retry/failover/dead-letter、有界静态 DAG、可恢复 SSE、正式 SQLite migration，以及 readiness 与低基数 Prometheus metrics。dispatch 是 **at-least-once**；外部副作用不保证 exactly-once。下游必须按稳定 `delivery_id`/幂等键去重，并拒绝旧 attempt/fence；发送后无法确认结果时进入 `outcome_unknown`，不得盲目重放。

调度状态基于 SQLite WAL，范围是单区域、小到中等规模部署，不提供跨区域共识或无限水平扩展；不提供 federation、动态 Agent 自助注册、通用 workflow 或 Saga/补偿事务引擎。mTLS、身份绑定、受保护出口和 proof 已有代码级验证，但 Protected MCP 完整拓扑、Go→Python strict、Docker/Kubernetes CNI/NetworkPolicy、Secret 隔离及 external DeploymentProof 的生产环境门禁尚未完成。Windows 仅支持开发，不提供 strict production assurance。完整说明见 [`src/KNOWN_LIMITATIONS.md`](./src/KNOWN_LIMITATIONS.md)。

## 6. 快速开始

### 环境要求

- Python 3.12 或 3.13（正式测试版本）
- Git
- OPA（Open Policy Agent）二进制，用于策略引擎

### 安装依赖

```powershell
# 使用 uv（推荐）
uv sync --dev

# 或 pip
pip install -e ".[dev]"
```

### 下载/放置 OPA

```powershell
# Windows
mkdir tools
Invoke-WebRequest -Uri "https://openpolicyagent.org/downloads/v1.19.0/opa_windows_amd64.exe" -OutFile "tools\opa.exe"

# Linux/macOS 参见 .github/workflows/ci.yml
```

### 运行全部测试

```powershell
$env:PYTHONPATH="src"
.venv\Scripts\python.exe -m pytest tests/ -q
```

测试套件覆盖配置校验、R1 风险分类、R2 判定流水线（审批/权限组合/预算/调用次数上限）、R3 哈希链审计与分级掩码、OPA/Rego fail-closed 策略、端到端 approve/deny 路径，以及 SDK、MCP Proxy 与 HTTP REST API 三条接入主线的安全路径与错误脱敏。

### 运行端到端示例

```powershell
$env:PYTHONPATH="src"
.venv\Scripts\python.exe examples/research_agent.py
```

示例会模拟一个完整的研究助手任务：
1. 读取内部合规检查清单；
2. 搜索公开资料（使用本地 mock，断网可运行）；
3. 写入本地摘要文件；
4. 发送邮件给张经理。

每个动作都会经过 **R1 风险分类 → R2 策略判定 → R2 代理转发执行 → R3 审计日志** 的完整闭环。

### 企业内部 Agent 接入（v0.13.0）

v0.13.0 起，推荐用 `LoopController` 治理企业内部 Agent 的工具调用。Agent 自己掌握主循环，只在每次调工具时把请求发给 Loop Controller：

```python
from loop_controller.controller import build_controller

controller = await build_controller(config)
result = await controller.evaluate_and_execute(
    agent_id="researcher_001",
    user_id="alice",
    tool_name="send_email",
    arguments={"to": "zhang@company.com", "subject": "摘要", "body": "请查收"},
)

if result.status == "require_approval":
    # 人工审批后继续
    final = await controller.resume_after_approval(result.request_id)
```

如果用 LangChain，可以直接使用 `GovernedTool` 包装：

```powershell
uv pip install -e ".[langchain]"
$env:PYTHONPATH="src"
.venv\Scripts\python.exe examples\integrations\langchain_example.py
```

`GovernedTool` 会把每个 tool call 转发到 Loop Controller，Agent 完全不知道治理细节。

### 运行真实 MCP server 示例（v0.9.0）

v0.9.0 引入了两个基于 Python 的真实 MCP server（fetch / sqlite），并提供了独立 Agent 示例：

```powershell
# 1. 启动 OPA
opa run --server --addr localhost:8181 policies/

# 2. 初始化演示数据库
.venv\Scripts\python.exe scripts\init_demo_db.py

# 3. 运行真实 Agent 场景
$env:PYTHONPATH="src"
.venv\Scripts\python.exe examples\research_agent.py --scenario research
.venv\Scripts\python.exe examples\research_agent.py --scenario query
.venv\Scripts\python.exe examples\research_agent.py --scenario update
.venv\Scripts\python.exe examples\research_agent.py --scenario notify
.venv\Scripts\python.exe examples\research_agent.py --scenario exfil
.venv\Scripts\python.exe examples\research_agent.py --scenario write-attack
```

`research_agent.py` 不调用 Loop Controller 内部 API，仅以标准 MCP client 身份启动 `lc proxy`，因此可代表外部 Agent。它会真实读取文件、查询 sqlite、写入文件、尝试外发邮件，并被 R2 治理。

### 启动治理台（v0.55.0）

Loop Controller 附带 Vue 3 + Element Plus 管理治理台（`frontend/`），
覆盖仪表盘、审批台（SSE 推送优先）、Agent/Profile 管理、RBAC 绑定、
Policy 生命周期（validate/shadow/publish/rollback）、A2A 任务树、
死信队列（列表 + 重放）、审计查询与系统配置。全部页面均接入
真实 Http 数据源（Mock 仅保留作测试替身）。

```powershell
# 1. 启动 OPA 与 Python Server（见上文）
# 2. 启动 Go A2A 内核（可选，A2A 页面需要）
cd go
go run ./cmd/kernel -addr :8080 -secret test-secret -db ..\data\a2a.db -development -allow-http

# 3. 启动前端开发服务器
cd frontend
npm install
npm run dev   # http://127.0.0.1:5173
```

前端详细文档见
[frontend/docs/frontend_development_plan.md](frontend/docs/frontend_development_plan.md)
与[前端接口契约说明](frontend/docs/frontend_api_gaps.md)。

> **配置安全说明**：`config/` 目录下的所有 token/key（如
> `identity.yaml` 中的 `dev-token-*`）均为开发用占位凭据，仅用于本地
> 演示与测试，**生产部署必须全部替换**并通过配置声明的 `*_token_env`
> 环境变量注入。

### 真实 LLM Agent 验证（v0.9.1）

v0.9.1 使用真实 LLM（DeepSeek）驱动 `LLMPlanner`，让 Agent 自主规划工具调用。API key 只通过环境变量 `LLM_API_KEY` 传入，不落盘：

```powershell
$env:LLM_API_KEY="sk-..."
$env:LOOP_CONTROLLER_AUDIT_HMAC_KEY="a"*64
.venv\Scripts\python.exe examples\llm_agent_demo.py --scenario research
.venv\Scripts\python.exe examples\llm_agent_demo.py --scenario notify
.venv\Scripts\python.exe examples\llm_agent_demo.py --scenario exfil
```

验证结果：

| 场景 | 结果 |
|---|---|
| `research` | LLM 自主完成 web_search → read_file → fetch_url → write_file，均 allow |
| `notify` | query_database allow，send_email 触发 require_approval，审批后发送成功 |
| `exfil` | read_file allow，send_email 到外部地址被 R2 deny |

---

## 7. 验收清单（A1-A14）

| ID | 验收项 | 自动化用例位置 |
|---|---|---|
| A1 | 工具未声明 → deny | `tests/test_checkpoint.py::test_evaluate_deny_tool_not_in_profile` |
| A2 | 读取外部目录 → deny | `tests/test_policy_engine.py::test_read_file_outside_dir_deny` |
| A3 | 写入外部目录 → deny | `tests/test_policy_engine.py::test_write_file_outside_deny` |
| A4 | 外部收件人 → deny | `tests/test_policy_engine.py::test_send_email_external_deny` |
| A5 | 审批 approve/deny 可切换 | `tests/test_e2e_research_agent.py::test_e2e_approve_path_event_sequence / test_e2e_deny_path` |
| A6 | 审批冲突检测 | `tests/test_checkpoint.py::test_build_approval_request_conflict` |
| A7 | 跨重启重放防护 | `tests/test_decision_store.py::test_persists_across_restarts` |
| A8 | 过期 Decision forward 抛异常 | `tests/test_checkpoint.py::test_forward_expired_decision` |
| A9 | 组合风险 deny | `tests/test_permission_interaction.py::test_deny_short_circuit` |
| A10 | 按工具 cost_per_call 预算熔断 | `tests/test_checkpoint.py::test_evaluate_budget_cost_per_call` |
| A11 | OPA 故障 fail-closed | `tests/test_policy_engine.py::test_opa_down_fail_closed` |
| A12 | 审计链篡改检出 | `tests/test_audit_store.py::test_detects_*` / `tests/test_e2e_research_agent.py::test_e2e_tamper_detection` |
| A13 | 参数分级掩码 | `tests/test_masker.py` / `tests/test_checkpoint.py::test_build_approval_request_uses_approval_request_mask_level` / `tests/test_e2e_research_agent.py::test_e2e_masking` |
| A14 | 断网运行 | `tests/test_e2e_research_agent.py::test_e2e_approve_path_event_sequence`（web_search 映射本地 mock） |

---

## 8. 当前阶段与路线图

### Phase 0：前期调研（已完成）

- [x] 调研企业内控运作方式
- [x] 调研 Agent 产品与架构逻辑
- [x] 调研 Agent 安全与治理框架
- [x] 完成 Zenity/Palo Alto/OPA 竞对调研
- [x] 完成 T1 Guardrail 测试 + T3 MCP 权限边界测试
- [x] 输出 R0-R3 架构初稿

### Phase 1：核心抽象设计（已完成）

- [x] 根据讨论反馈收敛 R0-R3 架构
- [x] 设计核心抽象：Agent、CapabilityProfile、Task、ActionProposal、Checkpoint、Policy、AuditEvent
- [x] 输出核心 API 接口草案
- [x] 明确 R1 轻量分类器与 R2 决策引擎边界

### Phase 2：最小可行原型（MVP，已完成）

- [x] 迭代 1：MVP 核心（模型、MCP 网关、OPA 策略、Checkpoint 占位、Planner、审计最小化）
- [x] 迭代 2：安全边界（持久化 DecisionStore、R0-delegate、权限组合、预算、调用次数上限）
- [x] 迭代 3：审计闭环（哈希链、分级掩码、审计埋点核对、CI/A1-A14 自动化）

### Phase 3：迭代完善与开源规范

- [x] v0.33.0：Python 工具治理层健壮性加固（SDK 并发安全、API 入口防御、错误脱敏、admin 权限隔离、配置校验 fail-closed、CI 分层）
- [x] v0.34.0：状态持久化 SQLite 化与 Harness 生产化（热更新、远程取消、幂等、资源隔离）
- [x] v0.35.0：A2A 交互治理层骨架（Go kernel、Agent Registry、Task Manager、Delegation Manager）
- [x] v0.36.0：A2A 自动发现、流式任务与 Runtime 委托集成
- [x] v0.37.0：单实例可靠 A2A 闭环（OpenAPI 权威协议、SQLite 状态层、真实委托执行）
- [x] v0.38.0：独立 Agent Interaction Governance（IIGE）平面
- [x] v0.39.0：交互治理协议与运行闭环收敛
- [x] v0.40.0：Agent 委托执行生命周期闭环（Task 状态机 CAS、SSE 重放、委派 token 参数绑定）
- [x] v0.41.0：分布式可靠性（outbox 原子领取、exec 租约故障转移）
- [x] v0.42.0：registry / router / discovery 持久化
- [x] v0.43.0–v0.45.0：Python 治理核心状态 SQLite 化（预算/预留/权限令牌/任务/告警/会话/对话）
- [x] v0.46.0：可信单跳 A2A 委托闭环（Bearer 身份绑定、独立工作负载凭证）
- [x] v0.47.0：可信递归委托、权限与预算衰减
- [x] v0.48.0：委托审批持久化与可靠恢复派发
- [x] v0.49.0：审批可靠性收敛与治理默认硬化（SQLite 工具审批主路径、双平面可靠 webhook、认证审批 principal、Checkpoint 显式降级）
- [x] v0.50.0：OPA Bundle 策略交付、加载确认、shadow 模拟与变更审计
- [x] v0.52.0：多租户、企业身份与 RBAC
- [x] v0.53.0：workload/delegated identity、Secret 引用、受保护出口、ExecutionReceipt、能力协商与静态部署 conformance
- [x] v0.54.0：可靠多 Agent 调度、durable assignment/lease/fence、retry/failover/dead-letter、有界静态 DAG、可靠 SSE、migration、readiness 与 metrics（支持环境门禁仍待执行）
- [x] v0.55.0：管理治理台全面接入真实接口（A2A 任务树、RBAC/Policy、
      死信队列重放、审批 SSE 推送）、管理台 A2A 代理端点、
      Go 内核控制面凭据装配、前端 CI 门禁；
      详见 [CHANGELOG.md](CHANGELOG.md)
- [ ] T3.5（可选）：LLMPlanner JSON Schema 契约实现
- [ ] 补充更多示例与文档
- [x] 建立完整 CI/CD、代码规范、贡献指南
- [x] 准备开源发布（CHANGELOG、贡献指南）

---

## 9. 关键证据

- **T1 测试**：LLM 判定型 Guardrail 对信息提取型注入的拦截率在 20%-60% 之间波动，且多次触发 API 速率限制；无 Guardrail 时 Agent 100% 泄露敏感信息。
- **T3 测试**：MCP 协议的 OAuth 2.1 授权为 OPTIONAL 且主要覆盖传输层，缺少工具级权限表达；Client Policy Gateway 是可行且必要的补充层。
- **竞对调研**：Zenity、Palo Alto/Protect AI、OPA 验证了 Runtime 强制和统一策略的必要性，但开源、可嵌入的"制度基础设施"仍是空白。

详见 [`reports/test_conclusion_report.md`](./reports/test_conclusion_report.md) 和 [`docs/research/03_runtime_governance_landscape.md`](./docs/research/03_runtime_governance_landscape.md)。

---

## 10. 贡献与联系

本项目已完成 MVP 并进入持续迭代阶段，欢迎任何形式的反馈、建议和贡献。

- 如果你对企业内控有经验，请帮助我们验证 R0-R3 映射模型的合理性；
- 如果你有 Agent 框架的开发经验，请帮助我们评估技术路线的可行性；
- 如果你只是对 "把 Agent 当人看" 这个理念感兴趣，也欢迎加入讨论。

参与方式见 [CONTRIBUTING.md](CONTRIBUTING.md)，版本变更见
[CHANGELOG.md](CHANGELOG.md)。

---

## 11. 许可证

Apache-2.0，详见 [LICENSE](./LICENSE)。
