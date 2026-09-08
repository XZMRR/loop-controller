"""ToolGovernor 单元测试（v0.16.0）。"""

from __future__ import annotations

from typing import Any

import pytest

from loop_controller import ToolGovernor
from loop_controller.controller import LoopController
from loop_controller.models import GovernanceResult


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
                "kwargs": kwargs,
            }
        )
        return self._response


@pytest.mark.asyncio
async def test_tool_governor_forwards_call_and_returns_content() -> None:
    controller = _MockController()
    governor = ToolGovernor(
        controller,
        agent_id="researcher_001",
        user_id="alice",
        default_task_context="default task",
    )

    result = await governor.call(
        "send_email",
        {"to": "zhang@company.com"},
    )

    assert result == "email sent"
    assert len(controller.calls) == 1
    call = controller.calls[0]
    assert call["agent_id"] == "researcher_001"
    assert call["user_id"] == "alice"
    assert call["tool_name"] == "send_email"
    assert call["arguments"] == {"to": "zhang@company.com"}
    assert call["kwargs"]["task_context"] == "default task"
    assert call["kwargs"]["session_id"] is None
    assert call["kwargs"]["task_id"] is None
    assert call["kwargs"]["action_kind"] == "tool_call"
    assert call["kwargs"]["target_agent_id"] is None


@pytest.mark.asyncio
async def test_tool_governor_forwards_delegation_metadata() -> None:
    controller = _MockController()
    governor = ToolGovernor(controller, agent_id="researcher_001", user_id="alice")

    await governor.call(
        "analyze_sales",
        {"region": "APAC"},
        action_kind="delegation",
        target_agent_id="research-agent",
    )

    call = controller.calls[0]
    assert call["arguments"] == {"region": "APAC"}
    assert call["kwargs"]["action_kind"] == "delegation"
    assert call["kwargs"]["target_agent_id"] == "research-agent"


@pytest.mark.asyncio
async def test_tool_governor_overrides_default_context() -> None:
    controller = _MockController()
    governor = ToolGovernor(
        controller,
        agent_id="researcher_001",
        user_id="alice",
        default_task_context="default task",
    )

    await governor.call(
        "send_email",
        {},
        task_context="specific task",
        session_id="s-001",
        task_id="t-001",
    )

    call = controller.calls[0]
    assert call["kwargs"]["task_context"] == "specific task"
    assert call["kwargs"]["session_id"] == "s-001"
    assert call["kwargs"]["task_id"] == "t-001"


@pytest.mark.asyncio
async def test_tool_governor_formats_require_approval() -> None:
    controller = _MockController()
    controller._response = GovernanceResult(
        status="require_approval",
        call_id="c1",
        tool_name="send_email",
        arguments={},
        request_id="req-42",
    )
    governor = ToolGovernor(controller, agent_id="researcher_001", user_id="alice")

    result = await governor.call("send_email", {})
    assert "requires approval" in result
    assert "req-42" in result
