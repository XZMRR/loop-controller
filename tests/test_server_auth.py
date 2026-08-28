"""HTTP 服务身份认证测试（v0.20.0）。

验证 ToolGovernServer 在 identity provider 可用时的 JWT 校验、401 拒绝、
agent_id 一致性校验等行为。
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("starlette")

from starlette.testclient import TestClient

from loop_controller.approval_watcher import ApprovalWatcher
from loop_controller.controller import LoopController
from loop_controller.identity import ConfigIdentityProvider
from loop_controller.models import Agent, GovernanceResult
from loop_controller.server import build_app


class _MockAuditEvent:
    def __init__(self, session_id: str | None, task_id: str | None):
        self.session_id = session_id
        self.task_id = task_id

    def model_dump(self) -> dict[str, Any]:
        return {"session_id": self.session_id, "task_id": self.task_id}


class _MockAuditStore:
    def __init__(self, events: list[_MockAuditEvent] | None = None):
        self._events = events or []

    async def iter_events(self):
        for event in self._events:
            yield event


class _MockApprovalRequest:
    def __init__(
        self,
        request_id: str,
        decision_id: str,
        tool_name: str,
        requester_id: str,
    ):
        self.request_id = request_id
        self.decision_id = decision_id
        self.tool_name = tool_name
        self.requester_id = requester_id
        self.reason = ""


class _MockApprovalStore:
    def __init__(self):
        self._pending: list[_MockApprovalRequest] = []
        self._records: dict[str, Any] = {}

    def get_pending(self) -> list[_MockApprovalRequest]:
        return list(self._pending)

    def get_request_by_id(self, request_id: str) -> _MockApprovalRequest | None:
        return next((req for req in self._pending if req.request_id == request_id), None)

    def get_record(self, decision_id: str) -> Any | None:
        return self._records.get(decision_id)

    def add_record(self, decision_id: str, record: Any) -> None:
        self._records[decision_id] = record

    def refresh(self) -> None:
        pass


class _MockApprovalManager:
    def __init__(self):
        self._store = _MockApprovalStore()


class _MockPolicyEngine:
    def __init__(self, base_url: str):
        self._base_url = base_url


class _MockCheckpoint:
    def __init__(self):
        self._policy_engine = _MockPolicyEngine("http://127.0.0.1:1")


class _MockRuntime:
    def __init__(self):
        self.approval_manager = _MockApprovalManager()
        self.audit_store = _MockAuditStore()
        self.checkpoint = _MockCheckpoint()


class _MockController(LoopController):
    def __init__(self) -> None:
        self.tool_calls: list[dict[str, Any]] = []
        self.started = False
        self.closed = False
        self._runtime = _MockRuntime()
        self._tool_response = GovernanceResult(
            status="allow",
            call_id="c1",
            tool_name="send_email",
            arguments={},
            content="email sent",
        )

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


def _identity_provider() -> ConfigIdentityProvider:
    agent = Agent(
        agent_id="researcher_001",
        name="RA",
        profile_id="research_assistant_v1",
        owner_id="zhang_manager",
    )
    return ConfigIdentityProvider(
        agents={"researcher_001": agent},
        users={"alice": "Alice"},
        allowed_tokens=[
            {
                "token": "valid-token",
                "agent_id": "researcher_001",
                "user_id": "alice",
            }
        ],
    )


def _build_client(
    *,
    require_auth: bool = True,
    api_key: str | None = None,
) -> tuple[TestClient, _MockController, ConfigIdentityProvider]:
    controller = _MockController()
    provider = _identity_provider()
    app = build_app(
        controller,
        api_key=api_key,
        watcher=ApprovalWatcher(),
        configure_logs=False,
        identity_provider=provider,
        entrypoints_config={"http": {"require_auth": require_auth}},
    )
    return TestClient(app), controller, provider


class TestIdentityEndpoint:
    """/v1/identity 调试端点测试。"""

    def test_identity_unauthenticated(self) -> None:
        client, _controller, _provider = _build_client()
        resp = client.get("/v1/identity")
        assert resp.status_code == 200
        data = resp.json()
        assert data["authenticated"] is False

    def test_identity_authenticated(self) -> None:
        client, _controller, _provider = _build_client()
        resp = client.get(
            "/v1/identity",
            headers={"Authorization": "Bearer valid-token"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["authenticated"] is True
        assert data["agent_id"] == "researcher_001"
        assert data["user_id"] == "alice"


class TestGovernToolCallAuth:
    """/v1/govern/tool-call 身份校验测试。"""

    def test_require_auth_missing_token_401(self) -> None:
        client, _controller, _provider = _build_client(require_auth=True)
        resp = client.post(
            "/v1/govern/tool-call",
            json={
                "agent_id": "researcher_001",
                "user_id": "alice",
                "tool_name": "send_email",
                "arguments": {},
            },
        )
        assert resp.status_code == 401

    def test_valid_token_uses_identity_agent_id(self) -> None:
        client, controller, _provider = _build_client(require_auth=True)
        resp = client.post(
            "/v1/govern/tool-call",
            json={
                "agent_id": "researcher_001",
                "user_id": "alice",
                "tool_name": "send_email",
                "arguments": {"to": "zhang@company.com"},
            },
            headers={"Authorization": "Bearer valid-token"},
        )
        assert resp.status_code == 200
        assert len(controller.tool_calls) == 1
        assert controller.tool_calls[0]["agent_id"] == "researcher_001"
        assert controller.tool_calls[0]["user_id"] == "alice"

    def test_inconsistent_agent_id_returns_400(self) -> None:
        client, _controller, _provider = _build_client(require_auth=True)
        resp = client.post(
            "/v1/govern/tool-call",
            json={
                "agent_id": "other_agent",
                "user_id": "alice",
                "tool_name": "send_email",
                "arguments": {},
            },
            headers={"Authorization": "Bearer valid-token"},
        )
        assert resp.status_code == 400

    def test_inconsistent_user_id_returns_400(self) -> None:
        client, _controller, _provider = _build_client(require_auth=True)
        resp = client.post(
            "/v1/govern/tool-call",
            json={
                "agent_id": "researcher_001",
                "user_id": "bob",
                "tool_name": "send_email",
                "arguments": {},
            },
            headers={"Authorization": "Bearer valid-token"},
        )
        assert resp.status_code == 400

    def test_no_auth_config_allows_request(self) -> None:
        client, controller, _provider = _build_client(require_auth=False)
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
        assert controller.tool_calls[0]["agent_id"] == "researcher_001"


class TestAdminEndpointsStillRequireApiKey:
    """admin / wait 端点在设置了 api_key 时仍需 API key。"""

    def test_admin_pending_without_api_key_401(self) -> None:
        client, _controller, _provider = _build_client(require_auth=True, api_key="secret")
        resp = client.get("/v1/admin/approvals/pending")
        assert resp.status_code == 401

    def test_admin_pending_with_valid_api_key(self) -> None:
        client, _controller, _provider = _build_client(require_auth=True, api_key="secret")
        resp = client.get(
            "/v1/admin/approvals/pending",
            headers={"X-API-Key": "secret"},
        )
        assert resp.status_code == 200
