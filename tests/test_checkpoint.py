"""Checkpoint 单元测试."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile

import pytest

from loop_controller import (
    ActionProposal,
    Agent,
    CapabilityProfile,
    Checkpoint,
    ConfigR0Delegate,
    Decision,
    JsonlAuditLogger,
    MockPolicyEngine,
    Task,
)


@pytest.fixture
def task() -> Task:
    return Task(task_id="t1", user_id="user_alice", session_id="s1", description="research")


@pytest.fixture
def agent() -> Agent:
    return Agent(agent_id="a1", name="researcher", profile_id="p1", owner_id="user_alice")


@pytest.fixture
def profile() -> CapabilityProfile:
    return CapabilityProfile(
        profile_id="p1",
        allowed_tools=["read_file", "write_file", "web_search", "send_email"],
    )


@pytest.fixture
def checkpoint(profile: CapabilityProfile) -> Checkpoint:
    return Checkpoint(
        policy_engine=MockPolicyEngine(),
        profile_store={"p1": profile},
    )


def test_evaluate_allow(task, agent, checkpoint):
    proposal = ActionProposal(
        task_id="t1",
        call_id="c1",
        agent_id="a1",
        tool_name="read_file",
        arguments={"path": "/tmp/report.md"},
        task_context="read report",
    )
    decision = checkpoint.evaluate(task, agent, proposal)
    assert decision.verdict == "allow"
    assert decision.call_id == "c1"


def test_evaluate_deny_for_unknown_profile(task, agent):
    agent_unknown = Agent(agent_id="a2", name="ghost", profile_id="p_missing", owner_id="user_alice")
    ckpt = Checkpoint(
        policy_engine=MockPolicyEngine(),
        profile_store={},
    )
    proposal = ActionProposal(
        task_id="t1",
        call_id="c1",
        agent_id="a2",
        tool_name="read_file",
        arguments={"path": "/tmp/report.md"},
        task_context="read report",
    )
    decision = ckpt.evaluate(task, agent_unknown, proposal)
    assert decision.verdict == "deny"


def test_evaluate_require_approval(task, agent, checkpoint):
    proposal = ActionProposal(
        task_id="t1",
        call_id="c2",
        agent_id="a1",
        tool_name="send_email",
        arguments={"to": "external@gmail.com", "subject": "summary"},
        task_context="send summary",
    )
    decision = checkpoint.evaluate(task, agent, proposal)
    assert decision.verdict == "require_approval"


def test_forward_with_allow(task, agent, checkpoint):
    proposal = ActionProposal(
        task_id="t1",
        call_id="c1",
        agent_id="a1",
        tool_name="read_file",
        arguments={"path": "/tmp/report.md"},
        task_context="read report",
    )
    decision = checkpoint.evaluate(task, agent, proposal)
    assert decision.verdict == "allow"

    result = checkpoint.forward(proposal, decision)
    assert result.status == "success"
    assert result.call_id == "c1"


def test_forward_rejects_deny(task, agent, checkpoint):
    proposal = ActionProposal(
        task_id="t1",
        call_id="c1",
        agent_id="a1",
        tool_name="send_email",
        arguments={"to": "external@gmail.com", "subject": "summary"},
        task_context="send summary",
    )
    from loop_controller import Decision
    deny = Decision(
        decision_id="d1",
        call_id="c1",
        task_id="t1",
        verdict="deny",
        reason="denied",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    with pytest.raises(ValueError, match="Cannot forward decision with verdict deny"):
        checkpoint.forward(proposal, deny)


def test_forward_rejects_replay(task, agent, checkpoint):
    proposal = ActionProposal(
        task_id="t1",
        call_id="c1",
        agent_id="a1",
        tool_name="read_file",
        arguments={"path": "/tmp/report.md"},
        task_context="read report",
    )
    decision = checkpoint.evaluate(task, agent, proposal)
    checkpoint.forward(proposal, decision)

    with pytest.raises(ValueError, match="Decision already used"):
        checkpoint.forward(proposal, decision)


def test_request_and_apply_approval_auto_approve(task, agent, profile):
    ckpt = Checkpoint(
        policy_engine=MockPolicyEngine(),
        profile_store={"p1": profile},
        r0_delegate=ConfigR0Delegate(approver_id="r0_boss", auto_approve=True),
    )
    proposal = ActionProposal(
        task_id="t1",
        call_id="c2",
        agent_id="a1",
        tool_name="send_email",
        arguments={"to": "external@gmail.com", "subject": "summary"},
        task_context="send summary",
    )
    initial = ckpt.evaluate(task, agent, proposal)
    assert initial.verdict == "require_approval"

    final = ckpt.request_and_apply_approval(task, agent, proposal, initial)
    assert final.verdict == "allow"


def test_audit_logging(task, agent, profile):
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "audit.jsonl"
        logger = JsonlAuditLogger(log_path)
        ckpt = Checkpoint(
            policy_engine=MockPolicyEngine(),
            profile_store={"p1": profile},
            audit_logger=logger,
        )
        proposal = ActionProposal(
            task_id="t1",
            call_id="c1",
            agent_id="a1",
            tool_name="read_file",
            arguments={"path": "/tmp/report.md"},
            task_context="read report",
        )
        ckpt.evaluate(task, agent, proposal)

        events = logger.read_events()
        assert len(events) == 1
        assert events[0]["action"] == "evaluate"
        assert events[0]["decision"] == "allow"
        assert events[0]["args_mask"]["path"] == "<path:report.md>"
