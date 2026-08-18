# Loop Controller 下一阶段开发方案（v0.3.0）

> **文档定位**：本文档是当前代码状态之后的执行计划，衔接《Loop Controller MVP 开发指南 v1.0》与《Loop Controller 方案 v1.2 增补》。
>
> - v0.1.0：MVP 主链路完成；
> - v0.2.0：P0 HMAC 信任加固 + P1 L2 会话级风险判定已完成；
> - **v0.3.0（Iteration 4/5）已执行完成**：动态会话上下文 + 真实异步审批 CLI 已落地，196 个测试通过。
>
> **状态**：已执行完成  
> **最后更新**：2026-08-18

---

## 0. 决策清单（TL;DR）

| # | 决策 | 结论 |
|---|---|---|
| D1 | 是否 push develop | **可以 push**，但必须先完成 §2 的发布前检查 |
| D2 | v0.2.0 tag 打在哪 | **打在包含 P0 修正与 P1 L2 的最终通过测试 HEAD 上**；不要打在旧 `d054fc8` 上 |
| D3 | v0.2.0 范围 | 允许包含 P0 HMAC + P1 L2；版本叙事调整为“可信执行基线” |
| D4 | 多轮对话上下文是否进 v0.2.0 | **不进**；在 v0.2.0 的已知限制中明确声明 |
| D5 | v0.3.0 主线 | **动态会话上下文 + 真实异步审批 + 策略模板** |
| D6 | 多 worker DecisionStore / RiskStateStore | **暂缓**，不在 v0.3.0 范围内；继续声明单进程假设 |
| D7 | P2 Proxy | 继续只做设计准备，**不动代码** |
| D8 | Task 是否改为可变 | **不允许**；新增 session 级 `ConversationContext` |
| D9 | Agent 自报上下文 | 继续坚持 v1.2 原则：**只能收紧，不能放宽** |

---

## 1. 当前状态与核心判断

### 1.1 当前代码状态

| 工作项 | 状态 | 说明 |
|---|---|---|
| P0 HMAC 信任加固 | 已完成并修正 | 默认 `hmac-sha256`；测试与 CI 自动注入 key；seal、域分离、混合算法拒绝、截断/伪造检测已覆盖 |
| P1 L2 会话级风险判定 | 已完成 | `SessionManager`、`RiskStateManager`、`session_risk_gate` 已落地；高风险会话下 modify 会升级为 require_approval |
| Iteration 4 动态会话上下文 | 已完成 | `ConversationContext`、`JsonlConversationStore`、`build_governance_context` 已落地 |
| Iteration 5 真实异步审批 CLI | 已完成 | `AsyncApprovalManager`、`JsonlApprovalStore`、`cli.py`、`needs_approval` 暂停态与 `resume_task` 已落地；A1–A14 验收通过 |
| 测试 | 201 passed | v0.3.0 代码已完成 |

### 1.2 已解决的核心缺口

> 多轮用户澄清后的 `task_context` 无法进入 R2 治理上下文。

该问题已在 Iteration 4 通过 `ConversationContext` + `JsonlConversationStore` + `build_governance_context` 解决：
- 同一 session 下的用户/Agent 消息按规则截断后进入 R2 policy input；
- Agent 自报内容受「只能收紧，不能放宽」原则约束，不能覆盖权威 `task_context`；
- 审计日志不保存完整对话原文。

当前未进入 v0.3.0 范围的是 **策略模板（Iteration 6）** 与 **真实 MCP 手动 gate**，不在本次交付内。

### 1.3 为什么不直接做 P2 Proxy

P2 Proxy 是正确方向，但现在做会遇到三个问题：

1. **内部 API 尚未稳定**：动态上下文会影响 `Planner`、`run_task`、`Checkpoint.evaluate` 和 `build_policy_input`；
2. **审批闭环尚未真实化**：L2 会把更多动作推向 `require_approval`，如果审批仍主要靠打桩，系统会出现“能识别风险，但不能顺畅处理风险”；
3. **Proxy 会放大现有缺口**：外部 Agent 接入后，上下文与审批语义会更复杂；内部闭环没稳定前暴露给外部，只会提前锁定一套很快要改的接口。

因此最优路线是：

