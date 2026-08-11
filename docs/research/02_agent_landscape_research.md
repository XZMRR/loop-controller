# Agent 产品与架构逻辑调研报告

> **文档定位**：为开源 Agent 框架 "Loop 控制器" 提供主流 Agent 技术路线、产品形态和架构逻辑的输入。本文回答的问题：当前 Agent 框架和产品如何设计？它们的核心抽象、循环控制、协作模式、安全治理方式是什么？
>
> **调研日期**：2026-08-03  
> **作者**：项目初始团队  
> **状态**：初稿（v0.1），将持续迭代更新。

---

## 1. Agent 的核心抽象

### 1.1 什么是 Agent

Agent（智能体）是指能够 **感知环境、进行推理、规划并执行动作** 以达成目标的系统。与传统单次 LLM 调用不同，Agent 的核心特征是：

- **自主性**：能够自主决定下一步动作；
- **循环性**：通过多次迭代（Loop）逐步完成任务；
- **工具使用**：能够调用外部工具（搜索、代码执行、API、数据库等）；
- **记忆能力**：能够维护短期上下文和长期知识；
- **可观测性**：执行过程可被追踪、审计和干预。

### 1.2 Agent 框架的四大核心组件

几乎所有 Agent 框架都围绕以下四个能力展开：

| 组件 | 作用 | 设计要点 |
|------|------|----------|
| **规划（Planning）** | 将复杂任务拆解为可执行的子任务 | 任务分解、依赖排序、反思修正、动态重规划 |
| **记忆（Memory）** | 管理上下文和历史信息 | 短期记忆（对话上下文）、长期记忆（向量数据库、RAG） |
| **工具（Tools）** | 连接外部系统 | 工具定义、调用协议、Schema 约束、权限控制 |
| **执行（Action）** | 协调规划、记忆、工具的协同运作 | 循环驱动、异常处理、结果反馈、终止条件 |

