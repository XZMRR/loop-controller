"""AsyncApprovalManager 单元测试（T2.2 / §7.5）。

Iteration 5 起 ``ConfigR0Delegate`` 被 ``AsyncApprovalManager`` 替代：
审批请求只被持久化，不会立即返回 verdict；审批结果通过 CLI 写入后再由
Runtime 读取。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from loop_controller.approval_manager import AsyncApprovalManager
from loop_controller.models import ApprovalRecord, ApprovalRequest


def make_request(tool_name: str, decision_id: str = "d1") -> ApprovalRequest:
    return ApprovalRequest(
        request_id="r1",
        decision_id=decision_id,
        call_id="c1",
        task_id="t1",
        agent_id="researcher_001",
        tool_name=tool_name,
        arguments_masked={"to": "zhang@company.com"},
        reason="test",
        requester_id="alice",
        approver_id="zhang_manager",
        created_at=datetime.now(UTC),
    )


@dataclass
class _InMemoryApprovalStore:
    """测试用内存 ApprovalStore 实现。"""

    _requests: dict[str, ApprovalRequest] = field(default_factory=dict)
    _responses: dict[str, ApprovalRecord] = field(default_factory=dict)

    def submit_request(self, request: ApprovalRequest) -> None:
        self._requests[request.decision_id] = request

    def get_pending(self) -> list[ApprovalRequest]:
        return [req for did, req in self._requests.items() if did not in self._responses]

    def get_request(self, decision_id: str) -> ApprovalRequest | None:
        return self._requests.get(decision_id)

    def get_request_by_id(self, request_id: str) -> ApprovalRequest | None:
        for req in self._requests.values():
            if req.request_id == request_id:
                return req
        return None

    def record_response(self, record: ApprovalRecord) -> None:
        self._responses[record.decision_id] = record

    def get_record(self, decision_id: str) -> ApprovalRecord | None:
        return self._responses.get(decision_id)

    def refresh(self) -> None:
        pass


@pytest.fixture
def store() -> _InMemoryApprovalStore:
    return _InMemoryApprovalStore()


@pytest.fixture
def manager(store: _InMemoryApprovalStore) -> AsyncApprovalManager:
    return AsyncApprovalManager(store)


async def test_submit_persists_request(manager: AsyncApprovalManager, store: _InMemoryApprovalStore) -> None:
    request = make_request("send_email")
    await manager.submit(request)
    assert store.get_request("d1") == request
    assert manager.check("d1") is None


async def test_check_returns_record_after_response(manager: AsyncApprovalManager, store: _InMemoryApprovalStore) -> None:
    request = make_request("send_email")
    await manager.submit(request)

    record = ApprovalRecord(
        request_id=request.request_id,
        decision_id=request.decision_id,
        verdict="approve",
        approver_id="zhang_manager",
        comment="approved",
        decided_at=datetime.now(UTC),
    )
    store.record_response(record)

    assert manager.check("d1") == record


async def test_get_pending_excludes_responded(manager: AsyncApprovalManager, store: _InMemoryApprovalStore) -> None:
    req1 = make_request("send_email", decision_id="d1")
    req2 = make_request("send_email", decision_id="d2")
    await manager.submit(req1)
    await manager.submit(req2)

    store.record_response(
        ApprovalRecord(
            request_id=req1.request_id,
            decision_id=req1.decision_id,
            verdict="approve",
            approver_id="zhang_manager",
            comment="ok",
            decided_at=datetime.now(UTC),
        )
    )

    pending = store.get_pending()
    assert len(pending) == 1
    assert pending[0].decision_id == "d2"