```text
发布 v0.2.0
→ 补齐 v0.3.0 的真实治理闭环
→ 再进入 P2 Proxy 设计与实现
```

---

## 2. Iteration R：发布 v0.2.0（0.5-1 人日）

### 2.1 版本叙事

建议将 v0.2.0 定义为：

```text
v0.2.0 — 可信执行基线
```

它包含：

- HMAC 审计链；
- seal 记录；
- 混合算法文件拒绝；
- SessionManager；
- RiskStateManager；
- session risk score 进入 Rego；
- 高风险会话自动升级人工审批。

这不再只是“P0 加固”，而是一个可演示的安全里程碑：

> 系统不仅能拒绝单个危险动作，还能记住会话中的异常累积，并据此提高后续动作的审批等级。

### 2.2 发布前检查

#### 安全回归

- [ ] `audit_hash_algo` 默认是 `hmac-sha256`；
- [ ] `LOOP_CONTROLLER_AUDIT_HMAC_KEY` 缺失时启动 fail-closed；
- [ ] key 小于 32 字节时启动 fail-closed；
- [ ] hex/base64 非法时启动 fail-closed；
- [ ] key 不出现在日志、异常、审计事件、测试输出中；
- [ ] event key 与 seal key 使用域分离；
- [ ] 审计事件或 seal 记录包含 `key_id`；
- [ ] 修改任一审计事件后验证失败；
- [ ] 删除任一审计事件后验证失败；
- [ ] 调整事件顺序后验证失败；
- [ ] 截断文件后验证失败；
- [ ] 伪造 seal 后验证失败；
- [ ] 旧 `sha256` 文件可验证或按声明策略处理；
- [ ] 混合算法文件被拒绝启动。

#### 会话风险回归

- [ ] `runtime.create_task(...)` 自动创建或复用 session；
- [ ] 30 分钟内同 `(user_id, agent_id)` 复用 session；
- [ ] 超过 30 分钟创建新 session；
- [ ] 手工构造的伪造 `session_id` fail-closed；
- [ ] `session_id` 与 `user_id` 不匹配 fail-closed；
- [ ] `session_id` 与 `agent_id` 不匹配 fail-closed；
- [ ] deny、critical、approval denied、approval granted、low-risk success 的加减分正确；
- [ ] score clamp 在 `[0, 1]`；
- [ ] `recent_tags` 最多 10 条；
- [ ] 低风险成功不会清掉 `recent_tags`；
- [ ] 进程重启后 risk state 可通过 JSONL replay 恢复；
- [ ] 最后一行损坏时忽略并记录 WARNING；
- [ ] per-profile threshold 会进入 Rego input；
- [ ] 高 session risk 下：
  - [ ] allow → require_approval；
  - [ ] modify → require_approval；
  - [ ] deny → deny。

#### 文档回归

- [ ] `KNOWN_LIMITATIONS.md` 声明多轮对话上下文尚未进入 R2；
- [ ] `KNOWN_LIMITATIONS.md` 继续声明单进程 Runtime 假设；
- [ ] `KNOWN_LIMITATIONS.md` 声明 SSE/HTTP MCP transport 尚不支持；
- [ ] `KNOWN_LIMITATIONS.md` 声明外部 Agent 直接接入尚不支持；
- [ ] README 更新 v0.2.0 能力边界；
- [ ] 发布检查清单更新到 v0.2.0。

### 2.3 发布步骤

在 174 个测试全部通过的最终 HEAD 上执行：

```bash
git status
git log --oneline -5

# 最终回归
pytest tests/ -q

# 按发布检查清单执行真实 MCP 手动 gate

git push origin develop

git tag -a v0.2.0 <final-head> -m "v0.2.0: trusted execution baseline"
git push origin v0.2.0
```

注意：

- `<final-head>` 必须是包含 P0 修正与 P1 L2、并通过全量测试的提交；
- **不要给旧 `d054fc8` 打 tag**，因为它仍保留 `sha256` 默认策略；
- 如果最终 HEAD 不是 `d739fea`，以实际通过测试的 HEAD 为准。

### 2.4 v0.2.0 Release Note 骨架

