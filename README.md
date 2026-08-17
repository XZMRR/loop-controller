# Loop Controller — 面向企业内控的 Agent 运行框架

> **项目阶段**：MVP 已完成（迭代 1/2/3 结束），R0-R3 四层治理模型可运行
> **首选语言**：Python（Agent 生态最丰富，社区传播友好）
> **文档语言**：中文为主，代码与核心 API 文档以英文为主，便于国际化开源

---

## 1. 项目愿景

**Loop Controller** 是一个开源的 Agent 运行框架，其设计灵感来自企业内控（Internal Control）部门的运作方式。我们相信：

> 当 Agent 被赋予越来越多的自主权和工具访问能力时，它不应该被当作一个无限制调用的函数，而应该被当作一名需要被 **聘用、授权、监督、审计** 的数字员工。

更进一步，我们相信：

> **真正有效的治理，不是通过无意义的逐个确认来“审计损失”，而是通过清晰的制度、良好的运行环境和无处不在的保护机制，让 Agent “自然而然地做对”。**

Loop Controller 的核心使命是：

- 为 Agent 组织提供一套 **数字化的“规章制度”基础设施**；
- 将企业内控中的 "三道防线"、COSO 五要素、风险评估、控制活动、监督闭环等思想，转化为 Agent 框架中的 **一等设计原语**；
- 在保持 Agent 自主性的同时，通过 **制度设计、环境塑造和智能保护**，降低风险发生的概率，而不是只在事后追责。

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
├── loop_controller/        # MVP 源码
├── Loop_Controller_MVP方案_纯工具调用_v1.1.md   # 当前权威实现依据
├── Loop_Controller_MVP开发指南_v1.0.md
├── development_log.md                          # 迭代开发记录
├── KNOWN_LIMITATIONS.md                        # MVP 明确声明的能力边界
└── 发布检查清单_v0.1.0.md                      # 发布前手动 gate 与回归清单
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
tests/legacy/security_experiments/  # 早期实验（已归档，pytest 忽略）
```

---

## 5. 快速开始

### 环境要求

- Python >= 3.12
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
Invoke-WebRequest -Uri "https://openpolicyagent.org/downloads/v1.0.1/opa_windows_amd64.exe" -OutFile "tools\opa.exe"

# Linux/macOS 参见 .github/workflows/ci.yml
```

### 运行全部测试

```powershell
$env:PYTHONPATH="src"
.venv\Scripts\python.exe -m pytest tests/ -q
```

当前已通过 **110+ 个测试**，覆盖：
- 配置加载与 7 条启动校验
- R1 `RuleBasedClassifier` 风险分类
- R2 `Checkpoint` 判定流水线、审批、权限组合、预算、调用次数上限
- R0-delegate 同步/异步审批打桩
- R3 哈希链、分级掩码、审计埋点
- OPA/Rego fail-closed 策略
- 端到端 approve/deny 路径

### 运行端到端示例

```powershell
$env:PYTHONPATH="src"
.venv\Scripts\python.exe examples/research_agent_example.py
```

示例会模拟一个完整的研究助手任务：
1. 读取内部合规检查清单；
2. 搜索公开资料（使用本地 mock，断网可运行）；
3. 写入本地摘要文件；
4. 发送邮件给张经理。

每个动作都会经过 **R1 风险分类 → R2 策略判定 → R2 代理转发执行 → R3 审计日志** 的完整闭环。

---

## 6. 验收清单（A1-A14）

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

## 7. 当前阶段与路线图

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

- [ ] T3.5（可选）：LLMPlanner JSON Schema 契约实现
- [ ] 补充更多示例与文档
- [ ] 建立完整 CI/CD、代码规范、贡献指南
- [ ] 准备开源发布（CHANGELOG、贡献指南）

---

## 8. 关键证据

- **T1 测试**：LLM 判定型 Guardrail 对信息提取型注入的拦截率在 20%-60% 之间波动，且多次触发 API 速率限制；无 Guardrail 时 Agent 100% 泄露敏感信息。
- **T3 测试**：MCP 协议的 OAuth 2.1 授权为 OPTIONAL 且主要覆盖传输层，缺少工具级权限表达；Client Policy Gateway 是可行且必要的补充层。
- **竞对调研**：Zenity、Palo Alto/Protect AI、OPA 验证了 Runtime 强制和统一策略的必要性，但开源、可嵌入的"制度基础设施"仍是空白。

详见 [`reports/test_conclusion_report.md`](./reports/test_conclusion_report.md) 和 [`docs/research/03_runtime_governance_landscape.md`](./docs/research/03_runtime_governance_landscape.md)。

---

## 9. 贡献与联系

本项目目前进入 MVP 实现阶段，欢迎任何形式的反馈、建议和贡献。

- 如果你对企业内控有经验，请帮助我们验证 R0-R3 映射模型的合理性；
- 如果你有 Agent 框架的开发经验，请帮助我们评估技术路线的可行性；
- 如果你只是对 "把 Agent 当人看" 这个理念感兴趣，也欢迎加入讨论。

---

## 10. 许可证

Apache-2.0，详见 [LICENSE](./LICENSE)。
