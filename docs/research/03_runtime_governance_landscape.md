# Agent 运行时治理竞对调研报告

> **文档定位**：为开源 Agent 框架 "Loop Controller" 提供主流 Agent 运行时治理方案的竞争格局输入。本文回答的问题：Zenity、Protect AI（Palo Alto Networks）和 OPA 三家在 Agent 治理层面各是什么定位？它们的控制点、策略执行方式、企业治理对齐度有何差异？Loop Controller 可以填补哪些空白？
>
> **调研日期**：2026-08-06\
> **作者**：祝鸣\
> **状态**：初稿（v0.1），将持续迭代更新。

***

## 1. 调研范围与核心问题

### 1.1 为什么选这三家

| 厂商                                  | 代表性           | 选择原因                                                                         |
| ----------------------------------- | ------------- | ---------------------------------------------------------------------------- |
| **Zenity**                          | 专用 Agent 治理平台 | Gartner 2026 年 4 月将其评为 "AI Agent Governance 领域 Company to Beat"，是赛道上最直接的参照对象 |
| **Protect AI / Palo Alto Networks** | 综合 AI 安全平台    | 被 Palo Alto 收购后整合进 Prisma AIRS，代表传统安全巨头进入 Agent 治理赛道后的产品化思路                  |
| **Open Policy Agent (OPA)**         | 通用策略引擎        | CNCF 毕业项目，是 "Policy-as-Code" 范式的代表，常被用作 Agent 授权层的基础设施                       |

### 1.2 分析维度

本次调研聚焦以下五个维度：

1. **产品定位**：是专用 Agent 治理平台、综合安全平台，还是通用策略引擎？
2. **控制点分布**：控制发生在部署前、运行时、还是事后响应？覆盖哪些执行环节？
3. **策略执行机制**：用什么语言/模型定义策略？如何强制执行？是否依赖 LLM 判定？
4. **企业治理对齐**：是否支持制度表达、审计闭环、风险责任人分离等内控需求？
5. **与 Loop Controller 的关系**：是替代、互补，还是可借鉴？

***

## 2. 三家厂商概述

### 2.1 Zenity：专用 Agent 治理平台

Zenity 是一家专注于 AI Agent 安全与治理的厂商，其产品架构完全围绕 Agent 生命周期设计。2026 年 4 月，Gartner 在《AI Vendor Race: Zenity Is the Company to Beat in AI Agent Governance》中将其评为该领域 "Company to Beat"。

#### 核心能力

- **AI Observability**：自动发现并持续盘点企业内所有 Agent，覆盖 SaaS 托管平台（Copilot Studio、Agentforce、Bedrock、Azure AI Foundry）、自建 Agent 和端侧 Agent，识别 Shadow AI。
- **AI Security Posture Management**：在部署前对 Agent 配置、权限、工具访问、Memory 等进行主动策略执行，降低运行前暴露面。
- **AI Detection & Response**：通过 "Clarity Agent" 实时监测工具调用、Memory 访问和数据使用模式，判断 Agent 行为是否与其预期目的一致。
- **Runtime Boundaries（2026-07 发布）**：在 Agent 做出决策但尚未转化为企业动作之前进行实时评估，根据意图、身份、请求动作、访问数据、工具、历史行为和企业策略决定允许、阻止或终止。
- **Agent Control Standard (ACS)**：2026 年 5 月由 Zenity 发起并开源（MIT 协议），定义了一套与厂商无关的运行时中间件 Hooks，覆盖输入接收、工具调用、规划到执行转换、Memory 存储、代码执行、子 Agent 调用等控制点。

#### 治理哲学

> "Govern AI decisions before they become enterprise actions."
>
> —— Zenity Runtime Boundaries 产品定位

Zenity 的核心思路是：把控制点前移到 "决策点"，而不是等动作发生后再告警或追溯。

***

### 2.2 Protect AI / Palo Alto Networks Prisma AIRS：综合 AI 安全平台

Protect AI 于 2025 年被 Palo Alto Networks 收购，其能力被整合进 **Prisma AIRS**（AI Runtime Security）平台。Palo Alto 将其定位为 "业界首个覆盖完整 Agentic AI 生命周期的平台"。

#### 核心能力

