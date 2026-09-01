"""Agent SDK 测试（v0.33.0）：@governed 装饰器、hook_tool_registry、launch_agent。"""

from __future__ import annotations

import asyncio
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


@pytest.mark.asyncio
async def test_contextvar_isolation() -> None:
    """set_current 只影响当前上下文，嵌套上下文不会互相污染。"""
    rt1 = GovernanceRuntime(AsyncMock(), agent_id="a1", user_id="u1")
    token1 = GovernanceRuntime.set_current(rt1)

    async def inner() -> None:
        rt2 = GovernanceRuntime(AsyncMock(), agent_id="a2", user_id="u2")
        GovernanceRuntime.set_current(rt2)
        assert GovernanceRuntime.current() is rt2

    # 用新 Task 运行，验证 ContextVar 修改不会污染父任务上下文
    await asyncio.create_task(inner())
    assert GovernanceRuntime.current() is rt1
    GovernanceRuntime.reset_current(token1)


@pytest.mark.asyncio
async def test_async_context_manager() -> None:
    """GovernanceRuntime 支持 async with，退出时自动 aclose 并恢复上下文。"""
    rt = GovernanceRuntime(AsyncMock(), agent_id="a1", user_id="u1")
    rt._controller.aclose = AsyncMock()

    async with rt as entered:
        assert entered is rt
        assert GovernanceRuntime.current() is rt

    rt._controller.aclose.assert_awaited_once()
    with pytest.raises(RuntimeError):
        GovernanceRuntime.current()


def test_governance_result_private_attrs_not_dumped() -> None:
    """_controller/_runtime 是 PrivateAttr，不应出现在 model_dump 中。"""
    result = GovernanceResult(
        status="require_approval",
        call_id="c1",
        tool_name="t",
        arguments={},
        request_id="r1",
    ).with_controller(object())
    dumped = result.model_dump()
    assert "_controller" not in dumped
    assert "_runtime" not in dumped


@pytest.mark.asyncio
async def test_governed_wait_for_approval_auto_retry() -> None:
    """@governed(wait_for_approval=True) 在审批通过后自动返回执行结果。"""
    pending = GovernanceResult(
        status="require_approval",
        call_id="c1",
        tool_name="send_email",
        arguments={"to": "bob@company.com"},
        request_id="r1",
    )
    final = GovernanceResult(
        status="allow",
        call_id="c1",
        tool_name="send_email",
        arguments={"to": "bob@company.com"},
        request_id="r1",
        content={"status": "sent"},
    )
    rt = _make_runtime(pending)
    rt.controller.resume_after_approval = AsyncMock(return_value=final)

    @governed(tool_name="send_email", wait_for_approval=True)
    async def send_email(to: str) -> dict[str, str]:
        return {"status": "unsent"}

    result = await send_email("bob@company.com")
    assert result == {"status": "sent"}
    rt.controller.resume_after_approval.assert_awaited_once_with("r1")


@pytest.mark.asyncio
async def test_governed_wait_for_approval_propagates_denial() -> None:
    """@governed(wait_for_approval=True) 在审批被拒绝时抛出 GovernanceDeniedError。"""
    pending = GovernanceResult(
        status="require_approval",
        call_id="c1",
        tool_name="send_email",
        arguments={"to": "bob@company.com"},
        request_id="r1",
    )
    final = GovernanceResult(
        status="deny",
        call_id="c1",
        tool_name="send_email",
        arguments={"to": "bob@company.com"},
        request_id="r1",
        reason="manager denied",
    )
    rt = _make_runtime(pending)
    rt.controller.resume_after_approval = AsyncMock(return_value=final)

    @governed(tool_name="send_email", wait_for_approval=True)
    async def send_email(to: str) -> dict[str, str]:
        return {"status": "unsent"}

    with pytest.raises(GovernanceDeniedError) as exc_info:
        await send_email("bob@company.com")
    assert exc_info.value.result.status == "deny"


def test_governed_wait_for_approval_sync() -> None:
    """同步 @governed(wait_for_approval=True) 也能自动等待审批。"""
    pending = GovernanceResult(
        status="require_approval",
        call_id="c1",
        tool_name="send_email",
        arguments={"to": "bob@company.com"},
        request_id="r1",
    )
    final = GovernanceResult(
        status="allow",
        call_id="c1",
        tool_name="send_email",
        arguments={"to": "bob@company.com"},
        request_id="r1",
        content={"status": "sent"},
    )
    rt = _make_runtime(pending)
    rt.controller.resume_after_approval = AsyncMock(return_value=final)

    @governed(tool_name="send_email", wait_for_approval=True)
    def send_email(to: str) -> dict[str, str]:
        return {"status": "unsent"}

    result = send_email("bob@company.com")
    assert result == {"status": "sent"}
    rt.controller.resume_after_approval.assert_awaited_once_with("r1")


@pytest.mark.asyncio
async def test_wait_for_approval_timeout_cancels_request() -> None:
    """wait_for_approval 超时后会调用 cancel_approval 清理审批请求。"""
    pending = GovernanceResult(
        status="require_approval",
        call_id="c1",
        tool_name="send_email",
        arguments={"to": "bob@company.com"},
        request_id="r1",
    )
    rt = _make_runtime(pending)
    rt.controller.resume_after_approval = AsyncMock(
        return_value=pending.model_copy()
    )
    rt.controller.cancel_approval = AsyncMock()

    bound = pending.with_controller(rt.controller, rt)
    with pytest.raises(GovernanceDeniedError) as exc_info:
        await bound.wait_for_approval(timeout=0.05, poll_interval=0.01)

    assert exc_info.value.result.error_code == "approval_timeout"
    rt.controller.cancel_approval.assert_awaited_once_with("r1")


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


def test_hook_tool_registry_rollback_on_failure() -> None:
    """hook_tool_registry 替换中途失败时，已替换的工具会被回滚。"""
    _make_runtime(
        GovernanceResult(
            status="allow",
            call_id="c1",
            tool_name="first",
            arguments={},
            content=None,
        )
    )

    class Registry:
        def __init__(self) -> None:
            self._tools: dict[str, Any] = {}
            self._setup = True

        def register(self, name: str, fn: Any) -> None:
            if not self._setup and name == "second":
                raise RuntimeError("boom")
            self._tools[name] = fn

        def get(self, name: str) -> Any:
            return self._tools[name]

        def list_tools(self) -> list[str]:
            return ["first", "second"]

    original_first = lambda: "original_first"  # noqa: E731
    original_second = lambda: "original_second"  # noqa: E731

    registry = Registry()
    registry.register("first", original_first)
    registry.register("second", original_second)
    registry._setup = False

    with pytest.raises(RuntimeError, match="boom"):
        GovernanceRuntime.current().hook_tool_registry(registry)

    # first 应被回滚为原始函数
    assert registry.get("first")() == "original_first"
    assert registry.get("second")() == "original_second"


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
