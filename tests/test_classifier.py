"""R1 轻量分类器单元测试."""

from __future__ import annotations

import pytest

from loop_controller import ActionProposal, Agent, CapabilityProfile, RuleBasedClassifier, Task


@pytest.fixture
def base_task() -> Task:
    return Task(task_id="t1", user_id="user_alice", session_id="s1", description="test")


@pytest.fixture
def base_agent() -> Agent:
    return Agent(agent_id="a1", name="researcher", profile_id="p1", owner_id="user_alice")


@pytest.fixture
def base_profile() -> CapabilityProfile:
    return CapabilityProfile(profile_id="p1")


@pytest.fixture
def classifier() -> RuleBasedClassifier:
    return RuleBasedClassifier()


def test_send_email_risk(base_task, base_agent, base_profile, classifier):
    proposal = ActionProposal(
        task_id="t1",
        call_id="c1",
        agent_id="a1",
        tool_name="send_email",
        arguments={"to": "zhang@company.com", "subject": "summary"},
        task_context="send research summary",
    )
    signal = classifier.classify(base_task, base_agent, proposal, base_profile)
    assert signal.risk_level == "high"
    assert "external_communication" in signal.tags
    assert signal.suggestion is not None


def test_read_file_risk(base_task, base_agent, base_profile, classifier):
    proposal = ActionProposal(
        task_id="t1",
        call_id="c1",
        agent_id="a1",
        tool_name="read_file",
        arguments={"path": "/tmp/summary.md"},
        task_context="read research summary",
    )
    signal = classifier.classify(base_task, base_agent, proposal, base_profile)
    assert signal.risk_level == "medium"
    assert "data_access" in signal.tags


def test_web_search_low_risk(base_task, base_agent, base_profile, classifier):
    proposal = ActionProposal(
        task_id="t1",
        call_id="c1",
        agent_id="a1",
        tool_name="web_search",
        arguments={"query": "OpenAI compliance controversy"},
        task_context="search public information",
    )
    signal = classifier.classify(base_task, base_agent, proposal, base_profile)
    assert signal.risk_level == "low"


def test_inter_agent_ignored(base_task, base_agent, base_profile, classifier):
    proposal = ActionProposal(
        task_id="t1",
        call_id="c1",
        agent_id="a1",
        tool_name="delegate",
        arguments={"target_agent": "a2"},
        task_context="delegate subtask",
        type="inter_agent",
    )
    signal = classifier.classify(base_task, base_agent, proposal, base_profile)
    assert signal.risk_level == "low"
    assert "inter_agent" in signal.tags
