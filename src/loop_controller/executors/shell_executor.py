"""Shell 执行器：在独立子进程中执行受限命令（v0.24.0）。"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
from typing import Any

from loop_controller.executors.base import ExecutionContext, ToolExecutor
from loop_controller.executors.shell_models import ShellToolSpec
from loop_controller.models import CapabilityProfile, Tool, ToolResult


class ShellExecutor(ToolExecutor):
    """通过子进程隔离执行受限 Shell/CLI 命令。"""

    # 子进程启动所需的最小系统/运行时环境变量（平台相关）。
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
        }
    )

    def __init__(
        self,
        tool_specs: dict[str, ShellToolSpec],
        *,
        env_extra: dict[str, str] | None = None,
    ) -> None:
        self._tool_specs = tool_specs
        self._env_extra = env_extra or {}

    def _get_spec(self, tool_name: str) -> ShellToolSpec:
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
        """渲染命令模板、校验参数、执行子进程并返回 ToolResult。"""
        try:
            spec = self._get_spec(tool_name)
        except KeyError:
            return self._error_result(
                context, tool_name, f"Shell 工具 {tool_name!r} 未注册", "shell_command_not_found"
            )

        try:
            command = self._render_command(spec, arguments)
        except ValueError as exc:
            return self._error_result(context, tool_name, str(exc), self._error_code_for(exc))

        env = self._build_env(spec)

        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
        except Exception as exc:  # noqa: BLE001
            return self._error_result(
                context, tool_name, f"子进程启动失败: {exc}", "shell_runtime_error"
            )

        try:
            max_output = spec.sandbox.max_output_bytes
            stdout, stderr, hit_limit = await asyncio.wait_for(
                self._communicate_with_limit(proc, max_output),
                timeout=spec.sandbox.timeout_seconds,
            )
        except TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
            return self._error_result(
                context, tool_name, "Shell 命令执行超时", "shell_timeout"
            )

        if hit_limit:
            return self._error_result(
                context,
                tool_name,
                f"子进程输出超过 {max_output} 字节限制",
                "shell_output_too_large",
            )

        if proc.returncode != 0:
            stderr_text = stderr.decode("utf-8", errors="replace")[:500]
            return self._error_result(
                context,
                tool_name,
                f"命令退出码 {proc.returncode}: {stderr_text}".rstrip(),
                "shell_runtime_error",
            )

        stdout_text = stdout.decode("utf-8", errors="replace")
        return ToolResult(
            call_id=context.call_id,
            task_id=context.task_id,
            tool_name=tool_name,
            status="success",
            content=stdout_text,
            elapsed_ms=0,
        )

    @staticmethod
    def _render_command(spec: ShellToolSpec, arguments: dict[str, Any]) -> list[str]:
        """把 command_template 中的占位符替换为参数值。

        占位符可以是完整 token，也可以是某个 token 的子串（例如字符串模板内）。

        Raises:
            ValueError: 参数缺失、未声明、不在白名单或包含危险字符。
        """
        placeholders = spec.placeholders

        # 校验每个占位符都有允许值列表
        missing_allowed = placeholders - set(spec.allowed_args.keys())
        if missing_allowed:
            raise ValueError(
                f"命令模板中的占位符缺少 allowed_args: {sorted(missing_allowed)}"
            )

        # 校验传入参数：先检查注入字符，再检查白名单
        for name in placeholders:
            if name not in arguments:
                raise ValueError(f"缺少参数 {name!r}")
            value = str(arguments[name])
            if spec.forbidden_pattern.search(value):
                raise ValueError(
                    f"参数 {name}={value!r} 包含禁止的 shell 元字符"
                )
            allowed = spec.allowed_args.get(name, [])
            if allowed and value not in allowed:
                raise ValueError(
                    f"参数 {name}={value!r} 不在允许列表 {allowed!r} 中"
                )

        def _replace(match: re.Match[str]) -> str:
            return str(arguments[match.group(1)])

        rendered: list[str] = []
        for token in spec.command_template:
            rendered.append(re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", _replace, token))
        return rendered

    @staticmethod
    def _error_code_for(exc: ValueError) -> str:
        msg = str(exc)
        if "缺少参数" in msg or "不在允许列表" in msg:
            return "shell_arg_not_allowed"
        if "禁止的 shell 元字符" in msg:
            return "shell_injection_blocked"
        if "缺少 allowed_args" in msg:
            return "shell_arg_not_allowed"
        return "shell_runtime_error"

    def _build_env(self, spec: ShellToolSpec) -> dict[str, str]:
        """构造子进程环境变量：系统必要变量 + 白名单变量 + 额外变量。"""
        filtered_env: dict[str, str] = {}
        for key in self._ESSENTIAL_ENV_VARS:
            if key in os.environ:
                filtered_env[key] = os.environ[key]
        for key in spec.sandbox.env_whitelist:
            if key in os.environ and key not in filtered_env:
                filtered_env[key] = os.environ[key]
        filtered_env.update(self._env_extra)
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
        max_output: int,
    ) -> tuple[bytes, bytes, bool]:
        """流式读取 stdout/stderr，超过限制时截断。"""
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
        """返回 Shell 工具元数据列表，按 Profile 过滤。"""
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
