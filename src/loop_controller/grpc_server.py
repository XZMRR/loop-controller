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
from typing import Any

import grpc
from grpc import aio as grpc_aio

from loop_controller.approval_watcher import ApprovalWatcher
from loop_controller.controller import LoopController
from loop_controller.v1 import governance_pb2, governance_pb2_grpc

logger = logging.getLogger("loop_controller.grpc_server")


def _governance_result(response) -> governance_pb2.EvaluateToolCallResponse:
    """把 GovernanceResult 属性映射到 gRPC response。"""
    return governance_pb2.EvaluateToolCallResponse(
        status=response.status,
        result=response.content if response.content is not None else response.reason or response.status,
        request_id=response.request_id or "",
        error_code=response.error_code or "",
    )


class ToolGovernanceServicer(governance_pb2_grpc.ToolGovernanceServicer):
    """gRPC servicer：把 LoopController 包装成标准 gRPC 服务。"""

    def __init__(
        self,
        controller: LoopController,
        watcher: ApprovalWatcher | None = None,
    ) -> None:
        self._controller = controller
        self._watcher = watcher or ApprovalWatcher()
        self._start_time = time.time()

    async def EvaluateToolCall(
        self,
        request: governance_pb2.EvaluateToolCallRequest,
        context: grpc_aio.ServicerContext,
    ) -> governance_pb2.EvaluateToolCallResponse:
        try:
            arguments = json.loads(request.arguments_json) if request.arguments_json else {}
        except json.JSONDecodeError as exc:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details(f"invalid arguments_json: {exc}")
            return governance_pb2.EvaluateToolCallResponse()

        result = await self._controller.evaluate_and_execute(
            agent_id=request.agent_id,
            user_id=request.user_id,
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
        result = await self._controller.resume_after_approval(request.request_id)
        return _governance_result(result)

    async def WaitForApproval(
        self,
        request: governance_pb2.WaitForApprovalRequest,
        context: grpc_aio.ServicerContext,
    ):
        request_id = request.request_id
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
        uptime = time.time() - self._start_time
        return governance_pb2.HealthResponse(
            status="ok",
            opa_reachable=opa_reachable,
            gateway_ready=gateway_ready,
            uptime_seconds=uptime,
        )

    async def ListPendingApprovals(
        self,
        request: governance_pb2.ListPendingApprovalsRequest,
        context: grpc_aio.ServicerContext,
    ) -> governance_pb2.ListPendingApprovalsResponse:
        store = self._controller._runtime.approval_manager._store
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

    async def _try_resume(self, request_id: str) -> Any | None:
        store = self._controller._runtime.approval_manager._store
        approval_request = store.get_request_by_id(request_id)
        if approval_request is None:
            return None
        record = store.get_record(approval_request.decision_id)
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


async def serve(
    controller: LoopController,
    port: int = 50051,
    watcher: ApprovalWatcher | None = None,
) -> grpc_aio.Server:
    """启动 gRPC 服务并返回 server 实例。"""
    server = grpc_aio.server()
    servicer = ToolGovernanceServicer(controller, watcher=watcher)
    add_servicer_to_server(servicer, server)
    address = f"[::]:{port}"
    server.add_insecure_port(address)
    await server.start()
    logger.info("Loop Controller gRPC server started on %s", address)
    return server
