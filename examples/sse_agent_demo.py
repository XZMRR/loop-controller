"""SSE 事件驱动审批演示（v0.19.0）。

本示例展示 Agent 如何通过 Server-Sent Events 实时等待人工审批结果。

前置条件：

    1. 启动 OPA：
       .venv\\Scripts\\lc opa-start
    2. 启动 Loop Controller HTTP 服务：
       .venv\\Scripts\\python.exe -m loop_controller.cli server --port 8080
    3. 设置环境变量：
       set LOOP_CONTROLLER_AUDIT_HMAC_KEY=...

运行方式：

    .venv\\Scripts\\python.exe examples/sse_agent_demo.py
"""

from __future__ import annotations

import asyncio
import os

import httpx

BASE_URL = os.environ.get("LOOP_CONTROLLER_URL", "http://127.0.0.1:8080")
API_KEY = os.environ.get("LOOP_CONTROLLER_API_KEY")


async def simulate_admin_approval(request_id: str, delay: float = 3.0) -> None:
    """模拟人工审批：查询 pending 后调用 CLI 审批。"""
    await asyncio.sleep(delay)
    headers = {}
    if API_KEY:
        headers["X-API-Key"] = API_KEY
    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        resp = await client.get("/v1/admin/approvals/pending", headers=headers)
        pending = resp.json().get("approvals", [])
        target = next((item for item in pending if item["request_id"] == request_id), None)
        if target is None:
            print(f"[admin] request_id={request_id} not found")
            return
        decision_id = target["decision_id"]
        print(f"[admin] approving decision_id={decision_id}")
        proc = await asyncio.create_subprocess_shell(
            f".venv\\Scripts\\lc approvals approve {decision_id} --approver zhang_manager",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            print(f"[admin] approval failed: {stderr.decode()}")
        else:
            print(f"[admin] approval ok: {stdout.decode().strip()}")


async def wait_for_approval_sse(client: httpx.AsyncClient, request_id: str, max_wait: int = 60) -> None:
    """通过 SSE 实时等待审批结果。"""
    print(f"[agent] SSE waiting for approval request_id={request_id} ...")
    params = {"request_id": request_id, "max_wait": max_wait}
    headers = {}
    if API_KEY:
        headers["X-API-Key"] = API_KEY
    async with client.stream("GET", "/v1/wait-for-approval/sse", params=params, headers=headers) as resp:
        async for line in resp.aiter_lines():
            print(f"[sse] {line}")


async def main() -> None:
    headers = {}
    if API_KEY:
        headers["X-API-Key"] = API_KEY

    async with httpx.AsyncClient(base_url=BASE_URL, headers=headers) as client:
        resp = await client.get("/health")
        print(f"[health] {resp.json()}")

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

        if result.get("status") != "require_approval":
            print("[agent] tool did not require approval, exiting")
            return

        request_id = result["request_id"]
        approval_task = asyncio.create_task(simulate_admin_approval(request_id, delay=2.0))
        await wait_for_approval_sse(client, request_id, max_wait=60)
        approval_task.cancel()
        try:
            await approval_task
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    asyncio.run(main())
