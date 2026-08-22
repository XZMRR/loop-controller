"""裸 Python Agent + ToolGovernor 演示（v0.16.0）。

本示例展示企业内部自定义 Python Agent 如何直接通过 ``ToolGovernor``
接入 Loop Controller，无需 LangChain / OpenAI Agents / AutoGen 等框架。

Agent 自己掌握主循环，ToolGovernor 只负责每次工具调用的治理。

运行方式：

    set LOOP_CONTROLLER_AUDIT_HMAC_KEY=0123456789abcdef...
    python examples/raw_python_agent_demo.py

前置条件：

    1. 启动 OPA：
       .venv\\Scripts\\lc opa-start
    2. 已安装项目：
       uv pip install -e .
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from loop_controller import ToolGovernor
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
        # 为一个 Agent 创建通用治理层
        governor = ToolGovernor(
            controller,
            agent_id="researcher_001",
            user_id="alice",
            default_task_context="AI 合规调研并发送摘要",
        )

        # Agent 自己决定调用顺序
        print("[Agent] 调用 web_search")
        search_result = await governor.call("web_search", {"query": "AI 合规 2026"})
        print(f"[Result] {search_result}")

        print("[Agent] 调用 read_file")
        read_result = await governor.call(
            "read_file",
            {"path": "data/kb/sample.txt"},
            task_context="读取参考资料",
        )
        print(f"[Result] {read_result}")

        print("[Agent] 调用 send_email（预计触发 require_approval）")
        email_result = await governor.call(
            "send_email",
            {
                "to": "zhang@company.com",
                "subject": "AI 合规调研摘要",
                "body": "请查收附件",
            },
        )
        print(f"[Result] {email_result}")

    finally:
        await controller.aclose()


if __name__ == "__main__":
    asyncio.run(main())
