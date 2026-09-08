# v0.15.0 开发文档：接入更多 Agent 框架

## 1. 目标

v0.14.0 完成后，Loop Controller 的产品入口只剩 `LoopController`：企业内部 Agent 自己掌握主循环，每次工具调用前把请求提交给 Loop Controller 做 R1/R2/R3 治理。

v0.13.0 已提供 **LangChain / LangGraph 适配器**（`src/loop_controller/adapters/langchain.py`）。v0.15.0 的目标是把同样的治理接入能力扩展到另外两个主流 Agent 框架：

- **OpenAI Agents SDK**（`openai-agents`）
- **AutoGen**（`autogen-agentchat >= 0.4`）

完成后，企业内部使用不同框架构建的 Agent 都能通过受治理的工具调用接入 Loop Controller，无需改动核心治理逻辑。

## 2. 设计原则

1. **Agent 自己掌握主循环**：Loop Controller 不替 Agent 做计划、不维护 Agent 状态。
2. **按次治理**：每个 tool call 独立走 `evaluate_and_execute`。
3. **可选依赖**：适配器依赖的框架包不进核心依赖，全部放到 `[project.optional-dependencies]`。
4. **与 LangChain 适配器保持一致 API 风格**：
   - 工厂函数命名：`govern_xxx(...)`
   - 输入：`controller`, `tool_name`, `description`, `agent_id`, `user_id`
   - 输出：框架可直接注册的工具对象/函数
   - 审批/阻断结果以自然语言字符串返回给 Agent

## 3. 新增文件清单

### 3.1 核心适配器

| 文件 | 说明 |
|---|---|
| `src/loop_controller/adapters/openai_agents.py` | OpenAI Agents SDK `@function_tool` 风格适配器 |
| `src/loop_controller/adapters/autogen.py` | AutoGen `register_function` 风格适配器 |
| `src/loop_controller/adapters/_shared.py` | 共享的结果格式化辅助（可选，如两个适配器都依赖） |

### 3.2 示例

| 文件 | 说明 |
|---|---|
| `examples/openai_agents_demo.py` | OpenAI Agents SDK Agent 接入演示 |
| `examples/autogen_agent_demo.py` | AutoGen Agent 接入演示 |

### 3.3 测试

| 文件 | 说明 |
|---|---|
| `tests/test_adapter_openai_agents.py` | OpenAI Agents 适配器单元测试（mock LoopController） |
| `tests/test_adapter_autogen.py` | AutoGen 适配器单元测试（mock LoopController） |

## 4. 适配器设计

### 4.1 OpenAI Agents SDK

OpenAI Agents SDK 中，工具是一个被 `@function_tool` 装饰的函数；Agent 通过 `Runner.run()` 自动触发函数调用。

我们提供：

```python
from loop_controller.adapters.openai_agents import govern_function_tool

tools = [
    govern_function_tool(
        controller,
        "send_email",
        "发送邮件（高风险，需要审批）",
        agent_id="researcher_001",
        user_id="alice",
    )(send_email_impl),  # 或直接写一个函数被装饰
]
```

实现要点：
- 内部用 `agents.function_tool` 装饰器保留原始函数签名与 schema。
- 实际执行时调用 `controller.evaluate_and_execute(...)`。
- 对 `require_approval` / `deny` / `error` 返回结构化自然语言提示。

### 4.2 AutoGen

AutoGen 中，Agent 通过 `register_function` 或直接把函数放进 `tools` 列表来调用。

我们提供：

```python
from loop_controller.adapters.autogen import govern_tool

@ govern_tool(controller, "send_email", agent_id="researcher_001", user_id="alice")
async def send_email(to: str, subject: str, body: str) -> str:
    """发送邮件。"""
    ...  # 实际不会被调用，schema 仅用于 AutoGen 识别
```

或：

```python
send_email_governed = govern_tool(controller, "send_email", agent_id="...", user_id="...")(send_email)
assistant.register_function(send_email_governed)
```

实现要点：
- 用 `functools.wraps` 保留原始函数签名与 docstring，让 AutoGen 正确生成工具 schema。
- 实际调用时忽略原始函数实现，转发给 `controller.evaluate_and_execute`。
- 返回字符串结果给 AutoGen。

## 5. 依赖配置

在 `pyproject.toml` 增加：

```toml
[project.optional-dependencies]
langchain = [
    "langchain>=0.3",
    "langchain-openai>=0.2",
    "langgraph>=0.3",
]
openai-agents = [
    "openai-agents>=0.1",
    "openai>=1.70",
]
autogen = [
    "autogen-agentchat>=0.4",
]
```

并考虑新增一个 `all-adapters` 组方便一键安装：

```toml
all-adapters = [
    "loop-controller[langchain,openai-agents,autogen]",
]
```

## 6. 测试策略

由于 `openai-agents` 和 `autogen` 可能不是每个环境都安装，测试需要：

1. 用 `pytest.importorskip(...)` 跳过未安装框架的测试。
2. 不依赖真实 LLM；构造一个 `MockLoopController`：
   - `evaluate_and_execute` 返回预定义 `GovernanceResult`
   - 验证调用参数正确
3. 验证装饰后的工具：
   - 保留原始函数名和 docstring
   - 保留参数 schema（OpenAI Agents SDK 可 introspect）
   - 调用后返回字符串结果

## 7. 验收标准

- [x] `src/loop_controller/adapters/openai_agents.py` 存在且 ruff/mypy 通过
- [x] `src/loop_controller/adapters/autogen.py` 存在且 ruff/mypy 通过
- [x] `examples/openai_agents_demo.py` 和 `examples/autogen_agent_demo.py` 可运行（在未安装对应包时优雅降级）
- [x] 测试覆盖两种适配器的核心路径，未安装依赖时自动 skip
- [x] `pytest -W error::DeprecationWarning tests/` 全部通过
- [x] `ruff check src tests examples` 通过
- [x] `mypy src` 无新增错误
- [x] `development_log.md` 追加 v0.15.0 记录

## 8. 潜在风险

- OpenAI Agents SDK 目前版本迭代较快，`@function_tool` API 可能变化；实现时做兼容处理。
- AutoGen 0.4+ 的 API 与旧版差异较大；文档和适配器都基于 `autogen-agentchat>=0.4`。
- 两个框架对 async tool 的支持不同；适配器内部统一使用 `async def`。
- 不强制把这些框架加入 CI 安装，避免 CI 体积膨胀；核心验证通过 mock 完成。
