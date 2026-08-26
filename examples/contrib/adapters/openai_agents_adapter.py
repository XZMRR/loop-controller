"""OpenAI Agents SDK 适配器示例：把 Loop Controller 包装成 SDK 工具（v0.15.0）。

本模块已从 Loop Controller 核心包移出，仅作为示例和迁移便利。
需要额外安装 ``openai-agents``：

    uv pip install openai-agents openai

使用方式：

    from agents import Agent, Runner
    from openai_agents_adapter import govern_function_tool
    from loop_controller.controller import build_controller

    controller = await build_controller(config)

    @govern_function_tool(
        controller,
        "send_email",
        "发送邮件（高风险，需要人工审批）",
        agent_id="researcher_001",
        user_id="alice",
    )
    async def send_email(to: str, subject: str, body: str) -> str:
        \"\"\"发送邮件。\"\"\"
        return "placeholder"

    agent = Agent(name="researcher", instructions="...", tools=[send_email])
    result = await Runner.run(agent, "帮我查资料并发邮件给 zhang@company.com")

Agent 自己掌握主循环，Loop Controller 只负责每次 tool call 的治理。
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any, cast

from loop_controller.controller import LoopController
from loop_controller.tool_governor import ToolGovernor

try:
    from agents import function_tool
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "使用 loop_controller.adapters.openai_agents 需要先安装 openai-agents: "
        "uv pip install 'loop-controller[openai-agents]'"
    ) from exc


_F = Callable[..., Any]


def govern_function_tool(
    controller: LoopController,
    tool_name: str,
    description: str,
    agent_id: str,
    user_id: str,
) -> Callable[[_F], _F]:
    """把 Loop Controller 治理下的工具包装成 OpenAI Agents SDK ``@function_tool``。

    Args:
        controller: LoopController 实例。
        tool_name: Loop Controller 内部 canonical_name，如 ``send_email``。
        description: Agent 看到的工具描述。
        agent_id: 默认 agent_id。
        user_id: 默认 user_id。

    Returns:
        一个装饰器；装饰函数后返回可被 ``Agent(tools=[...])`` 使用的工具对象。
    """

    governor = ToolGovernor(controller, agent_id=agent_id, user_id=user_id)

    def decorator(fn: _F) -> _F:
        # 保留原始函数签名、注解和 docstring，让 agents.function_tool 正确生成 schema
        @function_tool(name_override=tool_name, description_override=description)
        @functools.wraps(fn)
        async def _wrapped(**kwargs: Any) -> str:
            return await governor.call(tool_name, kwargs)

        return cast(_F, _wrapped)

    return decorator
