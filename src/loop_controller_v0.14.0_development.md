# v0.14.0 开发文档：彻底删除旧入口，全部测试改为 LoopController 驱动

## 1. 目标

v0.13.1 已经把 `run_task` / `resume_task` 迁出核心包，并保留了 `_run_task_compat.py` 兼容层，以及 `planner.py` / `llm_planner.py` 的弃用转发。

v0.14.0 的目标是：

- **彻底删除 `src/loop_controller/planner.py` 和 `src/loop_controller/llm_planner.py`**
- **彻底删除 `src/loop_controller/_run_task_compat.py`**
- **删除所有 `run_task` / `resume_task` 调用**
- **把所有相关测试改写成基于 `LoopController.evaluate_and_execute` / `resume_after_approval`**
- **更新/删除仍依赖旧入口的示例**
- **让 `pytest -W error::DeprecationWarning` 不再报错**

完成后，Loop Controller 的产品入口只有 `LoopController`。

## 2. 删除清单

### 2.1 核心包文件

| 文件 | 操作 | 原因 |
|---|---|---|
| `src/loop_controller/planner.py` | 删除 | 已迁出核心包，v0.13.1 起为弃用转发 |
| `src/loop_controller/llm_planner.py` | 删除 | 同上 |
| `src/loop_controller/_run_task_compat.py` | 删除 | v0.13.1 起为兼容层 |

### 2.2 测试文件改写

| 文件 | 操作 | 说明 |
|---|---|---|
| `tests/test_e2e_research_agent.py` | 重写 | 用 `LoopController` 驱动 A5/A12/A13/A14 场景 |
| `tests/test_e2e_sqlite.py` | 重写 | 用 `LoopController` 驱动 sqlite 场景 |
| `tests/test_e2e_real_mcp.py` | 重写 | 用 `LoopController` 驱动真实 MCP 场景 |
| `tests/test_runtime_conversation.py` | 重写 | 用 `LoopController` 驱动会话测试 |
| `tests/test_audit_events.py` | 重写 | 用 `LoopController` 触发并断言审计事件 |
| `tests/test_planner.py` | 删除 | 测试核心 Planner，核心包已不存在 |
| `tests/test_llm_planner.py` | 删除或迁移到 `examples/_demo_helpers/tests/` | 测试 LLMPlanner，核心包已不存在 |

### 2.3 示例文件更新

| 文件 | 操作 | 说明 |
|---|---|---|
| `examples/research_agent_example.py` | 重写或删除 | 改为 `LoopController` 驱动，或删除（保留 `examples/loop_controller_demo.py` 即可） |
| `examples/llm_agent_demo.py` | 重写 | 改为 `LoopController` + 外部 LLM Agent 驱动 |
| `examples/langchain_agent_demo.py` | 保留并更新 | 已经是 `LoopController` + LangGraph 驱动 |
| `examples/loop_controller_demo.py` | 保留 | 已经是 `LoopController` 驱动 |

### 2.4 `__init__.py` 清理

从 `src/loop_controller/__init__.py` 中移除：
- `Planner`
- `ScriptedPlanner`
- `LLMPlanner`
- `TaskRunResult`
- `UserQuestion`

## 3. 测试改写策略

### 3.1 通用辅助

新增测试辅助模块 `tests/controller_helpers.py`：

```python
import asyncio
import os
from pathlib import Path

from loop_controller.controller import LoopController, build_controller

REPO_ROOT = Path(__file__).resolve().parent.parent


def env_extra() -> dict[str, str]:
    return {"PYTHONPATH": str(REPO_ROOT / "src")}


def _set_hmac_key() -> None:
    os.environ.setdefault(
        "LOOP_CONTROLLER_AUDIT_HMAC_KEY",
        "a" * 64,
    )


async def controller_for(workdir: Path, opa_url: str) -> LoopController:
    """从临时工作目录构造 LoopController。"""
    from loop_controller.infra.config_loader import ConfigLoader

    _set_hmac_key()
    config = ConfigLoader().load(workdir / "config", opa_base_url=opa_url)
    controller = await build_controller(config, opa_url=opa_url, env_extra=env_extra())
    await controller.start()
    return controller
```

### 3.2 test_e2e_research_agent.py

原测试用 ScriptedPlanner 驱动 multi-step 任务。改写为：

