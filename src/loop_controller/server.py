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

import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

import httpx

from loop_controller.approval_watcher import ApprovalWatcher
from loop_controller.controller import LoopController
from loop_controller.logging_config import configure_logging, set_trace_id
from loop_controller.metrics import (
    observe_request,
    observe_tool_call,
    render_metrics,
    set_pending_approvals,
)
from loop_controller.metrics import (
    set_trace_id as metrics_set_trace_id,
)
from loop_controller.server_models import (
    AuditQueryResponse,
    GovernResponse,
    GovernToolRequest,
    HealthResponse,
    PendingApprovalItem,
    PendingApprovalsResponse,
    ResumeApprovalRequest,
    WaitApprovalResponse,
)

try:
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
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


class ToolGovernServer:
    """HTTP 治理服务封装。

    Args:
        controller: 已构造的 LoopController 实例。
        api_key: 可选 API key；未设置时不校验。
        watcher: 审批事件通知器；默认新建一个。
        start_time: 服务启动时间戳（用于 uptime 计算）。
    """

    def __init__(
        self,
        controller: LoopController,
        api_key: str | None = None,
        watcher: ApprovalWatcher | None = None,
        start_time: float | None = None,
    ) -> None:
        self._controller = controller
        self._api_key = api_key
        self._watcher = watcher or ApprovalWatcher()
        self._start_time = start_time or time.time()

    def _check_auth(self, request: Request) -> bool:
        if self._api_key is None:
            return True
        header = request.headers.get("x-api-key") or ""
        auth = request.headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
        else:
            token = ""
        return header == self._api_key or token == self._api_key

    async def _handle_health(self, request: Request) -> JSONResponse:
        opa_reachable = await self._opa_reachable()
        gateway_ready = self._controller.started if hasattr(self._controller, "started") else True
        uptime = time.time() - self._start_time
        return JSONResponse(
            HealthResponse(
                status="ok",
                opa_reachable=opa_reachable,
                gateway_ready=gateway_ready,
                uptime_seconds=round(uptime, 2),
            ).model_dump()
        )

    async def _handle_metrics(self, request: Request) -> PlainTextResponse:
        data = render_metrics()
        return PlainTextResponse(content=data, media_type="text/plain; version=0.0.4; charset=utf-8")

    async def _handle_govern_tool_call(self, request: Request) -> JSONResponse:
        if not self._check_auth(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        try:
            body = GovernToolRequest(**await request.json())
        except Exception as exc:
            logger.warning("invalid tool-call request: %s", exc)
            return JSONResponse({"error": f"invalid request: {exc}"}, status_code=422)

        logger.info(
            "tool_call request agent=%s user=%s tool=%s",
            body.agent_id,
            body.user_id,
            body.tool_name,
        )
        result = await self._controller.evaluate_and_execute(
            agent_id=body.agent_id,
            user_id=body.user_id,
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
            result=result.content
            if result.content is not None
            else result.reason or result.status,
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
        if not self._check_auth(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        try:
            body = ResumeApprovalRequest(**await request.json())
        except Exception as exc:
            return JSONResponse({"error": f"invalid request: {exc}"}, status_code=422)

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
        if not self._check_auth(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        request_id = request.query_params.get("request_id")
        if not request_id:
            return JSONResponse({"error": "missing request_id"}, status_code=422)

        max_wait = float(request.query_params.get("max_wait", "30"))
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
        if not self._check_auth(request):
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

        max_wait = float(request.query_params.get("max_wait", "60"))
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

    async def _try_resume(self, request_id: str) -> Any | None:
        """尝试恢复审批；若审批不存在则返回 None。"""
        store = self._controller._runtime.approval_manager._store
        approval_request = store.get_request_by_id(request_id)
        if approval_request is None:
            return None
        record = store.get_record(approval_request.decision_id)
        if record is None:
            return None
        return await self._controller.resume_after_approval(request_id)

    async def _handle_admin_pending_approvals(self, request: Request) -> JSONResponse:
        if not self._check_auth(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        store = self._controller._runtime.approval_manager._store
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

    async def _handle_admin_audit(self, request: Request) -> JSONResponse:
        if not self._check_auth(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        session_id = request.query_params.get("session_id")
        task_id = request.query_params.get("task_id")
        limit = int(request.query_params.get("limit", "100"))

        audit_store = self._controller._runtime.audit_store
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

        # 最新事件在前
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
) -> Starlette:
    """从 LoopController 构造 Starlette ASGI 应用。"""
    if configure_logs:
        configure_logging(json_format=os.environ.get("LOOP_CONTROLLER_JSON_LOGS", "").lower() == "true")
    server = ToolGovernServer(controller, api_key=api_key, watcher=watcher)

    @asynccontextmanager
    async def lifespan(app: Starlette):
        await controller.start()
        server._start_time = time.time()
        logger.info("Loop Controller HTTP server starting")
        yield
        logger.info("Loop Controller HTTP server shutting down")
        await controller.aclose()

    return Starlette(
        debug=False,
        lifespan=lifespan,
        middleware=[Middleware(MetricsMiddleware)],
        routes=[
            Route("/health", server._handle_health, methods=["GET"]),
            Route("/metrics", server._handle_metrics, methods=["GET"]),
            Route("/v1/govern/tool-call", server._handle_govern_tool_call, methods=["POST"]),
            Route("/v1/govern/resume-after-approval", server._handle_resume_after_approval, methods=["POST"]),
            Route("/v1/wait-for-approval", server._handle_wait_for_approval, methods=["GET"]),
            Route("/v1/wait-for-approval/sse", server._handle_wait_for_approval_sse, methods=["GET"]),
            Route("/v1/admin/approvals/pending", server._handle_admin_pending_approvals, methods=["GET"]),
            Route("/v1/admin/audit", server._handle_admin_audit, methods=["GET"]),
        ],
    )


def load_api_key() -> str | None:
    """从环境变量读取 API key；未设置时返回 None。"""
    return os.environ.get("LOOP_CONTROLLER_API_KEY") or None
