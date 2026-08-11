# 开发日志

> 本文档记录 Loop Controller 项目从初始阶段开始的关键决策、讨论和行动，便于后续任何参与者（人或 Agent）快速了解项目演进。

---

## 2026-08-06：R0-R3 架构定稿与竞对调研完成

### 背景

在前期测试证据（T1/T3）的基础上，需要完成竞对调研、明确差异化定位，并把领导指示的"R0 人类制定、R1/R2 不用大模型、R3 异步有偏采样"转化为可汇报的 R0-R3 架构。

### 讨论

- 对比 Zenity、Palo Alto/Protect AI、OPA 三家在 Agent 治理层面的差异：
  - Zenity：专用 Agent 治理平台，闭源，思路最接近 Loop Controller；
  - Palo Alto/Protect AI：综合 AI 安全平台，控制点偏向网关/流量；
  - OPA：通用策略引擎，缺少 Agent 治理的完整闭环。
- 明确 Loop Controller 差异化：开源 SDK、以内控方法论为设计前提、聚焦 Agent 内部循环的制度基础设施。
- 讨论 R0-delegate 位置：最终确定为 R0 授权的实时审批人类，放在 R0 治理层内，作为治理机制在运行时的延伸。
- 明确 R1/R2 实时执行不用大模型，R3 审计可用大模型但异步。

### 决策

1. **架构模型**：采用 R0-R3 四层治理模型 + 基础设施层，替代旧版"治理/编排/执行"三层模型。
2. **R0-delegate 归属**：放在 R0 治理层，是被 R0 授权的人类审批代理人。
3. **人机分工**：
   - R0 Governance：人类，定制度、批 Policy、问责；
   - R0-delegate：人类，实时审批例外；
   - R1 Agent：系统，执行 + 自检 + 申报；
   - R2 Checkpoint：系统，规则引擎 + 专用小模型，实时策略执行；
   - R3 Audit：系统，可用 LLM，异步审计。
4. **文档一致性**：更新 `docs/architecture/overview.md`、`docs/architecture/00_r0r3_architecture.md`、`README.md` 等关键文档，统一使用 R0-R3 叙事。

### 行动

- 完成 `docs/research/03_runtime_governance_landscape.md` 竞对调研报告；
- 更新 `reports/project_feasibility_report.md` 到 v2.2，加入竞对差异化分析；
- 创建 `docs/architecture/00_r0r3_architecture.md`；
- 重写 `docs/architecture/overview.md` 为 R0-R3 版本；
- 更新 `README.md` 反映当前状态和 R0-R3 架构；
- 更新 `docs/research/README.md` 索引；
- 创建 `docs/process/document_status_review_20260806.md` 梳理文档状态；
- 绘制 R0-R3 架构图和完整时序图（飞书画板）。

### 遗留问题

- R4 定义仍待领导明确；
- R2 专用小模型边界待确认；
- R3 审计结论是否有强制力待确认；
- Policy 表达形式（Python / YAML / JSON / DSL）待决策；
- Audit Store 独立性要求待确认。

---

## 2026-08-03：项目启动与前期调研

### 背景

- 实习地点：武汉光谷某新兴科技公司
- 项目目标：创建一个开源的 Agent 框架，最终方向是 "Loop 控制器"，符合企业内控部门的管理方式
- 核心理念：把 Agent 当人看
- 当前任务：完成前期调研，建立文档基础

### 今日行动

1. 与企业内控相关的中英文资料检索，覆盖：
   - 中国《企业内部控制基本规范》
   - COSO 内部控制框架
   - 华为、东方电气等企业的三道防线实践
   - 国际三道防线模型（IIA / CrossCheck Compliance / NASSCOM）
2. 与 Agent 框架/产品相关的资料检索，覆盖：
   - LangChain / LangGraph / CrewAI / AutoGen / MetaGPT
   - OpenAI Agents SDK
   - Anthropic Computer Use / Claude Agent SDK
   - MCP（Model Context Protocol）与 A2A（Agent2Agent）协议
   - 企业 Agent 治理与安全最佳实践
3. 输出文档：
   - [docs/research/01_internal_control_research.md](../research/01_internal_control_research.md)
   - [docs/research/02_agent_landscape_research.md](../research/02_agent_landscape_research.md)
   - [docs/research/README.md](../research/README.md)
   - [README.md](../../README.md)
   - [docs/architecture/overview.md](../architecture/overview.md)
   - [docs/process/development_log.md](./development_log.md)

