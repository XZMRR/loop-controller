"""LangChain Agent 集成测试。

使用真实 LoopController 和 LangChain tool，验证批量治理包装后
工具调用仍能被 Loop Controller 拦截、执行并审计。
"""

from __future__ import annotations

from typing import Any

import pytest


@pytest.mark.integration
@pytest.mark.asyncio
async def test_govern_langchain_tool_routes_to_controller(simple_controller: Any) -> None:
    """govern_langchain_tools 包装后，工具调用走 Loop Controller。"""
    pytest.importorskip("langchain_core", reason="langchain_core 未安装")
    from langchain_core.tools import BaseTool
    from pydantic import BaseModel, Field

    from examples.integrations.langchain_example import govern_langchain_tools
    from loop_controller.agent_sdk import GovernanceRuntime

    rt = GovernanceRuntime(simple_controller, agent_id="integration_agent", user_id="alice")
    GovernanceRuntime.set_current(rt)

    class AddInput(BaseModel):
        a: int = Field(description="第一个数")
        b: int = Field(description="第二个数")

    class AddTool(BaseTool):
        name: str = "add"
        description: str = "两数相加"
        args_schema: type[BaseModel] = AddInput

        def _run(self, a: int, b: int) -> int:
            return -1

        async def _arun(self, a: int, b: int) -> int:
            return -1

    try:
        tools = govern_langchain_tools([AddTool()], runtime=rt)
        result = await tools[0].ainvoke({"a": 2, "b": 3})
        assert result == 5
    finally:
        await rt.aclose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_langchain_agent_multi_step_workflow(simple_controller: Any) -> None:
    """多个 LangChain tool 被治理后，可组成多步 Agent 工作流。"""
    pytest.importorskip("langchain_core", reason="langchain_core 未安装")
    from langchain_core.tools import BaseTool
    from pydantic import BaseModel, Field

    from examples.integrations.langchain_example import govern_langchain_tools
    from loop_controller.agent_sdk import GovernanceRuntime

    rt = GovernanceRuntime(simple_controller, agent_id="integration_agent", user_id="alice")
    GovernanceRuntime.set_current(rt)

    class AddInput(BaseModel):
        a: int = Field(description="第一个数")
        b: int = Field(description="第二个数")

    class EchoInput(BaseModel):
        text: str = Field(description="需要回显的文本")

    class AddTool(BaseTool):
        name: str = "add"
        description: str = "两数相加"
        args_schema: type[BaseModel] = AddInput

        def _run(self, a: int, b: int) -> int:
            return -1

        async def _arun(self, a: int, b: int) -> int:
            return -1

    class EchoTool(BaseTool):
        name: str = "echo"
        description: str = "回显文本"
        args_schema: type[BaseModel] = EchoInput

        def _run(self, text: str) -> str:
            return text

        async def _arun(self, text: str) -> str:
            return text

    try:
        tools = govern_langchain_tools([AddTool(), EchoTool()], runtime=rt)
        add_tool, echo_tool = tools

        # Agent 第一步：计算
        sum_result = await add_tool.ainvoke({"a": 1, "b": 2})
        assert sum_result == 3

        # Agent 第二步：基于结果继续调用
        echo_result = await echo_tool.ainvoke({"text": f"sum is {sum_result}"})
        assert echo_result == "sum is 3"

        # 验证审计记录包含两个工具
        events = list(simple_controller._runtime.audit_store.list_recent(limit=50))
        targets = {e.target for e in events}
        assert "add" in targets
        assert "echo" in targets
    finally:
        await rt.aclose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_langchain_tool_require_approval_returns_governance_result(
    approval_controller: Any,
) -> None:
    """LangChain tool 触发审批时返回 GovernanceResult，不抛异常。"""
    pytest.importorskip("langchain_core", reason="langchain_core 未安装")
    from langchain_core.tools import BaseTool
    from pydantic import BaseModel, Field

    from examples.integrations.langchain_example import govern_langchain_tools
    from loop_controller.agent_sdk import GovernanceResult, GovernanceRuntime

    rt = GovernanceRuntime(approval_controller, agent_id="integration_agent", user_id="alice")
    GovernanceRuntime.set_current(rt)

    class SendEmailInput(BaseModel):
        to: str = Field(description="收件人")
        subject: str = Field(description="主题")
        body: str = Field(description="正文")

    class SendEmailTool(BaseTool):
        name: str = "send_email"
        description: str = "发送邮件"
        args_schema: type[BaseModel] = SendEmailInput

        def _run(self, to: str, subject: str, body: str) -> dict[str, str]:
            return {"status": "unsent"}

        async def _arun(self, to: str, subject: str, body: str) -> dict[str, str]:
            return {"status": "unsent"}

    try:
        tools = govern_langchain_tools([SendEmailTool()], runtime=rt)
        result = await tools[0].ainvoke(
            {"to": "bob@company.com", "subject": "hi", "body": "body"}
        )
        assert isinstance(result, GovernanceResult)
        assert result.status == "require_approval"
    finally:
        await rt.aclose()
