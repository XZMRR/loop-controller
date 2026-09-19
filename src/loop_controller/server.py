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

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import httpx

from loop_controller.approval_service import (
    ApprovalAuthenticationError,
    ApprovalAuthorizationError,
    ApprovalServiceError,
    build_approval_record,
    resolve_approver_principal,
)
from loop_controller.approval_watcher import ApprovalWatcher
from loop_controller.checkpoint import CheckpointError
from loop_controller.controller import LoopController
from loop_controller.execution_security import (
    REQUIRED_SECURITY_CAPABILITY_UNAVAILABLE,
    ExecutionRequestContext,
    ExecutionSecurityPolicy,
    TLSWorkloadIdentityResolver,
    WorkloadIdentityResolver,
    canonical_arguments_sha256,
    current_execution_request,
    resolve_workload_identity,
    supports_security_capabilities,
)
from loop_controller.go_kernel_bridge import (
    DelegationRequest,
    EventCursorExpiredError,
    EventCursorInvalidError,
)
from loop_controller.identity import (
    AgentIdentity,
    IdentityCredential,
    IdentityProvider,
    KillSwitchConfig,
    RevocationType,
)
from loop_controller.infra.admin_session import AdminSessionStore
from loop_controller.infra.approval_store import ApprovalStoreError
from loop_controller.infra.config_loader import ConfigLoader
from loop_controller.infra.policy_delivery import (
    ArtifactConflictError,
    CandidateLimitError,
    CandidateStateError,
    InvalidCandidateError,
    PolicyCASConflictError,
    SeparationOfDutiesError,
)
from loop_controller.infra.profile_config import ProfileConfigError, update_profile_tools
from loop_controller.infra.state_db import StateDatabaseError
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
from loop_controller.models import ActionProposal, AuditEvent, Task
from loop_controller.policy_lifecycle import PolicyStatusError, PolicyStatusForbiddenError
from loop_controller.policy_shadow import PolicyShadowService
from loop_controller.rbac import (
    AuthenticatedPrincipal,
    RbacDenial,
    RbacEnforcer,
    Role,
    StaticCredentialResolver,
)
from loop_controller.rbac.models import (
    PERM_APPROVAL_DECIDE,
    PERM_AUDIT_READ,
    PERM_CANDIDATE_CREATE,
    PERM_CANDIDATE_READ,
    PERM_PUBLISH,
    PERM_RBAC_MANAGE,
    PERM_ROLLBACK,
    PERM_SHADOW_RUN,
    PERM_STATUS_READ,
    PERM_VALIDATE_RUN,
    RESOURCE_PUBLISH_GLOBAL,
)
from loop_controller.secrets import (
    EncryptedFileSecretBackend,
    FileSecretBackend,
    MemorySecretBackend,
)
from loop_controller.server_models import (
    AdminA2AAgentItem,
    AdminA2AStatusResponse,
    AdminAgentDetail,
    AdminAgentItem,
    AdminAgentsResponse,
    AdminApprovalsResponse,
    AdminDelegationDispatch,
    AdminDelegationRequest,
    AdminDelegationResponse,
    AdminGovernEvaluateRequest,
    AdminGovernEvaluateResponse,
    AdminProfilesResponse,
    AdminProfileToolsUpdateRequest,
    AdminProfileUpdateResponse,
    AdminSecretItem,
    AdminSecretsResponse,
    AdminSessionLoginRequest,
    AdminSessionLoginResponse,
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

_ISO8601_AWARE_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


def _parse_audit_time(
    value: str | None, name: str
) -> tuple[datetime | None, JSONResponse | None]:
    if value is None:
        return None, None
    if len(value) > 64 or not value or _ISO8601_AWARE_RE.fullmatch(value) is None:
        return None, JSONResponse(
            {"error": "invalid_parameter", "message": f"invalid {name}"},
            status_code=400,
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None, JSONResponse(
            {"error": "invalid_parameter", "message": f"invalid {name}"},
            status_code=400,
        )
    return parsed, None


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
        execution_security: ExecutionSecurityPolicy | None = None,
        workload_identity_resolver: WorkloadIdentityResolver | None = None,
        delegation_token_verifier: Any = None,
        session_store: AdminSessionStore | None = None,
        interaction_engine: InteractionGovernanceEngine | None = None,
    ) -> None:
        self._controller = controller
        self._api_key = api_key
        self._watcher = watcher or ApprovalWatcher()
        self._start_time = start_time or time.time()
        self._identity_provider = identity_provider or _extract_identity_provider(controller)
        self._entrypoints_config = entrypoints_config or {}
        runtime_policy = getattr(
            controller._runtime, "execution_security_policy", None
        )
        self._execution_security = (
            execution_security or runtime_policy or ExecutionSecurityPolicy()
        )
        self._workload_identity_resolver = workload_identity_resolver or TLSWorkloadIdentityResolver(
            self._execution_security.workload_registry
        )
        self._delegation_token_verifier = delegation_token_verifier or getattr(
            controller._runtime, "delegation_token_verifier", None
        )
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

    @staticmethod
    def _check_bearer(request: Request, expected: str | None) -> bool:
        if not expected:
            return False
        auth = request.headers.get("authorization") or ""
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        return bool(token and len(token) == len(expected) and hmac.compare_digest(token, expected))

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

    # ------------------------------------------------------------------
    # v0.52 统一访问控制平面（认证 + 授权）
    # ------------------------------------------------------------------

    def _rbac_enforcer(self) -> RbacEnforcer | None:
        return getattr(self._controller._runtime, "rbac_enforcer", None)

    def _rbac_store(self) -> Any:
        return getattr(self._controller._runtime, "rbac_store", None)

    async def _authenticate(self, request: Request) -> AuthenticatedPrincipal | None:
        """凭证层解析（fail-closed）：静态角色凭证 → JWT → legacy api key 兼容层。

        凭证即身份：token 不落盘，轮换 = 替换 env 值，旧 token 即时失效。
        """
        runtime = self._controller._runtime
        resolver: StaticCredentialResolver | None = getattr(
            runtime, "rbac_credential_resolver", None
        )
        token = self._bearer_token(request)
        if resolver is not None:
            principal = resolver.resolve(request.headers.get("x-lc-principal"), token)
            if principal is not None:
                return principal
        provider: IdentityProvider | None = getattr(self, "_identity_provider", None)
        if provider is not None and token:
            identity = await provider.verify(IdentityCredential(token=token))
            if identity is not None:
                roles: tuple[str, ...] = ()
                rbac_cfg = getattr(getattr(runtime, "config", None), "rbac", None)
                if getattr(rbac_cfg, "dynamic_role_bindings", False):
                    role_map = getattr(rbac_cfg, "role_map", {}) or {}
                    mapped = {role_map[name] for name in identity.roles if name in role_map}
                    roles = tuple(sorted(mapped))
                return AuthenticatedPrincipal(
                    principal_id=identity.user_id or identity.agent_id,
                    tenant_id=identity.tenant_id,
                    roles=roles,
                    auth_method="jwt",
                )
        if self._rbac_enforcer() is not None:
            return None
        if self._check_api_key(request):
            rbac_cfg = getattr(getattr(runtime, "config", None), "rbac", None)
            if getattr(rbac_cfg, "legacy_key_role", "platform_admin") == "reject":
                return None
            return AuthenticatedPrincipal(
                principal_id=self._admin_actor_id(request),
                tenant_id=None,
                roles=(Role.PLATFORM_ADMIN.value,),
                auth_method="legacy-key",
            )
        return None

    def _record_denial(
        self,
        request: Request,
        principal: AuthenticatedPrincipal,
        permission: str,
        reason: str,
    ) -> None:
        """每次 403 写 rbac_denials（无自动清理，由部署方归档）。"""
        from datetime import UTC, datetime

        store = self._rbac_store()
        if store is None:
            return
        try:
            store.record_denial(
                RbacDenial(
                    denial_id=uuid.uuid4().hex,
                    actor=principal.principal_id,
                    endpoint=request.url.path,
                    required_permission=permission,
                    principal_tenant=principal.tenant_id,
                    reason=reason[:512],
                    created_at=datetime.now(UTC).isoformat(),
                )
            )
        except Exception as exc:  # noqa: BLE001 - 拒绝事件失败不阻断 403
            logger.warning("记录 RBAC 拒绝事件失败: %s", exc)

    async def _require(
        self,
        request: Request,
        permission: str,
        tenant_id: str | None = None,
    ) -> tuple[AuthenticatedPrincipal | None, JSONResponse | None]:
        """认证 + 授权一步式入口。

        enforcement 关闭（runtime.rbac_enforcer 为 None）时维持 legacy api key 行为，
        零行为变化；开启时未绑定角色一律 403 并写拒绝审计。
        """
        enforcer = self._rbac_enforcer()
        if enforcer is None:
            if not self._check_api_key(request):
                return None, JSONResponse({"error": "unauthorized"}, status_code=401)
            return (
                AuthenticatedPrincipal(
                    principal_id=self._admin_actor_id(request),
                    tenant_id=None,
                    roles=(Role.PLATFORM_ADMIN.value,),
                    auth_method="legacy-key",
                ),
                None,
            )
        principal = await self._authenticate(request)
        if principal is None:
            return None, JSONResponse({"error": "unauthorized"}, status_code=401)
        decision = enforcer.authorize(principal, permission, tenant_id)
        if not decision.allowed:
            self._record_denial(request, principal, permission, decision.reason)
            return None, JSONResponse(
                {"error": "forbidden", "reason": decision.reason}, status_code=403
            )
        return principal, None

    def _is_platform_admin(self, principal: AuthenticatedPrincipal) -> bool:
        enforcer = self._rbac_enforcer()
        if enforcer is None:
            return True
        return Role.PLATFORM_ADMIN in enforcer.roles_for(principal)

    async def _authenticate_only(
        self, request: Request
    ) -> tuple[AuthenticatedPrincipal | None, JSONResponse | None]:
        """仅认证（enforcement 关闭时维持 legacy api key 行为，零行为变化）。"""
        enforcer = self._rbac_enforcer()
        if enforcer is None:
            if not self._check_api_key(request):
                return None, JSONResponse({"error": "unauthorized"}, status_code=401)
            return (
                AuthenticatedPrincipal(
                    principal_id=self._admin_actor_id(request),
                    tenant_id=None,
                    roles=(Role.PLATFORM_ADMIN.value,),
                    auth_method="legacy-key",
                ),
                None,
            )
        principal = await self._authenticate(request)
        if principal is None:
            return None, JSONResponse({"error": "unauthorized"}, status_code=401)
        return principal, None

    def _authorize(
        self,
        request: Request,
        principal: AuthenticatedPrincipal,
        permission: str,
        tenant_id: str | None,
    ) -> JSONResponse | None:
        """资源租户已知的二次授权；返回 None 表示放行。"""
        enforcer = self._rbac_enforcer()
        if enforcer is None:
            return None
        decision = enforcer.authorize(principal, permission, tenant_id)
        if decision.allowed:
            return None
        self._record_denial(request, principal, permission, decision.reason)
        return JSONResponse({"error": "forbidden", "reason": decision.reason}, status_code=403)

    async def _require_self(
        self, request: Request, permission: str
    ) -> tuple[AuthenticatedPrincipal | None, JSONResponse | None]:
        """仅按角色集合鉴权（数据域由 handler 按 principal.tenant_id 过滤）。"""
        principal, error = await self._authenticate_only(request)
        if error is not None:
            return None, error
        assert principal is not None
        error = self._authorize(request, principal, permission, principal.tenant_id)
        if error is not None:
            return None, error
        return principal, None

    async def _audit_admin_operation(
        self,
        request: Request,
        operation: str,
        *,
        target: str,
        metadata: dict[str, Any] | None = None,
        actor_id: str | None = None,
    ) -> None:
        audit_store = getattr(self._controller._runtime, "audit_store", None)
        if audit_store is None:
            return
        actor_id = actor_id or self._admin_actor_id(request)
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
        degraded_backends = list(
            getattr(self._controller._runtime, "degraded_backends", ())
        )
        lifecycle = getattr(self._controller._runtime, "policy_lifecycle", None)
        policy_status = lifecycle.status() if lifecycle is not None else {}
        policy_degraded = bool(
            lifecycle is not None
            and (
                not opa_reachable
                or not policy_status.get("expected_revision")
                or policy_status.get("active_revision") != policy_status.get("expected_revision")
                or policy_status.get("stale_instances")
                or policy_status.get("error_instances")
            )
        )
        security_status = getattr(self._controller._runtime, "security_status", None)
        execution_security = (
            security_status.as_dict()
            if security_status is not None
            else {"status": "not_strict", "mode": "compatibility", "runtime_assurance": "unknown"}
        )
        index_status = getattr(audit_store, "index_status", None)
        if index_status is not None:
            execution_security["audit_correlation"] = {
                "status": "ready" if index_status.healthy else "degraded",
                "reason": None if index_status.healthy else "audit_index_degraded",
            }
            if execution_security.get("mode") == "strict" and not index_status.healthy:
                execution_security["status"] = "degraded"
                reasons = list(execution_security.get("reasons", []))
                if "audit_index_degraded" not in reasons:
                    reasons.append("audit_index_degraded")
                execution_security["reasons"] = reasons
        degraded = (
            evidence_status == "degraded"
            or anchor_summary["anchor_status"] not in {"disabled", "healthy"}
            or persistence_status != "healthy"
            or harness_degraded
            or bool(degraded_backends)
            or policy_degraded
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
                degraded_backends=degraded_backends,
                policy=policy_status,
                execution_security=execution_security,
                **anchor_summary,
            ).model_dump()
        )

    async def _handle_ready(self, request: Request) -> JSONResponse:
        response = await self._handle_health(request)
        body = bytes(response.body)
        import json
        payload = json.loads(body)
        security = payload["execution_security"]
        if security.get("mode") == "strict":
            runtime = self._controller._runtime
            harness = getattr(runtime, "harness_executor", None)
            harness_ready = True
            if "remote_harness" in security.get("enabled_egress_types", []):
                harness_ready = harness is not None and all(
                    item.status == "healthy" and not item.draining
                    for item in harness.backend_statuses()
                )
            ready = security.get("status") == "ready" and harness_ready
        else:
            ready = True
        return JSONResponse(payload, status_code=200 if ready else 503)

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

    def _verify_delegation_token(
        self, token: str | None, payload: dict[str, Any], *, lifecycle: bool = False
    ) -> str | None:
        if not token or self._delegation_token_verifier is None:
            return "delegation_token_required"
        try:
            claims = self._delegation_token_verifier.verify(token)
        except Exception:
            return "delegation_token_invalid"
        bindings = {
            "request_id": payload.get("request_id"),
            "interaction_id": payload.get("interaction_id"),
            "decision_id": payload.get("decision_id"),
            "task_id": payload.get("task_id"),
            "jti": payload.get("delegation_jti"),
            "tenant_id": payload.get("tenant_id"),
            "target_agent_id": payload.get("target_agent_id") or payload.get("agent_id"),
            "target_workload_id": payload.get("target_workload_id"),
            "target_instance_id": payload.get("target_instance_id"),
        }
        for name, expected in bindings.items():
            if expected is not None and claims.get(name) != expected:
                return "delegation_token_binding_mismatch"
        if claims.get("aud") != bindings["target_agent_id"]:
            return "delegation_token_binding_mismatch"
        if not lifecycle:
            if claims.get("tool_name") != payload.get("tool_name"):
                return "delegation_token_binding_mismatch"
            if claims.get("arguments_sha256") != canonical_arguments_sha256(
                payload.get("arguments") or {}
            ):
                return "delegation_token_binding_mismatch"
            if claims.get("allowed_tools") != payload.get("allowed_tools") or claims.get(
                "allowed_capabilities"
            ) != payload.get("allowed_capabilities"):
                return "delegation_token_binding_mismatch"
            if bool(claims.get("allow_redelegation")) != bool(
                payload.get("allow_redelegation")
            ):
                return "delegation_token_binding_mismatch"
        return None

    async def _handle_interaction_lifecycle(self, request: Request) -> JSONResponse:
        """接收 Go Kernel 的 Interaction/Task 生命周期审计通知。"""
        workload = await resolve_workload_identity(self._workload_identity_resolver, request)
        if self._execution_security.strict and workload is None:
            return JSONResponse({"error": "workload_identity_required"}, status_code=401)
        if not self._execution_security.strict:
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
            if self._execution_security.strict:
                strict_required = ("request_id", "tenant_id", "target_workload_id", "required_security_capabilities")
                strict_missing = [field for field in strict_required if not payload.get(field)]
                if strict_missing or workload is None:
                    raise ValueError(REQUIRED_SECURITY_CAPABILITY_UNAVAILABLE)
                if not supports_security_capabilities(
                    payload["required_security_capabilities"],
                    self._execution_security.supported_security_capabilities,
                ):
                    raise ValueError(REQUIRED_SECURITY_CAPABILITY_UNAVAILABLE)
                target_instance_id = payload.get("target_instance_id")
                if self._execution_security.require_instance_identity and (
                    not workload.authenticated_instance_id
                    or not target_instance_id
                    or workload.authenticated_instance_id != target_instance_id
                ):
                    return JSONResponse({"error": "workload_scope_mismatch"}, status_code=403)
                if payload["target_workload_id"] != workload.workload_id or not self._execution_security.workload_registry.authorize(
                    workload,
                    agent_id=payload["target_agent_id"],
                    tenant_id=payload["tenant_id"],
                    target_instance_id=(target_instance_id if self._execution_security.require_instance_identity else None),
                    lifecycle=True,
                ):
                    return JSONResponse({"error": "workload_scope_mismatch"}, status_code=403)
                token_error = self._verify_delegation_token(
                    payload.get("delegation_token"), payload, lifecycle=True
                )
                if token_error:
                    return JSONResponse({"error": token_error}, status_code=403)
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
        workload = await resolve_workload_identity(self._workload_identity_resolver, request)
        if self._execution_security.strict:
            if workload is None:
                return JSONResponse({"error": "workload_identity_required"}, status_code=401)
            identity = None
        else:
            authorized, identity = await self._check_agent_auth(request)
            if not authorized:
                return JSONResponse({"error": "unauthorized"}, status_code=401)

        try:
            body = GovernToolRequest(**await request.json())
        except Exception as exc:
            logger.warning("invalid tool-call request: %s", exc)
            return JSONResponse({"error": f"invalid request: {exc}"}, status_code=422)

        if self._execution_security.strict:
            required_fields = (
                "request_id", "decision_id", "task_id", "call_id", "tenant_id",
                "target_workload_id", "delegation_jti", "required_security_capabilities",
            )
            if any(not getattr(body, field) for field in required_fields):
                return JSONResponse(
                    {"status": "blocked", "result": REQUIRED_SECURITY_CAPABILITY_UNAVAILABLE,
                     "error_code": REQUIRED_SECURITY_CAPABILITY_UNAVAILABLE,
                     "supported_security_capabilities": sorted(self._execution_security.supported_security_capabilities)},
                    status_code=503,
                )
            if not supports_security_capabilities(
                body.required_security_capabilities or (),
                self._execution_security.supported_security_capabilities,
            ):
                return JSONResponse(
                    {"status": "blocked", "result": REQUIRED_SECURITY_CAPABILITY_UNAVAILABLE,
                     "error_code": REQUIRED_SECURITY_CAPABILITY_UNAVAILABLE,
                     "supported_security_capabilities": sorted(self._execution_security.supported_security_capabilities)},
                    status_code=503,
                )
            assert workload is not None and body.tenant_id is not None
            if self._execution_security.require_instance_identity and (
                not workload.authenticated_instance_id
                or not body.target_instance_id
                or workload.authenticated_instance_id != body.target_instance_id
            ):
                return JSONResponse({"error": "workload_scope_mismatch"}, status_code=403)
            if body.target_workload_id != workload.workload_id or not self._execution_security.workload_registry.authorize(
                workload,
                agent_id=body.agent_id,
                tenant_id=body.tenant_id,
                target_instance_id=(body.target_instance_id if self._execution_security.require_instance_identity else None),
            ):
                return JSONResponse({"error": "workload_scope_mismatch"}, status_code=403)
            token_error = self._verify_delegation_token(
                body.delegation_token, body.model_dump(mode="json")
            )
            if token_error:
                return JSONResponse({"error": token_error}, status_code=403)

        # 当身份 Provider 可用时，使用凭证中的 agent_id/user_id，请求体只做一致性校验。
        user_id: str | None
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
        context_token = None
        if self._execution_security.strict:
            assert workload is not None
            context_token = current_execution_request.set(
                ExecutionRequestContext(
                    workload_identity=workload,
                    request_id=body.request_id or "",
                    interaction_id=body.interaction_id,
                    decision_id=body.decision_id or "",
                    task_id=body.task_id or "",
                    call_id=body.call_id or "",
                    delegation_jti=body.delegation_jti,
                    tenant_id=body.tenant_id or "",
                    security_capabilities=frozenset(body.required_security_capabilities or ()),
                )
            )
        try:
            result = await self._controller.evaluate_and_execute(
                agent_id=agent_id,
                user_id=user_id or "",
                tool_name=body.tool_name,
                arguments=body.arguments,
                task_context=body.task_context,
                session_id=body.session_id,
                task_id=body.task_id,
            )
        finally:
            if context_token is not None:
                current_execution_request.reset(context_token)
        observe_tool_call(body.tool_name, result.status)
        self._refresh_pending_approvals()

        response = GovernResponse(
            status=result.status,
            result=result.content if result.content is not None else result.reason or result.status,
            request_id=result.request_id if result.status == "require_approval" else None,
            error_code=result.error_code,
            terminal_status=(
                result.terminal_status
                or (result.execution_receipt.status.value if result.execution_receipt else None)
            ),
            execution_receipt=result.execution_receipt,
            supported_security_capabilities=(
                sorted(self._execution_security.supported_security_capabilities)
                if self._execution_security.strict
                else None
            ),
        )
        logger.info(
            "tool_call result status=%s tool=%s request_id=%s",
            result.status,
            body.tool_name,
            result.request_id,
        )
        return JSONResponse(response.model_dump(mode="json"))

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
                terminal_status=(
                    result.terminal_status
                    or (result.execution_receipt.status.value if result.execution_receipt else None)
                ),
                execution_receipt=result.execution_receipt,
            ).model_dump(mode="json")
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
        principal, error = await self._require_self(request, PERM_APPROVAL_DECIDE)
        if error is not None:
            return error
        assert principal is not None

        store = self._controller._runtime.approval_manager._store
        store.refresh()
        pending = store.get_pending()
        if not self._is_platform_admin(principal):
            pending = [req for req in pending if req.tenant_id == principal.tenant_id]
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
        principal, error = await self._require_self(request, PERM_APPROVAL_DECIDE)
        if error is not None:
            return error
        assert principal is not None

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
        items, total = store.list_history(
            status=status,
            agent_id=request.query_params.get("agent_id"),
            tool_name=request.query_params.get("tool_name"),
            requester_id=request.query_params.get("requester_id"),
            approver_id=request.query_params.get("approver_id"),
            tenant_id=principal.tenant_id,
            all_tenants=self._is_platform_admin(principal),
            limit=limit,
            offset=offset,
        )
        return JSONResponse(
            AdminApprovalsResponse(
                approvals=items,
                total=total,
                limit=limit,
                offset=offset,
            ).model_dump(mode="json")
        )

    def _approval_cursor(self, event_id: int, scope: str) -> str:
        payload = json.dumps(
            {"v": 1, "id": event_id, "scope": scope},
            sort_keys=True, separators=(",", ":"),
        ).encode()
        key = (self._api_key or "loop-controller-approval-stream").encode()
        signature = hmac.new(key, payload, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(payload + signature).decode().rstrip("=")

    def _decode_approval_cursor(self, cursor: str, scope: str) -> int:
        if not cursor or any(char in cursor for char in "\r\n\x00"):
            raise ValueError("invalid")
        try:
            raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
            payload, signature = raw[:-32], raw[-32:]
            key = (self._api_key or "loop-controller-approval-stream").encode()
            if not hmac.compare_digest(signature, hmac.new(key, payload, hashlib.sha256).digest()):
                raise ValueError("invalid")
            data = json.loads(payload)
            if data != {"v": 1, "id": data.get("id"), "scope": scope}:
                raise ValueError("invalid")
            event_id = data["id"]
            if not isinstance(event_id, int) or event_id < 0:
                raise ValueError("invalid")
            return event_id
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise ValueError("invalid") from exc

    async def _handle_admin_approvals_stream(self, request: Request) -> Response:
        principal, error = await self._require_self(request, PERM_APPROVAL_DECIDE)
        if error is not None:
            return error
        assert principal is not None
        all_tenants = self._is_platform_admin(principal)
        scope = "platform" if all_tenants else f"tenant:{principal.tenant_id}"
        store = self._controller._runtime.approval_manager._store
        oldest, latest = store.event_bounds(
            tenant_id=principal.tenant_id, all_tenants=all_tenants
        )
        raw_cursor = request.headers.get("last-event-id")
        if raw_cursor is None:
            after_id = latest
        else:
            try:
                after_id = self._decode_approval_cursor(raw_cursor, scope)
            except ValueError:
                return JSONResponse({"error": "approval_cursor_invalid"}, status_code=400)
            if after_id > latest:
                return JSONResponse({"error": "approval_cursor_future"}, status_code=400)
            if oldest is not None and after_id < oldest - 1:
                return JSONResponse({"error": "approval_cursor_expired"}, status_code=410)
        try:
            max_wait = max(0.1, min(float(request.query_params.get("max_wait", "60")), 300.0))
        except ValueError:
            return JSONResponse({"error": "invalid_parameter"}, status_code=400)

        async def events() -> AsyncIterator[str]:
            nonlocal after_id
            deadline = time.monotonic() + max_wait
            yield "retry: 1000\n\n"
            heartbeat_at = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                rows = store.list_events(
                    after_id=after_id,
                    tenant_id=principal.tenant_id,
                    all_tenants=all_tenants,
                    limit=100,
                )
                if rows:
                    for row in rows:
                        after_id = row["event_id"]
                        cursor = self._approval_cursor(after_id, scope)
                        data = row["item"].model_dump(mode="json")
                        yield (
                            f"id: {cursor}\n"
                            f"event: {row['event_type']}\n"
                            f"data: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"
                        )
                    heartbeat_at = time.monotonic() + 10.0
                    continue
                if time.monotonic() >= heartbeat_at:
                    yield ": heartbeat\n\n"
                    heartbeat_at = time.monotonic() + 10.0
                await self._sleep(0.25)

        return StreamingResponse(
            events(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def _handle_admin_approval(self, request: Request, *, verdict: str) -> JSONResponse:
        auth = request.headers.get("authorization") or ""
        credential = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        try:
            principal = resolve_approver_principal(
                credential,
                self._entrypoints_config.get("approval_auth") or {},
            )
        except ApprovalAuthenticationError:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        except ApprovalAuthorizationError:
            return JSONResponse({"error": "forbidden"}, status_code=403)

        decision_id = request.path_params["decision_id"]
        try:
            body = await request.json() if request.method == "POST" else {}
        except Exception:  # noqa: BLE001
            body = {}
        if not isinstance(body, dict):
            body = {}
        comment = str(body.get("comment", "")).strip()

        approval_manager = self._controller._runtime.approval_manager
        store = approval_manager._store
        store.refresh()
        req = store.get_request(decision_id)
        existing = store.get_record(decision_id)

        # 审批 tenant 双轨锚定（v0.52 §4.4）：approval_auth principal 必须持有落在
        # 请求租户上的 approver RBAC 绑定；不一致即 403，fail-closed。
        enforcer = self._rbac_enforcer()
        if enforcer is not None and req is not None:
            req_tenant = getattr(req, "tenant_id", None)
            if not enforcer.approver_binding_matches(principal, req_tenant):
                denial_principal = AuthenticatedPrincipal(
                    principal_id=principal,
                    tenant_id=req_tenant,
                    roles=(),
                    auth_method="approval-auth",
                )
                reason = (
                    f"approver RBAC 绑定与请求租户不一致: request_tenant={req_tenant!r}"
                )
                self._record_denial(request, denial_principal, PERM_APPROVAL_DECIDE, reason)
                return JSONResponse({"error": "forbidden", "reason": reason}, status_code=403)

        identity = self._identity_provider

        def _approver_exists(user_id: str) -> bool:
            if identity is None:
                return False
            return identity.get_user(user_id) is not None

        try:
            record = build_approval_record(
                req,
                existing,
                principal,
                verdict,
                comment,
                approver_exists=_approver_exists,
            )
        except ApprovalAuthorizationError as exc:
            return JSONResponse({"error": str(exc)}, status_code=403)
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
            metadata={
                "principal": principal,
                "request_id": req.request_id if req is not None else None,
                "decision_id": decision_id,
                "action_summary": f"approval_{verdict}",
                "comment": comment,
            },
            actor_id=principal,
        )
        return JSONResponse({"decision_id": decision_id, "verdict": verdict})

    async def _handle_admin_approvals_approve(self, request: Request) -> JSONResponse:
        return await self._handle_admin_approval(request, verdict="approve")

    async def _handle_admin_approvals_deny(self, request: Request) -> JSONResponse:
        return await self._handle_admin_approval(request, verdict="deny")

    @staticmethod
    def _candidate_payload(candidate: Any) -> dict[str, Any]:
        return {
            "candidate_id": candidate.candidate_id,
            "state": candidate.state.value,
            "base_revision": candidate.base_revision,
            "source_sha256": candidate.source_sha256,
            "revision": candidate.revision,
            "artifact_sha256": candidate.artifact_sha256,
            "artifact_size": candidate.artifact_size,
            "created_by": candidate.created_by,
            "created_at": candidate.created_at,
            "published_at": candidate.published_at,
            "rollback_of_revision": candidate.rollback_of_revision,
            "tenant_id": candidate.tenant_id,
        }

    def _policy_services(self) -> tuple[Any, Any, Any]:
        runtime = self._controller._runtime
        return (
            getattr(runtime, "policy_delivery", None),
            getattr(runtime, "policy_validation", None),
            getattr(runtime, "policy_lifecycle", None),
        )

    async def _handle_policy_candidates(self, request: Request) -> JSONResponse:
        delivery, _validation, _lifecycle = self._policy_services()
        if delivery is None:
            return JSONResponse({"error": "policy_delivery_unavailable"}, status_code=503)
        if request.method == "GET":
            principal, error = await self._require_self(request, PERM_CANDIDATE_READ)
            if error is not None:
                return error
            assert principal is not None
            if self._is_platform_admin(principal):
                candidates = delivery.store.list_candidates()
            else:
                candidates = delivery.store.list_candidates(tenant_id=principal.tenant_id)
            return JSONResponse({"candidates": [self._candidate_payload(item) for item in candidates]})
        principal, error = await self._require_self(request, PERM_CANDIDATE_CREATE)
        if error is not None:
            return error
        assert principal is not None
        try:
            body = await request.json()
            files = body.get("files")
            if isinstance(files, list):
                files = {item["path"]: item["content"] for item in files}
            candidate = delivery.create_candidate(
                files, base_revision=body.get("base_revision"),
                actor=principal.principal_id, tenant_id=principal.tenant_id,
            )
            await self._audit_admin_operation(
                request, "policy_candidate_create", target=candidate.candidate_id,
                metadata={"base_revision": candidate.base_revision, "source_sha256": candidate.source_sha256, "result": "success", "actor": principal.principal_id, "auth_method": principal.auth_method},
                actor_id=principal.principal_id,
            )
            return JSONResponse(self._candidate_payload(candidate), status_code=201)
        except CandidateLimitError:
            return JSONResponse({"error": "candidate_limit"}, status_code=413)
        except (InvalidCandidateError, KeyError, TypeError, ValueError):
            return JSONResponse({"error": "invalid_candidate"}, status_code=400)
        except StateDatabaseError:
            return JSONResponse({"error": "policy_delivery_unavailable"}, status_code=503)

    async def _handle_policy_candidate(self, request: Request) -> JSONResponse:
        delivery, _validation, _lifecycle = self._policy_services()
        if delivery is None:
            return JSONResponse({"error": "policy_delivery_unavailable"}, status_code=503)
        principal, error = await self._authenticate_only(request)
        if error is not None:
            return error
        assert principal is not None
        candidate = delivery.store.get_candidate(request.path_params["candidate_id"])
        if candidate is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        error = self._authorize(request, principal, PERM_CANDIDATE_READ, candidate.tenant_id)
        if error is not None:
            return error
        return JSONResponse(self._candidate_payload(candidate))

    async def _handle_policy_validate(self, request: Request) -> JSONResponse:
        delivery, validation, _lifecycle = self._policy_services()
        if delivery is None or validation is None:
            return JSONResponse({"error": "policy_validation_unavailable"}, status_code=503)
        principal, error = await self._authenticate_only(request)
        if error is not None:
            return error
        assert principal is not None
        candidate_id = request.path_params["candidate_id"]
        existing = delivery.store.get_candidate(candidate_id)
        if existing is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        error = self._authorize(request, principal, PERM_VALIDATE_RUN, existing.tenant_id)
        if error is not None:
            return error
        if existing.state.value != "draft":
            return JSONResponse(self._candidate_payload(existing))
        try:
            candidate, result = validation.validate(candidate_id, actor=principal.principal_id)
            await self._audit_admin_operation(
                request, "policy_validate", target=candidate_id,
                metadata={"base_revision": candidate.base_revision, "target_revision": candidate.revision, "source_sha256": candidate.source_sha256, "artifact_sha256": candidate.artifact_sha256, "result": "success" if result.ok else "failed", "result_sha256": result.result_sha256, "actor": principal.principal_id, "auth_method": principal.auth_method},
                actor_id=principal.principal_id,
            )
            payload = {**self._candidate_payload(candidate), "validation": result.model_dump(mode="json")}
            return JSONResponse(payload, status_code=200 if result.ok else 422)
        except (InvalidCandidateError, CandidateStateError):
            return JSONResponse({"error": "candidate_state_conflict"}, status_code=409)
        except (StateDatabaseError, OSError):
            return JSONResponse({"error": "policy_validation_unavailable"}, status_code=503)

    async def _handle_policy_shadow(self, request: Request) -> JSONResponse:
        runtime = self._controller._runtime
        delivery = getattr(runtime, "policy_delivery", None)
        shadow = getattr(runtime, "policy_shadow", None)
        runner = getattr(runtime, "policy_shadow_runner", None)
        if delivery is None or (shadow is None and runner is None):
            return JSONResponse({"error": "shadow_unavailable"}, status_code=503)
        principal, error = await self._authenticate_only(request)
        if error is not None:
            return error
        assert principal is not None
        candidate = delivery.store.get_candidate(request.path_params["candidate_id"])
        if candidate is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        error = self._authorize(request, principal, PERM_SHADOW_RUN, candidate.tenant_id)
        if error is not None:
            return error
        if candidate.state.value not in {"validated", "published", "loaded"}:
            return JSONResponse({"error": "candidate_state_conflict"}, status_code=409)
        try:
            body = await request.json()
            requested_revision = body.get("revision")
            requested_hash = body.get("artifact_sha256")
            if requested_revision != candidate.revision or requested_hash != candidate.artifact_sha256:
                return JSONResponse({"error": "candidate_artifact_mismatch"}, status_code=409)
            if runner is not None:
                config = getattr(runtime, "config", None)
                delivery_cfg = getattr(config, "policy_delivery", None) if config is not None else None
                shadow = PolicyShadowService.for_candidate(
                    delivery, runner, candidate.candidate_id, requested_revision, requested_hash,
                    max_samples=getattr(delivery_cfg, "shadow_max_samples", 1000),
                    max_input_bytes=getattr(delivery_cfg, "shadow_max_input_bytes", 65536),
                )
            if shadow is None:
                return JSONResponse({"error": "shadow_unavailable"}, status_code=503)
            result = await shadow.run(body.get("samples", []))
            await self._audit_admin_operation(
                request, "policy_shadow", target=candidate.candidate_id,
                metadata={"base_revision": candidate.base_revision, "target_revision": candidate.revision, "source_sha256": candidate.source_sha256, "artifact_sha256": candidate.artifact_sha256, "input_set_digest": result.input_set_digest, "result_sha256": result.result_sha256, "result": "success" if result.ok else "failed", "actor": principal.principal_id, "auth_method": principal.auth_method},
                actor_id=principal.principal_id,
            )
            return JSONResponse(result.model_dump(mode="json"), status_code=200 if result.ok else 503)
        except (ValueError, TypeError):
            return JSONResponse({"error": "invalid_shadow_samples"}, status_code=400)

    async def _handle_policy_publish(self, request: Request) -> JSONResponse:
        delivery, _validation, _lifecycle = self._policy_services()
        if delivery is None:
            return JSONResponse({"error": "policy_delivery_unavailable"}, status_code=503)
        principal, error = await self._authenticate_only(request)
        if error is not None:
            return error
        assert principal is not None
        candidate_id = request.path_params["candidate_id"]
        candidate = delivery.store.get_candidate(candidate_id)
        if candidate is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        error = self._authorize(request, principal, PERM_PUBLISH, candidate.tenant_id)
        if error is not None:
            return error
        enforcer = self._rbac_enforcer()
        if enforcer is not None:
            scope = enforcer.check_publish_scope(principal)
            if not scope.allowed:
                self._record_denial(request, principal, RESOURCE_PUBLISH_GLOBAL, scope.reason)
                return JSONResponse({"error": "forbidden", "reason": scope.reason}, status_code=403)
        try:
            body = await request.json()
            waiver_reason = body.get("waiver_reason")
            waive = bool(waiver_reason) and enforcer is not None and enforcer.can_waive_separation(principal)
            result = delivery.store.publish(
                candidate_id, body.get("base_revision"), principal.principal_id,
                enforce_separation=enforcer is not None,
                waive_separation=waive, waiver_reason=waiver_reason,
            )
            candidate = delivery.store.get_candidate(candidate_id)
            assert candidate is not None
            await self._audit_admin_operation(
                request, "policy_publish", target=candidate_id,
                metadata={"base_revision": body.get("base_revision"), "target_revision": result["revision"], "source_sha256": candidate.source_sha256, "artifact_sha256": candidate.artifact_sha256, "generation": result["generation"], "result": "success", "actor": principal.principal_id, "auth_method": principal.auth_method, "separation_waived": waive},
                actor_id=principal.principal_id,
            )
            return JSONResponse({**result, "state": "published"}, status_code=202)
        except SeparationOfDutiesError as exc:
            return JSONResponse({"error": "separation_of_duties_violation", "reason": str(exc)}, status_code=409)
        except PolicyCASConflictError:
            return JSONResponse({"error": "base_revision_conflict"}, status_code=409)
        except CandidateStateError:
            return JSONResponse({"error": "candidate_state_conflict"}, status_code=409)
        except (ArtifactConflictError, StateDatabaseError):
            return JSONResponse({"error": "policy_delivery_unavailable"}, status_code=503)

    async def _handle_policy_rollback(self, request: Request) -> JSONResponse:
        delivery, _validation, _lifecycle = self._policy_services()
        if delivery is None:
            return JSONResponse({"error": "policy_delivery_unavailable"}, status_code=503)
        principal, error = await self._authenticate_only(request)
        if error is not None:
            return error
        assert principal is not None
        # 回滚按平台级影响面处理：全局 current pointer，仅 platform_admin 或 publish_scope 授权
        enforcer = self._rbac_enforcer()
        error = self._authorize(request, principal, PERM_ROLLBACK, principal.tenant_id)
        if error is not None:
            return error
        if enforcer is not None:
            scope = enforcer.check_publish_scope(principal)
            if not scope.allowed:
                self._record_denial(request, principal, RESOURCE_PUBLISH_GLOBAL, scope.reason)
                return JSONResponse({"error": "forbidden", "reason": scope.reason}, status_code=403)
        try:
            body = await request.json()
            waiver_reason = body.get("waiver_reason")
            waive = bool(waiver_reason) and enforcer is not None and enforcer.can_waive_separation(principal)
            candidate, result = delivery.store.rollback(
                body["revision"], body.get("base_revision"), principal.principal_id,
                enforce_separation=enforcer is not None,
                waive_separation=waive, waiver_reason=waiver_reason,
            )
            await self._audit_admin_operation(
                request, "policy_rollback", target=candidate.candidate_id,
                metadata={"base_revision": body.get("base_revision"), "target_revision": result["revision"], "artifact_sha256": candidate.artifact_sha256, "generation": result["generation"], "result": "success", "actor": principal.principal_id, "auth_method": principal.auth_method, "separation_waived": waive},
                actor_id=principal.principal_id,
            )
            return JSONResponse({**result, "candidate_id": candidate.candidate_id, "state": "published"}, status_code=202)
        except (KeyError, InvalidCandidateError):
            return JSONResponse({"error": "invalid_revision"}, status_code=404)
        except SeparationOfDutiesError as exc:
            return JSONResponse({"error": "separation_of_duties_violation", "reason": str(exc)}, status_code=409)
        except (PolicyCASConflictError, CandidateStateError):
            return JSONResponse({"error": "base_revision_conflict"}, status_code=409)
        except (ArtifactConflictError, StateDatabaseError):
            return JSONResponse({"error": "policy_delivery_unavailable"}, status_code=503)

    async def _handle_policy_status(self, request: Request) -> JSONResponse:
        _principal, error = await self._require_self(request, PERM_STATUS_READ)
        if error is not None:
            return error
        _delivery, _validation, lifecycle = self._policy_services()
        if lifecycle is None:
            return JSONResponse({"error": "policy_delivery_unavailable"}, status_code=503)
        return JSONResponse(lifecycle.status())

    async def _handle_policy_audit(self, request: Request) -> JSONResponse:
        principal, error = await self._require_self(request, PERM_AUDIT_READ)
        if error is not None:
            return error
        assert principal is not None
        delivery, _validation, _lifecycle = self._policy_services()
        if delivery is None:
            return JSONResponse({"error": "policy_delivery_unavailable"}, status_code=503)
        if self._is_platform_admin(principal) or principal.tenant_id is None:
            events = delivery.store.list_audit()
        else:
            events = delivery.store.list_audit(tenant_id=principal.tenant_id)
        return JSONResponse({"events": events})

    @staticmethod
    def _binding_payload(binding) -> dict:
        return {
            "binding_id": binding.binding_id,
            "principal": binding.principal,
            "tenant_id": binding.tenant_id,
            "role": binding.role.value,
            "granted_by": binding.granted_by,
            "created_at": binding.created_at,
        }

    @staticmethod
    def _grant_payload(grant) -> dict:
        return {
            "grant_id": grant.grant_id,
            "source_principal": grant.source_principal,
            "source_tenant": grant.source_tenant,
            "target_tenant": grant.target_tenant,
            "resources": list(grant.resources),
            "granted_by": grant.granted_by,
            "created_at": grant.created_at,
        }

    async def _handle_rbac_bindings(self, request: Request) -> JSONResponse:
        store = self._rbac_store()
        if store is None:
            return JSONResponse({"error": "rbac_unavailable"}, status_code=503)
        if request.method == "GET":
            principal, error = await self._require_self(request, PERM_RBAC_MANAGE)
            if error is not None:
                return error
            assert principal is not None
            if self._is_platform_admin(principal):
                bindings = store.list_all_bindings()
            elif principal.tenant_id is not None:
                bindings = store.list_all_bindings(tenant_id=principal.tenant_id)
            else:
                bindings = []
            return JSONResponse({"bindings": [self._binding_payload(b) for b in bindings]})
        principal, error = await self._require_self(request, PERM_RBAC_MANAGE)
        if error is not None:
            return error
        assert principal is not None
        try:
            body = await request.json()
            target_principal = body["principal"]
            tenant_id = body.get("tenant_id")
            role = Role(body["role"])
        except (KeyError, TypeError, ValueError):
            return JSONResponse({"error": "invalid_binding"}, status_code=400)
        if not isinstance(target_principal, str) or not target_principal:
            return JSONResponse({"error": "invalid_binding"}, status_code=400)
        # 平台角色只允许平台管理员在平台级绑定；租户管理员不可授予平台权限。
        if role is Role.PLATFORM_ADMIN and (tenant_id is not None or not self._is_platform_admin(principal)):
            self._record_denial(request, principal, PERM_RBAC_MANAGE, "platform_admin 仅限平台级管理员授予")
            return JSONResponse({"error": "forbidden"}, status_code=403)
        if not self._is_platform_admin(principal) and (principal.tenant_id is None or tenant_id != principal.tenant_id):
            self._record_denial(
                request, principal, PERM_RBAC_MANAGE,
                "tenant_admin 仅能管理本租户角色绑定",
            )
            return JSONResponse(
                {"error": "forbidden", "reason": "tenant_admin 仅能管理本租户角色绑定"},
                status_code=403,
            )
        try:
            binding = store.add_binding(target_principal, tenant_id, role, principal.principal_id)
        except StateDatabaseError:
            return JSONResponse({"error": "rbac_unavailable"}, status_code=503)
        await self._audit_admin_operation(
            request, "rbac_binding_create", target=binding.binding_id,
            metadata={"principal": target_principal, "tenant_id": tenant_id, "role": role.value, "result": "success", "actor": principal.principal_id, "auth_method": principal.auth_method},
            actor_id=principal.principal_id,
        )
        return JSONResponse(self._binding_payload(binding), status_code=201)

    async def _handle_rbac_binding_revoke(self, request: Request) -> JSONResponse:
        store = self._rbac_store()
        if store is None:
            return JSONResponse({"error": "rbac_unavailable"}, status_code=503)
        principal, error = await self._require_self(request, PERM_RBAC_MANAGE)
        if error is not None:
            return error
        assert principal is not None
        binding_id = request.path_params["binding_id"]
        binding = next(
            (b for b in store.list_all_bindings() if b.binding_id == binding_id), None
        )
        if binding is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        if not self._is_platform_admin(principal) and binding.tenant_id != principal.tenant_id:
            self._record_denial(
                request, principal, PERM_RBAC_MANAGE,
                "tenant_admin 仅能吊销本租户角色绑定",
            )
            return JSONResponse(
                {"error": "forbidden", "reason": "tenant_admin 仅能吊销本租户角色绑定"},
                status_code=403,
            )
        try:
            revoked = store.revoke_binding(binding_id, principal.principal_id)
        except StateDatabaseError:
            return JSONResponse({"error": "rbac_unavailable"}, status_code=503)
        if not revoked:
            return JSONResponse({"error": "not_found"}, status_code=404)
        await self._audit_admin_operation(
            request, "rbac_binding_revoke", target=binding_id,
            metadata={"principal": binding.principal, "tenant_id": binding.tenant_id, "role": binding.role.value, "result": "success", "actor": principal.principal_id, "auth_method": principal.auth_method},
            actor_id=principal.principal_id,
        )
        return JSONResponse({"binding_id": binding_id, "state": "revoked"})

    async def _handle_rbac_grants(self, request: Request) -> JSONResponse:
        store = self._rbac_store()
        if store is None:
            return JSONResponse({"error": "rbac_unavailable"}, status_code=503)
        if request.method == "GET":
            principal, error = await self._require_self(request, PERM_RBAC_MANAGE)
            if error is not None:
                return error
            assert principal is not None
            grants = store.list_all_grants()
            if not self._is_platform_admin(principal) and principal.tenant_id is not None:
                tenant = principal.tenant_id
                grants = [
                    g for g in grants
                    if g.source_tenant == tenant or g.target_tenant == tenant
                ]
            return JSONResponse({"grants": [self._grant_payload(g) for g in grants]})
        # 跨租户授权影响面为平台级：仅 platform_admin 可创建
        principal, error = await self._require(request, PERM_RBAC_MANAGE, tenant_id=None)
        if error is not None:
            return error
        assert principal is not None
        try:
            body = await request.json()
            grant = store.add_grant(
                body["source_principal"],
                body["source_tenant"],
                body["target_tenant"],
                tuple(body["resources"]),
                principal.principal_id,
            )
        except (KeyError, TypeError):
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        except StateDatabaseError:
            return JSONResponse({"error": "rbac_unavailable"}, status_code=503)
        await self._audit_admin_operation(
            request, "rbac_grant_create", target=grant.grant_id,
            metadata={"source_principal": grant.source_principal, "source_tenant": grant.source_tenant, "target_tenant": grant.target_tenant, "resources": list(grant.resources), "result": "success", "actor": principal.principal_id, "auth_method": principal.auth_method},
            actor_id=principal.principal_id,
        )
        return JSONResponse(self._grant_payload(grant), status_code=201)

    async def _handle_rbac_grant_revoke(self, request: Request) -> JSONResponse:
        store = self._rbac_store()
        if store is None:
            return JSONResponse({"error": "rbac_unavailable"}, status_code=503)
        principal, error = await self._require(request, PERM_RBAC_MANAGE, tenant_id=None)
        if error is not None:
            return error
        assert principal is not None
        grant_id = request.path_params["grant_id"]
        grant = next((g for g in store.list_all_grants() if g.grant_id == grant_id), None)
        if grant is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        try:
            revoked = store.revoke_grant(grant_id, principal.principal_id)
        except StateDatabaseError:
            return JSONResponse({"error": "rbac_unavailable"}, status_code=503)
        if not revoked:
            return JSONResponse({"error": "not_found"}, status_code=404)
        await self._audit_admin_operation(
            request, "rbac_grant_revoke", target=grant_id,
            metadata={"source_principal": grant.source_principal, "source_tenant": grant.source_tenant, "target_tenant": grant.target_tenant, "result": "success", "actor": principal.principal_id, "auth_method": principal.auth_method},
            actor_id=principal.principal_id,
        )
        return JSONResponse({"grant_id": grant_id, "state": "revoked"})

    async def _handle_opa_bundle(self, request: Request) -> Response:
        runtime = self._controller._runtime
        token = getattr(runtime, "bundle_reader_token", None)
        if not self._check_bearer(request, token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        lifecycle = getattr(runtime, "policy_lifecycle", None)
        if lifecycle is None:
            return JSONResponse({"error": "bundle_unavailable"}, status_code=503)
        revision = request.path_params.get("revision")
        try:
            data, digest, size, revision = lifecycle.bundle(revision)
        except FileNotFoundError:
            return JSONResponse({"error": "not_found"}, status_code=404)
        except (ArtifactConflictError, StateDatabaseError):
            return JSONResponse({"error": "bundle_unavailable"}, status_code=503)
        etag = f'"{digest}"'
        headers = {"Content-Type": "application/gzip", "Content-Length": str(size), "ETag": etag, "X-OPA-Bundle-Revision": revision, "Cache-Control": "private, no-cache"}
        if request.headers.get("if-none-match") == etag:
            headers.pop("Content-Length")
            return Response(status_code=304, headers=headers)
        if request.method == "HEAD":
            return Response(status_code=200, headers=headers)
        return Response(data, media_type="application/gzip", headers=headers)

    async def _handle_opa_status(self, request: Request) -> JSONResponse:
        runtime = self._controller._runtime
        if not self._check_bearer(request, getattr(runtime, "status_writer_token", None)):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        lifecycle = getattr(runtime, "policy_lifecycle", None)
        if lifecycle is None:
            return JSONResponse({"error": "status_unavailable"}, status_code=503)
        raw = await request.body()
        try:
            import json
            payload = json.loads(raw)
            result = lifecycle.receive_status(payload, raw_size=len(raw))
            return JSONResponse(result, status_code=202)
        except PolicyStatusForbiddenError:
            return JSONResponse({"error": "forbidden"}, status_code=403)
        except (PolicyStatusError, ValueError):
            return JSONResponse({"error": "invalid_status"}, status_code=400)
        except StateDatabaseError:
            return JSONResponse({"error": "status_unavailable"}, status_code=503)

    async def _handle_admin_audit(self, request: Request) -> JSONResponse:
        principal, error = await self._require_self(request, PERM_AUDIT_READ)
        if error is not None:
            return error
        assert principal is not None

        session_id = request.query_params.get("session_id")
        task_id = request.query_params.get("task_id")
        correlation_id = request.query_params.get("correlation_id")
        agent_id = request.query_params.get("agent_id")
        tool_name = request.query_params.get("tool_name")
        interaction_id = request.query_params.get("interaction_id")
        source_agent_id = request.query_params.get("source_agent_id")
        target_agent_id = request.query_params.get("target_agent_id")
        verdict = request.query_params.get("verdict")
        start_time, error = _parse_audit_time(
            request.query_params.get("start_time"), "start_time"
        )
        if error is not None:
            return error
        end_time, error = _parse_audit_time(
            request.query_params.get("end_time"), "end_time"
        )
        if error is not None:
            return error
        if start_time is not None and end_time is not None and start_time > end_time:
            return JSONResponse(
                {"error": "invalid_parameter", "message": "start_time must not exceed end_time"},
                status_code=400,
            )
        for name, value in (("session_id", session_id), ("task_id", task_id), ("correlation_id", correlation_id), ("interaction_id", interaction_id)):
            if value is not None and (not value.strip() or len(value) > 256):
                return JSONResponse(
                    {"error": "invalid_parameter", "message": f"invalid {name}"},
                    status_code=400,
                )
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
        tenant_id = None if self._is_platform_admin(principal) else principal.tenant_id
        interaction_filters = any(
            value is not None
            for value in (interaction_id, source_agent_id, target_agent_id, verdict)
        )
        if correlation_id is not None:
            selected = audit_store.query_by_correlation(
                correlation_id, limit=limit, tenant_id=tenant_id,
                start_time=start_time, end_time=end_time,
                agent_id=agent_id, tool_name=tool_name,
            )
        elif interaction_filters:
            selected = audit_store.query_interactions(
                interaction_id=interaction_id,
                source_agent_id=source_agent_id,
                target_agent_id=target_agent_id,
                verdict=verdict,
                tenant_id=tenant_id,
                start_time=start_time,
                end_time=end_time,
                agent_id=agent_id,
                tool_name=tool_name,
                limit=limit,
            )
        elif task_id and hasattr(audit_store, "query_by_task"):
            selected = audit_store.query_by_task(
                task_id, tenant_id=tenant_id, start_time=start_time,
                end_time=end_time, agent_id=agent_id, tool_name=tool_name,
                limit=limit,
            )
        elif session_id and hasattr(audit_store, "query_by_session"):
            selected = audit_store.query_by_session(
                session_id, tenant_id=tenant_id, start_time=start_time,
                end_time=end_time, agent_id=agent_id, tool_name=tool_name,
                limit=limit,
            )
        elif not session_id and hasattr(audit_store, "list_recent"):
            selected = audit_store.list_recent(
                limit, tenant_id=tenant_id, start_time=start_time, end_time=end_time,
                agent_id=agent_id, tool_name=tool_name,
            )
        else:
            selected = []
            async for event in audit_store.iter_events():
                if session_id and event.session_id != session_id:
                    continue
                if task_id and event.task_id != task_id:
                    continue
                if tenant_id is not None and event.tenant_id != tenant_id:
                    continue
                if start_time is not None and event.timestamp < start_time:
                    continue
                if end_time is not None and event.timestamp > end_time:
                    continue
                if agent_id is not None and event.actor_id != agent_id:
                    continue
                if tool_name is not None and event.target != tool_name:
                    continue
                selected.append(event)
            selected = selected[-limit:]
        selected = [
            event for event in selected
            if (tenant_id is None or event.tenant_id == tenant_id)
            and (start_time is None or event.timestamp >= start_time)
            and (end_time is None or event.timestamp <= end_time)
        ][:limit]
        events = []
        for event in selected:
            item = event.model_dump(mode="json")
            item["agent_id"] = event.actor_id
            item["tool_name"] = event.target
            events.append(item)
        return JSONResponse(AuditQueryResponse(events=events).model_dump())

    async def _require_console_admin(self, request: Request) -> JSONResponse | None:
        _, error = await self._require(request, PERM_RBAC_MANAGE, tenant_id=None)
        return error

    @staticmethod
    def _sanitize_kernel_payload(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: ToolGovernServer._sanitize_kernel_payload(item)
                for key, item in value.items()
                if not _SENSITIVE_KEY_PATTERN.search(str(key))
            }
        if isinstance(value, list):
            return [ToolGovernServer._sanitize_kernel_payload(item) for item in value]
        return value

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

    async def _handle_admin_secrets(self, request: Request) -> JSONResponse:
        """GET /v1/admin/secrets：列出不含明文的 Secret 引用元数据。"""
        error = await self._require_console_admin(request)
        if error is not None:
            return error
        broker = getattr(self._controller._runtime, "secret_broker", None)
        if broker is None:
            return JSONResponse({"error": "secret_broker_unavailable"}, status_code=503)
        if isinstance(broker, EncryptedFileSecretBackend):
            backend = "encrypted_file"
        elif isinstance(broker, FileSecretBackend):
            backend = "file"
        elif isinstance(broker, MemorySecretBackend):
            backend = "memory"
        else:
            return JSONResponse({"error": "secret_broker_unavailable"}, status_code=503)
        refs = await broker.list_refs()
        items = [
            AdminSecretItem(
                ref=item.ref,
                tenant_id=item.tenant_id,
                backend=backend,
                has_value=item.has_value,
            )
            for item in refs
        ]
        items.sort(key=lambda item: (item.tenant_id or "", item.ref))
        return JSONResponse(AdminSecretsResponse(secrets=items).model_dump(mode="json"))

    async def _handle_admin_agents(self, request: Request) -> JSONResponse:
        """GET /v1/admin/agents：列出已配置 Agent 及其吊销状态。"""
        error = await self._require_console_admin(request)
        if error is not None:
            return error
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
        error = await self._require_console_admin(request)
        if error is not None:
            return error
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
        error = await self._require_console_admin(request)
        if error is not None:
            return error
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
        error = await self._require_console_admin(request)
        if error is not None:
            return error
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
        error = await self._require_console_admin(request)
        if error is not None:
            return error
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
        error = await self._require_console_admin(request)
        if error is not None:
            return error
        bridge, gk = self._go_kernel_view()
        reachable = await bridge.ping() if bridge is not None else False
        return JSONResponse(
            AdminA2AStatusResponse(
                enabled=bool(gk.get("enabled", False)),
                reachable=reachable,
                base_url=str(gk.get("base_url", "")),
                local_agent=self._sanitize_kernel_payload(dict(gk.get("local_agent", {}) or {})),
            ).model_dump(mode="json")
        )

    async def _handle_admin_a2a_agents(self, request: Request) -> JSONResponse:
        """GET /v1/admin/a2a/agents：配置 Agent 与内核注册态合并视图。"""
        error = await self._require_console_admin(request)
        if error is not None:
            return error
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

    def _authorize_admin_a2a_task(
        self, request: Request, principal: AuthenticatedPrincipal, task: Any
    ) -> JSONResponse | None:
        if self._rbac_enforcer() is None or self._is_platform_admin(principal):
            return None
        if not isinstance(task, dict) or not isinstance(task.get("tenant_id"), str) or not task["tenant_id"]:
            return JSONResponse({"error": "forbidden"}, status_code=403)
        return self._authorize(request, principal, PERM_RBAC_MANAGE, task["tenant_id"])

    async def _handle_admin_a2a_tasks(self, request: Request) -> JSONResponse:
        """GET /v1/admin/a2a/tasks：列出当前可信 control scope 内任务。"""
        principal, error = await self._authenticate_only(request)
        if error is not None:
            return error
        assert principal is not None
        root_only_values = request.query_params.getlist("root_only")
        if len(root_only_values) > 1:
            return JSONResponse({"error": "invalid_root_only"}, status_code=400)
        raw_root_only = root_only_values[0] if root_only_values else "false"
        if raw_root_only not in {"true", "false"}:
            return JSONResponse({"error": "invalid_root_only"}, status_code=400)
        bridge, _gk = self._go_kernel_view()
        if bridge is None:
            return JSONResponse({"error": "go kernel disabled"}, status_code=503)
        try:
            tasks = await bridge.list_tasks(root_only=raw_root_only == "true")
        except Exception as exc:
            logger.warning("Go kernel task list failed: %s", exc)
            return JSONResponse({"error": "go kernel task list failed"}, status_code=502)
        visible: list[dict[str, Any]] = []
        for task in tasks:
            error = self._authorize_admin_a2a_task(request, principal, task)
            if error is not None:
                return JSONResponse({"error": "forbidden"}, status_code=403)
            visible.append(self._sanitize_kernel_payload(task))
        return JSONResponse({"tasks": visible})

    async def _handle_admin_a2a_task_query(self, request: Request) -> JSONResponse:
        """GET /v1/admin/a2a/tasks/{task_id}：经 Go 内核查询任务状态。"""
        principal, error = await self._authenticate_only(request)
        if error is not None:
            return error
        assert principal is not None
        bridge, _gk = self._go_kernel_view()
        if bridge is None:
            return JSONResponse({"error": "go kernel disabled"}, status_code=503)
        task = await bridge.query_task(request.path_params["task_id"])
        if task is None:
            return JSONResponse({"error": "task not found"}, status_code=404)
        error = self._authorize_admin_a2a_task(request, principal, task)
        if error is not None:
            return error
        return JSONResponse(self._sanitize_kernel_payload(task))

    async def _handle_admin_a2a_task_cancel(self, request: Request) -> JSONResponse:
        """POST /v1/admin/a2a/tasks/{task_id}/cancel：管理端取消委托任务。"""
        principal, error = await self._authenticate_only(request)
        if error is not None:
            return error
        assert principal is not None
        bridge, _gk = self._go_kernel_view()
        if bridge is None:
            return JSONResponse({"error": "go kernel disabled"}, status_code=503)
        task_id = request.path_params["task_id"]
        task_before = await bridge.query_task(task_id)
        if task_before is None:
            return JSONResponse({"error": "task not found"}, status_code=404)
        error = self._authorize_admin_a2a_task(request, principal, task_before)
        if error is not None:
            return error
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
        return JSONResponse(self._sanitize_kernel_payload(task))

    async def _handle_admin_a2a_task_stream(self, request: Request) -> Response:
        """GET /v1/admin/a2a/tasks/{task_id}/stream：SSE 转发任务状态流。"""
        principal, error = await self._authenticate_only(request)
        if error is not None:
            return error
        assert principal is not None
        bridge, _gk = self._go_kernel_view()
        if bridge is None:
            return JSONResponse({"error": "go kernel disabled"}, status_code=503)
        task_id = request.path_params["task_id"]
        task = await bridge.query_task(task_id)
        if task is None:
            return JSONResponse({"error": "task not found"}, status_code=404)
        error = self._authorize_admin_a2a_task(request, principal, task)
        if error is not None:
            return error

        cursor = request.headers.get("last-event-id")
        if cursor is not None and ("\r" in cursor or "\n" in cursor or "\x00" in cursor):
            return JSONResponse({"error": "event_cursor_invalid"}, status_code=400)
        stream = bridge.stream_task(task_id, cursor=cursor, include_sse=True)
        try:
            first = await anext(stream)
        except EventCursorExpiredError:
            await stream.aclose()
            return JSONResponse({"error": "event_cursor_expired"}, status_code=410)
        except EventCursorInvalidError as exc:
            await stream.aclose()
            return JSONResponse({"error": exc.code}, status_code=400)
        except StopAsyncIteration:
            await stream.aclose()
            return Response(status_code=204)

        def encode(frame: tuple[dict[str, Any], str | None, str | None]) -> str:
            event, event_id, event_type = frame
            fields = []
            if event_id is not None and "\r" not in event_id and "\n" not in event_id and "\x00" not in event_id:
                fields.append(f"id: {event_id}\n")
            if event_type is not None and "\r" not in event_type and "\n" not in event_type and "\x00" not in event_type:
                fields.append(f"event: {event_type}\n")
            fields.append(f"data: {json.dumps(self._sanitize_kernel_payload(event), ensure_ascii=False)}\n\n")
            return "".join(fields)

        async def events() -> AsyncIterator[str]:
            try:
                yield encode(first)
                async for frame in stream:
                    yield encode(frame)
            finally:
                await stream.aclose()

        return StreamingResponse(events(), media_type="text/event-stream")

    async def _handle_admin_a2a_delegation(self, request: Request) -> JSONResponse:
        """POST /v1/admin/a2a/delegations：管理端发起委托（与 Agent 自发委托同治理路径）。"""
        principal, error = await self._authenticate_only(request)
        if error is not None:
            return error
        assert principal is not None
        try:
            body = AdminDelegationRequest(**await request.json())
        except Exception as exc:
            return JSONResponse({"error": f"invalid request: {exc}"}, status_code=422)
        runtime = self._controller._runtime
        config = getattr(runtime, "config", None)
        if config is None:
            return JSONResponse({"error": "config unavailable"}, status_code=503)
        source = config.agents.get(body.source_agent_id)
        if source is None:
            return JSONResponse({"error": "unknown source agent"}, status_code=400)
        if self._rbac_enforcer() is not None and not self._is_platform_admin(principal):
            if not source.tenant_id or source.owner_id != principal.principal_id:
                self._record_denial(request, principal, PERM_RBAC_MANAGE, "source agent 不属于当前主体")
                return JSONResponse({"error": "forbidden"}, status_code=403)
        error = self._authorize(request, principal, PERM_RBAC_MANAGE, source.tenant_id)
        if error is not None:
            return error
        target = config.agents.get(body.target_agent_id)
        if target is not None:
            error = self._authorize(request, principal, PERM_RBAC_MANAGE, target.tenant_id)
            if error is not None:
                return error
        elif self._rbac_enforcer() is not None and not self._is_platform_admin(principal):
            return JSONResponse({"error": "forbidden"}, status_code=403)
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
            ).model_dump(mode="json")
        )

    async def _handle_admin_session_login(self, request: Request) -> JSONResponse:
        """POST /v1/admin/session/login：验证 Admin API Key，签发 Session Token。"""
        try:
            body = AdminSessionLoginRequest(**await request.json())
        except Exception as exc:
            return JSONResponse({"error": f"invalid request: {exc}"}, status_code=422)
        if not self._api_key or not hmac.compare_digest(body.api_key, self._api_key):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if self._rbac_enforcer() is not None:
            return JSONResponse({"error": "forbidden"}, status_code=403)
        config = getattr(self._controller._runtime, "config", None)
        rbac_cfg = getattr(config, "rbac", None)
        if rbac_cfg is not None:
            if getattr(rbac_cfg, "legacy_key_role", "platform_admin") == "reject":
                return JSONResponse({"error": "forbidden"}, status_code=403)
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
        if not self._session_store.validate(token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        revoked = self._session_store.revoke(token)
        await self._audit_admin_operation(
            request,
            "session_logout",
            target="admin_session",
            metadata={"revoked": revoked},
        )
        return JSONResponse({"revoked": revoked})

    async def _handle_admin_identity_config(self, request: Request) -> JSONResponse:
        """GET /v1/admin/identity：返回脱敏后的 Identity Provider 配置。"""
        error = await self._require_console_admin(request)
        if error is not None:
            return error
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
        error = await self._require_console_admin(request)
        if error is not None:
            return error
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
        error = await self._require_console_admin(request)
        if error is not None:
            return error
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
    execution_security: ExecutionSecurityPolicy | None = None,
    workload_identity_resolver: WorkloadIdentityResolver | None = None,
    delegation_token_verifier: Any = None,
    session_store: AdminSessionStore | None = None,
    interaction_engine: InteractionGovernanceEngine | None = None,
) -> Starlette:
    """从 LoopController 构造 Starlette ASGI 应用。"""
    if configure_logs:
        configure_logging(
            json_format=os.environ.get("LOOP_CONTROLLER_JSON_LOGS", "").lower() == "true"
        )
    runtime_config = getattr(controller._runtime, "config", None)
    if entrypoints_config is None and runtime_config is not None:
        entrypoints_config = getattr(runtime_config, "entrypoints_config", None)
    server = ToolGovernServer(
        controller,
        api_key=api_key,
        watcher=watcher,
        identity_provider=identity_provider,
        entrypoints_config=entrypoints_config,
        execution_security=execution_security,
        workload_identity_resolver=workload_identity_resolver,
        delegation_token_verifier=delegation_token_verifier,
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
            Route("/ready", server._handle_ready, methods=["GET"]),
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
                "/v1/admin/approvals/stream",
                server._handle_admin_approvals_stream,
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
            Route("/v1/admin/policy/candidates", server._handle_policy_candidates, methods=["GET", "POST"]),
            Route("/v1/admin/policy/candidates/{candidate_id}", server._handle_policy_candidate, methods=["GET"]),
            Route("/v1/admin/policy/candidates/{candidate_id}/validate", server._handle_policy_validate, methods=["POST"]),
            Route("/v1/admin/policy/candidates/{candidate_id}/shadow", server._handle_policy_shadow, methods=["POST"]),
            Route("/v1/admin/policy/candidates/{candidate_id}/publish", server._handle_policy_publish, methods=["POST"]),
            Route("/v1/admin/policy/rollback", server._handle_policy_rollback, methods=["POST"]),
            Route("/v1/admin/policy/status", server._handle_policy_status, methods=["GET"]),
            Route("/v1/admin/policy/audit", server._handle_policy_audit, methods=["GET"]),
            Route("/v1/admin/rbac/bindings", server._handle_rbac_bindings, methods=["GET", "POST"]),
            Route("/v1/admin/rbac/bindings/{binding_id}/revoke", server._handle_rbac_binding_revoke, methods=["POST"]),
            Route("/v1/admin/rbac/grants", server._handle_rbac_grants, methods=["GET", "POST"]),
            Route("/v1/admin/rbac/grants/{grant_id}/revoke", server._handle_rbac_grant_revoke, methods=["POST"]),
            Route("/v1/opa/bundles/current", server._handle_opa_bundle, methods=["GET", "HEAD"]),
            Route("/v1/opa/bundles/{revision}", server._handle_opa_bundle, methods=["GET", "HEAD"]),
            Route("/v1/opa/status", server._handle_opa_status, methods=["POST"]),
            Route("/v1/admin/audit", server._handle_admin_audit, methods=["GET"]),
            Route("/v1/admin/secrets", server._handle_admin_secrets, methods=["GET"]),
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
            Route("/v1/admin/a2a/tasks", server._handle_admin_a2a_tasks, methods=["GET"]),
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
    app.state.server = server
    return app


def load_api_key() -> str | None:
    """从环境变量读取 API key；未设置时返回 None。"""
    return os.environ.get("LOOP_CONTROLLER_API_KEY") or None
