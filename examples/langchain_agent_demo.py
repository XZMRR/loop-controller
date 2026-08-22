"""LangChain Agent + Loop Controller 演示（v0.13.0）。

本示例展示企业内部 LangChain / LangGraph Agent 如何接入 Loop Controller：
- Agent 自己掌握主循环（LangGraph create_react_agent）；
- 每个 tool call 都经过 Loop Controller 的 R1/R2/R3 治理；
- 高风险工具（如 send_email）触发 require_approval，Agent 收到结构化提示。

前置依赖：

    uv pip install langchain langchain-openai langgraph

运行方式：

    set LOOP_CONTROLLER_AUDIT_HMAC_KEY=0123456789abcdef...
    set OPENAI_API_KEY=sk-...
    python examples/langchain_agent_demo.py

环境变量：
- ``OPENAI_API_KEY``：LangChain Agent 调用 LLM 所需；
- ``LOOP_CONTROLLER_AUDIT_HMAC_KEY``：Loop Controller 审计 HMAC key。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from loop_controller.adapters.langchain import GovernedTool
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
        # 把公司内部工具包装成受治理的 LangChain Tool
        tools = [
            GovernedTool(
                controller,
                "web_search",
                "搜索公开网页资料并返回结果列表",
                agent_id="researcher_001",
                user_id="alice",
            ),
            GovernedTool(
                controller,
                "fetch_url",
                "抓取指定 URL 的网页内容",
                agent_id="researcher_001",
                user_id="alice",
            ),
            GovernedTool(
                controller,
                "send_email",
                "发送邮件（高风险，需人工审批）",
                agent_id="researcher_001",
                user_id="alice",
            ),
        ]

        # 如果已安装 langchain / langchain-openai / langgraph，则继续演示
        try:
            from langchain_openai import ChatOpenAI
            from langgraph.prebuilt import create_react_agent
        except ImportError:
            print("未安装 langchain / langchain-openai / langgraph，仅展示 GovernedTool 创建成功")
            print("工具列表：")
            for tool in tools:
                print(f"  - {tool.name}: {tool.description}")
            return

        # 支持 OpenAI 或 DeepSeek 兼容 API
        api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
        base_url = os.environ.get("OPENAI_BASE_URL") or os.environ.get("DEEPSEEK_BASE_URL")
        model_name = os.environ.get("MODEL", "deepseek-v4-flash")
        if not api_key:
            print("已安装 langchain，但未设置 OPENAI_API_KEY 或 DEEPSEEK_API_KEY，仅展示 GovernedTool 创建成功")
            print("工具列表：")
            for tool in tools:
                print(f"  - {tool.name}: {tool.description}")
            return

        # LangGraph ReAct Agent
        kwargs: dict[str, Any] = {
            "model": model_name,
            "temperature": 0,
            "api_key": api_key,
        }
        if base_url:
            kwargs["base_url"] = base_url
        model = ChatOpenAI(**kwargs)
        agent = create_react_agent(model=model, tools=tools)

        # 这个任务可能触发 send_email 的 require_approval。
        task = "帮我查一下 AI 合规资料，然后发邮件摘要给 zhang@company.com"
        print(f"\n[User] {task}")
        response = await agent.ainvoke(
            {"messages": [{"role": "user", "content": task}]}
        )
        print("\n--- Agent 输出 ---")
        print(response["messages"][-1].content)
    finally:
        await controller.aclose()


if __name__ == "__main__":
    asyncio.run(main())
