"""Runtime 组装 + R1 执行循环（§5.2）。

``Runtime`` 是运行时依赖容器；``run_task`` 是单任务执行循环。
迭代 2 已接通 DecisionStore 持久化、R0-delegate 审批打桩；
T3.1/T3.2 补全哈希链、args_hash/args_mask 与分级掩码。
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loop_controller.approval_manager import AsyncApprovalManager
from loop_controller.budget import JsonlBudgetLedger
from loop_controller.checkpoint import Checkpoint, CheckpointError
from loop_controller.classifier import LightweightClassifier, RuleBasedClassifier
from loop_controller.infra.approval_store import JsonlApprovalStore
from loop_controller.infra.audit_store import AuditStore, JsonlAuditStore
from loop_controller.infra.config_loader import AppConfig, ConfigLoader
from loop_controller.infra.conversation_store import JsonlConversationStore
from loop_controller.infra.decision_store import JsonlDecisionStore
from loop_controller.infra.identity import ConfigIdentityProvider
from loop_controller.infra.policy_store import FilePolicyStore
from loop_controller.infra.reservation_store import (
    InMemoryReservationStore,
    JsonlReservationStore,
    ReservationStore,
)
from loop_controller.infra.task_store import InMemoryTaskStore, JsonlTaskStore, TaskStore
from loop_controller.llm_planner import HttpxLLMClient, LLMPlanner
from loop_controller.masker import Masker
from loop_controller.mcp_gateway import MCPGateway
from loop_controller.models import (
    ActionProposal,
    Agent,
    ApprovalRecord,
    AuditAction,
    AuditEvent,
    BudgetCost,
    ConversationContext,
    ConversationMessage,
    Decision,
    RiskSignal,
    Task,
    TaskRunResult,
    ToolResult,
    UserQuestion,
)
from loop_controller.permission_interaction import (
    CapabilityBasedPermissionAnalyzer,
    CompositePermissionInteractionAnalyzer,
    ConfigPermissionInteractionAnalyzer,
)
from loop_controller.planner import Planner, ScriptedPlanner
from loop_controller.policy_engine import OPAPolicyEngine
from loop_controller.risk_state import JsonlRiskStateStore, RiskStateManager
from loop_controller.session import JsonlSessionBackend, Session, SessionManager
from loop_controller.utils.canonical import canonical_json

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Runtime：运行时依赖容器
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Runtime:
    """R1/R2/R3 运行时依赖容器（§5.2）。"""

    planner: Planner
    classifier: LightweightClassifier
    checkpoint: Checkpoint
    gateway: MCPGateway
    approval_manager: AsyncApprovalManager  # v0.3.0 异步审批管理器
    audit_store: AuditStore
    masker: Masker
    profiles: dict[str, Any]  # CapabilityProfile
    session_manager: SessionManager  # v1.2 会话分配与校验
    risk_manager: RiskStateManager  # v1.2 会话级风险状态
    conversation_store: JsonlConversationStore  # v0.3.0 会话上下文持久化
    task_store: TaskStore = field(default_factory=InMemoryTaskStore)  # v0.6.0 Task 持久化
    reservation_store: ReservationStore = field(default_factory=InMemoryReservationStore)  # v0.8.0 reservation 持久化

    def create_task(
        self,
        user_id: str,
        agent_id: str,
        description: str,
        session_id: str | None = None,
    ) -> tuple[Task, Session]:
        """通过 SessionManager 分配/复用 session，构造 Task（v1.2/v0.4.0 推荐入口）。

        Args:
            session_id: 显式指定复用已有 Session；为 None 时按 (user_id, agent_id) 自动分配。

        Returns:
            (Task, Session) 元组。
        """
        if session_id is not None:
            session = self.session_manager.get_session(session_id)
            if session is None or self.session_manager.is_session_expired(session_id):
                raise ValueError(f"session {session_id} not found or expired")
            if session.user_id != user_id:
                raise ValueError(
                    f"session {session_id} user_id mismatch: {session.user_id} != {user_id}"
                )
            session = self.session_manager.touch_session(session_id)
        else:
            session = self.session_manager.get_or_create_session(user_id, agent_id)

        task_id = uuid.uuid4().hex
        task = Task(
            task_id=task_id,
            session_id=session.session_id,
            user_id=user_id,
            agent_id=agent_id,
            description=description,
            status="created",
        )
        self.task_store.save(task)
        return task, session

    def get_task(self, task_id: str) -> Task | None:
        """v0.6.0：从持久化 TaskStore 读取 Task。"""
        return self.task_store.get(task_id)

    def add_user_message(self, session_id: str, task_id: str, content: str) -> ConversationMessage:
        """记录一条用户消息；供外部调用方在收到 ``needs_user_input`` 后写入回复。"""
        message = ConversationMessage(
            message_id=uuid.uuid4().hex,
            session_id=session_id,
            task_id=task_id,
            role="user",
            content=content,
        )
        self.conversation_store.append_message(message)
        return message

    def add_agent_message(
        self, session_id: str, task_id: str, content: str
    ) -> ConversationMessage:
        """记录一条 Agent 消息；通常由 Runtime 在 ``ask_user`` 时自动调用。"""
        message = ConversationMessage(
            message_id=uuid.uuid4().hex,
            session_id=session_id,
            task_id=task_id,
            role="agent",
            content=content,
        )
        self.conversation_store.append_message(message)
        return message

    def get_conversation_context(self, session_id: str) -> ConversationContext:
        """获取指定 session 的当前对话上下文。"""
        return self.conversation_store.get_context(session_id)

    async def start(self) -> None:
        """拉起 MCP server 子进程等异步初始化。"""
        await self.gateway.start()

    async def aclose(self) -> None:
        """关闭 MCP server 子进程。"""
        await self.gateway.aclose()


# ---------------------------------------------------------------------------
# Runtime 工厂
# ---------------------------------------------------------------------------


def build_runtime(
    config: AppConfig,
    *,
    opa_url: str = "http://127.0.0.1:8181",
    planner_yaml: str | Path | None = None,
    env_extra: dict[str, str] | None = None,
) -> Runtime:
    """从 ``AppConfig`` 组装 Runtime。

    Args:
        config: 经 ``ConfigLoader.load`` 加载并校验后的配置。
        opa_url: OPA sidecar HTTP 地址。
        planner_yaml: ScriptedPlanner 脚本路径；缺省使用 ``config/scriptured_plan.yaml``。
        env_extra: 传递给 MCP 子进程的额外环境变量；默认会注入 ``PYTHONPATH`` 指向项目 ``src``。
    """
    identity = ConfigIdentityProvider(config.agents, config.users)
    policy_store = FilePolicyStore(config.policy_dir)
    policy_engine = OPAPolicyEngine(base_url=opa_url, timeout=2.0)

    project_root = Path(config.policy_dir).parent
    mcp_env = {"PYTHONPATH": str(project_root / "src")}
    if env_extra is not None:
        mcp_env.update(env_extra)
    gateway = MCPGateway(
        mcp_servers=dict(config.mcp_servers),
        tool_mapping=config.tool_mapping,
        env_extra=mcp_env,
        cwd=str(project_root),
    )
    masker = Masker(config.masking_rules)
    budget_ledger = JsonlBudgetLedger(config.budget_ledger_path)
    session_manager = SessionManager(backend=JsonlSessionBackend(config.session_path))
    risk_manager = RiskStateManager(JsonlRiskStateStore(config.risk_state_path))
    conversation_store = JsonlConversationStore(
        config.conversation_path,
        max_messages_per_session=config.conversation_max_messages_per_session,
    )
    task_store = JsonlTaskStore(config.task_store_path)
    reservation_store = JsonlReservationStore(config.reservation_store_path)
    checkpoint = Checkpoint(
        profiles=config.profiles,
        policy_engine=policy_engine,
        policy_store=policy_store,
        gateway=gateway,
        identity=identity,
        session_manager=session_manager,
        risk_manager=risk_manager,
        decision_store=JsonlDecisionStore(config.decision_log_path),
        budget_ledger=budget_ledger,
        reservation_store=reservation_store,
        permission_analyzer=CompositePermissionInteractionAnalyzer(
            ConfigPermissionInteractionAnalyzer(config.permission_rules),
            CapabilityBasedPermissionAnalyzer(config.capability_rules),
        ),
        tool_costs={
            name: BudgetCost(token_count=entry.cost_per_call)
            for name, entry in config.tool_mapping.items()
        },
        masker=masker,
    )
    audit_key: bytes | None = None
    if config.audit_hash_algo == "hmac-sha256":
        audit_key = ConfigLoader.resolve_audit_key(config)
    audit_store = JsonlAuditStore(
        config.audit_log_path,
        hash_algo=config.audit_hash_algo,
        hmac_key=audit_key,
        key_id=config.audit_key_id,
    )
    approval_manager = AsyncApprovalManager(
        JsonlApprovalStore(config.approval_store_path)
    )

    if config.llm_planner is not None and config.llm_planner.enabled:
        planner: Planner = LLMPlanner(
            client=HttpxLLMClient(),
            config=config.llm_planner,
            gateway=gateway,
            budget_ledger=budget_ledger,
            audit_store=audit_store,
            profiles=config.profiles,
        )
    else:
        if planner_yaml is None:
            planner_yaml = project_root / "config" / "scripted_plan.yaml"
        planner = ScriptedPlanner.from_yaml(planner_yaml)

    return Runtime(
        planner=planner,
        classifier=RuleBasedClassifier(),
        checkpoint=checkpoint,
        gateway=gateway,
        approval_manager=approval_manager,
        audit_store=audit_store,
        masker=masker,
        profiles=config.profiles,
        session_manager=session_manager,
        risk_manager=risk_manager,
        conversation_store=conversation_store,
        task_store=task_store,
        reservation_store=reservation_store,
    )


# ---------------------------------------------------------------------------
# 执行循环（§5.2）
# ---------------------------------------------------------------------------


def _audit_event(
    task: Task,
    *,
    action: AuditAction,
    proposal: ActionProposal | None = None,
    decision: Decision | None = None,
    result: ToolResult | None = None,
    signal: RiskSignal | None = None,
    record=None,
    reason: str | None = None,
    masker: Masker | None = None,
) -> AuditEvent:
    """构造审计事件；含 args_hash（原始参数规范 JSON 摘要）与 args_mask（审计档掩码）。"""
    target = proposal.tool_name if proposal else None
    metadata: dict[str, Any] = {}
    if signal and signal.suggestion:
        metadata["classifier_suggestion"] = signal.suggestion
    if record and hasattr(record, "comment"):
        metadata["approval_comment"] = record.comment

    args_hash: str | None = None
    args_mask: dict | None = None
    if proposal is not None:
        args_hash = hashlib.sha256(
            canonical_json(proposal.arguments).encode("utf-8")
        ).hexdigest()
        if masker is not None:
            args_mask = masker.mask(proposal.arguments, "audit_log")

    return AuditEvent(
        event_id=uuid.uuid4().hex,
        trace_id=task.task_id,
        session_id=task.session_id,
        call_id=proposal.call_id if proposal else None,
        actor_type="agent" if action in ("propose",) else "checkpoint",
        actor_id=task.agent_id,
        action=action,
        target=target,
        decision=decision.verdict if decision else None,
        reason=reason
        or (decision.reason if decision else None)
        or (result.content if result else None),
        args_hash=args_hash,
        args_mask=args_mask,
        policy_version=decision.policy_version if decision else None,
        profile_version=decision.profile_version if decision else None,
        metadata=metadata,
    )


def _blocked_result(proposal: ActionProposal, decision: Decision) -> ToolResult:
    """被治理链路拦截的结果。"""
    return ToolResult(
        call_id=proposal.call_id,
        task_id=proposal.task_id,
        tool_name=proposal.tool_name,
        status="blocked",
        content=decision.reason,
        error_code="denied_by_checkpoint",
    )


def _blocked_decision(decision: Decision, proposal: ActionProposal, reason: str) -> Decision:
    """审批请求组装失败（如审批人冲突）时生成 deny Decision。"""
    return decision.model_copy(
        update={
            "verdict": "deny",
            "reason": reason,
            "expires_at": decision.expires_at,  # 保持原过期时间，但 max_uses=0 不可执行
            "max_uses": 0,
        }
    )


async def run_task(task: Task, agent: Agent, runtime: Runtime) -> TaskRunResult:
    """R1 执行循环入口（§5.2）。

    v0.3.0：返回 ``TaskRunResult``，支持 ``needs_user_input`` / ``needs_approval`` 暂停态。
    """
    return await _run_task_loop(task, agent, runtime, observations=[], pending=None)


async def resume_task(
    task: Task,
    agent: Agent,
    runtime: Runtime,
    *,
    observations: list[ToolResult] | None = None,
    pending: TaskRunResult | None = None,
) -> TaskRunResult:
    """在用户补充输入或审批完成后恢复任务执行。"""
    return await _run_task_loop(
        task, agent, runtime, observations=observations or [], pending=pending
    )


async def _run_task_loop(
    task: Task,
    agent: Agent,
    runtime: Runtime,
    *,
    observations: list[ToolResult],
    pending: TaskRunResult | None,
) -> TaskRunResult:
    """R1 执行循环（§5.2）：Planner → Classifier → Checkpoint.evaluate → forward/拦截。

    返回 ``completed``、``needs_user_input`` 或 ``needs_approval``。
    两种暂停态都不写 ``task_end``；外部补充输入/审批后调用 ``resume_task`` 继续。
    """
    audit = runtime.audit_store
    profile = runtime.profiles[agent.profile_id]
    conversation_context = runtime.get_conversation_context(task.session_id)

    # v1.2：校验 task.session_id 存在、活跃且绑定一致（fail-closed）
    runtime.session_manager.validate_and_touch(task)

    audit.append(_audit_event(task, action="task_start", masker=runtime.masker))
    ended = False
    try:
        # v0.3.0 Iteration 5：恢复待审批动作
        if pending is not None and pending.status == "needs_approval":
            decision = pending.pending_decision
            proposal = pending.pending_proposal
            if decision is None or proposal is None:
                raise ValueError("needs_approval pending 缺少 decision 或 proposal")

            record = runtime.approval_manager.check(decision.decision_id)
            if record is None:
                # 尚未审批，保持暂停态
                return pending

            decision = _finalize_after_approval(runtime, task, proposal, decision, record, audit)
            # v0.6.1：require_approval 路径在 evaluate() 已创建 pending_approval reservation，
            # 审批通过后 reservation 已转回 pending，执行前检查是否存在；不存在时回退到重新预留
            if decision.verdict in ("allow", "modify"):
                reservation = runtime.checkpoint.get_pending_reservation(proposal.call_id)
                if reservation is None:
                    if not runtime.checkpoint.reserve_for_execution(task.task_id, proposal):
                        raise CheckpointError(f"resume 时预算不足：{proposal.tool_name}")
            await _execute_decision(task, runtime, proposal, decision, audit, observations)
            ended = True

        while True:
            # 1. 规划下一步动作：Planner 只产出草案，框架组装 ActionProposal 并统一生成 call_id
            #    （v1.1 评审#7/#8：Planner 无权自定身份标识）
            planned = await runtime.planner.next_action(
                task, agent, observations, conversation_context
            )
            if planned is None:
                ended = True
                break

            # v0.3.0：Planner 显式请求用户输入；任务未结束，不写 task_end
            if isinstance(planned, UserQuestion):
                runtime.add_agent_message(
                    task.session_id,
                    task.task_id,
                    f"请求用户补充：{planned.question}",
                )
                return TaskRunResult(
                    status="needs_user_input",
                    task_id=task.task_id,
                    session_id=task.session_id,
                    question=planned.question,
                )

            proposal = ActionProposal(
                task_id=task.task_id,
                call_id=uuid.uuid4().hex,  # run_task 框架统一生成
                agent_id=agent.agent_id,
                tool_name=planned.tool_name,
                arguments=dict(planned.arguments),
                task_context=task.description[:200],  # 会被 checkpoint 用治理上下文替换
                reason=planned.reason,
            )

            # 2. R1 自检：轻量分类器，只产出信号
            signal = runtime.classifier.classify(task, agent, proposal, profile)
            proposal = proposal.model_copy(
                update={"risk_level": signal.risk_level, "risk_tags": signal.tags}
            )
            audit.append(
                _audit_event(
                    task,
                    action="propose",
                    proposal=proposal,
                    signal=signal,
                    reason=signal.reason,
                    masker=runtime.masker,
                )
            )

            # 3. R2 判定（传入 conversation_context 供框架构建治理上下文）
            decision = await runtime.checkpoint.evaluate(
                task, agent, proposal, conversation_context=conversation_context
            )
            audit.append(
                _audit_event(
                    task,
                    action="evaluate",
                    proposal=proposal,
                    decision=decision,
                    masker=runtime.masker,
                )
            )

            # 4. 需要审批 → 持久化 ApprovalRequest 并返回 needs_approval 暂停态
            if decision.verdict == "require_approval":
                try:
                    request = runtime.checkpoint.build_approval_request(decision, proposal, task)
                except Exception as exc:  # noqa: BLE001 - 冲突校验失败按 deny 处理
                    decision = _blocked_decision(decision, proposal, str(exc))
                else:
                    await runtime.approval_manager.submit(request)
                    return TaskRunResult(
                        status="needs_approval",
                        task_id=task.task_id,
                        session_id=task.session_id,
                        decision_id=decision.decision_id,
                        request_id=request.request_id,
                        pending_decision=decision,
                        pending_proposal=proposal,
                    )

            # 5. 执行或被拦截，结果都进入 observations 供下一步规划
            await _execute_decision(task, runtime, proposal, decision, audit, observations)
    finally:
        if ended:
            audit.append(_audit_event(task, action="task_end", masker=runtime.masker))
            runtime.checkpoint.forget_task(task.task_id)
            runtime.task_store.complete(task.task_id)

    return TaskRunResult(
        status="completed",
        task_id=task.task_id,
        session_id=task.session_id,
        question=None,
    )


async def _execute_decision(
    task: Task,
    runtime: Runtime,
    proposal: ActionProposal,
    decision: Decision,
    audit: AuditStore,
    observations: list[ToolResult],
) -> None:
    """根据 Decision 执行或拦截，并写审计。"""
    is_approval = "approval:granted" in decision.policy_hits
    if decision.verdict in ("allow", "modify"):
        try:
            result = await runtime.checkpoint.forward(proposal, decision, session_id=task.session_id)
        except CheckpointError as exc:
            # v0.3.0：审批通过后的 Decision 过期时写入 approval_expired 审计事件
            if is_approval and "已过期" in str(exc):
                audit.append(
                    _audit_event(
                        task,
                        action="approval_expired",
                        proposal=proposal,
                        decision=decision,
                        reason="审批授权已过期",
                        masker=runtime.masker,
                    )
                )
            raise
        # v0.3.0：审批通过后的 Decision 成功执行即消费，写入 approval_consumed
        if is_approval:
            audit.append(
                _audit_event(
                    task,
                    action="approval_consumed",
                    proposal=proposal,
                    decision=decision,
                    reason="审批授权已消费",
                    masker=runtime.masker,
                )
            )
    else:
        result = _blocked_result(proposal, decision)
    observations.append(result)
    audit.append(
        _audit_event(
            task,
            action="execute",
            proposal=proposal,
            decision=decision,
            result=result,
            masker=runtime.masker,
        )
    )


def _finalize_after_approval(
    runtime: Runtime,
    task: Task,
    proposal: ActionProposal,
    decision: Decision,
    record: ApprovalRecord,
    audit: AuditStore,
) -> Decision:
    """审批记录已存在时，完成审批审计与 finalize。"""
    audit.append(
        _audit_event(
            task,
            action="approve" if record.verdict == "approve" else "deny",
            proposal=proposal,
            record=record,
            masker=runtime.masker,
        )
    )
    # P0：强绑定校验需要原始 ApprovalRequest
    request = runtime.approval_manager.get_request(decision.decision_id)
    if request is None:
        raise CheckpointError(f"找不到 decision_id={decision.decision_id} 的审批请求")
    decision = runtime.checkpoint.finalize_after_approval(decision, record, request)
    # v1.2：审批结果进入会话风险状态
    if record.verdict == "approve":
        runtime.risk_manager.update(task.session_id, "approval_granted")
    else:
        runtime.risk_manager.update(task.session_id, "approval_denied")
    return decision
