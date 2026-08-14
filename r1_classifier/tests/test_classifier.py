"""RuleBasedClassifier 单元测试。"""

from __future__ import annotations

import pytest

from r1_classifier import (
    ActionProposal,
    Agent,
    CapabilityProfile,
    RiskLevel,
    RuleBasedClassifier,
    Task,
    ToolPermission,
)


@pytest.fixture()
def classifier() -> RuleBasedClassifier:
    return RuleBasedClassifier()


@pytest.fixture()
def task() -> Task:
    return Task(task_id="t1", user_id="u1", agent_id="a1", description="调研并发送报告")


@pytest.fixture()
def agent() -> Agent:
    return Agent(agent_id="a1", name="ResearchAssistant", profile_id="p1", owner_id="o1")


@pytest.fixture()
def profile() -> CapabilityProfile:
    return CapabilityProfile(
        profile_id="p1",
        version="1.0",
        description="研究助手岗位说明书",
        tools={
            "web_search": ToolPermission(tool_name="web_search", allowed=True),
            "read_file": ToolPermission(tool_name="read_file", allowed=True),
            "write_file": ToolPermission(tool_name="write_file", allowed=True),
            "send_email": ToolPermission(tool_name="send_email", allowed=True),
        },
    )


def make_proposal(tool_name: str, **arguments) -> ActionProposal:
    return ActionProposal(
        task_id="t1",
        call_id="c1",
        agent_id="a1",
        tool_name=tool_name,
        arguments=arguments,
    )


# --- CapabilityProfile 参与判定 ---


def test_unauthorized_tool_returns_high(classifier, task, agent, profile):
    signal = classifier.classify(
        task, agent, make_proposal("delete_file"), profile
    )
    assert signal.risk_level == RiskLevel.HIGH
    assert "unauthorized_tool" in signal.tags


# --- web_search ---


def test_web_search_normal_low(classifier, task, agent, profile):
    signal = classifier.classify(
        task, agent, make_proposal("web_search", query="OpenAI 合规争议"), profile
    )
    assert signal.risk_level == RiskLevel.LOW


def test_web_search_sensitive_query_medium(classifier, task, agent, profile):
    signal = classifier.classify(
        task, agent, make_proposal("web_search", query="公司内部 secret 泄露"), profile
    )
    assert signal.risk_level == RiskLevel.MEDIUM


# --- read_file ---


def test_read_file_normal_medium(classifier, task, agent, profile):
    signal = classifier.classify(
        task, agent, make_proposal("read_file", path="C:/kb/ai_checklist.md"), profile
    )
    assert signal.risk_level == RiskLevel.MEDIUM


def test_read_file_sensitive_path_high(classifier, task, agent, profile):
    signal = classifier.classify(
        task, agent, make_proposal("read_file", path="C:/secrets/credentials.json"), profile
    )
    assert signal.risk_level == RiskLevel.HIGH


# --- write_file ---


def test_write_file_normal_high(classifier, task, agent, profile):
    signal = classifier.classify(
        task, agent, make_proposal("write_file", path="C:/kb/summary.md"), profile
    )
    assert signal.risk_level == RiskLevel.HIGH


def test_write_file_sensitive_path_critical(classifier, task, agent, profile):
    signal = classifier.classify(
        task, agent, make_proposal("write_file", path="C:/app/.env"), profile
    )
    assert signal.risk_level == RiskLevel.CRITICAL


# --- send_email ---


def test_send_email_internal_high(classifier, task, agent, profile):
    signal = classifier.classify(
        task, agent, make_proposal("send_email", to="zhang@company.com"), profile
    )
    assert signal.risk_level == RiskLevel.HIGH


def test_send_email_external_critical(classifier, task, agent, profile):
    signal = classifier.classify(
        task, agent, make_proposal("send_email", to="attacker@evil.com"), profile
    )
    assert signal.risk_level == RiskLevel.CRITICAL
    assert signal.tags == ["send_email:to"]


# --- 边界情况 ---


def test_unknown_tool_default_low(classifier, task, agent, profile):
    """配置表中无规则但已授权（通过 ToolPermission 加入）的工具 -> low。"""
    tools = dict(profile.tools)
    tools["custom_tool"] = ToolPermission(tool_name="custom_tool", allowed=True)
    p = CapabilityProfile(
        profile_id="p1", version="1.0", description="", tools=tools
    )
    signal = classifier.classify(task, agent, make_proposal("custom_tool"), p)
    assert signal.risk_level == RiskLevel.LOW


def test_non_string_argument_skipped(classifier, task, agent, profile):
    """参数非字符串时不匹配正则，维持默认等级。"""
    signal = classifier.classify(
        task, agent, make_proposal("send_email", to=12345), profile
    )
    assert signal.risk_level == RiskLevel.HIGH


def test_custom_rules_dict_injected(classifier, task, agent, profile):
    """显式传入规则 dict 可覆盖默认配置。"""
    custom = {
        "classifier": {
            "unauthorized_tool_level": "critical",
            "tools": {
                "web_search": {
                    "default": "low",
                    "args": [
                        {
                            "key": "query",
                            "match": {"type": "regex", "pattern": "ban"},
                            "level": "high",
                            "reason": "命中自定义规则",
                        }
                    ],
                }
            },
        }
    }
    c = RuleBasedClassifier(rules=custom)
    signal = c.classify(
        task, agent, make_proposal("web_search", query="ban 关键词"), profile
    )
    assert signal.risk_level == RiskLevel.HIGH
    assert signal.reason == "命中自定义规则"