### 关键决策

- 文档以中文为主，代码/API 文档以英文为主，便于国际化开源
- 项目采用 "先提出问题 → 找到逻辑 → 完善实现" 的渐进式方法
- 不试图取代现有 Agent 框架，而是作为治理运行时层存在
- 前期调研结论将作为架构设计的核心输入

### 开放问题

详见各调研报告末尾及 [docs/architecture/overview.md](../architecture/overview.md)。

---

## 2026-08-03：决策 01 — Loop Controller 形态

### 背景

项目需要确定以什么形态交付：SDK、服务，还是混合形态。这个决策会影响后续的技术架构、开源传播策略和企业级演进路径。

### 讨论

- 选项 A（SDK）传播门槛低，但治理 enforcement 弱，难以满足企业集中管控需求；
- 选项 B（服务）治理能力强，但工程复杂度高，对早期开源传播不友好；
- 选项 C（先 SDK，后服务）兼顾传播与治理，最符合开源项目从社区到企业的成长路径。

实习公司的期望是：做出开源的、先进的、可用的框架，通过社区传播，后续再迭代进化。

### 决策

**选择选项 C：先 SDK，后服务。**

- 第一版聚焦轻量级 SDK，让开发者可以快速接入；
- SDK 中预留 "Governance Client" 抽象，未来可指向本地策略、远程服务或企业 IAM；
- 核心定位不变：作为现有 Agent 框架之上的治理运行时层，不取代它们。

### 行动

- 在后续架构设计中，所有抽象都要同时兼容 "本地模式" 和 "远程服务模式"；
- 第一版 MVP 以 SDK 形式交付，重点验证 "Loop + 治理" 的核心价值。

### 遗留问题

- SDK 与现有框架（LangChain、OpenAI Agents SDK、MCP 等）的集成边界如何划分？
- 第一版要解决什么最小场景？

---

## 2026-08-03：设计哲学补充 — 从"审计损失"到"制度基础设施"

### 背景

在讨论 Loop Controller 形态和场景时，提出一个更深层的洞察：传统网安/沙箱/逐个确认的方式虽然能明确责任，但本质上是"审计损失"，并没有真正降低风险。真正有效的治理应像社会降低犯罪率一样，依靠清晰的制度、良好的环境和无处不在的保护机制。

### 讨论

- 传统思路：沙箱 + 逐个确认 + 白名单 → 用户疲惫、机制形式化、仅用于事后追责；
- 目标思路：把 Agent 当人看，为 Agent 组织建立"规章制度"，明确每个"数字员工"的角色、权限和行为准则；
- 关键转变：从"审计损失"转向"预防损失"，从"技术控制"转向"组织行为学"。

### 决策

- **设计哲学**：制度优先于审批，预防优先于审计；
- **Loop Controller 定位**：不是审批工具，而是 Agent 组织的"制度基础设施"；
- **Human-in-the-Loop 原则**：只在真正需要判断的边界处介入，避免无意义确认；
- **技术语言**：第一版采用 **Python**，以利用最丰富的 Agent 生态并便于社区传播。

### 行动

- 更新 [README.md](../../README.md) 的愿景与核心隐喻，体现"制度基础设施"理念；
- 更新 [docs/architecture/overview.md](../architecture/overview.md) 的架构目标，加入"制度优先于审批"和"预防优先于审计"；
- 后续所有抽象设计都要回答：这个设计是在"写制度"还是在"加审批"？

### 遗留问题

- 如何用代码/配置表达"Agent 组织的规章制度"？（Policy as Code 的形态）
- 如何区分"默认保护"与"关键审批"的触发条件？
- 第一版场景如何体现"制度基础设施"而非"审批工具"？

---

## 2026-08-03：搭建现有 Agent 安全手段测试环境

### 背景

在继续架构设计之前，需要先通过实际运行现有 Agent 安全框架，理解其能力边界和失效模式，为 Loop Controller 的设计提供实证依据。重点测试方向来自 [reports/agent_security_framework_brief.md](../../reports/agent_security_framework_brief.md) 中的分层防御模型和 OWASP ASI01 典型案例。

### 决策

- 优先测试 **OpenAI Agents SDK 的 Guardrails**（输入/输出/工具），因为它最接近我们规划的原生 Runner + 治理形态；
- 同步编写测试计划文档 [reports/agent_security_test_plan.md](../../reports/agent_security_test_plan.md)；
- 测试结果记录在 [tests/security_experiments/test_results.md](../../tests/security_experiments/test_results.md)。

