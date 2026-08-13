# Loop Controller 项目可行性研究报告

> **文档定位**：论证 Loop Controller 作为“Agent 制度基础设施”的方向合理性与技术可行性，为开源项目立项提供参考。
>
> **报告日期**：2026-08-05  
> **状态**：精简版 v2.2（已修正 OWASP/MCP 事实表述，已补充 Zenity/Palo Alto/OPA 竞对深调与差异化分析）

---

## 一、摘要

### 1.1 项目一句话定位

Loop Controller 是一套**贴合实际企业管理模式的拟人化 Agent 管理系统**：把 Agent 当作数字员工，用岗位、权限、制度和审计来管理，而不是依赖模型的自我约束或无休止的人工确认。

### 1.2 核心判断

| 维度 | 结论 |
|------|------|
| **问题真实性** | ✅ 已验证。Agent 进入企业核心流程后，可控性、可审计性、合规性是核心瓶颈。 |
| **现有方案不足** | ✅ 已验证。Zenity、Palo Alto/Protect AI、OPA 等方案各有建树，但或偏向安全运营、或为通用引擎、或平台闭源，均未从企业内控视角提供开源、可嵌入的“制度表达层”。 |
| **技术可行性** | ✅ 可行。MCP Policy Gateway 已验证；声明式 Policy、Runtime 强制、审计机制均有成熟路径。 |
| **市场空间** | ✅ 存在。2025-2026 年 Agent 治理已成活跃赛道（Zenity 被 Gartner 评为 Company to Beat；Palo Alto 收购 Protect AI、Portkey 布局 AI Gateway），但开源 SDK 形态的制度基础设施仍是空白。 |
| **实施路径** | ✅ 清晰。从 Python SDK 起步，先验证单 Agent 治理，再逐步叠加多 Agent、服务化、企业集成。 |

---

## 二、问题背景

### 2.1 Agent 正在进入企业核心流程

2025-2026 年，AI Agent 从实验走向生产：

- Microsoft 365 Copilot、Salesforce Agentforce 等进入企业办公场景；
- OpenAI Operator、Anthropic Computer Use 等自主 Agent 开始执行实际任务；
- 企业内部大量自建 Agent 通过 MCP/A2A 协议连接工具和数据。

### 2.2 但企业治理严重滞后

Agent 与传统软件的根本区别在于：**它在运行时会根据环境自主决策**。这意味着：

- 无法像传统 API 一样用固定权限控制；
- 无法像传统应用一样预先审计所有执行路径；
- 不能把安全交给模型自律或用户逐个确认。

### 2.3 当前 Agent 安全方案的共同问题

| 问题 | 具体表现 | 后果 |
|------|---------|------|
| **重检测、轻设计** | 强调 Guardrail、异常检测、事后追溯 | 风险已发生，只能降低损失，不能预防 |
| **重审批、轻制度** | 频繁弹窗要求用户确认 | 用户疲惫、机制形式化、失去实际价值 |
| **重单点、轻体系** | 身份、沙箱、Guardrails、审计分散 | 缺少统一的“制度表达层”，管理成本高 |

### 2.4 现有竞对方案的结构性局限

2025-2026 年，Agent 治理赛道快速升温，已出现三类代表性方案，但它们与企业内控需求仍存在明显错位：

| 方案类型 | 代表 | 能力边界 | 与内控需求的差距 |
|----------|------|----------|------------------|
| **专用 Agent 治理平台** | Zenity | Runtime Boundaries、ACS Hooks、Shadow AI 发现、意图感知检测 | 平台闭源，作为安全产品采购，难以嵌入企业自有 Agent 流程；治理叙事强但产品形态偏向安全运营 |
| **综合 AI 安全平台** | Palo Alto / Protect AI (Prisma AIRS) | AI Gateway、Agent Registry、模型扫描、红队测试、运行时安全 | 把 Agent 治理纳入网络安全大盘，控制点偏向网关/流量，制度表达和审批闭环不是核心 |
| **通用策略引擎** | OPA (Open Policy Agent) | Rego 声明式策略、ABAC/RBAC、解耦决策与执行 | 只解决“策略决策”，不解决 Agent 发现、运行时 Hook、审计闭环、Human-in-the-Loop 等完整治理问题 |

