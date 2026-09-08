"""CapabilityGraphAnalyzer 单元测试（v0.10.0）。"""

from __future__ import annotations

from loop_controller.capability import Capability, CapabilityGraph, CapabilityGraphAnalyzer
from loop_controller.infra.config_loader import (
    CapabilityCombinationRule,
    CapabilityDef,
    CapabilityProducer,
)
from loop_controller.models import ActionProposal


def _proposal(tool_name: str, **kwargs) -> ActionProposal:
    return ActionProposal(
        task_id="t1",
        call_id="c1",
        agent_id="a1",
        tool_name=tool_name,
        arguments=dict(kwargs),
        task_context="test",
    )


def test_extract_single_capability() -> None:
    rules = CapabilityRules(
        capabilities={
            "data_read": CapabilityDef(
                name="data_read",
                produced_by=[
                    CapabilityProducer(tool="read_file", arg_match={"path": "data/kb/**"})
                ],
            )
        },
        combination_rules=[],
    )
    analyzer = CapabilityGraphAnalyzer(rules.capabilities, rules.combination_rules)

    caps = analyzer.extract_capabilities(_proposal("read_file", path="data/kb/secret.txt"))
    assert caps == {"data_read"}

    caps = analyzer.extract_capabilities(_proposal("read_file", path="other/file.txt"))
    assert caps == set()


def test_extract_capability_with_arg_not_match() -> None:
    rules = CapabilityRules(
        capabilities={
            "email_external": CapabilityDef(
                name="email_external",
                produced_by=[
                    CapabilityProducer(
                        tool="send_email",
                        arg_match={"to": "*@*.com"},
                        arg_not_match={"to": "*@company.com"},
                    )
                ],
            )
        },
        combination_rules=[],
    )
    analyzer = CapabilityGraphAnalyzer(rules.capabilities, rules.combination_rules)

    assert "email_external" in analyzer.extract_capabilities(
        _proposal("send_email", to="attacker@external.com")
    )
    assert "email_external" not in analyzer.extract_capabilities(
        _proposal("send_email", to="boss@company.com")
    )
    assert "email_external" not in analyzer.extract_capabilities(
        _proposal("send_email", to="not-an-email")
    )


def test_build_graph_from_history() -> None:
    rules = CapabilityRules(
        capabilities={
            "data_read": CapabilityDef(
                name="data_read",
                produced_by=[
                    CapabilityProducer(tool="read_file", arg_match={"path": "data/kb/**"}),
                    CapabilityProducer(tool="query_database", arg_match={"sql": "SELECT*"}),
                ],
            )
        },
        combination_rules=[],
    )
    analyzer = CapabilityGraphAnalyzer(rules.capabilities, rules.combination_rules)
    history = [
        _proposal("read_file", path="data/kb/a.txt"),
        _proposal("query_database", sql="SELECT * FROM users"),
    ]
    graph = analyzer.build_graph(history)
    assert graph.names() == {"data_read"}


def test_analyze_data_exfil_via_email() -> None:
    rules = make_sample_rules()
    analyzer = CapabilityGraphAnalyzer(rules.capabilities, rules.combination_rules)

    history = [_proposal("read_file", path="data/kb/secret.txt")]
    current = _proposal("send_email", to="attacker@external.com")

    tags, score, matched, _triggered = analyzer.analyze(current, history)
    assert "data_exfil" in tags
    assert score == 90
    assert any(rule.id == "data_exfil_via_email" for rule in matched)


def test_analyze_data_exfil_via_http() -> None:
    rules = make_sample_rules()
    analyzer = CapabilityGraphAnalyzer(rules.capabilities, rules.combination_rules)

    history = [_proposal("query_database", sql="SELECT * FROM customers")]
    current = _proposal("fetch_url", url="https://external.com/upload")

    tags, score, matched, _triggered = analyzer.analyze(current, history)
    assert "data_exfil_http" in tags
    assert score == 80
    assert any(rule.id == "data_exfil_via_http" for rule in matched)


def test_analyze_no_false_positive() -> None:
    rules = make_sample_rules()
    analyzer = CapabilityGraphAnalyzer(rules.capabilities, rules.combination_rules)

    # 没有历史读取能力，直接发外部邮件不应触发 data_exfil
    tags, score, matched, _triggered = analyzer.analyze(
            _proposal("send_email", to="attacker@external.com"), []
        )
    assert tags == []
    assert score == 0
    assert matched == []

    # 读取了内部文件但发给公司邮箱，不应触发外部邮件能力
    history = [_proposal("read_file", path="data/kb/secret.txt")]
    tags, score, matched, _triggered = analyzer.analyze(
        _proposal("send_email", to="boss@company.com"), history
    )
    assert tags == []
    assert score == 0
    assert matched == []


def test_capability_graph_add() -> None:
    graph = CapabilityGraph()
    g2 = graph.add(Capability(name="x"))
    g3 = g2.add(Capability(name="y"))
    assert graph.names() == set()  # 不可变
    assert g2.names() == {"x"}
    assert g3.names() == {"x", "y"}


class CapabilityRules:
    """测试用小规则容器。"""

    def __init__(
        self,
        capabilities: dict[str, CapabilityDef],
        combination_rules: list[CapabilityCombinationRule],
    ) -> None:
        self.capabilities = capabilities
        self.combination_rules = combination_rules


def make_sample_rules() -> CapabilityRules:
    return CapabilityRules(
        capabilities={
            "data_read": CapabilityDef(
                name="data_read",
                produced_by=[
                    CapabilityProducer(tool="read_file", arg_match={"path": "data/kb/**"}),
                    CapabilityProducer(tool="query_database", arg_match={"sql": "SELECT*"}),
                ],
            ),
            "email_external": CapabilityDef(
                name="email_external",
                produced_by=[
                    CapabilityProducer(
                        tool="send_email",
                        arg_match={"to": "*@*.com"},
                        arg_not_match={"to": "*@company.com"},
                    )
                ],
            ),
            "network_external": CapabilityDef(
                name="network_external",
                produced_by=[
                    CapabilityProducer(tool="fetch_url", arg_match={"url": "https://*"})
                ],
            ),
        },
        combination_rules=[
            CapabilityCombinationRule(
                id="data_exfil_via_email",
                description="read + external email",
                requires_any=["data_read"],
                triggers_any=["email_external"],
                action="deny",
                reason="detected data exfil pattern: read + external email",
                risk_tags=["data_exfil"],
                score=90,
            ),
            CapabilityCombinationRule(
                id="data_exfil_via_http",
                description="read + external http",
                requires_any=["data_read"],
                triggers_any=["network_external"],
                action="require_approval",
                reason="detected data upload pattern: read + external http",
                risk_tags=["data_exfil_http"],
                score=80,
            ),
        ],
    )
