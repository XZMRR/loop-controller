# LLMPlanner 设计补充（v1.0）

> **文档定位**：本文档是《Loop_Controller_MVP方案_纯工具调用_v1.1》（下称"方案"）§5.1 的补充设计，作为开发指南 **T3.5（LLMPlanner）** 的实施依据。方案已定义 `Planner` 协议与输出契约，本文补齐三样未细化的东西：**上下文管理（prompt 组装与摘要规则）**、**token 计量对接**、**失败兜底**。
>
> **目标场景**：面向技术社区/老板的演示——用真实 LLM 动态规划替换 ScriptedPlanner，展示"真实 Agent 被治理"的完整画面。
>
> **与方案的关系**：纯补充，零偏离。方案 §5.1 已定的接口、JSON Schema、身份字段规则本文不重述，直接引用。
>
> **最后更新**：2026-08-17

---

## 1. 架构位置与安全前提

### 1.1 LLMPlanner 在 R0-R3 中的位置

```
User → R1 [ LLMPlanner → PlannedAction ] → run_task 组装 ActionProposal
         │                                    （call_id/agent_id 由框架生成）
         ▼
      RuleBasedClassifier（不变）
         ▼
      R2 Checkpoint（不变，唯一权威）
```

**三条不可动摇的前提**：

1. **LLMPlanner 是非治理组件**。它只决定"建议下一步做什么"，该建议必须过 R2 完整判定。LLM 规划得再离谱，最坏结果是任务失败，不是安全边界失效；
2. **LLM 输出是不可信输入**。`LLMPlanner` 对 LLM 返回内容只做 Schema 校验与映射，不信任任何字段语义；`tool_name`、`arguments` 的合法性由 R2 最终判定；
3. **安全不靠 system prompt 乞求**。prompt 里可以写"不要试图绕过审核"，但这只是减少演示噪音的软引导，真正的强制永远在 R2。**文档、注释、演示解说中都不得把 prompt 约束描述为安全机制。**

### 1.2 与"断网可用"原则的关系

方案原则 6（核心控制流程断网可用）约束的是 **R1 自检、R2 判定、R3 审计**——这些全部保持本地（OPA sidecar、JSONL、规则分类器），LLMPlanner 的引入不改变这一点。

LLM 规划属于**增强层**，有两个部署选项：

| 选项 | 适用 | 说明 |
|---|---|---|
| 云端 API（OpenAI 兼容端点） | 联网演示 | 效果最好，推荐用于正式演示 |
| 本地模型（Ollama 等，OpenAI 兼容端点指向 `localhost:11434/v1`） | 断网演示 / 合规环境 | 治理能力不受影响，仅规划质量随模型能力变化 |

**降级关系**：LLM 不可用（断网且无本地模型）时，可切回 ScriptedPlanner——治理链路全程无感。

---

## 2. 输出契约（引用方案 §5.1，补充细节）

方案 §5.1 已定义 JSON Schema：`action`（`call_tool`/`finish`）、`tool_name`、`arguments`、`reason`，`additionalProperties: false`。补充三条实现细则：

### 2.1 校验顺序（任一失败即任务结束）

1. **JSON 可解析**：提取 LLM 输出中的第一个完整 JSON 对象（允许模型前后输出 markdown 代码块标记，正文必须恰为一个 JSON 对象）；
2. **Schema 校验**：按方案 §5.1 的 Schema；
3. **工具白名单预检**：`action=call_tool` 时，`tool_name` 必须在 `MCPGateway.list_tools(profile)` 的返回中——**这只是提前失败优化**（省去一次注定被 R2 拒绝的往返），真正的权限判定仍在 R2，两处的权威来源都是 CapabilityProfile，不会漂移。

### 2.2 映射规则

`action="finish"` → `None`（任务结束）；`action="call_tool"` → `PlannedAction(tool_name, arguments, reason)`。`task_id` / `call_id` / `agent_id` / `task_context` **LLM 不可见、不可输出**，由 `run_task` 框架组装时填充（v1.1 评审#8 已定）。

### 2.3 失败语义：不重试、不纠错

