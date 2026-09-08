"""事件驱动审批演示（v0.18.0）。

本示例展示 Agent 如何通过长轮询 ``/v1/wait-for-approval`` 等待人工审批，
而无需在 Agent 侧实现复杂的回调或 webhook。

场景：
- Agent 调用高风险工具（send_email），治理层返回 ``require_approval``；
- Agent 用返回的 ``request_id`` 调用 ``/v1/wait-for-approval`` 阻塞等待；
- 人工/另一个脚本在后台通过 ``lc approvals approve`` 审批；
- 长轮询返回最终结果，Agent 继续执行。

前置条件：

    1. 启动 OPA：
       .venv\\Scripts\\lc opa-start
    2. 启动 Loop Controller HTTP 服务：
       .venv\\Scripts\\python.exe -m loop_controller.cli server --port 8080
    3. 设置环境变量：
       set LOOP_CONTROLLER_AUDIT_HMAC_KEY=...

运行方式：

    .venv\\Scripts\\python.exe examples/http_agent_event_demo.py
"""

from __future__ import annotations

import asyncio
import os

import httpx

BASE_URL = os.environ.get("LOOP_CONTROLLER_URL", "http://127.0.0.1:8080")
API_KEY = os.environ.get("LOOP_CONTROLLER_API_KEY")


async def simulate_admin_approval(request_id: str, delay: float = 3.0) -> None:
    """模拟人工审批：先查询 pending，再调用 CLI 或 admin API 审批。"""
    await asyncio.sleep(delay)
    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        headers = {}
        if API_KEY:
            headers["X-API-Key"] = API_KEY
        # 查询待审批列表获取 decision_id
        resp = await client.get("/v1/admin/approvals/pending", headers=headers)
        pending = resp.json().get("approvals", [])
        target = next((item for item in pending if item["request_id"] == request_id), None)
        if target is None:
            print(f"[admin] request_id={request_id} not found in pending list")
            return
        decision_id = target["decision_id"]
        print(f"[admin] approving decision_id={decision_id} for request_id={request_id}")
        # 通过 CLI 审批（也可调用 MCP/admin 扩展接口）
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


async def wait_for_approval(client: httpx.AsyncClient, request_id: str, max_wait: int = 60) -> dict:
    """长轮询等待审批结果。"""
    print(f"[agent] waiting for approval request_id={request_id} ...")
    resp = await client.get(
        "/v1/wait-for-approval",
        params={"request_id": request_id, "max_wait": max_wait},
    )
    return resp.json()


async def main() -> None:
    headers = {}
    if API_KEY:
        headers["X-API-Key"] = API_KEY

    async with httpx.AsyncClient(base_url=BASE_URL, headers=headers) as client:
        # 1. 健康检查
        resp = await client.get("/health")
        print(f"[health] {resp.json()}")

        # 2. 调用高风险工具（预计触发 require_approval）
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

        # 3. 后台启动模拟审批
        approval_task = asyncio.create_task(simulate_admin_approval(request_id, delay=2.0))

        # 4. Agent 长轮询等待审批结果
        final = await wait_for_approval(client, request_id, max_wait=60)
        print(f"[agent] final result: {final}")

        # 5. 清理后台任务
        approval_task.cancel()
        try:
            await approval_task
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    asyncio.run(main())
