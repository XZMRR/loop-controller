"""Mock MCP server：send_email 只记录、web_search 返回固定结果（A14 断网可运行）。

被 MCPGateway 以 stdio 子进程拉起。发送记录追加写入 ``data/sent_emails.jsonl``，
可用环境变量 ``SENT_EMAILS_PATH`` 覆盖路径（供测试注入临时目录）。

兼容当前安装的 mcp SDK：使用 ``@server.list_tools()`` / ``@server.call_tool()``
装饰器式 handler 注册。
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import mcp.types as types
from mcp.server import Server
from mcp.server.lowlevel.server import NotificationOptions
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server

_DEFAULT_SENT_PATH = Path(__file__).resolve().parents[3] / "data" / "sent_emails.jsonl"

APP = Server("email_mock")


def _sent_path() -> Path:
    return Path(os.environ.get("SENT_EMAILS_PATH", _DEFAULT_SENT_PATH))


@APP.list_tools()
async def _on_list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="send_email",
            description="发送一封报告邮件（Mock：只记录不真发）",
            inputSchema={
                "type": "object",
                "properties": {
                    "to": {"type": "string"},
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                },
                "required": ["to", "subject", "body"],
            },
        ),
        types.Tool(
            name="web_search",
            description="搜索公开资料（Mock：返回固定结果，断网可运行）",
            inputSchema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        ),
    ]


@APP.call_tool()
async def _on_call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    if name == "send_email":
        path = _sent_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"tool": "send_email", "arguments": arguments}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
        text = json.dumps({"status": "queued"}, ensure_ascii=False)
    elif name == "web_search":
        query = str(arguments.get("query", ""))
        text = json.dumps(
            {
                "query": query,
                "results": [
                    {"title": "OpenAI 企业合规 官方政策页", "url": "https://example.com/1"},
                    {"title": "AI 治理 行业报告", "url": "https://example.com/2"},
                ],
            },
            ensure_ascii=False,
        )
    else:
        raise ValueError(f"unknown tool: {name}")
    return [types.TextContent(type="text", text=text)]


def _get_capabilities():
    # 兼容不同 mcp SDK 版本：新版本需要传入 NotificationOptions
    try:
        return APP.get_capabilities(NotificationOptions(), {})
    except TypeError:
        return APP.get_capabilities()


async def _main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await APP.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="email_mock",
                server_version="0.1.0",
                capabilities=_get_capabilities(),
            ),
        )


if __name__ == "__main__":
    asyncio.run(_main())