```markdown
## v0.2.0 — 可信执行基线

### 新增

- HMAC-SHA256 审计链与 seal 记录
- 审计链篡改、截断、伪造 seal 检测
- SessionManager：同一 user/agent 的连续任务流复用 session
- RiskStateManager：会话级风险评分、持久化与重启恢复
- Rego `session_risk_gate`：高风险会话自动升级人工审批

### 已知限制

- 多轮用户澄清尚未进入 R2 的 task_context
- 当前仍假设单进程 Runtime
- 外部 Agent 需通过框架内 Planner 接入，MCP Proxy 尚未实现
- SSE/HTTP MCP transport 尚未支持
```

---

## 3. v0.3.0 总体设计

### 3.1 版本目标

将 Loop Controller 从：

```text
能治理单轮、短任务
```

升级为：

```text
能治理多轮、持续、需要人工介入的真实任务
```

建议版本叙事：

```text
v0.3.0 — 真实治理闭环
```

### 3.2 范围内

| 模块 | 内容 |
|---|---|
| 动态会话上下文 | 多轮用户输入进入治理上下文；R2 使用框架构建的上下文 |
| 真实异步审批 | 审批请求持久化、CLI 审批、重启恢复、过期语义 |
| 策略模板 | 3-5 个可直接复用的 Rego 模板 |
| 文档与示例 | 多轮上下文示例、审批示例、限制更新 |

### 3.3 范围外

| 模块 | 原因 |
|---|---|
| 多 worker DecisionStore / RiskStateStore | 当前没有真实多进程部署需求；避免拖慢核心闭环 |
| P2 Proxy 代码 | 依赖 v0.3.0 的上下文与审批语义先稳定 |
| SSE/HTTP MCP transport | 与 P2 Proxy 一并设计更合适 |
| inter_agent 治理 | 属于 P3，不在单 Agent 工具调用治理范围内 |
| Earned Authority | 依赖更成熟的会话信誉与跨 session 基线 |
| 策略加密 | 等 Proxy 引入真实外部接入后再升级为刚需 |

### 3.4 迭代总览

| 迭代 | 目标 | 预估工作量 |
|---|---|---|
| Iteration R | 发布 v0.2.0 | 0.5-1 人日 |
| Iteration 4 | 动态会话上下文 | 3-5 人日 |
| Iteration 5 | 真实异步审批 | 3-4 人日 |
| Iteration 6 | 策略模板与 v0.3.0 发布 | 2-3 人日 |

合计约 **9-13 人日**。

---

## 4. Iteration 4：动态会话上下文（3-5 人日）

### 4.1 核心设计决策

#### 决策 1：Task 保持不可变

不允许通过修改 `Task.description` 来追加上下文。

理由：

- `Task` 是审计关联的锚点；
- 可变性会破坏 trace 语义；
- 一个 session 中可能有多个 Task，用户澄清不一定属于最初 Task。

#### 决策 2：新增 session 级 ConversationContext

新增：

```text
ConversationContext(session_id)
```

它跟随 session，而不是跟随单个 task。

#### 决策 3：治理上下文由框架构建

R2 使用的 `task_context` 必须由 Runtime/Checkpoint 从以下来源确定性构建：

- 当前 `Task.description`；
- 最近 N 条用户消息；
- 最近 N 条 Agent 回复；
- 必要的截断、长度与 hash 元数据。

不允许 Agent 自报一个权威版 `task_context` 直接覆盖框架上下文。

#### 决策 4：Agent 自报内容仍然单向使用

`PlannedAction.reason` 以及未来可能的 `declared_context` 只能：

- 触发更严格审查；
- 增加审计可读性；
- 作为审批展示材料。

不得：

- 降低风险等级；
- 绕过审批；
- 把 deny 变成 allow。

### 4.2 新模型建议

新增 `ConversationMessage`：

```python
class ConversationMessage(BaseModel):
    message_id: str
    session_id: str
    task_id: str | None
    role: Literal["user", "agent"]
    content: str
    created_at: datetime
```

新增 `ConversationContext`：

```python
class ConversationContext(BaseModel):
    session_id: str
    messages: list[ConversationMessage]
    updated_at: datetime
```

约束：

- `Task` 模型不变；
- `ConversationContext` 不归 Agent 写；
- 写入入口只能是 Runtime；
- Planner 读取的是只读视图。

