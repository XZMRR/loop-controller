"""权限组合规则分析器（§6.2 / 开发指南 T2.3）.

``ConfigPermissionInteractionAnalyzer`` 加载 ``permission_rules.yaml`` 中的静态规则，
对当前动作与任务历史做 POSIX glob 匹配；命中 deny 立即短路，命中 require_approval
则挂起标记继续走 Rego（deny 优先原则）。
"""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

from loop_controller.capability import CapabilityGraphAnalyzer
from loop_controller.infra.config_loader import (
    CapabilityCombinationRule,
    CapabilityRules,
    PermissionCondition,
    PermissionRule,
)
from loop_controller.models import ActionProposal
from loop_controller.utils.globmatch import glob_match


@runtime_checkable
class PermissionInteractionAnalyzer(Protocol):
    """权限组合分析接口（§6.2）。"""

    def check(
        self,
        current: ActionProposal,
        history: list[ActionProposal],
    ) -> PermissionRule | None: ...


class ConfigPermissionInteractionAnalyzer:
    """基于 YAML 配置的组合规则分析器。"""

    def __init__(self, rules: list[PermissionRule]) -> None:
        self._rules = list(rules)

    def check(
        self,
        current: ActionProposal,
        history: list[ActionProposal],
    ) -> PermissionRule | None:
        for rule in self._rules:
            if self._match(rule, current, history):
                return rule
        return None

    @staticmethod
    def _match(rule: PermissionRule, current: ActionProposal, history: list[ActionProposal]) -> bool:
        return all(
            ConfigPermissionInteractionAnalyzer._match_condition(cond, current, history)
            for cond in rule.when_all
        )

    @staticmethod
    def _match_condition(
        cond: PermissionCondition,
        current: ActionProposal,
        history: list[ActionProposal],
    ) -> bool:
        # history 侧：任一历史动作满足 tool_name + 参数 glob 匹配
        if cond.history_tool:
            return any(
                h.tool_name == cond.history_tool
                and _args_match_patterns(h.arguments, cond.history_arg_match or {})
                for h in history
            )

        # current 侧：tool_name 匹配且参数不满足（not_match）指定 glob
        if cond.current_tool:
            if current.tool_name != cond.current_tool:
                return False
            if cond.current_arg_not_match:
                # not_match：当前参数匹配了这些 pattern 才返回 False
                return not _args_match_patterns(current.arguments, cond.current_arg_not_match)
            return True

        return True


def _args_match_patterns(args: dict, patterns: dict[str, str]) -> bool:
    """args 中每个 key 至少有一个 glob pattern 匹配其字符串值。"""
    for key, pattern in patterns.items():
        if key not in args:
            return False
        value = str(args[key])
        if not glob_match(pattern, value):
            return False
    return True


class CapabilityBasedPermissionAnalyzer:
    """基于能力集合的组合风险分析器（v0.10.0）。

    把工具调用抽象为能力，检测"历史能力 + 当前能力"触发的组合风险。
    命中 deny 立即短路，命中 require_approval 则挂起标记继续走 Rego。
    多个规则命中时合并风险标签、取最高分数、按 deny > require_approval 定 action。
    """

    def __init__(self, rules: CapabilityRules) -> None:
        self._analyzer = CapabilityGraphAnalyzer(
            capability_defs=rules.capabilities,
            combination_rules=rules.combination_rules,
        )

    def check(
        self,
        current: ActionProposal,
        history: list[ActionProposal],
    ) -> PermissionRule | None:
        risk_tags, score, matched = self._analyzer.analyze(current, history)
        if not matched:
            return None
        return self._build_rule(matched, risk_tags, score)

    @staticmethod
    def _build_rule(
        matched: list[CapabilityCombinationRule],
        risk_tags: list[str],
        score: int,
    ) -> PermissionRule:
        """把命中的能力组合规则归并为单个 PermissionRule。"""
        action: Literal["deny", "require_approval"] = "require_approval"
        if any(rule.action == "deny" for rule in matched):
            action = "deny"
        return PermissionRule(
            id="capability:combined",
            description="combined capability risks",
            when_all=[],
            action=action,
            reason="; ".join(rule.reason for rule in matched),
            risk_tags=list(risk_tags),
            score=score,
        )


def _merge_rules(a: PermissionRule, b: PermissionRule) -> PermissionRule:
    """合并两个命中的 PermissionRule：deny 优先，标签合并，分数取最大。"""
    action: Literal["deny", "require_approval"] = "require_approval"
    if a.action == "deny" or b.action == "deny":
        action = "deny"
    ids = [a.id, b.id]
    descriptions = [a.description, b.description]
    reasons = [a.reason, b.reason]
    # 去重同时保持顺序
    unique_ids = list(dict.fromkeys(ids))
    unique_descs = list(dict.fromkeys(descriptions))
    unique_reasons = list(dict.fromkeys(reasons))
    tags = sorted(set(a.risk_tags) | set(b.risk_tags))
    return PermissionRule(
        id=" + ".join(unique_ids),
        description=" + ".join(unique_descs),
        when_all=[],
        action=action,
        reason="; ".join(unique_reasons),
        risk_tags=tags,
        score=max(a.score, b.score),
    )


class CompositePermissionInteractionAnalyzer:
    """组合分析器：按顺序运行多个 analyzer，合并结果。

    用于同时保留静态 YAML 规则与 v0.10.0 能力规则。
    deny 优先于 require_approval。
    """

    def __init__(self, *analyzers: PermissionInteractionAnalyzer) -> None:
        self._analyzers = list(analyzers)

    def check(
        self,
        current: ActionProposal,
        history: list[ActionProposal],
    ) -> PermissionRule | None:
        result: PermissionRule | None = None
        for analyzer in self._analyzers:
            rule = analyzer.check(current, history)
            if rule is None:
                continue
            if result is None:
                result = rule
            else:
                result = _merge_rules(result, rule)
            # deny 已是最严格裁决，可提前短路（但仍继续收集其他标签用于审计）
        return result
