"""通用 Python 治理层（v0.16.0）。

``ToolGovernor`` 是与具体 Agent 框架无关的 Python API：
企业内部自定义 Python Agent 可以直接实例化它，在每次调用工具时把请求
提交给 ``LoopController`` 做 R1/R2/R3 治理。

它也是所有框架适配器（LangChain / OpenAI Agents / AutoGen）的内部抽象，
避免每个适配器重复同一套 ``evaluate_and_execute + format_governance_result`` 逻辑。
"""

from __future__ import annotations

from typing import Any

from loop_controller.adapters._shared import format_governance_result
from loop_controller.controller import LoopController


class ToolGovernor:
    """通用工具调用治理入口。

    构造时固定 ``agent_id`` / ``user_id`` / ``default_task_context``；
    每次 ``call()`` 把 ``tool_name`` + ``arguments`` 提交给 ``LoopController``，
    返回可直接返回给 Agent 的自然语言字符串。

    Args:
        controller: LoopController 实例。
        agent_id: 默认 agent_id，会传给每次 ``evaluate_and_execute``。
        user_id: 默认 user_id，会传给每次 ``evaluate_and_execute``。
        default_task_context: 默认任务上下文；单次 ``call()`` 可覆盖。
    """

    def __init__(
        self,
        controller: LoopController,
        agent_id: str,
        user_id: str,
        *,
        default_task_context: str = "",
    ) -> None:
        self._controller = controller
        self._agent_id = agent_id
        self._user_id = user_id
        self._default_task_context = default_task_context

    async def call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        task_context: str | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
    ) -> str:
        """提交一次工具调用给 Loop Controller 治理。

        Args:
            tool_name: Loop Controller 内部 canonical_name，如 ``send_email``。
            arguments: 工具参数。
            task_context: 任务上下文；不指定则使用构造时的默认值。
            session_id: 可选 Session ID。
            task_id: 可选 Task ID。

        Returns:
            给 Agent 阅读的自然语言结果：执行结果、审批提示、拒绝原因或错误信息。
        """
        result = await self._controller.evaluate_and_execute(
            agent_id=self._agent_id,
            user_id=self._user_id,
            tool_name=tool_name,
            arguments=dict(arguments),
            task_context=task_context if task_context is not None else self._default_task_context,
            session_id=session_id,
            task_id=task_id,
        )
        return format_governance_result(result)
