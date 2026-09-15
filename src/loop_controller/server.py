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
import json
import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, AsyncIterator

import httpx

from loop_controller.approval_service import ApprovalServiceError, build_approval_record
from loop_controller.approval_watcher import ApprovalWatcher
from loop_controller.checkpoint import CheckpointError
from loop_controller.controller import LoopController
from loop_controller.go_kernel_bridge import DelegationRequest
from loop_controller.identity import (
    AgentIdentity,
    IdentityCredential,
    IdentityProvider,
    KillSwitchConfig,
    RevocationType,
)
from loop_controller.infra.approval_store import ApprovalStoreError, list_approval_history
from loop_controller.infra.admin_session import AdminSessionStore
from loop_controller.infra.config_loader import ConfigLoader
from loop_controller.infra.profile_config import ProfileConfigError, update_profile_tools
from loop_controller.interaction.engine import (
    InteractionAuthorizeEndpoint,
    InteractionGovernanceEngine,
)
from loop_controller.interaction.models import InteractionProposal
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
from loop_controller.models import ActionProposal, ApprovalRequest, AuditEvent, Task
from loop_controller.server_models import (
    AdminAgentDetail,
    AdminAgentItem,
    AdminAgentsResponse,
    AdminApprovalsResponse,
    AdminGovernEvaluateRequest,
    AdminGovernEvaluateResponse,
    AdminProfilesResponse,
    AdminProfileToolsUpdateRequest,
    AdminProfileUpdateResponse,
    AdminSessionLoginRequest,
    AdminSessionLoginResponse,
    AdminA2AAgentItem,
    AdminA2AStatusResponse,
    AdminDelegationDispatch,
    AdminDelegationRequest,
    AdminDelegationResponse,
    AdminDelegationApproval,
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


# 配置脱敏：键名命中敏感词时，字符串值/字符串列表项替换为掩码。
_SENSITIVE_KEY_PATTERN = re.compile(
    r"(secret|token|password|private|credential|api_key)", re.IGNORECASE
)
_MASK = "******"


def _mask_sensitive(value: Any, key: str = "") -> Any:
    """递归脱敏配置字典；命中敏感键名的字符串值替换为 ``******``。"""
    if isinstance(value, dict):
        return {k: _mask_sensitive(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        if _SENSITIVE_KEY_PATTERN.search(key):
            return [_MASK if isinstance(item, str) else _mask_sensitive(item) for item in value]
        return [_mask_sensitive(item) for item in value]
    if isinstance(value, str) and value and _SENSITIVE_KEY_PATTERN.search(key):
        return _MASK
    return value


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
        session_store: AdminSessionStore | None = None,
        interaction_engine: InteractionGovernanceEngine | None = None,
    ) -> None:
        self._controller = controller
        self._api_key = api_key
        self._watcher = watcher or ApprovalWatcher()
        self._start_time = start_time or time.time()
        self._identity_provider = identity_provider or _extract_identity_provider(controller)
        self._entrypoints_config = entrypoints_config or {}
        self._session_store = session_store or AdminSessionStore()
        # 管理端委托走 InteractionGovernanceEngine（与 Agent 自发委托同治理路径）；
        # 未注入时懒构造默认实例（默认 OPA 策略引擎）。
        self._interaction_engine = interaction_engine

    @property
    def interaction_engine(self) -> InteractionGovernanceEngine:
        if self._interaction_engine is None:
            self._interaction_engine = InteractionGovernanceEngine(self._controller)
        return self._interaction_engine

    def _http_require_auth(self) -> bool:
        """读取 entrypoints.http.require_auth；缺省 false 保持向后兼容。"""
        entrypoints = self._entrypoints_config.get("entrypoints") or self._entrypoints_config
        http_cfg = entrypoints.get("http") or {}
        return bool(http_cfg.get("require_auth", False))

    def _check_api_key(self, request: Request) -> bool:
        """管理员端点的全局 API key 强制校验；Bearer Session Token 作为替代凭据。"""
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
        # Session 登录签发的 Bearer Token（长期运行：前端不长期持有明文 API Key）
        return self._session_store.validate(token)

    def _bearer_token(self, request: Request) -> str:
        auth = request.headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return ""

    def _admin_actor_id(self, request: Request) -> str:
        """从请求中提取管理员身份匿名标识；未认证时返回 unauthenticated。"""
        if not self._api_key:
            return "unauthenticated"
        header = request.headers.get("x-api-key") or ""
        bearer = self._bearer_token(request)
        if not header and bearer and self._session_store.validate(bearer):
            return f"session:{hashlib.sha256(bearer.encode()).hexdigest()[:12]}"
        key = header or bearer
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

        # 委托类 require_approval：若同一 interaction 的审批单已被批准，则放行
        if response.get("verdict") == "require_approval" and self._delegation_approved(
            payload
        ):
            response = {
                **response,
                "verdict": "allow",
                "allowed": True,
                "reason": "approved via delegation approval record",
            }
        return JSONResponse(response)

    def _delegation_approved(self, payload: dict[str, Any]) -> bool:
        """查询 call_id=a2a-delegation:{interaction_id} 的审批单是否已被批准。

        除 call_id 外还校验委托快照与本次 authorize 请求一致（目标 Agent、
        工具名、参数），防止复用已批准的 interaction 放行被篡改的委托。
        """
        parent_interaction_id = payload.get("parent_interaction_id")
        if not parent_interaction_id:
            return False
        try:
            store = self._controller._runtime.approval_manager._store
            store.refresh()
            call_id = f"a2a-delegation:{parent_interaction_id}"
            for request in store.get_pending():
                if request.call_id == call_id:
                    return False  # 审批单存在但尚未审批
            for request in getattr(store, "requests", {}).values():
                if request.call_id != call_id:
                    continue
                if store.get_record(request.decision_id) is None:
                    continue
                snapshot = request.tool_arguments or {}
                if snapshot.get("target_agent_id") != payload.get("target_agent_id"):
                    return False
                if snapshot.get("tool_name") != payload.get("tool_name"):
                    return False
                if snapshot.get("arguments") != payload.get("arguments"):
                    return False
                return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("check delegation approval failed: %s", exc)
        return False

    async def _handle_interaction_lifecycle(self, request: Request) -> JSONResponse:
        """接收 Go Kernel 的 Interaction/Task 生命周期审计通知。"""
        authorized, _identity = await self._check_agent_auth(request)
        if not authorized:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("request body must be an object")
            required = (
                "event_id",
                "interaction_id",
                "decision_id",
                "task_id",
                "source_agent_id",
                "target_agent_id",
                "event",
            )
            missing = [field for field in required if not payload.get(field)]
            if missing:
                raise ValueError(f"missing lifecycle fields: {', '.join(missing)}")
            expected_event_id = f"lifecycle:{payload['task_id']}:{payload['event']}"
            if payload["event_id"] != expected_event_id:
                raise ValueError("lifecycle event_id does not match task and event")
            engine = InteractionGovernanceEngine(self._controller)
            event = engine.build_lifecycle_audit_event(payload)
        except (KeyError, ValueError) as exc:
            return JSONResponse({"error": f"invalid request: {exc}"}, status_code=422)
        await self._controller._runtime.audit_store.append_async(event)
        return JSONResponse({"accepted": True}, status_code=202)

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

        scope_error = self._validate_delegated_tool_scope(body)
        if scope_error is not None:
            return JSONResponse(
                {
                    "status": "blocked",
                    "result": scope_error,
                    "request_id": None,
                    "error_code": "delegation_scope_denied",
                },
                status_code=403,
            )
        if body.deadline is not None and body.deadline.timestamp() <= time.time():
            return JSONResponse(
                {
                    "status": "blocked",
                    "result": "delegated task deadline has expired",
                    "request_id": None,
                    "error_code": "task_deadline_expired",
                },
                status_code=409,
            )

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

    def _validate_delegated_tool_scope(self, body: GovernToolRequest) -> str | None:
        delegated = body.task_id is not None or body.allowed_tools is not None or body.allowed_capabilities is not None
        if not delegated:
            return None
        if body.task_id is None:
            return "delegated tool call requires task_id"
        if body.allowed_tools is None or body.allowed_capabilities is None:
            return "delegated tool call requires complete allowed_tools and allowed_capabilities scope"
        if body.tool_name not in body.allowed_tools:
            return f"tool {body.tool_name!r} is outside delegated allowed_tools"

        config = getattr(self._controller._runtime, "config", None)
        capability_rules = getattr(config, "capability_rules", None)
        if capability_rules is None:
            required_capabilities: set[str] = set()
        else:
            from loop_controller.capability import CapabilityGraphAnalyzer
            from loop_controller.models import ActionProposal

            proposal = ActionProposal(
                task_id=body.task_id or "delegated-scope-check",
                call_id="delegated-scope-check",
                agent_id=body.agent_id,
                tool_name=body.tool_name,
                arguments=body.arguments,
                task_context=body.task_context,
            )
            required_capabilities = CapabilityGraphAnalyzer(
                capability_rules.capabilities, []
            ).extract_capabilities(proposal)
        missing = required_capabilities.difference(body.allowed_capabilities or [])
        if missing:
            return "tool requires capabilities outside delegated scope: " + ", ".join(
                sorted(missing)
            )
        return None

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

    async def _handle_admin_approvals(self, request: Request) -> JSONResponse:
        """GET /v1/admin/approvals：审批历史（兼容 JSONL，按未来分页契约返回）。"""
        if not self._check_api_key(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        status = request.query_params.get("status")
        valid_status = {None, "all", "pending", "approve", "deny"}
        if status not in valid_status:
            return JSONResponse(
                {"error": "invalid_parameter", "message": "invalid approval status"},
                status_code=400,
            )
        try:
            limit = int(request.query_params.get("limit", "100"))
            offset = int(request.query_params.get("offset", "0"))
        except ValueError:
            return JSONResponse(
                {"error": "invalid_parameter", "message": "limit/offset must be integers"},
                status_code=400,
            )
        limit = max(1, min(limit, 1000))
        offset = max(0, offset)
        store = self._controller._runtime.approval_manager._store
        requests = getattr(store, "requests", {})
        responses = getattr(store, "responses", {})
        pending = [request for decision_id, request in requests.items() if decision_id not in responses]
        completed = [
            (request, responses[decision_id])
            for decision_id, request in requests.items()
            if decision_id in responses
        ]
        items = list_approval_history(
            pending,
            completed,
            status=status,
            agent_id=request.query_params.get("agent_id"),
            tool_name=request.query_params.get("tool_name"),
            requester_id=request.query_params.get("requester_id"),
            approver_id=request.query_params.get("approver_id"),
            limit=limit,
            offset=offset,
        )
        return JSONResponse(
            AdminApprovalsResponse(
                approvals=items,
                total=len(items),
                limit=limit,
                offset=offset,
            ).model_dump(mode="json")
        )

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

        # 委托类审批单：批准后自动派发（Go 内核 authorize 会经审批记录放行）
        dispatch_payload: dict[str, Any] | None = None
        if verdict == "approve" and req is not None and str(req.call_id).startswith(
            "a2a-delegation:"
        ):
            dispatch_payload = await self._dispatch_approved_delegation(req)
        await self._audit_admin_operation(
            request,
            f"approval_{verdict}",
            target=f"decision:{decision_id}",
            metadata={
                "approver_id": approver_id,
                "comment": comment,
                "delegation_dispatch": dispatch_payload,
            },
        )
        response: dict[str, Any] = {"decision_id": decision_id, "verdict": verdict}
        if dispatch_payload is not None:
            response["dispatch"] = dispatch_payload
        return JSONResponse(response)

    async def _dispatch_approved_delegation(self, req: ApprovalRequest) -> dict[str, Any]:
        """按审批单中保存的委托快照重建 DelegationRequest 并派发。"""
        payload = req.tool_arguments or {}
        bridge, _gk = self._go_kernel_view()
        if bridge is None:
            return {"attempted": False, "accepted": False, "reason": "go kernel disabled"}
        interaction_id = str(req.call_id).removeprefix("a2a-delegation:")
        try:
            delegation = await bridge.request_delegation(
                DelegationRequest(
                    request_id=req.decision_id,
                    initiator_agent_id=str(payload.get("source_agent_id") or ""),
                    target_agent_id=str(payload.get("target_agent_id") or ""),
                    tool_name=str(payload.get("tool_name") or ""),
                    arguments=payload.get("arguments") or {},
                    session_id=str(payload.get("session_id") or ""),
                    # 快照中的 task_id 是 Python 侧语义，内核任务必须由内核实建，
                    # 携带不存在的 task_id 会被内核以 "delegation task not found" 拒绝。
                    task_id=None,
                    risk_level=str(payload.get("risk_level") or "low"),
                    allow_redelegation=bool(payload.get("allow_redelegation")),
                    parent_interaction_id=interaction_id,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("dispatch approved delegation failed: %s", exc)
            return {"attempted": True, "accepted": False, "reason": str(exc)}
        if delegation.allowed:
            return {
                "attempted": True,
                "accepted": True,
                "task_id": delegation.task_id or "",
                "reason": "",
            }
        # 内核侧仍要求审批（authorize 翻转未生效等）：Python 审批单已批准，
        # 代为完成内核批准 + ResumeApproval（幂等，request_id=审批单 decision_id）。
        if delegation.verdict == "require_approval" and delegation.approval_id:
            consumed = await bridge.approve_delegation(
                delegation.approval_id,
                request_id=req.decision_id,
                reason="approved via console approval record",
            )
            if consumed is not None and consumed.status == "consumed" and consumed.task_id:
                return {
                    "attempted": True,
                    "accepted": True,
                    "task_id": consumed.task_id,
                    "reason": "",
                    "kernel_approval_id": delegation.approval_id,
                    "kernel_verdict": "require_approval",
                }
            return {
                "attempted": True,
                "accepted": False,
                "reason": "kernel approval resume failed",
                "kernel_approval_id": delegation.approval_id,
                "kernel_verdict": "require_approval",
            }
        return {
            "attempted": True,
            "accepted": False,
            "task_id": "",
            "reason": delegation.reason,
        }

    async def _handle_admin_kernel_approvals(self, request: Request) -> JSONResponse:
        """内核委托审批对账视图：枚举内核审批单并与审批台记录关联。"""
        if not self._check_api_key(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        bridge, _gk = self._go_kernel_view()
        if bridge is None:
            return JSONResponse({"enabled": False, "approvals": []})
        status = request.query_params.get("status", "")
        kernel_approvals = await bridge.list_delegation_approvals(status=status)
        # 关联审批台记录：派发时 request_id=审批单 decision_id，即内核 approval.request_id
        console_by_request_id = self._console_approvals_by_request_id()
        items = []
        for approval in kernel_approvals:
            console = console_by_request_id.get(str(approval.get("request_id") or ""))
            items.append(
                {
                    "kernel": approval,
                    "console_decision_id": console.get("decision_id") if console else "",
                    "console_verdict": console.get("verdict") if console else "",
                    "reconciled": bool(console),
                }
            )
        return JSONResponse({"enabled": True, "approvals": items})

    def _console_approvals_by_request_id(self) -> dict[str, dict[str, Any]]:
        """返回 {request_id(decision_id): {decision_id, verdict}} 供对账关联。"""
        try:
            store = self._controller._runtime.approval_manager._store
            store.refresh()
            result: dict[str, dict[str, Any]] = {}
            responses = getattr(store, "responses", {})
            for request in store.get_pending():
                result[request.decision_id] = {
                    "decision_id": request.decision_id,
                    "verdict": "pending",
                }
            for decision_id, record in responses.items():
                result[str(decision_id)] = {
                    "decision_id": str(decision_id),
                    "verdict": str(getattr(record, "verdict", "")),
                }
            return result
        except Exception as exc:  # noqa: BLE001
            logger.warning("collect console approvals for reconciliation failed: %s", exc)
            return {}

    async def _handle_admin_approvals_approve(self, request: Request) -> JSONResponse:
        return await self._handle_admin_approval(request, verdict="approve")

    async def _handle_admin_approvals_deny(self, request: Request) -> JSONResponse:
        return await self._handle_admin_approval(request, verdict="deny")

    async def _handle_admin_audit(self, request: Request) -> JSONResponse:
        if not self._check_api_key(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        session_id = request.query_params.get("session_id")
        task_id = request.query_params.get("task_id")
        agent_id = request.query_params.get("agent_id")
        tool_name = request.query_params.get("tool_name")
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
                payload = event.model_dump(mode="json")
                if session_id and payload.get("session_id") != session_id:
                    continue
                if task_id and payload.get("task_id") != task_id:
                    continue
                if agent_id and payload.get("agent_id") != agent_id:
                    continue
                if tool_name and payload.get("tool_name") != tool_name:
                    continue
                events.append(payload)
                if len(events) >= limit:
                    break
            events.reverse()
        return JSONResponse(AuditQueryResponse(events=events).model_dump())

    def _revoked_ids(self, entry_type: RevocationType) -> set[str]:
        """当前生效（未过期）的吊销条目 ID 集合。"""
        revocations = getattr(self._controller._runtime, "revocation_list", None)
        if revocations is None:
            return set()
        now = datetime.now(UTC)
        return {
            entry.id
            for entry in revocations.entries
            if entry.type == entry_type and (entry.expires_at is None or entry.expires_at > now)
        }

    async def _handle_admin_agents(self, request: Request) -> JSONResponse:
        """GET /v1/admin/agents：列出已配置 Agent 及其吊销状态。"""
        if not self._check_api_key(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        config = getattr(self._controller._runtime, "config", None)
        if config is None:
            return JSONResponse({"error": "config unavailable"}, status_code=503)
        revoked_ids = self._revoked_ids(RevocationType.AGENT)
        items = [
            AdminAgentItem(
                agent_id=agent.agent_id,
                name=agent.name,
                profile_id=agent.profile_id,
                owner_id=agent.owner_id,
                owner_name=config.users.get(agent.owner_id),
                tenant_id=agent.tenant_id,
                revoked=agent.agent_id in revoked_ids,
            )
            for agent in config.agents.values()
        ]
        return JSONResponse(AdminAgentsResponse(agents=items).model_dump(mode="json"))

    async def _handle_admin_agent_detail(self, request: Request) -> JSONResponse:
        """GET /v1/admin/agents/{agent_id}：返回单个 Agent 详情。"""
        if not self._check_api_key(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        agent_id = request.path_params["agent_id"]
        config = getattr(self._controller._runtime, "config", None)
        if config is None:
            return JSONResponse({"error": "config unavailable"}, status_code=503)
        agent = config.agents.get(agent_id)
        if agent is None:
            return JSONResponse({"error": "agent not found"}, status_code=404)
        revoked_ids = self._revoked_ids(RevocationType.AGENT)
        item = AdminAgentDetail(
            agent_id=agent.agent_id,
            name=agent.name,
            profile_id=agent.profile_id,
            owner_id=agent.owner_id,
            owner_name=config.users.get(agent.owner_id),
            tenant_id=agent.tenant_id,
            revoked=agent.agent_id in revoked_ids,
            description=getattr(agent, "description", None),
            metadata=getattr(agent, "metadata", {}) or {},
        )
        return JSONResponse(item.model_dump(mode="json"))

    async def _handle_admin_profiles(self, request: Request) -> JSONResponse:
        """GET /v1/admin/profiles：列出已加载的 CapabilityProfile。"""
        if not self._check_api_key(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        profiles = self._controller._runtime.profiles
        return JSONResponse(
            AdminProfilesResponse(
                profiles=[p.model_dump(mode="json") for p in profiles.values()]
            ).model_dump(mode="json")
        )

    async def _reload_runtime_profiles(self) -> bool:
        """从磁盘重载 profiles.yaml 并原地刷新共享映射；返回是否成功。"""
        runtime = self._controller._runtime
        config_dir = getattr(runtime, "config_dir", None)
        if config_dir is None:
            return False
        try:
            new_profiles = ConfigLoader().reload_profiles(config_dir)
        except Exception as exc:  # noqa: BLE001
            logger.warning("profiles.yaml 重载失败：%s", exc)
            return False
        runtime.profiles.clear()
        runtime.profiles.update(new_profiles)
        return True

    async def _handle_admin_profile_tools_update(self, request: Request) -> JSONResponse:
        """PUT /v1/admin/profiles/{profile_id}/tools：在线编辑工具策略并写回 + 热更新。"""
        if not self._check_api_key(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        runtime = self._controller._runtime
        config_dir = getattr(runtime, "config_dir", None)
        if config_dir is None:
            return JSONResponse({"error": "config dir unavailable"}, status_code=503)
        profile_id = request.path_params["profile_id"]
        try:
            body = AdminProfileToolsUpdateRequest(**await request.json())
        except Exception as exc:
            return JSONResponse({"error": f"invalid request: {exc}"}, status_code=422)
        try:
            profile = update_profile_tools(config_dir, profile_id, body.tools)
        except ProfileConfigError as exc:
            return JSONResponse(
                {"error": "invalid_profile", "message": str(exc)}, status_code=400
            )
        reloaded = await self._reload_runtime_profiles()
        await self._audit_admin_operation(
            request,
            "update_profile_tools",
            target=f"profile:{profile_id}",
            metadata={"tool_count": len(body.tools), "reloaded": reloaded},
        )
        return JSONResponse(
            AdminProfileUpdateResponse(
                profile=profile.model_dump(mode="json"),
                reloaded=reloaded,
            ).model_dump(mode="json")
        )

    async def _handle_admin_profiles_reload(self, request: Request) -> JSONResponse:
        """POST /v1/admin/profiles/reload：放弃内存修改，从磁盘统一重载全部 Profile。"""
        if not self._check_api_key(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        reloaded = await self._reload_runtime_profiles()
        if not reloaded:
            return JSONResponse({"error": "reload failed"}, status_code=500)
        await self._audit_admin_operation(
            request,
            "reload_profiles",
            target="profiles",
            metadata={"count": len(self._controller._runtime.profiles)},
        )
        profiles = self._controller._runtime.profiles
        return JSONResponse(
            AdminProfilesResponse(
                profiles=[p.model_dump(mode="json") for p in profiles.values()]
            ).model_dump(mode="json")
        )

    def _go_kernel_view(self) -> tuple[Any, dict[str, Any]]:
        """提取 (GoKernelBridge|None, go_kernel 配置段)。"""
        runtime = self._controller._runtime
        bridge = getattr(runtime, "go_kernel_bridge", None)
        gk: dict[str, Any] = {}
        config = getattr(runtime, "config", None)
        if config is not None:
            raw = getattr(config, "go_kernel_config", None) or {}
            gk = raw.get("go_kernel", {}) or {}
        return bridge, gk

    async def _handle_admin_a2a_status(self, request: Request) -> JSONResponse:
        """GET /v1/admin/a2a/status：Go 内核连接状态。"""
        if not self._check_api_key(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        bridge, gk = self._go_kernel_view()
        reachable = await bridge.ping() if bridge is not None else False
        return JSONResponse(
            AdminA2AStatusResponse(
                enabled=bool(gk.get("enabled", False)),
                reachable=reachable,
                base_url=str(gk.get("base_url", "")),
                local_agent=dict(gk.get("local_agent", {}) or {}),
            ).model_dump(mode="json")
        )

    async def _handle_admin_a2a_agents(self, request: Request) -> JSONResponse:
        """GET /v1/admin/a2a/agents：配置 Agent 与内核注册态合并视图。"""
        if not self._check_api_key(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        runtime = self._controller._runtime
        config = getattr(runtime, "config", None)
        if config is None:
            return JSONResponse({"error": "config unavailable"}, status_code=503)
        bridge, _gk = self._go_kernel_view()
        registered_ids: set[str] | None = None
        if bridge is not None and await bridge.ping():
            cards = await bridge.list_agents()
            registered_ids = {str(card.get("agent_id", "")) for card in cards}
        items = [
            AdminA2AAgentItem(
                agent_id=agent.agent_id,
                name=agent.name,
                profile_id=agent.profile_id,
                owner_name=config.users.get(agent.owner_id),
                registered=(
                    agent.agent_id in registered_ids if registered_ids is not None else None
                ),
            )
            for agent in config.agents.values()
        ]
        return JSONResponse(
            {
                "agents": [item.model_dump(mode="json") for item in items],
                "kernel_reachable": registered_ids is not None,
            }
        )

    async def _handle_admin_a2a_task_query(self, request: Request) -> JSONResponse:
        """GET /v1/admin/a2a/tasks/{task_id}：经 Go 内核查询任务状态。"""
        if not self._check_api_key(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        bridge, _gk = self._go_kernel_view()
        if bridge is None:
            return JSONResponse({"error": "go kernel disabled"}, status_code=503)
        task = await bridge.query_task(request.path_params["task_id"])
        if task is None:
            return JSONResponse({"error": "task not found"}, status_code=404)
        return JSONResponse(task)

    async def _handle_admin_a2a_task_cancel(self, request: Request) -> JSONResponse:
        """POST /v1/admin/a2a/tasks/{task_id}/cancel：管理端取消委托任务。"""
        if not self._check_api_key(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        bridge, _gk = self._go_kernel_view()
        if bridge is None:
            return JSONResponse({"error": "go kernel disabled"}, status_code=503)
        reason = ""
        try:
            body = await request.json()
            if isinstance(body, dict):
                reason = str(body.get("reason") or "")
        except Exception:
            reason = ""
        task = await bridge.cancel_task(request.path_params["task_id"], reason=reason)
        if task is None:
            return JSONResponse(
                {"error": "cancel failed or task not found"}, status_code=502
            )
        return JSONResponse(task)

    async def _handle_admin_a2a_task_stream(self, request: Request) -> StreamingResponse:
        """GET /v1/admin/a2a/tasks/{task_id}/stream：SSE 转发任务状态流。"""
        if not self._check_api_key(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)  # type: ignore[return-value]
        bridge, _gk = self._go_kernel_view()
        if bridge is None:
            return JSONResponse({"error": "go kernel disabled"}, status_code=503)  # type: ignore[return-value]
        task_id = request.path_params["task_id"]

        async def events() -> AsyncIterator[str]:
            async for event in bridge.stream_task(task_id):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    async def _handle_admin_a2a_delegation(self, request: Request) -> JSONResponse:
        """POST /v1/admin/a2a/delegations：管理端发起委托（与 Agent 自发委托同治理路径）。"""
        if not self._check_api_key(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = AdminDelegationRequest(**await request.json())
        except Exception as exc:
            return JSONResponse({"error": f"invalid request: {exc}"}, status_code=422)
        runtime = self._controller._runtime
        config = getattr(runtime, "config", None)
        if config is None:
            return JSONResponse({"error": "config unavailable"}, status_code=503)
        if body.source_agent_id not in config.agents:
            return JSONResponse({"error": "unknown source agent"}, status_code=400)
        # target 可以是外部 Agent（仅注册在 Go 内核）；其合法性由治理引擎
        # 通过 Agent Card 查询与信任校验兜底。

        proposal = InteractionProposal(
            interaction_id=uuid.uuid4().hex,
            request_id=uuid.uuid4().hex,
            session_id=body.session_id,
            task_id=body.task_id,
            source_agent_id=body.source_agent_id,
            target_agent_id=body.target_agent_id,
            tool_name=body.tool_name,
            arguments=body.arguments,
            risk_level=body.risk_level,
            parent_allow_redelegation=body.allow_redelegation,
            interaction_context="admin-console",
        )
        engine = self.interaction_engine
        decision = await engine.evaluate(proposal)

        # 交互审计：与 Agent 自发委托同一审计语义
        audit_store = getattr(runtime, "audit_store", None)
        if audit_store is not None:
            await audit_store.append_async(engine.build_audit_event(proposal, decision))

        # require_approval：自动生成审批单挂入审批台，批准后自动派发
        approval = None
        if decision.verdict == "require_approval":
            approval = await self._submit_delegation_approval(
                body, decision, proposal, self._admin_actor_id(request)
            )

        dispatch = AdminDelegationDispatch()
        if decision.allowed:
            bridge, _gk = self._go_kernel_view()
            if bridge is None:
                dispatch = AdminDelegationDispatch(
                    attempted=False, accepted=False, reason="go kernel disabled"
                )
            else:
                delegation = await bridge.request_delegation(
                    DelegationRequest(
                        request_id=decision.decision_id,
                        initiator_agent_id=body.source_agent_id,
                        target_agent_id=body.target_agent_id,
                        tool_name=body.tool_name,
                        arguments=decision.effective_args or body.arguments,
                        session_id=body.session_id,
                        task_id=body.task_id,
                        risk_level=body.risk_level,
                        allow_redelegation=body.allow_redelegation,
                        parent_interaction_id=decision.interaction_id,
                    )
                )
                dispatch = AdminDelegationDispatch(
                    attempted=True,
                    accepted=delegation.allowed,
                    task_id=delegation.task_id or "",
                    reason="" if delegation.allowed else delegation.reason,
                )
        return JSONResponse(
            AdminDelegationResponse(
                verdict=decision.verdict,
                allowed=decision.allowed,
                reason=decision.reason,
                decision_id=decision.decision_id,
                interaction_id=decision.interaction_id,
                escalation_target=decision.escalation_target,
                target_entrypoint=decision.target_entrypoint,
                modified_args=decision.modified_args,
                dispatch=dispatch,
                approval=approval,
            ).model_dump(mode="json")
        )

    async def _submit_delegation_approval(
        self,
        body: Any,
        decision: Any,
        proposal: Any,
        actor_id: str,
    ) -> AdminDelegationApproval | None:
        """把 require_approval 的委托转换为审批单提交到审批台。

        审批单以 ``call_id=a2a-delegation:{interaction_id}`` 标记，批准后由
        审批接口自动派发；审批人缺失（策略未指定升级对象）时不提交。
        """
        approver_id = (getattr(decision, "escalation_target", "") or "").strip()
        if not approver_id:
            logger.warning(
                "delegation require_approval without escalation target: %s",
                getattr(decision, "decision_id", ""),
            )
            return None
        runtime = self._controller._runtime
        try:
            masked = runtime.masker.mask(proposal.arguments, "approval_request")
        except Exception:  # noqa: BLE001
            masked = proposal.arguments
        request = ApprovalRequest(
            request_id=f"a2a-req-{decision.interaction_id}",
            decision_id=decision.decision_id,
            call_id=f"a2a-delegation:{decision.interaction_id}",
            task_id=proposal.task_id or decision.interaction_id,
            agent_id=proposal.source_agent_id,
            tool_name=proposal.tool_name,
            arguments_masked=masked,
            tool_arguments={
                "source_agent_id": body.source_agent_id,
                "target_agent_id": body.target_agent_id,
                "tool_name": body.tool_name,
                "arguments": proposal.arguments,
                "risk_level": body.risk_level,
                "session_id": body.session_id,
                "task_id": body.task_id,
                "allow_redelegation": body.allow_redelegation,
            },
            original_decision=None,
            reason=(
                f"A2A 委托需审批：{body.source_agent_id} → {body.target_agent_id}"
                f" / {body.tool_name}（{decision.reason}）"
            ),
            requester_id=actor_id,
            approver_id=approver_id,
        )
        try:
            await runtime.approval_manager.submit(request)
        except Exception as exc:  # noqa: BLE001
            logger.warning("submit delegation approval failed: %s", exc)
            return None
        return AdminDelegationApproval(
            request_id=request.request_id,
            decision_id=decision.decision_id,
            approver_id=approver_id,
        )

    async def _handle_admin_session_login(self, request: Request) -> JSONResponse:
        """POST /v1/admin/session/login：验证 Admin API Key，签发 Session Token。"""
        try:
            body = AdminSessionLoginRequest(**await request.json())
        except Exception as exc:
            return JSONResponse({"error": f"invalid request: {exc}"}, status_code=422)
        if not self._api_key or not hmac.compare_digest(body.api_key, self._api_key):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        session = self._session_store.create()
        await self._audit_admin_operation(
            request,
            "session_login",
            target="admin_session",
            metadata={"expires_at": datetime.fromtimestamp(session.expires_at, UTC)},
        )
        return JSONResponse(
            AdminSessionLoginResponse(
                token=session.token,
                expires_at=datetime.fromtimestamp(session.expires_at, UTC),
            ).model_dump(mode="json")
        )

    async def _handle_admin_session_logout(self, request: Request) -> JSONResponse:
        """POST /v1/admin/session/logout：吊销当前 Bearer Session Token。"""
        token = self._bearer_token(request)
        revoked = self._session_store.revoke(token) if token else False
        await self._audit_admin_operation(
            request,
            "session_logout",
            target="admin_session",
            metadata={"revoked": revoked},
        )
        return JSONResponse({"revoked": revoked})

    async def _handle_admin_identity_config(self, request: Request) -> JSONResponse:
        """GET /v1/admin/identity：返回脱敏后的 Identity Provider 配置。"""
        if not self._check_api_key(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        config = getattr(self._controller._runtime, "config", None)
        if config is None:
            return JSONResponse({"error": "config unavailable"}, status_code=503)
        identity_config = config.identity_config
        return JSONResponse(
            {
                "provider": identity_config.get("provider", "static"),
                "config": _mask_sensitive(identity_config),
            }
        )

    async def _handle_admin_entrypoints(self, request: Request) -> JSONResponse:
        """GET /v1/admin/entrypoints：返回脱敏后的入口认证配置（含顶层 entrypoints 键）。"""
        if not self._check_api_key(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        config = getattr(self._controller._runtime, "config", None)
        entrypoints = config.entrypoints_config if config is not None else self._entrypoints_config
        return JSONResponse(_mask_sensitive(entrypoints))

    async def _handle_admin_govern_evaluate(self, request: Request) -> JSONResponse:
        """POST /v1/admin/govern/evaluate：只读判定调试（不执行、不提交审批）。

        为复用真实判定链路，本接口直接调用 ``Checkpoint.evaluate``，存在
        可控副作用：防重放记录 call_id、按随机 task_id 预留预算（可回收）、
        deny 时更新随机 session 的风险状态（不累积）。合成 ID 统一带
        ``dryrun-`` 前缀，便于审计识别与清理。
        """
        if not self._check_api_key(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        runtime = self._controller._runtime
        config = getattr(runtime, "config", None)
        if config is None:
            return JSONResponse({"error": "config unavailable"}, status_code=503)
        try:
            body = AdminGovernEvaluateRequest(**await request.json())
        except Exception as exc:
            logger.warning("invalid govern evaluate request: %s", exc)
            return JSONResponse({"error": f"invalid request: {exc}"}, status_code=422)

        agent = config.agents.get(body.agent_id)
        if agent is None:
            return JSONResponse({"error": f"unknown agent: {body.agent_id}"}, status_code=404)
        profile = runtime.profiles.get(agent.profile_id)
        if profile is None:
            return JSONResponse({"error": f"unknown profile: {agent.profile_id}"}, status_code=404)

        task = Task(
            task_id=f"dryrun-{uuid.uuid4().hex[:12]}",
            session_id=f"dryrun-{uuid.uuid4().hex[:12]}",
            user_id=body.user_id,
            agent_id=body.agent_id,
            description=body.task_context or "admin dry-run evaluate",
        )
        proposal = ActionProposal(
            task_id=task.task_id,
            call_id=f"dryrun-{uuid.uuid4().hex}",
            agent_id=body.agent_id,
            tool_name=body.tool_name,
            arguments=body.arguments,
            task_context=body.task_context,
        )

        # 吊销前置拦截（与 LoopController.evaluate 一致）
        identity = AgentIdentity(
            agent_id=agent.agent_id,
            user_id=body.user_id,
            profile_id=agent.profile_id,
        )
        match = runtime.checkpoint.check_revocation(identity, body.tool_name, body.arguments)
        if match.revoked:
            return JSONResponse(
                AdminGovernEvaluateResponse(
                    verdict="blocked", reason=match.reason or "revoked"
                ).model_dump(mode="json")
            )

        # R1 轻量分类（与 LoopController._evaluate_proposal 一致）
        signal = runtime.classifier.classify(task, agent, proposal, profile)
        if body.tool_name in runtime.http_tool_names:
            signal = LoopController._bump_risk_signal(signal)
        proposal = proposal.model_copy(
            update={"risk_level": signal.risk_level, "risk_tags": signal.tags}
        )

        try:
            decision = await runtime.checkpoint.evaluate(task, agent, proposal)
        except CheckpointError as exc:
            return JSONResponse(
                AdminGovernEvaluateResponse(verdict="deny", reason=str(exc)).model_dump(mode="json")
            )

        return JSONResponse(
            AdminGovernEvaluateResponse(
                verdict=decision.verdict,
                reason=decision.reason,
                policy_hits=decision.policy_hits,
                risk_level=signal.risk_level,
                risk_tags=signal.tags,
                policy_version=decision.policy_version,
                profile_version=decision.profile_version,
            ).model_dump(mode="json")
        )

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
    session_store: AdminSessionStore | None = None,
    interaction_engine: InteractionGovernanceEngine | None = None,
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
        session_store=session_store,
        interaction_engine=interaction_engine,
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
                "/interaction/v1/delegations/lifecycle",
                server._handle_interaction_lifecycle,
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
            Route("/v1/admin/approvals", server._handle_admin_approvals, methods=["GET"]),
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
            Route("/v1/admin/agents", server._handle_admin_agents, methods=["GET"]),
            Route(
                "/v1/admin/agents/{agent_id}",
                server._handle_admin_agent_detail,
                methods=["GET"],
            ),
            Route("/v1/admin/profiles", server._handle_admin_profiles, methods=["GET"]),
            Route(
                "/v1/admin/profiles/reload",
                server._handle_admin_profiles_reload,
                methods=["POST"],
            ),
            Route(
                "/v1/admin/profiles/{profile_id}/tools",
                server._handle_admin_profile_tools_update,
                methods=["PUT"],
            ),
            Route(
                "/v1/admin/session/login",
                server._handle_admin_session_login,
                methods=["POST"],
            ),
            Route(
                "/v1/admin/session/logout",
                server._handle_admin_session_logout,
                methods=["POST"],
            ),
            Route("/v1/admin/a2a/status", server._handle_admin_a2a_status, methods=["GET"]),
            Route("/v1/admin/a2a/agents", server._handle_admin_a2a_agents, methods=["GET"]),
            Route(
                "/v1/admin/a2a/kernel-approvals",
                server._handle_admin_kernel_approvals,
                methods=["GET"],
            ),
            Route(
                "/v1/admin/a2a/tasks/{task_id}",
                server._handle_admin_a2a_task_query,
                methods=["GET"],
            ),
            Route(
                "/v1/admin/a2a/tasks/{task_id}/cancel",
                server._handle_admin_a2a_task_cancel,
                methods=["POST"],
            ),
            Route(
                "/v1/admin/a2a/tasks/{task_id}/stream",
                server._handle_admin_a2a_task_stream,
                methods=["GET"],
            ),
            Route(
                "/v1/admin/a2a/delegations",
                server._handle_admin_a2a_delegation,
                methods=["POST"],
            ),
            Route("/v1/admin/identity", server._handle_admin_identity_config, methods=["GET"]),
            Route("/v1/admin/entrypoints", server._handle_admin_entrypoints, methods=["GET"]),
            Route(
                "/v1/admin/govern/evaluate",
                server._handle_admin_govern_evaluate,
                methods=["POST"],
            ),
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
