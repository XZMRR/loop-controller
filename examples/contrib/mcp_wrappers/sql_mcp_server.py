"""SQL 数据库的 MCP 包装示例（v0.24.0）。

与 sqlite_server.py 不同，本示例演示如何以“通用 SQL 代理”形态把 SQL 能力
暴露为 MCP 工具。Loop Controller 只负责治理决策，实际 SQL 执行发生在本
server 进程（建议跑在受控 Harness / 容器中）。

启动方式：
  DATABASE_URL=sqlite:///data/sample.db python -m examples.contrib.mcp_wrappers.sql_mcp_server
"""

from __future__ import annotations

import os
import re
import sqlite3
import urllib.parse
from typing import Any

try:
    from mcp.server import Server
    from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool
except ModuleNotFoundError as exc:
    raise SystemExit(
        "mcp SDK 未安装，请执行：uv add --dev mcp 或 pip install mcp"
    ) from exc


_READ_ONLY_RE = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)
_FORBIDDEN_RE = re.compile(r";|--")

server = Server("loop-controller-sql-wrapper")

_QUERY_TOOL = Tool(
    name="query_sql",
    description="执行只读 SQL 查询（SELECT/WITH）。",
    input_schema={
        "type": "object",
        "properties": {
            "sql": {"type": "string"},
            "parameters": {"type": "object", "default": {}},
        },
        "required": ["sql"],
    },
)

_EXECUTE_TOOL = Tool(
    name="execute_sql",
    description="执行写操作 SQL（INSERT/UPDATE/DELETE/DDL）。",
    input_schema={
        "type": "object",
        "properties": {
            "sql": {"type": "string"},
            "parameters": {"type": "object", "default": {}},
        },
        "required": ["sql"],
    },
)


def _connect() -> sqlite3.Connection:
    url = os.environ.get("DATABASE_URL", "sqlite:///data/sample.db")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "sqlite":
        raise NotImplementedError(f"当前示例仅支持 sqlite，收到: {parsed.scheme}")
    path = parsed.path or ":memory:"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


@server.list_tools()
async def _list_tools() -> ListToolsResult:
    return ListToolsResult(tools=[_QUERY_TOOL, _EXECUTE_TOOL])


@server.call_tool()
async def _call_tool(name: str, arguments: dict[str, Any] | None) -> CallToolResult:
    if arguments is None:
        arguments = {}
    sql = str(arguments.get("sql", ""))
    parameters = dict(arguments.get("parameters") or {})

    if not sql:
        return CallToolResult(
            content=[TextContent(type="text", text="缺少 sql 参数")],
            is_error=True,
        )

    if _FORBIDDEN_RE.search(sql):
        return CallToolResult(
            content=[TextContent(type="text", text="SQL 命中禁止模式 (; 或 --)")],
            is_error=True,
        )

    if name == "query_sql" and not _READ_ONLY_RE.match(sql):
        return CallToolResult(
            content=[TextContent(type="text", text="query_sql 仅允许 SELECT/WITH 语句")],
            is_error=True,
        )

    try:
        conn = _connect()
        cur = conn.execute(sql, parameters)
        if name == "query_sql":
            rows = [dict(row) for row in cur.fetchall()]
            text = repr(rows)
        else:
            conn.commit()
            text = f"rows_affected={cur.rowcount}"
        conn.close()
    except Exception as exc:  # noqa: BLE001
        return CallToolResult(
            content=[TextContent(type="text", text=f"SQL 执行失败: {exc}")],
            is_error=True,
        )

    return CallToolResult(content=[TextContent(type="text", text=text)])


async def main() -> None:
    from mcp.server.stdio import stdio_server

    async with stdio_server(server) as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
