"""Checkpoint（方案 §6.1 / §6.6）：R2 判定与执行前校验。

``evaluate()`` 实现判定流水线步骤 0-7；``forward()`` 实现执行前校验 1-8。
治理语义只许住在本组件：``forward`` 的校验、modify 复核不得下沉到 MCPGateway，
也不得上浮到 R1（开发指南纪律 4）。

当前状态（迭代 1/2 完成，已对齐 v1.1）：
- 步骤 1 DecisionStore：``JsonlDecisionStore`` 持久化 + call_id 全局唯一检测（v1.1）；
- 步骤 3 调用次数上限、步骤 4 预算（按工具 ``cost_per_call``）、步骤 5 权限组合、步骤 6 OPA 已接通；
- 审批分支：``require_approval`` → R0-delegate async 接口（评审#4）。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from loop_controller.authority import AuthorityManager, NoopAuthorityManager
from loop_controller.executors.base import ExecutionContext, ExecutorRegistry
from loop_controller.executors.mcp_executor import MCPExecutor
from loop_controller.governance_context import build_context_meta, build_governance_context
from loop_controller.identity import AgentIdentity, IdentityProvider
from loop_controller.identity.revocation import RevocationList, RevocationMatch, RevocationType
from loop_controller.infra.audit_store import AuditStore
from loop_controller.infra.config_loader import PermissionRule
from loop_controller.infra.policy_store import PolicyStore
from loop_controller.infra.reservation_store import InMemoryReservationStore, ReservationStore
from loop_controller.mcp_gateway import MCPGateway
from loop_controller.models import (
    ActionProposal,
    Agent,
    ApprovalRequest,
    AuditAction,
    AuditEvent,
    BudgetCost,
    BudgetReservation,
    CapabilityProfile,
    ConversationContext,
    Decision,
    Task,
    ToolResult,
)
from loop_controller.policy_engine import PolicyEngine, build_policy_input
from loop_controller.risk_state import RiskStateManager
from loop_controller.session import SessionManager
from loop_controller.utils.canonical import canonical_json
from loop_controller.utils.globmatch import glob_match

logger = logging.getLogger(__name__)

# 与 policies/default.rego 的 package 声明一致（Rego 侧斜杠路径见 policy_engine）。
_PACKAGE = "loop_controller.tool_permission"

# Decision.expires_at 分档（§3.6）：allow/modify +5min、require_approval +15min、deny 立即过期。
_ALLOW_MODIFY_DELTA = timedelta(minutes=5)
_APPROVAL_DELTA = timedelta(minutes=15)

# 每次工具调用的估算成本（§3.8）：v1.1（评审#3）起按工具 ``tool_costs`` 计费，
# 未配置的工具回退到该默认值（恒为正，防零成本绕过预算）。
_DEFAULT_PER_CALL_COST = BudgetCost(token_count=1)

# v0.29.0 BudgetReservation 合法状态转移表。
_LEGAL_RESERVATION_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"pending_approval", "committed", "refunded", "expired"},
    "pending_approval": {"pending", "committed", "refunded", "expired"},
}


def _utc_now() -> datetime:
    return datetime.now(UTC)


class CheckpointError(Exception):
    """forward 前置校验失败（调用方语义错误 / 授权过期 / 重放），向上抛异常。"""


# ---------------------------------------------------------------------------
# §4.5 DecisionStore：判定存储（防重放）
# ---------------------------------------------------------------------------


@runtime_checkable
class DecisionStore(Protocol):
    """持久化已签发的 Decision 使用记录，提供跨重启防重放（§4.5）。"""

    def is_call_id_seen(
        self, call_id: str
    ) -> bool: ...  # v1.1：全局唯一性检测（不再按 task_id 分区）
    def record_proposal(self, task_id: str, call_id: str) -> None: ...
    def record_decision(self, decision: Decision) -> None: ...  # v0.3.0：记录完整 Decision 元信息
    def get_decision(self, decision_id: str) -> Decision | None: ...  # v0.3.0
    def use_decision(
        self, decision_id: str, now: datetime
    ) -> bool: ...  # v0.3.0：原子检查过期/次数并落盘
    def record_finalized(self, decision_id: str) -> None: ...  # v0.29.0
    def is_decision_finalized(self, decision_id: str) -> bool: ...  # v0.29.0


class DecisionAlreadyConsumed(CheckpointError):
    """审批结果已被消费，重复 resume 时抛出（v0.29.0）。"""


class InMemoryDecisionStore:
    """内存版 DecisionStore（迭代 1 占位；T2.1 替换为 Jsonl 持久化版）。

    接口与最终实现一致，仅不持久化（进程重启即失效）。
    """

    def __init__(self) -> None:
        self._call_ids: set[str] = set()
        self._decisions: dict[str, Decision] = {}
        self._used_counts: dict[str, int] = {}
        self._finalized: set[str] = set()

    def is_call_id_seen(self, call_id: str) -> bool:
        return call_id in self._call_ids

    def record_proposal(self, task_id: str, call_id: str) -> None:
        self._call_ids.add(call_id)

    def record_decision(self, decision: Decision) -> None:
        self._decisions[decision.decision_id] = decision
        self._used_counts.setdefault(decision.decision_id, 0)

    def get_decision(self, decision_id: str) -> Decision | None:
        return self._decisions.get(decision_id)

    def use_decision(self, decision_id: str, now: datetime) -> bool:
        decision = self._decisions.get(decision_id)
        if decision is None:
            return False
        if now >= decision.expires_at:
            return False
        if self._used_counts.get(decision_id, 0) >= decision.max_uses:
            return False
        self._used_counts[decision_id] = self._used_counts.get(decision_id, 0) + 1
        return True

    def record_finalized(self, decision_id: str) -> None:
        self._finalized.add(decision_id)

    def is_decision_finalized(self, decision_id: str) -> bool:
        return decision_id in self._finalized


# ---------------------------------------------------------------------------
# §3.8 BudgetLedger：预算记账
# ---------------------------------------------------------------------------


@runtime_checkable
class BudgetLedger(Protocol):
    """预算记账（§3.8）：reserve → commit / refund 三路径。"""

    def check_and_reserve(self, task_id: str, cost: BudgetCost) -> bool: ...
    def commit(self, task_id: str, cost: BudgetCost) -> None: ...
    def refund(self, task_id: str, cost: BudgetCost) -> None: ...


class InfiniteBudgetLedger:
    """恒通过的预算占位（迭代 1；T2.4 换 InMemoryBudgetLedger 真计数）。"""

    def check_and_reserve(self, task_id: str, cost: BudgetCost) -> bool:
        return True

    def commit(self, task_id: str, cost: BudgetCost) -> None:
        pass

    def refund(self, task_id: str, cost: BudgetCost) -> None:
        pass


# ---------------------------------------------------------------------------
# §6.2 PermissionInteractionAnalyzer：权限组合规则
# ---------------------------------------------------------------------------


@runtime_checkable
class PermissionInteractionAnalyzer(Protocol):
    """权限组合分析（§6.2）：返回命中的规则；无命中返回 None。"""

    def check(
        self,
        current: ActionProposal,
        history: list[ActionProposal],
    ) -> PermissionRule | None: ...


class NoopPermissionInteractionAnalyzer:
    """恒无命中的组合规则占位（迭代 1；T2.3 换真实现）。"""

    def check(
        self,
        current: ActionProposal,
        history: list[ActionProposal],
    ) -> PermissionRule | None:
        return None


# ---------------------------------------------------------------------------
# Checkpoint：evaluate + forward
# ---------------------------------------------------------------------------


class Checkpoint:
    """R2 判定与执行前校验（PDP + PEP 合一）。

    Args:
        profiles: profile_id -> CapabilityProfile（来自 ConfigLoader）。
        policy_engine: OPA/Rego 主策略引擎。
        policy_store: 策略版本来源（Decision.policy_version）。
        gateway: MCPGateway（forward 的默认执行通道；v0.20.0 保留以兼容旧构造）。
        executor_registry: 执行器注册表（v0.20.0 新增；未提供时自动用 gateway 构造 MCPExecutor）。
        identity: 可信身份源（步骤 0 交叉校验用）。
        decision_store: 防重放存储（默认内存占位）。
        budget_ledger: 预算记账（默认恒通过占位）。
        permission_analyzer: 组合规则分析（默认无命中占位）。
        now: 可注入的时间源（测试用），默认 UTC now。
    """

    def __init__(
        self,
        *,
        profiles: dict[str, CapabilityProfile],
        policy_engine: PolicyEngine,
        policy_store: PolicyStore,
        gateway: MCPGateway | None = None,
        executor_registry: ExecutorRegistry | None = None,
        identity: IdentityProvider,
        session_manager: SessionManager | None = None,  # v1.2 会话管理
        risk_manager: RiskStateManager | None = None,  # v1.2 风险状态管理
        decision_store: DecisionStore | None = None,
        budget_ledger: BudgetLedger | None = None,
        reservation_store: ReservationStore | None = None,  # v0.6.1 预算预留状态机
        permission_analyzer: PermissionInteractionAnalyzer | None = None,
        authority_manager: AuthorityManager | None = None,  # v0.11.0 动态权限提升
        tool_costs: dict[str, BudgetCost] | None = None,  # v1.1：tool_name -> 单次调用成本
        masker=None,  # Masker（T3.2 接入 build_approval_request）
        revocation_list: RevocationList | None = None,
        audit_store: AuditStore | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if executor_registry is None:
            if gateway is None:
                raise ValueError("Checkpoint 必须提供 gateway 或 executor_registry")
            executor_registry = ExecutorRegistry()
            executor_registry.set_default(MCPExecutor(gateway))
        self._profiles = profiles
        self._policy_engine = policy_engine
        self._policy_store = policy_store
        self._gateway = gateway
        self._executor_registry = executor_registry
        self._identity = identity
        self._session_manager = session_manager
        self._risk_manager = risk_manager or RiskStateManager()  # 默认内存实现
        self._decision_store = decision_store or InMemoryDecisionStore()
        self._budget_ledger = budget_ledger or InfiniteBudgetLedger()
        self._reservation_store = reservation_store or InMemoryReservationStore()
        self._permission_analyzer = permission_analyzer or NoopPermissionInteractionAnalyzer()
        self._authority_manager = authority_manager or NoopAuthorityManager()
        self._tool_costs = tool_costs or {}
        self._masker = masker
        self._revocation_list = revocation_list
        self._audit_store = audit_store
        self._now = now or _utc_now
        # per-task 已成功执行的动作历史（§6.1 步骤 5 / 偏离 D12），任务结束即弃。
        self._history: dict[str, list[ActionProposal]] = {}

    # -- 生命周期 -----------------------------------------------------------

    def forget_task(self, task_id: str) -> None:
        """任务结束时丢弃该任务的 per-task 历史（runtime 在 task_end 调用）。"""
        self._history.pop(task_id, None)

    def _cost_for(self, proposal: ActionProposal) -> BudgetCost:
        """该工具单次调用的估算成本（v1.1：按工具 cost_per_call；未配置回退默认值）。"""
        return self._tool_costs.get(proposal.tool_name, _DEFAULT_PER_CALL_COST)

    def _consume_authority_tokens(self, proposal: ActionProposal) -> None:
        """v0.11.0：动作成功执行后消费 token 预算；失败仅记录日志。"""
        cost = self._cost_for(proposal)
        for token_id in proposal.authority_token_ids:
            updated = self._authority_manager.consume(token_id, cost)
            if updated is None:
                logger.warning("Authority token %s budget exhausted or invalid", token_id)

    async def _audit_authority_consumption(
        self,
        proposal: ActionProposal,
        tokens: list,
        *,
        session_id: str | None,
        outcome: str,
        refunded: bool,
    ) -> None:
        if self._audit_store is None:
            return
        for token in tokens:
            await self._audit_store.append_async(
                AuditEvent(
                    event_id=uuid.uuid4().hex,
                    trace_id=proposal.task_id,
                    session_id=session_id or proposal.task_id,
                    call_id=proposal.call_id,
                    actor_type="checkpoint",
                    actor_id=proposal.agent_id,
                    action="authority_used",
                    target=proposal.tool_name,
                    reason=f"external execution outcome: {outcome}",
                    metadata={
                        "token_id": token.token_id,
                        "remaining_budget": token.remaining_budget.model_dump(mode="json"),
                        "execution_outcome": outcome,
                        "refunded": refunded,
                    },
                )
            )

    async def _audit_execution_event(
        self,
        action: AuditAction,
        proposal: ActionProposal,
        result: ToolResult | None,
        *,
        session_id: str | None,
        user_id: str,
        tenant_id: str | None,
        reason: str,
    ) -> None:
        """v0.36.1：写入 execution_* 阶段审计事件，避免同名 execute 重复。"""
        if self._audit_store is None:
            return
        metadata: dict[str, Any] = {
            "user_id": user_id,
        }
        if result is not None:
            metadata["result_status"] = result.status
            if result.error_code:
                metadata["error_code"] = result.error_code
            harness_evidence = result.metadata.get("harness_evidence")
            if harness_evidence is not None:
                metadata["harness_evidence"] = harness_evidence
        await self._audit_store.append_async(
            AuditEvent(
                event_id=uuid.uuid4().hex,
                trace_id=proposal.task_id,
                session_id=session_id or proposal.task_id,
                call_id=proposal.call_id,
                actor_type="agent",
                actor_id=proposal.agent_id,
                action=action,
                target=proposal.tool_name,
                decision="allow" if action == "execution_authorized" else None,
                reason=reason,
                metadata=metadata,
            )
        )

    def _refund_for(self, proposal: ActionProposal) -> None:
        """将当前 proposal 已预留的预算返还（P0：所有非执行路径必须 refund）。"""
        self._budget_ledger.refund(proposal.task_id, self._cost_for(proposal))

    def reserve_for_execution(self, task_id: str, proposal: ActionProposal) -> bool:
        """为即将执行的动作预留预算（resume 路径使用）。

        v0.6.1：若预留成功，同时创建 pending BudgetReservation。
        """
        cost = self._cost_for(proposal)
        if not self._budget_ledger.check_and_reserve(task_id, cost):
            return False
        now = self._now()
        reservation = BudgetReservation(
            reservation_id=uuid.uuid4().hex,
            task_id=task_id,
            call_id=proposal.call_id,
            tool_name=proposal.tool_name,
            cost=cost,
            state="pending",
            created_at=now,
            expires_at=now + _ALLOW_MODIFY_DELTA,
        )
        self._save_reservation(reservation)
        return True

    # v0.6.1：BudgetReservation 状态机辅助方法 ---------------------------------

    def _create_reservation(self, proposal: ActionProposal, now: datetime) -> BudgetReservation:
        """为成功预留预算的 proposal 创建 pending reservation。"""
        return BudgetReservation(
            reservation_id=uuid.uuid4().hex,
            task_id=proposal.task_id,
            call_id=proposal.call_id,
            tool_name=proposal.tool_name,
            cost=self._cost_for(proposal),
            state="pending",
            created_at=now,
            expires_at=now + _ALLOW_MODIFY_DELTA,
        )

    def _save_reservation(self, reservation: BudgetReservation) -> None:
        """保存 reservation 到 store。"""
        self._reservation_store.save(reservation)

    def _transition_reservation(
        self,
        reservation: BudgetReservation,
        state: str,
        *,
        expires_at: datetime | None = None,
    ) -> BudgetReservation:
        """转换 reservation 状态并持久化；非法转移抛 CheckpointError。"""
        current = self._reservation_store.get(reservation.reservation_id) or reservation
        if current.state == state:
            return current
        allowed = _LEGAL_RESERVATION_TRANSITIONS.get(current.state, set())
        if state not in allowed:
            raise CheckpointError(f"非法 reservation 状态转移：{current.state!r} -> {state!r}")
        update: dict = {"state": state}
        if expires_at is not None:
            update["expires_at"] = expires_at
        updated = current.model_copy(update=update)
        self._save_reservation(updated)
        return updated

    def _refund_reservation(self, reservation: BudgetReservation) -> BudgetReservation:
        """幂等返还未提交 reservation；历史 committed reservation 保持不变。

        v0.29.0：先写 reservation 状态为 refunded，再 budget.refund，
        避免崩溃窗口导致重复 commit。
        """
        current = self._reservation_store.get(reservation.reservation_id) or reservation
        if current.state not in ("pending", "pending_approval"):
            return current
        updated = self._transition_reservation(current, "refunded")
        self._budget_ledger.refund(updated.task_id, updated.cost)
        return updated

    def refund_reservation_for_call(self, call_id: str) -> BudgetReservation | None:
        """统一释放指定调用仍处于活动状态的预算预留。"""
        reservation = self._reservation_store.get_by_call_id(call_id)
        if reservation is None:
            return None
        return self._refund_reservation(reservation)

    def _commit_reservation(self, reservation: BudgetReservation) -> BudgetReservation:
        """确认 reservation 对应预算消耗并标记为 committed。"""
        current = self._reservation_store.get(reservation.reservation_id) or reservation
        if current.state not in ("pending", "pending_approval"):
            raise CheckpointError(f"cannot commit reservation in state {current.state!r}")
        self._budget_ledger.commit(current.task_id, current.cost)
        return self._transition_reservation(current, "committed")

    def _to_pending_approval(
        self, reservation: BudgetReservation, now: datetime
    ) -> BudgetReservation:
        """将 reservation 转为 pending_approval，保持预算预留。"""
        return self._transition_reservation(
            reservation, "pending_approval", expires_at=now + _APPROVAL_DELTA
        )

    def _expire_reservation(self, reservation: BudgetReservation) -> BudgetReservation:
        """将 reservation 标记为 expired（不自动 refund，由调用方决定是否返还）。"""
        return self._transition_reservation(reservation, "expired")

    def get_pending_reservation(self, call_id: str) -> BudgetReservation | None:
        """v0.6.1 / v0.29.0：查询未过期的 pending / pending_approval reservation。"""
        reservation = self._reservation_store.get_by_call_id(call_id)
        if reservation is None or reservation.state not in ("pending", "pending_approval"):
            return None
        if reservation.expires_at is not None and reservation.expires_at < self._now():
            return None
        return reservation

    def get_pending_reservations(self, task_id: str) -> list[BudgetReservation]:
        """v0.6.1 / v0.29.0：查询指定 task 下未过期的 pending / pending_approval reservation。"""
        now = self._now()
        return [
            r
            for r in self._reservation_store.list_by_task(task_id)
            if r.state in ("pending", "pending_approval")
            and (r.expires_at is None or r.expires_at >= now)
        ]

    def _get_expired_reservation_by_call_id(self, call_id: str) -> BudgetReservation | None:
        """v0.29.0-fix：查询 call_id 对应是否已存在过期但未被清理的 reservation。"""
        reservation = self._reservation_store.get_by_call_id(call_id)
        if reservation is None or reservation.state not in ("pending", "pending_approval"):
            return None
        if reservation.expires_at is None or reservation.expires_at >= self._now():
            return None
        return reservation

    def recover_stale_reservations(self, now: datetime | None = None) -> None:
        """v0.29.0：扫描并清理过期的 pending / pending_approval reservation。

        对每一项执行：refund 预算、标记 expired、写审计事件。
        幂等：已 refunded / expired / committed 的 reservation 会跳过。
        """
        now = now or self._now()
        for reservation in self._reservation_store.list_all():
            if reservation.state not in ("pending", "pending_approval"):
                continue
            if reservation.expires_at is None or reservation.expires_at >= now:
                continue
            current = self._reservation_store.get(reservation.reservation_id)
            if current is None:
                continue
            if current.state not in ("pending", "pending_approval"):
                continue
            if current.expires_at is None or current.expires_at >= now:
                continue
            self._budget_ledger.refund(current.task_id, current.cost)
            self._transition_reservation(current, "expired")
            if self._audit_store is not None:
                event = AuditEvent(
                    event_id=uuid.uuid4().hex,
                    trace_id=current.task_id,
                    session_id=current.task_id,
                    actor_type="system",
                    actor_id="checkpoint",
                    action="reservation_expired",
                    target=current.tool_name,
                    metadata={
                        "reservation_id": current.reservation_id,
                        "call_id": current.call_id,
                        "previous_state": current.state,
                        "cost": current.cost.model_dump(mode="json"),
                    },
                )
                try:
                    self._audit_store.append(event)
                except Exception as exc:
                    logger.warning("recover_stale_reservations 审计事件写入失败: %s", exc)

    # -- evaluate：判定流水线（§6.1） ----------------------------------------

    async def evaluate(
        self,
        task: Task,
        agent: Agent,
        proposal: ActionProposal,
        conversation_context: ConversationContext | None = None,
    ) -> Decision:
        """对一次工具申报给出权威判定。步骤顺序固定，任一失败即短路。"""
        now = self._now()
        policy_version = self._policy_store.current_version()

        # v0.3.0：用框架构建的治理上下文替换 proposal 中的静态 task_context
        if conversation_context is not None:
            governance_context = build_governance_context(task, conversation_context)
            context_meta = build_context_meta(task, conversation_context, governance_context)
            proposal = proposal.model_copy(update={"task_context": governance_context})
        else:
            context_meta = None

        # 步骤 0：身份交叉校验（agent 必须来自 IdentityProvider，proposal 与之一致）
        if proposal.agent_id != agent.agent_id:
            return self._deny(proposal, "identity mismatch", now, policy_version)
        if self._identity.get_agent(agent.agent_id) is None:
            return self._deny(proposal, "unknown agent", now, policy_version)

        # 步骤 1：重放检测（DecisionStore；v1.1 全局唯一性检测）
        if self._decision_store.is_call_id_seen(proposal.call_id):
            return self._deny(proposal, "duplicate call_id", now, policy_version)
        self._decision_store.record_proposal(task.task_id, proposal.call_id)

        # 步骤 2：Profile 与工具存在性（默认拒绝，不进入 Rego，减少攻击面）
        profile = self._profiles.get(agent.profile_id)
        if profile is None:
            return self._deny(proposal, "unknown profile", now, policy_version)
        perm = profile.tools.get(proposal.tool_name)
        if perm is None or not perm.allowed:
            return self._deny(proposal, "tool not permitted", now, policy_version)

        # 步骤 2.5：Session 连续拒绝硬熔断（v0.4.0）
        session_risk = self._risk_manager.get_profile(task.session_id)
        if session_risk.consecutive_deny_count >= profile.session_block_threshold:
            return self._deny(
                proposal,
                f"session blocked: consecutive deny count {session_risk.consecutive_deny_count}",
                now,
                policy_version,
                policy_hits=["session_consecutive_deny_block"],
            )

        # 步骤 3：调用次数上限（per-task 成功执行历史计数 vs max_calls_per_task）
        if perm.max_calls_per_task is not None:
            call_count = sum(
                1 for h in self._history.get(task.task_id, []) if h.tool_name == proposal.tool_name
            )
            if call_count >= perm.max_calls_per_task:
                return self._deny(proposal, "call limit exceeded", now, policy_version)

        # 步骤 4：预算（InMemoryBudgetLedger 按任务设置 Profile 上限；成本按工具 cost_per_call）
        if hasattr(self._budget_ledger, "set_budget"):
            self._budget_ledger.set_budget(task.task_id, profile.max_budget_token)
        if not self._budget_ledger.check_and_reserve(task.task_id, self._cost_for(proposal)):
            return self._deny(proposal, "budget exceeded", now, policy_version)

        # v0.6.1：预算预留成功后创建显式 reservation，后续所有路径统一流转状态
        reservation = self._create_reservation(proposal, now)
        self._save_reservation(reservation)

        # 步骤 5：权限组合分析（v0.10.0 Capability-Based Analyzer）
        history = self._history.get(task.task_id, [])
        pending_approval = False
        authority_override = False  # v0.11.0：有效 token 覆盖了 deny
        rule = self._permission_analyzer.check(proposal, history)
        if rule is not None:
            # 把组合风险标签/分数写入 proposal，供 Rego 与审计使用
            proposal = proposal.model_copy(
                update={
                    "combination_risk_tags": list(rule.risk_tags),
                    "combination_risk_score": rule.score,
                }
            )
            if rule.action == "deny":
                # v0.11.0：若持有覆盖触发能力的有效 token，把裁决权交给 Rego；否则短路 deny
                if rule.triggered_capabilities:
                    valid_tokens = self._authority_manager.validate_for_proposal(
                        proposal, rule.triggered_capabilities
                    )
                    if valid_tokens:
                        proposal = proposal.model_copy(
                            update={"authority_token_ids": [t.token_id for t in valid_tokens]}
                        )
                        authority_override = True
                    else:
                        self._refund_reservation(reservation)
                        return self._deny(
                            proposal, rule.reason, now, policy_version, policy_hits=[rule.id]
                        )
                else:
                    self._refund_reservation(reservation)
                    return self._deny(
                        proposal, rule.reason, now, policy_version, policy_hits=[rule.id]
                    )
            else:
                pending_approval = True  # require_approval 不短路，继续走 Rego（deny 优先原则）

        # 步骤 5.5：防御性校验 proposal 中声明的 token（无论组合规则是否命中）
        if proposal.authority_token_ids and not authority_override:
            # 仅校验 token 仍然有效；不影响已有 rule 决策
            valid_tokens = self._authority_manager.validate_for_proposal(proposal, [])
            proposal = proposal.model_copy(
                update={"authority_token_ids": [t.token_id for t in valid_tokens]}
            )

        # 步骤 6：主策略查询（OPA/Rego；引擎内部任何异常已 fail-closed 为 deny）
        session_risk = self._risk_manager.get_profile(task.session_id)
        rego_decision = await self._policy_engine.evaluate(
            _PACKAGE, build_policy_input(proposal, agent, profile, session_risk, context_meta)
        )
        verdict = rego_decision.get("verdict")
        if verdict == "deny":
            self._risk_manager.update(task.session_id, "deny")
            self._refund_reservation(reservation)
            return self._deny(
                proposal,
                rego_decision.get("reason", "denied by policy"),
                now,
                policy_version,
                policy_hits=rego_decision.get("policy_hits"),
            )
        if verdict not in ("allow", "modify", "require_approval"):
            self._risk_manager.update(task.session_id, "deny")
            self._refund_reservation(reservation)
            return self._deny(proposal, "invalid policy verdict", now, policy_version)

        # 步骤 7：汇总输出 Decision。
        # 裁决优先级总表（v1.1 显式声明，评审#6）：
        #     deny > require_approval > modify > allow
        # 任一来源（组合规则 / Rego / 前置检查）产出更严格的裁决时，覆盖更宽松的裁决；
        # 组合规则可以否决或升级 Rego 的 allow，反之不行。
        hits = list(rego_decision.get("policy_hits") or [])
        # v1.2：高 session_risk 时，Reg 返回的 modify 也必须升级为 require_approval。
        session_risk_above = session_risk.cumulative_risk_score >= profile.session_risk_threshold
        if (
            pending_approval
            or verdict == "require_approval"
            or (verdict == "modify" and session_risk_above)
        ):
            reason = rego_decision.get("reason", "requires human approval")
            if rule is not None and verdict != "require_approval":
                hits.append(rule.id)
            if verdict == "modify" and session_risk_above:
                reason = "session risk score above threshold; modify upgraded to approval"
                hits.append("session_risk_gate")
            self._risk_manager.update(task.session_id, "require_approval")
            if proposal.risk_level == "critical":
                self._risk_manager.update(task.session_id, "critical")
            # v0.6.1：require_approval 保持预算预留，审批通过后直接 commit，无需二次 reserve
            reservation = self._to_pending_approval(reservation, now)
            return self._handle_require_approval(
                agent, proposal, profile, reason, now, policy_version, hits
            )

        if proposal.risk_level == "critical":
            self._risk_manager.update(task.session_id, "critical")
        modified_args = rego_decision.get("modified_args") if verdict == "modify" else None
        decision = Decision(
            decision_id=uuid.uuid4().hex,
            call_id=proposal.call_id,
            task_id=proposal.task_id,
            verdict=verdict,  # allow / modify
            reason=rego_decision.get("reason", "allowed by policy"),
            modified_args=modified_args,  # 向后兼容
            original_args=proposal.arguments if verdict == "modify" else None,
            policy_modified_args=modified_args,
            effective_args=None,  # forward 复核后回填
            escalation_target=rego_decision.get("escalation_target"),
            policy_hits=hits,
            policy_version=policy_version,
            profile_version=profile.version,
            expires_at=now + _ALLOW_MODIFY_DELTA,
            max_uses=1,
        )
        self._decision_store.record_decision(decision)
        return decision

    def _handle_require_approval(
        self,
        agent: Agent,
        proposal: ActionProposal,
        profile: CapabilityProfile,
        reason: str,
        now: datetime,
        policy_version: str,
        hits: list[str],
    ) -> Decision:
        """返回 require_approval Decision；真正审批由 AsyncApprovalManager + CLI 完成。"""
        decision = Decision(
            decision_id=uuid.uuid4().hex,
            call_id=proposal.call_id,
            task_id=proposal.task_id,
            verdict="require_approval",
            reason=reason,
            escalation_target=agent.owner_id,
            policy_hits=hits,
            policy_version=policy_version,
            profile_version=profile.version,
            expires_at=now + _APPROVAL_DELTA,
            max_uses=1,
        )
        self._decision_store.record_decision(decision)
        return decision

    def build_approval_request(
        self,
        decision: Decision,
        proposal: ActionProposal,
        task: Task,
    ) -> ApprovalRequest:
        """组装审批请求；冲突校验失败直接抛 CheckpointError（§3.10）。"""
        approver_id = decision.escalation_target or ""
        if approver_id == task.user_id or approver_id == proposal.agent_id:
            raise CheckpointError(f"审批人冲突：approver_id={approver_id} 与 requester/agent 相同")
        return ApprovalRequest(
            request_id=uuid.uuid4().hex,
            decision_id=decision.decision_id,
            call_id=proposal.call_id,
            task_id=proposal.task_id,
            agent_id=proposal.agent_id,
            tool_name=proposal.tool_name,
            arguments_masked=(
                self._masker.mask(proposal.arguments, "approval_request")
                if self._masker is not None
                else dict(proposal.arguments)
            ),
            tool_arguments=dict(proposal.arguments),
            original_decision=decision,
            reason=decision.reason,
            requester_id=task.user_id,
            approver_id=approver_id,
        )

    def finalize_after_approval(
        self, decision: Decision, record, request: ApprovalRequest
    ) -> Decision:
        """审批通过后生成可执行 Decision（approve→allow；deny→deny）。

        P0：强绑定校验——审批记录必须与原始 Decision、ApprovalRequest 完全匹配，
        且审批人必须是 escalation_target；否则抛 CheckpointError。

        v0.29.0：只有所有校验（含 deny-comment）通过后，才标记 decision 为 finalized；
        未知审批 verdict 抛 CheckpointError。

        v0.6.1：同时流转对应的 BudgetReservation 状态；审批 deny 时 refund，
        approve 时 reservation 保持 pending 供 forward commit。
        """
        now = self._now()
        if decision.decision_id != record.decision_id:
            raise CheckpointError("审批记录 decision_id 与 Decision 不匹配")
        if request.request_id != record.request_id:
            raise CheckpointError("审批记录 request_id 与 ApprovalRequest 不匹配")
        if decision.escalation_target is None or decision.escalation_target != record.approver_id:
            raise CheckpointError("审批人不是该 Decision 的 escalation_target")
        if decision.verdict != "require_approval":
            raise CheckpointError("只有 require_approval 状态的 Decision 才能被审批")
        if decision.expires_at is not None and decision.expires_at < now:
            raise CheckpointError("Decision 已过期")
        if self._decision_store.is_decision_finalized(decision.decision_id):
            raise DecisionAlreadyConsumed("该审批结果已被应用，不可重复执行")

        # v0.6.1：找到对应 reservation
        reservation = self._reservation_store.get_by_call_id(decision.call_id)

        if record.verdict == "deny":
            if not record.comment or not str(record.comment).strip():
                raise CheckpointError("deny 审批必须提供原因")
            # 所有校验通过，标记 finalized
            self._decision_store.record_finalized(decision.decision_id)
            if reservation is not None:
                self._refund_reservation(reservation)
            return decision.model_copy(
                update={
                    "verdict": "deny",
                    "reason": f"approval denied: {record.comment}",
                    "expires_at": now,
                    "max_uses": 0,
                }
            )

        if record.verdict != "approve":
            raise CheckpointError(f"未知审批 verdict：{record.verdict!r}")

        # approve：reservation 保持 pending，forward 执行时 commit
        if reservation is not None and reservation.state == "pending_approval":
            self._transition_reservation(
                reservation, "pending", expires_at=now + _ALLOW_MODIFY_DELTA
            )
        # 所有校验通过，标记 finalized
        self._decision_store.record_finalized(decision.decision_id)
        return decision.model_copy(
            update={
                "verdict": "allow",
                "reason": f"approval granted: {record.comment}",
                "expires_at": now + _ALLOW_MODIFY_DELTA,
                "policy_hits": decision.policy_hits + ["approval:granted"],
            }
        )

    def _deny(
        self,
        proposal: ActionProposal,
        reason: str,
        now: datetime,
        policy_version: str,
        *,
        policy_hits: list[str] | None = None,
        profile_version: str = "",
    ) -> Decision:
        """deny 快捷构造：立即过期、max_uses=0（§3.6 分档）。"""
        decision = Decision(
            decision_id=uuid.uuid4().hex,
            call_id=proposal.call_id,
            task_id=proposal.task_id,
            verdict="deny",
            reason=reason,
            policy_hits=list(policy_hits or []),
            policy_version=policy_version,
            profile_version=profile_version,
            expires_at=now,  # 立即过期
            max_uses=0,
        )
        self._decision_store.record_decision(decision)
        return decision

    def resolve_secret_refs(self, tool_name: str, arguments: dict) -> list[str]:
        """统一解析执行器可信配置与参数补充声明中的 Secret 引用。"""
        return self._executor_registry.resolve_secret_refs(tool_name, arguments)

    def check_revocation(
        self,
        identity: AgentIdentity,
        tool_name: str,
        arguments: dict,
    ) -> RevocationMatch:
        """使用共享吊销快照与执行器当前可信配置检查一次调用。"""
        if self._revocation_list is None:
            return RevocationMatch(revoked=False)
        return self._revocation_list.match(
            identity, tool_name, self.resolve_secret_refs(tool_name, arguments)
        )

    async def handle_revocation_block(
        self,
        *,
        identity: AgentIdentity,
        proposal: ActionProposal,
        task: Task,
        match: RevocationMatch,
        stage: str,
    ) -> ToolResult:
        """统一退款、结构化审计并返回吊销阻断结果。"""
        self.refund_reservation_for_call(proposal.call_id)
        if self._audit_store is not None:
            await self._audit_store.append_async(
                AuditEvent(
                    event_id=uuid.uuid4().hex,
                    trace_id=task.task_id,
                    session_id=task.session_id,
                    call_id=proposal.call_id,
                    actor_type="agent",
                    actor_id=identity.agent_id,
                    action="revocation_blocked",
                    target=proposal.tool_name,
                    decision="blocked",
                    reason=match.reason or "revoked",
                    metadata={
                        "revocation_type": (
                            match.type.value
                            if isinstance(match.type, RevocationType)
                            else match.type
                        ),
                        "revocation_id": match.id,
                        "stage": stage,
                    },
                )
            )
        return ToolResult(
            call_id=proposal.call_id,
            task_id=proposal.task_id,
            tool_name=proposal.tool_name,
            status="blocked",
            content=match.reason or "revoked",
            error_code="revoked",
        )

    # -- forward：执行前校验（§6.6） ----------------------------------------

    async def forward(
        self,
        proposal: ActionProposal,
        decision: Decision,
        session_id: str | None = None,
        user_id: str = "",
        tenant_id: str | None = None,
    ) -> ToolResult:
        """校验 1-5 失败抛异常；modify 复核失败返回 blocked 结果（不抛异常）。

        Args:
            session_id: v1.2 新增，用于 forward 成功后按低风险成功衰减会话风险分。
            user_id: v0.20.0 新增，用于构造执行器上下文；调用方未提供时为空字符串。
            tenant_id: v0.22.0 新增，用于 Secret Broker 租户命名空间路由。
        """
        # v0.6.1 / v0.29.0：先定位或现场创建预算预留，使后续所有 CheckpointError
        # 路径（含 decision 过期、防重放失败等）都能统一 refund，避免预留悬空。
        reservation = self.get_pending_reservation(proposal.call_id)
        if reservation is None:
            reservation = self._get_expired_reservation_by_call_id(proposal.call_id)
            if reservation is not None:
                self._refund_reservation(reservation)
                raise CheckpointError("reservation expired")
            if not self.reserve_for_execution(proposal.task_id, proposal):
                raise CheckpointError("找不到对应预算预留且无法现场预留（预算不足）")
            reservation = self.get_pending_reservation(proposal.call_id)
            if reservation is None:
                raise CheckpointError("现场预留后仍找不到预算预留")

        try:
            # 校验 1-3：语义错误 / 过期授权一律抛异常（调用方 bug，不静默）
            if decision.call_id != proposal.call_id:
                raise CheckpointError("decision.call_id 与 proposal.call_id 不一致")
            if decision.verdict not in ("allow", "modify"):
                raise CheckpointError(f"verdict {decision.verdict!r} 不可执行（仅 allow/modify）")
            if self._now() >= decision.expires_at:
                raise CheckpointError("decision 已过期（授权作废）")
            # 校验 6：modify 复核（PEP 职责，不抛异常，返回 blocked）
            effective_args = proposal.arguments
            if decision.verdict == "modify":
                # v0.36.1：明确 original / policy_modified / effective 三阶段参数语义。
                if decision.original_args is None or canonical_json(
                    decision.original_args
                ) != canonical_json(proposal.arguments):
                    self._refund_reservation(reservation)
                    await self._audit_execution_event(
                        "execution_blocked",
                        proposal,
                        None,
                        session_id=session_id,
                        user_id=user_id,
                        tenant_id=tenant_id,
                        reason="Decision 记录的 original_args 与当前 proposal 不一致",
                    )
                    return self._blocked(proposal, "Decision 记录的 original_args 与当前 proposal 不一致")
                candidate = decision.policy_modified_args
                if candidate is None:
                    self._refund_reservation(reservation)
                    await self._audit_execution_event(
                        "execution_blocked",
                        proposal,
                        None,
                        session_id=session_id,
                        user_id=user_id,
                        tenant_id=tenant_id,
                        reason="policy_modified_args 缺失",
                    )
                    return self._blocked(proposal, "policy_modified_args 缺失")
                # 用改写后参数重新请求 OPA，期望 allow；不允许再次 modify。
                agent_for_modify = self._identity.get_agent(proposal.agent_id)
                if agent_for_modify is None:
                    self._refund_reservation(reservation)
                    raise CheckpointError(f"unknown agent_id: {proposal.agent_id}")
                profile_for_modify = self._profiles.get(agent_for_modify.profile_id)
                if profile_for_modify is None:
                    self._refund_reservation(reservation)
                    raise CheckpointError(f"unknown profile: {agent_for_modify.profile_id}")
                modified_proposal = proposal.model_copy(update={"arguments": candidate})
                session_risk_for_modify = self._risk_manager.get_profile(
                    session_id or proposal.task_id
                )
                recheck = await self._policy_engine.evaluate(
                    _PACKAGE,
                    build_policy_input(
                        modified_proposal,
                        agent_for_modify,
                        profile_for_modify,
                        session_risk_for_modify,
                    ),
                )
                if recheck.get("verdict") != "allow":
                    self._refund_reservation(reservation)
                    await self._audit_execution_event(
                        "execution_blocked",
                        proposal,
                        None,
                        session_id=session_id,
                        user_id=user_id,
                        tenant_id=tenant_id,
                        reason="改写后参数未通过策略复核",
                    )
                    return self._blocked(proposal, "改写后参数未通过策略复核")
                perm = self._tool_permission_for(proposal)
                if perm is None or not self._args_allowed(perm, candidate):
                    self._refund_reservation(reservation)
                    await self._audit_execution_event(
                        "execution_blocked",
                        proposal,
                        None,
                        session_id=session_id,
                        user_id=user_id,
                        tenant_id=tenant_id,
                        reason="modify 后参数未通过 Profile 白/黑名单复核",
                    )
                    return self._blocked(proposal, "modify 后参数未通过 Profile 白/黑名单复核")
                effective_args = candidate

            # 校验 7-8：最终吊销检查后转发执行；成功才记入 per-task 历史。
            if self._revocation_list is not None:
                agent = self._identity.get_agent(proposal.agent_id)
                if agent is None:
                    self._refund_reservation(reservation)
                    raise CheckpointError(f"unknown agent_id: {proposal.agent_id}")
                identity = AgentIdentity(
                    agent_id=agent.agent_id,
                    user_id=user_id,
                    harness_id=(agent.identity or {}).get("harness_id"),
                    profile_id=agent.profile_id,
                    tenant_id=agent.tenant_id,
                )
                match = self.check_revocation(identity, proposal.tool_name, effective_args)
                if match.revoked:
                    task = Task(
                        task_id=proposal.task_id,
                        session_id=session_id or proposal.task_id,
                        user_id=user_id,
                        agent_id=proposal.agent_id,
                        description="",
                        tenant_id=tenant_id,
                    )
                    return await self.handle_revocation_block(
                        identity=identity,
                        proposal=proposal,
                        task=task,
                        match=match,
                        stage="pre_execute",
                    )

            # 解析执行器并确认工具可达，避免在基础设施临时不可用或策略拒绝时
            # 过早消费 decision。
            try:
                executor = self._executor_registry.resolve_executor(proposal.tool_name)
            except Exception:
                self._refund_reservation(reservation)
                raise
            if executor is None:
                # v0.31.0：执行策略拒绝该工具（deny）。
                self._refund_reservation(reservation)
                await self._audit_execution_event(
                    "execution_blocked",
                    proposal,
                    None,
                    session_id=session_id,
                    user_id=user_id,
                    tenant_id=tenant_id,
                    reason="execution mode denied by policy",
                )
                return ToolResult(
                    call_id=proposal.call_id,
                    task_id=proposal.task_id,
                    tool_name=proposal.tool_name,
                    status="blocked",
                    content="execution mode denied by policy",
                    error_code="execution_mode_denied",
                )

            # 校验 4-5：防重放——原子检查决策是否存在、未过期、未超次数并记账。
            # 必须在确认执行器可达之后消费，确保基础设施/策略失败不会浪费 decision。
            # 运行时假设（v1.1 显式声明，评审#2）：MVP 为单进程 asyncio 事件循环，
            # 同一时刻不存在并行的 forward 调用，因此 use_decision 内部检查+记账原子。
            # 若未来引入多 worker/多进程部署，DecisionStore 必须升级为原子语义（§9.3）。
            if not self._decision_store.use_decision(decision.decision_id, self._now()):
                raise CheckpointError("decision 已过期、已用完或不存在（防重放）")

            authority_cost = self._cost_for(proposal)
            consumed_authority = self._authority_manager.validate_and_consume(proposal, authority_cost)
            if consumed_authority is None:
                raise CheckpointError("Authority token 无效、已过期或预算不足")

            # v0.36.1：Decision 已消费、Authority 已确认，记录执行授权。
            await self._audit_execution_event(
                "execution_authorized",
                proposal,
                None,
                session_id=session_id,
                user_id=user_id,
                tenant_id=tenant_id,
                reason="decision and authority validated before execution",
            )

            try:
                result = await executor.execute(
                    tool_name=proposal.tool_name,
                    arguments=effective_args,
                    context=ExecutionContext(
                        call_id=proposal.call_id,
                        task_id=proposal.task_id,
                        agent_id=proposal.agent_id,
                        user_id=user_id,
                        session_id=session_id,
                        tenant_id=tenant_id,
                    ),
                )
            except Exception:
                await self._audit_authority_consumption(
                    proposal,
                    consumed_authority,
                    session_id=session_id,
                    outcome="uncertain",
                    refunded=False,
                )
                await self._audit_execution_event(
                    "execution_outcome_unknown",
                    proposal,
                    None,
                    session_id=session_id,
                    user_id=user_id,
                    tenant_id=tenant_id,
                    reason="executor raised before returning a result",
                )
                self._refund_reservation(reservation)
                raise
            await self._audit_authority_consumption(
                proposal,
                consumed_authority,
                session_id=session_id,
                outcome="success" if result.status == "success" else "failed",
                refunded=False,
            )
            self._commit_reservation(reservation)
            if result.status == "success":
                await self._audit_execution_event(
                    "execution_completed",
                    proposal,
                    result,
                    session_id=session_id,
                    user_id=user_id,
                    tenant_id=tenant_id,
                    reason="tool executed",
                )
            else:
                await self._audit_execution_event(
                    "execution_failed",
                    proposal,
                    result,
                    session_id=session_id,
                    user_id=user_id,
                    tenant_id=tenant_id,
                    reason="tool execution returned failure",
                )
        except CheckpointError:
            if reservation is not None:
                self._refund_reservation(reservation)
            raise
        if result.status == "success":
            # v0.23.2：modify 后历史应记录实际生效参数，而非原始参数
            history_proposal = proposal
            if decision.verdict == "modify" and effective_args is not None:
                history_proposal = proposal.model_copy(update={"arguments": effective_args})
            self._history.setdefault(proposal.task_id, []).append(history_proposal)
            # v1.2：allow 且风险低时按低风险成功衰减会话风险分
            if (
                session_id is not None
                and decision.verdict == "allow"
                and proposal.risk_level == "low"
            ):
                self._risk_manager.update(session_id, "low_risk_success")
        return result

    def _tool_permission_for(self, proposal: ActionProposal):
        """按 proposal 定位工具的 ToolPermission（modify 复核用）。"""
        agent = self._identity.get_agent(proposal.agent_id)
        if agent is None:
            return None
        profile = self._profiles.get(agent.profile_id)
        if profile is None:
            return None
        return profile.tools.get(proposal.tool_name)

    @staticmethod
    def _args_allowed(perm, args: dict) -> bool:
        """Profile 参数白/黑名单复核（与 §3.3 语义一致：浅层字符串匹配）。"""
        for key, patterns in perm.allowed_args.items():
            if key not in args:
                continue
            value = str(args[key])
            if not any(glob_match(p, value) for p in patterns):
                return False
        for key, patterns in perm.denied_args.items():
            if key not in args:
                continue
            value = str(args[key])
            if any(glob_match(p, value) for p in patterns):
                return False
        return True

    @staticmethod
    def _blocked(proposal: ActionProposal, detail: str) -> ToolResult:
        return ToolResult(
            call_id=proposal.call_id,
            task_id=proposal.task_id,
            tool_name=proposal.tool_name,
            status="blocked",
            content=detail,
            error_code="modify_recheck_failed",
        )