**核心结论**：竞对方案验证了 Runtime 强制执行和统一策略层的市场必要性，但尚未出现一款**开源、可嵌入、以内控方法论为设计前提**的 Agent 治理基础设施。这正是 Loop Controller 的立项空间。

---

## 三、企业内控视角：需要什么

### 3.1 内控的本质不是“审计损失”，而是“预防损失”

良好的企业内控体系依靠：

1. **明确的岗位职责**：谁做什么、不能做什么；
2. **统一的制度标准**：一本组织统一遵守的《内控手册》；
3. **不相容职务分离**：执行、监督、评价由不同主体承担；
4. **默认保护机制**：在风险发生点前置控制；
5. **独立审计闭环**：发现问题、整改、验证、追责。

### 3.2 内控最小岗位结构抽象

基于 [docs/research/内控最小岗位结构抽象\_v0.1.md](../docs/research/内控最小岗位结构抽象_v0.1.md)，企业内控可抽象为四个不可删减的角色：

```
R0 治理者：定义风险偏好，批准制度，问责决策
    ↓
R1 风险责任人：在业务动作发生点执行控制
R2 标准与监督者：制定统一标准，监督和挑战 R1
R3 独立评价者：客观评价整体控制有效性
```

这四个角色对应企业内控的“三道防线 + 治理层”模型（IIA Three Lines Model 2020、COSO 五要素）。

### 3.3 把 Agent 当人看的含义

Loop Controller 的核心隐喻是：**把 Agent 当作数字员工来管理**。

一个数字员工应该有：

- **岗位说明书**：研究助理、客服、数据分析师还是代码助手？
- **权限范围**：能访问哪些文件、调用哪些工具、读写哪些系统？
- **行为边界**：能输出什么级别的信息？能否执行删除、发送、修改等高风险动作？
- **审批流程**：越权或高风险动作由谁批准？
- **审计记录**：它做了什么、为什么这么做、谁批准的？
- **整改闭环**：发现异常后如何调整它的“制度”？

---

## 四、Agent 现状：缺什么

### 4.1 结构对照分析

将企业内控四角色与现有 Agent 治理框架对比如下：

| 内控角色 | 现有 Agent 框架对应物 | 对应关系 | 关键缺口 |
|---------|---------------------|---------|---------|
| **R0 治理者** | 无对应角色 | 结构性缺失 | 风险偏好无定义、策略变更无审批、审计报告无接收方 |
| **R1 风险责任人** | System Prompt + 自我反思 | 部分对应但弱 | 仅靠提示词约束，无强制力，易被间接注入篡改 |
| **R2 标准与监督者** | Guardrails / Policy-as-Code / MCP Gateway | 部分对应但分散 | 缺少统一的制度表达层，标准散落各处 |
| **R3 独立评价者** | Trace / 可观测性 / 红队测试 | 存在但断链 | 发现问题后无制度化整改闭环，审计数据不独立 |

### 4.2 实测验证的关键缺口

通过 `tests/legacy/security_experiments/` 的测试，验证了上述缺口：

1. **R1 自控不可靠**：kimi-k2.5 在系统提示明确约束下仍会泄露邮箱、手机号、项目代号、账号密码（T1.4c）。
2. **R2 标准分散且不稳定**：输入/输出 Guardrail 对信息提取型攻击防御能力弱，同一份脚本不同时间跑出不同结果（T1.1、T1.2）。
3. **R3 审计断链**：现有 Trace 存储在 Agent 运行系统内，缺少独立的 Audit Store 和整改闭环。
4. **R0 整体缺席**：没有任何框架把“风险偏好”和“制度批准”作为运行时一等原语。

---

## 五、Loop Controller 方案

### 5.1 核心定位

> **Loop Controller = Agent 的“制度基础设施”**

我们不做新的 LLM 框架，不做另一个沙箱，也不做覆盖所有场景的通用 Agent 框架。我们要做的是：

- 一个**治理运行时层**；
- 把企业内控思想翻译成 Agent 运行时的原语；
- 让 Agent “默认做对”，只在真正需要判断的边界处找人。

换句话说，我们做的是**和实际企业管理模式贴合的拟人化 Agent 管理系统**。

### 5.2 四角色映射

