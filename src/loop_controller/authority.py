"""Earned Authority Manager（v0.11.0）：动态权限提升。

在静态 CapabilityProfile 天花板之上，按条件、预算、时间窗口授予临时能力（AuthorityToken），
让 Agent 在受控场景下获得阶段性更高权限。Rego 保留最终裁决权。
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from loop_controller.infra.authority_store import AuthorityStore, InMemoryAuthorityStore
from loop_controller.models import (
    ActionProposal,
    AuthorityEvaluationContext,
    AuthorityGrantRule,
    AuthorityRequest,
    AuthorityRules,
    AuthorityToken,
    BudgetCost,
)

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class DenyReason:
    """权限提升申请被拒绝的原因。"""

    reason: str


@runtime_checkable
class AuthorityManager(Protocol):
    """动态权限管理器接口。"""

    def request_authority(
        self,
        request: AuthorityRequest,
        context: AuthorityEvaluationContext,
    ) -> AuthorityToken | DenyReason: ...

    def validate_for_proposal(
        self,
        proposal: ActionProposal,
        required_capabilities: list[str],
    ) -> list[AuthorityToken]: ...

    def consume(self, token_id: str, cost: BudgetCost) -> AuthorityToken | None: ...

    def validate_and_consume(
        self, proposal: ActionProposal, cost: BudgetCost
    ) -> list[AuthorityToken] | None: ...

    def refund_consumed(self, tokens: list[AuthorityToken], cost: BudgetCost) -> None: ...

    def revoke_token(self, token_id: str, reason: str) -> bool: ...

    def revoke_expired_tokens(self, now: datetime | None = None) -> list[str]: ...


class NoopAuthorityManager:
    """恒拒绝的动态权限占位（迭代 1；v0.11.0 替换为真实现）。"""

    def request_authority(
        self,
        request: AuthorityRequest,
        context: AuthorityEvaluationContext,
    ) -> AuthorityToken | DenyReason:
        return DenyReason("authority manager disabled")

    def validate_for_proposal(
        self,
        proposal: ActionProposal,
        required_capabilities: list[str],
    ) -> list[AuthorityToken]:
        return []

    def consume(self, token_id: str, cost: BudgetCost) -> AuthorityToken | None:
        return None

    def validate_and_consume(
        self, proposal: ActionProposal, cost: BudgetCost
    ) -> list[AuthorityToken] | None:
        return [] if not proposal.authority_token_ids else None

    def refund_consumed(self, tokens: list[AuthorityToken], cost: BudgetCost) -> None:
        return None

    def revoke_token(self, token_id: str, reason: str) -> bool:
        return False

    def revoke_expired_tokens(self, now: datetime | None = None) -> list[str]:
        return []


class EarnedAuthorityManager:
    """基于条件声明式的动态权限提升管理器。"""

    def __init__(
        self,
        rules: AuthorityRules,
        store: AuthorityStore | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._rules = rules
        self._store = store or InMemoryAuthorityStore()
        self._now = now or _utc_now

    def request_authority(
        self,
        request: AuthorityRequest,
        context: AuthorityEvaluationContext,
    ) -> AuthorityToken | DenyReason:
        """评估并签发 AuthorityToken；不满足条件时返回 DenyReason。"""
        if not self._rules.enabled:
            return DenyReason("authority manager disabled")

        if not request.requested_capabilities:
            return DenyReason("no capability requested")

        # 每个请求的能力必须有对应规则
        for capability in request.requested_capabilities:
            if capability not in self._rules.grants:
                return DenyReason(f"capability {capability!r} has no grant rule")

        # 评估每个能力的条件（全部能力都必须满足各自条件）
        for capability in request.requested_capabilities:
            rule = self._rules.grants[capability]
            result = self._evaluate_conditions(rule, request, context)
            if isinstance(result, DenyReason):
                return result

        # 生成 token
        now = self._now()
        max_duration = min(
            self._rules.grants[c].max_duration_seconds for c in request.requested_capabilities
        )
        budget = BudgetCost(
            token_count=sum(
                self._rules.grants[c].budget_limit.token_count
                for c in request.requested_capabilities
            )
        )
        token = AuthorityToken(
            token_id=uuid.uuid4().hex,
            request_id=request.request_id,
            agent_id=request.agent_id,
            task_id=request.task_id,
            granted_capabilities=list(request.requested_capabilities),
            budget=budget,
            remaining_budget=budget,
            expires_at=now + timedelta(seconds=max_duration),
            created_at=now,
            revoked_at=None,
            audit_record_id=uuid.uuid4().hex,
        )
        if not self._store.create_if_capabilities_available(token, now):
            duplicated = request.requested_capabilities[0]
            return DenyReason(f"capability {duplicated!r} already granted for task")
        logger.info(
            "Granted authority token %s for capabilities %s to task %s",
            token.token_id,
            token.granted_capabilities,
            token.task_id,
        )
        return token

    def _evaluate_conditions(
        self,
        rule: AuthorityGrantRule,
        request: AuthorityRequest,
        context: AuthorityEvaluationContext,
    ) -> None | DenyReason:
        cond = rule.conditions
        if cond.user_confirmation and not request.user_confirmation:
            return DenyReason(
                f"capability {rule.capability!r} requires user_confirmation"
            )
        if cond.budget_remaining is not None:
            if context.task_budget_remaining < cond.budget_remaining:
                return DenyReason(
                    f"capability {rule.capability!r} requires budget_remaining >= {cond.budget_remaining}"
                )
        if cond.no_recent_denials_within_steps is not None:
            # 简化语义：只要近期（在窗口范围内）存在拒绝记录，就不授予。
            # context.recent_denial_count 由调用方根据最近 N 步统计。
            if context.recent_denial_count > 0:
                return DenyReason(
                    f"capability {rule.capability!r} requires no recent denials"
                )
        if cond.require_task_context_regex:
            pattern = re.compile(cond.require_task_context_regex)
            if not pattern.search(context.task_context):
                return DenyReason(
                    f"capability {rule.capability!r} requires task_context to match {cond.require_task_context_regex!r}"
                )
        return None

    def validate_for_proposal(
        self,
        proposal: ActionProposal,
        required_capabilities: list[str],
    ) -> list[AuthorityToken]:
        """验证 proposal 携带的 token 是否能覆盖 required_capabilities。"""
        if not required_capabilities or not proposal.authority_token_ids:
            return []
        now = self._now()
        valid: list[AuthorityToken] = []
        covered: set[str] = set()
        for token_id in proposal.authority_token_ids:
            token = self._store.get(token_id)
            if token is None:
                continue
            if not _is_active(token, now):
                continue
            if token.task_id != proposal.task_id:
                continue
            if token.agent_id != proposal.agent_id:
                continue
            valid.append(token)
            covered.update(token.granted_capabilities)
        if not required_capabilities or not covered.issuperset(required_capabilities):
            return []
        return valid

    def consume(self, token_id: str, cost: BudgetCost) -> AuthorityToken | None:
        """消费 token 预算；余额不足或 token 无效时返回 None。"""
        token = self._store.get(token_id)
        if token is None:
            return None
        return self._store.validate_and_consume(
            token_id, cost, self._now(), token.task_id, token.agent_id
        )

    def validate_and_consume(
        self, proposal: ActionProposal, cost: BudgetCost
    ) -> list[AuthorityToken] | None:
        """执行前逐个原子校验并消费；任一失败则安全返还已消费 token。"""
        consumed: list[AuthorityToken] = []
        for token_id in proposal.authority_token_ids:
            token = self._store.validate_and_consume(
                token_id, cost, self._now(), proposal.task_id, proposal.agent_id
            )
            if token is None:
                self.refund_consumed(consumed, cost)
                return None
            consumed.append(token)
        return consumed

    def refund_consumed(self, tokens: list[AuthorityToken], cost: BudgetCost) -> None:
        """仅当 token 未被后续修改时返还本次消费，避免覆盖其他 worker 状态。"""
        for token in tokens:
            self._store.refund_if_unchanged(token, cost)

    def revoke_token(self, token_id: str, reason: str) -> bool:
        """撤销指定 token。"""
        token = self._store.get(token_id)
        if token is None:
            return False
        if token.revoked_at is not None:
            return False
        updated = token.model_copy(update={"revoked_at": self._now()})
        self._store.save(updated, "token_revoked")
        logger.info("Revoked authority token %s: %s", token_id, reason)
        return True

    def revoke_expired_tokens(self, now: datetime | None = None) -> list[str]:
        """将已过期的 token 标记为 expired；返回被处理的 token_id 列表。"""
        now = now or self._now()
        expired: list[str] = []
        for token in self._store.list_all():
            if token.revoked_at is None and now >= token.expires_at:
                updated = token.model_copy(update={"revoked_at": token.expires_at})
                self._store.save(updated, "token_expired")
                expired.append(token.token_id)
        return expired


def _is_active(token: AuthorityToken, now: datetime) -> bool:
    """token 未撤销且未过期。"""
    if token.revoked_at is not None:
        return False
    return now < token.expires_at
