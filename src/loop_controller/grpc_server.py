"""Loop Controller gRPC 服务边界（v0.19.0）。

本模块属于可选扩展，需要额外安装 grpc 依赖：

    uv pip install "loop-controller[grpc]"

使用方式：

    from loop_controller.controller import build_controller
    from loop_controller.infra.config_loader import ConfigLoader
    from loop_controller.grpc_server import serve

    config = ConfigLoader().load("config")
    controller = await build_controller(config)
    server = await serve(controller, port=50051)
    await server.wait_for_termination()

CLI：

    lc grpc-server --config ./config --port 50051
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import grpc
from grpc import aio as grpc_aio

from loop_controller.approval_watcher import ApprovalWatcher
from loop_controller.controller import LoopController
from loop_controller.identity import (
    AgentIdentity,
    IdentityCredential,
    IdentityProvider,
    KillSwitchConfig,
    RevocationEntry,
    RevocationType,
)
from loop_controller.models import AuditEvent
from loop_controller.v1 import governance_pb2, governance_pb2_grpc

logger = logging.getLogger("loop_controller.grpc_server")


def _governance_result(response) -> governance_pb2.EvaluateToolCallResponse:
    """把 GovernanceResult 属性映射到 gRPC response。"""
    return governance_pb2.EvaluateToolCallResponse(
        status=response.status,
        result=response.content
        if response.content is not None
        else response.reason or response.status,
        request_id=response.request_id or "",
        error_code=response.error_code or "",
    )


def _extract_identity_provider(controller: LoopController) -> IdentityProvider | None:
    """从 LoopController 的 Runtime 中提取 IdentityProvider。"""
    runtime = getattr(controller, "_runtime", None)
    if runtime is None:
        return None
    checkpoint = getattr(runtime, "checkpoint", None)
    if checkpoint is None:
        return None
    return getattr(checkpoint, "_identity", None)


def _extract_client_cert_identity(context: grpc_aio.ServicerContext) -> IdentityCredential | None:
    """从 gRPC mTLS 上下文中提取客户端证书 CN/SAN。"""
    auth_context = context.auth_context()
    if not auth_context:
        return None
    cn = None
    sans: list[str] = []
    for key, values in auth_context.items():
        if key == "x509_common_name" and values:
            raw = values[0]
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            cn = raw.removeprefix("CN=")
        elif key == "x509_subject_alternative_name" and values:
            for value in values:
                if isinstance(value, bytes):
                    value = value.decode("utf-8", errors="replace")
                sans.append(value)
        elif key == "x509_pem_cert" and values and not cn:
            # 部分实现未单独提供 CN；尝试从 PEM 中解析 subject。
            import re

            raw = values[0]
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            match = re.search(r"CN=([^,/\n]+)", raw)
            if match:
                cn = match.group(1)
    if not cn and not sans:
        return None
    return IdentityCredential(cert_cn=cn, cert_sans=sans, cert_subject=cn)


class ToolGovernanceServicer(governance_pb2_grpc.ToolGovernanceServicer):
    """gRPC servicer：把 LoopController 包装成标准 gRPC 服务。"""

    def __init__(
        self,
        controller: LoopController,
        watcher: ApprovalWatcher | None = None,
        identity_provider: IdentityProvider | None = None,
        entrypoints_config: dict[str, Any] | None = None,
    ) -> None:
        self._controller = controller
        self._watcher = watcher or ApprovalWatcher()
        self._start_time = time.time()
        self._identity_provider = identity_provider or _extract_identity_provider(controller)
        self._entrypoints_config = entrypoints_config or {}

    def _grpc_require_auth(self) -> bool:
        """读取 entrypoints.grpc.require_auth；缺省 false 保持向后兼容。"""
        grpc_cfg = self._entrypoints_config.get("grpc") or {}
        return bool(grpc_cfg.get("require_auth", False))

    def _grpc_admin_agent_ids(self) -> set[str]:
        grpc_cfg = self._entrypoints_config.get("grpc") or {}
        return set(grpc_cfg.get("admin_agent_ids") or [])

    async def _verify_identity(self, context: grpc_aio.ServicerContext) -> AgentIdentity | None:
        """验证 gRPC 客户端 mTLS 身份；无 Provider 或未提供凭证返回 None。"""
        if self._identity_provider is None:
            return None
        credential = _extract_client_cert_identity(context)
        if credential is None:
            return None
        return await self._identity_provider.verify(credential)

    async def _require_identity(self, context: grpc_aio.ServicerContext) -> AgentIdentity | None:
        """需要身份认证时校验 mTLS 身份；未通过会设置 gRPC 错误码并返回 None。"""
        identity = await self._verify_identity(context)
        if self._grpc_require_auth() and identity is None:
            context.set_code(grpc.StatusCode.UNAUTHENTICATED)
            context.set_details("client certificate required or invalid")
            return None
        return identity

    async def _require_admin_identity(
        self, context: grpc_aio.ServicerContext
    ) -> AgentIdentity | None:
        """Admin RPC 要求有效、未吊销且在 allowlist 内的 mTLS 身份。"""
        identity = await self._verify_identity(context)
        if identity is None:
            context.set_code(grpc.StatusCode.UNAUTHENTICATED)
            context.set_details("admin client certificate required or invalid")
            return None
        if identity.agent_id not in self._grpc_admin_agent_ids():
            context.set_code(grpc.StatusCode.PERMISSION_DENIED)
            context.set_details("admin identity is not authorized")
            await self._audit_admin_operation(
                identity,
                "admin_operation_failed",
                target="grpc_admin",
                metadata={"reason": "not_authorized"},
            )
            return None
        revocations = getattr(self._controller._runtime, "revocation_list", None)
        if revocations is not None:
            identity_revoked = any(
                entry.type == RevocationType.AGENT
                and entry.id == identity.agent_id
                and (entry.tenant_id is None or entry.tenant_id == identity.tenant_id)
                and (entry.expires_at is None or entry.expires_at > datetime.now(UTC))
                for entry in revocations.entries
            )
            if identity_revoked:
                context.set_code(grpc.StatusCode.PERMISSION_DENIED)
                context.set_details("admin identity is revoked")
                await self._audit_admin_operation(
                    identity,
                    "admin_operation_failed",
                    target="grpc_admin",
                    metadata={"reason": "identity_revoked"},
                )
                return None
        return identity

    async def _audit_admin_operation(
        self,
        identity: AgentIdentity,
        operation: str,
        *,
        target: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        audit_store = getattr(self._controller._runtime, "audit_store", None)
        if audit_store is None:
            return
        await audit_store.append_async(
            AuditEvent(
                event_id=uuid.uuid4().hex,
                trace_id=uuid.uuid4().hex,
                session_id="admin",
                actor_type="system",
                actor_id=identity.agent_id,
                action="admin_operation",
                target=target,
                reason=operation,
                metadata=metadata or {},
            )
        )

    async def EvaluateToolCall(
        self,
        request: governance_pb2.EvaluateToolCallRequest,
        context: grpc_aio.ServicerContext,
    ) -> governance_pb2.EvaluateToolCallResponse:
        identity = await self._verify_identity(context)
        if self._grpc_require_auth() and identity is None:
            context.set_code(grpc.StatusCode.UNAUTHENTICATED)
            context.set_details("client certificate required or invalid")
            return governance_pb2.EvaluateToolCallResponse()

        try:
            arguments = json.loads(request.arguments_json) if request.arguments_json else {}
        except json.JSONDecodeError as exc:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details(f"invalid arguments_json: {exc}")
            return governance_pb2.EvaluateToolCallResponse()

        # 使用验证后的身份；请求体中的 agent_id/user_id 仅做一致性校验。
        if identity is not None:
            if request.agent_id and request.agent_id != identity.agent_id:
                context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
                context.set_details("agent_id inconsistent with certificate identity")
                return governance_pb2.EvaluateToolCallResponse()
            if request.user_id and request.user_id != identity.user_id:
                context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
                context.set_details("user_id inconsistent with certificate identity")
                return governance_pb2.EvaluateToolCallResponse()
            agent_id = identity.agent_id
            user_id = identity.user_id
        else:
            agent_id = request.agent_id
            user_id = request.user_id

        result = await self._controller.evaluate_and_execute(
            agent_id=agent_id,
            user_id=user_id,
            tool_name=request.tool_name,
            arguments=arguments,
            task_context=request.task_context,
            session_id=request.session_id or None,
            task_id=request.task_id or None,
        )
        return _governance_result(result)

    async def ResumeAfterApproval(
        self,
        request: governance_pb2.ResumeAfterApprovalRequest,
        context: grpc_aio.ServicerContext,
    ) -> governance_pb2.EvaluateToolCallResponse:
        identity = await self._require_identity(context)
        if self._grpc_require_auth() and identity is None:
            return governance_pb2.EvaluateToolCallResponse()
        if identity is not None and not self._approval_request_belongs_to(request.request_id, identity):
            context.set_code(grpc.StatusCode.PERMISSION_DENIED)
            context.set_details("approval request does not belong to caller")
            return governance_pb2.EvaluateToolCallResponse()
        result = await self._controller.resume_after_approval(request.request_id)
        return _governance_result(result)

    async def WaitForApproval(
        self,
        request: governance_pb2.WaitForApprovalRequest,
        context: grpc_aio.ServicerContext,
    ):
        identity = await self._require_identity(context)
        if self._grpc_require_auth() and identity is None:
            return
        request_id = request.request_id
        if identity is not None and not self._approval_request_belongs_to(request_id, identity):
            context.set_code(grpc.StatusCode.PERMISSION_DENIED)
            context.set_details("approval request does not belong to caller")
            return
        max_wait = request.max_wait_seconds or 60
        max_wait = max(1, min(max_wait, 300))

        # 立即推送 pending
        yield governance_pb2.EvaluateToolCallResponse(
            status="pending",
            result="pending",
            request_id=request_id,
        )

        deadline = time.monotonic() + max_wait
        while time.monotonic() < deadline:
            result = await self._try_resume(request_id)
            if result is not None:
                yield _governance_result(result)
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            wait_time = min(1.0, remaining)
            await self._watcher.wait(request_id, timeout=wait_time)

        yield governance_pb2.EvaluateToolCallResponse(
            status="pending",
            result="pending",
            request_id=request_id,
        )

    async def GetHealth(
        self,
        request: governance_pb2.HealthRequest,
        context: grpc_aio.ServicerContext,
    ) -> governance_pb2.HealthResponse:
        opa_reachable = await self._opa_reachable()
        gateway_ready = getattr(self._controller, "started", True)
        audit_store = self._controller._runtime.audit_store
        evidence_status = getattr(audit_store, "evidence_status", "disabled")
        anchor = getattr(self._controller._runtime, "evidence_anchor", None)
        anchor_summary = (
            anchor.sanitized_status()
            if anchor is not None
            else {
                "anchor_status": "disabled",
                "anchor_stream_id": "",
                "anchor_last_success_seq": 0,
                "anchor_lag_events": 0,
                "anchor_last_error_code": "",
            }
        )
        anchor_summary = {
            key: value if value is not None else "" for key, value in anchor_summary.items()
        }
        uptime = time.time() - self._start_time
        degraded = evidence_status == "degraded" or anchor_summary["anchor_status"] not in {
            "disabled",
            "healthy",
        }
        return governance_pb2.HealthResponse(
            status="degraded" if degraded else "ok",
            opa_reachable=opa_reachable,
            gateway_ready=gateway_ready,
            uptime_seconds=uptime,
            evidence_status=evidence_status,
            **anchor_summary,
        )

    async def ListPendingApprovals(
        self,
        request: governance_pb2.ListPendingApprovalsRequest,
        context: grpc_aio.ServicerContext,
    ) -> governance_pb2.ListPendingApprovalsResponse:
        identity = await self._require_admin_identity(context)
        if identity is None:
            return governance_pb2.ListPendingApprovalsResponse()
        store = self._controller._runtime.approval_manager._store
        store.refresh()
        pending = store.get_pending()
        approvals = [
            governance_pb2.PendingApproval(
                request_id=req.request_id,
                decision_id=req.decision_id,
                tool_name=req.tool_name,
                requester_id=req.requester_id,
                reason=req.reason,
            )
            for req in pending
        ]
        return governance_pb2.ListPendingApprovalsResponse(approvals=approvals)

    async def QueryAuditEvents(
        self,
        request: governance_pb2.QueryAuditEventsRequest,
        context: grpc_aio.ServicerContext,
    ):
        identity = await self._require_admin_identity(context)
        if identity is None:
            return
        audit_store = self._controller._runtime.audit_store
        session_id = request.session_id or None
        task_id = request.task_id or None
        limit = request.limit or 100

        count = 0
        async for event in audit_store.iter_events():
            payload = event.model_dump()
            if session_id and payload.get("session_id") != session_id:
                continue
            if task_id and payload.get("task_id") != task_id:
                continue
            yield governance_pb2.AuditEvent(
                event_id=payload.get("event_id", ""),
                trace_id=payload.get("trace_id", ""),
                session_id=payload.get("session_id", ""),
                action=payload.get("action", ""),
                actor_type=payload.get("actor_type", ""),
                actor_id=payload.get("actor_id", ""),
                target=payload.get("target", ""),
                decision=payload.get("decision") or "",
                reason=payload.get("reason") or "",
                timestamp=payload.get("timestamp") or "",
                payload_json=json.dumps(payload, ensure_ascii=False),
            )
            count += 1
            if count >= limit:
                break

    async def Revoke(
        self,
        request: governance_pb2.RevokeRequest,
        context: grpc_aio.ServicerContext,
    ) -> governance_pb2.RevokeResponse:
        identity = await self._require_admin_identity(context)
        if identity is None:
            return governance_pb2.RevokeResponse()
        revocations = getattr(self._controller._runtime, "revocation_list", None)
        if revocations is None:
            context.set_code(grpc.StatusCode.UNAVAILABLE)
            context.set_details("revocation unavailable")
            return governance_pb2.RevokeResponse()
        try:
            entry_type = RevocationType(request.type)
            if request.remove:
                tenant_id = request.tenant_id or None
                removed = revocations.remove(entry_type, request.id, tenant_id)
                await self._audit_admin_operation(
                    identity,
                    "revocation_removed",
                    target=f"{entry_type.value}:{request.id}",
                    metadata={"tenant_id": tenant_id, "removed": removed},
                )
                return governance_pb2.RevokeResponse(success=True, removed=removed)
            entry = RevocationEntry(
                type=entry_type,
                id=request.id,
                reason=request.reason,
                expires_at=datetime.fromisoformat(request.expires_at)
                if request.expires_at
                else None,
                tenant_id=request.tenant_id or None,
            )
            revocations.add(entry)
            await self._audit_admin_operation(
                identity,
                "revocation_added",
                target=f"{entry.type.value}:{entry.id}",
                metadata={"tenant_id": entry.tenant_id, "reason": entry.reason},
            )
            return governance_pb2.RevokeResponse(success=True)
        except ValueError as exc:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details(str(exc))
            return governance_pb2.RevokeResponse()

    async def SetKillSwitch(
        self,
        request: governance_pb2.SetKillSwitchRequest,
        context: grpc_aio.ServicerContext,
    ) -> governance_pb2.KillSwitchResponse:
        identity = await self._require_admin_identity(context)
        if identity is None:
            return governance_pb2.KillSwitchResponse()
        revocations = getattr(self._controller._runtime, "revocation_list", None)
        if revocations is None:
            context.set_code(grpc.StatusCode.UNAVAILABLE)
            return governance_pb2.KillSwitchResponse()
        config = KillSwitchConfig(
            enabled=request.enabled,
            reason=request.reason,
            except_tools=list(request.except_tools),
            except_agents=list(request.except_agents),
        )
        revocations.set_kill_switch(config)
        await self._audit_admin_operation(
            identity,
            "kill_switch_updated",
            target="kill_switch",
            metadata=config.model_dump(mode="json"),
        )
        return governance_pb2.KillSwitchResponse(**config.model_dump())

    async def GetRevocationList(
        self,
        request: governance_pb2.GetRevocationListRequest,
        context: grpc_aio.ServicerContext,
    ) -> governance_pb2.RevocationListResponse:
        identity = await self._require_admin_identity(context)
        if identity is None:
            return governance_pb2.RevocationListResponse()
        revocations = getattr(self._controller._runtime, "revocation_list", None)
        if revocations is None:
            context.set_code(grpc.StatusCode.UNAVAILABLE)
            return governance_pb2.RevocationListResponse()
        entries = [
            governance_pb2.RevocationEntry(
                type=entry.type.value,
                id=entry.id,
                reason=entry.reason,
                revoked_at=entry.revoked_at.isoformat(),
                expires_at=entry.expires_at.isoformat() if entry.expires_at else "",
                tenant_id=entry.tenant_id or "",
            )
            for entry in revocations.entries
        ]
        config = revocations.kill_switch
        return governance_pb2.RevocationListResponse(
            revocations=entries,
            kill_switch=governance_pb2.KillSwitchResponse(**config.model_dump()),
        )

    def _approval_request_belongs_to(self, request_id: str, identity: AgentIdentity) -> bool:
        approval_request = self._controller._runtime.approval_manager.get_request_by_id(request_id)
        return approval_request is not None and (
            approval_request.agent_id == identity.agent_id
            and approval_request.requester_id == identity.user_id
        )

    async def _try_resume(self, request_id: str) -> Any | None:
        approval_manager = self._controller._runtime.approval_manager
        approval_request = approval_manager.get_request_by_id(request_id)
        if approval_request is None:
            return None
        record = approval_manager.check(approval_request.decision_id)
        if record is None:
            return None
        return await self._controller.resume_after_approval(request_id)

    async def _opa_reachable(self) -> bool:
        try:
            import httpx

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


def add_servicer_to_server(
    servicer: ToolGovernanceServicer,
    server: grpc_aio.Server,
) -> None:
    """把 servicer 注册到 gRPC server。"""
    governance_pb2_grpc.add_ToolGovernanceServicer_to_server(servicer, server)


def _load_pem(path: str | None) -> bytes | None:
    """读取 PEM 文件；path 为 None 时返回 None。"""
    if path is None:
        return None
    with open(path, "rb") as f:
        return f.read()


def _build_server_credentials(
    server_key_path: str | None,
    server_cert_path: str | None,
    client_ca_cert_path: str | None,
    require_client_cert: bool,
) -> grpc.ServerCredentials | None:
    """构造 gRPC 服务端 TLS/mTLS 凭证。"""
    if not server_key_path or not server_cert_path:
        return None
    private_key = _load_pem(server_key_path)
    certificate_chain = _load_pem(server_cert_path)
    if private_key is None or certificate_chain is None:
        raise ValueError("server_key 与 server_cert 必须同时提供")
    client_ca = _load_pem(client_ca_cert_path)
    return grpc.ssl_server_credentials(
        ((private_key, certificate_chain),),
        root_certificates=client_ca,
        require_client_auth=require_client_cert and client_ca is not None,
    )


async def serve(
    controller: LoopController,
    port: int = 50051,
    watcher: ApprovalWatcher | None = None,
    identity_provider: IdentityProvider | None = None,
    entrypoints_config: dict[str, Any] | None = None,
    server_key: str | None = None,
    server_cert: str | None = None,
    client_ca_cert: str | None = None,
    require_client_cert: bool = False,
) -> grpc_aio.Server:
    """启动 gRPC 服务并返回 server 实例。"""
    grpc_cfg = (entrypoints_config or {}).get("grpc") or {}
    grpc_auth = grpc_cfg.get("auth")
    if grpc_auth == "mtls":
        if not server_key or not server_cert:
            raise ValueError("entrypoints.grpc.auth=mtls 时必须提供 server_key 与 server_cert")
        if not client_ca_cert:
            raise ValueError("entrypoints.grpc.auth=mtls 时必须提供 client_ca_cert 以验证客户端")
        require_client_cert = True
    if require_client_cert and (not server_key or not server_cert):
        raise ValueError("require_client_cert=true 时必须提供 server_key 与 server_cert")
    if require_client_cert and not client_ca_cert:
        raise ValueError("require_client_cert=true 时必须提供 client_ca_cert 以验证客户端")
    server = grpc_aio.server()
    servicer = ToolGovernanceServicer(
        controller,
        watcher=watcher,
        identity_provider=identity_provider,
        entrypoints_config=entrypoints_config,
    )
    add_servicer_to_server(servicer, server)
    address = f"[::]:{port}"
    credentials = _build_server_credentials(
        server_key, server_cert, client_ca_cert, require_client_cert
    )
    if credentials is not None:
        server.add_secure_port(address, credentials)
        logger.info("Loop Controller gRPC server started on %s (TLS/mTLS)", address)
    else:
        server.add_insecure_port(address)
        logger.info("Loop Controller gRPC server started on %s", address)
    await server.start()
    return server
