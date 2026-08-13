"""R2 跨动作风险状态管理.

SafeAgent 等方案通过 STM/LTM 维护 session 级风险状态，
使风险判断不是单点、孤立的。Loop Controller 在 R2 内预留 RiskStateManager，
MVP 阶段可返回空风险画像，未来用于权限组合分析、动态风险评分、异常模式检测。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from loop_controller.action_proposal import ActionProposal
from loop_controller.decision import Decision


@dataclass(frozen=True)
class RiskProfile:
    """Session 级风险画像.

    Attributes:
        session_id: 所属会话 ID。
        cumulative_risk_score: 累积风险分数，0.0-1.0。
        recent_tags: 最近命中的风险标签。
        denied_count: 被拒绝次数。
        approval_count: 被审批次数。
    """

    session_id: str
    cumulative_risk_score: float = 0.0
    recent_tags: list[str] = field(default_factory=list)
    denied_count: int = 0
    approval_count: int = 0


@runtime_checkable
class RiskStateManager(Protocol):
    """R2 跨动作风险状态管理器接口."""

    def get_session_risk(self, session_id: str) -> RiskProfile:
        """获取当前会话风险画像."""
        ...

    def update_after_decision(
        self,
        session_id: str,
        proposal: ActionProposal,
        decision: Decision,
    ) -> None:
        """每次决策后更新会话风险状态."""
        ...


class InMemoryRiskStateManager:
    """MVP 内存版风险状态管理器.

    只记录 denied/approval 次数，不累积复杂风险分数。
    """

    def __init__(self) -> None:
        self._profiles: dict[str, RiskProfile] = {}

    def get_session_risk(self, session_id: str) -> RiskProfile:
        """获取会话风险画像，不存在则返回空画像."""
        return self._profiles.get(session_id, RiskProfile(session_id=session_id))

    def update_after_decision(
        self,
        session_id: str,
        proposal: ActionProposal,
        decision: Decision,
    ) -> None:
        """根据决策结果更新计数."""
        profile = self._profiles.get(session_id, RiskProfile(session_id=session_id))
        denied = profile.denied_count
        approved = profile.approval_count

        if decision.verdict == "deny":
            denied += 1
        elif decision.verdict == "require_approval":
            approved += 1

        self._profiles[session_id] = RiskProfile(
            session_id=session_id,
            cumulative_risk_score=profile.cumulative_risk_score,
            recent_tags=profile.recent_tags,
            denied_count=denied,
            approval_count=approved,
        )