- **AI Agent Security**：实时防御提示注入、工具滥用、恶意 Agent 行为；发现并盘点企业内所有 Agent（包括未授权的 Shadow AI）。
- **AI Red Teaming**：以自主 Agent 方式持续对企业 AI 系统进行红队测试，声称覆盖 500+ 种专门攻击。
- **AI Model Security**：对模型本身进行深度架构分析，检测后门、数据投毒、恶意代码和依赖风险。
- **AI Runtime Security**：监控 AI 行为，执行实时防护，防止操纵、数据暴露和不安全动作。
- **AI Posture Management**：对训练/推理数据、Agent/应用完整性、模型访问权限进行可见性管理。
- **AI Gateway（2026-05 收购 Portkey 后整合）**：作为统一控制平面，集中识别、认证、授权每一次 Agentic 交互，提供 Agent Registry、语义路由、缓存、统一 LLM API 等运营能力。
- **Agent Identity Security**：通过 Idira（原 CyberArk）为 Agent 提供身份认证和最小权限控制。

#### 治理哲学

> "A unified vantage point to secure and govern AI agents at scale, identifying, authenticating and authorizing every agentic interaction in real time."
>
> —— Palo Alto Networks Prisma AIRS AI Gateway 定位

Palo Alto 的核心思路是：以传统网络安全厂商的集成优势，把 Agent 治理纳入已有的安全运营体系，通过 AI Gateway 实现集中控制。

***

### 2.3 Open Policy Agent (OPA)：通用策略引擎

OPA 是 CNCF 毕业项目，由 Styra 于 2016 年发起，定位为通用授权策略引擎。它不是专门为 Agent 设计的，但在 Agent 治理讨论中频繁出现，常被用于工具调用层的授权决策。

#### 核心能力

- **Policy-as-Code**：使用 Rego 语言编写声明式策略，将策略决策与策略执行解耦。
- **通用决策模型**：`Input (JSON) + Policy (Rego) + Data (JSON) = Decision (allow/deny)`。
- **ABAC/RBAC 支持**：可基于角色、属性、上下文进行细粒度授权。
- **多场景复用**：同一引擎可用于 Kubernetes Admission、API 授权、Terraform 合规、Kafka 访问控制等。
- **低延迟决策**：设计目标为毫秒级策略决策，适合嵌入请求链路。

#### 治理哲学

OPA 本身不解决 Agent 发现、审计闭环、制度审批等完整治理问题。它是一个可被 Agent Runtime 调用的 "策略决策点"，需要开发者自行设计 surrounding architecture（数据同步、审计、Hooks 集成等）。

***

## 3. 关键维度对比

| 维度                       | Zenity                                           | Palo Alto / Protect AI                | OPA               |
| ------------------------ | ------------------------------------------------ | ------------------------------------- | ----------------- |
| **产品形态**                 | 专用 Agent 治理平台                                    | 综合 AI 安全平台（含 Agent 治理模块）              | 通用策略引擎            |
| **主要控制点**                | 部署前姿态管理 + 运行时决策点（Runtime Boundaries / ACS Hooks） | 全生命周期 + 网络/网关层（AI Gateway） + 运行时安全    | 运行时授权决策点（需自行集成）   |
| **策略定义方式**               | 平台内置策略 + 意图感知 + 行为基线                             | AI Gateway 统一策略 + 传统安全规则 + 模型扫描       | Rego 声明式策略        |
| **运行时执行方式**              | 中间件 Hooks 返回 allow/deny/modify                   | AI Gateway 集中拦截 + Runtime Security 检测 | 被调用后返回 allow/deny |
| **Agent 发现能力**           | 强（Shadow AI Discovery）                           | 强（Shadow AI + 模型/应用发现）                | 无                 |
| **审计与闭环**                | 强调执行上下文和事后调查（DFIR + Guardian Agent 优化策略）         | 集成 Palo Alto 安全运营体系，强调事件响应            | 仅提供决策结果，审计需外部实现   |
| **企业内控对齐**               | 较强（制度层、三道防线概念明确）                                 | 中等（偏向安全运营和合规）                         | 弱（纯技术引擎，不含治理流程）   |
| **开源性**                  | ACS 标准开源（MIT），平台闭源                               | 闭源商业平台                                | 完全开源（CNCF 毕业）     |
| **与 Loop Controller 关系** | **直接竞对/参照**                                      | **部分重叠 + 可被集成**                       | **可借鉴/可集成**       |

***

## 4. 详细差异分析

### 4.1 产品定位差异：专用平台 vs. 综合平台 vs. 通用引擎

