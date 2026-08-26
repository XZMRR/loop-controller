"""AutoGen 适配器示例：把 Loop Controller 包装成 AutoGen Agent 可调用的工具（v0.15.0）。

本模块已从 Loop Controller 核心包移出，仅作为示例和迁移便利。
需要额外安装 ``autogen-agentchat``：

    uv pip install autogen-agentchat

使用方式：

    from autogen_agentchat.agents import AssistantAgent
    from autogen_adapter import govern_tool
    from loop_controller.controller import build_controller

    controller = await build_controller(config)

    @govern_tool(controller, "send_email", agent_id="researcher_001", user_id="alice")
    async def send_email(to: str, subject: str, body: str) -> str:
        \"\"\"发送邮件（高风险，需要人工审批）。\"\"\"
        return "placeholder"

    agent = AssistantAgent(
        name="researcher",
        model_client=...,
        tools=[send_email],
    )

    await agent.run(task="帮我查资料并发邮件给 zhang@company.com")

Agent 自己掌握主循环，Loop Controller 只负责每次 tool call 的治理。
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from typing import Any

from loop_controller.controller import LoopController
from loop_controller.tool_governor import ToolGovernor


def govern_tool(
    controller: LoopController,
    tool_name: str,
    agent_id: str,
    user_id: str,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """把 Loop Controller 治理下的工具包装成 AutoGen 可调用的工具函数。

    Args:
        controller: LoopController 实例。
        tool_name: Loop Controller 内部 canonical_name，如 ``send_email``。
        agent_id: 默认 agent_id。
        user_id: 默认 user_id。

    Returns:
        一个装饰器；装饰函数后返回可被 AutoGen ``tools=[...]`` 或 ``register_function`` 使用的函数。
    """
    governor = ToolGovernor(controller, agent_id=agent_id, user_id=user_id)

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        async def _wrapped(*args: Any, **kwargs: Any) -> str:
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            return await governor.call(tool_name, dict(bound.arguments))

        return _wrapped

    return decorator
