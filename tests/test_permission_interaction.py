"""ConfigPermissionInteractionAnalyzer 单元测试（T2.3 / §6.2）。"""

from __future__ import annotations

import uuid

from loop_controller.infra.config_loader import (
    CapabilityCombinationRule,
    CapabilityDef,
    CapabilityProducer,
    CapabilityRules,
    PermissionCondition,
    PermissionRule,
)
from loop_controller.models import ActionProposal
from loop_controller.permission_interaction import (
    CapabilityBasedPermissionAnalyzer,
    CompositePermissionInteractionAnalyzer,
    ConfigPermissionInteractionAnalyzer,
)


def make_proposal(tool_name: str, arguments: dict) -> ActionProposal:
    return ActionProposal(
        task_id="t1",
        call_id=uuid.uuid4().hex,
        agent_id="a1",
        tool_name=tool_name,
        arguments=arguments,
        task_context="ctx",
    )


def make_rule(action: str) -> PermissionRule:
    return PermissionRule(
        id="kb_read_plus_external_email",
        description="读取知识库后向外部邮箱发信",
        when_all=[
            PermissionCondition(
                history_tool="read_file",
                history_arg_match={"path": "/data/kb/**"},
            ),
            PermissionCondition(
                current_tool="send_email",
                current_arg_not_match={"to": "*@company.com"},
            ),
        ],
        action=action,
        reason="内部知识库内容禁止外发",
    )


def test_deny_rule_hits_after_kb_read_and_external_email() -> None:
    analyzer = ConfigPermissionInteractionAnalyzer([make_rule("deny")])
    history = [make_proposal("read_file", {"path": "/data/kb/checklist.md"})]
    current = make_proposal("send_email", {"to": "attacker@gmail.com"})

    rule = analyzer.check(current, history)
    assert rule is not None
    assert rule.action == "deny"


def test_no_hit_when_email_is_internal() -> None:
    analyzer = ConfigPermissionInteractionAnalyzer([make_rule("deny")])
    history = [make_proposal("read_file", {"path": "/data/kb/checklist.md"})]
    current = make_proposal("send_email", {"to": "zhang@company.com"})

    assert analyzer.check(current, history) is None


def test_no_hit_without_kb_read_history() -> None:
    analyzer = ConfigPermissionInteractionAnalyzer([make_rule("deny")])
    current = make_proposal("send_email", {"to": "attacker@gmail.com"})

    assert analyzer.check(current, []) is None


def test_require_approval_rule() -> None:
    rule = PermissionRule(
        id="contact_plus_external_email",
        description="读取联系人/知识库后向外部邮箱发信 = 数据外泄风险",
        when_all=[
            PermissionCondition(
                history_tool="read_file",
                history_arg_match={"path": "**/*contact*"},
            ),
            PermissionCondition(
                current_tool="send_email",
                current_arg_not_match={"to": "*@company.com"},
            ),
        ],
        action="require_approval",
        reason="检测到 读取内部资料→外发邮件 组合",
    )
    analyzer = ConfigPermissionInteractionAnalyzer([rule])

    history = [make_proposal("read_file", {"path": "/data/kb/customer_contacts.csv"})]
    current = make_proposal("send_email", {"to": "attacker@gmail.com"})

    hit = analyzer.check(current, history)
    assert hit is not None
    assert hit.action == "require_approval"


def test_capability_based_deny_short_circuit() -> None:
    rules = CapabilityRules(
        capabilities={
            "data_read": CapabilityDef(
                name="data_read",
                produced_by=[
                    CapabilityProducer(tool="read_file", arg_match={"path": "data/kb/**"})
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
            )
        ],
    )
    analyzer = CapabilityBasedPermissionAnalyzer(rules)

    history = [make_proposal("read_file", {"path": "data/kb/secret.txt"})]
    current = make_proposal("send_email", {"to": "attacker@external.com"})

    rule = analyzer.check(current, history)
    assert rule is not None
    assert rule.action == "deny"
    assert "data_exfil" in rule.risk_tags
    assert rule.score == 90


def test_capability_based_require_approval() -> None:
    rules = CapabilityRules(
        capabilities={
            "data_read": CapabilityDef(
                name="data_read",
                produced_by=[
                    CapabilityProducer(tool="query_database", arg_match={"sql": "SELECT*"})
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
                id="data_exfil_via_http",
                description="read + external http",
                requires_any=["data_read"],
                triggers_any=["network_external"],
                action="require_approval",
                reason="detected data upload pattern: read + external http",
                risk_tags=["data_exfil_http"],
                score=80,
            )
        ],
    )
    analyzer = CapabilityBasedPermissionAnalyzer(rules)

    history = [make_proposal("query_database", {"sql": "SELECT * FROM customers"})]
    current = make_proposal("fetch_url", {"url": "https://external.com/upload"})

    rule = analyzer.check(current, history)
    assert rule is not None
    assert rule.action == "require_approval"
    assert "data_exfil_http" in rule.risk_tags
    assert rule.score == 80


def test_composite_static_and_capability_rules() -> None:
    """Composite 分析器同时保留静态规则与能力规则。"""
    static_rule = make_rule("deny")
    capability_rules = CapabilityRules(
        capabilities={
            "data_read": CapabilityDef(
                name="data_read",
                produced_by=[
                    CapabilityProducer(tool="read_file", arg_match={"path": "data/kb/**"})
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
            )
        ],
    )
    composite = CompositePermissionInteractionAnalyzer(
        ConfigPermissionInteractionAnalyzer([static_rule]),
        CapabilityBasedPermissionAnalyzer(capability_rules),
    )

    history = [make_proposal("read_file", {"path": "data/kb/checklist.md"})]
    current = make_proposal("send_email", {"to": "attacker@gmail.com"})

    rule = composite.check(current, history)
    assert rule is not None
    assert rule.action == "deny"
    assert "data_exfil" in rule.risk_tags
    assert rule.score == 90


def test_composite_require_approval_only() -> None:
    """只有 require_approval 规则命中时返回 require_approval。"""
    capability_rules = CapabilityRules(
        capabilities={
            "data_read": CapabilityDef(
                name="data_read",
                produced_by=[
                    CapabilityProducer(tool="query_database", arg_match={"sql": "SELECT*"})
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
                id="data_exfil_via_http",
                description="read + external http",
                requires_any=["data_read"],
                triggers_any=["network_external"],
                action="require_approval",
                reason="detected data upload pattern: read + external http",
                risk_tags=["data_exfil_http"],
                score=80,
            )
        ],
    )
    composite = CompositePermissionInteractionAnalyzer(
        ConfigPermissionInteractionAnalyzer([]),
        CapabilityBasedPermissionAnalyzer(capability_rules),
    )

    history = [make_proposal("query_database", {"sql": "SELECT * FROM customers"})]
    current = make_proposal("fetch_url", {"url": "https://external.com/upload"})

    rule = composite.check(current, history)
    assert rule is not None
    assert rule.action == "require_approval"
