"""A2A 委托授权器（v0.37.0）。

把跨 Agent 委托提升为与 ``allow/deny/modify/require_approval`` 同级的治理动作。
``DelegationAuthorizer`` 在 Python R2 内部对 ``action_kind == "delegation"`` 的提案
完成授权判定；最终委托 token 由 Go 内核在收到授权响应后自行签发。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast

from loop_controller.go_kernel_bridge import (
    CURRENT_PROTOCOL_VERSION,
    AgentCard,
    check_protocol_version,
)
from loop_controller.models import (
    ActionProposal,
    EvaluationResult,
    GovernanceResult,
)

if TYPE_CHECKING:
    from loop_controller.controller import LoopController
    from loop_controller.go_kernel_bridge import GoKernelBridge

logger = logging.getLogger(__name__)


def _deny(reason: str, error_code: str = "delegation_denied") -> GovernanceResult:
    """构造统一的委托拒绝响应。"""
    return GovernanceResult(
        status="blocked",
        call_id="",
        tool_name="",
        arguments={},
        reason=reason,
        error_code=error_code,
    )


class DelegationAuthorizer:
    """委托动作授权器。

    Args:
        controller: 已启动的 LoopController。
        bridge: Go 内核桥接器；用于查询 Agent Card，若未提供则无法校验目标能力。
    """

    def __init__(
        self,
        controller: LoopController,
        bridge: GoKernelBridge | None = None,
    ) -> None:
        self._controller = controller
        self._bridge = cast(
            "GoKernelBridge | None",
            bridge if bridge is not None else getattr(
                controller._runtime, "go_kernel_bridge", None
            ),
        )

    async def _target_agent_card(self, target_agent_id: str) -> AgentCard | None:
        """查询目标 Agent Card；无桥接器时返回 None。"""
        if self._bridge is None:
            return None
        card = await self._bridge.get_agent(target_agent_id)
        if card is None:
            return None
        if isinstance(card, dict):
            return AgentCard.from_dict(card)
        return card

    async def _validate_target(
        self, proposal: ActionProposal
    ) -> tuple[bool, AgentCard | None, str]:
        """校验委托目标是否存在且具备 delegate_execution 能力。"""
        target_agent_id = proposal.target_agent_id
        if not target_agent_id:
            return False, None, "delegation 缺少 target_agent_id"

        card = await self._target_agent_card(target_agent_id)
        if card is None:
            return False, None, f"target agent not registered: {target_agent_id}"

        if "delegate_execution" not in card.capabilities:
            return (
                False,
                None,
                f"target agent {target_agent_id} 缺少 delegate_execution capability",
            )
        return True, card, ""

    async def _evaluate_delegation(
        self,
        proposal: ActionProposal,
    ) -> EvaluationResult:
        """复用 controller.evaluate 对 delegation 提案做策略判定。"""
        return await self._controller.evaluate(
            agent_id=proposal.agent_id,
            user_id="",
            tool_name=proposal.tool_name,
            arguments=proposal.arguments,
            session_id="",
            task_id=proposal.task_id,
            task_context=proposal.task_context,
        )

    async def authorize(
        self,
        proposal: ActionProposal,
    ) -> GovernanceResult:
        """对委托提案完成 R2 授权判定。

        Returns:
            - ``delegated``: 允许委托（Go 内核应据此签发 token 并创建 Task）。
            - ``blocked``: 明确拒绝。
            - ``require_approval``: 需要审批，由调用方继续走审批流程。
        """
        if proposal.action_kind != "delegation":
            return _deny(
                "DelegationAuthorizer 只能处理 action_kind=delegation 的提案",
                error_code="invalid_action_kind",
            )

        ok, card, reason = await self._validate_target(proposal)
        if not ok or card is None:
            return _deny(reason)

        eval_result = await self._evaluate_delegation(proposal)
        decision = eval_result.decision
        if decision is None:
            return _deny(eval_result.reason or "R2 did not return a decision")

        if decision.verdict == "deny":
            return _deny(decision.reason)
        if decision.verdict == "require_approval":
            return GovernanceResult(
                status="require_approval",
                call_id=proposal.call_id,
                tool_name=proposal.tool_name,
                arguments=proposal.arguments,
                decision=decision,
                reason=decision.reason,
            )
        if decision.verdict not in ("allow", "modify"):
            return _deny(f"unexpected verdict: {decision.verdict}")

        # 二次复核：若策略返回 modify，用改写后参数重新评估，期望 allow
        updated_decision = decision
        if decision.verdict == "modify":
            if decision.policy_modified_args is None:
                return _deny("modify verdict 缺少 policy_modified_args")
            modified_proposal = proposal.model_copy(
                update={"arguments": decision.policy_modified_args}
            )
            recheck = await self._evaluate_delegation(modified_proposal)
            if recheck.decision is None or recheck.decision.verdict != "allow":
                return _deny("modify 后参数未通过策略复核")
            updated_decision = recheck.decision

        final_decision = updated_decision.model_copy(
            update={
                "action_kind": "delegation",
                "target_agent_id": proposal.target_agent_id,
            }
        )

        return GovernanceResult(
            status="delegated",
            call_id=proposal.call_id,
            tool_name=proposal.tool_name,
            arguments=proposal.arguments,
            decision=final_decision,
            content={
                "delegated": True,
                "authorized": True,
                "target_agent_id": proposal.target_agent_id,
                "target_entrypoint": {
                    "type": card.entrypoint.type,
                    "url": card.entrypoint.url,
                },
            },
            reason="R2 authorized delegation",
        )


class DelegationAuthorizeEndpoint:
    """``POST /r2/v1/delegations/authorize`` 的 HTTP 处理逻辑。

    该端点由 Python HTTP Server 挂载，供 Go Kernel 在发起委托前调用。
    """

    def __init__(self, authorizer: DelegationAuthorizer) -> None:
        self._authorizer = authorizer

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
            return {
                "allowed": False,
                "verdict": "deny",
                "reason": reason,
                "protocol_version": CURRENT_PROTOCOL_VERSION,
            }

        request_id = payload.get("request_id", "")
        initiator_agent_id = payload.get("initiator_agent_id", "")
        target_agent_id = payload.get("target_agent_id", "")
        tool_name = payload.get("tool_name", "")
        arguments = payload.get("arguments") or {}

        if not all([request_id, initiator_agent_id, target_agent_id, tool_name]):
            return {
                "allowed": False,
                "verdict": "deny",
                "reason": "missing required delegation fields",
                "protocol_version": CURRENT_PROTOCOL_VERSION,
            }

        proposal = ActionProposal(
            task_id=request_id,
            call_id=request_id,
            agent_id=initiator_agent_id,
            tool_name=tool_name,
            arguments=arguments,
            task_context="",
            action_kind="delegation",
            target_agent_id=target_agent_id,
        )

        result = await self._authorizer.authorize(proposal)

        decision = result.decision
        target_entrypoint = (
            decision.target_entrypoint
            if decision is not None and decision.target_entrypoint is not None
            else result.content.get("target_entrypoint") if isinstance(result.content, dict) else None
        )

        return {
            "allowed": result.status in ("delegated", "allow"),
            "verdict": "allow" if result.status == "delegated" else result.status,
            "decision_id": decision.decision_id if decision is not None else "",
            "target_entrypoint": target_entrypoint,
            "reason": result.reason,
            "protocol_version": CURRENT_PROTOCOL_VERSION,
        }
