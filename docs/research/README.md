# 前期调研报告索引

本目录存放项目启动阶段的前提调研报告。所有报告以 Markdown 形式维护，便于版本控制和后续迭代。

---

## 报告列表

| 编号 | 文件名 | 主题 | 状态 | 核心问题 |
|------|--------|------|------|----------|
| 01 | [01_internal_control_research.md](./01_internal_control_research.md) | 企业内控部门运作方式 | ✅ 已完成 | 企业内控如何组织、流程如何运转、控制手段有哪些？如何映射到 Agent 治理？ |
| 02 | [02_agent_landscape_research.md](./02_agent_landscape_research.md) | Agent 产品与架构逻辑 | ✅ 已完成 | 主流 Agent 框架如何设计？循环控制、协作模式、安全治理的共识是什么？ |
| 03 | [03_runtime_governance_landscape.md](./03_runtime_governance_landscape.md) | Agent 运行时治理竞对调研 | ✅ 已完成 | Zenity、Palo Alto/Protect AI、OPA 在 Agent 治理层面的差异是什么？Loop Controller 的空白在哪里？ |

---

## 如何使用这些报告

1. **架构设计输入**：三份报告共同构成 Loop Controller 的领域知识基础。  
   - 内控报告提供 "把 Agent 当人看" 的组织与流程隐喻；  
   - Agent 报告提供技术实现层面的框架与协议参考；
   - 竞对调研报告验证市场空间并明确差异化定位。
2. **问题清单来源**：每份报告末尾都列出了留给架构设计的开放问题，将在后续文档中逐步回答。
3. **持续更新**：随着项目深入，调研结论可能被修正或补充。修改时请同步更新本索引中的 "状态" 信息。

---

## 关键结论速览

- **企业内控**：核心在于"制度设计 + 分层防控 + 监督闭环"，不是简单的事后审计。
- **Agent 技术生态**：主流框架正在收敛到 Agent / Tool / Runner / Guardrail / Handoff 等原语，但"治理层"覆盖不足。
- **竞对格局**：Zenity、Palo Alto/Protect AI、OPA 验证了 Runtime 强制与统一策略的必要性，但开源、可嵌入的"制度基础设施"仍是空白。

---

## 参考文档

- [`docs/architecture/overview.md`](../architecture/overview.md)：R0-R3 架构概览
- [`docs/architecture/00_r0r3_architecture.md`](../architecture/00_r0r3_architecture.md)：R0-R3 分层详细设计
- [`reports/project_feasibility_report.md`](../../reports/project_feasibility_report.md)：项目可行性论证
- [`reports/test_conclusion_report.md`](../../reports/test_conclusion_report.md)：T1/T3 测试证据
