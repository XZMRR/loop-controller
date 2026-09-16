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

    LOOP_CONTROLLER_ENTRYPOINT_TOOL_TOKEN=<workload-token>
    lc entrypoint-stub --port 8001 --kernel-url http://127.0.0.1:8080 \\
        --tool-url http://127.0.0.1:8000

多 Agent 重委托（如 research-agent 把 calculate_checksum 转给 specialist-agent）::

    LOOP_CONTROLLER_ENTRYPOINT_CONTROL_TOKEN=<control-token>
    lc entrypoint-stub --port 8001 --agent-id research-agent \\
        --redelegate calculate_checksum=specialist-agent
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import threading
import time
from typing import Any
from uuid import uuid4

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from loop_controller.execution_security_constants import EXECUTION_RECEIPT_V1

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
        agent_id: str = "",
        control_token: str = "",
        redelegate: dict[str, str] | None = None,
        redelegate_workloads: dict[str, str] | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.kernel_url = kernel_url.rstrip("/")
        self.auto_accept = auto_accept or auto_start
        self.auto_start = auto_start
        self.tool_url = tool_url.rstrip("/")
        self.tool_token = tool_token
        # 本实例服务的 Agent 身份（重委托时作为子委托 initiator；为空时取
        # 投递 payload 的 target_agent_id）。
        self.agent_id = agent_id
        # 本 Agent 自己的内核 control 凭证（token=initiator 绑定），用于发起
        # 子委托与查询子任务状态。
        self.control_token = control_token
        # 工具 -> 下游 Agent 的重委托映射：命中映射的工具不在本地执行，
        # 而是经内核发起带子任务 lineage 的子委托（防扩大由内核校验）。
        self.redelegate = dict(redelegate or {})
        self.redelegate_workloads = dict(redelegate_workloads or {})
        self._client = client or httpx.Client(timeout=15.0)
        self._lock = threading.RLock()
        self._driven_tasks: set[str] = set()
        self._driving_tasks: set[str] = set()
        self._task_status: dict[str, str] = {}
        self._task_tokens: dict[str, str] = {}

    @property
    def driven_tasks(self) -> set[str]:
        with self._lock:
            return set(self._driven_tasks)

    @property
    def task_status(self) -> dict[str, str]:
        with self._lock:
            return dict(self._task_status)

    def handle_create(
        self, payload: dict[str, Any], bearer_token: str
    ) -> tuple[int, dict[str, Any]]:
        delegation_token = str(payload.get("delegation_token") or "")
        if not bearer_token or bearer_token != delegation_token:
            return 403, {
                "error": "token_scope_mismatch",
                "detail": "bearer token does not match delegation_token",
            }
        task_id = str(payload.get("task_id") or "")
        if not task_id:
            return 400, {"error": "invalid_request", "detail": "task_id is required"}
        with self._lock:
            if task_id in self._driven_tasks:
                return 200, {"status": "accepted", "task_id": task_id, "idempotent": True}
            if self._task_status.get(task_id) == "cancelled":
                return 200, {"status": "cancelled", "task_id": task_id, "idempotent": True}
            if task_id in self._driving_tasks:
                return 200, {"status": "accepted", "task_id": task_id, "idempotent": True}
            self._driving_tasks.add(task_id)
            self._task_tokens[task_id] = delegation_token

        protocol_version = str(payload.get("protocol_version") or "")
        try:
            code, _ = self._callback_create(payload, delegation_token)
            if code not in (200, 201, 409):
                return 502, {"error": "create_callback_failed", "detail": f"kernel returned {code}"}
            if self.auto_accept:
                code, _ = self._callback(
                    task_id, delegation_token, "accept", {"protocol_version": protocol_version}
                )
                if code not in (200, 201, 409):
                    return 502, {
                        "error": "accept_callback_failed",
                        "detail": f"kernel returned {code}",
                    }
                with self._lock:
                    if self._task_status.get(task_id) != "cancelled":
                        self._task_status[task_id] = "accepted"
            if self.auto_start:
                code, _ = self._callback(
                    task_id, delegation_token, "start", {"protocol_version": protocol_version}
                )
                if code not in (200, 201):
                    return 502, {
                        "error": "start_callback_failed",
                        "detail": f"kernel returned {code}",
                    }
                with self._lock:
                    if self._task_status.get(task_id) != "cancelled":
                        self._task_status[task_id] = "running"

            with self._lock:
                if self._task_status.get(task_id) == "cancelled":
                    return 200, {"status": "cancelled", "task_id": task_id, "idempotent": True}
                self._driven_tasks.add(task_id)
                if payload.get("execution_mode") == "remote_results":
                    tool_name = str(payload.get("tool_name") or "")
                    if not self.tool_url and tool_name not in self.redelegate:
                        logger.warning(
                            "remote_results task %s dispatched without tool_url; skipping execution",
                            task_id,
                        )
                    else:
                        threading.Thread(
                            target=self._execute_remote,
                            args=(task_id, payload, delegation_token),
                            daemon=True,
                        ).start()
            return 201, {"status": "accepted", "task_id": task_id}
        finally:
            with self._lock:
                self._driving_tasks.discard(task_id)

    def _execute_remote(self, task_id: str, payload: dict[str, Any], delegation_token: str) -> None:
        """执行远端工作；只有内核确认 results 后才记录本地终态。"""
        tool_name = str(payload.get("tool_name") or "")
        if self.redelegate and tool_name in self.redelegate:
            status, outcome, error_code, consumed = self._run_child_delegation(payload)
            receipt = terminal_status = None
        else:
            status, outcome, error_code, consumed, receipt, terminal_status = self._run_tool(
                payload
            )
        consumed = self._clamp_consumed(consumed, payload)
        with self._lock:
            if self._task_status.get(task_id) == "cancelled":
                return
        body: dict[str, Any] = {
            "protocol_version": str(payload.get("protocol_version") or ""),
            "status": status,
            "consumed_budget": consumed,
        }
        if outcome is not None:
            body["outcome"] = outcome
        if error_code:
            body["error_code"] = error_code
        if receipt is not None:
            body["execution_receipt"] = receipt
        if terminal_status is not None:
            body["terminal_status"] = terminal_status
        code = 0
        parsed: dict[str, Any] = {}
        for attempt in range(5):
            with self._lock:
                if self._task_status.get(task_id) == "cancelled":
                    return
            code, parsed = self._callback(task_id, delegation_token, "results", body)
            if code in (200, 201):
                with self._lock:
                    if self._task_status.get(task_id) != "cancelled":
                        self._task_status[task_id] = status
                return
            if code not in (409, 503) or attempt == 4:
                break
            time.sleep(0.5)
        logger.warning("results callback for %s -> %s: %s", task_id, code, parsed)

    @staticmethod
    def _clamp_consumed(consumed: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        """把自报消耗钳制到任务预算信封内，避免结算被内核 409 拒绝。"""
        budget = payload.get("budget") or {}
        try:
            cap = int(budget.get("token_count") or 0)
        except (TypeError, ValueError):
            cap = 0
        clamped = dict(consumed or {"token_count": 0})
        try:
            reported = int(clamped.get("token_count") or 0)
        except (TypeError, ValueError):
            reported = 0
        if reported > cap:
            clamped["token_count"] = cap
        return clamped

    @staticmethod
    def _delegation_jti(token: str) -> str:
        try:
            parts = token.split(".")
            if len(parts) != 3:
                return ""
            claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
            jti = claims.get("jti") if isinstance(claims, dict) else None
            return jti if isinstance(jti, str) else ""
        except (ValueError, UnicodeError, binascii.Error):
            return ""

    def _run_tool(self, payload: dict[str, Any]) -> tuple[str, Any, str, dict[str, Any], Any, Any]:
        """调用 Python 治理层执行委托工具，返回终态、结果、消耗及执行证明。"""
        token = str(payload.get("delegation_token") or "")
        body = {
            "agent_id": payload.get("target_agent_id"),
            "user_id": payload.get("initiator_agent_id"),
            "tool_name": payload.get("tool_name"),
            "arguments": payload.get("arguments") or {},
            "session_id": payload.get("session_id"),
            "task_id": payload.get("task_id"),
            "call_id": f"call-{payload.get('task_id')}",
            "request_id": payload.get("request_id"),
            "interaction_id": payload.get("interaction_id"),
            "decision_id": payload.get("decision_id"),
            "tenant_id": payload.get("tenant_id"),
            "target_workload_id": payload.get("target_workload_id"),
            "target_instance_id": payload.get("target_instance_id"),
            "delegation_token": token,
            "delegation_jti": self._delegation_jti(token),
            # 内核 dispatch payload 不带 capability 清单；strict 治理层要求非空，
            # 默认声明需要执行回执（真实 Agent 由自身运行时声明）。
            "required_security_capabilities": payload.get("required_security_capabilities")
            or [EXECUTION_RECEIPT_V1],
            "task_context": f"delegated task {payload.get('task_id')}",
            "allowed_tools": payload.get("allowed_tools") or [],
            "allowed_capabilities": payload.get("allowed_capabilities") or [],
            "allow_redelegation": bool(payload.get("allow_redelegation")),
            "deadline": payload.get("deadline"),
        }
        headers = {"Authorization": f"Bearer {self.tool_token}"} if self.tool_token else {}
        try:
            resp = self._client.post(
                f"{self.tool_url}/v1/govern/tool-call", json=body, headers=headers
            )
        except httpx.HTTPError as exc:
            logger.warning("remote execution tool-call failed: %s", exc)
            return "failed", None, "executor_unreachable", {"token_count": 0}, None, None
        try:
            parsed = resp.json()
        except ValueError:
            return "failed", None, "executor_invalid_response", {"token_count": 0}, None, None
        if resp.status_code < 200 or resp.status_code >= 300:
            return "failed", None, "executor_http_error", {"token_count": 0}, None, None
        result = parsed.get("result")
        status = "completed" if parsed.get("status") == "allow" else "failed"
        # Agent 自报消耗：以结果体量估算 token 消耗（真实 Agent 由自身运行时上报）
        consumed = {
            "token_count": len(json.dumps(result, ensure_ascii=False)) if result is not None else 0
        }
        return (
            status,
            result,
            ""
            if status == "completed"
            else str(parsed.get("error_code") or "tool_execution_failed"),
            consumed if status == "completed" else {"token_count": 0},
            parsed.get("execution_receipt"),
            parsed.get("terminal_status"),
        )

    def _run_child_delegation(
        self, payload: dict[str, Any]
    ) -> tuple[str, Any, str, dict[str, Any]]:
        """把任务按重委托映射转发给下游 Agent，等待子任务终态并聚合结果。

        子委托带 parent_task_id 与父 delegation_token，内核 deriveLineage 会
        执行父链校验（发起方必须是父任务目标、父 token 签名与 claims 一致、
        scope 交集防扩大、deadline 收窄、防环与深度限制）。
        """
        tool_name = str(payload.get("tool_name") or "")
        target = self.redelegate.get(tool_name, "")
        task_id = str(payload.get("task_id") or "")
        if not payload.get("allow_redelegation"):
            return "failed", None, "redelegation_not_allowed", {"token_count": 0}
        if not self.control_token:
            return "failed", None, "control_token_missing", {"token_count": 0}
        tenant_id = str(payload.get("tenant_id") or "")
        # 投递 payload 携带 tenant/workload 绑定即 strict 场景：strict 内核强制
        # 要求子委托带 tenant_id + target_workload_id（delegation_binding_required）。
        strict_bound = bool(tenant_id or payload.get("target_workload_id"))
        workload = self.redelegate_workloads.get(target)
        if strict_bound and not workload:
            return "failed", None, "target_workload_missing", {"token_count": 0}
        initiator = self.agent_id or str(payload.get("target_agent_id") or "")
        body = {
            "protocol_version": str(payload.get("protocol_version") or ""),
            "request_id": f"redelegation-{task_id}-{uuid4().hex[:12]}",
            "initiator_agent_id": initiator,
            "target_agent_id": target,
            "tool_name": tool_name,
            "tenant_id": tenant_id,
            "arguments": payload.get("arguments") or {},
            "session_id": payload.get("session_id") or "",
            "parent_task_id": task_id,
            "allow_redelegation": False,
            "allowed_tools": [tool_name],
            "allowed_capabilities": payload.get("allowed_capabilities") or [],
            "risk_level": "low",
            # 继承父任务预算信封：子任务结算要求 consumed <= 子任务 budget，
            # 写死零预算会导致子任务真实消耗上报时被内核 409 拒绝。
            "budget": payload.get("budget") or {"token_count": 0, "payment_amount": 0},
        }
        if workload:
            body["target_workload_id"] = workload
        headers = {"Authorization": f"Bearer {self.control_token}"}
        try:
            resp = self._client.post(
                f"{self.kernel_url}/a2a/v1/delegations", json=body, headers=headers
            )
        except httpx.HTTPError as exc:
            logger.warning("child delegation request failed: %s", exc)
            return "failed", None, "kernel_unreachable", {"token_count": 0}
        try:
            parsed = resp.json()
        except ValueError:
            return "failed", None, "kernel_invalid_response", {"token_count": 0}
        if resp.status_code < 200 or resp.status_code >= 300 or not parsed.get("allowed"):
            reason = str(parsed.get("reason") or parsed.get("error") or "child_delegation_denied")
            return (
                "failed",
                {"reason": reason, "child_verdict": parsed.get("verdict", "")},
                "child_delegation_denied",
                {"token_count": 0},
            )
        child_id = str(parsed.get("task_id") or "")
        if not child_id:
            return "failed", None, "child_task_missing", {"token_count": 0}
        child = self._wait_child_task(child_id)
        if child is None:
            return "failed", {"child_task_id": child_id}, "child_task_timeout", {"token_count": 0}
        status = str(child.get("status") or "failed")
        consumed = child.get("consumed_budget") or {"token_count": 0}
        if status == "completed":
            return (
                "completed",
                {"child_task_id": child_id, "child_outcome": child.get("outcome")},
                "",
                consumed,
            )
        if status == "cancelled":
            return "cancelled", {"child_task_id": child_id}, "child_task_cancelled", consumed
        return (
            "failed",
            {"child_task_id": child_id},
            str(child.get("error_code") or "child_task_failed"),
            consumed,
        )

    def _wait_child_task(
        self, child_id: str, timeout_seconds: float = 60.0
    ) -> dict[str, Any] | None:
        """轮询子任务直至终态（父任务挂起期间子任务独立推进）。"""
        headers = {"Authorization": f"Bearer {self.control_token}"}
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                resp = self._client.get(
                    f"{self.kernel_url}/a2a/v1/tasks/{child_id}", headers=headers
                )
            except httpx.HTTPError as exc:
                logger.warning("child task poll failed: %s", exc)
                time.sleep(0.5)
                continue
            if resp.status_code != 200:
                time.sleep(0.5)
                continue
            try:
                task: Any = resp.json()
            except ValueError:
                time.sleep(0.5)
                continue
            if isinstance(task, dict) and task.get("status") in TERMINAL_STATUSES:
                return task
            time.sleep(0.5)
        return None

    def handle_cancel(self, task_id: str, bearer_token: str) -> tuple[int, dict[str, Any]]:
        """处理内核 entrypoint client 推送的取消通知。"""
        with self._lock:
            expected = self._task_tokens.get(task_id, "")
            if not expected or bearer_token != expected:
                return 403, {
                    "error": "token_scope_mismatch",
                    "detail": "bearer token does not match delegation_token",
                }
            current = self._task_status.get(task_id, "pending")
            if current in ("completed", "failed"):
                return 200, {"task_id": task_id, "status": current}
            self._task_status[task_id] = "cancelled"
            self._driven_tasks.discard(task_id)
            self._driving_tasks.discard(task_id)
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

    def _callback(
        self, task_id: str, token: str, action: str, body: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
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
    agent_id: str = "",
    control_token: str = "",
    redelegate: dict[str, str] | None = None,
    redelegate_workloads: dict[str, str] | None = None,
    client: httpx.Client | None = None,
) -> Starlette:
    stub = EntrypointStub(
        kernel_url,
        auto_accept=auto_accept,
        auto_start=auto_start,
        tool_url=tool_url,
        tool_token=tool_token,
        agent_id=agent_id,
        control_token=control_token,
        redelegate=redelegate,
        redelegate_workloads=redelegate_workloads,
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
                "agent_id": stub.agent_id,
                "auto_accept": stub.auto_accept,
                "auto_start": stub.auto_start,
                "tool_url": stub.tool_url,
                "redelegate": dict(stub.redelegate),
                "driven_tasks": sorted(stub.driven_tasks),
                "task_status": stub.task_status,
            }
        )

    return Starlette(
        routes=[
            Route("/a2a/v1/entrypoint/tasks", handle_tasks, methods=["POST"]),
            Route(
                "/a2a/v1/entrypoint/tasks/{task_id}/cancel", handle_task_cancel, methods=["POST"]
            ),
            Route("/health", handle_health, methods=["GET"]),
        ]
    )
