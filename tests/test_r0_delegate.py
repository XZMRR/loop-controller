"""ConfigR0Delegate 单元测试（T2.2 / §7.5）。"""

from __future__ import annotations

from datetime import datetime, timezone

from loop_controller.infra.config_loader import ApprovalConfig, ApprovalRule
from loop_controller.models import ApprovalRequest
from loop_controller.r0_delegate import ConfigR0Delegate


def make_request(tool_name: str, approver_id: str = "zhang_manager") -> ApprovalRequest:
    return ApprovalRequest(
        request_id="r1",
        decision_id="d1",
        call_id="c1",
        task_id="t1",
        agent_id="researcher_001",
        tool_name=tool_name,
        arguments_masked={"to": "zhang@company.com"},
        reason="test",
        requester_id="alice",
        approver_id=approver_id,
        created_at=datetime.now(timezone.utc),
    )


async def test_default_approve() -> None:
    delegate = ConfigR0Delegate(ApprovalConfig(default="zhang_manager"))
    record = await delegate.request_approval(make_request("send_email"))
    assert record.verdict == "approve"
    assert record.decision_id == "d1"
    assert record.approver_id == "zhang_manager"


async def test_rule_deny() -> None:
    delegate = ConfigR0Delegate(
        ApprovalConfig(
            default="zhang_manager",
            rules=[ApprovalRule(tool_name="send_email", approver="zhang_manager", behavior="deny")],
        )
    )
    record = await delegate.request_approval(make_request("send_email"))
    assert record.verdict == "deny"
    assert "behavior=deny" in record.comment


async def test_rule_approve_override_default() -> None:
    delegate = ConfigR0Delegate(
        ApprovalConfig(
            default="zhang_manager",
            rules=[ApprovalRule(tool_name="send_email", approver="zhang_manager", behavior="approve")],
        )
    )
    record = await delegate.request_approval(make_request("send_email"))
    assert record.verdict == "approve"
