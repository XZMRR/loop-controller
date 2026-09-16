"""Target-agent entrypoint stub（任务执行闭环 + 取消 + 远端回报）。

模拟一个独立 target agent 的 A2A entrypoint（如 research-agent :8001）：

- 接收 Go 内核 dispatch 的 ``POST /a2a/v1/entrypoint/tasks``（delegation token 认证）；
- 自动回调内核 ``accept`` → ``start``，驱动任务状态机前进。默认执行模式
  （``kernel_executor``）下执行事实发生在内核 executor（Python
  ``/v1/govern/tool-call``），stub 是远端 agent 的生命周期驱动替身；
- 远端回报模式（投递 payload 携带 ``execution_mode="remote_results"``）下
  内核挂起等待 results：stub 作为目标 Agent 运行时替身，用 ``tool_url`` /
  ``tool_token``（目标 Agent 自己的工作负载凭证）调用 Python 治理层执行
  真实工作，随后回调内核 entrypoint results 端点回报终态与自报消耗
  （consumed_budget，喂预算衰减链路）；
- 幂等：同一 task 的重复投递直接返回成功，不重复回调；``start`` 回调失败时
  返回 5xx，让内核 dispatcher 退避重试（重投 create 时会跳过已完成的 accept）；
- 取消：管理端经内核 ``POST /a2a/v1/tasks/{id}/cancel`` 取消时，内核经
  entrypoint client 推送 ``POST /a2a/v1/entrypoint/tasks/{id}/cancel`` 到本
  stub；stub 校验 delegation token 后置任务为 cancelled 并移出驱动集合，
  响应 ``{"task_id": ..., "status": "cancelled"}`` 供内核确认。已取消任务
  若被 dispatcher 重投 create，直接返回 200 不再驱动。

CLI::

    lc entrypoint-stub --port 8001 --kernel-url http://127.0.0.1:8080 \\
        --tool-url http://127.0.0.1:8000 --tool-token dev-token-agent-001
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


class EntrypointStub:
    """接收内核任务投递并回调 accept/start 驱动任务生命周期。

    ``remote_results`` 执行模式（投递 payload 携带 ``execution_mode``）下，
    内核不执行，任务挂起在 running；stub 作为目标 Agent 的运行时替身，
    用 ``tool_url``/``tool_token``（目标 Agent 自己的工作负载凭证）调用
    Python 治理层 ``/v1/govern/tool-call`` 执行真实工作，随后回调内核
    entrypoint results 端点回报终态与自报消耗（consumed_budget）。
    """

    def __init__(
        self,
        kernel_url: str,
        *,
        auto_accept: bool = True,
        auto_start: bool = True,
        tool_url: str = "",
        tool_token: str = "",
        client: httpx.Client | None = None,
    ) -> None:
        self.kernel_url = kernel_url.rstrip("/")
        self.auto_accept = auto_accept or auto_start
        self.auto_start = auto_start
        self.tool_url = tool_url.rstrip("/")
        self.tool_token = tool_token
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
        if payload.get("execution_mode") == "remote_results":
            # 远端回报模式：内核挂起等待 results；stub 在后台线程执行真实
            # 工作（经 Python 治理层）并回调 results 回报终态与消耗。
            if not self.tool_url:
                logger.warning("remote_results task %s dispatched without tool_url; skipping execution", task_id)
            else:
                threading.Thread(
                    target=self._execute_remote,
                    args=(task_id, payload, delegation_token),
                    daemon=True,
                ).start()
        return 201, {"status": "accepted", "task_id": task_id}

    def _execute_remote(self, task_id: str, payload: dict[str, Any], delegation_token: str) -> None:
        """目标 Agent 运行时替身：执行工作并回报 results（后台线程）。"""
        status, outcome, error_code, consumed = self._run_tool(payload)
        self._task_status[task_id] = status
        self._task_tokens.pop(task_id, None)
        body: dict[str, Any] = {
            "protocol_version": str(payload.get("protocol_version") or ""),
            "status": status,
            "consumed_budget": consumed,
        }
        if outcome is not None:
            body["outcome"] = outcome
        if error_code:
            body["error_code"] = error_code
        code, _ = self._callback(task_id, delegation_token, "results", body)
        if code not in (200, 201):
            logger.warning("results callback for %s -> %s; kernel will reject late results", task_id, code)

    def _run_tool(self, payload: dict[str, Any]) -> tuple[str, Any, str, dict[str, Any]]:
        """调用 Python 治理层执行委托工具，返回 (status, outcome, error_code, consumed)。"""
        body = {
            "agent_id": payload.get("target_agent_id"),
            "user_id": payload.get("initiator_agent_id"),
            "tool_name": payload.get("tool_name"),
            "arguments": payload.get("arguments") or {},
            "session_id": payload.get("session_id"),
            "task_id": payload.get("task_id"),
            "task_context": f"delegated task {payload.get('task_id')}",
            "allowed_tools": payload.get("allowed_tools") or [],
            "allowed_capabilities": payload.get("allowed_capabilities") or [],
            "allow_redelegation": bool(payload.get("allow_redelegation")),
            "deadline": payload.get("deadline"),
        }
        headers = {"Authorization": f"Bearer {self.tool_token}"} if self.tool_token else {}
        try:
            resp = self._client.post(f"{self.tool_url}/v1/govern/tool-call", json=body, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning("remote execution tool-call failed: %s", exc)
            return "failed", None, "executor_unreachable", {"token_count": 0}
        try:
            parsed = resp.json()
        except ValueError:
            return "failed", None, "executor_invalid_response", {"token_count": 0}
        if resp.status_code < 200 or resp.status_code >= 300:
            return "failed", None, "executor_http_error", {"token_count": 0}
        if parsed.get("status") != "allow":
            return "failed", None, str(parsed.get("error_code") or "tool_execution_failed"), {"token_count": 0}
        result = parsed.get("result")
        # Agent 自报消耗：以结果体量估算 token 消耗（真实 Agent 由自身运行时上报）
        consumed = {"token_count": len(json.dumps(result, ensure_ascii=False)) if result is not None else 0}
        return "completed", {"result": result}, "", consumed

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
    tool_url: str = "",
    tool_token: str = "",
    client: httpx.Client | None = None,
) -> Starlette:
    stub = EntrypointStub(
        kernel_url,
        auto_accept=auto_accept,
        auto_start=auto_start,
        tool_url=tool_url,
        tool_token=tool_token,
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
                "tool_url": stub.tool_url,
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
