"""端到端测试：多任务场景下的 BudgetLedger 与 RiskStateManager 拦截.

验证：
1. 同一 Task 内，Budget 超过上限后，第三个动作被 deny；
2. 同一 Session 跨多个 Task 内，连续被拒绝次数达到阈值后，
   RiskStateManager 自动触发拦截，新请求直接被 deny；
3. 审计日志正确记录所有判定事件。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from loop_controller import (
    ActionProposal,
    Agent,
    CapabilityProfile,
    Checkpoint,
    CheckpointConfig,
    InMemoryBudgetLedger,
    InMemoryRiskStateManager,
    JsonlAuditLogger,
    MockPolicyEngine,
    Task,
)


@pytest.fixture
def agent() -> Agent:
    return Agent(
        agent_id="researcher_001",
        name="Research Assistant",
        profile_id="researcher_profile",
        owner_id="user_alice",
    )


@pytest.fixture
def profile() -> CapabilityProfile:
    return CapabilityProfile(
        profile_id="researcher_profile",
        allowed_tools=["read_file"],
    )


@pytest.fixture
def checkpoint(agent: Agent, profile: CapabilityProfile) -> Checkpoint:
    """配置低预算、风险阈值 3 的 Checkpoint，便于触发拦截。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "audit.jsonl"
        audit_logger = JsonlAuditLogger(log_path)
        budget_ledger = InMemoryBudgetLedger(default_token_budget=2)
        risk_state_manager = InMemoryRiskStateManager()
        checkpoint = Checkpoint(
            policy_engine=MockPolicyEngine(),
            profile_store={"researcher_profile": profile},
            budget_ledger=budget_ledger,
            risk_state_manager=risk_state_manager,
            audit_logger=audit_logger,
            config=CheckpointConfig(risk_denied_threshold=3),
        )
        # 将日志路径附加上去，便于测试断言
        checkpoint._test_log_path = log_path  # type: ignore[attr-defined]
        yield checkpoint


def _propose(task: Task, call_id: str, tool_name: str, arguments: dict) -> ActionProposal:
    return ActionProposal(
        task_id=task.task_id,
        call_id=call_id,
        agent_id="researcher_001",
        tool_name=tool_name,
        arguments=arguments,
        task_context=f"use {tool_name}",
    )


def test_multi_task_budget_and_risk_state_interception(
    checkpoint: Checkpoint, agent: Agent, profile: CapabilityProfile
) -> None:
    """模拟同一 Session 下多个 Task，验证 Budget 和 RiskState 拦截。"""
    session_id = "session-research-001"

    # ---------- Task 1：预算限制（2 个 token 预算） ----------
    task1 = Task(
        task_id="task-1",
        user_id="user_alice",
        session_id=session_id,
        description="Read three files",
    )

    p1 = _propose(task1, "task1-call-1", "read_file", {"path": "/tmp/report1.md"})
    d1 = checkpoint.evaluate(task1, agent, p1)
    assert d1.verdict == "allow"
    checkpoint.forward(p1, d1)

    p2 = _propose(task1, "task1-call-2", "read_file", {"path": "/tmp/report2.md"})
    d2 = checkpoint.evaluate(task1, agent, p2)
    assert d2.verdict == "allow"
    checkpoint.forward(p2, d2)

    # 第三个动作应被 Budget 拦截（已预留 2，第 3 个需要 1）
    p3 = _propose(task1, "task1-call-3", "read_file", {"path": "/tmp/report3.md"})
    d3 = checkpoint.evaluate(task1, agent, p3)
    assert d3.verdict == "deny"
    assert "Budget" in d3.reason

    # ---------- Task 2：策略拒绝（工具不在 allowed_tools 中） ----------
    task2 = Task(
        task_id="task-2",
        user_id="user_alice",
        session_id=session_id,
        description="Try to send email",
    )
    p4 = _propose(task2, "task2-call-1", "send_email", {"to": "zhang@company.com"})
    d4 = checkpoint.evaluate(task2, agent, p4)
    assert d4.verdict == "deny"

    # 此时 Session 内已有 2 次拒绝（Task1 预算 + Task2 策略）
    risk = checkpoint.risk_state_manager.get_session_risk(session_id)
    assert risk.denied_count == 2

    # ---------- Task 3：再次策略拒绝 ----------
    task3 = Task(
        task_id="task-3",
        user_id="user_alice",
        session_id=session_id,
        description="Try to write file",
    )
    p5 = _propose(task3, "task3-call-1", "write_file", {"path": "/tmp/x.md", "content": "x"})
    d5 = checkpoint.evaluate(task3, agent, p5)
    assert d5.verdict == "deny"

    # 拒绝次数达到阈值 3
    risk = checkpoint.risk_state_manager.get_session_risk(session_id)
    assert risk.denied_count == 3

    # ---------- Task 4：RiskState 自动拦截 ----------
    task4 = Task(
        task_id="task-4",
        user_id="user_alice",
        session_id=session_id,
        description="Read file after repeated denials",
    )
    p6 = _propose(task4, "task4-call-1", "read_file", {"path": "/tmp/report4.md"})
    d6 = checkpoint.evaluate(task4, agent, p6)
    assert d6.verdict == "deny"
    assert "Session risk threshold" in d6.reason

    # 校验审计日志：至少包含 4 次 deny 和 2 次 allow
    events = checkpoint.audit_logger.read_events()
    verdicts = [e["decision"] for e in events]
    assert verdicts.count("allow") == 2
    assert verdicts.count("deny") >= 4

    # 校验 Task 4 的 deny 是因为 Session risk threshold
    task4_events = [e for e in events if e["trace_id"] == "task-4"]
    assert task4_events[0]["reason"].startswith("Session risk threshold")
