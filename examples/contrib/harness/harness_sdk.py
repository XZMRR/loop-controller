"""最小 Harness SDK 示例（v0.24.0）。

Harness 是运行在 Loop Controller 之外的“工具执行代理”。
它接收 Agent 的工具调用请求，先交给 Loop Controller 做治理决策
（身份、策略、审批），Loop Controller 返回 allow 后，Harness 才在
本地沙箱/容器中真正执行工具。

运行方式：
1. 启动 Loop Controller HTTP 服务（默认 http://127.0.0.1:8080）
2. python -m examples.contrib.harness.harness_sdk
3. 向 Harness 发起 POST /invoke：
     { "tool": "shell", "args": {"command": "echo", "args": ["hello"]},
       "agent_id": "demo", "user_id": "demo" }

当前示例只演示框架，真实执行位置可替换为容器 API。
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

LC_BASE_URL = os.environ.get("LC_BASE_URL", "http://127.0.0.1:8080")
LC_API_KEY = os.environ.get("LC_API_KEY")


async def _call_loop_controller(
    tool_name: str,
    arguments: dict[str, Any],
    agent_id: str,
    user_id: str,
) -> dict[str, Any]:
    """把工具调用提案提交给 Loop Controller 进行治理决策。"""
    headers = {}
    if LC_API_KEY:
        headers["x-api-key"] = LC_API_KEY
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{LC_BASE_URL}/v1/govern/tool-call",
            headers=headers,
            json={
                "agent_id": agent_id,
                "user_id": user_id,
                "tool_name": tool_name,
                "arguments": arguments,
                "task_context": "harness-sdk-invoke",
            },
        )
        resp.raise_for_status()
        return resp.json()


async def _execute_in_sandbox(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """在 Harness 本地沙箱中执行工具（示例：shell / echo / ls）。

    生产环境应替换为容器/Docker API 调用。
    """
    if tool == "shell":
        command = args.get("command", "")
        cmd_args = [str(a) for a in args.get("args", [])]
        allowed = {"echo": ["echo"], "ls": ["ls"], "pwd": ["pwd"]}
        base = allowed.get(command)
        if base is None:
            return {"status": "error", "error": f"命令 {command!r} 不在白名单"}
        proc = await asyncio.create_subprocess_exec(
            *base, *cmd_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return {
            "status": "success" if proc.returncode == 0 else "error",
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
        }
    return {"status": "error", "error": f"未知工具 {tool!r}"}


async def _handle_invoke(request: Request) -> JSONResponse:
    payload = await request.json()
    tool = str(payload["tool"])
    args = dict(payload.get("args") or {})
    agent_id = str(payload.get("agent_id", "harness_agent"))
    user_id = str(payload.get("user_id", "harness_user"))

    # 1. 先问 Loop Controller 是否允许
    decision = await _call_loop_controller(tool, args, agent_id, user_id)
    if decision.get("status") != "allow":
        return JSONResponse({
            "status": decision.get("status"),
            "loop_controller_result": decision,
        })

    # 2. 允许后，在隔离环境中执行
    result = await _execute_in_sandbox(tool, args)
    return JSONResponse({"status": "allow", "execution_result": result})


async def _handle_health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


app = Starlette(
    routes=[
        Route("/invoke", _handle_invoke, methods=["POST"]),
        Route("/health", _handle_health, methods=["GET"]),
    ],
)


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("HARNESS_PORT", "9000"))
    uvicorn.run(app, host="127.0.0.1", port=port)
