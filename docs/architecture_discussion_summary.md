# Loop Controller 架构讨论总结

> 本文件记录围绕 Loop Controller v0.5.0 源码的架构讨论要点，涵盖 R0-R3 治理模型、R1 Agent 行为、上下文边界、Proxy 模式、批量/并行问题、用户输入治理、小模型引入等关键议题。

---

## 一、项目定位：Loop Controller 是什么？

Loop Controller 是 **AI Agent 的实时治理运行时层**，核心职责是在 Agent 调用外部工具之前插入策略判定。

通俗类比：**报销系统里的财务审核 + 审计黑匣子**。

- Agent（员工）想调用外部工具（发邮件、读文件、搜网页）→ 必须填申请表（ActionProposal）。
- R2 Checkpoint（财务部）审核申请表 → 决定 allow / deny / modify / require_approval。
- 只有通过 R2 授权，才真正调用 MCP Server（银行转账）。
- R3 Audit（审计部）记录完整链路，不可篡改。

核心铁律：**R1 不能直接调用外部工具，所有外部工具调用必须经过 R2 授权。**

---

## 二、R0-R3 四层治理模型

| 层级 | 角色 | 企业内控映射 | 主要职责 |
|---|---|---|---|
| **R0** | Governance / R0-delegate | 董事会 / 被授权主管 | 制定制度、批准高风险动作、接收审计报告 |
| **R1** | Agent + 轻量分类器 | 业务部门 / 一线员工 | 接收任务、规划动作、自检、申报动作 |
| **R2** | Checkpoint + Policy Engine | 风控 / 合规 / 内控 | 统一策略执行、验证申报、授权或拦截 |
| **R3** | Audit | 内部审计 / 纪检 | 异步采集日志、脱敏审计、生成报告 |

R2 明确不使用大模型，必须轻量、确定性、断网可用。

---

## 三、R1 Agent 的行为模式

### 单步思考-行动循环

当前 R1 是典型单步循环，不是 harness loop / 多 Agent 编排：

```
输入（Task + Observations + Conversation）
  → Planner.next_action() 输出 PlannedAction
  → 框架组装 ActionProposal（统一生成 call_id）
  → 轻量分类器打风险标签
  → R2 Checkpoint.evaluate() 判定
  → 执行 / 拦截 / 审批
  → 结果回到 Observations
  → 下一轮
```

### Planner 的两种实现

| 实现 | 行为 |
|---|---|
| `ScriptedPlanner` | 按 YAML 脚本顺序执行，完全确定 |
| `LLMPlanner` | 调用真实 LLM，但每次也只输出一个动作 |

Planner 不生成 `call_id/task_id/agent_id`，这些身份字段由 `run_task` 框架统一生成，防止伪造。

---

## 四、上下文与 ID 管理

### ID 来源与维护

| ID | 来源 | 持久化 |
|---|---|---|
| `user_id` / `agent_id` / `profile_id` | YAML 配置 | 配置只读 |
| `session_id` | `SessionManager` 生成/复用 | `JsonlSessionBackend`（v0.4.0） |
| `task_id` | `Runtime.create_task` 生成 | 仅通过审计事件间接记录，无独立 TaskStore |
| `call_id` | `run_task` 框架生成 | `JsonlDecisionStore` |
| `decision_id` | `Checkpoint.evaluate` 生成 | `JsonlDecisionStore` |
| `request_id` | `build_approval_request` 生成 | `JsonlApprovalStore` |
| `event_id` / `trace_id` | `_audit_event` 生成 | `JsonlAuditStore` |

### R1 能看到的上下文

| 上下文 | 说明 |
|---|---|
| `Task` | 当前任务描述、user_id、agent_id、session_id |
| `ConversationContext` | session 级对话历史 |
| `observations` | 历史工具调用结果 |
| `CapabilityProfile` | Agent 的工具权限、预算、风险阈值 |

Loop Controller **不管理 Agent 内部推理过程、CoT、记忆系统**。

---

## 五、v0.5.0 新增：Proxy Server 模式

Loop Controller 可以暴露为一个 MCP Server，外部 Agent（如 Claude Desktop、Cursor）作为 MCP Client 连接。

### 两种入口

| 入口 | 适用场景 |
|---|---|
| `Runtime.run_task()` | 本地托管 Agent |
| `lc proxy --agent-id xxx --user-id xxx` | 外部 Agent 接入 |