校验失败 → 记审计（`metadata.planner_error`，含失败原因与原始输出的前 200 字符）→ 返回 `None` 终止任务。**不重试、不要求模型自我修正**——理由：① 输出整形/多轮纠错逻辑本身会成为 prompt injection 的攻击面；② 演示场景失败是可控的小概率事件，静默终止比不可预测的纠错行为更安全。

---

## 3. 上下文管理（本文核心新增）

### 3.1 问题

`run_task` 每轮把全部 `observations` 交给 Planner。真实工具结果很大（`web_search` 数千字、`read_file` 整个文件），直接全量进 prompt 会：① 爆 context window；② context rot——历史越长 LLM 决策质量越差；③ 每轮重复计费相同的历史 token。

### 3.2 Prompt 组装结构（每轮固定五段）

```
[system]   角色 + 输出契约 + 软引导（见 §3.4 模板）
[context]  任务描述：Task.description 原文
[tools]    授权工具列表：MCPGateway.list_tools(profile) 的
           name + description + input_schema（MVP 仅 4 个，全量给）
[history]  执行历史：分层摘要（见 §3.3）
[ask]      "请输出下一个动作的 JSON"
```

`[tools]` 段说明：MVP 只有 4 个工具，全量注入无压力。未来工具数 > 20 时再引入"工具检索"（先选类别再给 schema），**本期不实现**——`MCPGateway.list_tools` 按 Profile 过滤本身就是第一道 context 收敛。

### 3.3 执行历史的分层摘要规则

| 历史位置 | 进 prompt 的形态 |
|---|---|
| **最近 1 步** | 完整记录：tool_name + status + reason + content（截断至 2000 字符，超出部分以 `...[truncated, total=N chars]` 标注） |
| **更早的步骤** | 一行摘要：`[n] tool_name → status：一句话描述` |

一句话摘要的生成规则（**规则生成，不用 LLM 二次加工**）：

| status | 摘要模板 |
|---|---|
| success | `成功，返回 {前 80 字符}…` |
| blocked | `被治理层拦截：{Decision.reason}` |
| error | `执行失败：{error_code}` |

**设计权衡（必须向使用者说明）**：被摘要的早期 content，LLM 在后续轮次**拿不到全文**。这影响"读到长文 → 稍后引用其细节"的场景。MVP 的解法是利用 ReAct 的就近性——引导模型在读到内容后**立即**处理（摘要、写出），prompt 模板中写明这一点（§3.4）。引用化机制（content 落盘、prompt 传路径）留作未来优化，本期不实现。

**blocked 结果必须进历史**：LLM 看到"被拦截 + 原因"后会自适应调整（例如改发内部邮箱、改为写本地文件）——这不是 bug，是**演示的核心素材**：Agent 在治理约束下找合法路径完成任务。

### 3.4 System Prompt 模板（演示用）

```
你是一个企业研究助手，通过调用工具完成用户任务。

规则：
1. 你的每次输出必须且仅为一个 JSON 对象，格式：
   {"action": "call_tool", "tool_name": "...", "arguments": {...}, "reason": "..."}
   或 {"action": "finish"}
2. 你只能使用下方列出的工具。所有工具调用都会被独立的治理层审核，
   可能被修改、拒绝或要求人工审批——这是正常流程。如果被拦截，
   阅读拦截原因，选择合法替代方案继续完成任务。
3. 工具结果很大时，请在读到内容的下一步立即处理（摘要或写出），
   历史结果之后只保留摘要。
4. 任务完成或无法继续时，输出 {"action": "finish"}。
```

---

## 4. Token 计量与预算对接

### 4.1 两条独立的计费路径（时序关键）

LLM 规划发生在 `ActionProposal` 产生**之前**，因此它的 token 消耗走不进 `Checkpoint.evaluate` 的步骤 4。预算检查实际有两个点：

```
路径 A（规划消耗）：run_task 每轮调用 LLMPlanner 前
  → BudgetLedger.check_and_reserve(task_id, 预估上限)
  → LLM 调用 → 从响应 usage 取实际值（prompt_tokens + completion_tokens）
  → commit 实际值，差额 refund

路径 B（工具消耗）：Checkpoint.evaluate 步骤 4（现有逻辑不变）
  → 按 mcp_servers.yaml 的 cost_per_call 估算
```

