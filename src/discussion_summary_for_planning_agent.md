# 与代码 agent 讨论摘要（供策划 agent 审阅）

## 当前代码状态

- **P0 HMAC 信任加固**：已完成并修正（commit `d739fea`）。
  - `ConfigLoader.load()` 默认 `audit_hash_algo=hmac-sha256`。
  - 测试与 CI 自动注入 `LOOP_CONTROLLER_AUDIT_HMAC_KEY`。
  - 审计链新增 seal 记录、域分离、混合算法文件拒绝启动、截断/伪造 seal 检测。
- **P1 L2 会话级风险判定**：已完成（commit `db53736`）。
  - 新增 `SessionManager`、`RiskStateManager`、`session_risk_gate`。
  - modify 在高 session_risk 时升级为 require_approval。
- **测试**：174 个测试全部通过，未 push develop。

---

## 讨论主题与结论

### 1. Loop Controller 当前如何处理不同架构的 Agent

**核心定位**：Loop Controller 是一个**动作级治理层**。不管什么 Agent 架构（Scripted、LLM、ReAct、Plan-and-Execute），只要它能按协议吐出"下一步工具调用"，Loop Controller 就能治理。

**关键接口**：`Planner` Protocol（`src/loop_controller/planner.py`）

```python
class Planner(Protocol):
    async def next_action(
        self, task: Task, agent: Agent, observations: list[ToolResult]
    ) -> PlannedAction | None: ...
```

- Agent 只需要返回 `PlannedAction(tool_name, arguments, reason)`；
- 框架自动补全 `call_id/task_id/agent_id`，自动跑分类器，自动组装 R2 表单。

**当前支持形态**：

| 架构 | 支持状态 | 说明 |
|---|---|---|
| Scripted（脚本化） | ✅ 默认 | `ScriptedPlanner` 按 YAML 步骤执行 |
| LLM 单步规划 | ✅ 可选 | `LLMPlanner` 调用 LLM 输出下一步动作 |
| ReAct | ⚠️ 能跑但 thought 不可见 | thought 被吞在 LLMPlanner 内部，外部只能看到 action |
| Plan-and-Execute | ⚠️ 能跑但计划不可见 | 计划阶段若映射为 tool_call 可见，否则内部消化 |
| Multi-Agent | ❌ 不支持 | 只有一个 `planner` 槽位，没有 Agent 间消息总线 |
| 外部 Harness（LangChain/Bedrock） | ❌ 不支持直接接入 | 必须让 Harness 把动作送进来，或走 MCP Proxy |
| SSE/HTTP MCP transport | ❌ 不支持 | 当前只有 stdio transport |

**结论**：当前只落地了"Loop Controller 自己当司机"的形态，外部已有 Agent/harness 不能直接把动作送进来被治理。P2 Proxy 就是为此设计。

---

### 2. R2 表单字段来源

**Checkpoint.evaluate 输入**（`src/loop_controller/checkpoint.py#L218`）：

```python
async def evaluate(self, task: Task, agent: Agent, proposal: ActionProposal) -> Decision
```

**最终给 Rego/OPA 的 JSON 文档**（`src/loop_controller/policy_engine.py#L79-L118`）：

```json
{
  "tool_name": "...",         // Agent 决定
  "arguments": {...},         // Agent 决定
  "risk_level": "...",        // 框架决定（分类器）
  "risk_tags": [...],         // 框架决定（分类器）
  "task_context": "...",      // 框架决定（Task.description 截断）
  "agent": {                  // 框架决定（agents.yaml）
    "agent_id": "...",
    "owner_id": "..."
  },
  "profile": {                // 框架决定（profiles.yaml）
    "tools": {...}
  },
  "session_risk": {...}       // 框架决定（RiskStateManager）
}
```

**Agent 可控字段**：只有 `tool_name`、`arguments`、`reason`。
**框架可控字段**：`risk_level`、`risk_tags`、`task_context`、`agent` 身份、`profile` 权限、`session_risk`。

---

### 3. Agent 接入方式：主动 vs 被动

#### 方式 A：Loop Controller 内部 Agent（当前支持）

Agent 实现 `Planner` 协议，由 `run_task` 框架驱动。Agent 不需要知道 R2 存在。

```text
run_task → Agent.next_action() → ActionProposal → Classifier → Checkpoint → MCP
```