### 4.3 ConversationStore

新增基础设施组件：

```text
ConversationStore
```

P1 初版使用 JSONL：

```text
data/conversations.jsonl
```

配置项：

```yaml
conversation_path: "./data/conversations.jsonl"
conversation_max_messages_per_session: 100
```

行为：

- append-only；
- 启动时 replay；
- 每个 session 保留最近 N 条；
- 重启后可恢复；
- 最后一行损坏时忽略并 WARNING；
- 当前仍遵循单进程 Runtime 假设。

注意：ConversationStore 是运行状态存储，不等同于 AuditStore。它可能包含敏感对话，文件权限应与审计日志同级管理。

### 4.4 Runtime 接口调整

新增或调整：

```python
runtime.create_task(...)
runtime.add_user_message(session_id, task_id, content)
runtime.add_agent_message(session_id, task_id, content)
runtime.get_conversation_context(session_id)
```

`run_task` 启动时：

1. 校验 Task 与 session 绑定；
2. 加载 `ConversationContext`；
3. 将 context 传给 Planner；
4. 每次产生 proposal 时构建治理上下文；
5. 将治理上下文交给 Checkpoint。

### 4.5 Planner 协议调整

当前：

```python
async def next_action(
    self, task: Task, agent: Agent, observations: list[ToolResult]
) -> PlannedAction | None:
```

调整为：

```python
async def next_action(
    self,
    task: Task,
    agent: Agent,
    observations: list[ToolResult],
    conversation_context: ConversationContext,
) -> PlannedAction | None:
```

兼容策略：

- 这是受控内部协议，可以直接 breaking change；
- `ScriptedPlanner` 忽略 context 即可；
- `LLMPlanner` 使用 context 改进提示词；
- 所有 Planner 实现必须统一更新；
- 协议变更后必须跑全量 e2e。

### 4.6 R2 上下文构建规则

新增确定性构造函数，例如：

```python
build_governance_context(
    task: Task,
    conversation_context: ConversationContext,
    proposal: ActionProposal,
) -> GovernanceContext
```

建议规则：

- 当前 `Task.description` 永远置于最前；
- 纳入最近 5 条 user message；
- 纳入最近 3 条 agent message；
- R2 input 总长度默认不超过 2000 字符；
- Planner 可用更长上下文，默认不超过 8000 字符；
- 超长时使用现有规则：hash + length + preview；
- 不使用 LLM 总结 R2 实时路径。

示例：

```text
当前任务：帮我写个合规报告
用户补充：主题是 AI 数据安全
用户补充：需要包含 GDPR 和中国个保法
Agent 最近回复：我会先搜索相关资料
当前动作：web_search(...)
```

### 4.7 Rego input 兼容策略

为了不破坏现有策略：

- 顶层 `task_context` 字段保留；
- 但其内容从“初始 description 截断”升级为“动态治理上下文截断”；
- 新增 `context_meta` 字段：

```json
{
  "task_context": "...",
  "context_meta": {
    "session_id": "...",
    "message_count": 5,
    "context_length": 1832,
    "context_hash": "..."
  }
}
```

要求：

- `build_policy_input` 是唯一契约点；
- 必须新增 Python ↔ Rego contract test；
- 默认策略不因为新增字段而改变旧用例结果。

### 4.8 审计与隐私

原则：

- AuditStore 不保存完整对话；
- AuditEvent 只保存：
  - `context_hash`；
  - `context_length`；
  - 截断 preview；
- 完整对话只存在于 ConversationStore；
- ConversationStore 文件权限建议 `chmod 600`；
- 后续策略加密或敏感数据保护时，ConversationStore 一并纳入范围。

### 4.9 任务卡

| 任务 | 内容 | 预估 |
|---|---|---|
| T4.1 | 新增 `ConversationMessage` / `ConversationContext` 模型 | 0.5 人日 |
| T4.2 | 新增 `JsonlConversationStore` 与配置项 | 0.5-1 人日 |
| T4.3 | SessionManager 关联 ConversationContext | 0.5 人日 |
| T4.4 | Runtime 增加消息写入与读取接口 | 0.5 人日 |
| T4.5 | Planner 协议扩展，更新 ScriptedPlanner / LLMPlanner | 0.5-1 人日 |
| T4.6 | `build_governance_context` 与截断规则 | 0.5 人日 |
| T4.7 | `build_policy_input` 扩展与 Rego contract test | 0.5 人日 |
| T4.8 | 多轮对话示例与 e2e | 0.5-1 人日 |

