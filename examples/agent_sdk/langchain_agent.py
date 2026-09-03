"""LangChain Agent 接入示例（v0.32.0）。

演示如何把 LangChain BaseTool 列表批量接入 Loop Controller。
本示例不强制依赖 langchain_core；如未安装则直接返回原工具列表。

注意：loop_controller.integrations.langchain 已移除，govern_langchain_tools
现在位于 examples/integrations/langchain_example.py。
"""

from __future__ import annotations

import asyncio
from typing import Any

try:
    from langchain_core.tools import StructuredTool
except ImportError:
    StructuredTool = None  # type: ignore[misc,assignment]

from examples.integrations.langchain_example import govern_langchain_tools
from loop_controller import GovernanceRuntime


def _make_tools() -> list[Any]:
    if StructuredTool is None:
        return []

    def multiply(a: int, b: int) -> int:
        return a * b

    return [
        StructuredTool.from_function(
            name="multiply",
            func=multiply,
            description=" multiply two integers",
            args_schema=None,  # type: ignore[arg-type]
        )
    ]


async def main() -> None:
    rt = await GovernanceRuntime.from_config(
        "config",
        agent_id="demo_agent",
        user_id="demo_user",
    )
    try:
        tools = _make_tools()
        governed_tools = govern_langchain_tools(tools, runtime=rt)
        if governed_tools:
            result = await governed_tools[0].ainvoke({"a": 3, "b": 4})
            print(f"result: {result}")
        else:
            print("langchain_core 未安装，跳过 LangChain 工具调用")
    finally:
        await rt.aclose()


if __name__ == "__main__":
    asyncio.run(main())
