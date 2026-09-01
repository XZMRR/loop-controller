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
import uuid
from collections import OrderedDict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol

import httpx

from loop_controller.executors.base import ExecutionContext, ToolExecutor
from loop_controller.executors.harness_models import (
    DockerBackendConfig,
    HarnessBackendConfig,
    HarnessExecutionPolicy,
    HarnessSandboxConfig,
    HarnessToolSpec,
    HTTPBackendConfig,
    IsolatedSubprocessBackendConfig,
    SubprocessBackendConfig,
)
from loop_controller.executors.harness_protocol import (
    HARNESS_CANCEL_PATH,
    HARNESS_EXECUTE_PATH,
    HARNESS_PROTOCOL_VERSION,
    HarnessBackendStatus,
    HarnessCancelRequest,
    HarnessContext,
    HarnessExecuteRequest,
    HarnessExecuteResponse,
    HarnessSandbox,
)
from loop_controller.models import AuditAlert, CapabilityProfile, Tool, ToolResult

if TYPE_CHECKING:
    from loop_controller.infra.alert_store import AlertStore

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
        self._execute_url = f"{self._base_url}{HARNESS_EXECUTE_PATH}"
        self._execute_path = httpx.URL(self._execute_url).path
        self._cancel_url = f"{self._base_url}{HARNESS_CANCEL_PATH}"
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
                f"POST\n{self._execute_path}\n{HARNESS_PROTOCOL_VERSION}\n"
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

        if result.status == "success" and result.effective_sandbox is None:
            return self._error_result(
                context,
                tool_name,
                "Harness 响应缺少 effective_sandbox 回执",
                "harness_sandbox_attestation_missing",
            )

        if result.status == "success" and not self._sandbox_matches(
            request.sandbox, result.effective_sandbox
        ):
            return self._error_result(
                context,
                tool_name,
                "Harness 实际生效沙箱与请求不一致",
                "harness_sandbox_violation",
                {
                    "requested_sandbox": request.sandbox.model_dump(mode="json"),
                    "effective_sandbox": result.effective_sandbox.model_dump(mode="json")
                    if result.effective_sandbox
                    else None,
                },
            )

        metadata = dict(result.metadata)
        if result.evidence is not None:
            metadata["harness_evidence"] = result.evidence.model_dump(mode="json")

        return ToolResult(
            call_id=context.call_id,
            task_id=context.task_id,
            tool_name=tool_name,
            status=result.status,
            content=result.content,
            error_code=result.error_code,
            metadata=metadata,
        )

    @staticmethod
    def _sandbox_matches(requested: HarnessSandbox, effective: HarnessSandbox | None) -> bool:
        """v0.31.0：严格比较请求沙箱与实际生效沙箱是否一致。"""
        if effective is None:
            return False
        return requested.model_dump() == effective.model_dump()

    async def cancel_call(self, call_id: str) -> bool:
        """向远端 Harness 发送取消请求；仅 HTTP backend 支持。"""
        if self._client is None:
            return False
        body = HarnessCancelRequest(call_id=call_id).model_dump_json().encode("utf-8")
        try:
            response = await self._client.post(
                self._cancel_url,
                headers=self._headers(body),
                content=body,
                timeout=5.0,
            )
            return response.status_code in (200, 202, 204)
        except httpx.RequestError:
            return False

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

    def snapshot(self, *, draining: bool = False) -> HarnessBackendStatus:
        return HarnessBackendStatus(
            name=self.name,
            type=self.type,
            status=self.status,
            max_concurrent_calls=self.max_concurrent_calls,
            checked_at=self.checked_at,
            consecutive_failures=self.consecutive_failures,
            last_error_code=self.last_error_code,
            in_flight=self.in_flight,
            draining=draining,
        )


