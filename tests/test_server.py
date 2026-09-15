"""Loop Controller HTTP 服务测试（v0.17.0 / v0.18.0 / v0.19.0）。

未安装 starlette 时整个文件自动 skip；使用 TestClient 对 ASGI app 做同步调用。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytest.importorskip("starlette")

from starlette.testclient import TestClient

from loop_controller.approval_watcher import ApprovalWatcher
from loop_controller.controller import LoopController
from loop_controller.identity import ConfigIdentityProvider
from loop_controller.models import Agent, ApprovalRequest, AuditEvent, GovernanceResult
from loop_controller.server import build_app


class _MockAuditEvent:
    """极简审计事件 mock。"""

    def __init__(
        self,
        session_id: str | None,
        task_id: str | None,
        agent_id: str | None = None,
        tool_name: str | None = None,
    ):
        self.session_id = session_id
        self.task_id = task_id
        self.agent_id = agent_id
        self.tool_name = tool_name

    def model_dump(self, mode: str | None = None) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "tool_name": self.tool_name,
        }


class _MockAuditStore:
    """异步审计存储 mock。"""

    def __init__(self, events: list[_MockAuditEvent] | None = None):
        self._events = events or []

    async def append_async(self, event: Any) -> None:
        self._events.append(event)

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
        agent_id: str = "researcher_001",
    ):
        self.request_id = request_id
        self.decision_id = decision_id
        self.tool_name = tool_name
        self.requester_id = requester_id
        self.agent_id = agent_id
        self.reason = reason


class _MockApprovalStore:
    """审批存储 mock。"""

    def __init__(self, pending: list[_MockApprovalRequest] | None = None):
        self._pending = pending or []
        self._records: dict[str, Any] = {}

    def submit_request(self, request: Any) -> None:
        self._pending.append(request)

    def get_pending(self) -> list[_MockApprovalRequest]:
        return list(self._pending)

    def get_request_by_id(self, request_id: str) -> _MockApprovalRequest | None:
        return next((req for req in self._pending if req.request_id == request_id), None)

    def get_request(self, decision_id: str) -> Any | None:
        return next((req for req in self._pending if req.decision_id == decision_id), None)

    def record_response(self, record: Any) -> None:
        self.add_record(record.decision_id, record)

    def get_record(self, decision_id: str) -> Any | None:
        return self._records.get(decision_id)

    def add_record(self, decision_id: str, record: Any) -> None:
        self._records[decision_id] = record

    @property
    def requests(self) -> dict[str, Any]:
        return {req.decision_id: req for req in self._pending}

    @property
    def responses(self) -> dict[str, Any]:
        return dict(self._records)

    def refresh(self) -> None:
        pass


class _MockApprovalManager:
    """审批管理器 mock。"""

    def __init__(self, store: _MockApprovalStore | None = None):
        self._store = store or _MockApprovalStore()

    def get_request_by_id(self, request_id: str) -> Any | None:
        return self._store.get_request_by_id(request_id)

    def check(self, decision_id: str) -> Any | None:
        return self._store.get_record(decision_id)


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
        self.harness_executor = None


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
    identity_provider: ConfigIdentityProvider | None = None,
    entrypoints_config: dict[str, Any] | None = None,
) -> tuple[TestClient, _MockController]:
    controller = _MockController()
    app = build_app(
        controller,
        api_key=api_key,
        watcher=watcher,
        configure_logs=False,
        identity_provider=identity_provider,
        entrypoints_config=entrypoints_config,
    )
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


def test_govern_tool_call_enforces_delegated_allowed_tools() -> None:
    client, controller = _build_client()
    resp = client.post(
        "/v1/govern/tool-call",
        json={
            "agent_id": "researcher_001",
            "user_id": "alice",
            "tool_name": "send_email",
            "arguments": {},
            "task_id": "delegated-task-1",
            "allowed_tools": ["read_file"],
            "allowed_capabilities": [],
            "allow_redelegation": False,
        },
    )
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "delegation_scope_denied"
    assert controller.tool_calls == []


def test_govern_tool_call_rejects_expired_delegation_deadline() -> None:
    client, controller = _build_client()
    resp = client.post(
        "/v1/govern/tool-call",
        json={
            "agent_id": "researcher_001",
            "user_id": "alice",
            "tool_name": "send_email",
            "arguments": {},
            "task_id": "delegated-task-expired",
            "allowed_tools": ["send_email"],
            "allowed_capabilities": [],
            "deadline": "2020-01-01T00:00:00Z",
        },
    )
    assert resp.status_code == 409
    assert resp.json()["error_code"] == "task_deadline_expired"
    assert controller.tool_calls == []


def test_govern_tool_call_accepts_tool_within_delegated_scope() -> None:
    client, controller = _build_client()
    resp = client.post(
        "/v1/govern/tool-call",
        json={
            "agent_id": "researcher_001",
            "user_id": "alice",
            "tool_name": "send_email",
            "arguments": {},
            "task_id": "delegated-task-1",
            "allowed_tools": ["send_email"],
            "allowed_capabilities": [],
        },
    )
    assert resp.status_code == 200
    assert len(controller.tool_calls) == 1


@pytest.mark.parametrize(
    "delegation_context",
    [
        {"task_id": "delegated-task-1"},
        {
            "task_id": "delegated-task-1",
            "allowed_tools": ["send_email"],
        },
        {
            "task_id": "delegated-task-1",
            "allowed_capabilities": [],
        },
        {
            "allowed_tools": ["send_email"],
            "allowed_capabilities": [],
        },
    ],
)
def test_govern_tool_call_rejects_incomplete_delegated_scope(
    delegation_context: dict[str, Any],
) -> None:
    client, controller = _build_client()
    resp = client.post(
        "/v1/govern/tool-call",
        json={
            "agent_id": "researcher_001",
            "user_id": "alice",
            "tool_name": "send_email",
            "arguments": {},
            **delegation_context,
        },
    )
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "delegation_scope_denied"
    assert controller.tool_calls == []


def test_govern_tool_call_enforces_delegated_capabilities() -> None:
    from loop_controller.infra.config_loader import (
        CapabilityDef,
        CapabilityProducer,
        CapabilityRules,
    )

    client, controller = _build_client()
    controller._runtime.config = type(
        "Config",
        (),
        {
            "capability_rules": CapabilityRules(
                capabilities={
                    "send_external": CapabilityDef(
                        name="send_external",
                        produced_by=[CapabilityProducer(tool="send_email")],
                    )
                },
                combination_rules=[],
            )
        },
    )()
    resp = client.post(
        "/v1/govern/tool-call",
        json={
            "agent_id": "researcher_001",
            "user_id": "alice",
            "tool_name": "send_email",
            "arguments": {},
            "task_id": "delegated-task-1",
            "allowed_tools": ["send_email"],
            "allowed_capabilities": [],
        },
    )
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "delegation_scope_denied"
    assert controller.tool_calls == []


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


def test_wait_and_resume_reject_request_from_other_identity() -> None:
    provider = ConfigIdentityProvider(
        agents={
            "researcher_001": Agent(
                agent_id="researcher_001",
                name="Researcher",
                profile_id="default",
                owner_id="alice",
            ),
            "other_agent": Agent(
                agent_id="other_agent",
                name="Other",
                profile_id="default",
                owner_id="mallory",
            ),
        },
        users={"alice": "Alice", "mallory": "Mallory"},
        allowed_tokens=[
            {"token": "other-token", "agent_id": "other_agent", "user_id": "mallory"}
        ],
    )
    client, controller = _build_client(identity_provider=provider)
    controller._runtime.approval_manager._store._pending.append(
        _MockApprovalRequest("req-1", "d-1", "send_email", "alice")
    )
    headers = {"Authorization": "Bearer other-token"}

    wait = client.get(
        "/v1/wait-for-approval",
        params={"request_id": "req-1", "max_wait": 1},
        headers=headers,
    )
    resume = client.post(
        "/v1/govern/resume-after-approval",
        json={"request_id": "req-1"},
        headers=headers,
    )

    assert wait.status_code == 403
    assert resume.status_code == 403
    assert controller.resume_calls == []


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

    with client.stream(
        "GET", "/v1/wait-for-approval/sse", params={"request_id": "req-1", "max_wait": 5}
    ) as resp:
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

    with client.stream(
        "GET", "/v1/wait-for-approval/sse", params={"request_id": "req-1", "max_wait": 5}
    ) as resp:
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


def test_admin_approvals_history_filters_and_pagination() -> None:
    client, controller = _build_client(
        api_key="secret",
        identity_provider=_admin_identity_provider(),
    )
    from loop_controller.models import ApprovalRecord

    store = controller._runtime.approval_manager._store
    request = _pending_approval_request()
    store.submit_request(request)
    store.record_response(
        ApprovalRecord(
            request_id=request.request_id,
            decision_id=request.decision_id,
            verdict="approve",
            approver_id="zhang_manager",
            comment="ok",
        )
    )

    resp = client.get(
        "/v1/admin/approvals",
        params={"status": "approve", "tool_name": "send_email", "limit": 10},
        headers={"X-API-Key": "secret"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["approvals"]) == 1
    assert data["approvals"][0]["status"] == "approve"
    assert data["approvals"][0]["agent_id"] == "researcher_001"
    assert data["approvals"][0]["tool_name"] == "send_email"
    assert data["approvals"][0]["approver_id"] == "zhang_manager"
    assert data["total"] == 1
    assert data["limit"] == 10
    assert data["offset"] == 0


def test_admin_approvals_rejects_invalid_status() -> None:
    client, _controller = _build_client(api_key="secret")
    resp = client.get(
        "/v1/admin/approvals",
        params={"status": "weird"},
        headers={"X-API-Key": "secret"},
    )
    assert resp.status_code == 400


def test_admin_pending_approvals() -> None:
    client, controller = _build_client(api_key="secret")
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
    resp = client.get("/v1/admin/approvals/pending", headers={"X-API-Key": "secret"})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["approvals"]) == 1
    assert data["approvals"][0]["request_id"] == "req-1"
    assert data["approvals"][0]["tool_name"] == "send_email"


def test_admin_audit_query_by_agent_and_tool() -> None:
    client, controller = _build_client(api_key="secret")
    controller._runtime.audit_store = _MockAuditStore(
        [
            _MockAuditEvent(session_id="s-1", task_id="t-1", agent_id="a-1", tool_name="send_email"),
            _MockAuditEvent(session_id="s-2", task_id="t-2", agent_id="a-2", tool_name="web_search"),
        ]
    )
    resp = client.get(
        "/v1/admin/audit",
        params={"agent_id": "a-1", "tool_name": "send_email", "limit": 10},
        headers={"X-API-Key": "secret"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["events"]) == 1
    assert data["events"][0]["agent_id"] == "a-1"
    assert data["events"][0]["tool_name"] == "send_email"


def test_admin_audit_query() -> None:
    client, controller = _build_client(api_key="secret")
    controller._runtime.audit_store = _MockAuditStore(
        [
            _MockAuditEvent(session_id="s-1", task_id="t-1"),
            _MockAuditEvent(session_id="s-2", task_id="t-2"),
        ]
    )
    resp = client.get(
        "/v1/admin/audit",
        params={"session_id": "s-1", "limit": 10},
        headers={"X-API-Key": "secret"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["events"]) == 1
    assert data["events"][0]["session_id"] == "s-1"


def test_admin_pending_requires_configured_api_key() -> None:
    client, _controller = _build_client()

    assert client.get("/v1/admin/approvals/pending").status_code == 401


def test_admin_audit_requires_configured_api_key() -> None:
    client, _controller = _build_client()

    assert client.get("/v1/admin/audit").status_code == 401


def test_admin_harness_backends_is_authenticated_and_sanitized() -> None:
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from loop_controller.executors.harness_protocol import HarnessBackendStatus

    class HarnessExecutorStub:
        def backend_statuses(self) -> list[HarnessBackendStatus]:
            return [
                HarnessBackendStatus(
                    name="production",
                    type="http",
                    status="healthy",
                    max_concurrent_calls=20,
                    checked_at=datetime(2026, 8, 28, tzinfo=UTC),
                    in_flight=2,
                )
            ]

    client, controller = _build_client(api_key="secret")
    controller._runtime.harness_executor = HarnessExecutorStub()
    controller._runtime.harness_config = SimpleNamespace(
        base_url="https://user:password@harness.example",
        api_key="must-not-leak",
        key_env="HARNESS_SECRET",
    )

    assert client.get("/v1/admin/harness/backends").status_code == 401
    response = client.get("/v1/admin/harness/backends", headers={"X-API-Key": "secret"})
    assert response.status_code == 200
    assert response.json() == {
        "backends": [
            {
                "name": "production",
                "type": "http",
                "status": "healthy",
                "max_concurrent_calls": 20,
                "checked_at": "2026-08-28T00:00:00Z",
                "consecutive_failures": 0,
                "last_error_code": None,
                "in_flight": 2,
                "draining": False,
            }
        ]
    }
    serialized = response.text
    assert "password" not in serialized
    assert "must-not-leak" not in serialized
    assert "HARNESS_SECRET" not in serialized


class _HarnessExecutorStub:
    """用于 drain/reset 测试的 HarnessExecutor mock。"""

    def __init__(self, statuses: list[Any] | None = None) -> None:
        self._statuses = statuses or []
        self.drain_calls: list[str] = []
        self.reset_calls: list[str] = []

    def backend_statuses(self) -> list[Any]:
        return self._statuses

    async def drain_backend(self, name: str) -> bool:
        self.drain_calls.append(name)
        return True

    def reset_backend(self, name: str) -> None:
        self.reset_calls.append(name)


def test_health_aggregates_harness_backends() -> None:
    from loop_controller.executors.harness_protocol import HarnessBackendStatus

    stub = _HarnessExecutorStub(
        [
            HarnessBackendStatus(
                name="prod", type="http", status="healthy", max_concurrent_calls=20
            )
        ]
    )
    client, controller = _build_client()
    controller._runtime.harness_executor = stub
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["harness_backends"]) == 1
    assert data["harness_backends"][0]["name"] == "prod"
    assert data["status"] == "ok"


def test_health_degraded_when_harness_backend_unhealthy() -> None:
    from loop_controller.executors.harness_protocol import HarnessBackendStatus

    stub = _HarnessExecutorStub(
        [
            HarnessBackendStatus(
                name="prod", type="http", status="unhealthy", max_concurrent_calls=20
            )
        ]
    )
    client, controller = _build_client()
    controller._runtime.harness_executor = stub
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "degraded"


def test_admin_harness_drain_and_reset() -> None:
    from loop_controller.executors.harness_protocol import HarnessBackendStatus

    stub = _HarnessExecutorStub(
        [
            HarnessBackendStatus(
                name="prod", type="http", status="healthy", max_concurrent_calls=20
            )
        ]
    )
    client, controller = _build_client(api_key="secret")
    controller._runtime.harness_executor = stub

    resp = client.post("/v1/admin/harness/prod/drain", headers={"X-API-Key": "secret"})
    assert resp.status_code == 200
    assert resp.json() == {"backend": "prod", "drained": True}
    assert stub.drain_calls == ["prod"]

    resp = client.post("/v1/admin/harness/prod/reset", headers={"X-API-Key": "secret"})
    assert resp.status_code == 200
    assert resp.json() == {"backend": "prod", "reset": True}
    assert stub.reset_calls == ["prod"]

    audit = controller._runtime.audit_store._events
    admin_ops = [e for e in audit if e.action == "admin_operation"]
    assert len(admin_ops) == 2
    assert admin_ops[0].reason == "harness_drain"
    assert admin_ops[1].reason == "harness_reset"


def test_admin_harness_drain_reset_returns_404_for_unknown_backend() -> None:
    class Stub:
        async def drain_backend(self, name: str) -> bool:
            raise KeyError(f"Harness 后端 {name!r} 不存在")

        def reset_backend(self, name: str) -> None:
            raise KeyError(f"Harness 后端 {name!r} 不存在")

        def backend_statuses(self) -> list:
            return []

    client, controller = _build_client(api_key="secret")
    controller._runtime.harness_executor = Stub()
    resp = client.post("/v1/admin/harness/missing/drain", headers={"X-API-Key": "secret"})
    assert resp.status_code == 404
    resp = client.post("/v1/admin/harness/missing/reset", headers={"X-API-Key": "secret"})
    assert resp.status_code == 404


def test_admin_harness_drain_reset_returns_503_when_harness_disabled() -> None:
    client, controller = _build_client(api_key="secret")
    controller._runtime.harness_executor = None
    resp = client.post("/v1/admin/harness/prod/drain", headers={"X-API-Key": "secret"})
    assert resp.status_code == 503
    resp = client.post("/v1/admin/harness/prod/reset", headers={"X-API-Key": "secret"})
    assert resp.status_code == 503


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
        "/v1/admin/harness/backends",
        "/v1/wait-for-approval",
        "/v1/wait-for-approval/sse",
    ):
        resp = client.get(path)
        assert resp.status_code == 401, path
    for path in ("/v1/admin/harness/prod/drain", "/v1/admin/harness/prod/reset"):
        resp = client.post(path)
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


@pytest.mark.parametrize("api_key", [None, "secret"])
def test_admin_anchor_and_harness_endpoints_require_auth(api_key: str | None) -> None:
    client, _controller = _build_client(api_key=api_key)
    for path, method in (
        ("/v1/admin/harness/backends", "GET"),
        ("/v1/admin/harness/prod/drain", "POST"),
        ("/v1/admin/harness/prod/reset", "POST"),
        ("/v1/admin/evidence/anchor", "GET"),
        ("/v1/admin/evidence/anchor/verify", "POST"),
        ("/v1/admin/evidence/anchor/publish", "POST"),
        ("/v1/admin/evidence/anchor/bootstrap", "POST"),
    ):
        resp = client.request(method, path)
        assert resp.status_code == 401, path


class _MockAuditStoreWithAnchor(_MockAuditStore):
    def __init__(self) -> None:
        super().__init__()
        self._summary = {
            "evidence_status": "healthy",
            "anchor_status": "healthy",
            "anchor_stream_id": "deployment/default",
            "anchor_last_success_seq": 5,
            "anchor_lag_events": 0,
            "anchor_last_error_code": None,
        }

    async def append_async(self, event: Any) -> None:
        return None

    def anchor_summary(self) -> dict[str, object]:
        return self._summary

    async def verify_anchor(self) -> dict[str, object]:
        self._summary["anchor_status"] = "healthy"
        return self._summary

    async def publish_anchor(self) -> dict[str, object]:
        self._summary["anchor_last_success_seq"] = 6
        return self._summary

    async def bootstrap_anchor(self, event: Any) -> dict[str, object]:
        self._summary["anchor_status"] = "healthy"
        self._summary["anchor_last_success_seq"] = 1
        return self._summary


def test_admin_anchor_summary_returns_disabled_when_unconfigured() -> None:
    client, _controller = _build_client(api_key="secret")
    resp = client.get("/v1/admin/evidence/anchor", headers={"X-API-Key": "secret"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["anchor_status"] == "disabled"


def test_admin_anchor_verify_publish_bootstrap() -> None:
    client, controller = _build_client(api_key="secret")
    controller._runtime.audit_store = _MockAuditStoreWithAnchor()

    resp = client.post("/v1/admin/evidence/anchor/verify", headers={"X-API-Key": "secret"})
    assert resp.status_code == 200
    assert resp.json()["anchor_status"] == "healthy"

    resp = client.post("/v1/admin/evidence/anchor/publish", headers={"X-API-Key": "secret"})
    assert resp.status_code == 200
    assert resp.json()["anchor_last_success_seq"] == 6

    resp = client.post("/v1/admin/evidence/anchor/bootstrap", headers={"X-API-Key": "secret"})
    assert resp.status_code == 200
    assert resp.json()["anchor_last_success_seq"] == 1


def test_admin_anchor_publish_returns_conflict_when_blocked() -> None:
    class BlockingStore(_MockAuditStoreWithAnchor):
        async def publish_anchor(self) -> dict[str, object]:
            raise RuntimeError("当前 Anchor 状态不允许普通 publish")

    client, controller = _build_client(api_key="secret")
    controller._runtime.audit_store = BlockingStore()
    resp = client.post("/v1/admin/evidence/anchor/publish", headers={"X-API-Key": "secret"})
    assert resp.status_code == 409
    assert "不允许普通 publish" in resp.json()["error"]


def test_admin_anchor_bootstrap_returns_conflict_when_not_allowed() -> None:
    class BlockingStore(_MockAuditStoreWithAnchor):
        async def bootstrap_anchor(self, event: Any) -> dict[str, object]:
            raise RuntimeError("当前 Anchor 状态不允许 bootstrap")

    client, controller = _build_client(api_key="secret")
    controller._runtime.audit_store = BlockingStore()
    resp = client.post("/v1/admin/evidence/anchor/bootstrap", headers={"X-API-Key": "secret"})
    assert resp.status_code == 409
    assert "不允许 bootstrap" in resp.json()["error"]


def test_admin_evidence_anchor_with_real_store(tmp_path: Path) -> None:
    """使用临时目录、真实 JsonlAuditStore（启用 evidence chain）与内存 EvidenceAnchorBackend
    覆盖 Admin Anchor HTTP 端点，并校验每次 Admin 操作均写入审计事件。"""

    import asyncio
    import base64

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from loop_controller.audit.anchors import AnchorReceipt, AnchorReceiptVerifier
    from loop_controller.audit.evidence import EvidenceChain, HMACEvidenceSigner
    from loop_controller.audit.evidence_backends import LocalFileEvidenceBackend
    from loop_controller.infra.audit_store import JsonlAuditStore
    from loop_controller.models import AuditEvent
    from loop_controller.utils.canonical import canonical_json

    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()

    class _MemoryAnchorBackend:
        """可切换 latest 行为的内存 EvidenceAnchorBackend，用于构造 healthy /
        bootstrap_required 两种状态。"""

        def __init__(self, key: Ed25519PrivateKey) -> None:
            self.key = key
            self.receipts: dict[str, AnchorReceipt] = {}
            self.latest_returns_none = False

        def publish(self, payload, *, idempotency_key: str) -> AnchorReceipt:
            receipt = self._make_receipt(payload)
            self.receipts[payload.stream_id] = receipt
            return receipt

        def latest(self, stream_id: str) -> AnchorReceipt | None:
            if self.latest_returns_none:
                return None
            return self.receipts.get(stream_id)

        def close(self) -> None:
            return None

        def _make_receipt(self, payload) -> AnchorReceipt:
            unsigned = {
                "receipt_id": f"receipt-{payload.audit_seq}",
                "payload": payload.model_dump(mode="json"),
                "anchored_at": "2026-08-28T12:00:01.000000Z",
                "service_key_id": "service-1",
                "algorithm": "ed25519",
            }
            signature = self.key.sign(canonical_json(unsigned).encode("utf-8"))
            return AnchorReceipt.model_validate(
                {
                    **unsigned,
                    "signature": base64.b64encode(signature).decode("ascii"),
                }
            )

    backend = _MemoryAnchorBackend(private_key)
    verifier = AnchorReceiptVerifier({"service-1": public_key})

    def _build_store(path: Path) -> JsonlAuditStore:
        chain = EvidenceChain(
            LocalFileEvidenceBackend(path / "evidence"),
            HMACEvidenceSigner(b"test-key", key_id="hmac-1"),
            checkpoint_path=path / "checkpoint.json",
        )
        return JsonlAuditStore(
            path / "audit.jsonl",
            evidence_chain=chain,
            anchor_backend=backend,
            anchor_stream_id="deployment/default",
            anchor_receipt_verifier=verifier,
        )

    async def _collect_events(store: JsonlAuditStore) -> list[AuditEvent]:
        return [event async for event in store.iter_events()]

    store_dir = tmp_path / "store"
    store = _build_store(store_dir)
    store.append(
        AuditEvent(
            event_id="event-1",
            trace_id="trace-1",
            session_id="session-1",
            actor_type="agent",
            actor_id="agent-1",
            action="execute",
            target="web_search",
            reason="seed local chain",
        )
    )

    controller = _MockController()
    controller._runtime.audit_store = store
    client = TestClient(build_app(controller, api_key="secret", configure_logs=False))

    headers = {"X-API-Key": "secret"}

    # GET /v1/admin/evidence/anchor 返回摘要
    resp = client.get("/v1/admin/evidence/anchor", headers=headers)
    assert resp.status_code == 200
    summary = resp.json()
    assert summary["anchor_status"] == "healthy"
    assert summary["evidence_status"] == "healthy"
    assert summary["anchor_stream_id"] == "deployment/default"

    # POST /v1/admin/evidence/anchor/publish 在 healthy 状态下发布本地尾部
    resp = client.post("/v1/admin/evidence/anchor/publish", headers=headers)
    assert resp.status_code == 200
    publish_summary = resp.json()
    assert publish_summary["anchor_status"] == "healthy"
    assert publish_summary["anchor_last_success_seq"] == 1

    events = asyncio.run(_collect_events(store))
    admin_ops = [e for e in events if e.action == "admin_operation"]
    assert len(admin_ops) == 1
    assert admin_ops[0].reason == "anchor_publish"
    assert admin_ops[0].target == "anchor"

    # 将后端切换为返回 None，使下一次 verify 进入 bootstrap_required
    backend.latest_returns_none = True

    # POST /v1/admin/evidence/anchor/bootstrap 在 bootstrap_required 状态下写入
    # bootstrap 锚点和管理事件
    resp = client.post("/v1/admin/evidence/anchor/bootstrap", headers=headers)
    assert resp.status_code == 200
    bootstrap_summary = resp.json()
    assert bootstrap_summary["anchor_status"] == "healthy"
    assert bootstrap_summary["anchor_last_success_seq"] == 3

    events = asyncio.run(_collect_events(store))
    admin_ops = [e for e in events if e.action == "admin_operation"]
    assert len(admin_ops) == 2
    assert admin_ops[-1].reason == "anchor_bootstrap"
    assert admin_ops[-1].target == "anchor"
    bootstrap_events = [e for e in events if e.action == "anchor_bootstrap"]
    assert len(bootstrap_events) == 1
    assert bootstrap_events[0].target == "anchor"


def _admin_identity_provider() -> ConfigIdentityProvider:
    """构造一个仅包含审批人用户的 IdentityProvider。"""
    return ConfigIdentityProvider(
        agents={},
        users={"zhang_manager": "张经理"},
    )


def _pending_approval_request(decision_id: str = "d-1") -> ApprovalRequest:
    return ApprovalRequest(
        request_id="req-1",
        decision_id=decision_id,
        call_id="c1",
        task_id="t1",
        agent_id="researcher_001",
        tool_name="send_email",
        arguments_masked={"to": "zhang@company.com"},
        reason="test",
        requester_id="alice",
        approver_id="zhang_manager",
    )


def test_admin_approvals_approve_success() -> None:
    client, controller = _build_client(
        api_key="secret",
        identity_provider=_admin_identity_provider(),
    )
    store = controller._runtime.approval_manager._store
    store._pending.append(_pending_approval_request())

    resp = client.post(
        "/v1/admin/approvals/d-1/approve",
        json={"approver": "zhang_manager", "comment": "approved"},
        headers={"X-API-Key": "secret"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["decision_id"] == "d-1"
    assert data["verdict"] == "approve"

    record = store.get_record("d-1")
    assert record is not None
    assert record.verdict == "approve"
    assert record.approver_id == "zhang_manager"

    audit = controller._runtime.audit_store._events
    admin_ops = [e for e in audit if e.action == "admin_operation"]
    assert len(admin_ops) == 1
    assert admin_ops[0].reason == "approval_approve"


def test_admin_approvals_deny_success() -> None:
    client, controller = _build_client(
        api_key="secret",
        identity_provider=_admin_identity_provider(),
    )
    store = controller._runtime.approval_manager._store
    store._pending.append(_pending_approval_request())

    resp = client.post(
        "/v1/admin/approvals/d-1/deny",
        json={"approver": "zhang_manager", "comment": "suspicious"},
        headers={"X-API-Key": "secret"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["decision_id"] == "d-1"
    assert data["verdict"] == "deny"

    record = store.get_record("d-1")
    assert record is not None
    assert record.verdict == "deny"
    assert record.comment == "suspicious"

    audit = controller._runtime.audit_store._events
    admin_ops = [e for e in audit if e.action == "admin_operation"]
    assert len(admin_ops) == 1
    assert admin_ops[0].reason == "approval_deny"
    assert admin_ops[0].target == "decision:d-1"


def test_admin_approvals_deny_requires_comment() -> None:
    client, controller = _build_client(
        api_key="secret",
        identity_provider=_admin_identity_provider(),
    )
    store = controller._runtime.approval_manager._store
    store._pending.append(_pending_approval_request())

    resp = client.post(
        "/v1/admin/approvals/d-1/deny",
        json={"approver": "zhang_manager"},
        headers={"X-API-Key": "secret"},
    )
    assert resp.status_code == 422
    assert "deny 必须提供审批意见" in resp.json()["error"]


def test_admin_approvals_rejects_non_approver() -> None:
    client, controller = _build_client(
        api_key="secret",
        identity_provider=_admin_identity_provider(),
    )
    store = controller._runtime.approval_manager._store
    store._pending.append(_pending_approval_request())

    resp = client.post(
        "/v1/admin/approvals/d-1/approve",
        json={"approver": "ghost_user", "comment": "ok"},
        headers={"X-API-Key": "secret"},
    )
    assert resp.status_code == 422
    assert "ghost_user" in resp.json()["error"]


def test_admin_approvals_conflict_when_already_decided() -> None:
    client, controller = _build_client(
        api_key="secret",
        identity_provider=_admin_identity_provider(),
    )
    store = controller._runtime.approval_manager._store
    store._pending.append(_pending_approval_request())

    headers = {"X-API-Key": "secret"}
    body = {"approver": "zhang_manager", "comment": "approved"}
    assert (
        client.post("/v1/admin/approvals/d-1/approve", json=body, headers=headers).status_code
        == 200
    )

    resp = client.post("/v1/admin/approvals/d-1/approve", json=body, headers=headers)
    assert resp.status_code == 409
    assert "已有审批结果" in resp.json()["error"]

    audit = controller._runtime.audit_store._events
    admin_ops = [e for e in audit if e.action == "admin_operation"]
    assert len(admin_ops) == 1
    assert admin_ops[0].reason == "approval_approve"


# ---------------------------------------------------------------------------
# v0.33.0 安全加固测试
# ---------------------------------------------------------------------------


def test_body_size_limit_returns_413() -> None:
    """Content-Length 超过 max_body_size 时返回 413，不进入业务处理。"""
    client, controller = _build_client(
        entrypoints_config={"http": {"max_body_size": 1024}}
    )
    resp = client.post(
        "/v1/govern/tool-call",
        headers={"Content-Length": "2048"},
    )
    assert resp.status_code == 413
    assert resp.json()["error"] == "payload_too_large"
    assert controller.tool_calls == []


def test_invalid_content_length_returns_400() -> None:
    """Content-Length 非法时返回 400 而非 500。"""
    client, _controller = _build_client()
    resp = client.post(
        "/v1/govern/tool-call",
        headers={"Content-Length": "not-a-number"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_parameter"


def test_rate_limit_blocks_excessive_requests() -> None:
    """配置限流后超过 burst 的请求返回 429。"""
    client, _controller = _build_client(
        entrypoints_config={
            "http": {
                "rate_limit": {
                    "requests_per_minute": 1,
                    "burst": 1,
                }
            }
        }
    )
    for i in range(3):
        resp = client.get("/health")
        if i < 2:
            assert resp.status_code == 200
        else:
            assert resp.status_code == 429
            assert resp.json()["error"] == "rate_limited"


def test_api_key_length_mismatch_safe() -> None:
    """候选 key 长度与配置 key 不一致时不会触发 compare_digest 异常。"""
    client, _controller = _build_client(api_key="short")
    resp = client.get(
        "/v1/admin/approvals/pending",
        headers={"X-API-Key": "a-much-longer-key"},
    )
    assert resp.status_code == 401


def test_invalid_query_param_returns_400() -> None:
    """max_wait 等 query 参数非法时返回 400。"""
    client, _controller = _build_client()
    resp = client.get("/v1/wait-for-approval?request_id=r1&max_wait=abc")
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_parameter"


# ---------------------------------------------------------------------------
# 管理控制台最小可行接口（/v1/admin/agents|profiles|identity|entrypoints|govern/evaluate）
# ---------------------------------------------------------------------------

from datetime import UTC, datetime, timedelta  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from loop_controller.classifier import RuleBasedClassifier  # noqa: E402
from loop_controller.identity import (  # noqa: E402
    AgentIdentity,
    RevocationEntry,
    RevocationList,
    RevocationType,
)
from loop_controller.identity.revocation import RevocationMatch  # noqa: E402
from loop_controller.models import (  # noqa: E402
    CapabilityProfile,
    Decision,
    ToolPermission,
)


def _admin_config() -> SimpleNamespace:
    return SimpleNamespace(
        agents={
            "researcher_001": Agent(
                agent_id="researcher_001",
                name="Research Assistant",
                profile_id="research_v1",
                owner_id="zhang_manager",
            ),
            "writer_001": Agent(
                agent_id="writer_001",
                name="Writer",
                profile_id="writer_v1",
                owner_id="li_manager",
            ),
        },
        users={"zhang_manager": "张经理", "li_manager": "李经理"},
        identity_config={
            "provider": "static",
            "static": {"allowed_tokens": ["tok-secret-1", "tok-secret-2"]},
        },
        entrypoints_config={
            "entrypoints": {"http": {"require_auth": True, "api_key": "super-secret-key"}},
        },
    )


def _admin_profiles() -> dict[str, CapabilityProfile]:
    return {
        "research_v1": CapabilityProfile(
            profile_id="research_v1",
            version="abc123",
            description="研究助手",
            tools={
                "send_email": ToolPermission(
                    tool_name="send_email", allowed=True, require_approval=True
                )
            },
        )
    }


class _AdminMockCheckpoint:
    """提供吊销检查与判定结果的 Checkpoint mock。"""

    def __init__(self, revoked: bool = False) -> None:
        self._policy_engine = _MockPolicyEngine("http://127.0.0.1:1")
        self._revoked = revoked
        self.evaluated: list[str] = []

    def check_revocation(
        self, identity: AgentIdentity, tool_name: str, arguments: dict
    ) -> RevocationMatch:
        if self._revoked:
            return RevocationMatch(
                revoked=True, reason="agent revoked", type=RevocationType.AGENT, id=identity.agent_id
            )
        return RevocationMatch(revoked=False)

    async def evaluate(self, task: Any, agent: Any, proposal: Any, **kwargs: Any) -> Decision:
        self.evaluated.append(proposal.call_id)
        return Decision(
            decision_id="d-dryrun",
            call_id=proposal.call_id,
            task_id=task.task_id,
            verdict="allow",
            reason="allowed by policy",
            policy_hits=["default_allow"],
            policy_version="pv1",
            profile_version="abc123",
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )


class _AdminMockRuntime:
    def __init__(self, revoked: bool = False, with_revocation: bool = True) -> None:
        self.approval_manager = _MockApprovalManager()
        self.audit_store = _MockAuditStore()
        self.checkpoint = _AdminMockCheckpoint(revoked=revoked)
        self.harness_executor = None
        self.config = _admin_config()
        self.profiles = _admin_profiles()
        self.classifier = RuleBasedClassifier()
        self.http_tool_names: set[str] = set()
        revocations = RevocationList()
        if with_revocation:
            revocations.add(
                RevocationEntry(type=RevocationType.AGENT, id="writer_001", reason="测试吊销")
            )
        self.revocation_list = revocations


class _AdminMockController(_MockController):
    def __init__(self, revoked: bool = False) -> None:
        super().__init__()
        self._runtime = _AdminMockRuntime(revoked=revoked)


def _build_admin_client(
    api_key: str | None = "test-key", revoked: bool = False
) -> tuple[TestClient, _AdminMockController]:
    controller = _AdminMockController(revoked=revoked)
    app = build_app(controller, api_key=api_key, configure_logs=False)
    return TestClient(app), controller


def test_admin_agents_lists_config_with_revocation() -> None:
    client, _controller = _build_admin_client()
    resp = client.get("/v1/admin/agents", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 200
    agents = {a["agent_id"]: a for a in resp.json()["agents"]}
    assert agents["researcher_001"]["name"] == "Research Assistant"
    assert agents["researcher_001"]["owner_name"] == "张经理"
    assert agents["researcher_001"]["revoked"] is False
    assert agents["writer_001"]["revoked"] is True


def test_admin_agents_requires_api_key() -> None:
    client, _controller = _build_admin_client()
    resp = client.get("/v1/admin/agents")
    assert resp.status_code == 401


def test_admin_agent_detail_returns_full_fields() -> None:
    client, _controller = _build_admin_client()
    resp = client.get("/v1/admin/agents/researcher_001", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["agent_id"] == "researcher_001"
    assert data["name"] == "Research Assistant"
    assert data["profile_id"] == "research_v1"
    assert data["owner_id"] == "zhang_manager"
    assert data["owner_name"] == "张经理"
    assert data["revoked"] is False
    assert data["description"] is None
    assert data["metadata"] == {}


def test_admin_agent_detail_unknown_agent_returns_404() -> None:
    client, _controller = _build_admin_client()
    resp = client.get("/v1/admin/agents/ghost", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 404


def test_admin_agent_detail_revoked_flag() -> None:
    client, _controller = _build_admin_client(revoked=True)
    resp = client.get("/v1/admin/agents/writer_001", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 200
    assert resp.json()["revoked"] is True


def test_admin_agent_detail_requires_api_key() -> None:
    client, _controller = _build_admin_client()
    resp = client.get("/v1/admin/agents/researcher_001")
    assert resp.status_code == 401


def test_admin_profiles_returns_serialized_profiles() -> None:
    client, _controller = _build_admin_client()
    resp = client.get("/v1/admin/profiles", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 200
    profiles = {p["profile_id"]: p for p in resp.json()["profiles"]}
    profile = profiles["research_v1"]
    assert profile["tools"]["send_email"]["allowed"] is True
    assert profile["tools"]["send_email"]["require_approval"] is True


def _write_profiles_yaml(config_dir: Path, tools: dict[str, Any]) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "profiles.yaml").write_text(
        yaml.safe_dump(
            {
                "profiles": [
                    {
                        "profile_id": "research_v1",
                        "description": "研究助手",
                        "tools": tools,
                    }
                ]
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _build_profile_edit_client(tmp_path: Path) -> tuple[TestClient, Any]:
    client, controller = _build_admin_client()
    _write_profiles_yaml(
        tmp_path,
        {
            "send_email": {
                "allowed": True,
                "require_approval": True,
                "allowed_args": {"to": ["*@company.com"]},
                "max_calls_per_task": 1,
            },
            "web_search": {"allowed": True, "max_calls_per_task": 10},
        },
    )
    controller._runtime.config_dir = str(tmp_path)
    return client, controller


def test_admin_profile_tools_update_writes_reloads_and_audits(tmp_path: Path) -> None:
    client, controller = _build_profile_edit_client(tmp_path)
    resp = client.put(
        "/v1/admin/profiles/research_v1/tools",
        headers={"X-API-Key": "test-key"},
        json={
            "tools": {
                "send_email": {
                    "allowed": True,
                    "require_approval": False,
                    "allowed_args": {"to": ["*@company.com", "*@partner.com"]},
                },
                "web_search": {"allowed": True, "max_calls_per_task": 5},
            }
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["reloaded"] is True
    send_email = body["profile"]["tools"]["send_email"]
    assert send_email["require_approval"] is False
    assert send_email["allowed_args"]["to"] == ["*@company.com", "*@partner.com"]

    # 运行时映射已原地刷新（对象替换为磁盘重载版本）
    runtime_perm = controller._runtime.profiles["research_v1"].tools["send_email"]
    assert runtime_perm.require_approval is False

    # 文件确实写回（加载-修改-全量写回，注释不保留）
    on_disk = yaml.safe_load((tmp_path / "profiles.yaml").read_text(encoding="utf-8"))
    disk_tools = on_disk["profiles"][0]["tools"]
    assert disk_tools["send_email"]["allowed_args"]["to"] == ["*@company.com", "*@partner.com"]
    assert "require_approval" not in disk_tools["send_email"]

    # 审计已记录管理操作
    actions = [e.reason for e in controller._runtime.audit_store._events]
    assert "update_profile_tools" in actions


def test_admin_profile_tools_update_unknown_profile_returns_400(tmp_path: Path) -> None:
    client, _controller = _build_profile_edit_client(tmp_path)
    resp = client.put(
        "/v1/admin/profiles/ghost/tools",
        headers={"X-API-Key": "test-key"},
        json={"tools": {"web_search": {"allowed": True}}},
    )
    assert resp.status_code == 400


def test_admin_profile_tools_update_invalid_permission_returns_400(tmp_path: Path) -> None:
    client, controller = _build_profile_edit_client(tmp_path)
    resp = client.put(
        "/v1/admin/profiles/research_v1/tools",
        headers={"X-API-Key": "test-key"},
        json={"tools": {"web_search": {"allowed": True, "max_calls_per_task": "abc"}}},
    )
    assert resp.status_code == 400
    # 校验失败不写文件：磁盘内容保持初始状态
    on_disk = yaml.safe_load((tmp_path / "profiles.yaml").read_text(encoding="utf-8"))
    assert on_disk["profiles"][0]["tools"]["web_search"]["max_calls_per_task"] == 10


def test_admin_profiles_reload_syncs_from_disk(tmp_path: Path) -> None:
    client, controller = _build_profile_edit_client(tmp_path)
    # 绕过 API 直接改文件（模拟手工编辑），再触发统一 reload
    _write_profiles_yaml(
        tmp_path,
        {"send_email": {"allowed": False}},
    )
    resp = client.post("/v1/admin/profiles/reload", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 200
    profiles = {p["profile_id"]: p for p in resp.json()["profiles"]}
    assert profiles["research_v1"]["tools"]["send_email"]["allowed"] is False
    assert controller._runtime.profiles["research_v1"].tools["send_email"].allowed is False
    actions = [e.reason for e in controller._runtime.audit_store._events]
    assert "reload_profiles" in actions


def test_admin_profile_tools_update_requires_config_dir() -> None:
    client, _controller = _build_admin_client()  # mock runtime 无 config_dir
    resp = client.put(
        "/v1/admin/profiles/research_v1/tools",
        headers={"X-API-Key": "test-key"},
        json={"tools": {"web_search": {"allowed": True}}},
    )
    assert resp.status_code == 503


def test_admin_session_login_issues_bearer_token() -> None:
    client, controller = _build_admin_client()
    resp = client.post("/v1/admin/session/login", json={"api_key": "test-key"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["token"]
    assert body["token_type"] == "bearer"

    # Bearer Session Token 可访问管理端点
    resp = client.get(
        "/v1/admin/agents", headers={"Authorization": f"Bearer {body['token']}"}
    )
    assert resp.status_code == 200

    reasons = [e.reason for e in controller._runtime.audit_store._events]
    assert "session_login" in reasons


def test_admin_session_login_wrong_key_returns_401() -> None:
    client, _controller = _build_admin_client()
    resp = client.post("/v1/admin/session/login", json={"api_key": "wrong-key"})
    assert resp.status_code == 401


def test_admin_session_logout_revokes_token() -> None:
    client, controller = _build_admin_client()
    token = client.post("/v1/admin/session/login", json={"api_key": "test-key"}).json()["token"]
    resp = client.post(
        "/v1/admin/session/logout", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    assert resp.json()["revoked"] is True
    # 吊销后立即失效
    resp = client.get("/v1/admin/agents", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    reasons = [e.reason for e in controller._runtime.audit_store._events]
    assert "session_logout" in reasons


def test_admin_session_unknown_token_returns_401() -> None:
    client, _controller = _build_admin_client()
    resp = client.get(
        "/v1/admin/agents", headers={"Authorization": "Bearer not-a-real-token"}
    )
    assert resp.status_code == 401


class _FakeGoKernelBridge:
    """模拟 GoKernelBridge：reachable 控制 ping/list，tasks 模拟任务存储。"""

    def __init__(self, reachable: bool = True) -> None:
        self.reachable = reachable

    async def ping(self) -> bool:
        return self.reachable

    async def list_agents(self) -> list[dict[str, Any]]:
        if not self.reachable:
            return []
        return [{"agent_id": "researcher_001"}, {"agent_id": "loop-controller-local"}]

    async def query_task(self, task_id: str) -> dict[str, Any] | None:
        if not self.reachable:
            return None
        return {"task_id": task_id, "status": "completed"}


def test_admin_a2a_status_without_bridge() -> None:
    client, _controller = _build_admin_client()  # mock runtime 无 go_kernel_bridge
    resp = client.get("/v1/admin/a2a/status", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False
    assert body["reachable"] is False


def test_admin_a2a_status_and_agents_with_bridge() -> None:
    client, controller = _build_admin_client()
    controller._runtime.go_kernel_bridge = _FakeGoKernelBridge(reachable=True)
    resp = client.get("/v1/admin/a2a/status", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 200
    assert resp.json()["reachable"] is True

    resp = client.get("/v1/admin/a2a/agents", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["kernel_reachable"] is True
    agents = {a["agent_id"]: a for a in body["agents"]}
    assert agents["researcher_001"]["registered"] is True
    assert agents["writer_001"]["registered"] is False


def test_admin_a2a_agents_kernel_unreachable() -> None:
    client, controller = _build_admin_client()
    controller._runtime.go_kernel_bridge = _FakeGoKernelBridge(reachable=False)
    resp = client.get("/v1/admin/a2a/agents", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["kernel_reachable"] is False
    assert all(a["registered"] is None for a in body["agents"])


def test_admin_a2a_task_query() -> None:
    client, controller = _build_admin_client()
    controller._runtime.go_kernel_bridge = _FakeGoKernelBridge(reachable=True)
    resp = client.get("/v1/admin/a2a/tasks/t-1", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "completed"

    # 无 bridge 时 fail-closed
    client2, _controller2 = _build_admin_client()
    resp = client2.get("/v1/admin/a2a/tasks/t-1", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 503


class _FakeInteractionDecision:
    def __init__(self, verdict: str, allowed: bool, reason: str = "") -> None:
        self.verdict = verdict
        self.allowed = allowed
        self.reason = reason or verdict
        self.decision_id = "decision-1"
        self.interaction_id = "interaction-1"
        self.escalation_target = "zhang_manager" if verdict == "require_approval" else None
        self.target_entrypoint = {"type": "http", "url": "http://127.0.0.1:8001"}
        self.modified_args = None
        self.effective_args = None
        self.policy_version = "interaction/v0"
        self.profile_version = "p1"


class _FakeInteractionEngine:
    """模拟 InteractionGovernanceEngine：返回固定判定，记录提案。"""

    last_proposal: Any = None
    verdict: str = "allow"
    allowed: bool = True

    def __init__(self, controller: Any, policy_engine: Any = None) -> None:
        pass

    async def evaluate(self, proposal: Any) -> Any:
        _FakeInteractionEngine.last_proposal = proposal
        return _FakeInteractionDecision(self.verdict, self.allowed)

    def build_audit_event(self, proposal: Any, decision: Any) -> Any:
        return AuditEvent(
            schema_version="1.0",
            event_id="evt-delegation",
            trace_id="trace-1",
            session_id="",
            actor_type="agent",
            actor_id=proposal.source_agent_id,
            action="execution_authorized",
            target=proposal.tool_name,
            decision="allow" if decision.allowed else "deny",
            reason=decision.reason,
            metadata={"interaction_context": proposal.interaction_context},
        )


class _FakeDelegationBridge:
    def __init__(self, accept: bool = True) -> None:
        self.accept = accept
        self.requests: list[Any] = []

    async def ping(self) -> bool:
        return True

    async def request_delegation(self, req: Any) -> Any:
        self.requests.append(req)

        class _Resp:
            def __init__(self, accept: bool) -> None:
                self.allowed = accept
                self.task_id = "task-42" if accept else ""
                self.reason = "" if accept else "go_kernel_rejected"

        return _Resp(self.accept)


def _delegation_payload() -> dict[str, Any]:
    return {
        "source_agent_id": "researcher_001",
        "target_agent_id": "writer_001",
        "tool_name": "send_email",
        "arguments": {"to": "manager@company.com"},
    }


def test_admin_a2a_delegation_allow_dispatches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "loop_controller.server.InteractionGovernanceEngine", _FakeInteractionEngine
    )
    _FakeInteractionEngine.verdict = "allow"
    _FakeInteractionEngine.allowed = True
    client, controller = _build_admin_client()
    bridge = _FakeDelegationBridge(accept=True)
    controller._runtime.go_kernel_bridge = bridge

    resp = client.post(
        "/v1/admin/a2a/delegations",
        headers={"X-API-Key": "test-key"},
        json=_delegation_payload(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["verdict"] == "allow"
    assert body["allowed"] is True
    assert body["dispatch"]["attempted"] is True
    assert body["dispatch"]["accepted"] is True
    assert body["dispatch"]["task_id"] == "task-42"
    # 治理引擎收到的提案标记了 admin-console 上下文
    assert _FakeInteractionEngine.last_proposal.interaction_context == "admin-console"
    # 交互审计已写入
    reasons = [e.reason for e in controller._runtime.audit_store._events]
    assert "allow" in reasons


def test_admin_a2a_delegation_deny_skips_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "loop_controller.server.InteractionGovernanceEngine", _FakeInteractionEngine
    )
    _FakeInteractionEngine.verdict = "deny"
    _FakeInteractionEngine.allowed = False
    client, controller = _build_admin_client()
    bridge = _FakeDelegationBridge(accept=True)
    controller._runtime.go_kernel_bridge = bridge

    resp = client.post(
        "/v1/admin/a2a/delegations",
        headers={"X-API-Key": "test-key"},
        json=_delegation_payload(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["verdict"] == "deny"
    assert body["allowed"] is False
    assert body["dispatch"]["attempted"] is False
    assert bridge.requests == []


def test_admin_a2a_delegation_unknown_target_delegated_to_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # target 可以是外部 Agent（仅注册在 Go 内核），不由 handler 预先拒绝，
    # 而是交给治理引擎通过 Agent Card 查询兜底（假引擎默认 deny）。
    monkeypatch.setattr(
        "loop_controller.server.InteractionGovernanceEngine", _FakeInteractionEngine
    )
    client, _controller = _build_admin_client()
    payload = _delegation_payload()
    payload["target_agent_id"] = "ghost"
    resp = client.post(
        "/v1/admin/a2a/delegations",
        headers={"X-API-Key": "test-key"},
        json=payload,
    )
    assert resp.status_code == 200
    assert resp.json()["verdict"] == "deny"
    assert resp.json()["dispatch"]["attempted"] is False


def test_admin_a2a_delegation_allow_without_kernel_notes_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "loop_controller.server.InteractionGovernanceEngine", _FakeInteractionEngine
    )
    _FakeInteractionEngine.verdict = "allow"
    _FakeInteractionEngine.allowed = True
    client, _controller = _build_admin_client()  # 无 go_kernel_bridge
    resp = client.post(
        "/v1/admin/a2a/delegations",
        headers={"X-API-Key": "test-key"},
        json=_delegation_payload(),
    )
    assert resp.status_code == 200
    assert resp.json()["dispatch"]["reason"] == "go kernel disabled"


def test_admin_identity_masks_sensitive_values() -> None:
    client, _controller = _build_admin_client()
    resp = client.get("/v1/admin/identity", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["provider"] == "static"
    tokens = data["config"]["static"]["allowed_tokens"]
    assert tokens == ["******", "******"]
    assert "tok-secret-1" not in resp.text


def test_admin_entrypoints_masks_api_key() -> None:
    client, _controller = _build_admin_client()
    resp = client.get("/v1/admin/entrypoints", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 200
    entrypoints = resp.json()["entrypoints"]
    assert entrypoints["http"]["require_auth"] is True
    assert entrypoints["http"]["api_key"] == "******"
    assert "super-secret-key" not in resp.text


def test_admin_govern_evaluate_returns_decision_without_execution() -> None:
    client, controller = _build_admin_client()
    resp = client.post(
        "/v1/admin/govern/evaluate",
        headers={"X-API-Key": "test-key"},
        json={
            "agent_id": "researcher_001",
            "user_id": "alice",
            "tool_name": "send_email",
            "arguments": {"to": "zhang@company.com"},
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["verdict"] == "allow"
    assert data["dry_run"] is True
    assert data["risk_level"] == "high"  # RuleBasedClassifier: send_email -> high
    # 未触发真实执行
    assert controller.tool_calls == []
    # 合成 call_id 带 dryrun- 前缀
    assert controller._runtime.checkpoint.evaluated[0].startswith("dryrun-")


def test_admin_govern_evaluate_blocked_by_revocation() -> None:
    client, controller = _build_admin_client(revoked=True)
    resp = client.post(
        "/v1/admin/govern/evaluate",
        headers={"X-API-Key": "test-key"},
        json={"agent_id": "researcher_001", "user_id": "alice", "tool_name": "send_email"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["verdict"] == "blocked"
    assert data["reason"] == "agent revoked"
    # 被吊销时不进入 R2 判定
    assert controller._runtime.checkpoint.evaluated == []


def test_admin_govern_evaluate_unknown_agent_returns_404() -> None:
    client, _controller = _build_admin_client()
    resp = client.post(
        "/v1/admin/govern/evaluate",
        headers={"X-API-Key": "test-key"},
        json={"agent_id": "ghost", "user_id": "alice", "tool_name": "send_email"},
    )
    assert resp.status_code == 404