### 4.2 实现细则

- 预估上限：`max_tokens`（输出上限）+ 当轮 prompt 的本地估算（`len(text) // 3` 的粗估值即可）；
- 实际值来源：OpenAI 兼容端点响应的 `usage` 字段；本地模型无 usage 时回退到粗估值；
- 路径 A 超支 → 任务终止 + 审计（`metadata.planner_budget_exceeded`），不产生任何 ActionProposal；
- 两路径共享同一个 per-task 额度（`max_budget_token`），演示时把额度配足（建议 ≥ 50k），并在演示解说中展示审计事件里的真实 token 消耗——**这是"估算计费"升级为"部分真实计量"的第一步**，解说口径要准确。

---

## 5. 配置与密钥管理

新增 `config/llm_planner.yaml`：

```yaml
enabled: false                    # 默认关，演示时开；false 时 run_task 用 ScriptedPlanner
provider: openai-compatible
base_url: https://api.openai.com/v1    # 断网演示：http://localhost:11434/v1
model: gpt-4o-mini
api_key_env: LLM_API_KEY          # 只存环境变量名，不存 key 本身
max_tokens: 1000
temperature: 0.2
timeout_s: 30
```

**密钥纪律**：API key 只从环境变量读取；不落盘、不进任何 prompt、不进审计日志（`api_key` 本来就在 `masking_rules.yaml` 的字段黑名单里，双重保险）。ConfigLoader 启动校验追加一条：`enabled=true` 时 `api_key_env` 指向的环境变量必须存在。

---

## 6. 测试计划

| 测试 | 内容 | 依赖 |
|---|---|---|
| 契约解析 | 合法 JSON / markdown 包裹 JSON / 多 JSON / 非 JSON / 缺字段 / 多余字段 → 各自的映射或终止行为 | fake LLM client（返回预设字符串） |
| 工具白名单预检 | LLM 输出未授权工具名 → 终止 + 审计 | fake client |
| 摘要规则 | 三步以上历史 → 早期步骤只剩一行摘要；最近一步保留截断全文 | 纯函数测试 |
| 预算路径 A | fake client 返回带 usage 的响应 → commit 实际值；预算耗尽 → 任务终止、无 ActionProposal | fake client |
| 密钥纪律 | 审计日志全文检索不含 API key | e2e 断言 |
| 真实 API 冒烟 | 标记 `@pytest.mark.manual`，不进 CI | 真实端点 |

**纪律**：单测全部用 fake client，CI 不依赖任何真实 LLM 服务（与 FakeGateway 同理：外部服务不进 CI 回归路径）。

---

## 7. 演示检查点（接入四幕演示）

LLMPlanner 完成后，四幕演示的旁白升级为：

- **第一幕**："现在规划者是一个真实的 LLM——它自己决定先搜索、再读知识库、再写摘要。"（对比：之前是固定剧本）
- **第二幕**：审批链路不变，但触发点是 LLM **自主决定**要发邮件——"Agent 自己认为该发报告了，治理层拦下来等审批。"
- **第三幕追加亮点**：LLM 被拦截后的自适应——"发外部邮箱被拒，它自己改成了写本地报告。治理不是把 Agent 打死，是让它在规则内找路。"
- **第四幕**：审计事件中可展示每轮的真实 token 消耗。

**演示前 checklist**：`llm_planner.yaml` 的 `enabled=true`；预算额度 ≥ 50k；先用真实端点彩排两遍（LLM 行为有随机性，确认任务能收敛完成）；备好 ScriptedPlanner 作为现场降级预案。

---

## 8. 完成定义（T3.5 验收）

- [ ] §6 测试表全绿（真实 API 冒烟除外，手动执行）
- [ ] LLM 规划下示例任务端到端跑通，审批双结局均可演示
- [ ] 断网场景用本地模型（或降级 ScriptedPlanner）跑通
- [ ] KNOWN_LIMITATIONS.md 的 F4 条目更新为"已实现"
- [ ] development_log.md 追加迭代记录
