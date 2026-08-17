"""ConfigPermissionInteractionAnalyzer 单元测试（T2.3 / §6.2）。"""

from __future__ import annotations

import uuid

from loop_controller.infra.config_loader import PermissionCondition, PermissionRule
from loop_controller.models import ActionProposal
from loop_controller.permission_interaction import ConfigPermissionInteractionAnalyzer


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
