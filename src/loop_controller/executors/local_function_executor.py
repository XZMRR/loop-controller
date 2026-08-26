"""本地函数执行器：在独立子进程中调用 Python 函数（v0.23.0）。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from loop_controller.executors.base import ExecutionContext, ToolExecutor
from loop_controller.executors.local_function_models import LocalFunctionSpec
from loop_controller.models import CapabilityProfile, Tool, ToolResult

logger = logging.getLogger(__name__)


class LocalFunctionExecutor(ToolExecutor):
    """通过子进程隔离执行本地 Python 函数。"""

    def __init__(
        self,
        tool_specs: dict[str, LocalFunctionSpec],
        *,
        python_executable: str | None = None,
    ) -> None:
        self._tool_specs = tool_specs
        self._python_executable = python_executable or sys.executable

    def _get_spec(self, tool_name: str) -> LocalFunctionSpec:
        spec = self._tool_specs.get(tool_name)
        if spec is None:
            raise KeyError(tool_name)
        return spec

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: ExecutionContext,
    ) -> ToolResult:
        """序列化参数并通过子进程调用本地函数，返回 ToolResult。"""
        try:
            spec = self._get_spec(tool_name)
        except KeyError:
            return self._error_result(
                context, tool_name, f"本地函数 {tool_name!r} 未注册", "local_function_not_found"
            )

        payload = {
            "module": spec.module,
            "function": spec.function,
            "arguments": arguments,
            "sandbox": {
                "timeout_seconds": spec.sandbox.timeout_seconds,
                "max_output_bytes": spec.sandbox.max_output_bytes,
                "allowed_paths": spec.sandbox.allowed_paths,
                "env_whitelist": spec.sandbox.env_whitelist,
            },
        }

        # 构造 runner 命令；使用与主进程相同的解释器，确保模块可导入
        cmd = [
            self._python_executable,
            "-m",
            "loop_controller.executors.local_function_runner",
        ]

        # 仅保留白名单环境变量
        env = self._build_env(spec)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("本地函数子进程启动失败")
            return self._error_result(
                context, tool_name, f"子进程启动失败: {exc}", "local_function_runtime_error"
            )

        try:
            input_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            max_output = spec.sandbox.max_output_bytes
            stdout, stderr, hit_limit = await asyncio.wait_for(
                self._communicate_with_limit(proc, input_bytes, max_output),
                timeout=spec.sandbox.timeout_seconds,
            )
        except TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
            return self._error_result(
                context, tool_name, "本地函数执行超时", "local_function_timeout"
            )

        if hit_limit:
            return self._error_result(
                context,
                tool_name,
                f"子进程输出超过 {max_output} 字节限制",
                "local_function_output_too_large",
            )

        if proc.returncode != 0:
            stderr_text = stderr.decode("utf-8", errors="replace")[:500]
            return self._error_result(
                context,
                tool_name,
                f"子进程异常退出({proc.returncode}): {stderr_text}",
                "local_function_runtime_error",
            )

        if not stdout:
            return self._error_result(
                context,
                tool_name,
                "子进程无输出",
                "local_function_invalid_output",
            )

        stdout_text = stdout.decode("utf-8", errors="replace")

        try:
            result_doc = json.loads(stdout_text)
        except json.JSONDecodeError as exc:
            return self._error_result(
                context,
                tool_name,
                f"子进程输出非法 JSON: {exc}",
                "local_function_invalid_output",
            )

        status = result_doc.get("status", "error")
        if status == "success":
            return ToolResult(
                call_id=context.call_id,
                task_id=context.task_id,
                tool_name=tool_name,
                status="success",
                content=result_doc.get("content"),
                elapsed_ms=0,
            )

        return self._error_result(
            context,
            tool_name,
            result_doc.get("content", ""),
            result_doc.get("error_code", "local_function_runtime_error"),
        )

    # 子进程启动所需的最小系统/运行时环境变量（平台相关）。
    # 仅当 env_whitelist 非空时用于兜底，避免白名单过严导致 Python 无法定位依赖。
    _ESSENTIAL_ENV_VARS: frozenset[str] = frozenset(
        {
            "PATH",
            "SYSTEMROOT",
            "SYSTEMDRIVE",
            "APPDATA",
            "LOCALAPPDATA",
            "USERPROFILE",
            "HOME",
            "USER",
            "USERNAME",
            "USERDOMAIN",
            "TEMP",
            "TMP",
            "PYENV_ROOT",
            "VIRTUAL_ENV",
            "PYTHONHOME",
            "PYTHONNOUSERSITE",
            "PYTHONUTF8",
            "PYTHONIOENCODING",
            "PYTHONDONTWRITEBYTECODE",
        }
    )

    def _build_env(self, spec: LocalFunctionSpec) -> dict[str, str]:
        """根据 env_whitelist 构造子进程环境变量。"""
        import loop_controller

        package_dir = Path(loop_controller.__file__).parent.parent
        pythonpath = str(package_dir)
        user_pythonpath = os.environ.get("PYTHONPATH", "")
        if user_pythonpath:
            pythonpath = f"{pythonpath}{os.pathsep}{user_pythonpath}"

        # 沙箱环境变量策略：
        # - 始终保留系统必要变量（PATH/APPDATA 等）+ PYTHONPATH，保证 Python 能正常启动；
        # - 若配置了 env_whitelist，则额外透明白名单中的用户环境变量；
        # - 未配置白名单时**不**继承全部环境变量，避免敏感信息泄露。
        filtered_env: dict[str, str] = {}
        for key in self._ESSENTIAL_ENV_VARS:
            if key in os.environ:
                filtered_env[key] = os.environ[key]
        for key in spec.sandbox.env_whitelist:
            if key in os.environ and key not in filtered_env:
                filtered_env[key] = os.environ[key]
        # PYTHONPATH 必须保留，否则 runner 找不到 loop_controller 包
        if "PYTHONPATH" not in filtered_env:
            filtered_env["PYTHONPATH"] = pythonpath
        else:
            filtered_env["PYTHONPATH"] = f"{pythonpath}{os.pathsep}{filtered_env['PYTHONPATH']}"
        return filtered_env

    @staticmethod
    async def _read_stream_limited(
        stream: asyncio.StreamReader,
        chunks: list[bytes],
        limit: int,
    ) -> int:
        """按块读取子进程输出，累计超过 limit 后停止读取。"""
        total = 0
        while total <= limit:
            chunk = await stream.read(8192)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        return total

    async def _communicate_with_limit(
        self,
        proc: asyncio.subprocess.Process,
        input_bytes: bytes,
        max_output: int,
    ) -> tuple[bytes, bytes, bool]:
        """向子进程发送输入并流式读取 stdout/stderr，超过限制时截断。

        Returns:
            (stdout, stderr, hit_limit)
        """
        if proc.stdin is not None:
            proc.stdin.write(input_bytes)
            await proc.stdin.drain()
            proc.stdin.close()

        assert proc.stdout is not None and proc.stderr is not None

        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        stderr_limit = max(4096, max_output)
        stdout_task = asyncio.create_task(
            self._read_stream_limited(proc.stdout, stdout_chunks, max_output)
        )
        stderr_task = asyncio.create_task(
            self._read_stream_limited(proc.stderr, stderr_chunks, stderr_limit)
        )

        try:
            await asyncio.gather(stdout_task, stderr_task)
        except asyncio.CancelledError:
            # 外部 wait_for 超时：立即 kill 子进程并取消读取任务，重新抛出 CancelledError
            # 让 wait_for 可以正确返回 TimeoutError。
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
            stdout_task.cancel()
            stderr_task.cancel()
            raise

        stdout_total = sum(len(c) for c in stdout_chunks)
        stderr_total = sum(len(c) for c in stderr_chunks)
        hit_limit = stdout_total > max_output or stderr_total > stderr_limit

        if hit_limit:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
            stdout_task.cancel()
            stderr_task.cancel()
        else:
            await proc.wait()

        return b"".join(stdout_chunks), b"".join(stderr_chunks), hit_limit

    async def list_tools(self, profile: CapabilityProfile) -> list[Tool]:
        """返回本地函数工具元数据列表，按 Profile 过滤。"""
        allowed = set(profile.tools.keys()) if profile.tools else None
        tools: list[Tool] = []
        for name, spec in self._tool_specs.items():
            if allowed is not None and name not in allowed:
                continue
            tools.append(spec.to_tool())
        return tools

    @staticmethod
    def _error_result(
        context: ExecutionContext,
        tool_name: str,
        message: str,
        error_code: str,
    ) -> ToolResult:
        return ToolResult(
            call_id=context.call_id,
            task_id=context.task_id,
            tool_name=tool_name,
            status="error",
            content=message,
            error_code=error_code,
        )
