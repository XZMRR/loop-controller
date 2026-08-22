# v0.13.0 开发方案：Agent 驱动治理接口与 LangChain 适配器

> **项目定位**：Loop Controller 是企业内部 Agent 的工具调用治理基础设施，不是 Agent 开发框架，也不是面向陌生 Agent 的开放网关。
>
> **v0.13.0 目标**：把 Runtime 从"框架驱动 Agent"模式重构成"Agent 驱动治理"模式，让企业内部任何 Agent（以 LangChain 为首个示例）能够轻松接入完整 R0-R3 治理，同时让 Agent 源码与框架源码完全独立。
>
> **状态**：方案阶段。

---

## 1. 背景与定位澄清

### 1.1 项目到底是什么

Loop Controller 是企业内部的 **Agent 工具调用治理层**。

- **不是 Agent 的大脑**：Agent 怎么思考、怎么规划，由企业自己决定。
- **不是开放网关**：不面向互联网上不可信的陌生 Agent。
- **是安全运行时**：企业内部 Agent 要调工具时，必须过 Loop Controller 这一关；框架负责 R0 审批、R1 风险评估、R2 策略判定、R3 审计。

```text
企业内部 Agent（LangChain / AutoGen / OpenAI Agents / 自研）
      │
      │  "我要调 send_email(...)"
      ▼
Loop Controller 治理层
      │
      ├─ R1 Classifier：单次动作风险标签
      ├─ R2 Checkpoint：权限/预算/能力组合/Regro/审批
      ├─ R0 审批（如果需要）
      ├─ 执行
      └─ R3 审计
      │
      ▼  返回结果
企业内部 Agent 继续下一步规划
```

### 1.2 之前架构的偏差

v0.12.0 及之前，`run_task` 是核心入口，它内部掌握主循环：

```text
run_task
   │
   ├─▶ planner.next_action()
   ├─▶ classifier.classify()
   ├─▶ checkpoint.evaluate()
   ├─▶ checkpoint.forward()
   └─▶ 循环
```

这带来几个问题：

1. **框架包养了 Agent 的执行循环**：Agent 不是独立进程，而是被框架调用的模块。
2. **内置 Planner 成为产品一部分**：`ScriptedPlanner` / `LLMPlanner` 在核心包里，暗示用户"框架帮你规划"。
3. **Agent 源码与框架源码不独立**：要写一个被治理的 Agent，必须按 `Planner` Protocol 实现，嵌入框架。
4. **无法直接接入真实 Agent**：LangChain / AutoGen 等真实 Agent 有自己的执行循环，不可能被 `run_task` 驱动。

### 1.3 v0.13.0 的纠正

v0.13.0 把 Runtime 从 **"框架驱动 Agent"** 改为 **"Agent 驱动框架"**：

- Agent 自己掌握主循环。
- Agent 自己决定每一步调什么工具。
- Agent 把工具调用请求提交给 Loop Controller。
- Loop Controller 只负责治理和返回结果。

内置 Planner 从核心包移除或降级到 `examples/` / `tests/helpers/`，只作为演示/测试工具。

---

## 2. 新架构设计

### 2.1 两层结构

```text
┌─────────────────────────────────────────────┐
│  Agent 层（企业自研 / LangChain / AutoGen）    │
│  - 自己掌握任务计划                            │
│  - 自己调用 LLM / RAG / Memory                 │
│  - 只把工具调用请求发给 Loop Controller        │
└─────────────────────────────────────────────┘
                    │
                    │ Runtime API / SDK / Adapter
                    ▼
┌─────────────────────────────────────────────┐
│  Loop Controller 治理层                      │
│  - R1 Classifier：评估单次动作风险             │
│  - R2 Checkpoint：权限/预算/能力组合/审批      │
│  - R0 Approval：人工审批                        │
│  - R3 Audit：日志、哈希链、分析、告警          │
│  - MCP Gateway：执行真实工具                   │
└─────────────────────────────────────────────┘
```

### 2.2 Runtime 的新公共 API

Runtime 不再暴露 `run_task` 作为主要产品入口，而是暴露原子治理接口。

