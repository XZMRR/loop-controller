"""HarnessExecutor：把治理后的调用安全转发给外部 Harness。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import subprocess
import time
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

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
    HARNESS_EXECUTE_PATH,
    HARNESS_PROTOCOL_VERSION,
    HarnessBackendStatus,
    HarnessContext,
    HarnessExecuteRequest,
    HarnessExecuteResponse,
    HarnessSandbox,
)
from loop_controller.models import CapabilityProfile, Tool, ToolResult

logger = logging.getLogger(__name__)


def _metrics() -> Any | None:
    try:
        from loop_controller import metrics
    except ImportError:
        return None
    return metrics


class HarnessBackend(Protocol):
    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: ExecutionContext,
        sandbox: HarnessSandboxConfig,
    ) -> ToolResult: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def check_health(self) -> bool: ...


class _HTTPHarnessClient:
    """通过 HTTP 调用本地或远程 Harness。"""

    def __init__(self, config: HTTPBackendConfig) -> None:
        self.config = config
        self._base_url = config.base_url.rstrip("/")
        self._client: httpx.AsyncClient | None = None

    def _resolve_key(self) -> str | None:
        env_name = self.config.auth.key_env
        if self.config.auth.type == "none":
            return None
        key = os.environ.get(env_name or "")
        if not key:
            raise RuntimeError("Harness 认证密钥未配置")
        return key

    async def start(self) -> None:
        if self._client is not None and not self._client.is_closed:
            return
        self._resolve_key()
        tls = self.config.tls
        verify: bool | str = tls.ca_file or tls.verify
        cert = (
            (tls.client_cert_file, tls.client_key_file)
            if tls.client_cert_file and tls.client_key_file
            else None
        )
        self._client = httpx.AsyncClient(
            timeout=self.config.timeout_seconds,
            verify=verify,
            cert=cert,
        )

    async def stop(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def check_health(self) -> bool:
        if not self.config.health.enabled:
            return True
        if self._client is None:
            return False
        try:
            response = await self._client.get(
                f"{self._base_url}{self.config.health.path}",
                timeout=self.config.health.timeout_seconds,
            )
            return response.status_code == 200
        except httpx.RequestError:
            return False

    def _headers(self, body: bytes) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "X-Harness-Protocol-Version": HARNESS_PROTOCOL_VERSION,
        }
        key = self._resolve_key()
        if self.config.auth.type == "api_key" and key is not None:
            headers["X-Harness-API-Key"] = key
        elif self.config.auth.type == "hmac_sha256" and key is not None:
            key_id = self.config.auth.key_id or ""
            timestamp = str(int(time.time()))
            nonce = secrets.token_urlsafe(24)
            body_hash = hashlib.sha256(body).hexdigest()
            canonical = (
                f"POST\n{HARNESS_EXECUTE_PATH}\n{HARNESS_PROTOCOL_VERSION}\n"
                f"{key_id}\n{timestamp}\n{nonce}\n{body_hash}"
            )
            signature = base64.b64encode(
                hmac.new(key.encode(), canonical.encode(), hashlib.sha256).digest()
            ).decode("ascii")
            headers.update(
                {
                    "X-Harness-Key-Id": key_id,
                    "X-Harness-Timestamp": timestamp,
                    "X-Harness-Nonce": nonce,
                    "X-Harness-Signature": signature,
                }
            )
        return headers

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: ExecutionContext,
        sandbox: HarnessSandboxConfig,
    ) -> ToolResult:
        if self._client is None:
            return self._error_result(
                context, tool_name, "Harness 后端尚未启动", "harness_backend_unavailable"
            )
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
            sandbox=HarnessSandbox(**sandbox.model_dump()),
        )
        body = request.model_dump_json().encode("utf-8")
        try:
            response = await self._client.post(
                f"{self._base_url}{HARNESS_EXECUTE_PATH}",
                headers=self._headers(body),
                content=body,
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.TimeoutException:
            return self._error_result(
                context,
                tool_name,
                "Harness 请求超时，远端执行结果未知",
                "harness_request_timeout",
                {"execution_uncertain": True},
            )
        except httpx.RequestError:
            return self._error_result(
                context, tool_name, "Harness 后端不可达", "harness_backend_unavailable"
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403):
                return self._error_result(
                    context, tool_name, "Harness 认证失败", "harness_auth_failed"
                )
            return self._error_result(
                context,
                tool_name,
                f"Harness 返回 HTTP {exc.response.status_code}",
                "harness_http_error",
            )
        except json.JSONDecodeError:
            return self._error_result(
                context, tool_name, "Harness 响应格式非法", "harness_invalid_response"
            )

        try:
            result = HarnessExecuteResponse.model_validate(payload)
        except Exception:
            return self._error_result(
                context, tool_name, "Harness 响应格式非法", "harness_invalid_response"
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
        metadata: dict[str, Any] | None = None,
    ) -> ToolResult:
        return ToolResult(
            call_id=context.call_id,
            task_id=context.task_id,
            tool_name=tool_name,
            status="error",
            content=message,
            error_code=error_code,
            metadata=metadata or {},
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
        import socket

        if self._proc is not None and self._proc.returncode is None:
            return
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            self._port = sock.getsockname()[1]
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
            HTTPBackendConfig(name=self._command[0], base_url=f"http://127.0.0.1:{self._port}")
        )
        await self._client.start()
        for _ in range(50):
            await asyncio.sleep(0.1)
            try:
                assert self._client._client is not None
                response = await self._client._client.get(
                    f"http://127.0.0.1:{self._port}/health"
                )
                if response.status_code == 200:
                    return
            except httpx.RequestError:
                pass
        await self.stop()
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
        self._proc = None

    async def check_health(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: ExecutionContext,
        sandbox: HarnessSandboxConfig,
    ) -> ToolResult:
        if self._client is None:
            return _HTTPHarnessClient._error_result(
                context, tool_name, "Harness 后端尚未启动", "harness_backend_unavailable"
            )
        return await self._client.execute(tool_name, arguments, context, sandbox)


class _BackendState:
    def __init__(self, name: str, config: HarnessBackendConfig) -> None:
        self.name = name
        self.type = config.type
        self.max_concurrent_calls = config.max_concurrent_calls
        self.semaphore = asyncio.Semaphore(config.max_concurrent_calls)
        self.acquire_timeout = config.acquire_timeout_seconds
        self.in_flight = 0
        self.status: Literal["unknown", "healthy", "degraded", "unhealthy"] = "unknown"
        self.checked_at: datetime | None = None
        self.consecutive_failures = 0
        self.last_error_code: str | None = None

    def snapshot(self) -> HarnessBackendStatus:
        return HarnessBackendStatus(
            name=self.name,
            type=self.type,
            status=self.status,
            max_concurrent_calls=self.max_concurrent_calls,
            checked_at=self.checked_at,
            consecutive_failures=self.consecutive_failures,
            last_error_code=self.last_error_code,
            in_flight=self.in_flight,
        )


class HarnessExecutor(ToolExecutor):
    """Loop Controller 到 Harness 的桥接执行器。"""

    def __init__(
        self,
        tool_specs: dict[str, HarnessToolSpec],
        backends: dict[str, HarnessBackendConfig],
    ) -> None:
        self._tool_specs = tool_specs
        self._backend_configs = dict(backends)
        self._backends = {name: self._build_backend(config) for name, config in backends.items()}
        self._states = {name: _BackendState(name, config) for name, config in backends.items()}
        self._health_tasks: list[asyncio.Task[None]] = []
        self._started = False

    def _build_backend(self, config: HarnessBackendConfig) -> HarnessBackend:
        if isinstance(config, SubprocessBackendConfig):
            return _SubprocessHarnessBackend(config)
        if isinstance(config, HTTPBackendConfig):
            return _HTTPHarnessClient(config)
        raise ValueError(f"不支持的 Harness 后端类型: {type(config).__name__}")

    def _get_spec(self, tool_name: str) -> HarnessToolSpec:
        spec = self._tool_specs.get(tool_name)
        if spec is None:
            raise KeyError(f"Harness 工具 {tool_name!r} 未注册")
        return spec

    def secret_refs_for(self, tool_name: str) -> list[str]:
        spec = self._tool_specs.get(tool_name)
        if spec is None:
            return []
        config = self._backend_configs.get(spec.harness)
        refs = set(spec.secret_refs)
        if isinstance(config, HTTPBackendConfig) and config.auth.key_env:
            refs.add(config.auth.key_env)
        return sorted(refs)

    async def start(self) -> None:
        if self._started:
            return
        started: list[HarnessBackend] = []
        try:
            for name, backend in self._backends.items():
                await backend.start()
                started.append(backend)
                config = self._backend_configs[name]
                healthy = await backend.check_health()
                self._record_health(name, healthy)
                if (
                    isinstance(config, HTTPBackendConfig)
                    and config.health.enabled
                    and config.health.startup_required
                    and not healthy
                ):
                    raise RuntimeError(f"Harness 后端 {name!r} 启动健康检查失败")
            self._started = True
            for name, config in self._backend_configs.items():
                if isinstance(config, HTTPBackendConfig) and config.health.enabled:
                    self._health_tasks.append(asyncio.create_task(self._health_loop(name, config)))
        except BaseException:
            await asyncio.gather(*(backend.stop() for backend in reversed(started)))
            raise

    async def stop(self) -> None:
        for task in self._health_tasks:
            task.cancel()
        if self._health_tasks:
            await asyncio.gather(*self._health_tasks, return_exceptions=True)
        self._health_tasks.clear()
        await asyncio.gather(*(backend.stop() for backend in self._backends.values()))
        self._started = False

    async def _health_loop(self, name: str, config: HTTPBackendConfig) -> None:
        while True:
            await asyncio.sleep(config.health.interval_seconds)
            healthy = await self._backends[name].check_health()
            self._record_health(name, healthy)

    def _record_health(self, name: str, healthy: bool) -> None:
        state = self._states[name]
        state.checked_at = datetime.now(UTC)
        metrics = _metrics()
        if metrics is not None:
            metrics.set_harness_health(name, healthy)
        if healthy:
            state.status = "healthy"
            state.consecutive_failures = 0
            state.last_error_code = None
            return
        state.consecutive_failures += 1
        config = self._backend_configs[name]
        threshold = config.health.unhealthy_threshold if isinstance(config, HTTPBackendConfig) else 1
        state.status = "unhealthy" if state.consecutive_failures >= threshold else "degraded"
        state.last_error_code = "harness_backend_unavailable"

    def backend_statuses(self) -> list[HarnessBackendStatus]:
        return [self._states[name].snapshot() for name in sorted(self._states)]

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: ExecutionContext,
    ) -> ToolResult:
        spec = self._get_spec(tool_name)
        backend_name = spec.harness
        backend = self._backends.get(backend_name)
        state = self._states.get(backend_name)
        metrics = _metrics()
        call_started = time.perf_counter()

        def record_call(result: ToolResult) -> ToolResult:
            if metrics is not None:
                metrics.observe_harness_call(
                    backend_name,
                    tool_name,
                    result.status,
                    result.error_code,
                    time.perf_counter() - call_started,
                )
            return result

        if backend is None or state is None:
            return record_call(_HTTPHarnessClient._error_result(
                context, tool_name, "Harness 后端未配置", "harness_backend_not_found"
            ))
        config = self._backend_configs[backend_name]
        if isinstance(config, HTTPBackendConfig) and config.health.enabled and state.status != "healthy":
            return record_call(_HTTPHarnessClient._error_result(
                context, tool_name, "Harness 后端当前不可用", "harness_backend_unavailable"
            ))
        queue_started = time.perf_counter()
        try:
            await asyncio.wait_for(state.semaphore.acquire(), timeout=state.acquire_timeout)
        except TimeoutError:
            if metrics is not None:
                metrics.observe_harness_queue_wait(
                    backend_name, time.perf_counter() - queue_started
                )
                metrics.observe_harness_overloaded(backend_name)
            return record_call(_HTTPHarnessClient._error_result(
                context, tool_name, "Harness 后端繁忙", "harness_overloaded"
            ))
        if metrics is not None:
            metrics.observe_harness_queue_wait(backend_name, time.perf_counter() - queue_started)
        state.in_flight += 1
        if metrics is not None:
            metrics.set_harness_in_flight(backend_name, state.in_flight)
        try:
            result = await backend.execute(tool_name, arguments, context, spec.sandbox)
            return record_call(result)
        except asyncio.CancelledError:
            if metrics is not None:
                metrics.observe_harness_call(
                    backend_name,
                    tool_name,
                    "cancelled",
                    "cancelled",
                    time.perf_counter() - call_started,
                )
            raise
        except Exception:
            if metrics is not None:
                metrics.observe_harness_call(
                    backend_name,
                    tool_name,
                    "error",
                    "internal_error",
                    time.perf_counter() - call_started,
                )
            raise
        finally:
            state.in_flight -= 1
            if metrics is not None:
                metrics.set_harness_in_flight(backend_name, state.in_flight)
            state.semaphore.release()

    async def list_tools(self, profile: CapabilityProfile) -> list[Tool]:
        allowed = set(profile.tools.keys()) if profile.tools else None
        return [
            spec.to_tool()
            for name, spec in self._tool_specs.items()
            if allowed is None or name in allowed
        ]
