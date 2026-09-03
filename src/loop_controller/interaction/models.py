"""Agent Interaction Governance 核心模型（v0.38.0）.

所有模型均为 Pydantic v2 不可变模型，对应 OpenAPI/JSON Schema 权威协议。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

InteractionVerdict = Literal["allow", "deny", "modify", "require_approval"]
TrustLevel = Literal["none", "limited", "full"]


class DelegationCapability(BaseModel):
    """单个可被委托的能力配置。"""

    model_config = ConfigDict(frozen=True)

    tool_name: str
    allowed: bool = False
    require_approval: bool = False
    allowed_args: dict[str, list[str]] = Field(default_factory=dict)
    denied_args: dict[str, list[str]] = Field(default_factory=dict)
    allowed_target_agents: list[str] = Field(default_factory=list)
    denied_target_agents: list[str] = Field(default_factory=list)


class InteractionProfile(BaseModel):
    """Agent 在交互治理平面上的岗位说明书。

    ``capabilities`` 中未声明的能力名，IIGE 直接 deny（默认拒绝）。
    """

    model_config = ConfigDict(frozen=True)

    profile_id: str
    agent_id: str
    version: str = ""
    description: str = ""
    capabilities: dict[str, DelegationCapability] = Field(default_factory=dict)
    max_delegation_depth: int = Field(default=2, ge=0)
    allow_external_delegation: bool = False
    require_approval_for_external: bool = True


class AgentTrust(BaseModel):
    """source Agent 对 target Agent 的信任关系。"""

    model_config = ConfigDict(frozen=True)

    source_agent_id: str
    target_agent_id: str
    trust_level: TrustLevel = "none"
    expires_at: datetime | None = None
    conditions: dict[str, Any] = Field(default_factory=dict)

    def is_expired(self, now: datetime | None = None) -> bool:
        """检查信任关系是否已过期。"""
        if self.expires_at is None:
            return False
        if now is None:
            now = datetime.now(UTC)
        return now >= self.expires_at


class DelegationPolicy(BaseModel):
    """全局委托策略：按能力维度补充的允许/拒绝规则。"""

    model_config = ConfigDict(frozen=True)

    tool_name: str
    allowed: bool = True
    allowed_target_profiles: list[str] = Field(default_factory=list)
    denied_target_profiles: list[str] = Field(default_factory=list)
    reason: str = ""


class InteractionProposal(BaseModel):
    """IIGE 的输入模型：一次交互/委托申报。"""

    model_config = ConfigDict(frozen=True)

    interaction_id: str
    request_id: str
    session_id: str = ""
    task_id: str = ""
    source_agent_id: str
    target_agent_id: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    action_kind: Literal["delegation"] = "delegation"
    risk_level: str = "low"
    risk_tags: list[str] = Field(default_factory=list)
    delegation_depth: int = 0
    interaction_context: str = ""


class InteractionDecision(BaseModel):
    """IIGE 对 InteractionProposal 的权威判定。"""

    model_config = ConfigDict(frozen=True)

    decision_id: str
    interaction_id: str
    request_id: str
    verdict: InteractionVerdict
    reason: str
    policy_hits: list[str] = Field(default_factory=list)
    policy_version: str = ""
    profile_version: str = ""
    modified_args: dict[str, Any] | None = None
    original_args: dict[str, Any] | None = None
    effective_args: dict[str, Any] | None = None
    escalation_target: str | None = None
    target_entrypoint: dict[str, Any] | None = None
    delegation_token: str | None = None
    expires_at: datetime | None = None
    max_uses: int = 1

    @property
    def allowed(self) -> bool:
        """是否允许执行/委托。"""
        return self.verdict in ("allow", "modify")

    @property
    def blocked(self) -> bool:
        """是否明确拒绝。"""
        return self.verdict == "deny"

    @property
    def require_approval(self) -> bool:
        """是否需要审批。"""
        return self.verdict == "require_approval"