```python
class LoopController:
    """企业内部 Agent 工具调用治理入口。"""

    async def evaluate(
        self,
        agent_id: str,
        user_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        session_id: str | None = None,
        task_id: str | None = None,
        task_context: str = "",
    ) -> EvaluationResult:
        """
        对单次工具调用请求做 R1 + R2 治理判定。

        返回：
        - allow：允许执行，返回可执行的 Decision
        - deny：拒绝
        - require_approval：需要人工审批
        """

    async def execute(
        self,
        agent_id: str,
        decision: Decision,
    ) -> ToolResult:
        """
        执行一个已经通过 evaluate 或 finalize_after_approval 得到的 Decision。
        """

    async def evaluate_and_execute(
        self,
        agent_id: str,
        user_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        session_id: str | None = None,
        task_id: str | None = None,
        task_context: str = "",
    ) -> GovernanceResult:
        """
        便捷方法： evaluate + execute 一键完成。
        如果 require_approval，直接返回 require_approval 响应，不执行。
        """

    async def resume_after_approval(
        self,
        request_id: str,
    ) -> ToolResult:
        """
        在 CLI/管理员 approve 审批后，Agent 调用此方法恢复执行。
        """
```

### 2.3 Agent 侧调用模式

```python
# 企业内部 LangChain Agent
controller = build_controller(config)

while task_not_done:
    # Agent 自己决定下一步
    action = agent.decide_next_step(context)

    # 把工具调用请求交给 Loop Controller
    result = await controller.evaluate_and_execute(
        agent_id="researcher_001",
        user_id="alice",
        tool_name=action.tool_name,
        arguments=action.arguments,
        task_context=action.reason,
    )

    if result.status == "require_approval":
        # Agent 可以暂停、提示用户、或查询 approval_status
        await wait_for_approval(result.request_id)
        result = await controller.resume_after_approval(result.request_id)

    if result.status == "deny":
        # Agent 自己决定怎么处理
        agent.handle_deny(result.reason)
        continue

    # 把执行结果加入 Agent 自己的上下文
    context.add_observation(result.content)
```

### 2.4 与现有 `run_task` 的关系

`run_task` 不删除，但不再是核心产品 API。它会：

1. 移到 `examples/` 或 `src/loop_controller/_legacy_run_task.py` 作为兼容层。
2. 内部改成用新的 `LoopController.evaluate_and_execute` 循环实现。
3. 仅用于现有 e2e 测试和演示，不暴露给新用户。

---

## 3. 新增与改造模型

### 3.1 GovernanceResult

```python
class GovernanceResult(BaseModel):
    """Agent 驱动模式下，Loop Controller 对单次工具调用的响应。"""

    status: Literal["allow", "deny", "require_approval", "blocked", "error"]
    call_id: str
    tool_name: str
    arguments: dict[str, Any]
    decision: Decision | None = None  # allow 时有
    request_id: str | None = None  # require_approval 时有
    reason: str = ""
    content: Any = None  # allow 后执行成功/失败的结果内容
    error_code: str | None = None
```

### 3.2 EvaluationResult

```python
class EvaluationResult(BaseModel):
    """R1 + R2 判定结果，不含执行。"""

    status: Literal["allow", "deny", "require_approval", "blocked"]
    decision: Decision | None = None
    request_id: str | None = None
    reason: str = ""
    risk_signal: RiskSignal | None = None
```

### 3.3 现有模型的调整

- `ActionProposal`：继续作为内部结构，由 `LoopController.evaluate()` 自动构造。
- `Task`：保留，但 `LoopController` 提供 `ensure_task()` 让 Agent 不必自己创建。
- `Decision`：不变。
- `ToolResult`：不变。

---

## 4. 核心组件：LoopController

### 4.1 职责

`LoopController` 是 v0.13.0 新增的核心类，替代 `run_task` 成为 Agent 接入的主要入口。

它内部持有 `Runtime` 的所有治理能力，但不再持有 `Planner`。

### 4.2 构造

```python
async def build_controller(
    config: AppConfig,
    *,
    opa_url: str = "http://127.0.0.1:8181",
    env_extra: dict[str, str] | None = None,
) -> LoopController:
    """从 AppConfig 构造治理控制器。"""
```

### 4.3 evaluate 内部流程

```python
async def evaluate(self, agent_id, user_id, tool_name, arguments, ...):
    # 1. 获取/创建 Task、Session
    task = self._ensure_task(agent_id, user_id, task_id, session_id)

    # 2. 构造 ActionProposal
    proposal = ActionProposal(
        task_id=task.task_id,
        call_id=uuid.uuid4().hex,
        agent_id=agent_id,
        tool_name=tool_name,
        arguments=arguments,
        task_context=task_context,
    )

    # 3. R1 Classifier（可选，如果启用）
    risk_signal = await self._classifier.classify(task, agent, proposal)
    proposal = proposal.model_copy(update={
        "risk_level": risk_signal.risk_level,
        "risk_tags": risk_signal.tags,
    })

    # 4. R2 Checkpoint 判定
    agent = self._identity.get_agent(agent_id)
    decision = await self._checkpoint.evaluate(task, agent, proposal)

    # 5. 返回 EvaluationResult
    if decision.verdict == "require_approval":
        request = self._checkpoint.build_approval_request(decision, proposal, task)
        await self._approval_manager.submit(request)
        return EvaluationResult(
            status="require_approval",
            decision=decision,
            request_id=request.request_id,
        )

    if decision.verdict in ("allow", "modify"):
        return EvaluationResult(status="allow", decision=decision)

    if decision.verdict == "deny":
        return EvaluationResult(status="deny", reason=decision.reason)
```

