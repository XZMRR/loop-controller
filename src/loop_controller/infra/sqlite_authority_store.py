"""基于 SQLite 的 AuthorityStore 实现（v0.43.0）。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from loop_controller.infra.authority_store import AuthorityStoreError
from loop_controller.infra.state_db import StateDatabase, StateDatabaseError
from loop_controller.models import AuthorityToken, BudgetCost


class SqliteAuthorityStore:
    """基于 ``StateDatabase`` 的 AuthorityStore。

    ``authority_tokens`` 保存授予能力、预算上下限与剩余值；``validate_and_consume``
    与 ``refund_if_unchanged`` 通过单条 ``UPDATE`` 原子 CAS，保证多进程并发安全。
    """

    def __init__(self, db: StateDatabase) -> None:
        self._db = db

    @classmethod
    def from_path(cls, path: str | Path) -> SqliteAuthorityStore:
        """便捷构造：从数据库路径直接创建。"""
        return cls(StateDatabase(path))

    def save(self, token: AuthorityToken, event_type: str) -> None:
        try:
            self._db.save_authority_token(token.model_dump(mode="json"), event_type)
        except StateDatabaseError as exc:
            raise AuthorityStoreError(str(exc)) from exc

    def create_if_capabilities_available(self, token: AuthorityToken, now: datetime) -> bool:
        try:
            return self._db.create_authority_token_if_available(
                token.model_dump(mode="json"), now
            )
        except StateDatabaseError as exc:
            raise AuthorityStoreError(str(exc)) from exc

    def validate_and_consume(
        self,
        token_id: str,
        cost: BudgetCost,
        now: datetime,
        task_id: str,
        agent_id: str,
    ) -> AuthorityToken | None:
        try:
            row = self._db.consume_authority_token(
                token_id, cost.token_count, now, task_id, agent_id
            )
        except StateDatabaseError as exc:
            raise AuthorityStoreError(str(exc)) from exc
        return AuthorityToken.model_validate(row) if row else None

    def refund_if_unchanged(
        self, token: AuthorityToken, cost: BudgetCost
    ) -> AuthorityToken | None:
        try:
            current = self._db.get_authority_token(token.token_id)
        except StateDatabaseError as exc:
            raise AuthorityStoreError(str(exc)) from exc
        if current is None or current != token.model_dump(mode="json"):
            return None
        remaining = current["remaining_budget"]["token_count"] + cost.token_count
        if remaining > current["budget"]["token_count"]:
            return None
        try:
            row = self._db.refund_authority_token(
                token.token_id, token.remaining_budget.token_count, cost.token_count
            )
        except StateDatabaseError as exc:
            raise AuthorityStoreError(str(exc)) from exc
        return AuthorityToken.model_validate(row) if row else None

    def get(self, token_id: str) -> AuthorityToken | None:
        try:
            row = self._db.get_authority_token(token_id)
        except StateDatabaseError as exc:
            raise AuthorityStoreError(str(exc)) from exc
        return AuthorityToken.model_validate(row) if row else None

    def list_all(self) -> list[AuthorityToken]:
        try:
            rows = self._db.list_all_authority_tokens()
        except StateDatabaseError as exc:
            raise AuthorityStoreError(str(exc)) from exc
        return [AuthorityToken.model_validate(row) for row in rows]

    def list_active(self) -> list[AuthorityToken]:
        try:
            rows = self._db.list_active_authority_tokens(datetime.now(UTC))
        except StateDatabaseError as exc:
            raise AuthorityStoreError(str(exc)) from exc
        return [AuthorityToken.model_validate(row) for row in rows]

    def list_by_task(self, task_id: str) -> list[AuthorityToken]:
        try:
            rows = self._db.list_authority_tokens_by_task(task_id)
        except StateDatabaseError as exc:
            raise AuthorityStoreError(str(exc)) from exc
        return [AuthorityToken.model_validate(row) for row in rows]
