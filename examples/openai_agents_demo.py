"""OpenAI Agents SDK + Loop Controller 演示（v0.15.0）。

本示例展示企业内部 OpenAI Agents SDK Agent 如何接入 Loop Controller：
- Agent 自己掌握主循环（OpenAI Agents SDK Runner）；
- 每个 tool call 都经过 Loop Controller 的 R1/R2/R3 治理；
- 高风险工具（如 send_email）触发 require_approval，Agent 收到结构化提示。

前置依赖：

    uv pip install "loop-controller[openai-agents]"

运行方式：

    set LOOP_CONTROLLER_AUDIT_HMAC_KEY=0123456789abcdef...
    set OPENAI_API_KEY=sk-...
    python examples/openai_agents_demo.py

环境变量：
- ``OPENAI_API_KEY``：OpenAI Agents SDK 调用 LLM 所需；
- ``LOOP_CONTROLLER_AUDIT_HMAC_KEY``：Loop Controller 审计 HMAC key。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from loop_controller.adapters.openai_agents import govern_function_tool
from loop_controller.controller import build_controller
from loop_controller.infra.config_loader import ConfigLoader

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OPA_URL = "http://127.0.0.1:8181"


async def main() -> None:
    config = ConfigLoader().load(PROJECT_ROOT / "config", opa_base_url=OPA_URL)
    controller = await build_controller(
        config,
        opa_url=OPA_URL,
        env_extra={"PYTHONPATH": str(PROJECT_ROOT / "src")},
    )
    await controller.start()

    try:
        # 把公司内部工具包装成受治理的 OpenAI Agents SDK function_tool
        @govern_function_tool(
            controller,
            "web_search",
            "搜索公开网页资料并返回结果列表",
            agent_id="researcher_001",
            user_id="alice",
        )
        async def web_search(query: str) -> str:
            """搜索网页。"""
            return "placeholder"

        @govern_function_tool(
            controller,
            "fetch_url",
            "抓取指定 URL 的网页内容",
            agent_id="researcher_001",
            user_id="alice",
        )
        async def fetch_url(url: str) -> str:
            """抓取 URL。"""
            return "placeholder"

        @govern_function_tool(
            controller,
            "send_email",
            "发送邮件（高风险，需人工审批）",
            agent_id="researcher_001",
            user_id="alice",
        )
        async def send_email(to: str, subject: str, body: str) -> str:
            """发送邮件。"""
            return "placeholder"

        tools = [web_search, fetch_url, send_email]

        try:
            from agents import Agent, Runner
        except ImportError:
            print("未安装 openai-agents，仅展示 GovernedTool 创建成功")
            print("工具列表：")
            for tool in tools:
                print(f"  - {tool.name}: {tool.description}")
            return

        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            print("已安装 openai-agents，但未设置 OPENAI_API_KEY，仅展示 GovernedTool 创建成功")
            print("工具列表：")
            for tool in tools:
                print(f"  - {tool.name}: {tool.description}")
            return

        agent = Agent(
            name="researcher",
            instructions="你是一个企业研究助手，可以搜索网页、抓取链接，并在需要时发送邮件摘要。",
            model="gpt-4o-mini",
            tools=tools,
        )

        task = "帮我查一下 AI 合规资料，然后发邮件摘要给 zhang@company.com"
        print(f"\n[User] {task}")
        result = await Runner.run(agent, task)
        print("\n--- Agent 输出 ---")
        print(result.final_output)
    finally:
        await controller.aclose()


if __name__ == "__main__":
    asyncio.run(main())