### 4.4 execute 内部流程

```python
async def execute(self, agent_id, decision):
    proposal = self._recover_proposal(decision)
    session = self._session_manager.get(decision.session_id)
    result = await self._checkpoint.forward(proposal, decision, session_id=session.session_id)
    return result
```

---

## 5. LangChain 适配器

### 5.1 目标

v0.13.0 以 LangChain 为首个真实 Agent 框架示例，展示：

- 企业内部 LangChain Agent 如何接入 Loop Controller
- Agent 自己掌握主循环
- Loop Controller 只负责工具调用治理
- 高风险动作触发审批
- 审批通过后 Agent 继续执行

### 5.2 适配器设计

新增 `src/loop_controller/adapters/langchain.py`：

```python
class GovernedTool(BaseTool):
    """把任意 MCP/真实工具包装成受 Loop Controller 治理的 LangChain Tool。"""

    def __init__(
        self,
        controller: LoopController,
        tool_name: str,
        description: str,
        args_schema: type[BaseModel] | None = None,
    ) -> None:
        self._controller = controller
        self._tool_name = tool_name
        self._description = description
        self._args_schema = args_schema

    async def _arun(self, **kwargs) -> str:
        result = await self._controller.evaluate_and_execute(
            agent_id=self._agent_id,
            user_id=self._user_id,
            tool_name=self._tool_name,
            arguments=kwargs,
        )

        if result.status == "require_approval":
            return (
                f"[requires approval] request_id={result.request_id}. "
                "Approve via 'lc approvals approve {request_id}', then retry."
            )

        if result.status == "deny":
            return f"[denied] {result.reason}"

        return str(result.content)
```

### 5.3 演示示例

新增 `examples/langchain_agent_demo.py`：

```python
async def main():
    config = ConfigLoader().load("config")
    controller = await build_controller(config)

    # 把公司工具包装成受治理的 LangChain Tool
    tools = [
        GovernedTool(controller, "web_search", "搜索网页"),
        GovernedTool(controller, "send_email", "发送邮件"),
        GovernedTool(controller, "read_file", "读取文件"),
    ]

    # 标准 LangChain Agent
    llm = ChatOpenAI(model="gpt-4o-mini")
    agent = create_openai_tools_agent(llm, tools, prompt)
    agent_executor = AgentExecutor(agent=agent, tools=tools)

    # Agent 自己规划、自己循环；工具调用自动走 Loop Controller
    result = await agent_executor.ainvoke({"input": "查一下 AI 合规资料并发摘要给 zhang@company.com"})
    print(result)
```

### 5.4 演示要突出的治理能力

1. **A+B>C 能力组合检测**：Agent 先 `read_file` 再 `send_email`，触发 data exfil 风险，Checkpoint deny。
2. **预算控制**：多次 web_search 后预算不足，deny。
3. **高风险工具审批**：`send_email` 触发 require_approval，Agent 收到结构化响应后暂停。
4. **审计链**：每次工具调用都留下审计事件。

---

## 6. 弱化内置 Planner

### 6.1 具体动作

| 当前位置 | v0.13.0 动作 |
|---|---|
| `src/loop_controller/planner.py` | 移出核心包，放到 `tests/helpers/planner.py` 或 `examples/_demo_helpers/planner.py` |
| `runtime.py` 中的 `planner` 字段 | 从 `Runtime` dataclass 中移除 |
| `build_runtime` 的 `planner_yaml` 参数 | 移除 |
| `config/scripted_plan.yaml` | 移到 `examples/scripted_plan.yaml` |
| `src/loop_controller/llm_planner.py` | 移出核心包，放到 `examples/_demo_helpers/llm_planner.py` |

### 6.2 为什么可以移出

- `Planner` 不是治理组件。
- 企业 Agent 会自己决定计划。
- 框架内置 Planner 会误导产品定位。
- 测试和演示 still 需要，所以放到 helpers/ 里。

### 6.3 兼容层

`run_task` 作为兼容函数保留，但内部改为用 `LoopController.evaluate_and_execute` 循环：

```python
# src/loop_controller/_run_task_compat.py 或 examples/_compat.py
async def run_task(task: Task, agent: Agent, runtime: Runtime) -> TaskRunResult:
    """v0.13.0 后仅作兼容，内部调用 LoopController。"""
    controller = LoopController(runtime)
    ...
```

