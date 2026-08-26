"""本地 fetch MCP server（v0.9.0 生产验证）。

使用 ``httpx`` 执行真实 HTTP 请求，不依赖外部 npm 包。
只支持 GET，作为 R2 治理场景中的"外部数据获取"工具。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from mcp import types  # type: ignore[import-untyped]
from mcp.server import Server  # type: ignore[import-untyped]
from mcp.server.stdio import stdio_server  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)


_FETCH_TOOL = types.Tool(
    name="fetch",
    description="Fetch the content of a URL via HTTP GET. Only text/html responses are returned.",
    inputSchema={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The URL to fetch",
            },
        },
        "required": ["url"],
    },
)


def _create_server() -> Server[Any]:
    server: Server[Any] = Server("loop-controller-fetch")

    @server.list_tools()  # type: ignore[misc]
    async def _list_tools(_request: types.ListToolsRequest) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[_FETCH_TOOL])

    @server.call_tool()  # type: ignore[misc]
    async def _call_tool(_name: str, arguments: dict[str, Any] | None) -> types.CallToolResult:
        args = dict(arguments or {})
        url = args.get("url", "")
        if not isinstance(url, str) or not url:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text="'url' is required")],
                isError=True,
            )
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                response = await client.get(url)
                response.raise_for_status()
                text = response.text
        except httpx.HTTPStatusError as exc:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"HTTP {exc.response.status_code}: {exc.response.text[:200]}")],
                isError=True,
            )
        except Exception as exc:  # noqa: BLE001
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"fetch failed: {exc}")],
                isError=True,
            )
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=text[:5000])]
        )

    return server


async def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    server = _create_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
