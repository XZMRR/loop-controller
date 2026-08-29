"""Loop Controller HTTP 服务测试（v0.17.0 / v0.18.0 / v0.19.0）。

未安装 starlette 时整个文件自动 skip；使用 TestClient 对 ASGI app 做同步调用。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("starlette")

from starlette.testclient import TestClient

from loop_controller.approval_watcher import ApprovalWatcher
from loop_controller.controller import LoopController
from loop_controller.identity import ConfigIdentityProvider
from loop_controller.models import Agent, ApprovalRequest, GovernanceResult
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
) -> tuple[TestClient, _MockController]:
    controller = _MockController()
    app = build_app(
        controller,
        api_key=api_key,
        watcher=watcher,
        configure_logs=False,
        identity_provider=identity_provider,
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
            }
        ]
    }
    serialized = response.text
    assert "password" not in serialized
    assert "must-not-leak" not in serialized
    assert "HARNESS_SECRET" not in serialized


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
    assert admin_ops[0].target == "decision:d-1"


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
