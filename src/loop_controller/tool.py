"""MCP 工具抽象与工具调用结果.

Tool 描述 MCP Server 暴露的工具，ToolResult 描述工具执行结果。
Loop Controller 内部使用规范化工具名，通过 MCPGateway 映射到真实 MCP 工具名。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class Tool:
    """MCP 工具元数据.

    Attributes:
        canonical_name: Loop Controller 内部使用的规范化工具名，如 "read_file"。
        mcp_name: 真实 MCP server 工具名，如 "read_text_file"。
        description: 工具描述。
        input_schema: 工具参数 JSON Schema。
    """

    canonical_name: str
    mcp_name: str
    description: str
    input_schema: dict


@dataclass(frozen=True)
class ToolResult:
    """工具调用结果.

    Attributes:
        call_id: 关联的 ActionProposal.call_id。
        task_id: 关联的任务 ID。
        tool_name: Loop Controller 内部规范化工具名。
        status: 执行状态，success / error / blocked。
        content: 成功时的结构化内容；MVP 阶段用 str/dict。
        error_code: 失败时的错误码。
    """

    call_id: str
    task_id: str
    tool_name: str
    status: Literal["success", "error", "blocked"]
    content: Any
    error_code: str | None = None
