"""LoopController：企业内部 Agent 工具调用治理入口（v0.13.0）。

``LoopController`` 是 Agent 驱动治理模式的核心类。企业内部 Agent 自己掌握主循环，
只在每次要调用工具时把请求提交给本类；本类负责 R1 风险评估、R2 策略判定、
R0 审批协调、工具执行和 R3 审计。

Agent 不需要关心 ``ActionProposal``、``Decision``、``Task`` 等内部模型，
只需要提供 ``agent_id``、``user_id``、``tool_name``、``arguments`` 和可选上下文。
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from loop_controller.checkpoint import CheckpointError, DecisionAlreadyConsumed
from loop_controller.go_kernel_bridge import A2AMessage, DelegationRequest
from loop_controller.identity import AgentIdentity
from loop_controller.infra.config_loader import AppConfig
from loop_controller.masker import Masker
from loop_controller.models import (
    ActionProposal,
    ActorType,
    Agent,
    AuditAction,
    AuditEvent,
    Decision,
    EvaluationResult,
    GovernanceResult,
    RiskSignal,
    Task,
    ToolResult,
    Verdict,
)
from loop_controller.runtime import Runtime, build_runtime
from loop_controller.utils.canonical import canonical_json


def _audit_event(
    task: Task,
    action: AuditAction,
    *,
    proposal: ActionProposal | None = None,
    decision: Decision | None = None,
    decision_verdict: Verdict | None = None,
    result: ToolResult | None = None,
    signal: RiskSignal | None = None,
    reason: str | None = None,
    masker: Masker | None = None,
    actor_type: ActorType | None = None,
    actor_id: str | None = None,
) -> AuditEvent:
    """构造审计事件；含 args_hash 与 args_mask。"""
    target = proposal.tool_name if proposal else None
    metadata: dict[str, Any] = {}
    if signal and signal.suggestion:
        metadata["classifier_suggestion"] = signal.suggestion

    args_hash: str | None = None
    args_mask: dict | None = None
    if proposal is not None:
        args_hash = hashlib.sha256(canonical_json(proposal.arguments).encode("utf-8")).hexdigest()
        if masker is not None:
            args_mask = masker.mask(proposal.arguments, "audit_log")

    if actor_type is None:
        actor_type = "agent" if action == "propose" else "checkpoint"
    if actor_id is None:
        actor_id = task.agent_id

    verdict = (
        decision_verdict
        if decision_verdict is not None
        else (decision.verdict if decision else None)
    )
    reason_value = (
        reason or (decision.reason if decision else None) or (result.content if result else None)
    )

    return AuditEvent(
        event_id=uuid.uuid4().hex,
        trace_id=task.task_id,
        session_id=task.session_id,
        call_id=proposal.call_id if proposal else None,
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        target=target,
        decision=verdict,
        reason=reason_value,
        args_hash=args_hash,
        args_mask=args_mask,
        policy_version=decision.policy_version if decision else None,
        profile_version=decision.profile_version if decision else None,
        metadata=metadata,
    )


class LoopController:
    """企业内部 Agent 工具调用治理入口。

    Args:
        runtime: 已组装并启动的 Runtime（含 Checkpoint、Classifier、ApprovalManager 等）。
    """

    def __init__(self, runtime: Runtime) -> None:
        self._runtime = runtime

    @staticmethod
    def _bump_risk_signal(signal: RiskSignal) -> RiskSignal:
        """v0.21.0：HTTP 工具风险等级提升一级。"""
        order: list[str] = ["low", "medium", "high", "critical"]
        current = signal.risk_level
        if current in order and current != "critical":
            idx = order.index(current)
            return signal.model_copy(update={"risk_level": order[idx + 1]})
        return signal

    async def start(self) -> None:
        """拉起 MCP gateway 等异步初始化。"""
        await self._runtime.start()

    async def aclose(self) -> None:
        """关闭 MCP gateway 等异步资源。"""
        await self._runtime.aclose()

    async def _try_delegate_to_agent(
        self,
        task: Task,
        proposal: ActionProposal,
        decision: Decision,
    ) -> GovernanceResult | None:
        """v0.36.0：若参数包含 __target_agent_id，向 Go 内核请求跨 Agent 委托。

        返回 None 表示不委托，继续本地执行；返回 GovernanceResult 表示已委托或被拒绝。
        """
        bridge = self._runtime.go_kernel_bridge
        if bridge is None:
            return None
        target_agent_id = proposal.arguments.get("__target_agent_id")
        if not isinstance(target_agent_id, str):
            return None

        resp = await bridge.request_delegation(
            DelegationRequest(
                request_id=proposal.call_id,
                initiator_agent_id=proposal.agent_id,
                target_agent_id=target_agent_id,
                tool_name=proposal.tool_name,
                arguments={k: v for k, v in proposal.arguments.items() if k != "__target_agent_id"},
                session_id=task.session_id,
                task_id="",
                risk_level=proposal.risk_level,
            )
        )
        if not resp.allowed:
            return GovernanceResult(
                status="blocked",
                call_id=proposal.call_id,
                tool_name=proposal.tool_name,
                arguments=proposal.arguments,
                reason=f"delegation rejected: {resp.reason}",
                error_code="delegation_denied",
            )

        # v0.36.1：委托响应必须携带 task_id，否则无法确认 Task 已创建。
        if not resp.task_id:
            return GovernanceResult(
                status="blocked",
                call_id=proposal.call_id,
                tool_name=proposal.tool_name,
                arguments=proposal.arguments,
                reason="delegation response missing task_id",
                error_code="delegation_failed",
            )

        message_recorded = await bridge.route_message(
            A2AMessage(
                message_id=proposal.call_id,
                task_id=resp.task_id or task.task_id,
                from_agent_id=proposal.agent_id,
                to_agent_id=target_agent_id,
                parts=[{"type": "text", "text": proposal.tool_name}]
                + [{"type": "data", "data": {k: v for k, v in proposal.arguments.items() if k != "__target_agent_id"}}],
            )
        )
        if not message_recorded:
            return GovernanceResult(
                status="blocked",
                call_id=proposal.call_id,
                tool_name=proposal.tool_name,
                arguments=proposal.arguments,
                reason="delegation message routing failed",
                error_code="delegation_route_failed",
            )

        return GovernanceResult(
            status="allow",
            call_id=proposal.call_id,
            tool_name=proposal.tool_name,
            arguments=proposal.arguments,
            decision=decision,
            content={
                "delegated": True,
                "authorized": True,
                "task_created": True,
                "message_recorded": True,
                "target_agent_id": target_agent_id,
                "target_entrypoint": (
                    resp.target_entrypoint.url if resp.target_entrypoint else None
                ),
                "delegation_token": resp.delegation_token,
                "task_id": resp.task_id,
            },
        )

    async def evaluate(
        self,
        *,
        agent_id: str,
        user_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        session_id: str | None = None,
        task_id: str | None = None,
        task_context: str = "",
    ) -> EvaluationResult:
        """对单次工具调用请求做 R1 + R2 治理判定，不执行。

        返回 ``allow`` / ``deny`` / ``require_approval`` / ``blocked``。
        当返回 ``require_approval`` 时，审批请求已自动提交到 ApprovalManager。
        """
        task, agent, proposal = self._prepare(
            agent_id=agent_id,
            user_id=user_id,
            tool_name=tool_name,
            arguments=arguments,
            session_id=session_id,
            task_id=task_id,
            task_context=task_context,
        )
        blocked = await self._handle_revocation(task, agent, proposal, "initial")
        if blocked is not None:
            return EvaluationResult(status="blocked", reason=blocked.content)
        return await self._evaluate_proposal(task, agent, proposal)

    async def _evaluate_proposal(
        self,
        task: Task,
        agent: Agent,
        proposal: ActionProposal,
    ) -> EvaluationResult:
        """对已有的 ActionProposal 做 R1 + R2 治理判定，不执行。"""
        profile = self._runtime.profiles[agent.profile_id]
        conversation_context = self._runtime.get_conversation_context(task.session_id)

        # R1 轻量分类
        signal = self._runtime.classifier.classify(task, agent, proposal, profile)
        # v0.21.0：HTTP 工具默认风险提升一级
        if proposal.tool_name in self._runtime.http_tool_names:
            signal = self._bump_risk_signal(signal)
        proposal = proposal.model_copy(
            update={"risk_level": signal.risk_level, "risk_tags": signal.tags}
        )
        await self._runtime.audit_store.append_async(
            _audit_event(
                task,
                action="propose",
                proposal=proposal,
                signal=signal,
                reason=signal.reason,
                masker=self._runtime.masker,
            )
        )

        # R2 Checkpoint 判定
        try:
            decision = await self._runtime.checkpoint.evaluate(
                task, agent, proposal, conversation_context=conversation_context
            )
        except CheckpointError as exc:
            return EvaluationResult(status="deny", reason=str(exc))

        await self._runtime.audit_store.append_async(
            _audit_event(
                task,
                action="evaluate",
                proposal=proposal,
                decision=decision,
                masker=self._runtime.masker,
            )
        )

        if decision.verdict == "require_approval":
            request = self._runtime.checkpoint.build_approval_request(decision, proposal, task)
            await self._runtime.approval_manager.submit(request)
            return EvaluationResult(
                status="require_approval",
                decision=decision,
                request_id=request.request_id,
                risk_signal=signal,
            )

        if decision.verdict in ("allow", "modify"):
            return EvaluationResult(
                status="allow",
                decision=decision,
                risk_signal=signal,
            )

        return EvaluationResult(
            status="deny",
            reason=decision.reason,
            decision=decision,
            risk_signal=signal,
        )

    async def execute(
        self,
        *,
        agent_id: str,
        decision: Decision,
    ) -> ToolResult:
        """执行一个已经通过 ``evaluate`` 或审批 finalized 的 Decision。

        由于 ``Decision`` 不保存原始 ``arguments``，单独传 ``decision`` 无法执行。
        实际调用请使用 ``execute_with_proposal``，或改用 ``evaluate_and_execute``。
        """
        raise NotImplementedError(
            "execute(decision) is not supported because Decision does not store arguments; "
            "use execute_with_proposal(agent_id=..., decision=..., proposal=...) or evaluate_and_execute()"
        )

    async def execute_with_proposal(
        self,
        *,
        agent_id: str,
        decision: Decision,
        proposal: ActionProposal,
    ) -> ToolResult:
        """执行一个已经通过 ``evaluate`` 或审批 finalized 的 Decision。

        调用方必须保证：
        - ``decision.call_id == proposal.call_id``
        - ``decision.task_id == proposal.task_id``
        - Decision 未过期且仍在有效期内
        """
        agent = self._runtime.checkpoint._identity.get_agent(agent_id)
        if agent is None:
            return ToolResult(
                call_id=decision.call_id,
                task_id=decision.task_id,
                tool_name=proposal.tool_name,
                status="error",
                content=f"unknown agent_id: {agent_id}",
                error_code="unknown_agent",
            )

        if agent.agent_id != proposal.agent_id:
            return ToolResult(
                call_id=decision.call_id,
                task_id=decision.task_id,
                tool_name=proposal.tool_name,
                status="error",
                content="agent_id mismatch between caller and proposal",
                error_code="agent_id_mismatch",
            )

        if decision.call_id != proposal.call_id:
            return ToolResult(
                call_id=decision.call_id,
                task_id=decision.task_id,
                tool_name=proposal.tool_name,
                status="error",
                content="call_id mismatch between decision and proposal",
                error_code="call_id_mismatch",
            )

        if decision.task_id != proposal.task_id:
            return ToolResult(
                call_id=decision.call_id,
                task_id=decision.task_id,
                tool_name=proposal.tool_name,
                status="error",
                content="task_id mismatch between decision and proposal",
                error_code="task_id_mismatch",
            )

        task = self._runtime.get_task(decision.task_id)
        if task is None:
            return ToolResult(
                call_id=decision.call_id,
                task_id=decision.task_id,
                tool_name=proposal.tool_name,
                status="error",
                content=f"task not found: {decision.task_id}",
                error_code="task_not_found",
            )

        try:
            return await self._execute_proposal(task, proposal, decision)
        except CheckpointError as exc:
            return ToolResult(
                call_id=decision.call_id,
                task_id=decision.task_id,
                tool_name=proposal.tool_name,
                status="blocked",
                content=str(exc),
                error_code="execution_failed",
            )

    async def evaluate_and_execute(
        self,
        *,
        agent_id: str,
        user_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        session_id: str | None = None,
        task_id: str | None = None,
        task_context: str = "",
    ) -> GovernanceResult:
        """便捷方法：evaluate + execute 一键完成。

        - ``allow``：立即执行并返回结果；
        - ``require_approval``：提交审批请求，返回 require_approval 响应；
        - ``deny``：返回 deny 响应，不执行。
        """
        task, agent, proposal = self._prepare(
            agent_id=agent_id,
            user_id=user_id,
            tool_name=tool_name,
            arguments=arguments,
            session_id=session_id,
            task_id=task_id,
            task_context=task_context,
        )

        blocked = await self._handle_revocation(task, agent, proposal, "initial")
        if blocked is not None:
            return GovernanceResult(
                status="blocked",
                call_id=proposal.call_id,
                tool_name=tool_name,
                arguments=arguments,
                reason=blocked.content,
                content=blocked.content,
                error_code="revoked",
            )

        eval_result = await self._evaluate_proposal(task, agent, proposal)

        if eval_result.status in ("deny", "blocked"):
            return GovernanceResult(
                status=eval_result.status,
                call_id=proposal.call_id,
                tool_name=tool_name,
                arguments=arguments,
                reason=eval_result.reason,
            )

        if eval_result.status == "require_approval":
            return GovernanceResult(
                status="require_approval",
                call_id=proposal.call_id,
                tool_name=tool_name,
                arguments=arguments,
                decision=eval_result.decision,
                request_id=eval_result.request_id,
                reason=eval_result.decision.reason if eval_result.decision else "requires approval",
            )

        # allow / modify
        decision = eval_result.decision
        assert decision is not None

        # v0.36.0：可选的跨 Agent 委托门控
        delegated = await self._try_delegate_to_agent(task, proposal, decision)
        if delegated is not None:
            return delegated

        try:
            result = await self._execute_proposal(task, proposal, decision)
        except CheckpointError as exc:
            return GovernanceResult(
                status="blocked",
                call_id=proposal.call_id,
                tool_name=tool_name,
                arguments=arguments,
                reason=str(exc),
                error_code="execution_failed",
            )

        return GovernanceResult(
            status="allow" if result.status == "success" else result.status,
            call_id=proposal.call_id,
            tool_name=tool_name,
            arguments=arguments,
            decision=decision,
            content=result.content,
            error_code=result.error_code,
        )

    async def resume_after_approval(
        self,
        request_id: str,
    ) -> GovernanceResult:
        """CLI/管理员 approve 审批后，Agent 调用此方法恢复执行。

        通过 ``request_id`` 从 ApprovalManager 恢复原始 Decision 与 ApprovalRequest，
        完成 ``finalize_after_approval`` 后执行。
        """
        request = self._runtime.approval_manager.get_request_by_id(request_id)

        if request is None:
            return GovernanceResult(
                status="error",
                call_id="",
                tool_name="",
                arguments={},
                reason=f"approval request not found: {request_id}",
                error_code="approval_request_not_found",
            )

        task = self._runtime.get_task(request.task_id)
        if task is None:
            return GovernanceResult(
                status="error",
                call_id=request.call_id,
                tool_name=request.tool_name,
                arguments=request.tool_arguments,
                reason=f"task not found: {request.task_id}",
                error_code="task_not_found",
            )

        decision_id = request.decision_id
        record = self._runtime.approval_manager.check(decision_id)
        if record is None:
            return GovernanceResult(
                status="require_approval",
                call_id=request.call_id,
                tool_name=request.tool_name,
                arguments=request.tool_arguments,
                request_id=request_id,
                reason="approval not yet decided",
            )

        # 记录审批人动作
        approve_proposal = ActionProposal(
            task_id=request.task_id,
            call_id=request.call_id,
            agent_id=request.agent_id,
            tool_name=request.tool_name,
            arguments=request.tool_arguments,
            task_context="",
        )
        agent = self._runtime.checkpoint._identity.get_agent(request.agent_id)
        if agent is None:
            return GovernanceResult(
                status="error",
                call_id=request.call_id,
                tool_name=request.tool_name,
                arguments=request.tool_arguments,
                reason=f"unknown agent_id: {request.agent_id}",
                error_code="unknown_agent",
            )
        blocked = await self._handle_revocation(task, agent, approve_proposal, "approval_resume")
        if blocked is not None:
            return self._revoked_governance_result(approve_proposal, blocked.content)

        if record.verdict == "approve":
            approve_action: AuditAction = "approve"
            approve_verdict: Verdict = "allow"
        elif record.verdict == "deny":
            approve_action = "deny"
            approve_verdict = "deny"
        else:
            return GovernanceResult(
                status="error",
                call_id=request.call_id,
                tool_name=request.tool_name,
                arguments=request.tool_arguments,
                reason=f"unknown approval verdict: {record.verdict!r}",
                error_code="invalid_verdict",
            )
        await self._runtime.audit_store.append_async(
            _audit_event(
                task,
                action=approve_action,
                proposal=approve_proposal,
                decision_verdict=approve_verdict,
                reason=record.comment or record.verdict,
                masker=self._runtime.masker,
                actor_type="r0_delegate",
                actor_id=record.approver_id,
            )
        )

        original_decision = request.original_decision
        if original_decision is None:
            return GovernanceResult(
                status="error",
                call_id=request.call_id,
                tool_name=request.tool_name,
                arguments=request.tool_arguments,
                reason="original decision missing in approval request",
                error_code="approval_request_corrupted",
            )

        try:
            finalized = self._runtime.checkpoint.finalize_after_approval(
                original_decision, record, request
            )
        except DecisionAlreadyConsumed as exc:
            return GovernanceResult(
                status="error",
                call_id=request.call_id,
                tool_name=request.tool_name,
                arguments=request.tool_arguments,
                reason=str(exc),
                error_code="decision_already_consumed",
            )
        except CheckpointError as exc:
            return GovernanceResult(
                status="deny",
                call_id=request.call_id,
                tool_name=request.tool_name,
                arguments=request.tool_arguments,
                reason=str(exc),
            )

        if finalized.verdict != "allow":
            return GovernanceResult(
                status="deny",
                call_id=request.call_id,
                tool_name=request.tool_name,
                arguments=request.tool_arguments,
                reason=finalized.reason,
            )

        # 记录审批通过 Decision 已被消费
        await self._runtime.audit_store.append_async(
            _audit_event(
                task,
                action="approval_consumed",
                proposal=approve_proposal,
                decision=finalized,
                reason=finalized.reason,
                masker=self._runtime.masker,
            )
        )

        try:
            result = await self._execute_proposal(task, approve_proposal, finalized)
        except CheckpointError as exc:
            return GovernanceResult(
                status="blocked",
                call_id=request.call_id,
                tool_name=request.tool_name,
                arguments=request.tool_arguments,
                reason=str(exc),
                error_code="execution_failed",
            )

        return GovernanceResult(
            status="allow" if result.status == "success" else result.status,
            call_id=approve_proposal.call_id,
            tool_name=request.tool_name,
            arguments=request.tool_arguments,
            decision=finalized,
            content=result.content,
            error_code=result.error_code,
        )

    async def cancel_approval(self, request_id: str) -> None:
        """取消指定审批请求；主要用于 wait_for_approval 超时清理。"""
        await self._runtime.approval_manager.cancel_request(request_id)

    # -----------------------------------------------------------------------
    # 内部辅助
    # -----------------------------------------------------------------------

    @staticmethod
    def _secret_refs(arguments: dict[str, Any]) -> list[str]:
        refs: list[str] = []

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                for key, nested in value.items():
                    if key == "secret_ref":
                        if isinstance(nested, str):
                            refs.append(nested)
                        elif isinstance(nested, dict) and isinstance(nested.get("name"), str):
                            refs.append(nested["name"])
                    visit(nested)
            elif isinstance(value, list):
                for nested in value:
                    visit(nested)

        visit(arguments)
        return refs

    @staticmethod
    def _agent_identity(agent: Agent, user_id: str) -> AgentIdentity:
        return AgentIdentity(
            agent_id=agent.agent_id,
            user_id=user_id,
            harness_id=(agent.identity or {}).get("harness_id"),
            profile_id=agent.profile_id,
            tenant_id=agent.tenant_id,
        )

    def _check_revocation(
        self, agent: Agent, user_id: str, proposal: ActionProposal
    ) -> tuple[bool, str | None]:
        match = self._runtime.checkpoint.check_revocation(
            self._agent_identity(agent, user_id), proposal.tool_name, proposal.arguments
        )
        return match.revoked, match.reason

    async def _handle_revocation(
        self,
        task: Task,
        agent: Agent,
        proposal: ActionProposal,
        stage: str,
    ) -> ToolResult | None:
        identity = self._agent_identity(agent, task.user_id)
        match = self._runtime.checkpoint.check_revocation(
            identity, proposal.tool_name, proposal.arguments
        )
        if not match.revoked:
            return None
        return await self._runtime.checkpoint.handle_revocation_block(
            identity=identity,
            proposal=proposal,
            task=task,
            match=match,
            stage=stage,
        )

    @staticmethod
    def _revoked_governance_result(
        proposal: ActionProposal, reason: str | None
    ) -> GovernanceResult:
        message = reason or "revoked"
        return GovernanceResult(
            status="blocked",
            call_id=proposal.call_id,
            tool_name=proposal.tool_name,
            arguments=proposal.arguments,
            reason=message,
            content=message,
            error_code="revoked",
        )

    def _prepare(
        self,
        *,
        agent_id: str,
        user_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        session_id: str | None,
        task_id: str | None,
        task_context: str,
    ) -> tuple[Task, Agent, ActionProposal]:
        """校验身份、创建/复用 Task 和 Session，构造 ActionProposal。"""
        agent = self._runtime.checkpoint._identity.get_agent(agent_id)
        if agent is None:
            raise ValueError(f"unknown agent_id: {agent_id}")

        if task_id is not None:
            task = self._runtime.get_task(task_id)
            if task is None:
                raise ValueError(f"task not found: {task_id}")
        else:
            task, _session = self._runtime.create_task(
                user_id=user_id,
                agent_id=agent_id,
                description=task_context,
                session_id=session_id,
            )

        proposal = ActionProposal(
            task_id=task.task_id,
            call_id=uuid.uuid4().hex,
            agent_id=agent_id,
            tool_name=tool_name,
            arguments=dict(arguments),
            task_context=task_context,
        )
        return task, agent, proposal

    async def _execute_proposal(
        self,
        task: Task,
        proposal: ActionProposal,
        decision: Decision,
    ) -> ToolResult:
        """调用 Checkpoint.forward 执行 Decision；执行阶段审计由 Checkpoint 统一写入。"""
        try:
            self._runtime.require_execution_ready()
        except RuntimeError as exc:
            raise CheckpointError(str(exc)) from exc
        agent = self._runtime.checkpoint._identity.get_agent(proposal.agent_id)
        if agent is None:
            raise CheckpointError(f"unknown agent_id: {proposal.agent_id}")
        blocked = await self._handle_revocation(task, agent, proposal, "pre_execute")
        if blocked is not None:
            # v0.36.1：在 Controller 侧被吊销阻断的执行请求显式记录。
            await self._runtime.audit_store.append_async(
                _audit_event(
                    task,
                    action="execution_blocked",
                    proposal=proposal,
                    decision=decision,
                    result=blocked,
                    reason="execution blocked by revocation pre-check",
                    masker=self._runtime.masker,
                )
            )
            return blocked
        session = self._runtime.session_manager.get_session(task.session_id)
        session_id = session.session_id if session is not None else task.session_id
        result = await self._runtime.checkpoint.forward(
            proposal,
            decision,
            session_id=session_id,
            user_id=task.user_id,
            tenant_id=task.tenant_id,
        )
        return result


async def build_controller(
    config: AppConfig,
    *,
    opa_url: str = "http://127.0.0.1:8181",
    env_extra: dict[str, str] | None = None,
) -> LoopController:
    """从 ``AppConfig`` 构造治理控制器。

    等价于 ``build_runtime`` 后包一层 ``LoopController``。
    """
    runtime = build_runtime(config, opa_url=opa_url, env_extra=env_extra)
    return LoopController(runtime)
