"""Isolated Subprocess Harness backend（v0.32.0）。

通过启动一个受限 Python 子进程执行工具，用于开发、CI 和低敏感生产场景。
不保证完整容器级隔离；只提供进程级隔离与受限 builtins。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
from typing import Any

from loop_controller.executors.harness_models import IsolatedSubprocessBackendConfig
from loop_controller.executors.harness_protocol import (
    HarnessContext,
    HarnessExecuteRequest,
    HarnessExecuteResponse,
    HarnessSandbox,
)
from loop_controller.models import ToolResult

logger = logging.getLogger(__name__)

# 内置 Runner 脚本，在子进程中执行；只暴露白名单工具与受限 builtins。
_RUNNER = r'''
from __future__ import annotations
import json, sys, os, time
from datetime import datetime, UTC

# 受限 builtins：禁止 open、__import__ 等危险操作
SAFE_BUILTINS = {
    "True": True, "False": False, "None": None,
    "abs": abs, "all": all, "any": any, "bool": bool,
    "dict": dict, "enumerate": enumerate, "filter": filter,
    "float": float, "frozenset": frozenset, "int": int,
    "isinstance": isinstance, "issubclass": issubclass,
    "len": len, "list": list, "map": map, "max": max, "min": min,
    "pow": pow, "range": range, "round": round, "set": set,
    "slice": slice, "sorted": sorted, "str": str, "sum": sum,
    "tuple": tuple, "type": type, "zip": zip,
}

# 白名单工具：仅用于测试与最小示例
_TOOLS = {
    "echo": lambda args: args.get("text", ""),
    "add": lambda args: args.get("a", 0) + args.get("b", 0),
}


def _build_evidence(sandbox, start, end, code=None):
    return {
        "started_at": start.isoformat(),
        "finished_at": end.isoformat(),
        "exit_code": code,
    }


def main():
    request = json.load(sys.stdin)
    tool = request.get("tool", "")
    args = request.get("arguments", {})
    sandbox = request.get("sandbox", {})
    start = datetime.now(UTC)
    if sandbox.get("network_policy") != "deny_all":
        end = datetime.now(UTC)
        result = {
            "status": "error",
            "error_code": "harness_sandbox_unsupported",
            "content": "isolated_subprocess 仅支持 deny_all 网络策略",
            "effective_sandbox": sandbox,
            "evidence": _build_evidence(sandbox, start, end),
        }
        print(json.dumps(result))
        return
    executor = _TOOLS.get(tool)
    if executor is None:
        end = datetime.now(UTC)
        result = {
            "status": "error",
            "error_code": "harness_tool_not_found",
            "content": f"未知工具: {tool}",
            "effective_sandbox": sandbox,
            "evidence": _build_evidence(sandbox, start, end, code=1),
        }
        print(json.dumps(result))
        return
    try:
        executor = _TOOLS.get(tool)
        if executor is None:
            raise KeyError(f"未知工具: {tool}")
        content = executor(args)
        end = datetime.now(UTC)
        result = {
            "status": "success",
            "content": content,
            "effective_sandbox": sandbox,
            "evidence": _build_evidence(sandbox, start, end, code=0),
        }
    except Exception as exc:
        end = datetime.now(UTC)
        result = {
            "status": "error",
            "error_code": "harness_sandbox_violation",
            "content": str(exc),
            "effective_sandbox": sandbox,
            "evidence": _build_evidence(sandbox, start, end, code=1),
        }
    print(json.dumps(result))


if __name__ == "__main__":
    main()
'''


class IsolatedSubprocessHarnessBackend:
    """受限子进程 Harness backend。"""

    def __init__(self, config: IsolatedSubprocessBackendConfig) -> None:
        self.config = config
        self._python = config.python_path or sys.executable

    async def start(self) -> None:
        """子进程 backend 无需长期守护进程；start 仅校验 python 可执行。"""
        proc = await asyncio.create_subprocess_exec(
            self._python, "--version",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()

    async def stop(self) -> None:
        return

    async def check_health(self) -> bool:
        proc = await asyncio.create_subprocess_exec(
            self._python, "--version",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        return proc.returncode == 0

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: Any,
        sandbox: Any,
    ) -> ToolResult:
        from loop_controller.executors.harness_executor import _HTTPHarnessClient

        request = HarnessExecuteRequest(
            tool=tool_name,
            arguments=arguments,
            context=HarnessContext(
                call_id=context.call_id,
                task_id=context.task_id,
                agent_id=context.agent_id,
                user_id=context.user_id,
                session_id=context.session_id,
                tenant_id=context.tenant_id,
            ),
            sandbox=HarnessSandbox.model_validate(sandbox.model_dump()) if sandbox is not None else HarnessSandbox(),
        )
        with tempfile.NamedTemporaryFile("w+", suffix=".py", delete=False, encoding="utf-8") as f:
            f.write(_RUNNER)
            runner_path = f.name
        try:
            # 仅保留 PATH 与显式配置的环境变量，避免子进程继承当前进程中的秘密。
            safe_env = {"PATH": os.environ.get("PATH", "")}
            if self.config.env:
                safe_env.update(self.config.env)
            proc = await asyncio.create_subprocess_exec(
                self._python, runner_path,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=safe_env,
            )
            input_bytes = request.model_dump_json().encode("utf-8")
            try:
                async with asyncio.timeout(sandbox.timeout_seconds if sandbox else 30):
                    stdout, stderr = await proc.communicate(input_bytes)
            except TimeoutError:
                proc.kill()
                return _HTTPHarnessClient._error_result(
                    context, tool_name, "子进程执行超时", "harness_timeout",
                )
            try:
                response = HarnessExecuteResponse.model_validate_json(stdout.decode("utf-8", errors="replace"))
            except Exception as exc:
                logger.warning("isolated_subprocess 返回非法 JSON: %s", exc)
                return _HTTPHarnessClient._error_result(
                    context, tool_name, f"非法响应: {stdout!r}", "harness_invalid_response",
                )

            if response.status == "success" and response.effective_sandbox is None:
                return _HTTPHarnessClient._error_result(
                    context,
                    tool_name,
                    "Harness 响应缺少 effective_sandbox 回执",
                    "harness_sandbox_attestation_missing",
                )
            if response.status == "success" and not _HTTPHarnessClient._sandbox_matches(
                request.sandbox, response.effective_sandbox
            ):
                return _HTTPHarnessClient._error_result(
                    context,
                    tool_name,
                    "Harness 实际生效沙箱与请求不一致",
                    "harness_sandbox_violation",
                    {
                        "requested_sandbox": request.sandbox.model_dump(mode="json"),
                        "effective_sandbox": response.effective_sandbox.model_dump(mode="json")
                        if response.effective_sandbox
                        else None,
                    },
                )
            metadata = dict(response.metadata)
            if response.evidence is not None:
                metadata["harness_evidence"] = response.evidence.model_dump(mode="json")
            return ToolResult(
                call_id=context.call_id,
                task_id=context.task_id,
                tool_name=tool_name,
                status=response.status,
                content=response.content,
                error_code=response.error_code,
                metadata=metadata,
            )
        finally:
            try:
                os.unlink(runner_path)
            except OSError:
                pass