| 内控角色 | Loop Controller 原语 | 说明 |
|---------|---------------------|------|
| **R0 治理者** | `GovernanceClient` / `HumanOwner` | 定义风险偏好、风险阈值，批准 Policy 版本，接收审计报告 |
| **R1 风险责任人** | `Agent` + `Task` + `CapabilityProfile` | Agent 的角色、能力边界、默认限制 |
| **R2 标准与监督者** | `PolicyEngine` + `Checkpoint` + `RiskAssessment` | 统一策略执行、风险自适应门控、对 Agent 行为进行挑战 |
| **R3 独立评价者** | `AuditStore` + `Trace` + `PolicyReview` | 独立审计存储、异常检测、整改闭环 |

### 5.3 关键设计原则

1. **制度优先于审批**：先写好 Policy，让 Agent 默认不能越界，而不是每次动作都确认；
2. **Runtime 强制优先于模型自律**：Policy 由 Loop Controller 强制执行，不依赖模型是否“听话”；
3. **统一策略表达**：一份 Policy 定义 Agent 的工具白名单、资源范围、输出限制、审批规则；
4. **审计驱动改进**：审计不仅是记录，更是发现制度缺陷并推动 Policy 迭代的输入；
5. **与现有生态兼容**：不取代 LangChain、OpenAI Agents SDK、MCP，而是作为治理层接入。

### 5.4 形态演进

| 阶段 | 形态 | 目标 |
|------|------|------|
| **Phase 0** | 调研 + 测试 | 验证问题真实性和现有方案边界 ✅ |
| **Phase 1** | Python SDK | 验证“制度化的研究 Agent”单 Agent 场景 |
| **Phase 2** | 增强 SDK | 多 Agent、Human-in-the-Loop、Policy 版本管理 |
| **Phase 3** | 可选服务化 | 企业集中管控、审计汇聚、跨任务风险分析 |

---

## 六、技术可行性

### 6.1 已验证的技术点

| 技术点 | 验证方式 | 结论 |
|--------|---------|------|
| MCP Client 侧 Policy Gateway | T3 测试 | 可行，可拦截越权工具调用 |
| 声明式工具/路径白名单 | T3 测试 | 可行，可用规则引擎快速判定 |
| 输出层敏感信息过滤 | T1.2 测试 | LLM-based 不可靠，需要确定性规则 |
| Agent 自律不可信 | T1.4c 测试 | 系统提示无法防御信息泄露 |
| 审计日志记录 | T3 测试 | 可行，每个工具调用都可记录 ALLOW/BLOCK |

### 6.2 待验证但路径清晰的技术点

| 技术点 | 技术路径 | 风险等级 |
|--------|---------|---------|
| 声明式 Policy DSL | 参考 OPA/Rego、AWS IAM Policy、Kubernetes RBAC | 低 |
| Loop Runtime 强制 | 参考 OpenAI Agents SDK Runner + 自定义策略钩子 | 低 |
| Human-in-the-Loop | Web/CLI 中断 + 审批回调 | 中 |
| 独立 Audit Store | SQLite/PostgreSQL + 不可篡改日志 | 低 |
| MCP 工具绑定 | MCP Client + Policy Gateway 封装 | 低 |
| 行为基线检测 | 基于 Trace 的统计异常检测 | 中 |

### 6.3 技术栈建议

- **语言**：Python（已确认）
- **核心运行时**：自研 Loop Controller Runtime
- **工具协议**：MCP（Model Context Protocol）。注意 MCP 规范的 OAuth 2.1 授权为 OPTIONAL 且主要覆盖传输层，工具级细粒度权限需由 Client Policy Gateway 补充；
- **Agent 间协议**：A2A（后续支持）
- **策略引擎**：参考 Rego/OPA 思想，初期可用 Python 规则引擎
- **审计存储**：SQLite/PostgreSQL
- **LLM 接入**：OpenAI 兼容 API / 多 Provider 适配

---

## 七、市场与竞争分析

### 7.1 市场需求

企业级 Agent 治理是 2025-2026 年的热点方向：

- Microsoft、Google、Salesforce 等巨头都在布局 Agent 平台，但治理层仍较薄弱；
- OWASP 于 2025 年 12 月 9 日发布 Agentic AI Top 10（2026 版，ASI01–ASI10），其中 ASI03（身份与权限滥用）和 ASI10（Rogue Agents，即在 Policy 之外运行）与 Loop Controller 定位直接相关；
- 企业客户真正愿意买单的是“可控、可审计、可合规”。

### 7.2 现有竞争格局

