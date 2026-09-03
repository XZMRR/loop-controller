"""Interaction Governance Engine 核心实现（v0.38.0）."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from loop_controller.go_kernel_bridge import (
    CURRENT_PROTOCOL_VERSION,
    AgentCard,
    check_protocol_version,
)
from loop_controller.interaction.models import (
    AgentTrust,
    DelegationCapability,
    InteractionDecision,
    InteractionProfile,
    InteractionProposal,
    InteractionVerdict,
)
from loop_controller.interaction.policy_engine import (
    INTERACTION_PACKAGE,
    InteractionPolicyEngine,
)
from loop_controller.models import AuditAction, AuditDecision, AuditEvent

if TYPE_CHECKING:
    from loop_controller.controller import LoopController

logger = logging.getLogger(__name__)

DEFAULT_INTERACTION_DECISION_TTL_MINUTES = 5


class InteractionGovernanceError(Exception):
    """交互治理引擎内部错误。"""


class InteractionGovernanceEngine:
    """Agent 交互治理引擎。

    负责委托授权、Agent 信任校验、交互审计。
    不处理本地工具执行，也不走 R2 工具治理平面。
    """

    def __init__(
        self,
        controller: LoopController,
        policy_engine: InteractionPolicyEngine | None = None,
    ) -> None:
        """初始化。

        Args:
            controller: 已启动的 LoopController，用于查询 Agent/Runtime 依赖。
            policy_engine: 可选自定义策略引擎；未提供时使用默认 OPA 客户端。
        """
        self._controller = controller
        self._policy_engine = policy_engine or InteractionPolicyEngine()
        self._runtime = controller._runtime

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    async def evaluate(self, proposal: InteractionProposal) -> InteractionDecision:
        """评估交互/委托提案，返回权威判定。

        Returns:
            InteractionDecision，verdict 为 allow/modify/deny/require_approval。
        """
        now = datetime.now(UTC)

        # 1. source agent 存在性
        config = self._runtime.config
        if config is None:
            return self._deny(proposal, "interaction configuration unavailable", now)
        source_agent = config.agents.get(proposal.source_agent_id)
        if source_agent is None:
            return self._deny(proposal, f"unknown source agent: {proposal.source_agent_id}", now)

        # 2. 目标 Agent Card（通过 Go Kernel bridge）
        target_card = await self._target_agent_card(proposal.target_agent_id)
        if target_card is None:
            return self._deny(
                proposal,
                f"target agent not registered: {proposal.target_agent_id}",
                now,
            )

        if "delegate_execution" not in target_card.capabilities:
            return self._deny(
                proposal,
                f"target agent {proposal.target_agent_id} 缺少 delegate_execution capability",
                now,
            )
        if target_card.entrypoint.type not in ("http", "https") or not target_card.entrypoint.url:
            return self._deny(
                proposal,
                f"target agent {proposal.target_agent_id} has invalid entrypoint",
                now,
            )

        # 3. source interaction profile
        source_profile = self._profile_for_agent(source_agent.agent_id)
        if source_profile is None:
            return self._deny(
                proposal,
                f"source agent {proposal.source_agent_id} 未绑定 interaction profile",
                now,
            )

        # 4. source profile 是否允许发起该能力委托
        cap = source_profile.capabilities.get(proposal.tool_name)
        if cap is None or not cap.allowed:
            return self._deny(
                proposal,
                f"tool {proposal.tool_name} 不允许由 {proposal.source_agent_id} 委托",
                now,
            )

        # 5. 显式拒绝的目标 Agent
        if proposal.target_agent_id in cap.denied_target_agents:
            return self._deny(
                proposal,
                f"target agent {proposal.target_agent_id} 在能力拒绝列表中",
                now,
            )

        allowed_targets = cap.allowed_target_agents
        if allowed_targets and proposal.target_agent_id not in allowed_targets:
            return self._deny(
                proposal,
                f"target agent {proposal.target_agent_id} 不在能力允许列表中",
                now,
            )

        # 6. 委托深度
        if proposal.delegation_depth > source_profile.max_delegation_depth:
            return self._deny(
                proposal,
                f"delegation depth {proposal.delegation_depth} exceeds max {source_profile.max_delegation_depth}",
                now,
            )

        delegation_policy = config.interaction_config.policies.get(proposal.tool_name)
        if delegation_policy is None or not delegation_policy.allowed:
            return self._deny(proposal, f"delegation policy denies {proposal.tool_name}", now)

        target_profile = self._profile_for_agent(proposal.target_agent_id)
        target_profile_id = target_profile.profile_id if target_profile else None
        if target_profile_id in delegation_policy.denied_target_profiles:
            return self._deny(
                proposal,
                f"target profile {target_profile_id} is denied for {proposal.tool_name}",
                now,
            )
        if delegation_policy.allowed_target_profiles and (
            target_profile_id not in delegation_policy.allowed_target_profiles
        ):
            return self._deny(
                proposal,
                f"target profile {target_profile_id or '<external>'} is not allowed for {proposal.tool_name}",
                now,
            )

        is_external = target_profile is None
        if is_external and not source_profile.allow_external_delegation:
            return self._deny(proposal, "external delegation is not allowed", now)

        # 7. Agent 间信任
        trust = config.interaction_config.trust.get(
            f"{proposal.source_agent_id}:{proposal.target_agent_id}"
        )
        if trust is None:
            return self._deny(
                proposal,
                f"no trust relationship from {proposal.source_agent_id} to {proposal.target_agent_id}",
                now,
            )
        if trust.trust_level == "none" or trust.is_expired(now):
            return self._deny(
                proposal,
                f"trust from {proposal.source_agent_id} to {proposal.target_agent_id} is none or expired",
                now,
            )

        # 8. 构造 profile/trust 视图
        source_profile_view = self._build_profile_view(source_profile, cap)
        target_profile_view = self._build_target_profile_view(proposal.target_agent_id)
        trust_view = self._build_trust_view(trust)

        # 9. 调用 OPA 独立策略包
        policy_hits: list[str] = []
        rego_decision = await self._policy_engine.evaluate(
            proposal,
            source_agent,
            source_profile_view,
            target_profile_view,
            trust_view,
            target_card.capabilities,
            delegation_policy,
        )
        verdict = rego_decision.get("verdict")
        if verdict not in ("allow", "modify", "require_approval"):
            reason = rego_decision.get("reason", "denied by interaction policy")
            policy_hits = list(rego_decision.get("policy_hits") or [])
            return self._deny(proposal, reason, now, policy_hits=policy_hits)

        reason = rego_decision.get("reason", "interaction policy allowed")
        policy_hits = list(rego_decision.get("policy_hits") or [])

        # 10. modify 二次复核
        modified_args: dict[str, Any] | None = None
        if verdict == "modify":
            modified_args = rego_decision.get("modified_args")
            if modified_args is None:
                return self._deny(
                    proposal, "modify verdict 缺少 modified_args", now, policy_hits=policy_hits
                )
            recheck_proposal = proposal.model_copy(update={"arguments": modified_args})
            recheck = await self._policy_engine.evaluate(
                recheck_proposal,
                source_agent,
                source_profile_view,
                target_profile_view,
                trust_view,
                target_card.capabilities,
                delegation_policy,
            )
            if recheck.get("verdict") != "allow":
                return self._deny(
                    proposal,
                    "modify 后参数未通过 interaction 策略复核",
                    now,
                    policy_hits=policy_hits,
                )

        effective_args = modified_args if modified_args is not None else proposal.arguments

        # 11. require_approval 不走 token 签发
        if verdict == "require_approval":
            return InteractionDecision(
                decision_id=str(uuid4()),
                interaction_id=proposal.interaction_id,
                request_id=proposal.request_id,
                verdict="require_approval",
                reason=reason,
                policy_hits=policy_hits,
                policy_version=INTERACTION_PACKAGE,
                profile_version=source_profile.version,
                target_entrypoint={"type": target_card.entrypoint.type, "url": target_card.entrypoint.url},
                escalation_target=source_agent.owner_id,
                expires_at=now + timedelta(minutes=DEFAULT_INTERACTION_DECISION_TTL_MINUTES),
            )

        # 12. allow/modify：返回授权
        return InteractionDecision(
            decision_id=str(uuid4()),
            interaction_id=proposal.interaction_id,
            request_id=proposal.request_id,
            verdict=verdict,  # type: ignore[arg-type]
            reason=reason,
            policy_hits=policy_hits,
            policy_version=INTERACTION_PACKAGE,
            profile_version=source_profile.version,
            original_args=proposal.arguments,
            modified_args=modified_args,
            effective_args=effective_args,
            target_entrypoint={"type": target_card.entrypoint.type, "url": target_card.entrypoint.url},
            expires_at=now + timedelta(minutes=DEFAULT_INTERACTION_DECISION_TTL_MINUTES),
            max_uses=1,
        )

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _deny(
        self,
        proposal: InteractionProposal,
        reason: str,
        now: datetime,
        *,
        policy_hits: list[str] | None = None,
    ) -> InteractionDecision:
        return InteractionDecision(
            decision_id=str(uuid4()),
            interaction_id=proposal.interaction_id,
            request_id=proposal.request_id,
            verdict="deny",
            reason=reason,
            policy_hits=list(policy_hits or []),
            policy_version=INTERACTION_PACKAGE,
            profile_version="",
            expires_at=now,
            max_uses=0,
        )

    async def _target_agent_card(self, target_agent_id: str) -> AgentCard | None:
        """查询目标 Agent Card。"""
        bridge = getattr(self._runtime, "go_kernel_bridge", None)
        if bridge is None:
            return None
        card = await bridge.get_agent(target_agent_id)
        if card is None:
            return None
        if isinstance(card, dict):
            return AgentCard.from_dict(card)
        return cast(AgentCard, card)

    def _build_profile_view(
        self, profile: InteractionProfile, cap: DelegationCapability
    ) -> dict[str, Any]:
        return {
            "profile_id": profile.profile_id,
            "version": profile.version,
            "max_delegation_depth": profile.max_delegation_depth,
            "allow_external_delegation": profile.allow_external_delegation,
            "require_approval_for_external": profile.require_approval_for_external,
            "capabilities": {
                cap.tool_name: {
                    "allowed": cap.allowed,
                    "require_approval": cap.require_approval,
                    "allowed_args": cap.allowed_args,
                    "denied_args": cap.denied_args,
                    "allowed_target_agents": cap.allowed_target_agents,
                    "denied_target_agents": cap.denied_target_agents,
                }
            },
        }

    def _build_target_profile_view(self, target_agent_id: str) -> dict[str, Any] | None:
        """构造目标 profile 视图；target 未绑定 profile 时返回 None。"""
        profile = self._profile_for_agent(target_agent_id)
        if profile is None:
            return None
        return {
            "profile_id": profile.profile_id,
            "version": profile.version,
            "capabilities": {
                name: {
                    "allowed": cap.allowed,
                    "require_approval": cap.require_approval,
                }
                for name, cap in profile.capabilities.items()
            },
        }

    def _profile_for_agent(self, agent_id: str) -> InteractionProfile | None:
        config = self._runtime.config
        if config is None:
            return None
        return next(
            (p for p in config.interaction_config.profiles.values() if p.agent_id == agent_id),
            None,
        )

    def _build_trust_view(self, trust: AgentTrust) -> dict[str, Any]:
        return {
            "source_agent_id": trust.source_agent_id,
            "target_agent_id": trust.target_agent_id,
            "trust_level": trust.trust_level,
            "expires_at": trust.expires_at.isoformat() if trust.expires_at else None,
            "conditions": trust.conditions,
        }

    # ------------------------------------------------------------------
    # 审计（可选：由 Runtime 统一消费）
    # ------------------------------------------------------------------

    def build_audit_event(
        self,
        proposal: InteractionProposal,
        decision: InteractionDecision,
    ) -> AuditEvent:
        """根据判定结果构造交互审计事件。"""
        return AuditEvent(
            schema_version="1.0",
            event_id=str(uuid4()),
            trace_id=proposal.task_id or proposal.interaction_id,
            session_id=proposal.session_id or "",
            actor_type="agent",
            actor_id=proposal.source_agent_id,
            action=self._audit_action(decision.verdict),
            target=proposal.tool_name,
            decision=self._audit_decision(decision.verdict),
            reason=decision.reason,
            policy_version=decision.policy_version,
            profile_version=decision.profile_version,
            metadata={
                "interaction_id": proposal.interaction_id,
                "request_id": proposal.request_id,
                "source_agent_id": proposal.source_agent_id,
                "target_agent_id": proposal.target_agent_id,
                "verdict": decision.verdict,
                "delegation_depth": proposal.delegation_depth,
                "policy_hits": decision.policy_hits,
                "target_entrypoint": decision.target_entrypoint,
            },
        )

    def build_rejection_audit_event(
        self,
        payload: dict[str, Any],
        reason: str,
        rejection_type: str,
    ) -> AuditEvent:
        """为协议或请求边界拒绝构造交互审计事件。"""
        interaction_id = str(
            payload.get("interaction_id") or payload.get("request_id") or uuid4()
        )
        return AuditEvent(
            schema_version="1.0",
            event_id=str(uuid4()),
            trace_id=str(payload.get("task_id") or interaction_id),
            session_id=str(payload.get("session_id") or ""),
            actor_type="agent",
            actor_id=str(payload.get("source_agent_id") or "unknown"),
            action="deny",
            target=str(payload.get("tool_name") or "delegation"),
            decision="deny",
            reason=reason,
            metadata={
                "interaction_id": interaction_id,
                "request_id": payload.get("request_id"),
                "source_agent_id": payload.get("source_agent_id") or "unknown",
                "target_agent_id": payload.get("target_agent_id"),
                "verdict": "deny",
                "policy_hits": [],
                "target_entrypoint": None,
                "protocol_rejection": True,
                "rejection_type": rejection_type,
                "received_protocol_version": payload.get("protocol_version"),
            },
        )

    def _audit_action(self, verdict: InteractionVerdict) -> AuditAction:
        if verdict == "allow":
            return "execution_authorized"
        if verdict == "modify":
            return "execution_authorized"
        if verdict == "require_approval":
            return "evaluate"
        return "deny"

    def _audit_decision(self, verdict: InteractionVerdict) -> AuditDecision:
        if verdict == "allow":
            return "allow"
        if verdict == "modify":
            return "allow"
        if verdict == "require_approval":
            return "require_approval"
        return "deny"


class InteractionAuthorizeEndpoint:
    """``POST /interaction/v1/delegations/authorize`` HTTP 处理逻辑。"""

    def __init__(self, engine: InteractionGovernanceEngine) -> None:
        self._engine = engine
        self.last_proposal: InteractionProposal | None = None
        self.last_decision: InteractionDecision | None = None
        self.last_rejection_event: AuditEvent | None = None

    @staticmethod
    def _check_protocol_version(version: str) -> tuple[bool, str]:
        """校验协议版本；major/minor 不一致则 fail-closed。"""
        try:
            check_protocol_version(version)
        except ValueError as exc:
            return False, str(exc)
        return True, ""

    async def handle(self, payload: dict[str, Any]) -> dict[str, Any]:
        """处理授权请求并返回响应字典。"""
        protocol_version = payload.get("protocol_version", "")
        ok, reason = self._check_protocol_version(protocol_version)
        if not ok:
            self.last_rejection_event = self._engine.build_rejection_audit_event(
                payload,
                reason,
                "incompatible_protocol_version",
            )
            return {
                "allowed": False,
                "verdict": "deny",
                "reason": reason,
                "protocol_version": CURRENT_PROTOCOL_VERSION,
            }

        required = [
            "request_id",
            "source_agent_id",
            "target_agent_id",
            "tool_name",
        ]
        missing = [f for f in required if not payload.get(f)]
        if missing:
            missing_reason = f"missing required delegation fields: {', '.join(missing)}"
            self.last_rejection_event = self._engine.build_rejection_audit_event(
                payload,
                missing_reason,
                "missing_required_fields",
            )
            return {
                "allowed": False,
                "verdict": "deny",
                "reason": missing_reason,
                "protocol_version": CURRENT_PROTOCOL_VERSION,
            }

        proposal = InteractionProposal(
            interaction_id=payload.get("interaction_id") or payload["request_id"],
            request_id=payload["request_id"],
            session_id=payload.get("session_id", ""),
            task_id=payload.get("task_id", ""),
            source_agent_id=payload["source_agent_id"],
            target_agent_id=payload["target_agent_id"],
            tool_name=payload["tool_name"],
            arguments=payload.get("arguments") or {},
            risk_level=payload.get("risk_level", "low"),
            risk_tags=payload.get("risk_tags", []),
            delegation_depth=payload.get("delegation_depth", 0),
            interaction_context=payload.get("interaction_context", ""),
        )

        decision = await self._engine.evaluate(proposal)
        self.last_proposal = proposal
        self.last_decision = decision

        response: dict[str, Any] = {
            "allowed": decision.allowed,
            "verdict": decision.verdict,
            "decision_id": decision.decision_id,
            "reason": decision.reason,
            "protocol_version": CURRENT_PROTOCOL_VERSION,
        }
        if decision.target_entrypoint:
            response["target_entrypoint"] = decision.target_entrypoint
        if decision.escalation_target:
            response["escalation_target"] = decision.escalation_target
        if decision.modified_args is not None:
            response["modified_args"] = decision.modified_args
        if decision.original_args is not None:
            response["original_args"] = decision.original_args
        return response
