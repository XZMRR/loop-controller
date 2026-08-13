# Loop Controller — 面向企业内控的 Agent 运行框架

> **项目阶段**：前期调研与架构设计已完成，进入核心抽象设计阶段  
> **当前目标**：用 R0-R3 治理模型定义 Loop Controller 的核心架构，准备 MVP 设计  
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
| **R3 Audit** | 内部审计/纪检监察 | 异步采集日志、有偏采样、评估有效性、反馈整改 |

因此，Loop Controller 不是另一个"审批工具"，而是一个 **Agent 组织的制度基础设施**。

---

## 4. 项目文档结构

```
docs/
├── research/           # 前期调研报告
│   ├── 01_internal_control_research.md   # 企业内控运作方式
│   ├── 02_agent_landscape_research.md    # Agent 产品与架构逻辑
│   ├── 03_runtime_governance_landscape.md  # 竞对调研：Zenity/Palo Alto/OPA
│   └── 内控最小岗位结构抽象_v0.1.md      # 企业内控最小岗位结构抽象
├── architecture/       # 架构设计文档
│   ├── overview.md     # 架构概览（R0-R3 模型）
│   ├── 00_r0r3_architecture.md  # R0-R3 分层详细设计
│   └── 05_mvp_core_abstractions.md  # MVP 核心抽象与接口设计
reports/               # 汇报与研究报告
├── project_feasibility_report.md      # 项目可行性论证
├── test_conclusion_report.md          # T1/T3 测试结论
├── test_methodology_appendix.md       # 测试方法论附录
├── agent_security_framework_brief.md  # Agent 安全框架现状
└── agent_security_test_plan.md        # 测试计划

tests/legacy/security_experiments/  # 现有 Agent 安全手段测试（已归档）
├── README.md
├── TEST_GUIDE.md
├── test_results.md
├── t1_openai_agents_guardrails/  # T1 三层 Guardrail 测试
└── t3_mcp_permission_boundary/   # T3 MCP 权限边界测试
```

---

## 5. 当前阶段与路线图

### Phase 0：前期调研（已完成）

- [x] 调研企业内控运作方式
- [x] 调研 Agent 产品与架构逻辑
- [x] 调研 Agent 安全与治理框架
- [x] 完成 Zenity/Palo Alto/OPA 竞对调研
- [x] 完成 T1 Guardrail 测试 + T3 MCP 权限边界测试
- [x] 输出 R0-R3 架构初稿

### Phase 1：核心抽象设计（进行中）

- [ ] 根据讨论反馈收敛 R0-R3 架构
- [ ] 设计核心抽象：Agent、CapabilityProfile、Task、Loop、Checkpoint、Policy、AuditRecord
- [ ] 输出核心 API 接口草案
- [ ] 绘制 Loop 控制器状态机图
- [ ] 确定 Policy 表达形式（Python / YAML / JSON / DSL）

### Phase 2：最小可行原型（MVP）

- [ ] 实现一个最小化的 Loop 运行时
- [ ] 集成至少一个 LLM Provider
- [ ] 支持基础的工具调用与权限控制
- [ ] 输出可审计的执行 Trace
- [ ] 目标场景："制度化的研究 Agent"

### Phase 3：迭代完善与开源规范

- [ ] 补充测试、文档、示例
- [ ] 建立 CI/CD、代码规范、贡献指南
- [ ] 准备开源发布（LICENSE、README、CHANGELOG）

---

## 6. 关键证据

- **T1 测试**：LLM 判定型 Guardrail 对信息提取型注入的拦截率在 20%-60% 之间波动，且多次触发 API 速率限制；无 Guardrail 时 Agent 100% 泄露敏感信息。
- **T3 测试**：MCP 协议的 OAuth 2.1 授权为 OPTIONAL 且主要覆盖传输层，缺少工具级权限表达；Client Policy Gateway 是可行且必要的补充层。
- **竞对调研**：Zenity、Palo Alto/Protect AI、OPA 验证了 Runtime 强制和统一策略的必要性，但开源、可嵌入的"制度基础设施"仍是空白。

详见 [`reports/test_conclusion_report.md`](./reports/test_conclusion_report.md) 和 [`docs/research/03_runtime_governance_landscape.md`](./docs/research/03_runtime_governance_landscape.md)。

---

## 7. 贡献与联系

本项目目前处于早期调研与架构设计阶段，欢迎任何形式的反馈、建议和贡献。

- 如果你对企业内控有经验，请帮助我们验证 R0-R3 映射模型的合理性；
- 如果你有 Agent 框架的开发经验，请帮助我们评估技术路线的可行性；
- 如果你只是对 "把 Agent 当人看" 这个理念感兴趣，也欢迎加入讨论。

---

## 8. 许可证

待定（建议选择 Apache-2.0 或 MIT，以符合开源社区规范）。