```python
async def test_e2e_approve_path_event_sequence(opa_server, workdir):
    controller = await controller_for(workdir, opa_server)
    try:
        # A5: web_search allow
        r1 = await controller.evaluate_and_execute(
            agent_id="researcher_001",
            user_id="alice",
            tool_name="web_search",
            arguments={"query": "AI compliance"},
            task_context="调研 AI 合规并发送摘要邮件",
        )
        assert r1.status == "allow"

        # read_file allow
        r2 = await controller.evaluate_and_execute(
            agent_id="researcher_001",
            user_id="alice",
            tool_name="read_file",
            arguments={"path": "data/kb/ai_compliance_checklist.md"},
        )
        assert r2.status == "allow"

        # write_file allow
        r3 = await controller.evaluate_and_execute(
            agent_id="researcher_001",
            user_id="alice",
            tool_name="write_file",
            arguments={"path": "data/output/summary.md", "content": "摘要"},
        )
        assert r3.status == "allow"

        # send_email require_approval
        r4 = await controller.evaluate_and_execute(
            agent_id="researcher_001",
            user_id="alice",
            tool_name="send_email",
            arguments={"to": "zhang@company.com", "subject": "摘要", "body": "请查收"},
        )
        assert r4.status == "require_approval"

        # 模拟审批并 resume
        store = controller._runtime.approval_manager._store
        request = store.get_request(r4.decision.decision_id)
        store.record_response(
            ApprovalRecord(
                request_id=request.request_id,
                decision_id=request.decision_id,
                verdict="approve",
                approver_id="zhang_manager",
                comment="approved by e2e",
            )
        )
        final = await controller.resume_after_approval(r4.request_id)
        assert final.status == "allow"
    finally:
        await controller.aclose()
```

### 3.3 test_e2e_sqlite.py / test_e2e_real_mcp.py

同样改为 `LoopController.evaluate_and_execute` 手动驱动每一步。

### 3.4 test_runtime_conversation.py

用同一个 `session_id` 连续调用 `evaluate_and_execute`，验证会话上下文和对话历史。原 `_AskThenSearchPlanner` 可以保留在 `examples/_demo_helpers/` 中，但由测试直接按步骤调用 `LoopController`。

### 3.5 test_audit_events.py

原测试通过 `run_task` 触发事件序列。改写为直接调用 `LoopController.evaluate_and_execute`，并断言 `audit.jsonl` 中的 `propose` / `evaluate` / `execute` 事件。

由于 `LoopController` 目前不写 `task_start` / `task_end`，测试中对这些事件的断言需要移除或调整。

## 4. 设计决策

### 4.1 是否保留 `examples/research_agent_example.py`

建议**删除** `research_agent_example.py`，因为：

- `examples/loop_controller_demo.py` 已经展示了 `LoopController` 基本用法；
- `examples/langchain_agent_demo.py` 展示了真实 Agent 接入；
- `research_agent_example.py` 的 ScriptedPlanner 多步演示可以用 `loop_controller_demo.py` 替代。

如果保留，必须彻底重写为 `LoopController` 驱动。

### 4.2 是否保留 `examples/llm_agent_demo.py`

**保留并重写**。它展示了外部真实 LLM Agent 如何接入，这是核心场景。重写后：

- 仍用 DeepSeek / OpenAI 驱动 Agent 决策；
- 不再使用 `run_task`；
- Agent 自己维护 observations 和 reasoning；
- 每一步通过 `LoopController.evaluate_and_execute` 提交工具调用。

### 4.3 是否保留 `test_planner.py` / `test_llm_planner.py`

- `test_planner.py`：删除。`ScriptedPlanner` 不再是核心包组件。
- `test_llm_planner.py`：删除或迁移到 `examples/_demo_helpers/tests/`。如果 LLMPlanner 作为演示辅助保留，可以保留其测试，但必须在 `examples/_demo_helpers/` 下，不再属于核心测试。

## 5. 关键实现步骤

1. 创建 `tests/controller_helpers.py`
2. 重写 `tests/test_e2e_research_agent.py`
3. 重写 `tests/test_e2e_sqlite.py`
4. 重写 `tests/test_e2e_real_mcp.py`
5. 重写 `tests/test_runtime_conversation.py`
6. 重写 `tests/test_audit_events.py`
7. 删除 `tests/test_planner.py`
8. 迁移/删除 `tests/test_llm_planner.py`
9. 重写/删除 `examples/research_agent_example.py`
10. 重写 `examples/llm_agent_demo.py`
11. 清理 `src/loop_controller/__init__.py`
12. 删除 `src/loop_controller/planner.py`、`llm_planner.py`、`_run_task_compat.py`
13. 运行 `pytest -W error::DeprecationWarning tests/` 确保无弃用警告报错
14. 运行 `ruff check src tests examples`
15. 更新 `src/development_log.md`

## 6. 验收标准

- `pytest tests/`：**全部通过**
- `pytest -W error::DeprecationWarning tests/`：**无弃用警告报错**
- `ruff check src tests examples`：**All checks passed**
- `mypy src`：**无新增错误**（允许既有 PyYAML stub 错误）
- 核心包不再存在 `Planner`、`ScriptedPlanner`、`LLMPlanner`、`run_task`、`resume_task`
- `src/loop_controller/__init__.py` 不再 re-export Planner 相关符号
- `examples/research_agent_example.py` 要么删除，要么完全基于 `LoopController`
- `examples/llm_agent_demo.py` 完全基于 `LoopController`
