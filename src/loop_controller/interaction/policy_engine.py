"""IIGE 策略引擎（v0.38.0）.

复用 OPAPolicyEngine 的 HTTP 客户端行为，但使用独立的 package：
``loop_controller.interaction.delegation``。
"""

from __future__ import annotations

from typing import Any

from loop_controller.interaction.models import DelegationPolicy, InteractionProposal
from loop_controller.models import Agent
from loop_controller.policy_engine import OPAPolicyEngine

INTERACTION_PACKAGE = "loop_controller.interaction.delegation"

FAIL_CLOSED_DENY = {
    "verdict": "deny",
    "reason": "interaction policy engine unavailable",
    "policy_hits": ["fail_closed"],
}


class InteractionPolicyEngine:
    """交互治理策略引擎。"""

    def __init__(self, base_url: str = "http://127.0.0.1:8181", timeout: float = 2.0) -> None:
        """初始化。

        Args:
            base_url: OPA HTTP 服务地址。
            timeout: 请求超时（秒）。
        """
        self._engine = OPAPolicyEngine(base_url=base_url, timeout=timeout)

    async def evaluate(
        self,
        proposal: InteractionProposal,
        source_agent: Agent,
        source_profile: dict[str, Any],
        target_profile: dict[str, Any] | None,
        trust: dict[str, Any] | None,
        target_capabilities: list[str],
        delegation_policy: DelegationPolicy,
    ) -> dict[str, Any]:
        """调用 OPA 评估交互提案。

        任何异常路径均已由底层 OPAPolicyEngine fail-closed 为 deny。
        """
        input_doc = build_interaction_input(
            proposal,
            source_agent,
            source_profile,
            target_profile,
            trust,
            target_capabilities,
            delegation_policy,
        )
        return await self._engine.evaluate(INTERACTION_PACKAGE, input_doc)


def build_interaction_input(
    proposal: InteractionProposal,
    source_agent: Agent,
    source_profile: dict[str, Any],
    target_profile: dict[str, Any] | None,
    trust: dict[str, Any] | None,
    target_capabilities: list[str],
    delegation_policy: DelegationPolicy,
) -> dict[str, Any]:
    """构造 IIGE Rego input 文档（Python ↔ Rego 唯一契约点）."""
    doc: dict[str, Any] = {
        "action_kind": proposal.action_kind,
        "source_agent": {
            "agent_id": source_agent.agent_id,
            "owner_id": source_agent.owner_id,
        },
        "target_agent": {
            "agent_id": proposal.target_agent_id,
        },
        "tool_name": proposal.tool_name,
        "arguments": proposal.arguments,
        "risk_level": proposal.risk_level,
        "risk_tags": proposal.risk_tags,
        "delegation_depth": proposal.delegation_depth,
        "interaction_context": proposal.interaction_context,
        "source_profile": source_profile,
        "target_profile": target_profile,
        "trust": trust,
        "target_capabilities": target_capabilities,
        "delegation_policy": delegation_policy.model_dump(mode="json"),
    }
    if proposal.session_id:
        doc["session_id"] = proposal.session_id
    if proposal.task_id:
        doc["task_id"] = proposal.task_id
    return doc
