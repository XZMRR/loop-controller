"""R2 Checkpoint：策略执行入口.

Checkpoint 是 R2 的统一入口，负责：
1. evaluate：接收 R1 的 ActionProposal，返回 Decision；
2. forward：对 allow/modify 的 Decision，校验有效性并代理转发到 MCP Gateway。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from loop_controller.action_proposal import ActionProposal
from loop_controller.agent import Agent
from loop_controller.audit import AuditEvent, AuditLogger, hash_arguments, mask_arguments
from loop_controller.budget import BudgetCost, BudgetLedger, InMemoryBudgetLedger
from loop_controller.capability_profile import CapabilityProfile
from loop_controller.decision import Decision
from loop_controller.mcp_gateway import MCPGateway, MockMCPGateway
from loop_controller.permission_interaction import (
    PermissionInteractionAnalyzer,
    StaticPermissionInteractionAnalyzer,
)
from loop_controller.policy_engine import PolicyEngine
from loop_controller.r0_delegate import ApprovalRequest, ApprovalRecord, R0Delegate
from loop_controller.risk_state import RiskStateManager
from loop_controller.task import Task
from loop_controller.tool import ToolResult


@dataclass
class CheckpointConfig:
    """Checkpoint 配置.

    Attributes:
        policy_package: OPA/Rego 包名。
        decision_ttl_seconds: allow/modify Decision 有效期（秒）。
        approval_ttl_seconds: require_approval Decision 有效期（秒）。
    """

    policy_package: str = "loop_controller.tool_permission"
    decision_ttl_seconds: int = 300
    approval_ttl_seconds: int = 900


class Checkpoint:
    """R2 统一入口.

    MVP 内同时承担 PDP（策略决策点）和 PEP（策略执行点）角色。
    """

    def __init__(
        self,
        policy_engine: PolicyEngine,
        profile_store: dict[str, CapabilityProfile],
        permission_interaction: PermissionInteractionAnalyzer | None = None,
        budget_ledger: BudgetLedger | None = None,
        risk_state_manager: RiskStateManager | None = None,
        r0_delegate: R0Delegate | None = None,
        mcp_gateway: MCPGateway | None = None,
        audit_logger: AuditLogger | None = None,
        config: CheckpointConfig | None = None,
    ) -> None:
        """初始化 Checkpoint.

        Args:
            policy_engine: 策略引擎，如 OPAPolicyEngine 或 MockPolicyEngine。
            profile_store: CapabilityProfile 内存存储。
            permission_interaction: 权限组合分析器，默认静态规则。
            budget_ledger: 预算账本，默认内存版。
            risk_state_manager: 跨动作风险状态，默认 None（MVP 不打桩实例化）。
            r0_delegate: R0-delegate 审批接口，默认 None。
            mcp_gateway: MCP Client 代理，默认 Mock。
            audit_logger: 审计日志接口，默认 None。
            config: Checkpoint 配置。
        """
        self.policy_engine = policy_engine
        self.profile_store = profile_store
        self.permission_interaction = permission_interaction or StaticPermissionInteractionAnalyzer()
        self.budget_ledger = budget_ledger or InMemoryBudgetLedger()
        self.risk_state_manager = risk_state_manager
        self.r0_delegate = r0_delegate
        self.mcp_gateway = mcp_gateway or MockMCPGateway()
        self.audit_logger = audit_logger
        self.config = config or CheckpointConfig()
        self._used_decisions: set[str] = set()

    def evaluate(
        self,
        task: Task,
        agent: Agent,
        proposal: ActionProposal,
    ) -> Decision:
        """对 ActionProposal 做策略判定，返回 Decision.

        流程：
        1. 校验 Agent 的 CapabilityProfile；
        2. 预算检查并预留；
        3. 权限组合分析；
        4. 调用 PolicyEngine 做最终判定；
        5. 审计 evaluate 事件；
        6. 更新 RiskStateManager。
        """
        # 1. 校验 CapabilityProfile
        profile = self.profile_store.get(agent.profile_id)
        if profile is None:
            return self._deny(proposal, f"Agent profile {agent.profile_id} not found")

        # 2. 预算预留
        cost = self._estimate_cost(proposal)
        if not self.budget_ledger.check_and_reserve(proposal, cost):
            return self._deny(proposal, "Budget exceeded")

        # 3. 权限组合分析
        interaction_risk = self.permission_interaction.check(proposal, [])
        if interaction_risk and interaction_risk.risk_level in ("high", "critical"):
            decision = self._deny(proposal, interaction_risk.reason)
            self._log_event(task, agent, proposal, "evaluate", decision)
            self._update_risk_state(task.session_id, proposal, decision)
            return decision

        # 4. 调用 PolicyEngine
        input_doc = self._build_policy_input(proposal, profile, task)
        policy_result = self.policy_engine.evaluate(self.config.policy_package, input_doc)
        verdict = policy_result.get("verdict", "deny")
        reason = policy_result.get("reason", "Policy default deny")
        modified_args = policy_result.get("modified_args")

        decision: Decision
        if verdict == "require_approval":
            decision = Decision(
                decision_id=str(uuid4()),
                call_id=proposal.call_id,
                task_id=proposal.task_id,
                verdict="require_approval",
                reason=reason,
                expires_at=datetime.now(timezone.utc) + timedelta(seconds=self.config.approval_ttl_seconds),
                max_uses=1,
                escalation_target="r0_delegate_default",
            )
        elif verdict in ("allow", "modify"):
            decision = Decision(
                decision_id=str(uuid4()),
                call_id=proposal.call_id,
                task_id=proposal.task_id,
                verdict=verdict,
                modified_args=modified_args,
                reason=reason,
                expires_at=datetime.now(timezone.utc) + timedelta(seconds=self.config.decision_ttl_seconds),
                max_uses=1,
            )
        else:
            decision = self._deny(proposal, reason)

        # 5. 审计
        self._log_event(task, agent, proposal, "evaluate", decision)

        # 6. 更新风险状态
        self._update_risk_state(task.session_id, proposal, decision)

        return decision

    def forward(
        self,
        proposal: ActionProposal,
        decision: Decision,
    ) -> ToolResult:
        """对 allow/modify Decision，校验后通过 MCP Gateway 转发工具调用.

        Args:
            proposal: 原始动作申报。
            decision: R2 签发的 Decision。

        Returns:
            ToolResult。

        Raises:
            ValueError: Decision 无效或已过期。
        """
        # 1. 校验 Decision 有效性
        if decision.call_id != proposal.call_id:
            raise ValueError("Decision call_id mismatch")
        if datetime.now(timezone.utc) > decision.expires_at:
            raise ValueError("Decision expired")
        if decision.decision_id in self._used_decisions:
            raise ValueError("Decision already used")
        if decision.verdict not in ("allow", "modify"):
            raise ValueError(f"Cannot forward decision with verdict {decision.verdict}")

        self._used_decisions.add(decision.decision_id)

        # 2. 使用 modify 后的参数
        arguments = decision.modified_args if decision.verdict == "modify" else proposal.arguments

        # 3. 通过 MCP Gateway 转发（R2 是唯一授权出口）
        return self.mcp_gateway.call_tool(proposal.tool_name, arguments, proposal.call_id)

    def request_and_apply_approval(
        self,
        task: Task,
        agent: Agent,
        proposal: ActionProposal,
        decision: Decision,
    ) -> Decision:
        """对 require_approval 的 Decision，请求 R0-delegate 审批并返回最终 Decision.

        如果 R0-delegate 未配置，则直接拒绝。
        """
        if self.r0_delegate is None:
            return self._deny(proposal, "R0-delegate not configured")

        approval_request = ApprovalRequest(
            decision_id=decision.decision_id,
            call_id=proposal.call_id,
            task_id=proposal.task_id,
            agent_id=proposal.agent_id,
            tool_name=proposal.tool_name,
            arguments_summary=str(mask_arguments(proposal.arguments)),
            reason=decision.reason,
            requester_id=task.user_id,
            requested_at=datetime.now(timezone.utc),
        )
        record = self.r0_delegate.request_approval(approval_request)

        if record.approved:
            final_decision = Decision(
                decision_id=str(uuid4()),
                call_id=proposal.call_id,
                task_id=proposal.task_id,
                verdict="allow",
                reason=f"Approved by {record.approver_id}: {record.reason}",
                expires_at=datetime.now(timezone.utc) + timedelta(seconds=self.config.decision_ttl_seconds),
                max_uses=1,
            )
        else:
            final_decision = self._deny(proposal, f"Denied by {record.approver_id}: {record.reason}")

        self._log_event(task, agent, proposal, "approve" if record.approved else "deny", final_decision)
        self._update_risk_state(task.session_id, proposal, final_decision)
        return final_decision

    def _deny(self, proposal: ActionProposal, reason: str) -> Decision:
        """生成 deny Decision."""
        return Decision(
            decision_id=str(uuid4()),
            call_id=proposal.call_id,
            task_id=proposal.task_id,
            verdict="deny",
            reason=reason,
            expires_at=datetime.now(timezone.utc),
            max_uses=0,
        )

    @staticmethod
    def _build_policy_input(
        proposal: ActionProposal,
        profile: CapabilityProfile,
        task: Task,
    ) -> dict[str, Any]:
        """构造给 PolicyEngine 的输入文档."""
        return {
            "proposal": {
                "task_id": proposal.task_id,
                "call_id": proposal.call_id,
                "agent_id": proposal.agent_id,
                "tool_name": proposal.tool_name,
                "arguments": proposal.arguments,
                "task_context": proposal.task_context,
                "risk_level": proposal.risk_level,
            },
            "profile": {
                "profile_id": profile.profile_id,
                "allowed_tools": profile.allowed_tools,
                "denied_args": profile.denied_args,
                "tool_permissions": {
                    name: {
                        "allowed": perm.allowed,
                        "require_approval": perm.require_approval,
                    }
                    for name, perm in profile.tool_permissions.items()
                },
            },
            "task": {
                "task_id": task.task_id,
                "user_id": task.user_id,
                "session_id": task.session_id,
                "description": task.description,
            },
        }

    @staticmethod
    def _estimate_cost(proposal: ActionProposal) -> BudgetCost:
        """估算单次动作成本；MVP 简化实现."""
        return BudgetCost(token_count=1)

    def _log_event(
        self,
        task: Task,
        agent: Agent,
        proposal: ActionProposal,
        action: str,
        decision: Decision,
    ) -> None:
        """记录审计事件."""
        if self.audit_logger is None:
            return

        event = AuditEvent(
            event_id=str(uuid4()),
            trace_id=task.task_id,
            timestamp=datetime.now(timezone.utc),
            actor_type="agent" if action in ("propose", "classify") else "checkpoint",
            actor_id=agent.agent_id if action in ("propose", "classify") else "checkpoint",
            action=action,  # type: ignore[arg-type]
            target=proposal.tool_name,
            args_hash=hash_arguments(proposal.arguments),
            args_mask=mask_arguments(proposal.arguments),
            decision=decision.verdict,
            reason=decision.reason,
            session_id=task.session_id,
        )
        self.audit_logger.log(event)

    def _update_risk_state(
        self,
        session_id: str,
        proposal: ActionProposal,
        decision: Decision,
    ) -> None:
        """更新跨动作风险状态."""
        if self.risk_state_manager is None:
            return
        self.risk_state_manager.update_after_decision(session_id, proposal, decision)
