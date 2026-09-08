"""Mock MCP server：send_email 只记录、web_search 返回固定结果（A14 断网可运行）。

被 MCPGateway 以 stdio 子进程拉起。发送记录追加写入 ``data/sent_emails.jsonl``，
可用环境变量 ``SENT_EMAILS_PATH`` 覆盖路径（供测试注入临时目录）。

兼容 mcp>=1.0（FastMCP）与 mcp>=2.0（MCPServer）两种 API。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

try:
    from mcp.server.fastmcp import FastMCP  # type: ignore[import-untyped]

    _APP = FastMCP("email_mock")
    _TOOL_DECORATOR = _APP.tool

    def _run() -> None:  # type: ignore[no-untyped-def]
        _APP.run()

except ImportError:
    from mcp.server.mcpserver import MCPServer  # type: ignore[import-not-found]

    _APP = MCPServer("email_mock")
    _TOOL_DECORATOR = _APP.tool

    def _run() -> None:
        _APP.run()

_DEFAULT_SENT_PATH = Path(__file__).resolve().parents[3] / "data" / "sent_emails.jsonl"


def _sent_path() -> Path:
    return Path(os.environ.get("SENT_EMAILS_PATH", _DEFAULT_SENT_PATH))


@_TOOL_DECORATOR()
def send_email(to: str, subject: str, body: str) -> str:  # noqa: ARG001
    """发送一封报告邮件（Mock：只记录不真发）。"""
    path = _sent_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"tool": "send_email", "arguments": {"to": to, "subject": subject, "body": body}}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
        f.flush()
    return json.dumps({"status": "queued"}, ensure_ascii=False)


@_TOOL_DECORATOR()
def web_search(query: str) -> str:
    """搜索公开资料（Mock：返回固定结果，断网可运行）。"""
    return json.dumps(
        {
            "query": query,
            "status": "ok",
            "results": [
                {"title": "OpenAI 企业合规 官方政策页", "url": "https://example.com/1"},
                {"title": "AI 治理 行业报告", "url": "https://example.com/2"},
            ],
        },
        ensure_ascii=False,
    )


if __name__ == "__main__":
    _run()