class HarnessExecutor(ToolExecutor):
    """Loop Controller 到 Harness 的桥接执行器。"""

    def __init__(
        self,
        tool_specs: dict[str, HarnessToolSpec],
        backends: dict[str, HarnessBackendConfig],
        execution_policy: HarnessExecutionPolicy | None = None,
        alert_store: AlertStore | None = None,
    ) -> None:
        self._tool_specs = tool_specs
        self._backend_configs = dict(backends)
        self._backends = {name: self._build_backend(config) for name, config in backends.items()}
        self._states = {name: _BackendState(name, config) for name, config in backends.items()}
        self._health_tasks: dict[str, asyncio.Task[None]] = {}
        self._started = False
        self._execution_policy = execution_policy or HarnessExecutionPolicy()
        self._draining: set[str] = set()
        self._alert_store = alert_store
        # v0.34.0：按 call_id 缓存已完成结果，提供跨重试幂等基础。
        self._idempotency_cache: OrderedDict[str, ToolResult] = OrderedDict()
        # call_id -> backend_name，用于远程取消。
        self._in_flight_calls: dict[str, str] = {}

    def _cache_result(self, call_id: str, result: ToolResult) -> None:
        """保留最近 1000 条结果，超出时按 LRU 淘汰。"""
        if call_id in self._idempotency_cache:
            self._idempotency_cache.move_to_end(call_id)
            return
        self._idempotency_cache[call_id] = result
        while len(self._idempotency_cache) > 1000:
            self._idempotency_cache.popitem(last=False)

    def _build_backend(self, config: HarnessBackendConfig) -> HarnessBackend:
        if isinstance(config, SubprocessBackendConfig):
            return _SubprocessHarnessBackend(config)
        if isinstance(config, HTTPBackendConfig):
            return _HTTPHarnessClient(config)
        if isinstance(config, DockerBackendConfig):
            from loop_controller.executors.docker_harness_backend import DockerHarnessBackend
            return DockerHarnessBackend(config)
        if isinstance(config, IsolatedSubprocessBackendConfig):
            from loop_controller.executors.isolated_subprocess_harness import (
                IsolatedSubprocessHarnessBackend,
            )
            return IsolatedSubprocessHarnessBackend(config)
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
                    self._health_tasks[name] = asyncio.create_task(self._health_loop(name, config))
        except BaseException:
            await asyncio.gather(*(backend.stop() for backend in reversed(started)))
            raise

    async def stop(self) -> None:
        tasks = list(self._health_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
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
        return [
            self._states[name].snapshot(draining=name in self._draining)
            for name in sorted(self._states)
        ]

    def is_tool_available(self, tool_name: str) -> bool:
        """判断工具是否已注册 Harness spec 且其后端存在。"""
        spec = self._tool_specs.get(tool_name)
        if spec is None:
            return False
        return spec.harness in self._backends

    def has_healthy_backend(self, tool_name: str) -> bool:
        """v0.31.0：工具对应 backend 是否可用于执行。"""
        spec = self._tool_specs.get(tool_name)
        if spec is None:
            return False
        state = self._states.get(spec.harness)
        if state is None:
            return False
        return state.status in ("healthy", "degraded") and state.name not in self._draining

    async def drain_backend(self, name: str, timeout_seconds: float = 30.0) -> bool:
        """v0.31.0：停止接收新请求并等待在途调用完成。"""
        if name not in self._states:
            raise KeyError(f"Harness 后端 {name!r} 不存在")
        self._draining.add(name)
        state = self._states[name]
        deadline = time.perf_counter() + timeout_seconds
        while state.in_flight > 0 and time.perf_counter() < deadline:
            await asyncio.sleep(0.1)
        return state.in_flight == 0

    def reset_backend(self, name: str) -> None:
        """v0.31.0：清空失败计数并触发一次健康检查。"""
        if name not in self._states:
            raise KeyError(f"Harness 后端 {name!r} 不存在")
        self._draining.discard(name)
        state = self._states[name]
        state.consecutive_failures = 0
        state.last_error_code = None
        if name in self._backends:
            asyncio.create_task(self._check_and_record_health(name))

    async def _check_and_record_health(self, name: str) -> None:
        try:
            healthy = await self._backends[name].check_health()
        except Exception:
            healthy = False
        self._record_health(name, healthy)

    async def update_specs(
        self,
        tool_specs: dict[str, HarnessToolSpec],
        backend_configs: dict[str, HarnessBackendConfig],
        execution_policy: HarnessExecutionPolicy | None = None,
    ) -> None:
        """热更新 Harness 工具规格、后端配置与执行策略。

        平滑替换：新后端先启动 health check，旧后端 drain 后移除。
        更新失败时保留旧配置并抛异常。
        """
        self._tool_specs = dict(tool_specs)
        if execution_policy is not None:
            self._execution_policy = execution_policy

        old_configs = self._backend_configs
        old_backends = self._backends
        old_states = self._states

        old_names = set(old_configs)
        new_names = set(backend_configs)
        changed_names = {
            name
            for name in old_names & new_names
            if old_configs[name].model_dump() != backend_configs[name].model_dump()
        }
        kept_names = (old_names & new_names) - changed_names
        added_names = new_names - old_names
        removed_names = old_names - new_names

        new_backends: dict[str, HarnessBackend] = {}
        new_states: dict[str, _BackendState] = {}
        for name in kept_names:
            new_backends[name] = old_backends[name]
            new_states[name] = old_states[name]
        for name in added_names | changed_names:
            config = backend_configs[name]
            new_backends[name] = self._build_backend(config)
            new_states[name] = _BackendState(name, config)

        self._backend_configs = dict(backend_configs)
        self._backends = new_backends
        self._states = new_states

        started_names: list[str] = []
        try:
            for name in sorted(added_names | changed_names):
                backend = self._backends[name]
                if self._started:
                    await backend.start()
                config = self._backend_configs[name]
                if self._started:
                    healthy = await backend.check_health()
                    self._record_health(name, healthy)
                    if (
                        isinstance(config, HTTPBackendConfig)
                        and config.health.enabled
                        and config.health.startup_required
                        and not healthy
                    ):
                        raise RuntimeError(f"Harness 后端 {name!r} 热更新健康检查失败")
                started_names.append(name)
        except BaseException:
            logger.warning("Harness 热更新失败，回滚到旧配置")
            self._backend_configs = old_configs
            self._backends = old_backends
            self._states = old_states
            for name in started_names:
                await self._backends[name].stop()
            raise

        await self._restart_health_loops(removed_names | changed_names)

        drain_names = removed_names | changed_names
        for name in drain_names:
            old_state = old_states.get(name)
            if old_state is not None and self._started:
                deadline = time.perf_counter() + old_state.acquire_timeout
                while old_state.in_flight > 0 and time.perf_counter() < deadline:
                    await asyncio.sleep(0.1)
            old_backend = old_backends.get(name)
            if old_backend is not None:
                await old_backend.stop()
            self._draining.discard(name)

    async def _restart_health_loops(self, names_to_cancel: set[str]) -> None:
        """取消指定后端的健康轮询，并按当前配置重新创建需要的任务。"""
        for name in names_to_cancel:
            task = self._health_tasks.pop(name, None)
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        if not self._started:
            return
        for name, config in self._backend_configs.items():
            if isinstance(config, HTTPBackendConfig) and config.health.enabled and name not in self._health_tasks:
                self._health_tasks[name] = asyncio.create_task(self._health_loop(name, config))

    def _effective_sandbox(self, tool_name: str) -> HarnessSandboxConfig:
        """优先使用执行策略中工具级沙箱覆盖，否则回退到 HarnessToolSpec 沙箱。"""
        spec = self._get_spec(tool_name)
        tool_policy = self._execution_policy.tools.get(tool_name)
        if tool_policy is not None:
            return tool_policy.sandbox
        return spec.sandbox

    def _save_sandbox_alert(self, context: ExecutionContext, tool_name: str, result: ToolResult) -> None:
        """Harness 沙箱回执缺失或不一致时写入安全告警。"""
        if self._alert_store is None:
            return
        error_code = result.error_code or "harness_sandbox_unknown"
        title = (
            "Harness 沙箱回执缺失"
            if error_code == "harness_sandbox_attestation_missing"
            else "Harness 沙箱回执与请求不一致"
        )
        alert = AuditAlert(
            alert_id=uuid.uuid4().hex,
            session_id=context.session_id or context.task_id,
            task_id=context.task_id,
            rule_id=error_code,
            severity="critical",
            title=title,
            description=result.content or title,
            evidence=[context.call_id],
        )
        try:
            self._alert_store.save_alert(alert)
        except Exception:
            logger.exception("写入 Harness 沙箱告警失败")

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: ExecutionContext,
    ) -> ToolResult:
        # v0.34.0：按 call_id 返回缓存结果，提供幂等基础。
        cached = self._idempotency_cache.get(context.call_id)
        if cached is not None:
            return cached.model_copy(update={"metadata": {**cached.metadata, "idempotent": True}})

        spec = self._get_spec(tool_name)
        sandbox = self._effective_sandbox(tool_name)
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
        if backend_name in self._draining:
            return record_call(_HTTPHarnessClient._error_result(
                context, tool_name, "Harness 后端正在排空", "harness_backend_unavailable"
            ))
        if state.status in ("unknown", "unhealthy"):
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
        self._in_flight_calls[context.call_id] = backend_name
        try:
            result = await backend.execute(tool_name, arguments, context, sandbox)
            result = record_call(result)
            self._cache_result(context.call_id, result)
            if result.error_code in (
                "harness_sandbox_attestation_missing",
                "harness_sandbox_violation",
            ):
                self._save_sandbox_alert(context, tool_name, result)
            return result
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
            self._in_flight_calls.pop(context.call_id, None)
            state.semaphore.release()

    async def list_tools(self, profile: CapabilityProfile) -> list[Tool]:
        allowed = set(profile.tools.keys()) if profile.tools else None
        return [
            spec.to_tool()
            for name, spec in self._tool_specs.items()
            if allowed is None or name in allowed
        ]

    async def cancel_call(self, call_id: str) -> bool:
        """取消指定 call_id 的在途 Harness 调用；当前仅 HTTP backend 支持远程取消。"""
        backend_name = self._in_flight_calls.get(call_id)
        if backend_name is None:
            return False
        backend = self._backends.get(backend_name)
        if backend is None or not isinstance(backend, _HTTPHarnessClient):
            return False
        return await backend.cancel_call(call_id)
