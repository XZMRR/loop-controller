"""浏览器自动化的 MCP 包装示例（v0.24.0）。

这是一个“占位/规范示例”，演示如何把浏览器能力包装成 MCP 工具。
实际执行需要 Playwright，本文件故意不引入 Playwright，避免把浏览器依赖
拖入 Loop Controller。

部署方式：
1. 在独立容器/沙箱中安装 Playwright：
     pip install playwright
     playwright install chromium
2. 把本 server 作为 MCP server 启动；
3. Loop Controller 通过 config/mcp_servers.yaml 注册它；
4. Loop Controller 只负责治理，浏览器动作在容器里执行。
"""

from __future__ import annotations

from typing import Any

try:
    from mcp.server import Server
    from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool
except ModuleNotFoundError as exc:
    raise SystemExit(
        "mcp SDK 未安装，请执行：uv add --dev mcp 或 pip install mcp"
    ) from exc

server = Server("loop-controller-browser-wrapper")

_NAVIGATE_TOOL = Tool(
    name="browser_navigate",
    description="导航到指定 URL（需要在独立 Harness/容器中运行本 server）。",
    input_schema={
        "type": "object",
        "properties": {"url": {"type": "string"}},
        "required": ["url"],
    },
)

_GET_TEXT_TOOL = Tool(
    name="browser_get_text",
    description="获取当前页面文本内容。",
    input_schema={"type": "object", "properties": {}},
)


@server.list_tools()
async def _list_tools() -> ListToolsResult:
    return ListToolsResult(tools=[_NAVIGATE_TOOL, _GET_TEXT_TOOL])


@server.call_tool()
async def _call_tool(name: str, arguments: dict[str, Any] | None) -> CallToolResult:
    # 占位实现：提醒用户需要在独立环境中接入真实 Playwright。
    message = (
        f"browser tool {name!r} 被调用，但当前为占位示例。"
        "请在独立 Harness/容器中实现 Playwright 执行逻辑，"
        "并让 Loop Controller 通过 MCP 注册此 server。"
    )
    return CallToolResult(
        content=[TextContent(type="text", text=message)],
        is_error=True,
    )


async def main() -> None:
    from mcp.server.stdio import stdio_server

    async with stdio_server(server) as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