| 类型                               | 优势                                             | 局限                                              |
| -------------------------------- | ---------------------------------------------- | ----------------------------------------------- |
| **Zenity（专用平台）**                 | 对 Agent 治理问题理解深，控制点贴近 Agent 生命周期，有明确的 "制度层" 叙事 | 需要企业额外采购，与既有安全栈集成成本存在                           |
| **Palo Alto / Protect AI（综合平台）** | 可利用现有客户基础、安全运营体系、网络/身份能力，一站式覆盖广                | Agent 治理可能被视为安全平台的模块之一，制度层表达未必是核心               |
| **OPA（通用引擎）**                    | 开源、灵活、被广泛验证，可作为 Agent Runtime 的策略决策层           | 不提供 Agent 发现、审计闭环、Human-in-the-Loop、审批流程等完整治理能力 |

### 4.2 运行时执行差异

#### Zenity：决策点控制

Zenity 的 Runtime Boundaries 和 ACS 强调在 Agent 执行工作流的多个关键节点插入 Hooks：

- 输入接收
- 工具调用发起
- 规划到执行的转换
- Memory 存储
- 代码执行
- 子 Agent 调用

每个 Hook 都可以返回 `allow / deny / modify`，策略执行内联在动作发生前完成。这种方式与我们 Loop Controller 的 "Checkpoint" 概念高度相似。

#### Palo Alto：网关集中控制

Palo Alto 通过 AI Gateway 作为所有 Agent 流量的统一入口：

- 统一 LLM API 接入
- Agent Registry
- 实时识别、认证、授权
- 与 Idira（CyberArk）集成做 Agent 身份安全

其控制更偏向 "网络边界 + 网关拦截" 模式，对 Agent 内部循环（Loop）中各阶段的细粒度控制不如 Zenity  explicit。

#### OPA：策略决策点

OPA 本身不直接拦截 Agent 行为，而是作为被调用的策略引擎：

```
Agent Runtime → 调用 OPA → 返回 allow/deny → Runtime 决定是否执行
```

这种方式需要开发者自己设计 Hooks、数据同步、审计记录等。OPA 解决 "如何做出策略决策"，但不解决 "何时调用策略"、"如何强制"、"如何整改"。

### 4.3 策略模型差异

| 厂商            | 策略模型                      | 特点                                 |
| ------------- | ------------------------- | ---------------------------------- |
| **Zenity**    | 意图感知 + 行为基线 + 企业策略        | 强调 "Agent 应该做什么" 与 "它实际在做什么" 的偏差检测 |
| **Palo Alto** | 统一网关策略 + 安全规则 + 模型/应用风险评分 | 强调流量层面的统一策略和威胁检测                   |
| **OPA**       | Rego 声明式策略                | 强调可审计、可版本化、可测试的策略即代码               |

### 4.4 企业治理对齐差异

这是 Loop Controller 最应关注的差异点。

| 内控需求        | Zenity                       | Palo Alto                    | OPA                    |
| ----------- | ---------------------------- | ---------------------------- | ---------------------- |
| **岗位/角色定义** | 支持 Agent 角色、能力边界定义           | 支持 Agent Registry 和 Identity | 需自行建模                  |
| **统一制度手册**  | 有统一策略层叙事                     | 策略分散在安全平台各模块                 | Rego 文件可视为制度表达，但缺少治理流程 |
| **不相容职务分离** | Guardian Agent / R2 挑战者角色有体现 | 较弱                           | 无                      |
| **默认保护机制**  | Runtime Boundaries 前置控制      | AI Gateway 前置拦截              | 依赖调用方实现                |
| **独立审计闭环**  | DFIR + 策略优化闭环                | 集成 SOC/SIEM                  | 仅决策日志                  |
| **风险责任人问责** | 较强调 "Governance" 叙事          | 偏向安全事件响应                     | 无                      |

**关键观察**：

- Zenity 已经开始用企业治理语言包装产品（Governance、Runtime Boundaries、Guardian Agent），是 Loop Controller 最直接的竞对。
- Palo Alto 更偏向 "安全运营" 视角，Agent 治理是其 AI 安全平台的一部分。
- OPA 是技术基础设施，不具备完整治理语义，但 Loop Controller 可借鉴其策略引擎设计。

***

## 5. 对 Loop Controller 的启示

### 5.1 市场空间验证

三家厂商的共同方向说明：

