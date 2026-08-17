"""权限组合规则分析器（§6.2 / 开发指南 T2.3）.

``ConfigPermissionInteractionAnalyzer`` 加载 ``permission_rules.yaml`` 中的静态规则，
对当前动作与任务历史做 POSIX glob 匹配；命中 deny 立即短路，命中 require_approval
则挂起标记继续走 Rego（deny 优先原则）。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from loop_controller.infra.config_loader import PermissionCondition, PermissionRule
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