### 行动

- 创建测试目录 `tests/security_experiments/`；
- 安装依赖：`openai-agents`、`openai`、`python-dotenv`；
- 实现 4 个测试脚本：
  - `t1_1_input_guardrail.py`：提示注入检测
  - `t1_2_output_guardrail.py`：PII 泄露检测
  - `t1_3_tool_guardrail.py`：高风险工具拦截
  - `t1_4_goal_hijack.py`：间接提示注入/目标劫持
- 根据 OpenAI Agents SDK 官方文档校正 Guardrail API（`@input_guardrail`、`@output_guardrail`、`@tool_input_guardrail` 等）。

### 遗留问题

- 测试脚本尚未实际运行，需要 API Key 才能验证；
- 需要补充 Promptfoo 红队测试环境（T2）；
- 需要补充 MCP 工具权限边界测试环境（T3）。

---

## 2026-08-05：现有 Agent 安全手段实测（T1 三层 Guardrail + T3 MCP 权限边界）

### 背景

前期已搭建测试环境，但尚未实际运行。今日在配置好 Kimi API 后，系统性地跑完了 T1（OpenAI Agents SDK 三层 Guardrail）和 T3（MCP 工具权限边界）测试，并拉取了官方 MCP servers 仓库对比 mock 与真实 filesystem server 的差异。

### 行动

1. 运行并记录 T1.1 输入 Guardrail 测试：
   - 能拦截直接/间接/目标漂移型提示注入；
   - 对"信息提取型注入"防御不稳定（"列出联系方式"通过，"提取关键信息"被拦截）。
2. 运行并记录 T1.2 输出 Guardrail 测试：
   - 同一份脚本不同时间跑出不同结果，判定极不稳定；
   - 系统提示有时比 Guardrail 更能引导 Agent 主动脱敏，但这依赖模型自律。
3. 运行并记录 T1.3 工具 Guardrail 测试：
   - 发现 `ToolGuardrailFunctionOutput.reject_content()` 的行为是"拒绝工具调用但继续执行"；
   - 工具 Guardrail 只有在 Agent 实际调用工具时才生效，无法阻止意图产生。
4. 运行并记录 T1.4 / T1.4b / T1.4c 目标劫持测试：
   - 系统提示无法防御信息泄露型攻击；
   - 只要请求合理包装成"提取关键信息"或"列出联系方式"，Agent 就会泄露邮箱、手机号、项目代号、账号密码。
5. 搭建并运行 T3 MCP 权限边界测试：
   - 下载官方 MCP servers 仓库，分析 filesystem server 的 `path-validation.ts` 和 `lib.ts`；
   - 创建基础版 mock server/client 和真实攻击场景版；
   - 验证了 Client 侧 Policy Gateway 的可行性，以及 Server enforcement 对路径遍历、符号链接绕过的防御；
   - 发现 Client Policy 和 Server enforcement 必须互补。

### 关键发现

- **现有 Guardrail 方案依赖模型自律或 LLM 概率判断，不适合作为核心安全机制**；
- **信息泄露不能靠输入/输出 Guardrail 解决，需要确定性的输出策略和结构化校验**；
- **MCP 协议本身不强制权限控制，Client 侧 Policy Gateway 是 Loop Controller 的明确切入点**；
- **真实 filesystem server 处理了路径遍历、符号链接、父目录检查等复杂边界，mock 在核心模型上一致但细节有差距**。

### 决策

- 暂时不再扩展 T2/T4/T5 测试，当前实证已经足够支撑架构设计；
- 下一步进入 Loop Controller 核心抽象设计：Policy 模型、Task 对象、Loop Runtime、审计机制；
- 在 Policy 设计中必须支持：工具白名单、资源路径范围、输出限制、审批规则、审计级别。

### 遗留问题

- Policy 应该用 Python 类 / YAML / JSON / DSL 哪种形式表达？
- 如何在 Agent 调用工具前就限制其可用工具集？
- 输出层如何区分"敏感主题"和"敏感值"？
- Human-in-the-Loop 的触发条件如何与 Policy 关联？

---

## 后续记录模板

```
## YYYY-MM-DD：标题

### 背景
（为什么做这件事）

### 讨论
（关键观点、利弊权衡）

### 决策
（最终决定了什么）

### 行动
（具体做了什么）

### 遗留问题
（尚未解决的问题）
```
