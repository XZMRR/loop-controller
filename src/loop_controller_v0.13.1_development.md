# v0.13.1 开发文档：彻底移除核心 Planner，迁移测试到 LoopController

## 1. 目标

v0.13.0 引入了 `LoopController` 作为企业对内 Agent 工具调用治理的主要入口，但核心包仍然保留 `Runtime.planner` 和 `run_task()` 循环，形成“框架驱动 Agent”与“Agent 驱动框架”并存的两套入口。

v0.13.1 的目标是：

- **从核心包彻底移除 `Planner` / `ScriptedPlanner` / `LLMPlanner`**，它们只存在于 `examples/_demo_helpers/` 中，作为演示/测试辅助；
- **从 `Runtime` 移除 `planner` 字段**，`build_runtime()` 不再接收 `planner_yaml`；
- **`run_task()` / `resume_task()` 从 `runtime.py` 移出**，放入 `src/loop_controller/_run_task_compat.py` 作为兼容层；
- **所有核心测试改写成基于 `LoopController.evaluate_and_execute`**，不再依赖 `run_task`。

完成后，Loop Controller 的对外产品 API 只有：

```python
from loop_controller.controller import LoopController, build_controller

controller = await build_controller(config)
result = await controller.evaluate_and_execute(...)
final = await controller.resume_after_approval(...)
```

## 2. 项目定位回顾

Loop Controller 是企业内部 Agent 的工具调用治理基础设施，不是 Agent 大脑。Agent 自己掌握主循环，Loop Controller 只负责：

- R1 风险分类
- R2 策略判定
- R0 审批协调
- 工具执行
- R3 审计

因此核心包不应该包含任何“帮 Agent 决定下一步”的组件。

## 3. 设计决策

### 3.1 Runtime 的职责

`Runtime` 是内部依赖容器，保存：

- `checkpoint: Checkpoint`
- `classifier: RuleBasedClassifier`
- `approval_manager: AsyncApprovalManager`
- `gateway: MCPGateway`
- `audit_store: AuditStore`
- `session_manager: SessionManager`
- `risk_manager: SessionRiskManager`
- `conversation_store: ConversationStore`
- `task_store: TaskStore`
- `reservation_store: ReservationStore`
- `audit_analyzer: AuditAnalyzer | None`
- `profiles: dict[str, CapabilityProfile]`
- `config: AppConfig`

**移除** `planner: Planner | None`。

### 3.2 build_runtime 签名变更

旧签名：

```python
async def build_runtime(
    config: AppConfig,
    *,
    opa_url: str = "http://127.0.0.1:8181",
    planner_yaml: str | Path | None = None,
    env_extra: dict[str, str] | None = None,
) -> Runtime: ...
```

新签名：

```python
async def build_runtime(
    config: AppConfig,
    *,
    opa_url: str = "http://127.0.0.1:8181",
    env_extra: dict[str, str] | None = None,
) -> Runtime: ...
```

删除 `planner_yaml` 参数及其相关分支。

### 3.3 run_task / resume_task 兼容层

将 `run_task()` 和 `resume_task()` 整体移到 `src/loop_controller/_run_task_compat.py`。

兼容层需要：

- 内部 import `ScriptedPlanner`（从 `examples/_demo_helpers/planner.py`）
- 或者保留 `src/loop_controller/planner.py` 作为转发，但不再被 `runtime.py` 引用

v0.14.0 时兼容层会彻底删除。

### 3.4 测试迁移策略

核心测试需要改写：

| 测试文件 | 改写方式 |
|---|---|
| `tests/test_e2e_research_agent.py` | 用 `LoopController` 手动驱动研究助手场景 |
| `tests/test_runtime_conversation.py` | 用 `LoopController` 驱动多轮对话 |
| `tests/test_audit_events.py` | 用 `LoopController` 触发事件并断言 |
| `tests/test_planner.py` | 删除或移到 `examples/_demo_helpers/tests/` |
| `tests/test_llm_planner.py` | 删除或移到 `examples/_demo_helpers/tests/` |

不需要改写的测试：

- `tests/test_controller.py`（v0.13.0 已新增）
- 所有 unit tests 不涉及 `run_task` 的

## 4. 关键实现步骤

### 4.1 创建 _run_task_compat.py

