# v0.16.0 开发文档：通用 Python 治理层 + 适配器重构

## 1. 目标

v0.15.0 已经为 LangChain / OpenAI Agents SDK / AutoGen 提供了适配器，但三个适配器重复了同一套核心逻辑：

```python
result = await controller.evaluate_and_execute(
    agent_id=..., user_id=..., tool_name=..., arguments=...
)
return format_governance_result(result)
```

v0.16.0 的目标：

1. **抽出通用 Python 治理层 `ToolGovernor`**：
   - 与具体 Agent 框架无关
   - 直接面向企业内部自定义 Python Agent
   - 为所有框架适配器提供统一内部抽象
2. **重构三个现有适配器**，让它们内部调用 `ToolGovernor`
3. **保持向后兼容**：现有适配器 API 不变
4. **提供裸 Python Agent 使用示例**

## 2. 设计

### 2.1 ToolGovernor 位置与导出

新增 `src/loop_controller/tool_governor.py`，并在 `src/loop_controller/__init__.py` 中导出 `ToolGovernor`。

### 2.2 ToolGovernor API

```python
from loop_controller import ToolGovernor

governor = ToolGovernor(
    controller,
    agent_id="researcher_001",
    user_id="alice",
    default_task_context="",
)

# Agent 主循环中直接调用
result = await governor.call(
    "send_email",
    {"to": "zhang@company.com", "subject": "摘要", "body": "请查收"},
    task_context="发送调研摘要",
    session_id="s-001",
)
# result 是 str，可直接返回给 Agent
```

设计要点：

- 构造时固定 `agent_id` / `user_id` / `default_task_context`
- `call()` 时传入 `tool_name` 和 `arguments`
- 可选覆盖 `task_context`、`session_id`、`task_id`
- 返回统一格式化后的字符串，与现有适配器行为一致

### 2.3 适配器重构

| 适配器 | 变更 |
|---|---|
| `adapters/langchain.py` | `govern_tool` 内部创建 `ToolGovernor`，`_governed` 调用 `governor.call(...)` |
| `adapters/openai_agents.py` | `govern_function_tool` 内部创建 `ToolGovernor`，`_wrapped` 调用 `governor.call(...)` |
| `adapters/autogen.py` | `govern_tool` 内部创建 `ToolGovernor`，`_wrapped` 调用 `governor.call(...)` |
| `adapters/_shared.py` | 保留 `format_governance_result`，也可由 `ToolGovernor` 内部复用 |

### 2.4 向后兼容

- `govern_tool(...)`、`govern_function_tool(...)` 签名不变
- 返回值类型不变
- 现有示例和测试不需要修改

## 3. 新增文件

| 文件 | 说明 |
|---|---|
| `src/loop_controller/tool_governor.py` | `ToolGovernor` 通用治理层 |
| `examples/raw_python_agent_demo.py` | 裸 Python Agent 直接调用 `ToolGovernor` |
| `tests/test_tool_governor.py` | `ToolGovernor` 单元测试 |

## 4. 修改文件

| 文件 | 说明 |
|---|---|
| `src/loop_controller/__init__.py` | 导出 `ToolGovernor` |
| `src/loop_controller/adapters/langchain.py` | 使用 `ToolGovernor` |
| `src/loop_controller/adapters/openai_agents.py` | 使用 `ToolGovernor` |
| `src/loop_controller/adapters/autogen.py` | 使用 `ToolGovernor` |
| `src/development_log.md` | 追加 v0.16.0 记录 |
| `src/loop_controller_v0.16.0_development.md` | 本文档 |

## 5. 测试策略

1. `test_tool_governor.py`：
   - Mock `LoopController`
   - 验证 `ToolGovernor.call()` 正确转发参数
   - 验证返回字符串格式
   - 验证 `default_task_context` 可被单次调用覆盖

2. 现有适配器测试：
   - 保持不变，验证重构后行为一致
   - AutoGen 适配器测试继续用 mock controller
   - OpenAI Agents 测试继续 skip（未安装依赖）

3. 端到端测试：
   - 运行完整 `pytest -W error::DeprecationWarning tests/`

## 6. 验收标准

- [x] `ToolGovernor` 实现并通过 ruff/mypy
- [x] 三个适配器重构后测试仍然通过
- [x] 新增裸 Python Agent 示例
- [x] `pytest -W error::DeprecationWarning tests/` 全部通过
- [x] `ruff check src tests examples` 通过
- [x] `mypy src` 无新增错误
- [x] `development_log.md` 追加 v0.16.0 记录
- [x] 现有适配器 API 保持向后兼容

## 7. 后续铺垫

v0.16.0 完成后，通用治理层成为内部核心抽象。v0.17.0 服务化时：

```python
@app.post("/v1/govern/tool-call")
async def govern_endpoint(req: GovernRequest) -> str:
    return await tool_governor.call(
        req.tool_name,
        req.arguments,
        task_context=req.task_context,
        session_id=req.session_id,
    )
```

`ToolGovernor.call()` 可以直接被 HTTP 服务复用。
