"""Target-agent entrypoint stub（任务执行闭环 + 取消）。

模拟一个独立 target agent 的 A2A entrypoint（如 research-agent :8001）：

- 接收 Go 内核 dispatch 的 ``POST /a2a/v1/entrypoint/tasks``（delegation token 认证）；
- 自动回调内核 ``accept`` → ``start``，驱动任务状态机前进。执行事实发生在
  内核 executor（Python ``/v1/govern/tool-call``），stub 是远端 agent 的
  生命周期驱动替身；
- 幂等：同一 task 的重复投递直接返回成功，不重复回调；``start`` 回调失败时
  返回 5xx，让内核 dispatcher 退避重试（重投 create 时会跳过已完成的 accept）；
- 取消：管理端经内核 ``POST /a2a/v1/tasks/{id}/cancel`` 取消时，内核经
  entrypoint client 推送 ``POST /a2a/v1/entrypoint/tasks/{id}/cancel`` 到本
  stub；stub 校验 delegation token 后置任务为 cancelled 并移出驱动集合，
  响应 ``{"task_id": ..., "status": "cancelled"}`` 供内核确认。已取消任务
  若被 dispatcher 重投 create，直接返回 200 不再驱动。

CLI::

    lc entrypoint-stub --port 8001 --kernel-url http://127.0.0.1:8080
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


class EntrypointStub:
    """接收内核任务投递并回调 accept/start 驱动任务生命周期。"""

    def __init__(
        self,
        kernel_url: str,
        *,
        auto_accept: bool = True,
        auto_start: bool = True,
        client: httpx.Client | None = None,
    ) -> None:
        self.kernel_url = kernel_url.rstrip("/")
        self.auto_accept = auto_accept or auto_start
        self.auto_start = auto_start
        self._client = client or httpx.Client(timeout=15.0)
        self._driven_tasks: set[str] = set()
        self._task_status: dict[str, str] = {}
        self._task_tokens: dict[str, str] = {}

    @property
    def driven_tasks(self) -> set[str]:
        return set(self._driven_tasks)

    @property
    def task_status(self) -> dict[str, str]:
        return dict(self._task_status)

    def handle_create(self, payload: dict[str, Any], bearer_token: str) -> tuple[int, dict[str, Any]]:
        delegation_token = str(payload.get("delegation_token") or "")
        if not bearer_token or bearer_token != delegation_token:
            return 403, {"error": "token_scope_mismatch", "detail": "bearer token does not match delegation_token"}
        task_id = str(payload.get("task_id") or "")
        if not task_id:
            return 400, {"error": "invalid_request", "detail": "task_id is required"}
        if task_id in self._driven_tasks:
            return 200, {"status": "accepted", "task_id": task_id, "idempotent": True}
        # 已取消任务不再驱动：dispatcher 重投 create 时直接确认取消结果，
        # 避免重新走 accept/start 回调导致内核 409 重试循环。
        if self._task_status.get(task_id) == "cancelled":
            return 200, {"status": "cancelled", "task_id": task_id, "idempotent": True}
        self._task_tokens[task_id] = delegation_token

        protocol_version = str(payload.get("protocol_version") or "")
        # 1) 幂等登记：回调 create 让内核写入 delegation message（accept/start
        #    回调的 token 执行范围校验依赖该 message）。重复投递返回 409 时
        #    说明任务已被接受/启动，继续后续回调。
        code, _ = self._callback_create(payload, delegation_token)
        if code == 409:
            pass
        elif code not in (200, 201):
            return 502, {"error": "create_callback_failed", "detail": f"kernel returned {code}"}
        if self.auto_accept:
            code, _ = self._callback(task_id, delegation_token, "accept", {"protocol_version": protocol_version})
            if code == 409:
                pass  # 已被其他实例接受，继续
            elif code not in (200, 201):
                return 502, {"error": "accept_callback_failed", "detail": f"kernel returned {code}"}
            else:
                self._task_status[task_id] = "accepted"
        if self.auto_start:
            code, _ = self._callback(task_id, delegation_token, "start", {"protocol_version": protocol_version})
            if code not in (200, 201):
                # 不标记已驱动，让内核 dispatcher 退避重投后重试 start
                return 502, {"error": "start_callback_failed", "detail": f"kernel returned {code}"}
            self._task_status[task_id] = "running"

        self._driven_tasks.add(task_id)
        return 201, {"status": "accepted", "task_id": task_id}

    def handle_cancel(self, task_id: str, bearer_token: str) -> tuple[int, dict[str, Any]]:
        """处理内核 entrypoint client 推送的取消通知。"""
        expected = self._task_tokens.get(task_id, "")
        if not expected or bearer_token != expected:
            return 403, {"error": "token_scope_mismatch", "detail": "bearer token does not match delegation_token"}
        current = self._task_status.get(task_id, "pending")
        if current in TERMINAL_STATUSES:
            # 幂等：已终态直接回当前状态（内核 cancelTaskExecution 依赖
            # status == "cancelled" 判定 confirmed）
            return 200, {"task_id": task_id, "status": current}
        self._task_status[task_id] = "cancelled"
        self._driven_tasks.discard(task_id)
        self._task_tokens.pop(task_id, None)
        return 200, {"task_id": task_id, "status": "cancelled"}

    def _callback_create(self, payload: dict[str, Any], token: str) -> tuple[int, dict[str, Any]]:
        url = f"{self.kernel_url}/a2a/v1/entrypoint/tasks"
        try:
            resp = self._client.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:
            logger.warning("entrypoint create callback failed: %s", exc)
            return 503, {}
        parsed: dict[str, Any] = {}
        try:
            parsed = resp.json()
        except ValueError:
            pass
        if resp.status_code not in (200, 201, 409):
            logger.warning("entrypoint create callback -> %s: %s", resp.status_code, parsed)
        return resp.status_code, parsed

    def _callback(self, task_id: str, token: str, action: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        url = f"{self.kernel_url}/a2a/v1/entrypoint/tasks/{task_id}/{action}"
        try:
            resp = self._client.post(
                url,
                json=body,
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:  # 内核不可达，交由内核 dispatcher 重试
            logger.warning("entrypoint %s callback failed: %s", action, exc)
            return 503, {}
        parsed: dict[str, Any] = {}
        try:
            parsed = resp.json()
        except ValueError:
            pass
        if resp.status_code not in (200, 201, 409):
            logger.warning("entrypoint %s callback -> %s: %s", action, resp.status_code, parsed)
        return resp.status_code, parsed


def create_app(
    kernel_url: str,
    *,
    auto_accept: bool = True,
    auto_start: bool = True,
    client: httpx.Client | None = None,
) -> Starlette:
    stub = EntrypointStub(
        kernel_url,
        auto_accept=auto_accept,
        auto_start=auto_start,
        client=client,
    )

    async def handle_tasks(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid_json"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"error": "invalid_json"}, status_code=400)
        auth = request.headers.get("Authorization", "")
        bearer = auth[7:] if auth.lower().startswith("bearer ") else ""
        status, body = stub.handle_create(payload, bearer)
        return JSONResponse(body, status_code=status)

    async def handle_task_cancel(request: Request) -> JSONResponse:
        auth = request.headers.get("Authorization", "")
        bearer = auth[7:] if auth.lower().startswith("bearer ") else ""
        status, body = stub.handle_cancel(request.path_params["task_id"], bearer)
        return JSONResponse(body, status_code=status)

    async def handle_health(request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "kernel_url": stub.kernel_url,
                "auto_accept": stub.auto_accept,
                "auto_start": stub.auto_start,
                "driven_tasks": sorted(stub.driven_tasks),
                "task_status": stub.task_status,
            }
        )

    return Starlette(
        routes=[
            Route("/a2a/v1/entrypoint/tasks", handle_tasks, methods=["POST"]),
            Route("/a2a/v1/entrypoint/tasks/{task_id}/cancel", handle_task_cancel, methods=["POST"]),
            Route("/health", handle_health, methods=["GET"]),
        ]
    )
