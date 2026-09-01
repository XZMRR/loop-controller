"""框架集成测试（v0.32.0）：可选示例集成。

未安装可选依赖时自动 skip，保持核心测试套件可在最小依赖下运行。
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from loop_controller.agent_sdk import GovernanceRuntime
from loop_controller.models import GovernanceResult


def _make_runtime(mock_result: GovernanceResult) -> GovernanceRuntime:
    controller = AsyncMock()
    controller.evaluate_and_execute = AsyncMock(return_value=mock_result)
    rt = GovernanceRuntime(controller, agent_id="a1", user_id="u1")
    GovernanceRuntime.set_current(rt)
    return rt


@pytest.fixture(autouse=True)
def _reset_current_runtime() -> None:
    GovernanceRuntime.reset_current()


@pytest.mark.asyncio
async def test_govern_langchain_tools_wraps_base_tool() -> None:
    pytest.importorskip("langchain_core", reason="langchain_core 未安装")
    from langchain_core.tools import BaseTool
    from pydantic import BaseModel, Field

    from examples.integrations.langchain_example import govern_langchain_tools

    rt = _make_runtime(
        GovernanceResult(
            status="allow",
            call_id="c1",
            tool_name="lc_echo",
            arguments={"message": "hello"},
            content="hello",
        )
    )

    class EchoInput(BaseModel):
        message: str = Field(description="message")

    class EchoTool(BaseTool):
        name: str = "lc_echo"
        description: str = "echo"
        args_schema: type[BaseModel] = EchoInput

        def _run(self, message: str) -> str:
            return message

        async def _arun(self, message: str) -> str:
            return message

    tools = govern_langchain_tools([EchoTool()], runtime=rt)
    result = await tools[0].ainvoke({"message": "hello"})
    assert result == "hello"
