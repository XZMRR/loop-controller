"""函数式 Agent 接入示例（v0.32.0）。

演示如何用最少的代码把一组 Python 函数接入 Loop Controller 治理。
"""

from __future__ import annotations

import asyncio

from loop_controller import GovernanceRuntime, governed


@governed(tool_name="echo")
def echo(text: str) -> str:
    """本地 echo 工具。"""
    return text


@governed(tool_name="add")
def add(a: int, b: int) -> int:
    """本地加法工具。"""
    return a + b


async def main() -> None:
    rt = await GovernanceRuntime.from_config(
        "config",
        agent_id="demo_agent",
        user_id="demo_user",
        default_task_context="函数式 Agent 示例",
    )
    try:
        print(echo("hello loop controller"))
        print(add(1, 2))
    finally:
        await rt.aclose()


if __name__ == "__main__":
    asyncio.run(main())
