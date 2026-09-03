"""Loop Controller HTTP 服务（v0.17.0 / v0.18.0）。

本模块属于可选扩展，需要额外安装 server 依赖：

    uv pip install "loop-controller[server]"

使用方式：

    from loop_controller.controller import build_controller
    from loop_controller.infra.config_loader import ConfigLoader
    from loop_controller.server import build_app

    config = ConfigLoader().load("config")
    controller = await build_controller(config)
    app = build_app(controller, api_key="optional-key")
    # uvicorn app:app

CLI：

    lc server --config ./config --port 8080 --opa-url http://127.0.0.1:8181
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

import httpx

from loop_controller.approval_service import ApprovalServiceError, build_approval_record
from loop_controller.approval_watcher import ApprovalWatcher
from loop_controller.controller import LoopController
from loop_controller.identity import (
    AgentIdentity,
    IdentityCredential,
    IdentityProvider,
    KillSwitchConfig,
    RevocationType,
)
from loop_controller.infra.approval_store import ApprovalStoreError
from loop_controller.interaction.engine import (
    InteractionAuthorizeEndpoint,
    InteractionGovernanceEngine,
)
from loop_controller.logging_config import configure_logging, set_trace_id
from loop_controller.metrics import (
    observe_request,
    observe_tool_call,
    render_metrics,
    set_pending_approvals,
    set_persistence_durability,
)
from loop_controller.metrics import (
    set_trace_id as metrics_set_trace_id,
)
from loop_controller.models import AuditEvent
from loop_controller.server_models import (
    AuditQueryResponse,
    GovernResponse,
    GovernToolRequest,
    HealthResponse,
    PendingApprovalItem,
    PendingApprovalsResponse,
    ResumeApprovalRequest,
    RevocationListResponse,
    RevokeRequest,
    WaitApprovalResponse,
)

try:
    from starlette.applications import Starlette
    from starlette.exceptions import HTTPException
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
    from starlette.middleware.cors import CORSMiddleware
    from starlette.requests import Request
    from starlette.responses import (
        JSONResponse,
        PlainTextResponse,
        Response,
        StreamingResponse,
    )
    from starlette.routing import Route
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "使用 loop_controller.server 需要先安装 server 依赖: "
        "uv pip install 'loop-controller[server]'"
    ) from exc

logger = logging.getLogger("loop_controller.server")


def _extract_identity_provider(controller: LoopController) -> IdentityProvider | None:
    """从 LoopController 的 Runtime 中提取 IdentityProvider。"""
    runtime = getattr(controller, "_runtime", None)
    if runtime is None:
        return None
    checkpoint = getattr(runtime, "checkpoint", None)
    if checkpoint is None:
        return None
    return getattr(checkpoint, "_identity", None)


class MetricsMiddleware(BaseHTTPMiddleware):
    """记录请求耗时与 trace_id。"""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        trace_id = request.headers.get("x-trace-id") or uuid.uuid4().hex[:16]
        set_trace_id(trace_id)
        metrics_set_trace_id(trace_id)
        request.state.trace_id = trace_id

        start = time.perf_counter()
        response = await call_next(request)
        duration = time.perf_counter() - start

        observe_request(
            endpoint=request.url.path,
            status_code=response.status_code,
            duration=duration,
        )
        response.headers["X-Trace-ID"] = trace_id
        return response


DEFAULT_MAX_HTTP_BODY_SIZE = 1 * 1024 * 1024  # 1 MB


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """限制单个 HTTP 请求体大小，超过时直接返回 413。"""

    def __init__(self, app: Any, max_size: int = DEFAULT_MAX_HTTP_BODY_SIZE) -> None:
        super().__init__(app)
        self._max_size = max_size

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                size = int(content_length)
            except ValueError:
                return JSONResponse(
                    {"error": "invalid_parameter", "message": "invalid Content-Length"},
                    status_code=400,
                )
            if size > self._max_size:
                return JSONResponse(
                    {"error": "payload_too_large", "message": "request body too large"},
                    status_code=413,
                )
        return await call_next(request)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """基于内存的滑动窗口限流（按 client IP + 路径）。

    未配置时默认放行；适用于单进程部署。
    """

    def __init__(
        self,
        app: Any,
        requests_per_minute: int = 120,
        burst: int = 20,
        window_seconds: int = 60,
    ) -> None:
        super().__init__(app)
        self._limit = max(1, requests_per_minute)
        self._burst = max(1, burst)
        self._window = window_seconds
        self._requests: dict[str, list[float]] = {}

    def _key(self, request: Request) -> str:
        client = request.client.host if request.client else "unknown"
        return f"{client}:{request.url.path}"

    def _is_allowed(self, key: str, now: float) -> bool:
        window = self._window
        cutoff = now - window
        timestamps = self._requests.setdefault(key, [])
        # 清理过期记录
        while timestamps and timestamps[0] < cutoff:
            timestamps.pop(0)
        # 突发容量 = 窗口内请求数 < limit + burst
        if len(timestamps) < self._limit + self._burst:
            timestamps.append(now)
            return True
        return False

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        now = time.time()
        key = self._key(request)
        if not self._is_allowed(key, now):
            return JSONResponse(
                {"error": "rate_limited", "message": "too many requests"},
                status_code=429,
                headers={"Retry-After": str(self._window)},
            )
        return await call_next(request)


class ToolGovernServer:
    """HTTP 治理服务封装。

    Args:
        controller: 已构造的 LoopController 实例。
        api_key: 可选 API key；未设置时不校验。
        watcher: 审批事件通知器；默认新建一个。
        start_time: 服务启动时间戳（用于 uptime 计算）。
        identity_provider: 身份 Provider；默认从 controller Runtime 读取。
        entrypoints_config: 入口认证配置；默认空 dict（向后兼容）。
    """

    def __init__(
        self,
        controller: LoopController,
        api_key: str | None = None,
        watcher: ApprovalWatcher | None = None,
        start_time: float | None = None,
        identity_provider: IdentityProvider | None = None,
        entrypoints_config: dict[str, Any] | None = None,
    ) -> None:
        self._controller = controller
        self._api_key = api_key
        self._watcher = watcher or ApprovalWatcher()
        self._start_time = start_time or time.time()
        self._identity_provider = identity_provider or _extract_identity_provider(controller)
        self._entrypoints_config = entrypoints_config or {}

    def _http_require_auth(self) -> bool:
        """读取 entrypoints.http.require_auth；缺省 false 保持向后兼容。"""
        entrypoints = self._entrypoints_config.get("entrypoints") or self._entrypoints_config
        http_cfg = entrypoints.get("http") or {}
        return bool(http_cfg.get("require_auth", False))

    def _check_api_key(self, request: Request) -> bool:
        """管理员端点的全局 API key 强制校验。"""
        if not self._api_key:
            return False
        header = request.headers.get("x-api-key") or ""
        auth = request.headers.get("authorization") or ""
        token = ""
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
        for candidate in (header, token):
            if candidate and len(candidate) == len(self._api_key):
                if hmac.compare_digest(candidate, self._api_key):
                    return True
        return False

    def _admin_actor_id(self, request: Request) -> str:
        """从请求中提取管理员 API key 的匿名标识；未认证时返回 unauthenticated。"""
        if not self._api_key:
            return "unauthenticated"
        header = request.headers.get("x-api-key") or ""
        auth = request.headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
        else:
            token = ""
        key = header or token
        if not key:
            return "unauthenticated"
        return f"api-key:{hashlib.sha256(key.encode()).hexdigest()[:12]}"

    async def _audit_admin_operation(
        self,
        request: Request,
        operation: str,
        *,
        target: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        audit_store = getattr(self._controller._runtime, "audit_store", None)
        if audit_store is None:
            return
        actor_id = self._admin_actor_id(request)
        trace_id = getattr(request.state, "trace_id", uuid.uuid4().hex[:16])
        await audit_store.append_async(
            AuditEvent(
                event_id=uuid.uuid4().hex,
                trace_id=trace_id,
                session_id="admin",
                actor_type="system",
                actor_id=actor_id,
                action="admin_operation",
                target=target,
                reason=operation,
                metadata=metadata or {},
            )
        )

    async def _verify_identity(self, request: Request) -> AgentIdentity | None:
        """从 Authorization: Bearer <jwt> 提取并验证身份；未提供/无 Provider 返回 None。"""
        if self._identity_provider is None:
            return None
        auth = request.headers.get("authorization") or ""
        if not auth.lower().startswith("bearer "):
            return None
        token = auth[7:].strip()
        if not token:
            return None
        credential = IdentityCredential(token=token)
        return await self._identity_provider.verify(credential)

    async def _check_agent_auth(self, request: Request) -> tuple[bool, AgentIdentity | None]:
        """校验 agent 端点身份：优先 identity，无 provider 但有 api_key 时回退到 api_key。

        Returns:
            (authorized, identity): identity 仅在通过 identity provider 验证时非 None。
        """
        identity = await self._verify_identity(request)
        if identity is not None:
            return True, identity
        if self._identity_provider is None and self._api_key is not None:
            return self._check_api_key(request), None
        if self._http_require_auth():
            return False, None
        return True, None

    async def _handle_health(self, request: Request) -> JSONResponse:
        opa_reachable = await self._opa_reachable()
        gateway_ready = self._controller.started if hasattr(self._controller, "started") else True
        audit_store = self._controller._runtime.audit_store
        evidence_status = getattr(audit_store, "evidence_status", "disabled")
        anchor = getattr(self._controller._runtime, "evidence_anchor", None)
        anchor_summary = (
            anchor.sanitized_status()
            if anchor is not None
            else {
                "anchor_status": "disabled",
                "anchor_stream_id": None,
                "anchor_last_success_seq": 0,
                "anchor_lag_events": 0,
                "anchor_last_error_code": None,
            }
        )
        uptime = time.time() - self._start_time
        persistence = getattr(self._controller._runtime, "persistence_status", None)
        persistence_summary = persistence.as_dict() if persistence is not None else {}
        fsync_enabled = persistence_summary.get("fsync_enabled", True)
        persistence_status = persistence_summary.get("status", "healthy")
        durability = (
            "safe"
            if fsync_enabled and persistence_status in {"healthy", "tail_repaired"}
            else "unsafe"
        )
        set_persistence_durability(durability == "safe", bool(fsync_enabled))
        executor = getattr(self._controller._runtime, "harness_executor", None)
        if executor is not None:
            harness_backends = [status.model_dump(mode="json") for status in executor.backend_statuses()]
        else:
            harness_backends = []
        harness_degraded = any(
            backend["status"] not in {"healthy", "degraded"} or backend.get("draining", False)
            for backend in harness_backends
        )
        degraded = (
            evidence_status == "degraded"
            or anchor_summary["anchor_status"] not in {"disabled", "healthy"}
            or persistence_status != "healthy"
            or harness_degraded
        )
        return JSONResponse(
            HealthResponse(
                status="degraded" if degraded else "ok",
                opa_reachable=opa_reachable,
                gateway_ready=gateway_ready,
                evidence_status=evidence_status,
                persistence=persistence_summary,
                durability=durability,
                uptime_seconds=round(uptime, 2),
                harness_backends=harness_backends,
                **anchor_summary,
            ).model_dump()
        )

    async def _handle_identity(self, request: Request) -> JSONResponse:
        """v0.20.0：调试端点，返回当前 Bearer token 解析出的身份摘要。"""
        identity = await self._verify_identity(request)
        if identity is None:
            return JSONResponse(
                {
                    "authenticated": False,
                    "provider_available": self._identity_provider is not None,
                }
            )
        return JSONResponse(
            {
                "authenticated": True,
                "agent_id": identity.agent_id,
                "user_id": identity.user_id,
                "profile_id": identity.profile_id,
                "harness_id": identity.harness_id,
            }
        )

    async def _handle_delegation_authorize(self, request: Request) -> JSONResponse:
        """由 IIGE 完成 Agent 委托授权；旧 R2 路由复用同一处理器。"""
        authorized, identity = await self._check_agent_auth(request)
        if not authorized or identity is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        try:
            payload = await request.json()
        except Exception as exc:
            logger.warning("invalid delegation authorize request: %s", exc)
            return JSONResponse({"error": f"invalid request: {exc}"}, status_code=422)
        if not isinstance(payload, dict):
            return JSONResponse({"error": "request body must be an object"}, status_code=422)

        source_agent_id = payload.get("source_agent_id") or payload.get("initiator_agent_id")
        if source_agent_id != identity.agent_id:
            return JSONResponse({"error": "source agent identity mismatch"}, status_code=403)
        payload["source_agent_id"] = source_agent_id
        if "arguments" not in payload and "arguments_json" in payload:
            return JSONResponse(
                {"error": "arguments_json is no longer supported; use arguments object"},
                status_code=422,
            )

        engine = InteractionGovernanceEngine(self._controller)
        endpoint = InteractionAuthorizeEndpoint(engine)
        try:
            response = await endpoint.handle(payload)
        except Exception as exc:
            logger.warning("invalid interaction proposal: %s", exc)
            return JSONResponse({"error": f"invalid request: {exc}"}, status_code=422)

        if endpoint.last_proposal is not None and endpoint.last_decision is not None:
            event = engine.build_audit_event(endpoint.last_proposal, endpoint.last_decision)
            await self._controller._runtime.audit_store.append_async(event)
        elif endpoint.last_rejection_event is not None:
            await self._controller._runtime.audit_store.append_async(
                endpoint.last_rejection_event
            )
        return JSONResponse(response)

    async def _handle_metrics(self, request: Request) -> PlainTextResponse:
        if self._api_key is not None and not self._check_api_key(request):
            return PlainTextResponse(content="unauthorized", status_code=401)
        data = render_metrics()
        return PlainTextResponse(
            content=data, media_type="text/plain; version=0.0.4; charset=utf-8"
        )

    async def _handle_govern_tool_call(self, request: Request) -> JSONResponse:
        authorized, identity = await self._check_agent_auth(request)
        if not authorized:
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        try:
            body = GovernToolRequest(**await request.json())
        except Exception as exc:
            logger.warning("invalid tool-call request: %s", exc)
            return JSONResponse({"error": f"invalid request: {exc}"}, status_code=422)

        # 当身份 Provider 可用时，使用凭证中的 agent_id/user_id，请求体只做一致性校验。
        if identity is not None:
            if body.agent_id and body.agent_id != identity.agent_id:
                return JSONResponse(
                    {"error": "agent_id inconsistent with identity"},
                    status_code=400,
                )
            if body.user_id and body.user_id != identity.user_id:
                return JSONResponse(
                    {"error": "user_id inconsistent with identity"},
                    status_code=400,
                )
            agent_id = identity.agent_id
            user_id = identity.user_id
        else:
            agent_id = body.agent_id
            user_id = body.user_id

        logger.info(
            "tool_call request agent=%s user=%s tool=%s",
            agent_id,
            user_id,
            body.tool_name,
        )
        result = await self._controller.evaluate_and_execute(
            agent_id=agent_id,
            user_id=user_id,
            tool_name=body.tool_name,
            arguments=body.arguments,
            task_context=body.task_context,
            session_id=body.session_id,
            task_id=body.task_id,
        )
        observe_tool_call(body.tool_name, result.status)
        self._refresh_pending_approvals()

        response = GovernResponse(
            status=result.status,
            result=result.content if result.content is not None else result.reason or result.status,
            request_id=result.request_id if result.status == "require_approval" else None,
            error_code=result.error_code,
        )
        logger.info(
            "tool_call result status=%s tool=%s request_id=%s",
            result.status,
            body.tool_name,
            result.request_id,
        )
        return JSONResponse(response.model_dump())

    async def _handle_resume_after_approval(self, request: Request) -> JSONResponse:
        authorized, identity = await self._check_agent_auth(request)
        if not authorized:
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        try:
            body = ResumeApprovalRequest(**await request.json())
        except Exception as exc:
            return JSONResponse({"error": f"invalid request: {exc}"}, status_code=422)

        if identity is not None and not self._approval_request_belongs_to(body.request_id, identity):
            return JSONResponse({"error": "approval request does not belong to caller"}, status_code=403)

        logger.info("resume_after_approval request_id=%s", body.request_id)
        result = await self._controller.resume_after_approval(body.request_id)
        self._refresh_pending_approvals()
        return JSONResponse(
            GovernResponse(
                status=result.status,
                result=result.content
                if result.content is not None
                else result.reason or result.status,
                request_id=body.request_id if result.status == "require_approval" else None,
                error_code=result.error_code,
            ).model_dump()
        )

    async def _handle_wait_for_approval(self, request: Request) -> JSONResponse:
        authorized, identity = await self._check_agent_auth(request)
        if not authorized:
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        request_id = request.query_params.get("request_id")
        if not request_id:
            return JSONResponse({"error": "missing request_id"}, status_code=422)
        if identity is not None and not self._approval_request_belongs_to(request_id, identity):
            return JSONResponse({"error": "approval request does not belong to caller"}, status_code=403)

        try:
            max_wait = float(request.query_params.get("max_wait", "30"))
        except ValueError:
            return JSONResponse(
                {"error": "invalid_parameter", "message": "max_wait must be a number"},
                status_code=400,
            )
        max_wait = max(1.0, min(max_wait, 300.0))

        # 轮询 ApprovalStore，等待审批记录出现；watcher 可在同进程内立即唤醒
        deadline = time.monotonic() + max_wait
        while time.monotonic() < deadline:
            result = await self._try_resume(request_id)
            if result is not None:
                return JSONResponse(
                    WaitApprovalResponse(
                        status=result.status,
                        result=result.content
                        if result.content is not None
                        else result.reason or result.status,
                        request_id=request_id,
                        error_code=result.error_code,
                    ).model_dump()
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            wait_time = min(1.0, remaining)
            await self._watcher.wait(request_id, timeout=wait_time)

        return JSONResponse(
            WaitApprovalResponse(status="pending", request_id=request_id).model_dump()
        )

    async def _handle_wait_for_approval_sse(self, request: Request) -> StreamingResponse:
        """SSE 实时审批等待。"""
        authorized, identity = await self._check_agent_auth(request)
        if not authorized:
            return StreamingResponse(
                self._sse_error("unauthorized"),
                status_code=401,
                media_type="text/event-stream",
            )

        request_id = request.query_params.get("request_id")
        if not request_id:
            return StreamingResponse(
                self._sse_error("missing request_id"),
                status_code=422,
                media_type="text/event-stream",
            )
        if identity is not None and not self._approval_request_belongs_to(request_id, identity):
            return StreamingResponse(
                self._sse_error("approval request does not belong to caller"),
                status_code=403,
                media_type="text/event-stream",
            )

        try:
            max_wait = float(request.query_params.get("max_wait", "60"))
        except ValueError:
            return StreamingResponse(
                self._sse_error("max_wait must be a number"),
                status_code=400,
                media_type="text/event-stream",
            )
        max_wait = max(1.0, min(max_wait, 300.0))

        async def event_stream():
            # 立即发送 pending 心跳
            yield self._sse_event("pending", {"request_id": request_id, "status": "pending"})
            deadline = time.monotonic() + max_wait
            while time.monotonic() < deadline:
                result = await self._try_resume(request_id)
                if result is not None:
                    payload = {
                        "request_id": request_id,
                        "status": result.status,
                        "result": result.content
                        if result.content is not None
                        else result.reason or result.status,
                    }
                    if result.error_code:
                        payload["error_code"] = result.error_code
                    yield self._sse_event("result", payload)
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                wait_time = min(1.0, remaining)
                await self._watcher.wait(request_id, timeout=wait_time)
            yield self._sse_event("pending", {"request_id": request_id, "status": "pending"})

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )

    @staticmethod
    def _sse_event(event: str, data: dict) -> bytes:
        import json

        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()

    @staticmethod
    def _sse_error(message: str):
        import json

        yield f"event: error\ndata: {json.dumps({'error': message}, ensure_ascii=False)}\n\n".encode()

    def _approval_request_belongs_to(self, request_id: str, identity: AgentIdentity) -> bool:
        approval_request = self._controller._runtime.approval_manager.get_request_by_id(request_id)
        return approval_request is not None and (
            approval_request.agent_id == identity.agent_id
            and approval_request.requester_id == identity.user_id
        )

    async def _try_resume(self, request_id: str) -> Any | None:
        """尝试恢复审批；若审批不存在则返回 None。"""
        approval_manager = self._controller._runtime.approval_manager
        approval_request = approval_manager.get_request_by_id(request_id)
        if approval_request is None:
            return None
        record = approval_manager.check(approval_request.decision_id)
        if record is None:
            return None
        return await self._controller.resume_after_approval(request_id)

    async def _handle_admin_pending_approvals(self, request: Request) -> JSONResponse:
        if not self._check_api_key(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        store = self._controller._runtime.approval_manager._store
        store.refresh()
        pending = store.get_pending()
        items = [
            PendingApprovalItem(
                request_id=req.request_id,
                decision_id=req.decision_id,
                tool_name=req.tool_name,
                requester_id=req.requester_id,
                reason=req.reason,
            )
            for req in pending
        ]
        return JSONResponse(PendingApprovalsResponse(approvals=items).model_dump())

    async def _handle_admin_revoke(self, request: Request) -> JSONResponse:
        if not self._check_api_key(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        revocations = getattr(self._controller._runtime, "revocation_list", None)
        if revocations is None:
            return JSONResponse({"error": "revocation unavailable"}, status_code=503)
        try:
            if request.method == "DELETE":
                entry_type = RevocationType(request.query_params.get("type", ""))
                entry_id = request.query_params.get("id", "")
                if not entry_id:
                    raise ValueError("missing id")
                tenant_id = request.query_params.get("tenant_id")
                removed = revocations.remove(entry_type, entry_id, tenant_id)
                await self._audit_admin_operation(
                    request,
                    "revocation_removed",
                    target=f"{entry_type.value}:{entry_id}",
                    metadata={"tenant_id": tenant_id, "removed": removed},
                )
                return JSONResponse({"removed": removed})
            body = RevokeRequest.model_validate(await request.json())
            entry = body.to_entry()
            revocations.add(entry)
            await self._audit_admin_operation(
                request,
                "revocation_added",
                target=f"{entry.type.value}:{entry.id}",
                metadata={"tenant_id": entry.tenant_id, "reason": entry.reason},
            )
            return JSONResponse({"revoked": True})
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": f"invalid request: {exc}"}, status_code=422)

    async def _handle_admin_revocation_list(self, request: Request) -> JSONResponse:
        if not self._check_api_key(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        revocations = getattr(self._controller._runtime, "revocation_list", None)
        if revocations is None:
            return JSONResponse({"error": "revocation unavailable"}, status_code=503)
        response = RevocationListResponse(
            revocations=revocations.entries, kill_switch=revocations.kill_switch
        )
        return JSONResponse(response.model_dump(mode="json"))

    async def _handle_admin_kill_switch(self, request: Request) -> JSONResponse:
        if not self._check_api_key(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        revocations = getattr(self._controller._runtime, "revocation_list", None)
        if revocations is None:
            return JSONResponse({"error": "revocation unavailable"}, status_code=503)
        try:
            config = KillSwitchConfig.model_validate(await request.json())
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": f"invalid request: {exc}"}, status_code=422)
        revocations.set_kill_switch(config)
        await self._audit_admin_operation(
            request,
            "kill_switch_updated",
            target="kill_switch",
            metadata=config.model_dump(mode="json"),
        )
        return JSONResponse(config.model_dump(mode="json"))

    async def _handle_admin_harness_backends(self, request: Request) -> JSONResponse:
        if not self._check_api_key(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        executor = getattr(self._controller._runtime, "harness_executor", None)
        if executor is None:
            return JSONResponse({"backends": []})
        return JSONResponse(
            {"backends": [status.model_dump(mode="json") for status in executor.backend_statuses()]}
        )

    async def _handle_admin_harness_drain(self, request: Request) -> JSONResponse:
        if not self._check_api_key(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        executor = getattr(self._controller._runtime, "harness_executor", None)
        if executor is None:
            return JSONResponse({"error": "harness unavailable"}, status_code=503)

        name = request.path_params["name"]
        try:
            drained = await executor.drain_backend(name)
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        await self._audit_admin_operation(
            request,
            "harness_drain",
            target=f"harness:{name}",
            metadata={"backend": name, "drained": drained},
        )
        return JSONResponse({"backend": name, "drained": drained})

    async def _handle_admin_harness_reset(self, request: Request) -> JSONResponse:
        if not self._check_api_key(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        executor = getattr(self._controller._runtime, "harness_executor", None)
        if executor is None:
            return JSONResponse({"error": "harness unavailable"}, status_code=503)

        name = request.path_params["name"]
        try:
            executor.reset_backend(name)
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        await self._audit_admin_operation(
            request,
            "harness_reset",
            target=f"harness:{name}",
            metadata={"backend": name},
        )
        return JSONResponse({"backend": name, "reset": True})

    async def _handle_admin_evidence_anchor(self, request: Request) -> JSONResponse:
        if not self._check_api_key(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        audit_store = self._controller._runtime.audit_store
        if hasattr(audit_store, "anchor_summary"):
            return JSONResponse(audit_store.anchor_summary())
        return JSONResponse(
            {
                "evidence_status": "disabled",
                "anchor_status": "disabled",
                "anchor_stream_id": None,
                "anchor_last_success_seq": 0,
                "anchor_lag_events": 0,
                "anchor_last_error_code": None,
            }
        )

    async def _handle_admin_evidence_anchor_verify(self, request: Request) -> JSONResponse:
        if not self._check_api_key(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        audit_store = self._controller._runtime.audit_store
        if not hasattr(audit_store, "verify_anchor"):
            return JSONResponse({"error": "anchor unavailable"}, status_code=503)
        summary = await audit_store.verify_anchor()
        await self._audit_admin_operation(
            request,
            "anchor_verify",
            target="anchor",
            metadata={"anchor_status": summary.get("anchor_status")},
        )
        return JSONResponse(summary)

    async def _handle_admin_evidence_anchor_publish(self, request: Request) -> JSONResponse:
        if not self._check_api_key(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        audit_store = self._controller._runtime.audit_store
        if not hasattr(audit_store, "publish_anchor"):
            return JSONResponse({"error": "anchor unavailable"}, status_code=503)
        try:
            summary = await audit_store.publish_anchor()
        except RuntimeError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        await self._audit_admin_operation(
            request,
            "anchor_publish",
            target="anchor",
            metadata={"anchor_status": summary.get("anchor_status")},
        )
        return JSONResponse(summary)

    async def _handle_admin_evidence_anchor_bootstrap(self, request: Request) -> JSONResponse:
        if not self._check_api_key(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        audit_store = self._controller._runtime.audit_store
        if not hasattr(audit_store, "bootstrap_anchor"):
            return JSONResponse({"error": "anchor unavailable"}, status_code=503)

        trace_id = getattr(request.state, "trace_id", uuid.uuid4().hex[:16])
        event = AuditEvent(
            event_id=uuid.uuid4().hex,
            trace_id=trace_id,
            session_id="admin",
            actor_type="system",
            actor_id=self._admin_actor_id(request),
            action="anchor_bootstrap",
            target="anchor",
            reason="explicit bootstrap",
        )
        try:
            summary = await audit_store.bootstrap_anchor(event)
        except RuntimeError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        await self._audit_admin_operation(
            request,
            "anchor_bootstrap",
            target="anchor",
            metadata={"anchor_status": summary.get("anchor_status")},
        )
        return JSONResponse(summary)

    async def _handle_admin_approval(self, request: Request, *, verdict: str) -> JSONResponse:
        if not self._check_api_key(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        decision_id = request.path_params["decision_id"]
        try:
            body = await request.json() if request.method == "POST" else {}
        except Exception:  # noqa: BLE001
            body = {}
        if not isinstance(body, dict):
            body = {}
        approver_id = str(body.get("approver", "")).strip()
        comment = str(body.get("comment", "")).strip()
        if not approver_id:
            return JSONResponse({"error": "approver is required"}, status_code=422)

        approval_manager = self._controller._runtime.approval_manager
        store = approval_manager._store
        store.refresh()
        req = store.get_request(decision_id)
        existing = store.get_record(decision_id)

        identity = self._identity_provider

        def _approver_exists(user_id: str) -> bool:
            if identity is None:
                return False
            return identity.get_user(user_id) is not None

        try:
            record = build_approval_record(
                req,
                existing,
                approver_id,
                verdict,
                comment,
                approver_exists=_approver_exists,
            )
        except ApprovalServiceError as exc:
            status = 409 if "已有审批结果" in str(exc) else 422
            return JSONResponse({"error": str(exc)}, status_code=status)

        try:
            store.record_response(record)
        except ApprovalStoreError as exc:
            if "已有审批结果" in str(exc):
                return JSONResponse({"error": "approval already recorded"}, status_code=409)
            return JSONResponse({"error": str(exc)}, status_code=500)
        if req is not None:
            self._watcher.notify(req.request_id)
        await self._audit_admin_operation(
            request,
            f"approval_{verdict}",
            target=f"decision:{decision_id}",
            metadata={"approver_id": approver_id, "comment": comment},
        )
        return JSONResponse({"decision_id": decision_id, "verdict": verdict})

    async def _handle_admin_approvals_approve(self, request: Request) -> JSONResponse:
        return await self._handle_admin_approval(request, verdict="approve")

    async def _handle_admin_approvals_deny(self, request: Request) -> JSONResponse:
        return await self._handle_admin_approval(request, verdict="deny")

    async def _handle_admin_audit(self, request: Request) -> JSONResponse:
        if not self._check_api_key(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        session_id = request.query_params.get("session_id")
        task_id = request.query_params.get("task_id")
        interaction_id = request.query_params.get("interaction_id")
        source_agent_id = request.query_params.get("source_agent_id")
        target_agent_id = request.query_params.get("target_agent_id")
        verdict = request.query_params.get("verdict")
        valid_verdicts = {"allow", "deny", "modify", "require_approval"}
        if verdict is not None and verdict not in valid_verdicts:
            return JSONResponse(
                {"error": "invalid_parameter", "message": "invalid interaction verdict"},
                status_code=400,
            )
        try:
            limit = int(request.query_params.get("limit", "100"))
        except ValueError:
            return JSONResponse(
                {"error": "invalid_parameter", "message": "limit must be an integer"},
                status_code=400,
            )
        limit = max(1, min(limit, 1000))

        audit_store = self._controller._runtime.audit_store
        interaction_filters = any(
            value is not None
            for value in (interaction_id, source_agent_id, target_agent_id, verdict)
        )
        if interaction_filters:
            interaction_events = audit_store.query_interactions(
                interaction_id=interaction_id,
                source_agent_id=source_agent_id,
                target_agent_id=target_agent_id,
                verdict=verdict,
                limit=limit,
            )
            events = [event.model_dump(mode="json") for event in interaction_events]
        else:
            events = []
            async for event in audit_store.iter_events():
                payload = event.model_dump()
                if session_id and payload.get("session_id") != session_id:
                    continue
                if task_id and payload.get("task_id") != task_id:
                    continue
                events.append(payload)
                if len(events) >= limit:
                    break
            events.reverse()
        return JSONResponse(AuditQueryResponse(events=events).model_dump())

    def _refresh_pending_approvals(self) -> None:
        try:
            store = self._controller._runtime.approval_manager._store
            set_pending_approvals(len(store.get_pending()))
        except Exception:  # noqa: BLE001
            pass

    async def _opa_reachable(self) -> bool:
        """通过访问 OPA /health 判断可达性。"""
        try:
            engine = getattr(self._controller._runtime.checkpoint, "_policy_engine", None)
            if engine is None:
                return False
            base_url = getattr(engine, "_base_url", None)
            if not base_url:
                return False
            async with httpx.AsyncClient(trust_env=False, timeout=2.0) as client:
                resp = await client.get(f"{base_url}/health")
                return resp.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    async def _sleep(seconds: float) -> None:
        import asyncio

        await asyncio.sleep(seconds)


def build_app(
    controller: LoopController,
    api_key: str | None = None,
    watcher: ApprovalWatcher | None = None,
    configure_logs: bool = True,
    identity_provider: IdentityProvider | None = None,
    entrypoints_config: dict[str, Any] | None = None,
) -> Starlette:
    """从 LoopController 构造 Starlette ASGI 应用。"""
    if configure_logs:
        configure_logging(
            json_format=os.environ.get("LOOP_CONTROLLER_JSON_LOGS", "").lower() == "true"
        )
    server = ToolGovernServer(
        controller,
        api_key=api_key,
        watcher=watcher,
        identity_provider=identity_provider,
        entrypoints_config=entrypoints_config,
    )

    @asynccontextmanager
    async def lifespan(app: Starlette):
        await controller.start()
        server._start_time = time.time()
        logger.info("Loop Controller HTTP server starting")
        yield
        logger.info("Loop Controller HTTP server shutting down")
        await controller.aclose()

    entrypoints = entrypoints_config or {}
    http_cfg = (entrypoints.get("entrypoints") or entrypoints).get("http") or {}
    cors_cfg = http_cfg.get("cors") or {}
    rate_limit_cfg = http_cfg.get("rate_limit") or {}
    max_body_size = int(http_cfg.get("max_body_size", DEFAULT_MAX_HTTP_BODY_SIZE))

    middleware: list[Middleware] = [
        Middleware(BodySizeLimitMiddleware, max_size=max_body_size)
    ]
    if rate_limit_cfg:
        middleware.append(
            Middleware(
                RateLimitMiddleware,
                requests_per_minute=int(rate_limit_cfg.get("requests_per_minute", 120)),
                burst=int(rate_limit_cfg.get("burst", 20)),
            )
        )
    middleware.append(Middleware(MetricsMiddleware))
    if cors_cfg.get("origins"):
        middleware.append(
            Middleware(
                CORSMiddleware,
                allow_origins=list(cors_cfg["origins"]),
                allow_methods=["*"],
                allow_headers=["*"],
            )
        )

    app = Starlette(
        debug=False,
        lifespan=lifespan,
        middleware=middleware,
        routes=[
            Route("/health", server._handle_health, methods=["GET"]),
            Route("/v1/identity", server._handle_identity, methods=["GET"]),
            Route("/v1/health", server._handle_health, methods=["GET"]),
            Route("/metrics", server._handle_metrics, methods=["GET"]),
            Route("/v1/govern/tool-call", server._handle_govern_tool_call, methods=["POST"]),
            Route(
                "/interaction/v1/delegations/authorize",
                server._handle_delegation_authorize,
                methods=["POST"],
            ),
            Route(
                "/r2/v1/delegations/authorize",
                server._handle_delegation_authorize,
                methods=["POST"],
            ),
            Route(
                "/v1/govern/resume-after-approval",
                server._handle_resume_after_approval,
                methods=["POST"],
            ),
            Route("/v1/wait-for-approval", server._handle_wait_for_approval, methods=["GET"]),
            Route(
                "/v1/wait-for-approval/sse", server._handle_wait_for_approval_sse, methods=["GET"]
            ),
            Route(
                "/v1/admin/approvals/pending",
                server._handle_admin_pending_approvals,
                methods=["GET"],
            ),
            Route(
                "/v1/admin/harness/backends", server._handle_admin_harness_backends, methods=["GET"]
            ),
            Route(
                "/v1/admin/harness/{name}/drain",
                server._handle_admin_harness_drain,
                methods=["POST"],
            ),
            Route(
                "/v1/admin/harness/{name}/reset",
                server._handle_admin_harness_reset,
                methods=["POST"],
            ),
            Route(
                "/v1/admin/evidence/anchor", server._handle_admin_evidence_anchor, methods=["GET"]
            ),
            Route(
                "/v1/admin/evidence/anchor/verify",
                server._handle_admin_evidence_anchor_verify,
                methods=["POST"],
            ),
            Route(
                "/v1/admin/evidence/anchor/publish",
                server._handle_admin_evidence_anchor_publish,
                methods=["POST"],
            ),
            Route(
                "/v1/admin/evidence/anchor/bootstrap",
                server._handle_admin_evidence_anchor_bootstrap,
                methods=["POST"],
            ),
            Route(
                "/v1/admin/approvals/{decision_id}/approve",
                server._handle_admin_approvals_approve,
                methods=["POST"],
            ),
            Route(
                "/v1/admin/approvals/{decision_id}/deny",
                server._handle_admin_approvals_deny,
                methods=["POST"],
            ),
            Route("/admin/revoke", server._handle_admin_revoke, methods=["POST", "DELETE"]),
            Route("/admin/revocation-list", server._handle_admin_revocation_list, methods=["GET"]),
            Route("/admin/kill-switch", server._handle_admin_kill_switch, methods=["POST"]),
            Route("/v1/admin/audit", server._handle_admin_audit, methods=["GET"]),
        ],
    )

    async def _http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        if not isinstance(exc, HTTPException):
            return JSONResponse(
                {"error": "internal_error"},
                status_code=500,
            )
        return JSONResponse(
            {"error": "http_error", "message": str(exc.detail)},
            status_code=exc.status_code,
        )

    async def _generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled HTTP exception: %s", exc)
        return JSONResponse(
            {"error": "internal_error"},
            status_code=500,
        )

    app.add_exception_handler(HTTPException, _http_exception_handler)
    app.add_exception_handler(Exception, _generic_exception_handler)
    return app


def load_api_key() -> str | None:
    """从环境变量读取 API key；未设置时返回 None。"""
    return os.environ.get("LOOP_CONTROLLER_API_KEY") or None
