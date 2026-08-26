"""参考 Harness 服务器（v0.25.0）。

接收 Loop Controller 转发的执行请求，根据工具名路由到具体后端执行，
并返回受控结果。默认自带一个 echo 示例工具与受限 shell 工具。

启动方式：
  python -m examples.contrib.harness.harness_server
或
  HARNESS_PORT=9000 python -m examples.contrib.harness.harness_server

Loop Controller 中可配置 backend：
  type: subprocess
  command: ["python", "-m", "examples.contrib.harness.harness_server"]
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from loop_controller.executors.harness_protocol import (
    HarnessExecuteRequest,
    HarnessExecuteResponse,
)

logger = logging.getLogger(__name__)


_ALLOWED_SHELL_COMMANDS: dict[str, list[str]] = {
    "echo": ["echo"],
    "ls": ["ls"],
    "pwd": ["pwd"],
}


async def _execute_echo(arguments: dict[str, Any]) -> HarnessExecuteResponse:
    text = str(arguments.get("text", ""))
    return HarnessExecuteResponse(
        status="success",
        content={"echo": text},
        metadata={"tool": "echo"},
    )


async def _execute_shell(
    arguments: dict[str, Any], sandbox: Any
) -> HarnessExecuteResponse:
    command = str(arguments.get("command", "")).strip()
    args = [str(a) for a in arguments.get("args", [])]
    allowed = _ALLOWED_SHELL_COMMANDS.get(command)
    if allowed is None:
        return HarnessExecuteResponse(
            status="error",
            error_code="harness_command_not_allowed",
            content=f"命令 {command!r} 不在允许列表中",
        )

    timeout = float(sandbox.timeout_seconds if sandbox else 30)
    max_output = int(sandbox.max_output_bytes if sandbox else 64 * 1024)

    full_cmd = allowed + args
    try:
        proc = await asyncio.create_subprocess_exec(
            *full_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except TimeoutError:
        if proc.returncode is None:
            proc.kill()
        return HarnessExecuteResponse(
            status="error",
            error_code="harness_timeout",
            content="命令执行超时",
        )

    output = stdout[:max_output].decode("utf-8", errors="replace")
    if stderr:
        output += "\n[stderr] " + stderr[:max_output].decode(
            "utf-8", errors="replace"
        )
    return HarnessExecuteResponse(
        status="success" if proc.returncode == 0 else "error",
        content={"stdout": output, "returncode": proc.returncode},
        error_code=None,
        metadata={"tool": "shell"},
    )


async def _handle_execute(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
        req = HarnessExecuteRequest.model_validate(payload)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            {
                "status": "error",
                "error_code": "harness_invalid_request",
                "content": f"请求格式非法: {exc}",
            },
            status_code=400,
        )

    if req.tool == "echo":
        result = await _execute_echo(req.arguments)
    elif req.tool == "shell":
        result = await _execute_shell(req.arguments, req.sandbox)
    else:
        result = HarnessExecuteResponse(
            status="error",
            error_code="harness_tool_not_found",
            content=f"Harness 不支持工具 {req.tool!r}",
        )

    return JSONResponse(result.model_dump(mode="json"))


async def _handle_health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


app = Starlette(
    routes=[
        Route("/harness/v1/execute", _handle_execute, methods=["POST"]),
        Route("/health", _handle_health, methods=["GET"]),
    ],
)


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("HARNESS_PORT", "9000"))
    uvicorn.run(app, host="127.0.0.1", port=port)
