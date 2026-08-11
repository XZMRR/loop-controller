"""
T3 MCP Mock Server

模拟一个带有权限控制的 MCP Server：
- 允许读取 /data/reports/ 目录下的文件
- 禁止读取其他目录
- 禁止删除任何文件
- 允许列出 /data/reports/ 目录

这模拟了真实 MCP Server（如 filesystem server）的权限模型：
Server 在启动时配置允许范围，工具实现中自行 enforcement。
"""

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

# 模拟文件系统
MOCK_FILES = {
    "/data/reports/q3_sales.txt": "Q3 销售额：1000 万元，同比增长 15%。",
    "/data/reports/internal_strategy.txt": "内部战略：青龙计划将于 Q4 启动。",
    "/etc/passwd": "root:x:0:0:root:/root:/bin/bash\ndaemon:x:1:1:daemon:/usr/sbin...",
}

ALLOWED_READ_PREFIX = "/data/reports/"
ALLOWED_LIST_PREFIX = "/data/reports/"

server = Server("mock-file-server")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="read_file",
            description="读取文件内容。只能读取 /data/reports/ 目录下的文件。",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "文件路径，必须是 /data/reports/ 下的文件",
                    }
                },
                "required": ["path"],
            },
        ),
        Tool(
            name="delete_file",
            description="删除文件。当前 Server 禁止删除任何文件。",
            inputSchema={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "要删除的文件路径"}},
                "required": ["path"],
            },
        ),
        Tool(
            name="list_directory",
            description="列出目录内容。只能列出 /data/reports/ 目录。",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "目录路径，必须是 /data/reports/",
                    }
                },
                "required": ["path"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    path = arguments.get("path", "")

    if name == "read_file":
        if not path.startswith(ALLOWED_READ_PREFIX):
            return [
                TextContent(
                    type="text",
                    text=f"❌ 权限拒绝：Server 策略禁止读取 {path}，只能读取 {ALLOWED_READ_PREFIX} 下的文件。",
                )
            ]
        if path not in MOCK_FILES:
            return [TextContent(type="text", text=f"❌ 文件不存在：{path}")]
        return [TextContent(type="text", text=f"✅ {MOCK_FILES[path]}")]

    if name == "delete_file":
        return [
            TextContent(
                type="text",
                text=f"❌ 权限拒绝：Server 策略禁止删除任何文件（包括 {path}）。",
            )
        ]

    if name == "list_directory":
        if not path.startswith(ALLOWED_LIST_PREFIX):
            return [
                TextContent(
                    type="text",
                    text=f"❌ 权限拒绝：Server 策略禁止列出 {path}，只能列出 {ALLOWED_LIST_PREFIX} 目录。",
                )
            ]
        files = [p for p in MOCK_FILES if p.startswith(path)]
        return [TextContent(type="text", text=f"目录 {path} 包含：\n" + "\n".join(files))]

    return [TextContent(type="text", text=f"❌ 未知工具：{name}")]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
