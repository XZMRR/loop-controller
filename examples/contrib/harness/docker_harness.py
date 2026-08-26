"""容器化 Harness 示例（v0.24.0）。

此示例演示如何用 Docker 作为执行隔离环境：
- Harness 接收到 Loop Controller 的 allow 决策后；
- 把工具调用参数序列化；
- 启动一个一次性的 Docker 容器执行实际工具；
- 收集 stdout 并返回。

前置条件：
  - 已安装 Docker 并启动 daemon；
  - 已构建镜像：docker build -t lc-tool-runner -f examples/contrib/harness/Dockerfile .

运行方式：
  LC_BASE_URL=http://127.0.0.1:8080 python -m examples.contrib.harness.docker_harness
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

LC_BASE_URL = os.environ.get("LC_BASE_URL", "http://127.0.0.1:8080")
LC_API_KEY = os.environ.get("LC_API_KEY")
DOCKER_IMAGE = os.environ.get("LC_TOOL_RUNNER_IMAGE", "lc-tool-runner")


async def _call_loop_controller(
    tool_name: str,
    arguments: dict[str, Any],
    agent_id: str,
    user_id: str,
) -> dict[str, Any]:
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
                "task_context": "docker-harness-invoke",
            },
        )
        resp.raise_for_status()
        return resp.json()


async def _execute_in_docker(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """启动一次性 Docker 容器执行工具调用。"""
    # 把参数写入临时文件，挂载进容器，避免通过 shell 拼接命令
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump({"tool": tool, "args": args}, f)
        input_path = f.name

    try:
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "run",
            "--rm",
            "--read-only",
            "--network=none",
            "--memory=128m",
            "--cpus=0.5",
            "-v",
            f"{input_path}:/input.json:ro",
            DOCKER_IMAGE,
            "/input.json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            return {
                "status": "error",
                "stdout": stdout.decode("utf-8", errors="replace"),
                "stderr": stderr.decode("utf-8", errors="replace"),
            }
        try:
            output = json.loads(stdout.decode("utf-8"))
        except json.JSONDecodeError:
            output = {"stdout": stdout.decode("utf-8", errors="replace")}
        return {"status": "success", "output": output}
    finally:
        os.unlink(input_path)


async def _handle_invoke(request: Request) -> JSONResponse:
    payload = await request.json()
    tool = str(payload["tool"])
    args = dict(payload.get("args") or {})
    agent_id = str(payload.get("agent_id", "harness_agent"))
    user_id = str(payload.get("user_id", "harness_user"))

    decision = await _call_loop_controller(tool, args, agent_id, user_id)
    if decision.get("status") != "allow":
        return JSONResponse({
            "status": decision.get("status"),
            "loop_controller_result": decision,
        })

    result = await _execute_in_docker(tool, args)
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

    port = int(os.environ.get("HARNESS_PORT", "9001"))
    uvicorn.run(app, host="127.0.0.1", port=port)
