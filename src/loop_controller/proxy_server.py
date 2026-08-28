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

v0.7.0 变更：
- 新增 ``loop_controller_approval_status`` 内部工具，Agent 可主动查询审批状态。
"""

from __future__ import annotations

import json
import logging
import os
import ssl
import uuid
from dataclasses import dataclass
from typing import Any, cast

import uvicorn
from mcp import types  # type: ignore[import-untyped]
from mcp.server import Server  # type: ignore[import-untyped]
from mcp.server.sse import SseServerTransport  # type: ignore[import-untyped]
from mcp.server.stdio import stdio_server  # type: ignore[import-untyped]
from starlette.applications import Starlette  # type: ignore[import-untyped]
from starlette.middleware import Middleware  # type: ignore[import-untyped]
from starlette.middleware.base import BaseHTTPMiddleware  # type: ignore[import-untyped]
from starlette.requests import Request  # type: ignore[import-untyped]
from starlette.responses import Response  # type: ignore[import-untyped]
from starlette.routing import Mount, Route  # type: ignore[import-untyped]

from loop_controller.checkpoint import CheckpointError, DecisionAlreadyConsumed
from loop_controller.identity import AgentIdentity, IdentityCredential, IdentityProvider
from loop_controller.models import (
    ActionProposal,
    ApprovalRequest,
    CapabilityProfile,
    Decision,
    ToolResult,
)
from loop_controller.runtime import Runtime

logger = logging.getLogger(__name__)

# v0.7.0：内部 MCP 工具名，用于查询审批状态。
_APPROVAL_STATUS_TOOL_NAME = "loop_controller_approval_status"

_APPROVAL_STATUS_SCHEMA = {
    "type": "object",
    "properties": {
        "decision_id": {
            "type": "string",
            "description": "require_approval 响应中的 decision_id",
        }
    },
    "required": ["decision_id"],
}


@dataclass(frozen=True)
class ProxyIdentity:
    """Proxy 侧固定的默认身份。"""

    agent_id: str
    user_id: str
    session_id: str | None = None


class _IdentityExtractionMiddleware(BaseHTTPMiddleware):
    """SSE 模式下从请求中提取并验证 mTLS 身份，写入 scope 供 MCP handler 使用。"""

    def __init__(self, app: Any, proxy: LoopControllerProxyServer) -> None:
        super().__init__(app)
        self._proxy = proxy

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        identity = await self._proxy._resolve_sse_identity(request)
        request.state.loop_controller_identity = identity
        if self._proxy._sse_require_auth() and identity is None:
            return Response(content="unauthorized", status_code=401)
        return cast(Response, await call_next(request))


class LoopControllerProxyServer:
    """Loop Controller MCP Proxy Server。

    Args:
        runtime: 已组装并启动 gateway 的 Runtime。
        identity: 默认身份；SSE 模式下可被请求 header 覆盖。
        identity_token: stdio 模式下的外部身份 token；未提供则读取环境变量。
        identity_cert: SSE 模式下服务器 TLS 证书路径。
        identity_key: SSE 模式下服务器 TLS 私钥路径。
        client_ca_cert: SSE 模式下要求客户端 mTLS 时的 CA 证书路径。
        entrypoints_config: 入口认证配置；默认空 dict（向后兼容）。
    """

    def __init__(
        self,
        runtime: Runtime,
        identity: ProxyIdentity,
        *,
        identity_token: str | None = None,
        identity_cert: str | None = None,
        identity_key: str | None = None,
        client_ca_cert: str | None = None,
        entrypoints_config: dict[str, Any] | None = None,
    ) -> None:
        self._runtime = runtime
        self._identity = identity
        self._identity_token = identity_token
        self._identity_cert = identity_cert
        self._identity_key = identity_key
        self._client_ca_cert = client_ca_cert
        self._entrypoints_config = entrypoints_config or {}
        self._server: Server[Any] | None = None
        self._agent = runtime.checkpoint._identity.get_agent(identity.agent_id)
        if self._agent is None:
            raise ValueError(f"agent_id {identity.agent_id!r} 不存在于 agents.yaml")
        profile = runtime.profiles.get(self._agent.profile_id)
        if profile is None:
            raise ValueError(f"profile {self._agent.profile_id!r} 不存在")
        self._profile = cast(CapabilityProfile, profile)

    def _identity_provider(self) -> IdentityProvider | None:
        """从 Runtime Checkpoint 提取 IdentityProvider。"""
        checkpoint = getattr(self._runtime, "checkpoint", None)
        if checkpoint is None:
            return None
        return getattr(checkpoint, "_identity", None)

    def _stdio_require_auth(self) -> bool:
        cfg = self._entrypoints_config.get("mcp_proxy_stdio") or {}
        return bool(cfg.get("require_auth", False))

    def _sse_require_auth(self) -> bool:
        cfg = self._entrypoints_config.get("mcp_proxy_sse") or {}
        return bool(cfg.get("require_auth", False))

    async def _verify_stdio_identity(self) -> None:
        """stdio 模式下校验外部身份 token；require_auth 为 false 时跳过。"""
        if not self._stdio_require_auth():
            return
        token = self._identity_token or os.environ.get("LOOP_CONTROLLER_IDENTITY_TOKEN")
        if not token:
            raise ValueError(
                "mcp_proxy_stdio.require_auth=true：必须提供 --identity-token "
                "或设置 LOOP_CONTROLLER_IDENTITY_TOKEN"
            )
        provider = self._identity_provider()
        if provider is None:
            raise ValueError("未配置 identity provider，无法验证 stdio 身份 token")
        credential = IdentityCredential(token=token)
        verified = await provider.verify(credential)
        if verified is None:
            raise ValueError("stdio 身份 token 验证失败")
        if (
            verified.agent_id != self._identity.agent_id
            or verified.user_id != self._identity.user_id
        ):
            raise ValueError("stdio 身份 token 与配置身份不一致")
        revoked, reason = self._check_revocation(verified, "")
        if revoked:
            raise ValueError(reason or "stdio identity revoked")
        logger.info(
            "stdio identity verified: agent_id=%s user_id=%s",
            verified.agent_id,
            verified.user_id,
        )

    # -- 公共入口 -----------------------------------------------------------

    async def run_stdio(self) -> None:
        """以 stdio 传输启动 MCP Proxy。"""
        await self._verify_stdio_identity()
        await self._run_stdio_async()

    def run_sse(self, host: str = "127.0.0.1", port: int = 8080) -> None:
        """以 SSE 传输启动 MCP Proxy。"""
        app = self._build_starlette_app()
        kwargs: dict[str, Any] = {"host": host, "port": port, "log_level": "warning"}
        if self._identity_cert and self._identity_key:
            kwargs["ssl_keyfile"] = self._identity_key
            kwargs["ssl_certfile"] = self._identity_cert
            if self._client_ca_cert:
                kwargs["ssl_ca_certs"] = self._client_ca_cert
                kwargs["ssl_cert_reqs"] = ssl.CERT_REQUIRED
        uvicorn.run(app, **kwargs)

    # -- Server 构建 --------------------------------------------------------

    def _build_server(self) -> Server[Any]:
        """构造低层 MCP Server 并注册 tools/list 与 tools/call。"""
        server: Server[Any] = Server("loop-controller-proxy")

        @server.list_tools()  # type: ignore[misc]
        async def _handle_list_tools(_request: types.ListToolsRequest) -> types.ListToolsResult:
            return await self._handle_list_tools_impl()

        @server.call_tool()  # type: ignore[misc]
        async def _handle_call_tool(
            name: str, arguments: dict[str, Any] | None
        ) -> types.CallToolResult:
            return await self._handle_call_tool_impl(name, arguments or {})

        self._server = server
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

    async def _resolve_sse_identity(self, request: Request) -> ProxyIdentity | None:
        """SSE 模式下解析请求身份：优先 mTLS header，再回退到显式 header/默认身份。

        mTLS header 只有在服务本身配置为要求客户端证书（client_ca_cert 已设置）
        且请求走 HTTPS 时才会被信任；否则攻击者可直接伪造这些 header。
        """
        provider = self._identity_provider()
        headers = request.headers
        # 当反向代理终止 mTLS 时，通常会把证书信息转发为以下 header。
        cert_cn = headers.get("x-ssl-client-cn")
        cert_san_header = headers.get("x-ssl-client-san")
        cert_sans = (
            [s.strip() for s in cert_san_header.split(",") if s.strip()] if cert_san_header else []
        )
        mtls_terminated = self._client_ca_cert is not None and request.url.scheme == "https"
        if mtls_terminated and provider is not None and (cert_cn or cert_sans):
            credential = IdentityCredential(cert_cn=cert_cn, cert_sans=cert_sans)
            verified = await provider.verify(credential)
            if verified is not None:
                revoked, _reason = self._check_revocation(verified, "")
                if revoked:
                    return None
                return ProxyIdentity(
                    agent_id=verified.agent_id,
                    user_id=verified.user_id,
                    session_id=headers.get(
                        "x-loop-controller-session-id", self._identity.session_id
                    ),
                )
            # 提供了 mTLS header 但验证失败：拒绝，避免 header 伪造后 fallback 到默认身份。
            return None
        identity = self._resolve_identity()
        agent = self._runtime.checkpoint._identity.get_agent(identity.agent_id)
        if agent is None:
            return None
        fallback_identity = AgentIdentity(
            agent_id=agent.agent_id,
            user_id=identity.user_id,
            harness_id=(agent.identity or {}).get("harness_id"),
            profile_id=agent.profile_id,
            tenant_id=agent.tenant_id,
        )
        revoked, _reason = self._check_revocation(fallback_identity, "")
        return None if revoked else identity

    def _build_starlette_app(self) -> Starlette:
        """构造 SSE 模式使用的 Starlette app。"""
        sse = SseServerTransport("/messages/")
        server = self._build_server()

        async def handle_sse(request: Request) -> Response:
            async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
                await server.run(streams[0], streams[1], server.create_initialization_options())
            return Response()

        return Starlette(
            middleware=[Middleware(_IdentityExtractionMiddleware, proxy=self)],
            routes=[
                Route("/sse", endpoint=handle_sse, methods=["GET"]),
                Mount("/messages/", app=sse.handle_post_message),
            ],
        )

    # -- 请求处理 -----------------------------------------------------------

    async def _handle_list_tools_impl(self) -> types.ListToolsResult:
        """返回按 Profile 过滤后的工具列表，并注入 Loop Controller 内部工具。"""
        tools = await self._runtime.gateway.list_tools(self._profile)
        mcp_tools = [
            types.Tool(
                name=tool.canonical_name,
                description=tool.description,
                inputSchema=tool.input_schema,
            )
            for tool in tools
        ]
        # v0.7.0：注入审批状态查询工具
        mcp_tools.append(
            types.Tool(
                name=_APPROVAL_STATUS_TOOL_NAME,
                description="查询 Loop Controller 审批状态。返回 pending / approved / denied / expired / not_found。",
                inputSchema=_APPROVAL_STATUS_SCHEMA,
            )
        )
        return types.ListToolsResult(tools=mcp_tools)

    async def _handle_call_tool_impl(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> types.CallToolResult:
        """把 MCP tool call 映射为 ActionProposal，经 Checkpoint 治理后转发。"""
        identity = self._resolve_identity()
        agent = self._runtime.checkpoint._identity.get_agent(identity.agent_id)
        if agent is None:
            return self._error_result(f"unknown agent_id: {identity.agent_id}")

        raw_arguments = dict(arguments)
        # v0.7.0：内部工具优先路由，不进入治理流程。
        if name == _APPROVAL_STATUS_TOOL_NAME:
            return self._handle_approval_status(raw_arguments)

        retry_decision_id = self._extract_retry_decision_id(raw_arguments)

        # v0.5.1：如果携带 decision_id，走重试恢复路径。
        if retry_decision_id:
            try:
                return await self._handle_retry(
                    retry_decision_id,
                    name,
                    raw_arguments,
                    identity,
                    agent,
                )
            except Exception as exc:
                logger.exception("Proxy retry 失败")
                return self._error_result(f"retry failed: {exc}")

        # 正常路径：创建 Task、判定、执行或提交审批。
        return await self._handle_normal_call(name, raw_arguments, identity, agent)

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
            return self._error_result(f"decision_id={decision_id} not approved or not found")

        if record.verdict == "deny":
            return self._error_result(f"approval denied for decision_id={decision_id}")

        request = self._runtime.approval_manager.get_request(decision_id)
        if request is None:
            return self._error_result(f"approval request not found for decision_id={decision_id}")

        # 参数一致性校验：防止 decision_id 被复用于不同参数调用。
        if request.tool_name != tool_name or request.tool_arguments != arguments:
            return self._error_result("retry parameters mismatch original approved request")

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

        resume_proposal = ActionProposal(
            task_id=task.task_id,
            call_id=request.call_id,
            agent_id=agent.agent_id,
            tool_name=tool_name,
            arguments=arguments,
            task_context="",
        )
        blocked = await self._handle_revocation(
            identity=identity,
            agent=agent,
            task=task,
            proposal=resume_proposal,
            stage="approval_resume",
        )
        if blocked is not None:
            return self._tool_result_to_mcp(blocked)

        # 将原始 require_approval Decision 转换为可执行的 allow/deny Decision。
        try:
            finalized_decision = self._runtime.checkpoint.finalize_after_approval(
                original_decision, record, request
            )
        except DecisionAlreadyConsumed as exc:
            return self._error_result(
                json.dumps(
                    {
                        "status": "error",
                        "error_code": "decision_already_consumed",
                        "message": str(exc),
                    },
                    ensure_ascii=False,
                )
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
        proposal = resume_proposal

        try:
            result = await self._runtime.checkpoint.forward(
                proposal,
                finalized_decision,
                session_id=identity.session_id,
                user_id=task.user_id,
                tenant_id=task.tenant_id,
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
        blocked = await self._handle_revocation(
            identity=identity,
            agent=agent,
            task=task,
            proposal=proposal,
            stage="initial",
        )
        if blocked is not None:
            return self._tool_result_to_mcp(blocked)

        try:
            decision = await self._runtime.checkpoint.evaluate(task, agent, proposal)
        except Exception as exc:
            logger.exception("Proxy evaluate 失败")
            return self._error_result(f"governance evaluation failed: {exc}")

        if decision.verdict == "require_approval":
            try:
                request = self._runtime.checkpoint.build_approval_request(decision, proposal, task)
                await self._runtime.approval_manager.submit(request)
            except Exception as exc:
                logger.exception("Proxy submit approval request 失败")
                return self._error_result(f"failed to submit approval request: {exc}")
            return self._require_approval_response(decision, request)

        if decision.verdict == "deny":
            return self._error_result(f"DENIED: {decision.reason}")

        try:
            result = await self._runtime.checkpoint.forward(
                proposal,
                decision,
                session_id=session.session_id,
                user_id=task.user_id,
                tenant_id=task.tenant_id,
            )
        except Exception as exc:
            logger.exception("Proxy forward 失败")
            return self._error_result(f"execution failed: {exc}")

        return self._tool_result_to_mcp(result)

    # -- 辅助方法 -----------------------------------------------------------

    @staticmethod
    def _secret_refs(arguments: dict[str, Any]) -> list[str]:
        refs: list[str] = []

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                for key, nested in value.items():
                    if key == "secret_ref":
                        if isinstance(nested, str):
                            refs.append(nested)
                        elif isinstance(nested, dict) and isinstance(nested.get("name"), str):
                            refs.append(nested["name"])
                    visit(nested)
            elif isinstance(value, list):
                for nested in value:
                    visit(nested)

        visit(arguments)
        return refs

    def _check_revocation(
        self,
        identity: AgentIdentity,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> tuple[bool, str | None]:
        match = self._runtime.checkpoint.check_revocation(identity, tool_name, arguments or {})
        return match.revoked, match.reason

    async def _handle_revocation(
        self,
        *,
        identity: ProxyIdentity,
        agent: Any,
        task: Any,
        proposal: ActionProposal,
        stage: str,
    ) -> ToolResult | None:
        verified = AgentIdentity(
            agent_id=agent.agent_id,
            user_id=identity.user_id,
            harness_id=(agent.identity or {}).get("harness_id"),
            profile_id=agent.profile_id,
            tenant_id=agent.tenant_id,
        )
        match = self._runtime.checkpoint.check_revocation(
            verified, proposal.tool_name, proposal.arguments
        )
        if not match.revoked:
            return None
        return await self._runtime.checkpoint.handle_revocation_block(
            identity=verified,
            proposal=proposal,
            task=task,
            match=match,
            stage=stage,
        )

    def _current_request(self) -> Request | None:
        """从 MCP RequestContext 获取当前原始请求（SSE 为 Request，stdio 为 None）。"""
        server = self._server
        if server is None:
            return None
        try:
            request = server.request_context.request
        except LookupError:
            return None
        return request if isinstance(request, Request) else None

    def _resolve_identity(self) -> ProxyIdentity:
        """解析本次请求的身份。stdio 下使用默认身份；SSE 下可被 header 覆盖。"""
        request = self._current_request()
        if request is not None:
            # SSE 模式下 middleware 已验证并写入 state
            state_identity = getattr(request.state, "loop_controller_identity", None)
            if isinstance(state_identity, ProxyIdentity):
                return state_identity
            headers = request.headers
            agent_id = headers.get("x-loop-controller-agent-id", self._identity.agent_id)
            user_id = headers.get("x-loop-controller-user-id", self._identity.user_id)
            session_id = headers.get("x-loop-controller-session-id", self._identity.session_id)
            return ProxyIdentity(agent_id=agent_id, user_id=user_id, session_id=session_id)
        return self._identity

    def _extract_retry_decision_id(
        self,
        arguments: dict[str, Any],
    ) -> str | None:
        """v0.5.1：从 SSE header 或 stdio 参数中提取重试 decision_id。

        SSE 优先读取 ``x-loop-controller-decision-id`` header；
        stdio 读取保留参数 ``_loop_controller_decision_id``（读取后从参数中剔除）。
        """
        # SSE header
        request = self._current_request()
        if request is not None:
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
            isError=True,
        )

    def _tool_result_to_mcp(self, result: ToolResult) -> types.CallToolResult:
        """把内部 ToolResult 转成 MCP CallToolResult。"""
        if result.status == "success":
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=str(result.content))]
            )
        return self._error_result(str(result.content))

    def _handle_approval_status(self, arguments: dict[str, Any]) -> types.CallToolResult:
        """v0.7.0：查询指定 decision_id 的审批状态。"""
        decision_id = arguments.get("decision_id")
        if not isinstance(decision_id, str) or not decision_id:
            return self._error_result("argument 'decision_id' is required and must be a string")

        record = self._runtime.approval_manager.check(decision_id)
        decision = self._runtime.approval_manager.get_decision(decision_id)

        # 先判断审批记录（approve / deny）
        if record is not None:
            status = "approved" if record.verdict == "approve" else "denied"
            payload = {
                "status": status,
                "decision_id": decision_id,
                "can_retry": status == "approved",
            }
            return types.CallToolResult(
                content=[
                    types.TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))
                ]
            )

        # 无审批记录时，检查 Decision 是否过期
        if decision is None:
            payload = {
                "status": "not_found",
                "decision_id": decision_id,
                "can_retry": False,
            }
            return types.CallToolResult(
                content=[
                    types.TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))
                ]
            )

        if self._runtime.checkpoint._now() >= decision.expires_at:
            payload = {
                "status": "expired",
                "decision_id": decision_id,
                "can_retry": False,
            }
            return types.CallToolResult(
                content=[
                    types.TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))
                ]
            )

        payload = {
            "status": "pending",
            "decision_id": decision_id,
            "can_retry": False,
        }
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]
        )

    @staticmethod
    def _error_result(message: str) -> types.CallToolResult:
        """构造错误响应；所有治理拦截和执行失败统一通过 TextContent 返回。"""
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"[loop-controller] {message}")],
            isError=True,
        )
