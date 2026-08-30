"""Agent SDK 测试（v0.32.0）：@governed 装饰器、hook_tool_registry、launch_agent。"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from loop_controller.agent_sdk import (
    GovernanceDeniedError,
    GovernanceRuntime,
    governed,
    launch_agent,
)
from loop_controller.models import GovernanceResult


@pytest.fixture(autouse=True)
def _reset_current_runtime() -> None:
    GovernanceRuntime.reset_current()


def _make_runtime(mock_result: GovernanceResult) -> GovernanceRuntime:
    controller = AsyncMock()
    controller.evaluate_and_execute = AsyncMock(return_value=mock_result)
    rt = GovernanceRuntime(controller, agent_id="a1", user_id="u1")
    GovernanceRuntime.set_current(rt)
    return rt


@pytest.mark.asyncio
async def test_governed_async_returns_content() -> None:
    rt = _make_runtime(
        GovernanceResult(
            status="allow",
            call_id="c1",
            tool_name="echo",
            arguments={"text": "hello"},
            content="hello",
        )
    )

    @governed(tool_name="echo")
    async def echo(text: str) -> str:
        return text

    result = await echo("hello")
    assert result == "hello"
    assert rt.controller.evaluate_and_execute.await_count == 1


def test_governed_sync_returns_content() -> None:
    rt = _make_runtime(
        GovernanceResult(
            status="allow",
            call_id="c1",
            tool_name="write_file",
            arguments={"path": "/tmp/a.txt", "content": "x"},
            content={"bytes": 1},
        )
    )

    @governed
    def write_file(path: str, content: str) -> dict[str, Any]:
        return {"bytes": len(content)}

    result = write_file("/tmp/a.txt", "x")
    assert result == {"bytes": 1}
    rt.controller.evaluate_and_execute.assert_awaited_once()
    call_kwargs = rt.controller.evaluate_and_execute.await_args.kwargs
    assert call_kwargs["tool_name"] == "write_file"
    assert call_kwargs["arguments"] == {"path": "/tmp/a.txt", "content": "x"}


def test_governed_denied_raises() -> None:
    rt = _make_runtime(
        GovernanceResult(
            status="deny",
            call_id="c1",
            tool_name="fetch_url",
            arguments={"url": "https://example.com"},
            reason="not allowed",
        )
    )

    @governed
    def fetch_url(url: str) -> str:
        return url

    with pytest.raises(GovernanceDeniedError):
        fetch_url("https://example.com")
    assert rt.controller.evaluate_and_execute.await_count == 1


def test_hook_tool_registry_dict() -> None:
    rt = _make_runtime(
        GovernanceResult(
            status="allow",
            call_id="c1",
            tool_name="add",
            arguments={"a": 1, "b": 2},
            content=3,
        )
    )

    registry: dict[str, Any] = {"tools": {}}

    def add(a: int, b: int) -> int:
        return a + b

    registry["tools"]["add"] = add

    rt = GovernanceRuntime.current()
    rt.hook_tool_registry(registry)

    assert registry["tools"]["add"](1, 2) == 3


def test_hook_tool_registry_object() -> None:
    _make_runtime(
        GovernanceResult(
            status="allow",
            call_id="c1",
            tool_name="mul",
            arguments={"a": 2, "b": 3},
            content=6,
        )
    )

    class Registry:
        def __init__(self) -> None:
            self._tools: dict[str, Any] = {}

        def register(self, name: str, fn: Any) -> None:
            self._tools[name] = fn

        def get(self, name: str) -> Any:
            return self._tools[name]

        def list_tools(self) -> list[str]:
            return list(self._tools.keys())

    registry = Registry()
    registry.register("mul", lambda a, b: a * b)

    GovernanceRuntime.current().hook_tool_registry(registry)

    assert registry.get("mul")(2, 3) == 6


@pytest.mark.asyncio
async def test_launch_agent_runs_entrypoint() -> None:
    rt = _make_runtime(
        GovernanceResult(
            status="allow",
            call_id="c1",
            tool_name="noop",
            arguments={},
            content="ok",
        )
    )
    rt.controller.aclose = AsyncMock()

    with patch("loop_controller.agent_sdk.GovernanceRuntime.from_config", return_value=rt):
        with patch(
            "importlib.import_module",
            return_value=MagicMock(run=lambda: "done"),
        ):
            result = await launch_agent(
                "fake_module:run",
                "/tmp/config",
                agent_id="a1",
                user_id="u1",
            )
    assert result == "done"
    rt.controller.aclose.assert_awaited_once()
