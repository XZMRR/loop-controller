"""Loop Controller HTTP 服务测试（v0.17.0 / v0.18.0 / v0.19.0）。

未安装 starlette 时整个文件自动 skip；使用 TestClient 对 ASGI app 做同步调用。
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("starlette")

from starlette.testclient import TestClient

from loop_controller.approval_watcher import ApprovalWatcher
from loop_controller.controller import LoopController
from loop_controller.models import GovernanceResult
from loop_controller.server import build_app


class _MockAuditEvent:
    """极简审计事件 mock。"""

    def __init__(self, session_id: str | None, task_id: str | None):
        self.session_id = session_id
        self.task_id = task_id

    def model_dump(self) -> dict[str, Any]:
        return {"session_id": self.session_id, "task_id": self.task_id}


class _MockAuditStore:
    """异步审计存储 mock。"""

    def __init__(self, events: list[_MockAuditEvent] | None = None):
        self._events = events or []

    async def iter_events(self):
        for event in self._events:
            yield event


class _MockApprovalRequest:
    """待审批请求 mock。"""

    def __init__(
        self,
        request_id: str,
        decision_id: str,
        tool_name: str,
        requester_id: str,
        reason: str = "",
    ):
        self.request_id = request_id
        self.decision_id = decision_id
        self.tool_name = tool_name
        self.requester_id = requester_id
        self.reason = reason


class _MockApprovalStore:
    """审批存储 mock。"""

    def __init__(self, pending: list[_MockApprovalRequest] | None = None):
        self._pending = pending or []
        self._records: dict[str, Any] = {}

    def get_pending(self) -> list[_MockApprovalRequest]:
        return list(self._pending)

    def get_request_by_id(self, request_id: str) -> _MockApprovalRequest | None:
        return next((req for req in self._pending if req.request_id == request_id), None)

    def get_record(self, decision_id: str) -> Any | None:
        return self._records.get(decision_id)

    def add_record(self, decision_id: str, record: Any) -> None:
        self._records[decision_id] = record


class _MockApprovalManager:
    """审批管理器 mock。"""

    def __init__(self, store: _MockApprovalStore | None = None):
        self._store = store or _MockApprovalStore()


class _MockPolicyEngine:
    """策略引擎 mock，用于 health 检查。"""

    def __init__(self, base_url: str):
        self._base_url = base_url


class _MockCheckpoint:
    """Checkpoint mock。"""

    def __init__(self, base_url: str = "http://127.0.0.1:1"):
        self._policy_engine = _MockPolicyEngine(base_url)


class _MockRuntime:
    """Runtime mock，提供 approval_manager 与 audit_store。"""

    def __init__(
        self,
        approval_manager: _MockApprovalManager | None = None,
        audit_store: _MockAuditStore | None = None,
    ):
        self.approval_manager = approval_manager or _MockApprovalManager()
        self.audit_store = audit_store or _MockAuditStore()
        self.checkpoint = _MockCheckpoint()


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
        self._runtime = _MockRuntime()

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


def _build_client(
    api_key: str | None = None,
    watcher: ApprovalWatcher | None = None,
) -> tuple[TestClient, _MockController]:
    controller = _MockController()
    app = build_app(controller, api_key=api_key, watcher=watcher, configure_logs=False)
    return TestClient(app), controller


def test_health() -> None:
    client, _controller = _build_client()
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "opa_reachable" in data
    assert "gateway_ready" in data
    assert "uptime_seconds" in data


def test_govern_tool_call() -> None:
    client, controller = _build_client()
    payload = {
        "agent_id": "researcher_001",
        "user_id": "alice",
        "tool_name": "send_email",
        "arguments": {"to": "zhang@company.com"},
        "task_context": "发送摘要",
        "session_id": "s-001",
    }
    resp = client.post("/v1/govern/tool-call", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "allow"
    assert data["result"] == "email sent"

    assert len(controller.tool_calls) == 1
    call = controller.tool_calls[0]
    assert call["agent_id"] == "researcher_001"
    assert call["user_id"] == "alice"
    assert call["tool_name"] == "send_email"
    assert call["arguments"] == {"to": "zhang@company.com"}
    assert call["kwargs"]["task_context"] == "发送摘要"
    assert call["kwargs"]["session_id"] == "s-001"


def test_govern_tool_call_require_approval() -> None:
    client, controller = _build_client()
    controller._tool_response = GovernanceResult(
        status="require_approval",
        call_id="c1",
        tool_name="send_email",
        arguments={},
        request_id="req-42",
        reason="needs approval",
    )
    resp = client.post(
        "/v1/govern/tool-call",
        json={
            "agent_id": "researcher_001",
            "user_id": "alice",
            "tool_name": "send_email",
            "arguments": {},
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "require_approval"
    assert data["request_id"] == "req-42"


def test_govern_tool_call_validation_error() -> None:
    client, _controller = _build_client()
    resp = client.post("/v1/govern/tool-call", json={"agent_id": "x"})
    assert resp.status_code == 422


def test_resume_after_approval() -> None:
    client, controller = _build_client()
    resp = client.post("/v1/govern/resume-after-approval", json={"request_id": "req-1"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "allow"
    assert data["result"] == "email resumed"
    assert controller.resume_calls == ["req-1"]


def test_wait_for_approval_returns_result() -> None:
    client, controller = _build_client()
    store = controller._runtime.approval_manager._store
    store._pending.append(
        _MockApprovalRequest(
            request_id="req-1",
            decision_id="d-1",
            tool_name="send_email",
            requester_id="researcher_001",
        )
    )
    store.add_record("d-1", {"status": "approved"})

    resp = client.get("/v1/wait-for-approval", params={"request_id": "req-1", "max_wait": 1})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "allow"
    assert data["result"] == "email resumed"
    assert data["request_id"] == "req-1"


def test_wait_for_approval_pending_timeout() -> None:
    client, _controller = _build_client()
    resp = client.get("/v1/wait-for-approval", params={"request_id": "req-missing", "max_wait": 1})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "pending"
    assert data["request_id"] == "req-missing"


def test_wait_for_approval_sse_returns_result() -> None:
    watcher = ApprovalWatcher()
    client, controller = _build_client(watcher=watcher)
    store = controller._runtime.approval_manager._store
    store._pending.append(
        _MockApprovalRequest(
            request_id="req-1",
            decision_id="d-1",
            tool_name="send_email",
            requester_id="researcher_001",
        )
    )
    store.add_record("d-1", {"status": "approved"})

    with client.stream("GET", "/v1/wait-for-approval/sse", params={"request_id": "req-1", "max_wait": 5}) as resp:
        text = ""
        for line in resp.iter_lines():
            text += line + "\n"
            if '"status": "allow"' in line:
                break

    assert "event: pending" in text
    assert "event: result" in text
    assert '"status": "allow"' in text


def test_wait_for_approval_sse_notified() -> None:
    watcher = ApprovalWatcher()
    client, controller = _build_client(watcher=watcher)
    store = controller._runtime.approval_manager._store
    store._pending.append(
        _MockApprovalRequest(
            request_id="req-1",
            decision_id="d-1",
            tool_name="send_email",
            requester_id="researcher_001",
        )
    )
    store.add_record("d-1", {"status": "approved"})

    with client.stream("GET", "/v1/wait-for-approval/sse", params={"request_id": "req-1", "max_wait": 5}) as resp:
        text = ""
        for line in resp.iter_lines():
            text += line + "\n"
            if '"status": "allow"' in line:
                break

    assert "event: result" in text
    assert '"status": "allow"' in text


def test_wait_for_approval_sse_missing_request_id() -> None:
    client, _controller = _build_client()
    resp = client.get("/v1/wait-for-approval/sse")
    assert resp.status_code == 422
    assert b"missing request_id" in resp.content


def test_metrics_endpoint() -> None:
    client, _controller = _build_client()
    client.get("/health")
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "loop_controller_requests_total" in resp.text


def test_admin_pending_approvals() -> None:
    client, controller = _build_client()
    store = controller._runtime.approval_manager._store
    store._pending.append(
        _MockApprovalRequest(
            request_id="req-1",
            decision_id="d-1",
            tool_name="send_email",
            requester_id="researcher_001",
            reason="needs approval",
        )
    )
    resp = client.get("/v1/admin/approvals/pending")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["approvals"]) == 1
    assert data["approvals"][0]["request_id"] == "req-1"
    assert data["approvals"][0]["tool_name"] == "send_email"


def test_admin_audit_query() -> None:
    client, controller = _build_client()
    controller._runtime.audit_store = _MockAuditStore(
        [
            _MockAuditEvent(session_id="s-1", task_id="t-1"),
            _MockAuditEvent(session_id="s-2", task_id="t-2"),
        ]
    )
    resp = client.get("/v1/admin/audit", params={"session_id": "s-1", "limit": 10})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["events"]) == 1
    assert data["events"][0]["session_id"] == "s-1"


def test_api_key_auth_header() -> None:
    client, _controller = _build_client(api_key="secret")
    resp = client.post(
        "/v1/govern/tool-call",
        json={
            "agent_id": "a",
            "user_id": "u",
            "tool_name": "t",
            "arguments": {},
        },
        headers={"X-API-Key": "wrong"},
    )
    assert resp.status_code == 401


def test_api_key_bearer() -> None:
    client, _controller = _build_client(api_key="secret")
    resp = client.post(
        "/v1/govern/tool-call",
        json={
            "agent_id": "a",
            "user_id": "u",
            "tool_name": "t",
            "arguments": {},
        },
        headers={"Authorization": "Bearer secret"},
    )
    assert resp.status_code == 200


def test_api_key_protects_admin_and_wait_endpoints() -> None:
    client, _controller = _build_client(api_key="secret")
    for path in (
        "/v1/admin/approvals/pending",
        "/v1/admin/audit",
        "/v1/wait-for-approval",
        "/v1/wait-for-approval/sse",
    ):
        resp = client.get(path)
        assert resp.status_code == 401, path


def test_empty_api_key_not_bypassed() -> None:
    """P1.15：空字符串 api_key 不能被视为未配置，从而放行所有请求。"""
    client, _controller = _build_client(api_key="")
    for path in (
        "/v1/admin/approvals/pending",
        "/v1/admin/audit",
    ):
        resp = client.get(path, headers={"X-API-Key": ""})
        assert resp.status_code == 401, path
        resp = client.get(path, headers={"Authorization": "Bearer "})
        assert resp.status_code == 401, path


def test_lifespan_starts_and_closes_controller() -> None:
    client, controller = _build_client()
    with client:
        pass
    assert controller.started
    assert controller.closed
