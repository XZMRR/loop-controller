"""HarnessExecutor：把 Loop Controller 治理后的调用转发给外部 Harness（v0.25.0）。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from typing import Any, Protocol

import httpx

from loop_controller.executors.base import ExecutionContext, ToolExecutor
from loop_controller.executors.harness_models import (
    HarnessBackendConfig,
    HarnessSandboxConfig,
    HarnessToolSpec,
    HTTPBackendConfig,
    SubprocessBackendConfig,
)
from loop_controller.executors.harness_protocol import (
    HarnessContext,
    HarnessExecuteRequest,
    HarnessExecuteResponse,
    HarnessSandbox,
)
from loop_controller.models import CapabilityProfile, Tool, ToolResult

logger = logging.getLogger(__name__)


class HarnessBackend(Protocol):
    """Harness 后端抽象。"""

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: ExecutionContext,
        sandbox: HarnessSandboxConfig,
    ) -> ToolResult: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


class _HTTPHarnessClient:
    """通过 HTTP 调用本地或远程 Harness。"""

    def __init__(self, config: HTTPBackendConfig) -> None:
        self._base_url = config.base_url.rstrip("/")
        self._timeout = config.timeout_seconds
        self._api_key = None
        if config.api_key_env:
            self._api_key = os.environ.get(config.api_key_env)
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        self._client = httpx.AsyncClient(timeout=self._timeout)

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: ExecutionContext,
        sandbox: HarnessSandboxConfig,
    ) -> ToolResult:
        if self._client is None:
            await self.start()
            assert self._client is not None

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
            sandbox=HarnessSandbox(
                timeout_seconds=sandbox.timeout_seconds,
                max_output_bytes=sandbox.max_output_bytes,
                allowed_hosts=sandbox.allowed_hosts,
                allowed_paths=sandbox.allowed_paths,
                env_whitelist=sandbox.env_whitelist,
            ),
        )
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            headers["x-harness-api-key"] = self._api_key

        try:
            resp = await self._client.post(
                f"{self._base_url}/harness/v1/execute",
                headers=headers,
                content=request.model_dump_json(),
            )
            resp.raise_for_status()
            payload = resp.json()
        except httpx.RequestError as exc:
            return self._error_result(
                context,
                tool_name,
                f"Harness 请求失败: {exc}",
                "harness_request_error",
            )
        except httpx.HTTPStatusError as exc:
            return self._error_result(
                context,
                tool_name,
                f"Harness 返回错误: {exc.response.status_code}",
                "harness_http_error",
            )
        except json.JSONDecodeError as exc:
            return self._error_result(
                context,
                tool_name,
                f"Harness 响应 JSON 非法: {exc}",
                "harness_invalid_response",
            )

        try:
            result = HarnessExecuteResponse.model_validate(payload)
        except Exception as exc:  # noqa: BLE001
            return self._error_result(
                context,
                tool_name,
                f"Harness 响应格式非法: {exc}",
                "harness_invalid_response",
            )

        return ToolResult(
            call_id=context.call_id,
            task_id=context.task_id,
            tool_name=tool_name,
            status=result.status,
            content=result.content,
            error_code=result.error_code,
            metadata=result.metadata,
        )

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


class _SubprocessHarnessBackend:
    """启动本地子进程 Harness 并通过 HTTP 调用。"""

    def __init__(self, config: SubprocessBackendConfig) -> None:
        self._command = config.command
        self._env = config.env
        self._port: int | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._client: _HTTPHarnessClient | None = None

    async def start(self) -> None:
        # 选一个可用端口
        import socket

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        self._port = sock.getsockname()[1]
        sock.close()

        env = dict(os.environ)
        env.update(self._env)
        env["HARNESS_PORT"] = str(self._port)

        self._proc = await asyncio.create_subprocess_exec(
            *self._command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        self._client = _HTTPHarnessClient(
            HTTPBackendConfig(
                name=self._command[0],
                type="http",
                base_url=f"http://127.0.0.1:{self._port}",
            )
        )
        # 等待 Harness 健康检查就绪
        assert self._client is not None
        for _ in range(50):
            await asyncio.sleep(0.1)
            try:
                await self._client.start()
                http_client = self._client._client
                assert http_client is not None
                # 做一个简单的健康检查
                resp = await http_client.get(
                    f"http://127.0.0.1:{self._port}/health"
                )
                if resp.status_code == 200:
                    return
            except Exception:  # noqa: BLE001
                await self._client.stop()
        await self._client.stop()
        raise RuntimeError("子进程 Harness 启动超时")

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.stop()
        if self._proc is not None and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except TimeoutError:
                self._proc.kill()
                await self._proc.wait()

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: ExecutionContext,
        sandbox: HarnessSandboxConfig,
    ) -> ToolResult:
        if self._client is None:
            await self.start()
        assert self._client is not None
        return await self._client.execute(tool_name, arguments, context, sandbox)


class HarnessExecutor(ToolExecutor):
    """Loop Controller 到 Harness 的桥接执行器。"""

    def __init__(
        self,
        tool_specs: dict[str, HarnessToolSpec],
        backends: dict[str, HarnessBackendConfig],
    ) -> None:
        self._tool_specs = tool_specs
        self._backends: dict[str, HarnessBackend] = {}
        for name, config in backends.items():
            self._backends[name] = self._build_backend(config)

    def _build_backend(self, config: HarnessBackendConfig) -> HarnessBackend:
        if isinstance(config, SubprocessBackendConfig):
            return _SubprocessHarnessBackend(config)
        if isinstance(config, HTTPBackendConfig):
            return _HTTPHarnessClient(config)
        raise NotImplementedError(f"Harness 后端类型尚未实现: {type(config).__name__}")

    def _get_spec(self, tool_name: str) -> HarnessToolSpec:
        spec = self._tool_specs.get(tool_name)
        if spec is None:
            raise KeyError(f"Harness 工具 {tool_name!r} 未注册")
        return spec

    def secret_refs_for(self, tool_name: str) -> list[str]:
        return []

    async def start(self) -> None:
        await asyncio.gather(
            *[backend.start() for backend in self._backends.values()]
        )

    async def stop(self) -> None:
        await asyncio.gather(
            *[backend.stop() for backend in self._backends.values()]
        )

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: ExecutionContext,
    ) -> ToolResult:
        spec = self._get_spec(tool_name)
        backend = self._backends.get(spec.harness)
        if backend is None:
            return ToolResult(
                call_id=context.call_id,
                task_id=context.task_id,
                tool_name=tool_name,
                status="error",
                content=f"Harness 后端 {spec.harness!r} 未配置",
                error_code="harness_backend_not_found",
            )
        return await backend.execute(
            tool_name, arguments, context, spec.sandbox
        )

    async def list_tools(self, profile: CapabilityProfile) -> list[Tool]:
        allowed = set(profile.tools.keys()) if profile.tools else None
        tools: list[Tool] = []
        for name, spec in self._tool_specs.items():
            if allowed is not None and name not in allowed:
                continue
            tools.append(spec.to_tool())
        return tools