### 4.10 验收标准

- [x] C1：多轮用户澄清后，R2 能看到合并后的治理上下文；
- [x] C2：Agent 不能通过自报内容覆盖权威 `task_context`；
- [x] C3：Task 保持不可变；
- [x] C4：ConversationContext 绑定 session，而不是绑定单个 task；
- [x] C5：重启后上下文可恢复；
- [x] C6：上下文超长时按规则截断；
- [x] C7：审计日志不保存完整对话；
- [x] C8：旧单轮任务行为不变；
- [x] C9：ScriptedPlanner 与 LLMPlanner 都能运行；
- [x] C10：Python ↔ Rego input contract test 通过。

---

## 5. Iteration 5：真实异步审批（3-4 人日）

### 5.1 目标

当前 `ConfigR0Delegate` 适合演示，但不是真实企业审批。v0.3.0 要实现最小但真实的异步审批闭环：

```text
动作被判为 require_approval
→ 生成持久化审批请求
→ 人类稍后 approve / deny
→ 系统根据审批结果执行或阻断
→ 全部进入审计链
```

### 5.2 新增 ApprovalStore

建议路径：

```yaml
approval_store_path: "./data/approvals.jsonl"
```

记录类型：

```text
approval_requested
approval_approved
approval_denied
approval_expired
approval_consumed
```

核心字段：

- `decision_id`
- `task_id`
- `session_id`
- `user_id`
- `agent_id`
- `tool_name`
- `arguments_masked`
- `arguments_hash`
- `risk_level`
- `risk_tags`
- `session_risk_score`
- `status`
- `requester_id`
- `approver_id`
- `created_at`
- `expires_at`
- `decided_at`
- `decision_reason`

### 5.3 审批状态机

```text
pending
  ├─ approve → approved
  ├─ deny    → denied
  └─ timeout → expired

approved
  └─ forward 成功一次 → consumed
```

规则：

- 只有 `pending` 可以被审批；
- `approved` 只能消费一次；
- `expired` 不能再审批；
- `denied` 是终态；
- `consumed` 是终态；
- 所有状态迁移追加 JSONL，不改写历史行。

### 5.4 时间语义

沿用 v1.1 的分层有效期：

- `require_approval` Decision：15 分钟；
- 审批通过后的新 allow Decision：从审批通过时刻重新起算 5 分钟；
- `max_uses = 1`；
- deny 立即过期。

审批超时后：

- 状态迁移为 `expired`；
- 原动作不得执行；
- 审计记录 `approval_expired`。

### 5.5 审批入口

v0.3.0 先提供 CLI，不做 Web UI。

建议命令：

```bash
lc approvals list
lc approvals show <decision_id>
lc approvals approve <decision_id> --approver alice --reason "..."
lc approvals deny <decision_id> --approver alice --reason "..."
```

必须校验：

- `approver_id` 存在于 IdentityProvider；
- `approver_id != requester_id`；
- `approver_id != agent_id`；
- deny 必须带 reason；
- approve 可以带 reason，建议必填。

### 5.6 通知机制

v0.3.0 只要求定义通知适配器接口：

```python
class ApprovalNotifier(Protocol):
    async def notify(self, request: ApprovalRequest) -> None:
        ...
```

初版实现：

- `LogApprovalNotifier`：写 WARNING/INFO 日志；
- 可选 `WebhookApprovalNotifier`：向配置 URL 发 HTTP POST。

IM、邮件、企业微信、飞书等具体适配器放到后续版本。

### 5.7 与现有 ConfigR0Delegate 的关系

- `ConfigR0Delegate` 已被移除，不再保留同步打桩实现；
- 统一由 `AsyncApprovalManager` + `JsonlApprovalStore` 提供真实异步审批；
- `approval.yaml` 仅用于确定 `escalation_target`（`approver`），不再控制 approve/deny 行为；
- v0.3.0 的示例默认使用 `async_store` 模式。

