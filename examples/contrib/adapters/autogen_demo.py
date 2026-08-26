"""AutoGen + Loop Controller 演示（v0.15.0）。

本示例展示企业内部 AutoGen Agent 如何接入 Loop Controller：
- Agent 自己掌握主循环（autogen_agentchat AssistantAgent）；
- 每个 tool call 都经过 Loop Controller 的 R1/R2/R3 治理；
- 高风险工具（如 send_email）触发 require_approval，Agent 收到结构化提示。

前置依赖：

    uv pip install loop-controller autogen-agentchat

运行方式：

    set LOOP_CONTROLLER_AUDIT_HMAC_KEY=0123456789abcdef...
    set OPENAI_API_KEY=sk-...
    python examples/contrib/adapters/autogen_demo.py

环境变量：
- ``OPENAI_API_KEY``：AutoGen Agent 调用 LLM 所需；
- ``LOOP_CONTROLLER_AUDIT_HMAC_KEY``：Loop Controller 审计 HMAC key。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from autogen_adapter import govern_tool

from loop_controller.controller import build_controller
from loop_controller.infra.config_loader import ConfigLoader

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
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
        # 把公司内部工具包装成受治理的 AutoGen 工具函数
        @govern_tool(controller, "web_search", agent_id="researcher_001", user_id="alice")
        async def web_search(query: str) -> str:
            """搜索公开网页资料并返回结果列表。"""
            return "placeholder"

        @govern_tool(controller, "fetch_url", agent_id="researcher_001", user_id="alice")
        async def fetch_url(url: str) -> str:
            """抓取指定 URL 的网页内容。"""
            return "placeholder"

        @govern_tool(controller, "send_email", agent_id="researcher_001", user_id="alice")
        async def send_email(to: str, subject: str, body: str) -> str:
            """发送邮件（高风险，需人工审批）。"""
            return "placeholder"

        tools = [web_search, fetch_url, send_email]

        try:
            from autogen_agentchat.agents import AssistantAgent
            from autogen_ext.models.openai import OpenAIChatCompletionClient
        except ImportError:
            print("未安装 autogen-agentchat / autogen-ext，仅展示 GovernedTool 创建成功")
            print("工具列表：")
            for tool in tools:
                print(f"  - {tool.__name__}: {tool.__doc__}")
            return

        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            print("已安装 autogen，但未设置 OPENAI_API_KEY，仅展示 GovernedTool 创建成功")
            print("工具列表：")
            for tool in tools:
                print(f"  - {tool.__name__}: {tool.__doc__}")
            return

        agent = AssistantAgent(
            name="researcher",
            model_client=OpenAIChatCompletionClient(
                model="gpt-4o-mini",
                api_key=api_key,
            ),
            tools=tools,
            system_message="你是一个企业研究助手，可以搜索网页、抓取链接，并在需要时发送邮件摘要。",
        )

        task = "帮我查一下 AI 合规资料，然后发邮件摘要给 zhang@company.com"
        print(f"\n[User] {task}")
        result = await agent.run(task=task)
        print("\n--- Agent 输出 ---")
        print(result.messages[-1].content)
    finally:
        await controller.aclose()


if __name__ == "__main__":
    asyncio.run(main())