### Proxy 模式数据流

```
外部 Agent: tools/list
  → Loop Controller 返回按 CapabilityProfile 过滤后的工具列表

外部 Agent: tools/call(name, arguments)
  → Loop Controller 内部：
    1. create_task()
    2. 组装 ActionProposal
    3. Checkpoint.evaluate()
    4. allow/modify → forward 执行真实 MCP Server
    5. require_approval → 直接返回 BLOCKED（Proxy 模式不支持异步挂起）
```

### 关键限制

- Proxy 模式下单次 `tools/call` 创建一个独立 Task。
- `require_approval` 直接返回 `BLOCKED`，需要用户审批后重试。
- 外部 Agent 并发调用时，每个调用独立创建 Task，history 互相不可见，组合规则失效。

---

## 六、接入外部 Agent 的要求

### 外部 Agent 需要做什么？

1. **不提供真实 MCP Server 的直连配置**。
2. **所有 MCP 配置只指向 Loop Controller Proxy**。
3. Loop Controller 内部配置真实 MCP Server、工具映射、Profile、Agent 身份。

### 流程

```
Agent 开发者提供：想用的工具列表 + 对应 MCP Server 信息
企业管理员：
  1. 注册 MCP Server 到 config/mcp_servers.yaml
  2. 配置 tool_mapping.yaml
  3. 在 profiles.yaml 中授权
  4. 在 agents.yaml 中注册 Agent
  5. 给 Agent 开发者 Loop Controller Proxy 的配置
Agent 开发者：
  把原来的直连 MCP 配置替换为 Loop Controller Proxy
```

### 关键风险

如果外部 Agent 配置里同时保留了直连 MCP Server，它就可以绕过治理。必须通过部署架构防止：网络隔离、凭证集中管理、Loop Controller 作为唯一网关。

---

## 七、批量 / 并行工具调用问题

### 当前架构

- R2 按**单个 ActionProposal** 串行判定。
- `Planner.next_action()` 一次只返回一个动作。
- `PermissionInteractionAnalyzer` 检查当前 proposal 与**历史已执行动作**的组合风险。

### 问题

如果 Agent Planner 一次性想调用多个工具（A、B、C），当前架构会强制串行化：

```
A → R2 → 执行 → B → R2 → 执行 → C → R2 → 执行
```

- 性能下降。
- Proxy 模式下如果 Agent 并发调用，组合规则失效。

### 为什么不支持批量

MCP 协议本身是单次 tool call，Loop Controller 作为中间层无法自动知道哪些调用属于同一个"计划批次"。

### 可行方案（未来扩展）

| 方案 | 说明 |
|---|---|
| Agent 串行调用 | 改动最小，推荐 |
| Agent 传 batch_id | 需要 Agent 配合 |
| Agent 先发计划声明 | 非标准 MCP |
| R2 提供 batch evaluate 接口 | 需要扩展 ActionProposal / Decision |
| 组合工具封装 | 业务固定流程 |

### 建议

MVP 维持串行，文档中明确说明。未来通过 `batch_id`、`tool_group` 或 `evaluate_batch` 扩展。

---

## 八、用户输入与 task_context 问题

### 当前状态

| 场景 | task_context |
|---|---|
| 本地模式 | 由 `build_governance_context()` 生成，包含 `Task.description` 和最近对话消息 |
| Proxy 模式 | 为空字符串 `""` |
| 默认 Rego 策略 | **不使用** `task_context` |
| 轻量分类器 | **不读取** `task_context` |

### 问题

如果 R2 完全不看用户输入，存在明显盲区：

- 用户说"查天气"，Agent 却读客户名单 → 可能放行。
- 同样的工具在不同用户意图下风险不同 → 无法区分。
- Agent 主动执行用户未要求的动作 → 无法识别。

### 为什么不直接让 Rego 理解自然语言

Rego 是确定性规则语言，无法穷举自然语言表达。试图用 Rego 写自然语言规则会失败。

### 建议方案

MVP 阶段：
- 保留 `task_context` 作为人类可读审计字段。
- 在 `ActionProposal` 中新增 `intent_tag: str | None = None` 字段作为扩展口。
- 默认策略仍基于工具名/参数/风险标签，不基于自然语言上下文。

