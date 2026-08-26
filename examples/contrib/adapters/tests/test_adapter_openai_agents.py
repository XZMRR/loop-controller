"""OpenAI Agents SDK 适配器测试（v0.15.0 / v0.18.0）。

未安装 openai-agents 时整个文件自动 skip；用 MockLoopController 验证装饰器行为。
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("agents")

from agents.tool import FunctionTool

from loop_controller.controller import LoopController
from loop_controller.models import GovernanceResult

from ..openai_agents_adapter import govern_function_tool


class _MockController(LoopController):
    """只记录 evaluate_and_execute 调用参数并返回预设结果的 mock。"""

    def __init__(self) -> None:  # noqa: D107
        self.calls: list[dict[str, Any]] = []
        self._response = GovernanceResult(
            status="allow",
            call_id="c1",
            tool_name="send_email",
            arguments={},
            content="email sent",
        )

    async def start(self) -> None:
        pass

    async def aclose(self) -> None:
        pass

    async def evaluate_and_execute(
        self,
        *,
        agent_id: str,
        user_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        **kwargs: Any,
    ) -> GovernanceResult:
        self.calls.append(
            {
                "agent_id": agent_id,
                "user_id": user_id,
                "tool_name": tool_name,
                "arguments": arguments,
            }
        )
        return self._response


@pytest.mark.asyncio
async def test_govern_function_tool_preserves_signature_and_calls_controller() -> None:
    controller = _MockController()

    @govern_function_tool(
        controller,
        "send_email",
        "发送邮件（高风险，需人工审批）",
        agent_id="researcher_001",
        user_id="alice",
    )
    async def send_email(to: str, subject: str, body: str) -> str:
        """发送邮件。"""
        return "should not be called"

    # 新版 OpenAI Agents SDK 的 @function_tool 返回 FunctionTool 实例，
    # 可直接传给 Agent(tools=[...])。
    assert isinstance(send_email, FunctionTool)
    assert send_email.name == "send_email"
    assert send_email.description == "发送邮件（高风险，需人工审批）"
    schema = send_email.params_json_schema
    assert schema["properties"].keys() == {"to", "subject", "body"}
    assert "to" in schema["required"]

    # 通过 __wrapped__ 直接调用被治理的函数，绕过 SDK 运行时
    result = await send_email.__wrapped__(to="zhang@company.com", subject="summary", body="body text")
    assert result == "email sent"
    assert len(controller.calls) == 1
    call = controller.calls[0]
    assert call["agent_id"] == "researcher_001"
    assert call["user_id"] == "alice"
    assert call["tool_name"] == "send_email"
    assert call["arguments"] == {"to": "zhang@company.com", "subject": "summary", "body": "body text"}