#### 方式 B：外部 Agent 主动接入（P2）

外部 Agent 修改自身代码，每次调用工具前先请求 Loop Controller。

```text
外部 Agent → Proxy.evaluate(agent_id, tool_name, arguments, task_context)
              ↓
           Decision
              ↓
           allow → 调 MCP
           deny  → 返回错误
```

#### 方式 C：外部 Agent 被动接入（P2 MCP Proxy）

外部 Agent 无感知，以为自己直连 MCP server，实际先打到 Proxy。

```text
外部 Agent → 以为在调 MCP Server
              ↓
           MCP Proxy
              ↓
           Checkpoint.evaluate()
              ↓
           allow → 转发真实 MCP Server
           deny  → 返回错误
```

**结论**：当前只支持方式 A；P2 需要实现方式 B/C。不存在"既不改 Agent 代码、又不建 Proxy"的治理方式。

---

### 4. 上下文管理缺口（关键问题）

#### 当前框架的上下文

| 上下文类型 | 动态/静态 | 说明 |
|---|---|---|
| 输入上下文（Task.description） | 静态 | 只包含用户第一句话 |
| R2 task_context | 静态 | `Task.description[:200]` |
| 工具调用上下文（observations） | 动态 | 只含工具执行结果 |
| 会话风险上下文（session_risk） | 动态 | 风险分数/标签累计 |

#### 缺失的上下文

- **用户后续输入**：Agent 多轮询问用户后，后续澄清信息不会进入 `task_context` 或 `observations`。
- **对话历史**：Agent 与用户的完整对话链没有保存。
- **Agent 内部推理过程**：ReAct 的 thought、Plan-and-Execute 的计划。

#### 影响示例

```text
用户：帮我写个合规报告
Agent：关于哪个主题？
用户：AI 数据安全
Agent：需要哪些法规？
用户：GDPR 和中国个保法
Agent：好的，我搜索资料 → 调用 web_search
```

调用 `web_search` 时，R2 的 `task_context` 仍然是：

```text
"帮我写个合规报告"
```

而不是：

```text
"帮我写个关于 AI 数据安全、包含 GDPR 和中国个保法的合规报告"
```

#### 可能修复方案

1. **扩展 Task 允许更新 description**：破坏不可变性，不推荐。
2. **引入 ConversationContext**：新增 `messages` 字段，传递给 Planner 和 Checkpoint，动态生成 `task_context`。
3. **Agent 提供 task_context**：把 `task_context` 放入 `PlannedAction`，由 Agent 主动总结。风险是 Agent 可操纵上下文。
4. **Hybrid**：框架维护对话历史，Agent 决定何时触发工具调用，R2 从对话历史中自动提取最新上下文。

**结论**：当前 MVP 默认假设"任务描述一次性给全"，多轮交互上下文是明确缺口，需要后续版本处理。

---

## 需要策划 agent 决策的问题

1. **是否现在 push develop？**
   - 当前本地 develop 领先远程，包含 P0/P1 修正，174 测试通过。

2. **v0.2.0 tag 打在哪？**
   - 方案 A：只给 P0 commit `d054fc8` 打 v0.2.0，P1 作为 v0.3.0；
   - 方案 B：给修正后的 `d739fea` 打 v0.2.0（P0 + P1 合并发布）。

3. **下一步优先级**：
   - A. 发布 v0.2.0（push + tag + 更新发布检查清单）；
   - B. 补全上下文管理（多轮对话上下文）；
   - C. 其他文档/示例完善；
   - D. 开始 P2 Proxy 设计（策划 agent 此前说"不要动代码，只记录原则"）。

4. **多轮对话上下文是否纳入 v0.2.0 范围？**
   - 如果纳入，需要修改 `Planner` 协议、`run_task`、`Checkpoint.evaluate`、`build_policy_input`。
   - 如果不纳入，需在 `KNOWN_LIMITATIONS.md` 或 `development_log.md` 中明确声明。

---

## 建议

代码 agent 倾向：

1. **先 push develop**（安全且已完成）；
2. **v0.2.0 打在 `d739fea`**（P0 + P1 合并发布，故事更完整）；
3. **多轮对话上下文不纳入 v0.2.0**，作为 v0.3.0 或 post-MVP 方向；
4. **P2 Proxy 按策划 agent 要求，继续只记录原则、不动代码**。
