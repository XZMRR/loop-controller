"""Target-agent entrypoint stub（任务执行闭环）。

模拟一个独立 target agent 的 A2A entrypoint（如 research-agent :8001）：

- 接收 Go 内核 dispatch 的 ``POST /a2a/v1/entrypoint/tasks``（delegation token 认证）；
- 自动回调内核 ``accept`` → ``start``，驱动任务状态机前进。执行事实发生在
  内核 executor（Python ``/v1/govern/tool-call``），stub 是远端 agent 的
  生命周期驱动替身；
- 幂等：同一 task 的重复投递直接返回成功，不重复回调；``start`` 回调失败时
  返回 5xx，让内核 dispatcher 退避重试（重投 create 时会跳过已完成的 accept）。

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

    @property
    def driven_tasks(self) -> set[str]:
        return set(self._driven_tasks)

    def handle_create(self, payload: dict[str, Any], bearer_token: str) -> tuple[int, dict[str, Any]]:
        delegation_token = str(payload.get("delegation_token") or "")
        if not bearer_token or bearer_token != delegation_token:
            return 403, {"error": "token_scope_mismatch", "detail": "bearer token does not match delegation_token"}
        task_id = str(payload.get("task_id") or "")
        if not task_id:
            return 400, {"error": "invalid_request", "detail": "task_id is required"}
        if task_id in self._driven_tasks:
            return 200, {"status": "accepted", "task_id": task_id, "idempotent": True}

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
        if self.auto_start:
            code, _ = self._callback(task_id, delegation_token, "start", {"protocol_version": protocol_version})
            if code not in (200, 201):
                # 不标记已驱动，让内核 dispatcher 退避重投后重试 start
                return 502, {"error": "start_callback_failed", "detail": f"kernel returned {code}"}

        self._driven_tasks.add(task_id)
        return 201, {"status": "accepted", "task_id": task_id}

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

    async def handle_health(request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "kernel_url": stub.kernel_url,
                "auto_accept": stub.auto_accept,
                "auto_start": stub.auto_start,
                "driven_tasks": sorted(stub.driven_tasks),
            }
        )

    return Starlette(
        routes=[
            Route("/a2a/v1/entrypoint/tasks", handle_tasks, methods=["POST"]),
            Route("/health", handle_health, methods=["GET"]),
        ]
    )