1. **Agent 治理是一个真实且活跃的市场**，2025-2026 年头部安全厂商和创业公司都在加码。
2. **Runtime 强制执行是共识**，无论是 Zenity 的 Runtime Boundaries、Palo Alto 的 AI Gateway，还是 OPA 在工具调用层的决策，都说明 "控制点前移" 是行业趋势。
3. **"制度层" 叙事仍有差异化空间**：Zenity 虽然提出了 Governance 概念，但其产品本质是安全平台；Palo Alto 更偏向安全运营；OPA 是纯引擎。从企业内控视角出发的 "拟人化 Agent 管理制度基础设施" 仍是一个未被充分表达的空白。

### 5.2 Loop Controller 的差异化机会

| 差异点                         | 说明                                                |
| --------------------------- | ------------------------------------------------- |
| **内控方法论驱动**                 | 把 COSO、三道防线、岗位说明书等成熟企业管理概念翻译成 Agent Runtime 原语    |
| **开源 SDK 起步**               | 与 Zenity/Palo Alto 的闭源平台形成生态位差异，先占开发者社区           |
| **Client 侧 Policy Gateway** | 专注 MCP Client 侧的统一策略层，不替代 Server enforcement，形成互补 |
| **审计驱动改进**                  | 不仅记录日志，更要让审计结果回流到 Policy 迭代，形成制度闭环                |
| **轻量化可嵌入**                  | 不追求覆盖所有平台，先从 Python SDK 验证单 Agent 场景              |

### 5.3 需要持续关注的风险

1. **Zenity 的 ACS 开源标准可能快速占领生态位**：如果 ACS 成为事实标准，Loop Controller 需要考虑兼容或参与其 Hook 规范。
2. **Palo Alto 的网关集成优势明显**：对于已有 Palo Alto 安全栈的企业，Loop Controller 需要证明其在 "制度层" 的独特价值。
3. **OPA 是最可能被复用的基础设施**：Loop Controller 的策略引擎可以借鉴 Rego 思想，但应避免直接复刻，而是聚焦 Agent 治理场景的需求。

***

## 6. 结论

Zenity、Palo Alto/Protect AI 和 OPA 分别代表了 Agent 治理赛道的三种典型路线：

- **Zenity** 是 "专用 Agent 治理平台" 的代表，最接近 Loop Controller 想解决的问题，但它是闭源商业产品。
- **Palo Alto/Protect AI** 是 "综合 AI 安全平台" 的代表，把 Agent 治理纳入传统安全运营体系，控制点偏向网关和流量。
- **OPA** 是 "通用策略引擎" 的代表，提供了策略即代码的基础设施，但缺少完整治理语义。

Loop Controller 的机会在于：以 **开源 SDK + 企业内控方法论** 切入，填补 "制度表达层" 的空白，而不是与 Zenity 或 Palo Alto 在封闭平台上正面竞争，也不是简单做一个 OPA 的 Agent 封装。

***

## 参考来源

- [Gartner Names Zenity as the Company to Beat in AI Agent Governance](https://zenity.io/recognition)
- [Agent Control Standard Launches Open Framework for AI Agent Governance](https://ittech-pulse.com/news/agent-control-standard-launches-open-framework-for-ai-agent-governance/)
- [Zenity advances AI governance with Runtime Boundaries](https://www.helpnetsecurity.com/2026/07/27/zenity-exposure-management-runtime-boundaries/)
- [The Hard(er) Challenge in Agent Governance Is Authorization - Futurum Group](https://futurumgroup.com/wp-content/uploads/2026/06/The-Harder-Challenge-in-Agent-Governance-is-Authorization_AIOFM202606.pdf)
- [Palo Alto Networks Secures the AI Agent Revolution with the Launch of Prisma AIRS 2.0](https://www.prnewswire.com/news-releases/palo-alto-networks-secures-the-ai-agent-revolution-with-the-launch-of-prisma-airs-2-0--302596820.html)
- [Securing and Governing AI Agents At Scale Through A Unified AI Gateway](https://www.paloaltonetworks.co.uk/blog/2026/05/securing-and-governing-ai-agents-at-scale-through-a-unified-ai-gateway/)
- [Prisma AIRS AI Runtime Security Documentation](https://docs.paloaltonetworks.com/ai-runtime-security)
- [Why Open Policy Agent is the Missing Guardrail for Your AI Agents](https://codilime.com/blog/why-use-open-policy-agent-for-your-ai-agents/)
- [Open Policy Agent (OPA) Official Documentation](https://www.openpolicyagent.org/docs/latest/)

