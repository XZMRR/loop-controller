"""执行器抽象单元测试（v0.20.0）。

覆盖 ExecutionContext、ExecutorRegistry 路由、MCPExecutor 转发。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from loop_controller.executors import (
    ExecutionContext,
    ExecutorRegistry,
    MCPExecutor,
    ToolExecutor,
)
from loop_controller.executors.base import ExecutorRegistryError
from loop_controller.models import CapabilityProfile, Tool, ToolResult


class TestExecutionContext:
    """执行上下文模型测试。"""

    def test_creation(self) -> None:
        ctx = ExecutionContext(
            call_id="call-1",
            task_id="task-1",
            agent_id="agent-1",
            user_id="user-1",
            session_id="session-1",
        )
        assert ctx.call_id == "call-1"
        assert ctx.task_id == "task-1"
        assert ctx.agent_id == "agent-1"
        assert ctx.user_id == "user-1"
        assert ctx.session_id == "session-1"

    def test_session_optional(self) -> None:
        ctx = ExecutionContext(
            call_id="call-1",
            task_id="task-1",
            agent_id="agent-1",
            user_id="user-1",
        )
        assert ctx.session_id is None


class _FakeExecutor(ToolExecutor):
    """测试用执行器，记录调用参数并返回固定结果。"""

    def __init__(self, prefix: str = "fake") -> None:
        self.prefix = prefix
        self.calls: list[dict[str, Any]] = []

    def secret_refs_for(self, tool_name: str) -> list[str]:
        return []

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: ExecutionContext,
    ) -> ToolResult:
        self.calls.append(
            {
                "tool_name": tool_name,
                "arguments": arguments,
                "context": context,
            }
        )
        return ToolResult(
            call_id=context.call_id,
            task_id=context.task_id,
            tool_name=tool_name,
            status="success",
            content=f"{self.prefix}:{tool_name}",
        )

    async def list_tools(self, profile: CapabilityProfile) -> list[Tool]:
        return [
            Tool(
                canonical_name=f"{self.prefix}_tool",
                mcp_name=f"{self.prefix}_tool",
                description="fake",
                input_schema={},
            )
        ]


class TestExecutorRegistry:
    """ExecutorRegistry 路由测试。"""

    def test_register_and_get(self) -> None:
        registry = ExecutorRegistry()
        executor = _FakeExecutor()
        registry.register("tool_a", executor)
        assert registry.get_executor("tool_a") is executor

    def test_default_fallback(self) -> None:
        registry = ExecutorRegistry()
        default = _FakeExecutor("default")
        registry.set_default(default)
        assert registry.get_executor("unregistered") is default

    def test_specific_overrides_default(self) -> None:
        registry = ExecutorRegistry()
        default = _FakeExecutor("default")
        specific = _FakeExecutor("specific")
        registry.set_default(default)
        registry.register("tool_a", specific)
        assert registry.get_executor("tool_a") is specific
        assert registry.get_executor("other") is default

    def test_missing_executor_raises(self) -> None:
        registry = ExecutorRegistry()
        with pytest.raises(ExecutorRegistryError, match="没有注册执行器"):
            registry.get_executor("missing")

    def test_resolve_secret_refs_merges_and_deduplicates(self) -> None:
        registry = ExecutorRegistry()
        executor = _FakeExecutor()
        executor.secret_refs_for = lambda tool_name: ["trusted", "shared"]  # type: ignore[method-assign]
        registry.register("tool_a", executor)

        assert registry.resolve_secret_refs(
            "tool_a",
            {
                "secret_ref": {"name": "declared"},
                "nested": [{"secret_ref": "shared"}],
            },
        ) == ["declared", "shared", "trusted"]

    def test_register_rejects_non_executor(self) -> None:
        """P2：注册对象必须符合 ToolExecutor 协议。"""
        registry = ExecutorRegistry()
        with pytest.raises(TypeError, match="ToolExecutor"):
            registry.register("bad", object())  # type: ignore[arg-type]

    def test_set_default_rejects_non_executor(self) -> None:
        """P2：默认执行器必须符合 ToolExecutor 协议。"""
        registry = ExecutorRegistry()
        with pytest.raises(TypeError, match="ToolExecutor"):
            registry.set_default(object())  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_execute_routing(self) -> None:
        registry = ExecutorRegistry()
        executor = _FakeExecutor()
        registry.register("tool_a", executor)
        ctx = ExecutionContext(
            call_id="c1", task_id="t1", agent_id="a1", user_id="u1"
        )
        result = await registry.get_executor("tool_a").execute(
            "tool_a", {"x": 1}, ctx
        )
        assert result.status == "success"
        assert result.content == "fake:tool_a"
        assert executor.calls[0]["arguments"] == {"x": 1}


class TestMultiExecutorRouting:
    """v0.21.0：MCP 与 HTTP 执行器并存分发。"""

    @pytest.mark.asyncio
    async def test_mcp_and_http_routing(self) -> None:
        from loop_controller.executors import HTTPExecutor
        from loop_controller.executors.http_client import HTTPClient
        from loop_controller.executors.http_models import HTTPToolSpec

        registry = ExecutorRegistry()
        mcp = _FakeExecutor("mcp")
        http_client = HTTPClient()
        http_spec = HTTPToolSpec(
            tool_name="http_tool",
            base_url="https://api.example.com",
            path="/x",
        )
        http = HTTPExecutor(http_client, {"http_tool": http_spec})

        registry.register("mcp_tool", mcp)
        registry.register("http_tool", http)

        ctx = ExecutionContext(
            call_id="c1", task_id="t1", agent_id="a1", user_id="u1"
        )
        assert registry.get_executor("mcp_tool") is mcp
        assert registry.get_executor("http_tool") is http

        mcp_result = await registry.get_executor("mcp_tool").execute(
            "mcp_tool", {}, ctx
        )
        assert mcp_result.content == "mcp:mcp_tool"


class TestMCPExecutor:
    """MCPExecutor 转发测试。"""

    @pytest.fixture
    def gateway(self) -> AsyncMock:
        mock = AsyncMock()
        mock.call_tool = AsyncMock(
            return_value=ToolResult(
                call_id="c1",
                task_id="t1",
                tool_name="send_email",
                status="success",
                content="sent",
            )
        )
        mock.list_tools = AsyncMock(return_value=[])
        return mock

    @pytest.mark.asyncio
    async def test_execute_forwards_to_gateway(self, gateway: AsyncMock) -> None:
        executor = MCPExecutor(gateway)
        ctx = ExecutionContext(
            call_id="c1", task_id="t1", agent_id="a1", user_id="u1"
        )
        result = await executor.execute(
            "send_email", {"to": "x@y.com"}, ctx
        )
        assert result.status == "success"
        gateway.call_tool.assert_awaited_once_with(
            "send_email",
            {"to": "x@y.com"},
            "c1",
            "t1",
            agent_id="a1",
            user_id="u1",
            session_id=None,
            tenant_id=None,
        )

    @pytest.mark.asyncio
    async def test_list_tools_forwards_to_gateway(self, gateway: AsyncMock) -> None:
        executor = MCPExecutor(gateway)
        profile = CapabilityProfile(profile_id="p1")
        await executor.list_tools(profile)
        gateway.list_tools.assert_awaited_once_with(profile)
