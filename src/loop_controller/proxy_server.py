"""Loop Controller MCP Proxy Server（v0.5.1）。

把 Loop Controller 包装成一个 MCP Server，外部 Agent 作为 MCP Client 连接后，
所有 tool call 都经过 Checkpoint 治理，再转发到真实 MCP Server。

- stdio 传输：身份通过构造时传入的 ProxyIdentity 确定；
  重试 decision_id 通过保留参数 ``_loop_controller_decision_id`` 传入。
- SSE 传输：每个 tool call 请求可携带 HTTP header 覆盖身份，
  重试 decision_id 通过 ``x-loop-controller-decision-id`` header 传入。

v0.5.1 变更：
- ``require_approval`` 返回结构化 JSON，便于 Agent 解析；
- 审批通过后 Agent 可携带 ``decision_id`` 重试，Proxy 会执行原调用；
- 重试时校验 tool 参数与原始审批请求一致，防止 decision_id 被复用于不同调用。
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any, cast

import anyio
import mcp_types as types  # type: ignore[import-not-found]
import uvicorn
from mcp.server import Server  # type: ignore[import-untyped]
from mcp.server.context import ServerRequestContext  # type: ignore[import-not-found]
from mcp.server.sse import SseServerTransport  # type: ignore[import-untyped]
from mcp.server.stdio import stdio_server  # type: ignore[import-untyped]
from starlette.applications import Starlette  # type: ignore[import-untyped]
from starlette.requests import Request  # type: ignore[import-untyped]
from starlette.responses import Response  # type: ignore[import-untyped]
from starlette.routing import Mount, Route  # type: ignore[import-untyped]

from loop_controller.checkpoint import CheckpointError
from loop_controller.models import (
    ActionProposal,
    ApprovalRequest,
    CapabilityProfile,
    Decision,
    ToolResult,
)
from loop_controller.runtime import Runtime

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProxyIdentity:
    """Proxy 侧固定的默认身份。"""

    agent_id: str
    user_id: str
    session_id: str | None = None


class LoopControllerProxyServer:
    """Loop Controller MCP Proxy Server。

    Args:
        runtime: 已组装并启动 gateway 的 Runtime。
        identity: 默认身份；SSE 模式下可被请求 header 覆盖。
    """

    def __init__(self, runtime: Runtime, identity: ProxyIdentity) -> None:
        self._runtime = runtime
        self._identity = identity
        self._agent = runtime.checkpoint._identity.get_agent(identity.agent_id)
        if self._agent is None:
            raise ValueError(f"agent_id {identity.agent_id!r} 不存在于 agents.yaml")
        profile = runtime.profiles.get(self._agent.profile_id)
        if profile is None:
            raise ValueError(f"profile {self._agent.profile_id!r} 不存在")
        self._profile = cast(CapabilityProfile, profile)

    # -- 公共入口 -----------------------------------------------------------

    def run_stdio(self) -> None:
        """以 stdio 传输启动 MCP Proxy。"""
        anyio.run(self._run_stdio_async)

    def run_sse(self, host: str = "127.0.0.1", port: int = 8080) -> None:
        """以 SSE 传输启动 MCP Proxy。"""
        app = self._build_starlette_app()
        uvicorn.run(app, host=host, port=port, log_level="warning")

    # -- Server 构建 --------------------------------------------------------

    def _build_server(self) -> Server[Any]:
        """构造低层 MCP Server 并注册 tools/list 与 tools/call。"""
        server: Server[Any] = Server("loop-controller-proxy")
        server.add_request_handler(  # type: ignore[attr-defined]
            "tools/list",
            types.PaginatedRequestParams,
            self._handle_list_tools,
        )
        server.add_request_handler(  # type: ignore[attr-defined]
            "tools/call",
            types.CallToolRequestParams,
            self._handle_call_tool,
        )
        return server

    async def _run_stdio_async(self) -> None:
        """stdio 启动协程。"""
        server = self._build_server()
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )

    def _build_starlette_app(self) -> Starlette:
        """构造 SSE 模式使用的 Starlette app。"""
        sse = SseServerTransport("/messages/")
        server = self._build_server()

        async def handle_sse(request: Request) -> Response:
            async with sse.connect_sse(
                request.scope, request.receive, request._send
            ) as streams:
                await server.run(
                    streams[0], streams[1], server.create_initialization_options()
                )
            return Response()

        return Starlette(
            routes=[
                Route("/sse", endpoint=handle_sse, methods=["GET"]),
                Mount("/messages/", app=sse.handle_post_message),
            ]
        )

    # -- 请求处理 -----------------------------------------------------------

    async def _handle_list_tools(
        self,
        ctx: ServerRequestContext[Any, Any],
        params: types.PaginatedRequestParams | None,
    ) -> types.ListToolsResult:
        """返回按 Profile 过滤后的工具列表。"""
        tools = await self._runtime.gateway.list_tools(self._profile)
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=tool.canonical_name,
                    description=tool.description,
                    input_schema=tool.input_schema,
                )
                for tool in tools
            ]
        )

    async def _handle_call_tool(
        self,
        ctx: ServerRequestContext[Any, Any],
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult:
        """把 MCP tool call 映射为 ActionProposal，经 Checkpoint 治理后转发。"""
        identity = self._resolve_identity(ctx)
        agent = self._runtime.checkpoint._identity.get_agent(identity.agent_id)
        if agent is None:
            return self._error_result(f"unknown agent_id: {identity.agent_id}")

        raw_arguments = dict(params.arguments or {})
        retry_decision_id = self._extract_retry_decision_id(ctx, raw_arguments)

        # v0.5.1：如果携带 decision_id，走重试恢复路径。
        if retry_decision_id:
            try:
                return await self._handle_retry(
                    retry_decision_id,
                    params.name,
                    raw_arguments,
                    identity,
                    agent,
                )
            except Exception as exc:
                logger.exception("Proxy retry 失败")
                return self._error_result(f"retry failed: {exc}")

        # 正常路径：创建 Task、判定、执行或提交审批。
        return await self._handle_normal_call(params.name, raw_arguments, identity, agent)

    async def _handle_retry(
        self,
        decision_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        identity: ProxyIdentity,
        agent: Any,
    ) -> types.CallToolResult:
        """v0.5.1：审批通过后携带 decision_id 重试。"""
        record = self._runtime.approval_manager.check(decision_id)
        if record is None:
            return self._error_result(
                f"decision_id={decision_id} not approved or not found"
            )

        if record.verdict == "deny":
            return self._error_result(f"approval denied for decision_id={decision_id}")

        request = self._runtime.approval_manager.get_request(decision_id)
        if request is None:
            return self._error_result(
                f"approval request not found for decision_id={decision_id}"
            )

        # 参数一致性校验：防止 decision_id 被复用于不同参数调用。
        if request.tool_name != tool_name or request.tool_arguments != arguments:
            return self._error_result(
                "retry parameters mismatch original approved request"
            )

        original_decision = request.original_decision
        if original_decision is None:
            return self._error_result(
                f"original decision not recorded for decision_id={decision_id}"
            )

        # v0.6.0：通过持久化 TaskStore 恢复原始 Task。
        task = self._runtime.get_task(request.task_id)
        if task is None:
            return self._error_result(
                "original task not available; please retry without decision_id"
            )

        # 将原始 require_approval Decision 转换为可执行的 allow/deny Decision。
        try:
            finalized_decision = self._runtime.checkpoint.finalize_after_approval(
                original_decision, record, request
            )
        except CheckpointError as exc:
            return self._error_result(str(exc))
        except Exception as exc:
            logger.exception("Proxy finalize_after_approval 失败")
            return self._error_result(f"finalize failed: {exc}")

        if finalized_decision.verdict == "deny":
            return self._error_result(
                f"approval denied for decision_id={decision_id}: {finalized_decision.reason}"
            )

        # 重试时必须复用原始 call_id，否则 forward 会判定 decision.call_id 不一致。
        proposal = ActionProposal(
            task_id=task.task_id,
            call_id=request.call_id,
            agent_id=agent.agent_id,
            tool_name=tool_name,
            arguments=arguments,
            task_context="",
        )

        try:
            result = await self._runtime.checkpoint.forward(
                proposal, finalized_decision, session_id=identity.session_id
            )
        except CheckpointError as exc:
            return self._error_result(str(exc))
        except Exception as exc:
            logger.exception("Proxy retry forward 失败")
            return self._error_result(f"execution failed: {exc}")

        return self._tool_result_to_mcp(result)

    async def _handle_normal_call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        identity: ProxyIdentity,
        agent: Any,
    ) -> types.CallToolResult:
        """v0.5.1：正常 tool call 路径。"""
        try:
            task, session = self._runtime.create_task(
                user_id=identity.user_id,
                agent_id=agent.agent_id,
                description=f"proxy call: {tool_name}",
                session_id=identity.session_id,
            )
        except ValueError as exc:
            return self._error_result(str(exc))

        proposal = ActionProposal(
            task_id=task.task_id,
            call_id=uuid.uuid4().hex,
            agent_id=agent.agent_id,
            tool_name=tool_name,
            arguments=arguments,
            task_context="",
        )

        try:
            decision = await self._runtime.checkpoint.evaluate(task, agent, proposal)
        except Exception as exc:
            logger.exception("Proxy evaluate 失败")
            return self._error_result(f"governance evaluation failed: {exc}")

        if decision.verdict == "require_approval":
            try:
                request = self._runtime.checkpoint.build_approval_request(
                    decision, proposal, task
                )
                await self._runtime.approval_manager.submit(request)
            except Exception as exc:
                logger.exception("Proxy submit approval request 失败")
                return self._error_result(f"failed to submit approval request: {exc}")
            return self._require_approval_response(decision, request)

        if decision.verdict == "deny":
            return self._error_result(f"DENIED: {decision.reason}")

        try:
            result = await self._runtime.checkpoint.forward(
                proposal, decision, session_id=session.session_id
            )
        except Exception as exc:
            logger.exception("Proxy forward 失败")
            return self._error_result(f"execution failed: {exc}")

        return self._tool_result_to_mcp(result)

    # -- 辅助方法 -----------------------------------------------------------

    def _resolve_identity(self, ctx: ServerRequestContext[Any, Any]) -> ProxyIdentity:
        """解析本次请求的身份。stdio 下使用默认身份；SSE 下可被 header 覆盖。"""
        request = getattr(ctx, "request", None)
        if isinstance(request, Request):
            headers = request.headers
            agent_id = headers.get("x-loop-controller-agent-id", self._identity.agent_id)
            user_id = headers.get("x-loop-controller-user-id", self._identity.user_id)
            session_id = headers.get("x-loop-controller-session-id", self._identity.session_id)
            return ProxyIdentity(agent_id=agent_id, user_id=user_id, session_id=session_id)
        return self._identity

    def _extract_retry_decision_id(
        self,
        ctx: ServerRequestContext[Any, Any],
        arguments: dict[str, Any],
    ) -> str | None:
        """v0.5.1：从 SSE header 或 stdio 参数中提取重试 decision_id。

        SSE 优先读取 ``x-loop-controller-decision-id`` header；
        stdio 读取保留参数 ``_loop_controller_decision_id``（读取后从参数中剔除）。
        """
        # SSE header
        request = getattr(ctx, "request", None)
        if isinstance(request, Request):
            header_id = request.headers.get("x-loop-controller-decision-id")
            if header_id:
                return header_id

        # stdio 保留参数
        reserved_key = "_loop_controller_decision_id"
        if reserved_key in arguments:
            decision_id = arguments.pop(reserved_key)
            if isinstance(decision_id, str):
                return decision_id

        # 兼容 fallback：参数中显式出现 "decision_id"（不推荐，可能与工具参数冲突）
        fallback = arguments.get("decision_id")
        if isinstance(fallback, str):
            return fallback

        return None

    def _require_approval_response(
        self,
        decision: Decision,
        request: ApprovalRequest,
    ) -> types.CallToolResult:
        """v0.5.1：构造结构化的 require_approval 响应。"""
        payload = {
            "status": "require_approval",
            "decision_id": decision.decision_id,
            "request_id": request.request_id,
            "tool_name": request.tool_name,
            "reason": decision.reason,
            "expires_at": decision.expires_at.isoformat(),
            "retry_instruction": (
                f"Approve via 'lc approvals approve {request.request_id}', "
                f"then retry with x-loop-controller-decision-id: {decision.decision_id}"
            ),
        }
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
            is_error=True,
        )

    def _tool_result_to_mcp(self, result: ToolResult) -> types.CallToolResult:
        """把内部 ToolResult 转成 MCP CallToolResult。"""
        if result.status == "success":
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=str(result.content))]
            )
        return self._error_result(str(result.content))

    @staticmethod
    def _error_result(message: str) -> types.CallToolResult:
        """构造错误响应；所有治理拦截和执行失败统一通过 TextContent 返回。"""
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"[loop-controller] {message}")],
            is_error=True,
        )