#### 7.2.1 Agent 治理核心竞对

| 竞对 | 产品形态 | 核心能力 | 与 Loop Controller 的差异 |
|------|----------|----------|---------------------------|
| **Zenity** | 专用 Agent 治理平台（闭源） | Runtime Boundaries、ACS Hooks、Shadow AI 发现、意图感知检测 | Zenity 思路最接近 Loop Controller，但它是闭源安全平台，面向 CISO 采购；Loop Controller 是开源 SDK，面向开发者和架构师嵌入自有 Agent 流程 |
| **Palo Alto / Protect AI** | 综合 AI 安全平台（Prisma AIRS） | AI Gateway、Agent Registry、模型扫描、红队测试、AI Runtime Security | Palo Alto 把 Agent 治理纳入网络安全运营体系，控制点在网络/网关层；Loop Controller 聚焦 Agent 内部循环（Loop）的制度层，强调岗位、权限、审批、审计闭环 |
| **OPA** | 通用策略引擎（开源） | Rego 声明式策略、ABAC/RBAC、策略决策与执行解耦 | OPA 只提供策略决策能力，不解决 Agent 发现、运行时 Hook、审计闭环、Human-in-the-Loop；Loop Controller 可借鉴其策略引擎思想，但提供面向 Agent 治理的完整运行时层 |

#### 7.2.2 周边生态关系

| 方向 | 代表 | 与 Loop Controller 的关系 |
|------|------|---------------------------|
| Agent 框架 | OpenAI Agents SDK、LangGraph、CrewAI | **被集成**，Loop Controller 在其之上做治理 |
| Guardrail 产品 | Galileo、Promptfoo | **补充**，Loop Controller 可调用其检测能力，但提供统一策略层 |
| MCP Gateway | TrueFoundry、Reco.ai | **类似但不同**，它们聚焦工具网关，Loop Controller 聚焦全生命周期治理 |
| 可观测性 | Langfuse、Arize、Phoenix | **补充**，Loop Controller 可输出标准化 Trace |
| 安全智能体 | Microsoft Security Copilot | **不同赛道**，它们用 AI 做安全，我们保护 AI |

### 7.3 Loop Controller 的差异化

#### 7.3.1 与 Zenity 的差异化：开源嵌入 vs. 闭源平台

- **Zenity** 是企业采购的安全平台，强调跨平台发现、统一策略、Runtime Boundaries；
- **Loop Controller** 是开源 SDK，强调嵌入企业自有 Agent 代码、与开发流程结合、从制度设计出发；
- **差异价值**：对于自建 Agent 的企业和开发者，Loop Controller 提供可定制、可审计、可版本控制的治理层，而不需要切换到一个外部安全平台。

#### 7.3.2 与 Palo Alto / Protect AI 的差异化：制度层 vs. 安全运营层

- **Palo Alto** 的优势在于网络、身份、网关、SOC 集成，AI Gateway 是其核心控制点；
- **Loop Controller** 的优势在于 Agent 内部执行循环的精细控制：岗位说明书、CapabilityProfile、Checkpoint、RiskAssessment、Human-in-the-Loop；
- **差异价值**：网关层无法完全感知 Agent 的意图和任务上下文，Loop Controller 在 Agent 运行时内部建立制度层，与网关层互补而非替代。

#### 7.3.3 与 OPA 的差异化：完整治理运行时 vs. 策略决策引擎

- **OPA** 提供的是通用授权决策能力，开发者需要自行解决 Hook 调用、数据同步、审计、整改闭环；
- **Loop Controller** 面向 Agent 场景提供完整治理运行时：Agent 角色定义、Task 上下文、Policy 版本、Audit Store、Policy Review；
- **差异价值**：Loop Controller 不是做一个 Agent 版的 OPA，而是把 OPA 的策略思想嵌入到 Agent 内控流程中。

#### 7.3.4 核心差异化总结

| 差异维度 | Loop Controller | 竞对典型做法 |
|----------|-----------------|--------------|
| **产品形态** | 开源 Python SDK | 闭源平台或通用引擎 |
| **设计出发点** | 企业内控（岗位、权限、制度、审计） | 安全运营或通用授权 |
| **控制粒度** | Agent 内部 Loop 的每个阶段 | 网关/流量层或单一策略决策点 |
| **治理闭环** | Policy 定义 → Runtime 强制 → Audit → Policy Review | 多为检测-响应或决策-执行 |
| **嵌入方式** | 作为库嵌入企业自有 Agent | 作为外部平台或 sidecar |