> 参考：[CSDN: AI Agent 框架对比区别](https://blog.csdn.net/sinat_20277079/article/details/154843997)

---

## 2. Agent 的循环控制架构

### 2.1 经典循环：ReAct

ReAct（Reasoning + Acting）是当前最主流的 Agent 循环范式，由 Yao et al. 于 2022 年提出。其核心思想是：模型在每次动作前先输出 **思考（Thought）**，再输出 **动作（Action）**，然后观察结果（Observation），循环往复。

```
用户输入 → 思考（Thought）→ 动作（Action）→ 观察（Observation）→ 思考 → ... → 终止/输出
```

### 2.2 生产级循环的四个阶段

生产级 Agent 工作流通常被抽象为四个阶段：

```
规划（Plan） → 执行（Execute） → 观察（Observe） → 反思（Reflect）
        ↑__________________________________________↓
```

| 阶段 | 职责 |
|------|------|
| **规划** | 任务分解、工具选择、依赖关系确定 |
| **执行** | 调用工具、执行操作、生成中间结果 |
| **观察** | 解析工具返回、评估结果质量、标记异常 |
| **反思** | 判断是否满足目标、决定重试/拆分/放弃/继续 |

### 2.3 循环控制的关键工程问题

从 "Demo 级 Agent" 到 "生产级 Agent"，最大的区别在于循环控制的健壮性：

| 问题 | 说明 | 应对策略 |
|------|------|----------|
| **循环永不终止** | 模型持续请求更多工具调用 | 硬编码最大迭代次数、预算上限 |
| **循环震荡** | 模型在两个动作之间反复横跳 | 检测重复动作、状态哈希、 stagnation 检测 |
| **上下文爆炸** | 多轮后上下文超出窗口，模型 "迷失" | 上下文压缩、摘要、Checkpoint、子 Agent |
| **错误累积** | 第一步错误导致后续全错 | 验证中间结果、回滚机制、反思阶段 |
| **状态丢失** | 执行中断后无法恢复 | Checkpoint、可恢复状态、持久化 |

> 参考：[Loops, planning, reflection - Massive](https://docs.masst.dev/ai/agents/loops-planning)

> 关键观点：**The loop is your program, the model is just one instruction in it.** 运行时作者必须拥有控制权，而不是让模型决定是否停止、重试或花费多少预算。

---

## 3. 主流 Agent 框架对比

### 3.1 分类概览

| 框架类型 | 代表 | 核心定位 |
|----------|------|----------|
| 生态型基础工具集 | LangChain | 模块化、可组合、工具生态丰富 |
| 灵活定制型 | LangGraph | 基于图的状态机，适合复杂工作流 |
| 多 Agent 协作型 | CrewAI、AutoGen、MetaGPT | 基于角色/对话的协作团队 |
| 厂商原生型 | OpenAI Agents SDK、Anthropic Claude Agent SDK | 与自家模型深度集成，强调生产级能力 |
| 轻量入门型 | OpenAI Swarm | 极简多 Agent 快速原型 |

### 3.2 LangChain / LangGraph

| 维度 | 说明 |
|------|------|
| **核心定位** | Agent 开发的 "生态基石"，提供从数据连接到工具集成的全流程组件 |
| **LangChain 特点** | 组件化、可组合；支持多种模型、工具、向量数据库；生态最丰富 |
| **LangGraph 特点** | 将工作流建模为有向图，节点可以是 Agent、函数或决策点；支持条件分支、并行执行、持久化状态 |
| **适用场景** | 复杂决策流程、需要精细控制状态流转的企业级应用 |

> 参考：[DataCamp: CrewAI vs LangGraph vs AutoGen](https://www.datacamp.com/ru/tutorial/crewai-vs-langgraph-vs-autogen)

### 3.3 CrewAI / AutoGen / MetaGPT

| 框架 | 核心模式 | 特点 |
|------|----------|------|
| **CrewAI** | 基于角色（Role-based）的团队协作 | 模拟真实组织架构，Agent 有明确角色、目标和任务，适合流程化协作 |
| **AutoGen** | 对话驱动（Conversation-driven） | 强调 Agent 之间的自然语言交互和动态角色扮演，灵活但可能不可控 |
| **MetaGPT** | 软件公司模拟 | 将软件开发流程（需求、架构、编码、测试）分配给不同角色 Agent，强调 SOP |

### 3.4 OpenAI Agents SDK

OpenAI Agents SDK（2025 年 3 月发布，MIT 协议）是 OpenAI 官方的 Agent 生产框架，核心原语非常精简：

| 原语 | 作用 |
|------|------|
| **Agent** | 配置指令、工具、Handoff 目标、Guardrails 的 LLM |
| **Runner** | 执行循环，负责工具调用、会话管理、循环终止 |
| **Handoff** | Agent 间 delegation，保留完整上下文 |
| **Guardrail** | 输入/输出校验，失败时快速中断 |
| **Tool** | 通过 `@function_tool` 装饰的函数，支持 MCP 服务器 |
| **Session** | 持久化会话记忆 |
| **Tracing** | 内置 Trace，可视化执行流程 |

其循环大致为：

```
Runner.run(agent, input)
  → 调用 LLM（附带 tools）
  → 解析响应
     ├─ 工具调用 → 执行工具 → 追加结果 → 回到循环
     ├─ Handoff → 切换 Agent → 继续循环
     └─ end_turn → 返回结果
```

**关键启示**：生产级 Agent 框架正在收敛到少量核心原语（Agent / Tool / Runner / Guardrail / Handoff），并将复杂编排交给宿主代码完成。

> 参考：[OpenAI Agents SDK 官方文档](https://openai.github.io/openai-agents-python/)、[FutureAGI: What is the OpenAI Agents SDK?](https://futureagi.com/blog/what-is-openai-agents-sdk-2026/)

### 3.5 Anthropic Claude Agent SDK / Computer Use

Anthropic 的路线强调：

- **Computer Use**：让模型像人一样 "看屏幕、移动光标、点击按钮、输入文本"，通过截图 + 坐标操作 GUI；
- **Model Context Protocol (MCP)**：标准化 Agent 与外部工具和数据的连接；
- **Human-in-the-Loop**：对高风险动作（金融交易、文件删除、账户修改）必须人工确认；
- **Verify 循环**：Claude Code 中的典型循环是 `gather context → take action → verify work → repeat`。

> 参考：[Anthropic: Building agents with the Claude Agent SDK](https://www.anthropic.com/engineering/building-agents-with-the-claude-agent-sdk)、[AIWiki: Anthropic Computer Use](https://aiwiki.ai/wiki/anthropic_computer_use)

---

## 4. 关键协议与标准

### 4.1 Model Context Protocol (MCP)

MCP 由 Anthropic 于 2024 年底推出，现已成为 Agent 与外部工具/数据连接的事实标准之一，被形象地称为 "AI 的 USB-C"。

#### 核心架构

```
┌─────────────┐         ┌─────────────┐         ┌─────────────┐
│  MCP Host   │────────▶│  MCP Client │────────▶│  MCP Server │
│  (LLM/IDE/  │         │ (协议管理)   │         │(工具/数据)  │
│   Agent)    │◀────────│             │◀────────│             │
└─────────────┘         └─────────────┘         └─────────────┘
```

#### 核心原语

| 原语 | 含义 |
|------|------|
| **Tools** | Agent 可调用的可执行函数/操作 |
| **Resources** | Agent 可读取的数据源（文档、数据库记录等） |
| **Prompts** | 可复用的提示模板 |
| **Sampling** | 服务器向主机请求模型推理的能力 |

#### 解决的问题

- 将 M×N 的碎片化集成（M 个 Agent × N 个工具）转变为 M+N 的模块化集成；
- 提高互操作性、解耦工具与 Agent 框架；
- 标准化发现、调用、认证和状态管理。

> 参考：[InfoQ: MCP：构建更智能、模块化 AI 代理的通用连接器](https://www.infoq.cn/article/gGzM88bFa2obywlwodM4)、[MCP 官方文档](https://modelcontextprotocol.io/)

### 4.2 Agent2Agent (A2A) 协议

A2A 由 Google 于 2025 年推出，捐赠给 Linux 基金会，解决的是 **Agent 之间的横向通信** 问题，与 MCP 形成互补：

| 维度 | MCP | A2A |
|------|-----|-----|
| 方向 | 垂直：Agent ↔ 工具/数据 | 横向：Agent ↔ Agent |
| 核心问题 | 如何连接外部能力 | 如何协作完成任务 |
| 关键概念 | Tools / Resources / Prompts | Agent Cards / Tasks / Messages |

A2A 的核心概念：

- **Agent Cards**：Agent 发布的 JSON 能力说明书（身份、技能、接入方式）；
- **Tasks**：工作单元，有明确生命周期：`submitted → working → input-required → completed/failed/canceled`；
- **Messages**：结构化消息，支持文本、文件、数据等类型。

> 参考：[SalmanQ: MCP, A2A, Skills, Toolbox](https://www.salmanq.com/blog/agent-protocols-mcp-a2a/)

---

## 5. 企业级 Agent 的治理与安全

### 5.1 为什么企业级 Agent 需要治理

Agent 与普通 LLM 应用的区别在于 **自主性 + 工具调用 + 多步骤执行**。这意味着：

- 单个 Agent 可能执行高风险的组合动作；
- 多 Agent 协作可能产生不可预期的行为；
- 传统的 "代码审查 + 单元测试" 无法覆盖运行时的决策路径。

### 5.2 治理栈的五层模型

业界对企业 Agent 治理的共识是分层建设：

| 层级 | 内容 | 对应能力 |
|------|------|----------|
| **Layer 1: 身份与认证** | 为每个 Agent、工具分配唯一身份 | Agent ID、OAuth、MCP 认证 |
| **Layer 2: 注册表** | 统一管理 Agent 和工具的能力清单 | Agent Cards、Tool Registry |
| **Layer 3: 策略引擎与网关** | 执行访问控制、审批流、策略校验 | Policy Engine、Guardrails |
| **Layer 4: 可观测性平台** | 记录、监控、异常检测 | Tracing、Logging、SIEM |
| **Layer 5: 人机协同** | 高风险场景的人工确认 | Human-in-the-Loop、审批面板 |

### 5.3 关键安全控制

| 控制类型 | 说明 |
|----------|------|
| **最小权限** | Agent 只能访问其角色所需的工具和数据 |
| **沙箱化** | 限制 Agent 的文件、网络、系统命令执行能力 |
| **行为签名** | Agent 执行前获得安全授权/签名 |
| **输入/输出 Guardrails** | 防止提示注入、敏感数据泄露、有害输出 |
| **审计日志** | 全程记录指令链、执行链、响应链 |
| **异常检测** | 监测访问异常数据源、API 调用量异常等 |
| **红队测试** | 部署前后进行对抗性测试 |

> 参考：[Skywork.ai: Safety & Guardrails for Agentic AI Systems](https://skywork.ai/blog/agentic-ai-safety-best-practices-2025-enterprise/)、[Microsoft Azure: Agent Factory – Creating a blueprint for safe and secure AI agents](https://azure.microsoft.com/en-us/blog/agent-factory-creating-a-blueprint-for-safe-and-secure-ai-agents/)、[Subramanya.ai: The Governance Stack](https://subramanya.ai/2025/11/20/the-governance-stack-operationalizing-ai-agent-governance-at-enterprise-scale/)

---

## 6. 对 "Loop 控制器" 项目的启发

### 6.1 设计原则建议

基于以上调研，项目可以遵循以下原则：

1. **Loop 是核心，但 Loop 不是全部**：真正的价值在于 **围绕 Loop 的治理、审计、控制、干预机制**。
2. **控制先于智能**：在 Agent 能够自主决策之前，先建立身份、权限、审批、审计。
3. **将 Agent 当人看**：参考企业内控的三道防线，设计 Agent 的 "业务自控 → 框架风控 → 独立审计" 三层体系。
4. **标准化接口**：拥抱 MCP（垂直工具集成）和 A2A（横向 Agent 协作），避免重复造轮子。
5. **可观测性内建**：Trace、Event、Log 应该是一等公民，而非后期补丁。

### 6.2 关键架构问题

1. Loop 控制器应该采用 **中心化 Runtime** 还是 **去中心化编排**？
2. 如何在 Loop 中嵌入 **风险评估** 和 **控制检查点**？
3. Agent 的 **权限模型** 应该采用 RBAC、ABAC 还是基于任务上下文的动态授权？
4. **Human-in-the-Loop** 应该以什么粒度介入？动作级、任务级、异常级？
5. 如何设计 **审计数据结构** 才能既满足企业内控要求，又不牺牲执行效率？
6. 如何与 MCP/A2A 生态共存：作为 Host、作为 Orchestrator、还是作为 Governance Layer？

这些问题将在下一阶段的架构设计文档中继续展开。

---

## 7. 参考来源

1. [CSDN: AI Agent 框架对比区别](https://blog.csdn.net/sinat_20277079/article/details/154843997)
2. [DataCamp: CrewAI vs LangGraph vs AutoGen](https://www.datacamp.com/ru/tutorial/crewai-vs-langgraph-vs-autogen)
3. [OpenAI Agents SDK 官方文档](https://openai.github.io/openai-agents-python/)
4. [FutureAGI: What is the OpenAI Agents SDK?](https://futureagi.com/blog/what-is-openai-agents-sdk-2026/)
5. [Anthropic: Building agents with the Claude Agent SDK](https://www.anthropic.com/engineering/building-agents-with-the-claude-agent-sdk)
6. [AIWiki: Anthropic Computer Use](https://aiwiki.ai/wiki/anthropic_computer_use)
7. [Loops, planning, reflection - Massive](https://docs.masst.dev/ai/agents/loops-planning)
8. [InfoQ: MCP：构建更智能、模块化 AI 代理的通用连接器](https://www.infoq.cn/article/gGzM88bFa2obywlwodM4)
9. [MCP 官方文档](https://modelcontextprotocol.io/)
10. [SalmanQ: MCP, A2A, Skills, Toolbox](https://www.salmanq.com/blog/agent-protocols-mcp-a2a/)
11. [Skywork.ai: Safety & Guardrails for Agentic AI Systems](https://skywork.ai/blog/agentic-ai-safety-best-practices-2025-enterprise/)
12. [Microsoft Azure: Agent Factory](https://azure.microsoft.com/en-us/blog/agent-factory-creating-a-blueprint-for-safe-and-secure-ai-agents/)
13. [Subramanya.ai: The Governance Stack](https://subramanya.ai/2025/11/20/the-governance-stack-operationalizing-ai-agent-governance-at-enterprise-scale/)
14. [Agent-Orchestrated Architecture for Distributed AI Systems](https://ceur-ws.org/Vol-4158/Paper07.pdf)
15. [OrchVis: Hierarchical Multi-Agent Orchestration for Human Oversight](https://arxiv.org/pdf/2510.24937)
