"""Planner（§5.1）：决定 R1 的下一个动作。

非治理组件，不参与任何判定。MVP 提供 ``ScriptedPlanner``（默认，行为完全确定），
``LLMPlanner`` 迭代 3 再做（演示增强，非治理能力）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

import yaml

from loop_controller.models import (
    Agent,
    ConversationContext,
    PlannedAction,
    Task,
    ToolResult,
    UserQuestion,
)


@runtime_checkable
class Planner(Protocol):
    """决定 R1 的下一个动作；返回 None 表示任务完成（§5.1）。

    v1.1（评审#7/#8）：只输出**动作草案** ``PlannedAction``，不含 call_id/task_id/
    agent_id——这些身份字段由 run_task 框架统一生成/填充，Planner 无权自定。

    T3.5：``next_action`` 改为 async，以便 LLMPlanner 调用 ``MCPGateway.list_tools``。

    v0.3.0 Iteration 4：新增 ``conversation_context``；返回类型扩展为
    ``PlannedAction | UserQuestion | None``，显式表达需要用户补充输入。
    """

    async def next_action(
        self,
        task: Task,
        agent: Agent,
        observations: list[ToolResult],
        conversation_context: ConversationContext,
    ) -> PlannedAction | UserQuestion | None: ...


class ScriptedPlanner:
    """从 YAML 脚本读取预定义动作序列，逐一发出（§5.1）。

    行为完全确定、可复现：治理链路的每个分支都可精确触发。
    忽略 ``observations``（脚本不依赖前序结果；LLMPlanner 才会利用）。
    """

    def __init__(self, steps: list[PlannedAction]) -> None:
        self._steps = list(steps)
        self._index = 0

    @classmethod
    def from_yaml(cls, path: str | Path) -> "ScriptedPlanner":
        """从 ``scripted_plan.yaml`` 加载步骤序列。"""
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        steps = [PlannedAction(**step) for step in data.get("steps", [])]
        return cls(steps)

    async def next_action(
        self,
        task: Task,
        agent: Agent,
        observations: list[ToolResult],
        conversation_context: ConversationContext,
    ) -> PlannedAction | UserQuestion | None:
        if self._index >= len(self._steps):
            return None
        step = self._steps[self._index]
        self._index += 1
        return step