### 5.8 任务卡

| 任务 | 内容 | 预估 |
|---|---|---|
| T5.1 | 定义 ApprovalStore 记录模型与状态机 | 0.5 人日 |
| T5.2 | 实现 `JsonlApprovalStore` 与启动 replay | 0.5-1 人日 |
| T5.3 | Checkpoint 接入 pending approval 持久化 | 0.5 人日 |
| T5.4 | 实现 CLI approve / deny / list / show | 1 人日 |
| T5.5 | 审批通过后的单次执行与 Decision 重建 | 0.5-1 人日 |
| T5.6 | 审批过期任务与审计事件 | 0.5 人日 |
| T5.7 | ApprovalNotifier 接口与 Log/Webhook 实现 | 0.5 人日 |
| T5.8 | 重启恢复与 e2e | 0.5 人日 |

### 5.9 验收标准

- [x] A1：`require_approval` 后动作不会立即执行；
- [x] A2：审批请求持久化到磁盘；
- [x] A3：进程重启后 pending approval 仍可查询；
- [x] A4：approve 后动作只能执行一次；
- [x] A5：deny 后动作不能执行；
- [x] A6：审批超时后动作不能执行；
- [x] A7：审批人不能等于 requester；
- [x] A8：审批人不能等于 agent；
- [x] A9：deny 必须带 reason；
- [x] A10：审批 approve / deny / expire / consume 全部进入审计链；
- [x] A11：审批通过后新 allow Decision 重新起算 5 分钟；
- [x] A12：重复 approve 同一 decision 被拒绝；
- [x] A13：重复消费同一 approved decision 被拒绝；
- [x] A14：`AsyncApprovalManager` 替代 `ConfigR0Delegate`，相关单元测试通过。

---

## 6. Iteration 6：策略模板与 v0.3.0 发布（2-3 人日）

### 6.1 策略模板目标

不是做策略市场，而是提供一组企业试点可直接套用的模板。

建议首批模板：

| 模板 | 说明 | 默认动作 |
|---|---|---|
| 文件路径白名单 | 只允许访问指定目录 | deny |
| 敏感目录保护 | 禁止读写 `.ssh`、`.env`、密钥目录 | deny |
| 外部邮件保护 | 外部收件人必须审批或拒绝 | require_approval / deny |
| 高会话风险门控 | session risk 超过阈值必须审批 | require_approval |
| critical 工具门控 | critical 工具必须审批 | require_approval |

### 6.2 模板要求

每个模板必须包含：

- Rego 文件；
- 对应 profile 示例；
- 至少一个 allow 用例；
- 至少一个 deny 用例；
- 至少一个 require_approval 用例（如适用）；
- README 说明。

### 6.3 文档更新

- README：更新 v0.3.0 能力；
- KNOWN_LIMITATIONS：移除已解决项，保留新边界；
- 发布检查清单：升级为 v0.3.0；
- 示例：
  - 多轮对话上下文示例；
  - 异步审批示例；
  - session risk 示例。

### 6.4 v0.3.0 发布门槛

- [x] Iteration 4 全部验收通过；
- [x] Iteration 5 全部验收通过；
- [ ] 策略模板全部有测试；
- [x] 全量 pytest 通过；
- [ ] 真实 MCP 手动 gate 通过；
- [x] README 与 KNOWN_LIMITATIONS 已更新；
- [x] 多轮对话 demo 可演示；
- [x] 异步审批 demo 可演示。

建议 tag：

```text
v0.3.0 — 真实治理闭环
```

---

## 7. 明确暂缓项

### 7.1 多 worker 存储

暂缓内容：

- 多 worker DecisionStore；
- 多 worker RiskStateStore；
- 多 worker ApprovalStore；
- SQLite / Postgres / Redis 选型。

原因：

- 当前还没有真实多 worker 部署需求；
- 过早引入会拖慢上下文与审批闭环；
- 多 worker 是部署扩展问题，不是当前治理语义问题。

继续声明：

> 当前版本假设单进程 Runtime；违反该假设的部署不在安全承诺范围内。

触发条件：

