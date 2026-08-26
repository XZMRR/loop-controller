"""子进程执行后端示例（v0.25.0）。

演示如何在不启动 HTTP 服务器的情况下，直接在 Harness 中执行受限命令。
生产环境建议通过 harness_server.py 走 HTTP 层，以便统一鉴权和审计。
"""

from __future__ import annotations

import asyncio
from typing import Any


class SubprocessBackend:
    """直接启动子进程执行工具的示例后端。"""

    async def execute(
        self,
        tool: str,
        arguments: dict[str, Any],
        *,
        timeout_seconds: float = 30.0,
        max_output_bytes: int = 64 * 1024,
    ) -> dict[str, Any]:
        if tool != "shell":
            return {"status": "error", "error_code": "unsupported_tool"}

        command = str(arguments.get("command", "")).strip()
        args = [str(a) for a in arguments.get("args", [])]
        allowed = {"echo": ["echo"], "ls": ["ls"], "pwd": ["pwd"]}
        base = allowed.get(command)
        if base is None:
            return {
                "status": "error",
                "error_code": "command_not_allowed",
                "content": f"命令 {command!r} 不在白名单",
            }

        proc = await asyncio.create_subprocess_exec(
            *base, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_seconds
            )
        except TimeoutError:
            proc.kill()
            return {"status": "error", "error_code": "timeout"}

        output = stdout[:max_output_bytes].decode("utf-8", errors="replace")
        if stderr:
            output += "\n[stderr] " + stderr[:max_output_bytes].decode(
                "utf-8", errors="replace"
            )
        return {
            "status": "success" if proc.returncode == 0 else "error",
            "content": output,
        }


if __name__ == "__main__":
    import asyncio

    backend = SubprocessBackend()
    print(asyncio.run(backend.execute("shell", {"command": "echo", "args": ["hello"]})))
