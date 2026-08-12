# Loop Controller 架构概览（草案 v0.2）

> **文档定位**：Loop Controller 的架构入口文档。本版本从旧版"治理/编排/执行"三层模型更新为 **R0-R3 治理模型**，详细分层设计见 [`00_r0r3_architecture.md`](./00_r0r3_architecture.md)。
>
> **状态**：草案 v0.2，待评审  
> **最后更新**：2026-08-06

---

## 1. 架构目标

1. **制度优先于审批**：通过 Policy 和 CapabilityProfile 让 Agent 默认做对，只在边界处找人。
2. **Runtime 强制优先于模型自律**：Policy 由 R2 Checkpoint 强制执行，不依赖 LLM 是否"理解"提示词。
3. **R1/R2 实时执行不用大模型**：用确定性规则 + 专用小模型。
4. **R3 审计可用大模型，但异步**：审计不阻塞主流程。
5. **人类只在 R0/R0-delegate 做治理/审批决策**。
6. **兼容并包**：不取代现有 Agent 框架，作为治理运行时层嵌入。
7. **渐进式复杂度**：从单 Agent Loop 开始，逐步支持多 Agent、多租户、企业级部署。

---

## 2. 问题空间

### 2.1 我们要解决的核心问题

| 问题 | 描述 |
|------|------|
| **谁来负责** | Agent 执行高风险动作时，谁授权、谁负责、如何追责？ |
| **边界在哪** | Agent 能访问什么工具、能执行什么动作、能输出什么信息？ |
| **怎么知道它没越界** | 如何判断 Agent 没有偏离目标、执行越权操作？ |
| **出错了怎么办** | 如何检测异常、如何干预、如何回滚、如何整改？ |
| **如何审计** | 如何向合规部门证明 Agent 行为可解释、可追溯？ |

### 2.2 第一版不在范围内的问题

- 复杂的自然语言理解优化；
- 特定行业的领域模型；
- 大规模分布式多 Agent 协商协议；
- 完整的 GUI 自动化（Computer Use）。

---

## 3. 核心架构：R0-R3 四层模型

Loop Controller 借鉴企业内控的"三道防线"，把治理抽象为四个角色：

| 角色 | 企业内控映射 | 主要职责 | 实时/异步 |
|------|-------------|---------|----------|
| **R0 Governance** | 董事会/经营层/治理层 | 定风险偏好、批 Policy、接收审计报告、问责 | 异步 |
| **R0-delegate** | 被授权的业务主管/安全员 | 实时审批例外请求，必要时升级到 R0 | 实时 |
| **R1 Agent** | 业务部门/一线员工 | 执行任务、自检、申报动作、执行获批动作 | 实时 |
| **R2 Checkpoint** | 风控/合规/内控部 | 统一策略执行、验证申报、返回 allow/deny/modify/require_approval | 实时 |
| **R3 Audit** | 内部审计/纪检监察 | 异步采集日志、有偏采样、评估有效性、反馈整改 | 异步 |

详细的分层职责、运行时流程和基础设施见 [`00_r0r3_architecture.md`](./00_r0r3_architecture.md)。

---

## 4. 核心抽象（候选）

| 抽象 | 说明 | 企业内控映射 |
|------|------|-------------|
| **Agent** | 具有身份、角色、能力边界的执行实体 | 企业员工 |
| **CapabilityProfile** | Agent 的岗位说明书：能做什么、不能做什么 | 岗位职责 |
| **Task** | 需要 Agent 完成的业务目标 | 工作任务 |
| **Loop** | Agent 执行 Task 时的迭代循环 | 工作过程 |
| **Checkpoint** | Loop 中的策略检查点 | 关键控制点 |
| **Action** | Agent 对外部世界的一次调用 | 业务操作 |
| **Policy** | 组织层面的行为规则 | 制度/办法 |
| **RiskAssessment** | 对任务或动作的风险评估结果 | 风险评估报告 |
| **Trace** | 一次完整任务执行的链路记录 | 审计底稿 |

---

## 5. 关键证据支撑

架构设计基于以下实证和调研：

- **T1 测试**：LLM 判定型 Guardrail 拦截率 20%-60%，无法作为企业级安全边界；Agent 在无 Guardrail 时 100% 泄露敏感信息。
- **T3 测试**：MCP 协议缺少工具级权限表达，Client Policy Gateway 是可行且必要的补充层。
- **竞对调研**：Zenity、Palo Alto/Protect AI、OPA 验证了 Runtime 强制和统一策略的必要性，但开源、可嵌入的"制度基础设施"仍是空白。

详见：
- [`reports/test_conclusion_report.md`](../../reports/test_conclusion_report.md)
- [`docs/research/03_runtime_governance_landscape.md`](../research/03_runtime_governance_landscape.md)

---

## 6. 需要做出的关键决策

1. **R2 专用模型边界**：哪些场景允许 R2 使用专用小模型？
2. **R3 审计结论强制力**：R3 发现严重违规时能否触发暂停/降级？
3. **R4 定义**：讨论中提到的 R0-R4 中，R4 指什么？
4. **Policy 表达形式**：Python 类 / YAML / JSON / DSL？
5. **Audit Store 独立性**：是否需要与 R1/R2 运行时物理隔离？

---

## 7. 进展更新

- [x] 根据讨论反馈收敛 R0-R3 架构
- [x] 输出核心 API 接口草案：详见 [05_mvp_core_abstractions.md](./05_mvp_core_abstractions.md)
- [ ] 绘制 Loop 控制器的状态机图
- [ ] 开始 Phase 1 MVP 代码实现