```python
"""run_task 兼容层（v0.13.1）。

旧入口保留到 v0.14.0，仅用于演示和过渡期内已有测试。
核心产品入口请使用 loop_controller.controller.LoopController。
"""

from __future__ import annotations

import warnings
from pathlib import Path

from loop_controller.runtime import Runtime
from loop_controller.models import Agent, Task
from loop_controller.utils.canonical import canonical_json

warnings.warn(
    "run_task 已弃用，v0.14.0 将彻底删除；请使用 LoopController 治理接口。",
    DeprecationWarning,
    stacklevel=2,
)

# 动态导入演示用 Planner，避免核心包依赖
try:
    from examples._demo_helpers.planner import Planner, ScriptedPlanner
except ImportError:
    from loop_controller.planner import Planner, ScriptedPlanner  # type: ignore

async def run_task(task: Task, agent: Agent, runtime: Runtime) -> None: ...
async def resume_task(task: Task, agent: Agent, runtime: Runtime, *, ...) -> None: ...
```

### 4.2 修改 runtime.py

1. 从 `Runtime` dataclass 删除 `planner` 字段
2. 从 `build_runtime` 删除 `planner_yaml` 参数
3. 删除 `run_task()` / `resume_task()` 函数
4. 删除 `from loop_controller.planner` 和 `from loop_controller.llm_planner` import
5. 保留 `create_task()` / `get_task()` / `start()` / `aclose()` 等辅助方法

### 4.3 改写核心测试

#### test_e2e_research_agent.py

用 `LoopController` 实现 A5/A12/A13/A14 场景：

```python
controller = await build_controller(config, opa_url=opa_server, env_extra=_env_extra())

# A5: web_search allow
result = await controller.evaluate_and_execute(...)
assert result.status == "allow"

# A12: send_email require_approval
result = await controller.evaluate_and_execute(...)
assert result.status == "require_approval"
# 模拟审批
final = await controller.resume_after_approval(result.request_id)
assert final.status == "allow"

# A13: 读取 kb 后尝试发外部邮件 → deny（能力组合规则）
...

# A14: 未知工具 deny
...
```

#### test_runtime_conversation.py

用同一个 `session_id` 连续调用 `evaluate_and_execute`，验证会话上下文和对话历史。

#### test_audit_events.py

用 `LoopController` 触发 propose/evaluate/execute/task_start/task_end 事件，断言审计日志字段。

### 4.4 清理 examples

- `examples/research_agent_example.py`：继续使用兼容层 `run_task`，import 改为 `from examples._demo_helpers.planner import ScriptedPlanner`
- `examples/llm_agent_demo.py`：继续使用兼容层，import LLMPlanner 改为 `from examples._demo_helpers.llm_planner import LLMPlanner`
- 或者统一改为直接使用 `LoopController`

## 5. 文件变更清单

| 文件 | 变更 |
|---|---|
| `src/loop_controller/runtime.py` | 移除 planner 字段、build_runtime planner_yaml 参数、run_task/resume_task |
| `src/loop_controller/_run_task_compat.py` | 新增兼容层 |
| `src/loop_controller/planner.py` | 保留为转发/弃用 shim |
| `src/loop_controller/llm_planner.py` | 保留为转发/弃用 shim |
| `tests/test_e2e_research_agent.py` | 重写为 LoopController 测试 |
| `tests/test_runtime_conversation.py` | 重写为 LoopController 测试 |
| `tests/test_audit_events.py` | 重写为 LoopController 测试 |
| `tests/test_planner.py` | 删除或迁移 |
| `tests/test_llm_planner.py` | 删除或迁移 |
| `examples/research_agent_example.py` | import 路径调整 |
| `examples/llm_agent_demo.py` | import 路径调整 |
| `src/development_log.md` | 记录 v0.13.1 |

## 6. 验收标准

- `pytest tests/` 全部通过
- `ruff check src tests examples` 全绿
- `mypy src` 无新增错误（允许既有 PyYAML stub 错误）
- 以下 import 不再出现在核心包中：
  - `from loop_controller.planner import ...`
  - `from loop_controller.llm_planner import ...`
- `examples/research_agent_example.py` 仍能运行成功
- `examples/langchain_agent_demo.py` 仍能运行成功
