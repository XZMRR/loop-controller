"""本地 sqlite MCP server（v0.9.0 生产验证）。

使用 ``sqlite3`` 执行真实 SQL，不依赖外部 npm 包。
提供 ``query``（只读 SELECT）和 ``execute``（写操作）两个工具，便于 R2 区分风险等级。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Any

from mcp import types  # type: ignore[import-untyped]
from mcp.server import Server  # type: ignore[import-untyped]
from mcp.server.stdio import stdio_server  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)

_QUERY_TOOL = types.Tool(
    name="query",
    description="Execute a read-only SQL query (SELECT). Returns JSON rows.",
    inputSchema={
        "type": "object",
        "properties": {
            "sql": {
                "type": "string",
                "description": "The SQL query to execute",
            },
        },
        "required": ["sql"],
    },
)

_EXECUTE_TOOL = types.Tool(
    name="execute",
    description="Execute a write SQL statement (INSERT/UPDATE/DELETE/DDL). Use with caution.",
    inputSchema={
        "type": "object",
        "properties": {
            "sql": {
                "type": "string",
                "description": "The SQL statement to execute",
            },
        },
        "required": ["sql"],
    },
)


def _create_server(db_path: Path) -> Server[Any]:
    server: Server[Any] = Server("loop-controller-sqlite")

    @server.list_tools()  # type: ignore[misc]
    async def _list_tools(_request: types.ListToolsRequest) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[_QUERY_TOOL, _EXECUTE_TOOL])

    @server.call_tool()  # type: ignore[misc]
    async def _call_tool(name: str, arguments: dict[str, Any] | None) -> types.CallToolResult:
        args = dict(arguments or {})
        sql = args.get("sql", "")
        if not isinstance(sql, str) or not sql:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text="'sql' is required")],
                isError=True,
            )
        try:
            conn = sqlite3.connect(str(db_path))
            try:
                if name == "query":
                    sql_upper = sql.strip().upper()
                    if not sql_upper.startswith("SELECT") and not sql_upper.startswith("WITH"):
                        return types.CallToolResult(
                            content=[types.TextContent(type="text", text="'query' tool only supports SELECT")],
                            isError=True,
                        )
                    conn.row_factory = sqlite3.Row
                    rows = [dict(row) for row in conn.execute(sql)]
                    text = json.dumps(rows, ensure_ascii=False, default=str)
                elif name == "execute":
                    cur = conn.execute(sql)
                    conn.commit()
                    text = json.dumps({"rows_affected": cur.rowcount}, ensure_ascii=False)
                else:
                    return types.CallToolResult(
                        content=[types.TextContent(type="text", text=f"Unknown tool: {name}")],
                        isError=True,
                    )
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"sqlite error: {exc}")],
                isError=True,
            )
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)])

    return server


async def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/company.db")
    db_path = db_path.resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    server = _create_server(db_path)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
