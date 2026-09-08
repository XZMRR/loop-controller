"""AutoGen 适配器测试（v0.15.0）。

不依赖真实 AutoGen / LLM；用 MockLoopController 验证装饰器行为。
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from loop_controller.controller import LoopController
from loop_controller.models import GovernanceResult

from ..autogen_adapter import govern_tool


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
async def test_govern_tool_preserves_signature_and_calls_controller() -> None:
    controller = _MockController()

    @govern_tool(controller, "send_email", agent_id="researcher_001", user_id="alice")
    async def send_email(to: str, subject: str, body: str) -> str:
        """发送邮件。"""
        return "should not be called"

    # AutoGen 通过 inspect.signature 生成工具 schema，必须保留原函数签名
    sig = inspect.signature(send_email)
    params = list(sig.parameters.keys())
    assert params == ["to", "subject", "body"]
    assert send_email.__doc__ == "发送邮件。"

    result = await send_email("zhang@company.com", "summary", "body text")
    assert result == "email sent"
    assert len(controller.calls) == 1
    call = controller.calls[0]
    assert call["agent_id"] == "researcher_001"
    assert call["user_id"] == "alice"
    assert call["tool_name"] == "send_email"
    assert call["arguments"] == {"to": "zhang@company.com", "subject": "summary", "body": "body text"}


@pytest.mark.asyncio
async def test_govern_tool_returns_formatted_result() -> None:
    controller = _MockController()
    controller._response = GovernanceResult(
        status="require_approval",
        call_id="c1",
        tool_name="send_email",
        arguments={},
        request_id="req-42",
    )

    @govern_tool(controller, "send_email", agent_id="researcher_001", user_id="alice")
    async def send_email(to: str, subject: str, body: str) -> str:
        return "placeholder"

    result = await send_email("zhang@company.com", "summary", "body")
    assert "requires approval" in result
    assert "req-42" in result
