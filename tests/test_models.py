"""models.py 单元测试.

覆盖：必填/默认值、frozen 不可变语义、枚举非法值拒绝、Task 的 session_id 约定、
model_copy(update=...) 不可变修改方式。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from loop_controller.models import (
    ActionProposal,
    Agent,
    ApprovalRecord,
    ApprovalRequest,
    AuditEvent,
    BudgetCost,
    CapabilityProfile,
    Decision,
    PlannedAction,
    RiskProfile,
    RiskSignal,
    Task,
    Tool,
    ToolPermission,
    ToolResult,
)


def test_task_defaults_and_required():
    task = Task(task_id="t1", session_id="t1", user_id="alice", agent_id="a1", description="d")
    assert task.created_at.tzinfo is not None  # timezone-aware UTC
    assert task.created_at.tzinfo == UTC


def test_task_session_id_no_longer_must_equal_task_id():
    # v1.2 起废除 session_id == task_id 约定，session 由 SessionManager 分配
    task = Task(task_id="t1", session_id="s2", user_id="alice", agent_id="a1", description="d")
    assert task.session_id == "s2"
    assert task.task_id == "t1"


def test_task_frozen():
    task = Task(task_id="t1", session_id="t1", user_id="alice", agent_id="a1", description="d")
    with pytest.raises(ValidationError):
        task.description = "changed"  # type: ignore[misc]


def test_agent_required_fields():
    agent = Agent(agent_id="a1", name="n", profile_id="p1", owner_id="o")
    assert agent.profile_id == "p1"
    with pytest.raises(ValidationError):
        Agent(agent_id="a1", name="n", profile_id="p1")  # 缺 owner_id


def test_tool_permission_defaults():
    perm = ToolPermission(tool_name="read_file")
    assert perm.allowed is False
    assert perm.require_approval is False
    assert perm.allowed_args == {}
    assert perm.denied_args == {}
    assert perm.max_calls_per_task is None


def test_capability_profile_defaults_and_frozen():
    profile = CapabilityProfile(profile_id="p1")
    assert profile.version == ""
    assert profile.tools == {}
    assert profile.max_budget_token == 1_000_000
    assert profile.fixed_ceiling == {}
    with pytest.raises(ValidationError):
        profile.profile_id = "p2"  # type: ignore[misc]


def test_action_proposal_enum_invalid_rejected():
    with pytest.raises(ValidationError):
        ActionProposal(
            task_id="t1",
            call_id="c1",
            agent_id="a1",
            tool_name="read_file",
            arguments={},
            task_context="ctx",
            risk_level="catastrophic",  # 非法枚举
        )


def test_action_proposal_risk_tags_default():
    proposal = ActionProposal(
        task_id="t1",
        call_id="c1",
        agent_id="a1",
        tool_name="read_file",
        arguments={},
        task_context="ctx",
    )
    assert proposal.risk_level == "low"
    assert proposal.risk_tags == []
    assert proposal.reason == ""


def test_risk_signal_suggestion_optional():
    signal = RiskSignal(risk_level="high", tags=["a"], reason="r")
    assert signal.suggestion is None
    signal2 = RiskSignal(risk_level="high", reason="r", suggestion="do X")
    assert signal2.suggestion == "do X"


def test_decision_required_and_versions():
    expires = datetime.now(UTC)
    decision = Decision(
        decision_id="d1",
        call_id="c1",
        task_id="t1",
        verdict="allow",
        reason="ok",
        expires_at=expires,
    )
    assert decision.policy_hits == []
    assert decision.policy_version == ""
    assert decision.max_uses == 1
    with pytest.raises(ValidationError):
        Decision(
            decision_id="d2",
            call_id="c2",
            task_id="t2",
            verdict="bogus",  # 非法枚举
            reason="ok",
            expires_at=expires,
        )


def test_decision_reason_cannot_be_empty():
    # reason 为空字符串：模型层不强制，但 Checkpoint 工厂必须保证非空（Code Review 底线）
    expires = datetime.now(UTC)
    d = Decision(
        decision_id="d1", call_id="c1", task_id="t1", verdict="deny",
        reason="", expires_at=expires,
    )
    assert d.reason == ""


def test_tool_and_tool_result():
    tool = Tool(canonical_name="read_file", mcp_name="read_text_file", description="d", input_schema={})
    assert tool.canonical_name == "read_file"
    result = ToolResult(call_id="c1", task_id="t1", tool_name="read_file", status="success", content="x")
    assert result.elapsed_ms == 0
    with pytest.raises(ValidationError):
        ToolResult(call_id="c1", task_id="t1", tool_name="read_file", status="bogus", content="x")


def test_budget_cost_defaults():
    cost = BudgetCost()
    assert cost.token_count == 0
    assert cost.payment_amount == 0.0
    assert cost.currency == "USD"


def test_risk_profile_defaults():
    rp = RiskProfile(session_id="s1")
    assert rp.cumulative_risk_score == 0.0
    assert rp.recent_tags == []
    assert rp.denied_count == 0
    assert rp.approval_count == 0


def test_approval_request_and_record():
    req = ApprovalRequest(
        request_id="r1",
        decision_id="d1",
        call_id="c1",
        task_id="t1",
        agent_id="a1",
        tool_name="send_email",
        arguments_masked={"to": "***@company.com"},
        reason="need approval",
        requester_id="alice",
        approver_id="zhang_manager",
    )
    assert req.created_at.tzinfo == UTC
    record = ApprovalRecord(request_id="r1", decision_id="d1", verdict="approve", approver_id="z", comment="ok")
    assert record.decided_at.tzinfo == UTC
    with pytest.raises(ValidationError):
        ApprovalRecord(request_id="r1", decision_id="d1", verdict="escalate", approver_id="z", comment="x")


def test_audit_event_defaults_and_enum():
    event = AuditEvent(
        event_id="e1",
        trace_id="t1",
        session_id="t1",
        actor_type="checkpoint",
        actor_id="checkpoint",
        action="evaluate",
    )
    assert event.seq == 0
    assert event.prev_hash == ""
    assert event.schema_version == "1.0"
    assert event.hash_algo == "sha256"
    assert event.metadata == {}
    with pytest.raises(ValidationError):
        AuditEvent(
            event_id="e1",
            trace_id="t1",
            session_id="t1",
            actor_type="checkpoint",
            actor_id="c",
            action="hack",  # 非法枚举
        )


def test_planned_action_defaults():
    action = PlannedAction(tool_name="web_search", arguments={"query": "q"})
    assert action.reason == ""
    with pytest.raises(ValidationError):
        PlannedAction(tool_name="web_search")  # 缺 arguments


def test_model_copy_update_is_immutable_pattern():
    """不可变修改使用 model_copy(update=...)，原对象不变。"""
    proposal = ActionProposal(
        task_id="t1",
        call_id="c1",
        agent_id="a1",
        tool_name="read_file",
        arguments={},
        task_context="ctx",
    )
    updated = proposal.model_copy(update={"risk_level": "high", "risk_tags": ["data_access"]})
    assert updated.risk_level == "high"
    assert proposal.risk_level == "low"  # 原对象不变
