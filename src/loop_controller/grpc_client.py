"""Loop Controller gRPC 客户端封装（v0.19.0）。

本模块属于可选扩展，需要额外安装 grpc 依赖：

    uv pip install "loop-controller[grpc]"

使用方式：

    from loop_controller.grpc_client import ToolGovernanceClient

    client = ToolGovernanceClient("localhost:50051")
    response = await client.evaluate_tool_call(
        agent_id="researcher_001",
        user_id="alice",
        tool_name="send_email",
        arguments={"to": "zhang@company.com", "subject": "x", "body": "y"},
    )
    print(response.status, response.result)
"""

from __future__ import annotations

from typing import Any

from loop_controller.v1 import governance_pb2, governance_pb2_grpc

try:
    from grpc import aio as grpc_aio
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "使用 loop_controller.grpc_client 需要先安装 grpc 依赖: "
        "uv pip install 'loop-controller[grpc]'"
    ) from exc


class ToolGovernanceClient:
    """Loop Controller gRPC 客户端。"""

    def __init__(self, target: str, channel: grpc_aio.Channel | None = None) -> None:
        self._channel = channel or grpc_aio.insecure_channel(target)
        self._stub = governance_pb2_grpc.ToolGovernanceStub(self._channel)

    async def evaluate_tool_call(
        self,
        *,
        agent_id: str,
        user_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        task_context: str = "",
        session_id: str | None = None,
        task_id: str | None = None,
    ) -> governance_pb2.EvaluateToolCallResponse:
        """调用 EvaluateToolCall。"""
        import json

        request = governance_pb2.EvaluateToolCallRequest(
            agent_id=agent_id,
            user_id=user_id,
            tool_name=tool_name,
            arguments_json=json.dumps(arguments, ensure_ascii=False),
            task_context=task_context,
            session_id=session_id or "",
            task_id=task_id or "",
        )
        return await self._stub.EvaluateToolCall(request)

    async def resume_after_approval(
        self, request_id: str
    ) -> governance_pb2.EvaluateToolCallResponse:
        """调用 ResumeAfterApproval。"""
        request = governance_pb2.ResumeAfterApprovalRequest(request_id=request_id)
        return await self._stub.ResumeAfterApproval(request)

    async def wait_for_approval(
        self, request_id: str, max_wait_seconds: int = 60
    ):
        """调用 WaitForApproval server-streaming，异步迭代响应。"""
        request = governance_pb2.WaitForApprovalRequest(
            request_id=request_id,
            max_wait_seconds=max_wait_seconds,
        )
        async for response in self._stub.WaitForApproval(request):
            yield response

    async def get_health(self) -> governance_pb2.HealthResponse:
        """调用 GetHealth。"""
        return await self._stub.GetHealth(governance_pb2.HealthRequest())

    async def list_pending_approvals(self) -> governance_pb2.ListPendingApprovalsResponse:
        """调用 ListPendingApprovals。"""
        return await self._stub.ListPendingApprovals(
            governance_pb2.ListPendingApprovalsRequest()
        )

    async def query_audit_events(
        self,
        *,
        session_id: str | None = None,
        task_id: str | None = None,
        limit: int = 100,
    ):
        """调用 QueryAuditEvents server-streaming，异步迭代事件。"""
        request = governance_pb2.QueryAuditEventsRequest(
            session_id=session_id or "",
            task_id=task_id or "",
            limit=limit,
        )
        async for event in self._stub.QueryAuditEvents(request):
            yield event

    async def close(self) -> None:
        """关闭 gRPC channel。"""
        await self._channel.close()
