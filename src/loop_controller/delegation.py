"""旧委托授权 API 的 IIGE 兼容包装层。

v0.39.0 起，Agent 委托不得进入 R2 Tool Checkpoint。本模块仅保留旧类名和
``initiator_agent_id`` 请求字段兼容，所有授权判定均转发到独立 IIGE。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loop_controller.interaction.engine import (
    InteractionAuthorizeEndpoint,
    InteractionGovernanceEngine,
)
from loop_controller.interaction.models import InteractionProposal
from loop_controller.models import ActionProposal, GovernanceResult

if TYPE_CHECKING:
    from loop_controller.controller import LoopController
    from loop_controller.go_kernel_bridge import GoKernelBridge


class DelegationAuthorizer:
    """旧 Python API 的兼容包装器，内部只调用 IIGE。"""

    def __init__(
        self,
        controller: LoopController,
        bridge: GoKernelBridge | None = None,
        *,
        engine: InteractionGovernanceEngine | None = None,
    ) -> None:
        del bridge
        self._engine = engine or InteractionGovernanceEngine(controller)

    async def authorize(self, proposal: ActionProposal) -> GovernanceResult:
        """把旧 ``ActionProposal`` 转换为 IIGE 提案并返回兼容结果。"""
        if proposal.action_kind != "delegation":
            return self._blocked(proposal, "invalid action_kind", "invalid_action_kind")
        if not proposal.target_agent_id:
            return self._blocked(
                proposal,
                "delegation requires target_agent_id",
                "invalid_delegation",
            )

        decision = await self._engine.evaluate(
            InteractionProposal(
                interaction_id=proposal.call_id,
                request_id=proposal.call_id,
                task_id=proposal.task_id,
                source_agent_id=proposal.agent_id,
                target_agent_id=proposal.target_agent_id,
                tool_name=proposal.tool_name,
                arguments=proposal.arguments,
                risk_level=proposal.risk_level,
                risk_tags=proposal.risk_tags,
                delegation_depth=int(
                    (proposal.delegation_context or {}).get("delegation_depth", 0)
                ),
                interaction_context=proposal.task_context,
            )
        )
        if decision.verdict == "require_approval":
            return GovernanceResult(
                status="require_approval",
                call_id=proposal.call_id,
                tool_name=proposal.tool_name,
                arguments=proposal.arguments,
                request_id=decision.decision_id,
                reason=decision.reason,
            )
        if not decision.allowed:
            return self._blocked(proposal, decision.reason, "delegation_denied")

        effective_args = decision.effective_args or decision.modified_args or proposal.arguments
        return GovernanceResult(
            status="delegated",
            call_id=proposal.call_id,
            tool_name=proposal.tool_name,
            arguments=effective_args,
            content={
                "delegated": True,
                "authorized": True,
                "target_agent_id": proposal.target_agent_id,
                "target_entrypoint": decision.target_entrypoint,
                "decision_id": decision.decision_id,
            },
            reason=decision.reason,
        )

    @staticmethod
    def _blocked(
        proposal: ActionProposal,
        reason: str,
        error_code: str,
    ) -> GovernanceResult:
        return GovernanceResult(
            status="blocked",
            call_id=proposal.call_id,
            tool_name=proposal.tool_name,
            arguments=proposal.arguments,
            reason=reason,
            error_code=error_code,
        )


class DelegationAuthorizeEndpoint:
    """旧 ``/r2/v1/delegations/authorize`` 请求格式的 IIGE 兼容适配器。"""

    def __init__(self, authorizer: DelegationAuthorizer) -> None:
        self._endpoint = InteractionAuthorizeEndpoint(authorizer._engine)

    async def handle(self, payload: dict[str, Any]) -> dict[str, Any]:
        """将旧 source 字段映射后交给权威 Interaction endpoint。"""
        adapted = dict(payload)
        if "source_agent_id" not in adapted:
            adapted["source_agent_id"] = adapted.pop("initiator_agent_id", "")
        return await self._endpoint.handle(adapted)
