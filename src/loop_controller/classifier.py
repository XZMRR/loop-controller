"""R1 轻量分类器.

轻量分类器位于 R1 执行层，对即将提交的 ActionProposal 做快速风险扫描，
输出 RiskSignal。它只输出风险信号，不做最终是否执行的判定。
MVP 使用规则实现；接口已预留，未来可替换为专用小模型或更复杂编码器。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

from loop_controller.action_proposal import ActionProposal
from loop_controller.agent import Agent
from loop_controller.capability_profile import CapabilityProfile
from loop_controller.task import Task


@dataclass(frozen=True)
class RiskSignal:
    """轻量分类器输出的风险信号.

    Attributes:
        risk_level: 风险等级，R2 可参考。
        tags: 风险标签列表，如 ["external_communication", "pii_involved"]。
        reason: 可解释的风险原因。
        suggestion: 可选的缓解建议，供 R1 调整动作。
    """

    risk_level: Literal["low", "medium", "high", "critical"]
    tags: list[str] = field(default_factory=list)
    reason: str = ""
    suggestion: str | None = None


@runtime_checkable
class LightweightClassifier(Protocol):
    """R1 轻量分类器接口.

    实现类必须只输出 RiskSignal，不能输出 allow/deny 等决策。
    """

    def classify(
        self,
        task: Task,
        agent: Agent,
        proposal: ActionProposal,
        profile: CapabilityProfile,
    ) -> RiskSignal:
        """对 ActionProposal 做风险预检，返回风险信号.

        Args:
            task: 当前任务上下文。
            agent: 执行 Agent。
            proposal: 待提交的动作申报。
            profile: Agent 的能力画像。

        Returns:
            RiskSignal，包含风险等级、标签、原因和建议。
        """
        ...


class RuleBasedClassifier:
    """基于规则的轻量分类器，MVP 打桩实现.

    规则按工具名匹配，便于快速调整。未来可替换为基于小模型或
    更复杂特征工程的分类器，而无需改动 R2。
    """

    def __init__(self, rules: dict[str, RiskSignal] | None = None) -> None:
        """初始化规则表.

        Args:
            rules: 可选的自定义规则，key 为规范化工具名。
        """
        self._rules = rules or _DEFAULT_RULES

    def classify(
        self,
        task: Task,
        agent: Agent,
        proposal: ActionProposal,
        profile: CapabilityProfile,
    ) -> RiskSignal:
        """按工具名查找预设规则，返回对应 RiskSignal.

        未命中规则时返回 low 风险。
        """
        if proposal.type != "tool_call":
            return RiskSignal(
                risk_level="low",
                tags=["inter_agent"],
                reason="非工具调用类型，由 R1 内部处理",
            )

        if proposal.tool_name in self._rules:
            return self._rules[proposal.tool_name]

        return RiskSignal(
            risk_level="low",
            tags=[],
            reason="常规操作",
        )


# MVP 默认规则表。可按项目需求扩展。
_DEFAULT_RULES: dict[str, RiskSignal] = {
    "send_email": RiskSignal(
        risk_level="high",
        tags=["external_communication"],
        reason="send_email 涉及外部通信",
        suggestion="请确认收件人白名单",
    ),
    "write_file": RiskSignal(
        risk_level="medium",
        tags=["data_modification"],
        reason="写文件可能覆盖或篡改已有内容",
    ),
    "read_file": RiskSignal(
        risk_level="medium",
        tags=["data_access"],
        reason="读取本地文件",
    ),
    "web_search": RiskSignal(
        risk_level="low",
        tags=["external_query"],
        reason="向外部搜索引擎查询公开信息",
    ),
}
