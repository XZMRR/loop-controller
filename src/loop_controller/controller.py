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

from loop_controller.checkpoint import CheckpointError
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
        args_hash = hashlib.sha256(
            canonical_json(proposal.arguments).encode("utf-8")
        ).hexdigest()
        if masker is not None:
            args_mask = masker.mask(proposal.arguments, "audit_log")

    if actor_type is None:
        actor_type = "agent" if action == "propose" else "checkpoint"
    if actor_id is None:
        actor_id = task.agent_id

    verdict = decision_verdict if decision_verdict is not None else (decision.verdict if decision else None)
    reason_value = reason or (decision.reason if decision else None) or (result.content if result else None)

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
        self._runtime.audit_store.append(
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

        self._runtime.audit_store.append(
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

        eval_result = await self._evaluate_proposal(task, agent, proposal)

        if eval_result.status == "deny":
            return GovernanceResult(
                status="deny",
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
        store = self._runtime.approval_manager._store
        request = store.get_request_by_id(request_id)

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
        if record.verdict == "approve":
            approve_action: AuditAction = "approve"
            approve_verdict: Verdict = "allow"
        else:
            approve_action = "deny"
            approve_verdict = "deny"
        self._runtime.audit_store.append(
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
        self._runtime.audit_store.append(
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

    # -----------------------------------------------------------------------
    # 内部辅助
    # -----------------------------------------------------------------------

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
        """调用 Checkpoint.forward 执行 Decision，并写审计事件。"""
        session = self._runtime.session_manager.get_session(task.session_id)
        session_id = session.session_id if session is not None else task.session_id
        result = await self._runtime.checkpoint.forward(
            proposal,
            decision,
            session_id=session_id,
            user_id=task.user_id,
            tenant_id=task.tenant_id,
        )
        self._runtime.audit_store.append(
            _audit_event(
                task,
                action="execute",
                proposal=proposal,
                decision=decision,
                result=result,
                masker=self._runtime.masker,
            )
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
