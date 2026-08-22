"""LangChain 适配器：把 Loop Controller 包装成 LangChain Tool（v0.13.0）。

本模块属于框架扩展，需要额外安装 ``langchain`` / ``langchain-core``：

    uv pip install langchain langchain-openai

使用方式：

    from langchain.agents import create_openai_tools_agent, AgentExecutor
    from langchain_openai import ChatOpenAI
    from loop_controller.adapters.langchain import govern_tool
    from loop_controller.controller import build_controller

    controller = await build_controller(config)
    tools = [
        govern_tool(controller, "web_search", "搜索网页"),
        govern_tool(controller, "send_email", "发送邮件"),
    ]
    llm = ChatOpenAI(model="gpt-4o-mini")
    agent = create_openai_tools_agent(llm, tools, prompt)
    agent_executor = AgentExecutor(agent=agent, tools=tools)
    await agent_executor.ainvoke({"input": "..."})

Agent 自己掌握主循环，Loop Controller 只负责每次 tool call 的治理。
"""

from __future__ import annotations

from typing import Any

try:
    from langchain_core.tools import StructuredTool
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "使用 loop_controller.adapters.langchain 需要先安装 langchain-core: "
        "uv pip install langchain-core"
    ) from exc

from loop_controller.controller import LoopController
from loop_controller.tool_governor import ToolGovernor


def govern_tool(
    controller: LoopController,
    tool_name: str,
    description: str,
    agent_id: str,
    user_id: str,
    args_schema: type[Any] | None = None,
) -> StructuredTool:
    """把 Loop Controller 治理下的工具包装成 LangChain StructuredTool。

    Args:
        controller: LoopController 实例。
        tool_name: Loop Controller 内部 canonical_name，如 ``send_email``。
        description: LangChain Agent 看到的工具描述。
        agent_id: 默认 agent_id。
        user_id: 默认 user_id。
        args_schema: 可选 Pydantic 参数 schema；未提供时 LLM 可能不约束参数。
    """
    governor = ToolGovernor(controller, agent_id=agent_id, user_id=user_id)

    async def _governed(**kwargs: Any) -> str:
        return await governor.call(tool_name, kwargs)

    return StructuredTool.from_function(
        coroutine=_governed,
        name=tool_name,
        description=description,
        args_schema=args_schema,
    )


class GovernedTool:
    """兼容旧名；请使用 ``govern_tool`` 工厂函数。"""

    def __new__(  # type: ignore[misc]
        cls,
        controller: LoopController,
        tool_name: str,
        description: str,
        agent_id: str,
        user_id: str,
        args_schema: type[Any] | None = None,
    ) -> StructuredTool:
        return govern_tool(
            controller=controller,
            tool_name=tool_name,
            description=description,
            agent_id=agent_id,
            user_id=user_id,
            args_schema=args_schema,
        )
