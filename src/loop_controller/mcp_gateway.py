"""MCP Client Policy Gateway.

R2 不直接暴露原始 MCP client 给 R1，而是通过 MCPGateway 做两件事：
1. list_tools：按 CapabilityProfile 过滤后返回工具列表；
2. call_tool：只转发已被 R2 授权的调用。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from loop_controller.capability_profile import CapabilityProfile
from loop_controller.tool import Tool, ToolResult


@runtime_checkable
class MCPGateway(Protocol):
    """R2 的 MCP Client 代理接口."""

    def list_tools(self, profile: CapabilityProfile) -> list[Tool]:
        """返回按 CapabilityProfile 过滤后的工具列表."""
        ...

    def call_tool(self, tool_name: str, arguments: dict[str, Any], call_id: str) -> ToolResult:
        """调用已被 R2 授权的工具."""
        ...


class MockMCPGateway:
    """MVP Mock MCP Gateway.

    不连接真实 MCP Server，只返回固定结果，用于快速验证 R0-R3 闭环。
    """

    def __init__(self, tools: list[Tool] | None = None) -> None:
        """初始化.

        Args:
            tools: 可选的工具列表；默认包含 4 个研究助手常用工具。
        """
        self._tools = tools or _DEFAULT_TOOLS
        self._tool_map = {t.canonical_name: t for t in self._tools}

    def list_tools(self, profile: CapabilityProfile) -> list[Tool]:
        """按 CapabilityProfile.allowed_tools 过滤."""
        allowed = set(profile.allowed_tools)
        return [t for t in self._tools if t.canonical_name in allowed]

    def call_tool(self, tool_name: str, arguments: dict[str, Any], call_id: str) -> ToolResult:
        """Mock 执行，返回成功结果."""
        tool = self._tool_map.get(tool_name)
        if tool is None:
            return ToolResult(
                call_id=call_id,
                task_id="unknown",
                tool_name=tool_name,
                status="error",
                content=None,
                error_code="tool_not_found",
            )
        return ToolResult(
            call_id=call_id,
            task_id="unknown",
            tool_name=tool_name,
            status="success",
            content=f"Mock result for {tool_name} with args {arguments}",
        )


_DEFAULT_TOOLS: list[Tool] = [
    Tool(
        canonical_name="read_file",
        mcp_name="read_text_file",
        description="Read a text file from the filesystem.",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
    ),
    Tool(
        canonical_name="write_file",
        mcp_name="write_file",
        description="Write content to a file.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        },
    ),
    Tool(
        canonical_name="web_search",
        mcp_name="brave_web_search",
        description="Search the web.",
        input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
    ),
    Tool(
        canonical_name="send_email",
        mcp_name="send_email",
        description="Send an email.",
        input_schema={
            "type": "object",
            "properties": {
                "to": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
            },
        },
    ),
]
