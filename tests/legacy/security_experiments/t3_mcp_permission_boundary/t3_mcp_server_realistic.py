"""
T3 MCP Mock Server（更接近真实 filesystem server 的版本）

升级点：
1. 路径规范化（resolve/normalize）
2. 防御路径遍历攻击（../../../etc/passwd）
3. 防御符号链接绕过
4. 新文件创建时检查父目录
5. 更真实的错误信息

参考：官方 MCP filesystem server 的 path-validation.ts 和 lib.ts
"""

import asyncio
import posixpath

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

# 模拟文件系统
MOCK_FILES = {
    "/data/reports/q3_sales.txt": "Q3 销售额：1000 万元，同比增长 15%。",
    "/data/reports/internal_strategy.txt": "内部战略：青龙计划将于 Q4 启动。",
    "/etc/passwd": "root:x:0:0:root:/root:/bin/bash\ndaemon:x:1:1:daemon:/usr/sbin...",
}

# 模拟符号链接：/data/reports/link_to_passwd -> /etc/passwd
MOCK_SYMLINKS = {
    "/data/reports/link_to_passwd": "/etc/passwd",
}

ALLOWED_DIRECTORIES = ["/data/reports"]

server = Server("mock-file-server-realistic")


def normalize_path(path_str: str) -> str:
    """按 POSIX 语义规范化路径，模拟 Unix filesystem server。"""
    # 展开 ~
    if path_str.startswith("~"):
        path_str = "/home/user" + path_str[1:]
    # 解析 . 和 ..
    normalized = posixpath.normpath(path_str)
    # 确保是绝对路径
    if not posixpath.isabs(normalized):
        normalized = posixpath.join("/data/reports", normalized)
    return normalized


def is_path_allowed(absolute_path: str) -> bool:
    """检查绝对路径是否在允许的目录范围内。"""
    normalized = normalize_path(absolute_path)
    for allowed in ALLOWED_DIRECTORIES:
        normalized_allowed = posixpath.normpath(allowed)
        # 路径相同或是在允许目录下
        if normalized == normalized_allowed:
            return True
        prefix = normalized_allowed.rstrip("/") + "/"
        if normalized.startswith(prefix):
            return True
    return False


async def validate_path(requested_path: str) -> str:
    """
    模拟真实 filesystem server 的 validatePath：
    1. 规范化路径
    2. 检查是否在允许目录内
    3. 检查符号链接真实目标
    4. 新文件检查父目录
    """
    absolute = normalize_path(requested_path)

    # 1. 路径规范化后是否在允许目录内
    if not is_path_allowed(absolute):
        raise PermissionError(
            f"Access denied - path outside allowed directories: {absolute} "
            f"not in {', '.join(ALLOWED_DIRECTORIES)}"
        )

    # 2. 如果路径本身是允许的目录（如 /data/reports），直接返回
    if is_path_allowed(absolute) and absolute in ALLOWED_DIRECTORIES:
        return absolute

    # 3. 处理符号链接：检查真实目标
    if absolute in MOCK_SYMLINKS:
        real_target = MOCK_SYMLINKS[absolute]
        if not is_path_allowed(real_target):
            raise PermissionError(
                f"Access denied - symlink target outside allowed directories: {real_target}"
            )
        return real_target

    # 4. 如果文件不存在，检查父目录
    if absolute not in MOCK_FILES:
        parent_dir = posixpath.dirname(absolute)
        if not is_path_allowed(parent_dir):
            raise PermissionError(
                f"Access denied - parent directory outside allowed directories: {parent_dir}"
            )

    return absolute


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="read_file",
            description="读取文件内容。只能读取允许的目录下的文件。",
            inputSchema={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "文件路径"}},
                "required": ["path"],
            },
        ),
        Tool(
            name="write_file",
            description="写入文件。只能写入允许的目录。",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "content": {"type": "string", "description": "文件内容"},
                },
                "required": ["path", "content"],
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
            description="列出目录内容。只能列出允许的目录。",
            inputSchema={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "目录路径"}},
                "required": ["path"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "read_file":
            path = arguments.get("path", "")
            valid_path = await validate_path(path)
            if valid_path not in MOCK_FILES:
                return [TextContent(type="text", text=f"❌ 文件不存在：{valid_path}")]
            return [TextContent(type="text", text=f"✅ {MOCK_FILES[valid_path]}")]

        if name == "write_file":
            path = arguments.get("path", "")
            content = arguments.get("content", "")
            valid_path = await validate_path(path)
            return [TextContent(type="text", text=f"✅ 成功写入 {valid_path}：{content[:30]}...")]

        if name == "delete_file":
            return [
                TextContent(
                    type="text",
                    text="❌ 权限拒绝：Server 策略禁止删除任何文件。",
                )
            ]

        if name == "list_directory":
            path = arguments.get("path", "")
            valid_path = await validate_path(path)
            files = [p for p in MOCK_FILES if p.startswith(valid_path.rstrip("/") + "/")]
            return [TextContent(type="text", text=f"目录 {valid_path} 包含：\n" + "\n".join(files))]

        return [TextContent(type="text", text=f"❌ 未知工具：{name}")]

    except PermissionError as e:
        return [TextContent(type="text", text=f"🚫 Server 拒绝：{str(e)}")]
    except Exception as e:
        return [TextContent(type="text", text=f"❌ Server 异常：{type(e).__name__}: {str(e)[:300]}")]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
