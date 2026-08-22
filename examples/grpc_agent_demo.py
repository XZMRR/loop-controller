"""gRPC 调用 Loop Controller 的 Agent 演示（v0.19.0）。

本示例展示任意语言/框架如何通过 gRPC 调用 Python 工具治理服务。

前置条件：

    1. 启动 OPA：
       .venv\\Scripts\\lc opa-start
    2. 启动 Loop Controller gRPC 服务：
       .venv\\Scripts\\python.exe -m loop_controller.cli grpc-server --port 50051
    3. 设置环境变量：
       set LOOP_CONTROLLER_AUDIT_HMAC_KEY=...

运行方式：

    .venv\\Scripts\\python.exe examples/grpc_agent_demo.py
"""

from __future__ import annotations

import asyncio
import os

from loop_controller.grpc_client import ToolGovernanceClient

TARGET = os.environ.get("LOOP_CONTROLLER_GRPC_TARGET", "localhost:50051")


async def main() -> None:
    client = ToolGovernanceClient(TARGET)
    try:
        health = await client.get_health()
        print(f"[health] {health.status} opa={health.opa_reachable} uptime={health.uptime_seconds:.2f}s")

        response = await client.evaluate_tool_call(
            agent_id="researcher_001",
            user_id="alice",
            tool_name="web_search",
            arguments={"query": "AI 合规 2026"},
            task_context="调研 AI 合规",
        )
        print(f"[web_search] status={response.status} result={response.result}")

        response = await client.evaluate_tool_call(
            agent_id="researcher_001",
            user_id="alice",
            tool_name="send_email",
            arguments={
                "to": "zhang@company.com",
                "subject": "AI 合规摘要",
                "body": "请查收",
            },
            task_context="发送调研摘要",
        )
        print(f"[send_email] status={response.status} request_id={response.request_id}")

        if response.status == "require_approval":
            request_id = response.request_id
            print(f"[agent] waiting for approval request_id={request_id}")
            async for update in client.wait_for_approval(request_id, max_wait_seconds=60):
                print(f"[wait] status={update.status} result={update.result}")
                if update.status != "pending":
                    break
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
