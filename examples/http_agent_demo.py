"""通过 HTTP 调用 Loop Controller 的 Agent 演示（v0.17.0）。

本示例展示任意语言/任意框架的 Agent 如何通过 HTTP 接入 Loop Controller：
- Agent 不需要安装 loop-controller 包；
- 只需要能发 HTTP POST 请求；
- Loop Controller 作为独立服务运行。

前置条件：

    1. 启动 OPA：
       .venv\\Scripts\\lc opa-start
    2. 启动 Loop Controller HTTP 服务：
       .venv\\Scripts\\python.exe -m loop_controller.cli server --port 8080
    3. 设置环境变量：
       set LOOP_CONTROLLER_AUDIT_HMAC_KEY=...

运行方式：

    .venv\\Scripts\\python.exe examples/http_agent_demo.py
"""

from __future__ import annotations

import asyncio
import os

import httpx

BASE_URL = os.environ.get("LOOP_CONTROLLER_URL", "http://127.0.0.1:8080")
API_KEY = os.environ.get("LOOP_CONTROLLER_API_KEY")


async def main() -> None:
    headers = {}
    if API_KEY:
        headers["X-API-Key"] = API_KEY

    async with httpx.AsyncClient(base_url=BASE_URL, headers=headers) as client:
        # 1. 健康检查
        resp = await client.get("/health")
        print(f"[health] {resp.json()}")

        # 2. 调用低风险工具
        resp = await client.post(
            "/v1/govern/tool-call",
            json={
                "agent_id": "researcher_001",
                "user_id": "alice",
                "tool_name": "web_search",
                "arguments": {"query": "AI 合规 2026"},
                "task_context": "调研 AI 合规",
            },
        )
        print(f"[web_search] {resp.json()}")

        # 3. 调用高风险工具（预计触发 require_approval）
        resp = await client.post(
            "/v1/govern/tool-call",
            json={
                "agent_id": "researcher_001",
                "user_id": "alice",
                "tool_name": "send_email",
                "arguments": {
                    "to": "zhang@company.com",
                    "subject": "AI 合规摘要",
                    "body": "请查收",
                },
                "task_context": "发送调研摘要",
            },
        )
        result = resp.json()
        print(f"[send_email] {result}")

        if result.get("status") == "require_approval":
            request_id = result.get("request_id")
            print("\n请在另一个终端执行审批：")
            print("  .venv\\Scripts\\lc approvals approve <decision_id> --approver zhang_manager")
            print("然后按回车继续，模拟 Agent 调用 resume-after-approval...")
            input()

            resp = await client.post(
                "/v1/govern/resume-after-approval",
                json={"request_id": request_id},
            )
            print(f"[resume] {resp.json()}")


if __name__ == "__main__":
    asyncio.run(main())