---

## 7. 改动面清单

| 文件 | 改动 |
|------|------|
| `src/loop_controller/controller.py` | **新增** `LoopController` 类，替代 `run_task` 成为主要入口 |
| `src/loop_controller/models.py` | 新增 `GovernanceResult`、`EvaluationResult` |
| `src/loop_controller/runtime.py` | 移除 `planner` 字段；`build_runtime` 移除 `planner_yaml`；保留其余治理能力 |
| `src/loop_controller/planner.py` | **移出**核心包，放到 `tests/helpers/` 或 `examples/_demo_helpers/` |
| `src/loop_controller/llm_planner.py` | **移出**核心包，放到 `examples/_demo_helpers/` |
| `src/loop_controller/adapters/langchain.py` | **新增** LangChain `GovernedTool` 适配器 |
| `examples/langchain_agent_demo.py` | **新增** 真实 LangChain Agent 演示 |
| `examples/research_agent_example.py` | 改写为使用 `LoopController` 而非 `run_task` |
| `examples/llm_agent_demo.py` | 改写或移到 `examples/_demo_helpers/` |
| `tests/test_controller.py` | **新增** `LoopController` 单元测试 |
| `tests/test_adapters_langchain.py` | **新增** LangChain 适配器测试 |
| `tests/test_runtime_conversation.py` | 改用 `LoopController` |
| `tests/test_e2e_research_agent.py` | 改用 `LoopController` |
| `tests/test_planner.py` | 移到 `tests/helpers/` 相关测试 |
| `config/scripted_plan.yaml` | 移到 `examples/` |
| `src/loop_controller_v0.13.0_development.md` | 本文档 |
| `src/development_log.md` | 记录 v0.13.0 架构决策 |
| `README.md` | 更新项目定位描述 |

---

## 8. 验收标准

- [ ] `pytest tests/` 全部通过；
- [ ] `ruff check src tests` 无告警；
- [ ] 新增 `test_controller.py` 覆盖 `evaluate` / `execute` / `evaluate_and_execute` / `resume_after_approval`；
- [ ] 新增 `test_adapters_langchain.py` 覆盖 LangChain 适配器 allow / deny / require_approval 三条路径；
- [ ] `examples/langchain_agent_demo.py` 可直接运行，展示 Agent 自己规划 + Loop Controller 治理；
- [ ] `examples/langchain_agent_demo.py` 能触发 `send_email` 的 require_approval；
- [ ] `examples/langchain_agent_demo.py` 能触发 `read_file` + `send_email` 的能力组合 deny；
- [ ] 内置 `ScriptedPlanner` / `LLMPlanner` 不再位于 `src/loop_controller/` 核心包内；
- [ ] `README.md` 更新为新的项目定位；
- [ ] 文档中明确说明：Loop Controller 不替 Agent 规划，只治理工具调用。

---

## 9. 风险与注意事项

1. **大量测试依赖 `run_task`**
   - `test_e2e_research_agent.py`、`test_runtime_conversation.py` 等需要重写。
   - 建议保留 `_run_task_compat` 兼容层，让测试渐进迁移。

2. **PlannedAction 的语义保留**
   - 虽然 Planner 移出，`PlannedAction` 仍作为 Agent 提交草案的结构。
   - LangChain 适配器内部把 LangChain 的 tool call 转换成 `PlannedAction`。

3. **Agent 主循环的审批恢复**
   - 审批通过后，LangChain Agent 不会自动知道。
   - 适配器需要返回明确的 `require_approval` 提示，或提供 `approval_status` 查询工具。

4. **会话和 Task 生命周期**
   - `LoopController.evaluate_and_execute` 会自动创建 Task 和 Session。
   - Agent 可以传 `session_id` 复用会话，不传则由框架分配。

5. **R1 Classifier 的位置**
   - 保留在核心包里，但它从 `run_task` 内部调用改为 `LoopController.evaluate()` 内部调用。
   - 仍然只对单次动作做风险评估，不做计划级评估。

6. **MCP Proxy 的命运**
   - v0.13.0 不动 `proxy_server.py`。
   - 它继续作为边界兼容协议存在，但不是主要接入方式。

---

## 10. 后续版本展望

| 版本 | 方向 |
|------|------|
| v0.13.0 | Agent 驱动治理接口 + LangChain 适配器 |
| v0.14.0 | AutoGen / OpenAI Agents SDK 适配器 |
| v0.15.0 | 多 Agent 协作与委托协议（企业内部场景） |
| v0.16.0 | 企业管理后台 / Web UI / 审计看板 |

---

*文档创建时间：2026-08-20（Europe/Moscow）*