- 出现第一个需要多副本部署的真实用户；
- P2 Proxy 服务化后需要横向扩展；
- Decision / Risk / Approval 存储出现实际并发冲突。

### 7.2 P2 Proxy 代码

当前只做设计准备，不写实现。

准备内容：

- Decision API 草案；
- 身份凭证到 `agent_id` 的映射规则；
- synthetic `task_id` / `session_id` / `call_id` 生成规则；
- Proxy 与 Checkpoint 的进程边界；
- MCP stdio / HTTP / SSE transport 选择。

P2 启动条件：

- v0.3.0 发布；
- ConversationContext 与审批状态机稳定；
- 出现第一个外来 Agent 接入需求；
- 直连网络隔离方案可落地。

### 7.3 SSE / HTTP MCP transport

当前只有 stdio。SSE/HTTP 不单独做，放入 P2 Proxy 阶段统一设计。

原因：

- 当前内部 Agent 不需要；
- 外部 Agent 接入时才需要面对 transport 多样性；
- 单独提前做会增加当前维护面。

---

## 8. 风险与坑位清单

### R1：上下文字段漂移

风险：Python 构建的 context 与 Rego 期待不一致，导致 default deny 或策略失效。

对策：

- `build_policy_input` 是唯一契约点；
- 必须维护 contract test；
- 新增字段保持向后兼容。

### R2：上下文越长越不安全

风险：把完整对话塞给 R2，引入敏感信息泄漏与策略误判。

对策：

- R2 只拿确定性截断后的治理上下文；
- AuditStore 不保存完整对话；
- ConversationStore 文件权限受控。

### R3：Agent 操纵上下文

风险：Agent 通过自报“这是低风险”来影响判定。

对策：

- 自报内容只能收紧，不能放宽；
- 权威 context 由 Runtime 构建；
- 策略评审禁止 allow 规则依赖自报内容。

### R4：审批状态机出现双写

风险：ApprovalStore 与 DecisionStore 状态不一致。

对策：

- Decision 生命周期以 DecisionStore 为准；
- ApprovalStore 只描述审批动作；
- consume 时再次校验 Decision；
- 所有状态迁移 append-only。

### R5：审批绕过

风险：高 session risk 升级为审批后，审批通过被当成永久授权。

对策：

- 审批通过后的 allow Decision 只有 5 分钟；
- `max_uses = 1`；
- consume 后不能重用。

### R6：R2 实时路径引入 LLM

风险：为了总结上下文，把 LLM 放进 R2，违背 v1.1/v1.2 原则。

对策：

- R2 实时路径禁止 LLM；
- 只做规则截断、拼接、hash；
- LLM 总结只允许用于 R1 规划或 R3 异步分析。

---

## 9. 进度跟踪表

| 迭代 | 任务 | 状态 | Commit | 测试数 | 备注 |
|---|---|---|---|---|---|
| R | v0.2.0 发布 | completed | — | — | — |
| 4 | Conversation 模型与 Store | completed | — | 201 | — |
| 4 | Planner 协议扩展 | completed | — | 201 | — |
| 4 | R2 动态治理上下文 | completed | — | 201 | — |
| 5 | ApprovalStore | completed | — | 201 | — |
| 5 | CLI 审批 | completed | — | 201 | — |
| 5 | 审批后单次执行 | completed | — | 201 | — |
| 6 | 策略模板 | pending | — | — | 未进入 v0.3.0 范围 |
| 6 | v0.3.0 发布 | pending | — | — | 待手动 gate / tag |

---

## 10. 完成定义

本方案执行完成后，Loop Controller 应具备以下能力：

1. 同一 `(user_id, agent_id)` 的多轮任务能共享 session；
2. 用户后续澄清能进入 R2 的治理上下文；
3. 高风险会话会把原本 allow/modify 的动作升级为人工审批；
4. 审批请求能跨进程重启存活；
5. 人类可以通过 CLI 真实 approve / deny；
6. 审批通过后的动作只能短期、单次执行；
7. 所有上下文、审批、执行事件均可审计；
8. 默认策略模板能覆盖最常见的企业试点场景；
9. P2 Proxy 所需的上下文、审批、Decision 生命周期语义已经稳定。

达到以上状态后，再进入 P2 Proxy 设计与实现，是最小返工路径。
