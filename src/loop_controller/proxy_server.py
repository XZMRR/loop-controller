"""Loop Controller MCP Proxy Server（v0.5.0）。

把 Loop Controller 包装成一个 MCP Server，外部 Agent 作为 MCP Client 连接后，
所有 tool call 都经过 Checkpoint 治理，再转发到真实 MCP Server。

- stdio 传输：身份通过构造时传入的 ProxyIdentity 确定；
- SSE 传输：每个 tool call 请求可携带 HTTP header 覆盖身份，否则使用构造时身份。

v0.5.0 明确不暴露异步审批：require_approval 直接 deny 并返回审批指引。
"""

from __future__ import annotations

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

from loop_controller.models import ActionProposal, CapabilityProfile
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

        try:
            task, session = self._runtime.create_task(
                user_id=identity.user_id,
                agent_id=agent.agent_id,
                description=f"proxy call: {params.name}",
                session_id=identity.session_id,
            )
        except ValueError as exc:
            return self._error_result(str(exc))

        proposal = ActionProposal(
            task_id=task.task_id,
            call_id=uuid.uuid4().hex,
            agent_id=agent.agent_id,
            tool_name=params.name,
            arguments=dict(params.arguments or {}),
            task_context="",
        )

        try:
            decision = await self._runtime.checkpoint.evaluate(task, agent, proposal)
        except Exception as exc:
            logger.exception("Proxy evaluate 失败")
            return self._error_result(f"governance evaluation failed: {exc}")

        if decision.verdict == "require_approval":
            return self._error_result(
                f"BLOCKED: requires human approval (decision_id={decision.decision_id}). "
                "Approve via 'lc approvals approve <decision_id>' and retry."
            )

        if decision.verdict == "deny":
            return self._error_result(f"DENIED: {decision.reason}")

        try:
            result = await self._runtime.checkpoint.forward(
                proposal, decision, session_id=session.session_id
            )
        except Exception as exc:
            logger.exception("Proxy forward 失败")
            return self._error_result(f"execution failed: {exc}")

        if result.status == "success":
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=str(result.content))]
            )
        return self._error_result(str(result.content))

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

    @staticmethod
    def _error_result(message: str) -> types.CallToolResult:
        """构造错误响应；所有治理拦截和执行失败统一通过 TextContent 返回。"""
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"[loop-controller] {message}")],
            is_error=True,
        )