未来阶段：
- 在 R1 轻量分类器中引入本地小模型。
- 小模型读取 `task_context`，输出结构化标签（`risk_tags`、`intent_tag`）。
- R2 Rego 基于这些标签做确定性判定。

---

## 九、轻量分类器与小模型

### 当前实现

`RuleBasedClassifier` 只有 4 条规则：
- `send_email` → high
- `read_file` → medium
- 参数含邮箱 → 追加 `pii_involved`
- 参数含凭证 → 追加 `credential_involved`

### 是否需要小模型

| 阶段 | 建议 |
|---|---|
| MVP | **不用**小模型，保持确定性规则 |
| 未来 | **必须上**，否则无法覆盖语义级风险 |

### 小模型放在哪里

**只放在 R1 轻量分类器**，不要放进 R2。

架构：

```
R1 轻量分类器（本地小模型）
  输入：task_context + tool_name + arguments + 历史摘要
  输出：risk_level, risk_tags, intent_tag
        ↓
R2 Checkpoint（Rego 规则）
  输入：tool_name + arguments + risk_level + risk_tags + intent_tag
  输出：allow / deny / modify / require_approval
```

### 推荐模型

| 模型 | 用途 | 推理延迟 |
|---|---|---|
| Qwen2.5-Instruct 0.5B/1.8B | 意图标签分类 | 30-80ms |
| Phi-3 mini / Llama 3.2 1B | 风险分类 | 50-100ms |
| BGE-small / all-MiniLM | 语义相似度 | 10-30ms |

### PermissionInteractionAnalyzer

MVP 保持静态规则。未来基于 R1 输出的标签组合做规则匹配，例如：

```
history 含 customer_data_intent
+ current 含 external_communication
→ require_approval
```

---

## 十、产品边界：我们是不是 abnormal？

**不是 abnormal。**

Loop Controller 的定位是 **Agent 外部的 Runtime Governance Boundary**，与 Zenity、Palo Alto Prisma AIRS 同类型。

关键边界：
- 不管 Agent 内部 CoT / memory / 对话上下文。
- 只在工具调用这一层插入治理 hook。
- 不依赖 Agent 自律，强制守住动作执行边界。

这是"外部管边界"，不是"内部管对齐"。

---

## 十一、生产部署缺口

| 组件 | 当前状态 | 说明 |
|---|---|---|
| `InMemoryBudgetLedger` | 内存 | 未持久化，重启丢失 |
| `TaskStore` | 缺失 | 无法查询活跃任务列表 |
| `PermissionInteractionAnalyzer` | 静态规则占位 | 复杂组合风险未实现 |
| `Policy Compiler` | 直接用 Rego | 自然语言编译未做 |
| `Earned Authority` | 未实现 | 动态权限提升未做 |
| R3 审计分析 | 只有存储 | 无 LLM 采样分析组件 |
| Proxy 审批恢复 | 不支持 | require_approval 直接返回 BLOCKED |

---

## 十二、结论与建议

### 现在（MVP）应该做什么

1. 跑通单 Agent 串行治理闭环：Planner → ActionProposal → R2 → 执行/拦截/审批 → 审计。
2. 保持 `RuleBasedClassifier` 和 Rego 确定性规则。
3. 在 `ActionProposal` 中新增 `intent_tag` 字段作为未来扩展口。
4. 文档中明确边界：
   - Loop Controller 不管 Agent 内部上下文。
   - 当前只支持串行判定，批量/并行是未来扩展点。
   - R2 不基于自然语言做判定，只基于结构化信号和规则。

### 未来应该做什么

1. 在 R1 轻量分类器中引入本地小模型，输出 `intent_tag` 和 `risk_tags`。
2. R2 Rego 基于标签扩展规则。
3. 考虑 Proxy 层 session 级串行锁或 batch_id 机制，处理并发调用。
4. 实现持久化 BudgetLedger。
5. R3 异步审计引入 LLM 做风险采样分析。

---

## 十三、关键原则

- **治理优先于性能**：在工具调用层串行是合理 trade-off。
- **R2 不用大模型**：保持确定性、可解释、断网可用。
- **默认拒绝**：未明确允许的动作默认 deny。
- **外部 Agent 必须只走 Loop Controller**：否则可以绕过治理。
- **上下文边界清晰**：我们不掌握 Agent 内部状态，只掌握 session 级治理状态。
