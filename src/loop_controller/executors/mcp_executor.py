"""MCP 工具执行器：把工具调用转发到 MCPGateway。"""

from __future__ import annotations

from typing import Any

from loop_controller.executors.base import ExecutionContext, ToolExecutor
from loop_controller.mcp_gateway import MCPGateway
from loop_controller.models import CapabilityProfile, Tool, ToolResult


class MCPExecutor(ToolExecutor):
    """通过 MCPGateway 执行工具调用。

    v0.20.0 中所有工具都走此执行器；后续 HTTP / 本地函数执行器与 MCPExecutor
    并列注册到 ExecutorRegistry。
    """

    def __init__(self, gateway: MCPGateway) -> None:
        self._gateway = gateway

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: ExecutionContext,
    ) -> ToolResult:
        """转发到 MCPGateway.call_tool，透传完整治理上下文。"""
        return await self._gateway.call_tool(
            tool_name,
            arguments,
            context.call_id,
            context.task_id,
            agent_id=context.agent_id,
            user_id=context.user_id,
            session_id=context.session_id,
            tenant_id=context.tenant_id,
        )

    async def list_tools(self, profile: CapabilityProfile) -> list[Tool]:
        """返回按 Profile 过滤后的 MCP 工具列表。"""
        return await self._gateway.list_tools(profile)