**一句话差异化**：Loop Controller 不是又一个 Agent 安全平台，也不是 OPA 的 Agent 封装；它是一套**开源的、可嵌入的、以内控方法论为设计前提的 Agent 制度基础设施**。

---

## 八、风险与挑战

### 8.1 技术风险

| 风险 | 说明 | 应对 |
|------|------|------|
| LLM 输出不可控 | Agent 行为本质上是概率性的 | 不依赖模型自律，用 Runtime 强制兜底 |
| 策略引擎复杂度高 | Policy 既要表达力强，又要可解释 | 从简单规则开始，逐步引入 DSL |
| 多协议兼容性 | MCP、A2A、Function Calling 并存 | 先专注 MCP，其他协议后续适配 |
| 性能开销 | 每次工具调用都走 Policy Gateway | 异步 + 缓存 + 分层校验 |

### 8.2 市场风险

| 风险 | 说明 | 应对 |
|------|------|------|
| 巨头与先发者挤压 | Zenity 已被 Gartner 评为 Company to Beat 且主导 ACS 开源标准；Palo Alto 通过收购 Protect AI、Portkey 快速整合 AI Gateway；Microsoft/Google 也可能推出平台级治理层 | 坚持开源 SDK 差异化，不跟闭源平台拼覆盖面；聚焦“制度层”叙事和开发者社区；适时兼容 ACS Hooks 等生态标准 |
| 开源商业化难 | 纯 SDK 变现周期长 | 先验证场景，再考虑服务化 |
| 用户认知不足 | “制度基础设施”概念较新 | 用测试报告和 Demo 教育市场 |

### 8.3 执行风险

| 风险 | 说明 | 应对 |
|------|------|------|
| 范围蔓延 | 想做太多功能 | 坚持“先设计大架构，再做减法”，从最小场景开始 |
| 过度工程 | 一开始就服务化 | 先做 SDK，验证后再演进 |
| 缺少用户反馈 | 闭门造车 | 尽早开源、写文档、做 Demo |

---

## 九、实施路径建议

### 9.1 第一阶段：核心抽象设计（1-2 周）

输出：

- Policy 模型设计文档
- Task 对象设计文档
- Loop Runtime 接口草案
- 与 MCP 集成的设计文档

### 9.2 第二阶段：MVP SDK（3-4 周）

目标场景：**制度化的研究 Agent**

功能：

- Agent 角色定义（研究助理）；
- Policy 定义（可读文件、可搜索、不可写、不可发送邮件）；
- MCP 工具绑定与权限控制；
- 输出层敏感信息过滤；
- 审计日志记录。

### 9.3 第三阶段：增强治理（后续）

- Human-in-the-Loop 审批；
- Policy 版本管理；
- 多 Agent 协作；
- A2A 协议支持；
- 独立 Audit Store；
- 异常检测与整改闭环。

---

## 十、结论

Loop Controller 方向是**可行且有价值的**。

**可行性依据**：

1. 问题真实存在，企业内控有成熟的方法论可借鉴；
2. 现有 Agent 治理方案存在结构性缺口，有差异化空间；
3. 关键技术点已有可行路径，部分已在测试中验证；
4. 市场热点明确，开源 SDK 形态适合作为切入点。

**建议决策**：

- 继续推进项目；
- 优先完成核心抽象设计；
- 用“制度化的研究 Agent”作为 MVP 场景；
- 保持开源、文档优先、社区驱动的节奏。

---

## 参考文档

- [reports/test\_conclusion\_report.md](./test_conclusion_report.md)
- [reports/test\_methodology\_appendix.md](./test_methodology_appendix.md)
- [docs/research/内控最小岗位结构抽象\_v0.1.md](../docs/research/内控最小岗位结构抽象_v0.1.md)
- [docs/research/01\_internal\_control\_research.md](../docs/research/01_internal_control_research.md)
- [docs/research/02\_agent\_landscape\_research.md](../docs/research/02_agent_landscape_research.md)
- [docs/research/03\_runtime\_governance\_landscape.md](../docs/research/03_runtime_governance_landscape.md)
- [reports/agent\_security\_framework\_brief.md](./agent_security_framework_brief.md)
