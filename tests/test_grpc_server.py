"""Loop Controller gRPC 服务测试（v0.19.0）。

未安装 grpcio 时整个文件自动 skip；使用 in-process gRPC server 测试。
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("grpc")

import grpc
from grpc import aio as grpc_aio

from loop_controller.controller import LoopController
from loop_controller.grpc_client import ToolGovernanceClient
from loop_controller.grpc_server import (
    ToolGovernanceServicer,
    add_servicer_to_server,
    serve,
)
from loop_controller.models import GovernanceResult


class _MockController(LoopController):
    """只记录调用参数并返回预设结果的 mock。"""

    def __init__(self) -> None:  # noqa: D107
        self.tool_calls: list[dict[str, Any]] = []
        self.resume_calls: list[str] = []
        self._tool_response = GovernanceResult(
            status="allow",
            call_id="c1",
            tool_name="send_email",
            arguments={},
            content="email sent",
        )
        self._resume_response = GovernanceResult(
            status="allow",
            call_id="c2",
            tool_name="send_email",
            arguments={},
            content="email resumed",
        )
        self.started = False
        self.closed = False

    async def start(self) -> None:
        self.started = True

    async def aclose(self) -> None:
        self.closed = True

    async def evaluate_and_execute(
        self,
        *,
        agent_id: str,
        user_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        **kwargs: Any,
    ) -> GovernanceResult:
        self.tool_calls.append(
            {
                "agent_id": agent_id,
                "user_id": user_id,
                "tool_name": tool_name,
                "arguments": arguments,
                "kwargs": kwargs,
            }
        )
        return self._tool_response

    async def resume_after_approval(self, request_id: str) -> GovernanceResult:
        self.resume_calls.append(request_id)
        return self._resume_response


class _MockApprovalRequest:
    def __init__(self, request_id: str, decision_id: str, tool_name: str, requester_id: str):
        self.request_id = request_id
        self.decision_id = decision_id
        self.tool_name = tool_name
        self.requester_id = requester_id
        self.reason = ""


class _MockApprovalStore:
    def __init__(self):
        self._pending: list[_MockApprovalRequest] = []
        self._records: dict[str, Any] = {}

    def get_pending(self):
        return list(self._pending)

    def get_request_by_id(self, request_id: str):
        return next((req for req in self._pending if req.request_id == request_id), None)

    def get_record(self, decision_id: str):
        return self._records.get(decision_id)

    def add_record(self, decision_id: str, record: Any) -> None:
        self._records[decision_id] = record

    def refresh(self) -> None:
        pass


class _MockApprovalManager:
    def __init__(self):
        self._store = _MockApprovalStore()

    def get_request_by_id(self, request_id: str) -> Any | None:
        return self._store.get_request_by_id(request_id)

    def check(self, decision_id: str) -> Any | None:
        return self._store.get_record(decision_id)


class _MockRuntime:
    def __init__(self):
        self.approval_manager = _MockApprovalManager()
        self.audit_store = _MockAuditStore()


class _MockAuditEvent:
    def __init__(self, session_id: str | None, task_id: str | None):
        self.session_id = session_id
        self.task_id = task_id

    def model_dump(self):
        return {"session_id": self.session_id, "task_id": self.task_id}


class _MockAuditStore:
    def __init__(self, events: list[_MockAuditEvent] | None = None):
        self._events = events or []

    async def iter_events(self):
        for event in self._events:
            yield event


class _MockAuditStoreWithAnchor(_MockAuditStore):
    """带有 evidence_status 与 anchor_summary 的 mock audit store，用于校验 HealthResponse。"""

    def __init__(self) -> None:
        super().__init__()
        self.evidence_status = "healthy"
        self._summary = {
            "anchor_status": "healthy",
            "anchor_stream_id": "deployment/default",
            "anchor_last_success_seq": 5,
            "anchor_lag_events": 0,
            "anchor_last_error_code": "",
        }

    def anchor_summary(self) -> dict[str, object]:
        return self._summary.copy()


class _MockEvidenceAnchor:
    """让 gRPC server 的 anchor summary 来源于 audit store。"""

    def __init__(self, store: _MockAuditStoreWithAnchor) -> None:
        self._store = store

    def sanitized_status(self) -> dict[str, object]:
        return self._store.anchor_summary()


@pytest.fixture
async def grpc_client():
    controller = _MockController()
    controller._runtime = _MockRuntime()
    controller.started = True
    server = grpc_aio.server()
    servicer = ToolGovernanceServicer(controller)
    add_servicer_to_server(servicer, server)
    port = server.add_insecure_port("localhost:0")
    await server.start()
    client = ToolGovernanceClient(f"localhost:{port}")
    try:
        yield client, controller
    finally:
        await client.close()
        await server.stop(None)


@pytest.mark.asyncio
async def test_evaluate_tool_call(grpc_client) -> None:
    client, controller = grpc_client
    response = await client.evaluate_tool_call(
        agent_id="researcher_001",
        user_id="alice",
        tool_name="send_email",
        arguments={"to": "zhang@company.com"},
        task_context="发送摘要",
        session_id="s-1",
    )
    assert response.status == "allow"
    assert response.result == "email sent"
    assert len(controller.tool_calls) == 1
    call = controller.tool_calls[0]
    assert call["agent_id"] == "researcher_001"
    assert call["tool_name"] == "send_email"
    assert call["arguments"] == {"to": "zhang@company.com"}
    assert call["kwargs"]["task_context"] == "发送摘要"
    assert call["kwargs"]["session_id"] == "s-1"


@pytest.mark.asyncio
async def test_resume_after_approval(grpc_client) -> None:
    client, controller = grpc_client
    response = await client.resume_after_approval("req-1")
    assert response.status == "allow"
    assert response.result == "email resumed"
    assert controller.resume_calls == ["req-1"]


@pytest.mark.asyncio
async def test_wait_for_approval_streaming(grpc_client) -> None:
    client, controller = grpc_client
    store = controller._runtime.approval_manager._store
    store._pending.append(_MockApprovalRequest("req-1", "d-1", "send_email", "researcher_001"))
    store.add_record("d-1", {"status": "approved"})

    responses = []
    async for response in client.wait_for_approval("req-1", max_wait_seconds=5):
        responses.append(response)
        if response.status != "pending":
            break

    assert len(responses) >= 2
    assert responses[0].status == "pending"
    assert responses[-1].status == "allow"
    assert responses[-1].result == "email resumed"


@pytest.mark.asyncio
async def test_get_health(grpc_client) -> None:
    client, controller = grpc_client
    store = _MockAuditStoreWithAnchor()
    controller._runtime.audit_store = store
    controller._runtime.evidence_anchor = _MockEvidenceAnchor(store)
    response = await client.get_health()
    summary = store.anchor_summary()
    assert response.status == "ok"
    assert response.gateway_ready is True
    assert response.evidence_status == store.evidence_status
    assert response.anchor_status == summary["anchor_status"]
    assert response.anchor_stream_id == summary["anchor_stream_id"]
    assert response.anchor_last_success_seq == summary["anchor_last_success_seq"]
    assert response.anchor_lag_events == summary["anchor_lag_events"]
    assert response.anchor_last_error_code == summary["anchor_last_error_code"]


@pytest.mark.asyncio
async def test_list_pending_approvals(grpc_client) -> None:
    client, controller = grpc_client
    store = controller._runtime.approval_manager._store
    store._pending.append(_MockApprovalRequest("req-1", "d-1", "send_email", "researcher_001"))
    response = await client.list_pending_approvals()
    assert len(response.approvals) == 1
    assert response.approvals[0].request_id == "req-1"
    assert response.approvals[0].tool_name == "send_email"


@pytest.mark.asyncio
async def test_query_audit_events(grpc_client) -> None:
    client, controller = grpc_client
    controller._runtime.audit_store = _MockAuditStore(
        [
            _MockAuditEvent(session_id="s-1", task_id="t-1"),
            _MockAuditEvent(session_id="s-2", task_id="t-2"),
        ]
    )
    events = []
    async for event in client.query_audit_events(session_id="s-1", limit=10):
        events.append(event)
    assert len(events) == 1
    assert "s-1" in events[0].payload_json


@pytest.fixture
async def grpc_client_require_auth():
    """require_auth=true 但无 mTLS 凭证的 in-process gRPC server。"""
    controller = _MockController()
    controller._runtime = _MockRuntime()
    controller.started = True
    server = grpc_aio.server()
    servicer = ToolGovernanceServicer(
        controller, entrypoints_config={"grpc": {"require_auth": True}}
    )
    add_servicer_to_server(servicer, server)
    port = server.add_insecure_port("localhost:0")
    await server.start()
    client = ToolGovernanceClient(f"localhost:{port}")
    try:
        yield client, controller
    finally:
        await client.close()
        await server.stop(None)


@pytest.mark.asyncio
async def test_require_auth_evaluate_rejects_unauthenticated(
    grpc_client_require_auth,
) -> None:
    client, _controller = grpc_client_require_auth
    with pytest.raises(grpc_aio.AioRpcError) as exc_info:
        await client.evaluate_tool_call(
            agent_id="researcher_001",
            user_id="alice",
            tool_name="send_email",
            arguments={},
        )
    assert exc_info.value.code() == grpc.StatusCode.UNAUTHENTICATED


@pytest.mark.asyncio
async def test_require_auth_resume_rejects_unauthenticated(
    grpc_client_require_auth,
) -> None:
    client, _controller = grpc_client_require_auth
    with pytest.raises(grpc_aio.AioRpcError) as exc_info:
        await client.resume_after_approval("req-1")
    assert exc_info.value.code() == grpc.StatusCode.UNAUTHENTICATED


@pytest.mark.asyncio
async def test_require_auth_wait_rejects_unauthenticated(
    grpc_client_require_auth,
) -> None:
    client, _controller = grpc_client_require_auth
    with pytest.raises(grpc_aio.AioRpcError) as exc_info:
        async for _ in client.wait_for_approval("req-1", max_wait_seconds=1):
            pass
    assert exc_info.value.code() == grpc.StatusCode.UNAUTHENTICATED


@pytest.mark.asyncio
async def test_require_auth_list_pending_rejects_unauthenticated(
    grpc_client_require_auth,
) -> None:
    client, _controller = grpc_client_require_auth
    with pytest.raises(grpc_aio.AioRpcError) as exc_info:
        await client.list_pending_approvals()
    assert exc_info.value.code() == grpc.StatusCode.UNAUTHENTICATED


@pytest.mark.asyncio
async def test_require_auth_query_audit_rejects_unauthenticated(
    grpc_client_require_auth,
) -> None:
    client, _controller = grpc_client_require_auth
    with pytest.raises(grpc_aio.AioRpcError) as exc_info:
        async for _ in client.query_audit_events(limit=10):
            pass
    assert exc_info.value.code() == grpc.StatusCode.UNAUTHENTICATED


@pytest.mark.asyncio
async def test_require_auth_get_health_allows_unauthenticated(
    grpc_client_require_auth,
) -> None:
    """health 检查保持公开。"""
    client, _controller = grpc_client_require_auth
    response = await client.get_health()
    assert response.status == "ok"


@pytest.mark.asyncio
async def test_mtls_config_requires_certs() -> None:
    """v0.23.2：entrypoints.grpc.auth=mtls 但缺少证书时必须拒绝启动。"""
    controller = _MockController()
    controller._runtime = _MockRuntime()

    with pytest.raises(ValueError, match="entrypoints.grpc.auth=mtls"):
        await serve(
            controller,
            port=50051,
            entrypoints_config={"grpc": {"auth": "mtls", "require_auth": True}},
        )

    with pytest.raises(ValueError, match="entrypoints.grpc.auth=mtls"):
        await serve(
            controller,
            port=50051,
            server_key="dummy",
            server_cert="dummy",
            entrypoints_config={"grpc": {"auth": "mtls", "require_auth": True}},
        )
