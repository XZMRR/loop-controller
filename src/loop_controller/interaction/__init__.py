"""Agent Interaction Governance Engine（IIGE）.

v0.38.0 将 Agent 交互治理（委托授权、Agent 信任、交互审计）从 Python R2
工具治理平面独立出来，形成与工具治理并列的独立平面。

本包负责：
- InteractionProposal / InteractionDecision 等核心模型
- InteractionGovernanceEngine 授权判定
- 独立 OPA 策略包 ``loop_controller.interaction.delegation``
- 交互事件审计写入
"""

from __future__ import annotations

from loop_controller.interaction.engine import InteractionGovernanceEngine
from loop_controller.interaction.models import (
    AgentTrust,
    DelegationPolicy,
    InteractionDecision,
    InteractionProfile,
    InteractionProposal,
    InteractionVerdict,
)

__all__ = [
    "AgentTrust",
    "DelegationPolicy",
    "InteractionDecision",
    "InteractionGovernanceEngine",
    "InteractionProfile",
    "InteractionProposal",
    "InteractionVerdict",
]
