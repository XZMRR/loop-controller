"""Shell 命令的 MCP 包装示例（v0.24.0）。

警告：此 server 只是“把 shell 命令包装成 MCP 工具”的示例。
生产环境应把它跑在受控 Harness / 容器 / 沙箱中，由 Loop Controller 负责
治理决策（身份、策略、审批、审计），而不是让 Loop Controller 进程自己执行。

启动方式：
  python -m examples.contrib.mcp_wrappers.shell_mcp_server
然后在 config/mcp_servers.yaml 中注册：
  servers:
    shell:
      command: ["python", "-m", "examples.contrib.mcp_wrappers.shell_mcp_server"]
  tool_mapping:
    run_shell: {server: shell, mcp_name: run_shell, cost_per_call: 100}
"""

from __future__ import annotations

import asyncio
import subprocess
from typing import Any

try:
    from mcp.server import Server
    from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool
except ModuleNotFoundError as exc:
    raise SystemExit(
        "mcp SDK 未安装，请执行：uv add --dev mcp 或 pip install mcp"
    ) from exc


_TIMEOUT = 30
_MAX_OUTPUT = 64 * 1024

_ALLOWED_COMMANDS = {
    "ls": ["ls"],
    "pwd": ["pwd"],
    "echo": ["echo"],
}

server = Server("loop-controller-shell-wrapper")

_RUN_SHELL_TOOL = Tool(
    name="run_shell",
    description="在隔离包装器中执行一条受控 shell 命令（示例：ls/pwd/echo）。",
    input_schema={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": f"允许的命令前缀之一：{list(_ALLOWED_COMMANDS)}",
            },
            "args": {
                "type": "array",
                "items": {"type": "string"},
                "default": [],
            },
        },
        "required": ["command"],
    },
)


@server.list_tools()
async def _list_tools() -> ListToolsResult:
    return ListToolsResult(tools=[_RUN_SHELL_TOOL])


@server.call_tool()
async def _call_tool(name: str, arguments: dict[str, Any] | None) -> CallToolResult:
    if arguments is None:
        arguments = {}
    if name != "run_shell":
        return CallToolResult(
            content=[TextContent(type="text", text=f"未知工具: {name}")],
            is_error=True,
        )

    command = str(arguments.get("command", "")).strip()
    args = [str(a) for a in arguments.get("args", [])]
    base = _ALLOWED_COMMANDS.get(command)
    if base is None:
        return CallToolResult(
            content=[TextContent(type="text", text=f"命令 {command!r} 不在允许列表中")],
            is_error=True,
        )

    # 构造完整命令并做基本转义/限制，不通过 shell 解析
    full_cmd = base + args
    try:
        proc = await asyncio.create_subprocess_exec(
            *full_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=_TIMEOUT
        )
    except TimeoutError:
        if proc.returncode is None:
            proc.kill()
        return CallToolResult(
            content=[TextContent(type="text", text="命令执行超时")],
            is_error=True,
        )
    except Exception as exc:  # noqa: BLE001
        return CallToolResult(
            content=[TextContent(type="text", text=f"执行失败: {exc}")],
            is_error=True,
        )

    output = stdout[:_MAX_OUTPUT].decode("utf-8", errors="replace")
    if stderr:
        output += "\n[stderr] " + stderr[:_MAX_OUTPUT].decode("utf-8", errors="replace")
    return CallToolResult(
        content=[TextContent(type="text", text=output)],
        is_error=proc.returncode != 0,
    )


async def main() -> None:
    from mcp.server.stdio import stdio_server

    async with stdio_server(server) as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
