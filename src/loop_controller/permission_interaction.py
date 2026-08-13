"""权限组合风险分析.

检测多个独立权限/工具组合后产生的新能力（A + B > C）。
MVP 阶段使用静态规则表，接口已预留未来扩展为图分析或能力集合代数。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from loop_controller.action_proposal import ActionProposal
from loop_controller.classifier import RiskSignal


@dataclass(frozen=True)
class PermissionInteractionRule:
    """静态权限组合规则.

    Attributes:
        id: 规则标识。
        tool_names: 触发规则的工具名集合。
        condition: 人类可读的条件描述；MVP 阶段由代码解析简化条件。
        risk: 风险等级。
        action: R2 应采取的动作，如 require_approval / deny。
    """

    id: str
    tool_names: list[str]
    condition: str
    risk: str
    action: str


@runtime_checkable
class PermissionInteractionAnalyzer(Protocol):
    """权限组合分析器接口."""

    def check(
        self,
        current_proposal: ActionProposal,
        history: list[ActionProposal],
    ) -> RiskSignal | None:
        """检查当前动作与历史动作组合后是否产生新风险.

        Args:
            current_proposal: 当前待判定动作。
            history: 同一任务内已发生的动作申报。

        Returns:
            若触发组合风险则返回 RiskSignal，否则返回 None。
        """
        ...


class StaticPermissionInteractionAnalyzer:
    """基于静态规则表的权限组合分析器.

    规则示例：
        read_file 读取 contact 后 send_email → 疑似泄露通讯录钓鱼。
    """

    def __init__(self, rules: list[PermissionInteractionRule] | None = None) -> None:
        """初始化规则表."""
        self._rules = rules or _DEFAULT_RULES

    def check(
        self,
        current_proposal: ActionProposal,
        history: list[ActionProposal],
    ) -> RiskSignal | None:
        """按规则表匹配组合风险."""
        tool_sequence = [p.tool_name for p in history] + [current_proposal.tool_name]
        tool_set = set(tool_sequence)

        for rule in self._rules:
            if set(rule.tool_names).issubset(tool_set):
                return RiskSignal(
                    risk_level="high",  # MVP 简化：组合风险统一 high
                    tags=["permission_interaction", rule.id],
                    reason=f"触发权限组合规则：{rule.condition}",
                )
        return None


_DEFAULT_RULES: list[PermissionInteractionRule] = [
    PermissionInteractionRule(
        id="read_contact_then_email",
        tool_names=["read_file", "send_email"],
        condition="读取文件后发送外部邮件，存在数据外泄风险",
        risk="high",
        action="require_approval",
    ),
    PermissionInteractionRule(
        id="search_then_write",
        tool_names=["web_search", "write_file"],
        condition="搜索外部信息后写入本地文件，需确认信息来源可信度",
        risk="medium",
        action="allow",
    ),
]
