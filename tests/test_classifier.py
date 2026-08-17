"""RuleBasedClassifier 单元测试（§3.5 四条规则 + 敏感模式扫描）。"""

from __future__ import annotations

import uuid

import pytest

from loop_controller.classifier import RuleBasedClassifier
from loop_controller.models import (
    ActionProposal,
    Agent,
    CapabilityProfile,
    Task,
    ToolPermission,
)


@pytest.fixture
def task() -> Task:
    return Task(task_id="t1", session_id="t1", user_id="alice", agent_id="researcher_001",
                description="调研 AI 合规")


@pytest.fixture
def agent() -> Agent:
    return Agent(agent_id="researcher_001", name="RA", profile_id="p1", owner_id="zhang_manager")


@pytest.fixture
def profile() -> CapabilityProfile:
    return CapabilityProfile(
        profile_id="p1",
        tools={
            "web_search": ToolPermission(tool_name="web_search", allowed=True),
            "read_file": ToolPermission(tool_name="read_file", allowed=True),
            "send_email": ToolPermission(tool_name="send_email", allowed=True),
        },
    )


def make_proposal(task: Task, agent: Agent, tool_name: str, arguments: dict) -> ActionProposal:
    return ActionProposal(
        task_id=task.task_id,
        call_id=uuid.uuid4().hex,
        agent_id=agent.agent_id,
        tool_name=tool_name,
        arguments=arguments,
        task_context=task.description[:200],
    )


def classify(rule: RuleBasedClassifier, task, agent, profile, tool_name, arguments):
    return rule.classify(
        task, agent, make_proposal(task, agent, tool_name, arguments), profile
    )


def test_send_email_high_external(task, agent, profile) -> None:
    signal = classify(
        RuleBasedClassifier(), task, agent, profile, "send_email",
        {"to": "zhang@company.com"},
    )
    assert signal.risk_level == "high"
    assert "external_communication" in signal.tags


def test_read_file_medium_data_access(task, agent, profile) -> None:
    signal = classify(
        RuleBasedClassifier(), task, agent, profile, "read_file",
        {"path": "/data/kb/doc.md"},
    )
    assert signal.risk_level == "medium"
    assert signal.tags == ["data_access"]


def test_web_search_low(task, agent, profile) -> None:
    signal = classify(
        RuleBasedClassifier(), task, agent, profile, "web_search",
        {"query": "OpenAI 合规"},
    )
    assert signal.risk_level == "low"
    assert signal.tags == []


def test_email_value_raises_to_high_with_pii(task, agent, profile) -> None:
    # read_file（基础 medium）参数值含邮箱 → 提升 high + pii_involved
    signal = classify(
        RuleBasedClassifier(), task, agent, profile, "read_file",
        {"path": "/data/kb/doc.md", "content": "contact alice@company.com"},
    )
    assert signal.risk_level == "high"
    assert signal.tags == ["data_access", "pii_involved"]


def test_password_field_credential(task, agent, profile) -> None:
    signal = classify(
        RuleBasedClassifier(), task, agent, profile, "web_search",
        {"password": "hunter2"},
    )
    assert signal.risk_level == "high"
    assert signal.tags == ["credential_involved"]


def test_bearer_token_value_credential(task, agent, profile) -> None:
    signal = classify(
        RuleBasedClassifier(), task, agent, profile, "web_search",
        {"authorization": "Bearer abc.def.ghi"},
    )
    assert signal.risk_level == "high"
    assert signal.tags == ["credential_involved"]
