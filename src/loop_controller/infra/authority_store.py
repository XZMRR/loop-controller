"""AuthorityToken 持久化存储。"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from loop_controller.infra.durable_io import DurableIOError, DurableJsonlFile
from loop_controller.models import AuthorityToken, BudgetCost

PathLike = str | Path


class AuthorityStoreError(Exception):
    """AuthorityStore 损坏或操作失败时抛出（fail-closed）。"""


class AuthorityStore(Protocol):
    def save(self, token: AuthorityToken, event_type: str) -> None: ...
    def create_if_capabilities_available(self, token: AuthorityToken, now: datetime) -> bool: ...
    def validate_and_consume(
        self,
        token_id: str,
        cost: BudgetCost,
        now: datetime,
        task_id: str,
        agent_id: str,
    ) -> AuthorityToken | None: ...
    def refund_if_unchanged(
        self, token: AuthorityToken, cost: BudgetCost
    ) -> AuthorityToken | None: ...
    def get(self, token_id: str) -> AuthorityToken | None: ...
    def list_all(self) -> list[AuthorityToken]: ...
    def list_active(self) -> list[AuthorityToken]: ...
    def list_by_task(self, task_id: str) -> list[AuthorityToken]: ...


class InMemoryAuthorityStore:
    def __init__(self) -> None:
        self._tokens: dict[str, AuthorityToken] = {}
        self._task_index: dict[str, set[str]] = {}

    def save(self, token: AuthorityToken, event_type: str) -> None:
        self._tokens[token.token_id] = token
        self._task_index.setdefault(token.task_id, set()).add(token.token_id)

    def create_if_capabilities_available(self, token: AuthorityToken, now: datetime) -> bool:
        requested = set(token.granted_capabilities)
        for existing in self.list_by_task(token.task_id):
            if _is_active(existing, now) and requested.intersection(existing.granted_capabilities):
                return False
        self.save(token, "token_created")
        return True

    def validate_and_consume(
        self,
        token_id: str,
        cost: BudgetCost,
        now: datetime,
        task_id: str,
        agent_id: str,
    ) -> AuthorityToken | None:
        token = self._tokens.get(token_id)
        if (
            token is None
            or not _is_active(token, now)
            or token.task_id != task_id
            or token.agent_id != agent_id
        ):
            return None
        remaining = token.remaining_budget.token_count - cost.token_count
        if remaining < 0:
            return None
        updated = token.model_copy(
            update={"remaining_budget": BudgetCost(token_count=remaining)}
        )
        self.save(updated, "token_used")
        return updated

    def refund_if_unchanged(
        self, token: AuthorityToken, cost: BudgetCost
    ) -> AuthorityToken | None:
        current = self._tokens.get(token.token_id)
        if current != token:
            return None
        updated = current.model_copy(update={
            "remaining_budget": BudgetCost(
                token_count=current.remaining_budget.token_count + cost.token_count
            )
        })
        self.save(updated, "token_refunded")
        return updated

    def get(self, token_id: str) -> AuthorityToken | None:
        return self._tokens.get(token_id)

    def list_all(self) -> list[AuthorityToken]:
        return list(self._tokens.values())

    def list_active(self) -> list[AuthorityToken]:
        now = datetime.now(UTC)
        return [t for t in self._tokens.values() if _is_active(t, now)]

    def list_by_task(self, task_id: str) -> list[AuthorityToken]:
        return [self._tokens[tid] for tid in self._task_index.get(task_id, set())]


@dataclass
class JsonlAuthorityStore:
    path: PathLike
    _path: Path = field(init=False, repr=False)
    _by_id: dict[str, AuthorityToken] = field(init=False, repr=False, default_factory=dict)
    _by_task: dict[str, set[str]] = field(init=False, repr=False, default_factory=dict)

    def __post_init__(self) -> None:
        self._path = Path(str(self.path))
        self._durable = DurableJsonlFile(self._path)
        self._lock = threading.RLock()
        self._refresh()

    def _refresh_locked(self, transaction) -> None:
        by_id: dict[str, AuthorityToken] = {}
        by_task: dict[str, set[str]] = {}
        try:
            for raw in transaction.read_complete_lines():
                record = json.loads(raw)
                event_type = record.pop("type", None)
                record.pop("timestamp", None)
                if event_type not in {
                    "token_created",
                    "token_used",
                    "token_refunded",
                    "token_revoked",
                    "token_expired",
                }:
                    continue
                token = AuthorityToken.model_validate(record)
                by_id[token.token_id] = token
                by_task.setdefault(token.task_id, set()).add(token.token_id)
        except (TypeError, ValueError, json.JSONDecodeError, DurableIOError) as exc:
            raise AuthorityStoreError(f"AuthorityStore {self._path} 损坏: {exc}") from exc
        self._by_id, self._by_task = by_id, by_task

    def _refresh(self) -> None:
        with self._lock:
            try:
                with self._durable.transaction() as transaction:
                    self._refresh_locked(transaction)
            except DurableIOError as exc:
                raise AuthorityStoreError(f"无法读取 AuthorityStore {self._path}: {exc}") from exc

    def save(self, token: AuthorityToken, event_type: str) -> None:
        with self._lock:
            try:
                with self._durable.transaction() as transaction:
                    transaction.repair_incomplete_tail()
                    self._refresh_locked(transaction)
                    existing = self._by_id.get(token.token_id)
                    if event_type == "token_created" and existing is not None:
                        if existing == token:
                            return
                        raise AuthorityStoreError(f"token {token.token_id} 已存在")
                    if event_type != "token_created" and existing is None:
                        raise AuthorityStoreError(f"token {token.token_id} 不存在")
                    if existing is not None and existing.revoked_at is not None and existing != token:
                        raise AuthorityStoreError(f"token {token.token_id} 已撤销")
                    if event_type == "token_used" and existing is not None:
                        if token.remaining_budget.token_count >= existing.remaining_budget.token_count:
                            raise AuthorityStoreError(f"token {token.token_id} 消费状态冲突")
                    if event_type == "token_refunded" and existing is not None:
                        if (
                            token.remaining_budget.token_count
                            <= existing.remaining_budget.token_count
                            or token.remaining_budget.token_count > token.budget.token_count
                        ):
                            raise AuthorityStoreError(f"token {token.token_id} 返还状态冲突")
                    record = token.model_dump(mode="json")
                    record["type"] = event_type
                    record["timestamp"] = datetime.now(UTC).isoformat()
                    transaction.append_json(record)
                    self._update_indices(token)
            except DurableIOError as exc:
                raise AuthorityStoreError(f"无法写入 AuthorityStore {self._path}: {exc}") from exc

    def create_if_capabilities_available(self, token: AuthorityToken, now: datetime) -> bool:
        with self._lock:
            try:
                with self._durable.transaction() as transaction:
                    transaction.repair_incomplete_tail()
                    self._refresh_locked(transaction)
                    requested = set(token.granted_capabilities)
                    for token_id in self._by_task.get(token.task_id, set()):
                        existing = self._by_id[token_id]
                        if _is_active(existing, now) and requested.intersection(
                            existing.granted_capabilities
                        ):
                            return False
                    record = token.model_dump(mode="json")
                    record["type"] = "token_created"
                    record["timestamp"] = datetime.now(UTC).isoformat()
                    transaction.append_json(record)
                    self._update_indices(token)
                    return True
            except DurableIOError as exc:
                raise AuthorityStoreError(f"无法写入 AuthorityStore {self._path}: {exc}") from exc

    def validate_and_consume(
        self,
        token_id: str,
        cost: BudgetCost,
        now: datetime,
        task_id: str,
        agent_id: str,
    ) -> AuthorityToken | None:
        with self._lock:
            try:
                with self._durable.transaction() as transaction:
                    transaction.repair_incomplete_tail()
                    self._refresh_locked(transaction)
                    token = self._by_id.get(token_id)
                    if (
                        token is None
                        or not _is_active(token, now)
                        or token.task_id != task_id
                        or token.agent_id != agent_id
                    ):
                        return None
                    remaining = token.remaining_budget.token_count - cost.token_count
                    if remaining < 0:
                        return None
                    updated = token.model_copy(update={
                        "remaining_budget": BudgetCost(token_count=remaining)
                    })
                    record = updated.model_dump(mode="json")
                    record["type"] = "token_used"
                    record["timestamp"] = datetime.now(UTC).isoformat()
                    transaction.append_json(record)
                    self._update_indices(updated)
                    return updated
            except DurableIOError as exc:
                raise AuthorityStoreError(f"无法写入 AuthorityStore {self._path}: {exc}") from exc

    def refund_if_unchanged(
        self, token: AuthorityToken, cost: BudgetCost
    ) -> AuthorityToken | None:
        with self._lock:
            try:
                with self._durable.transaction() as transaction:
                    transaction.repair_incomplete_tail()
                    self._refresh_locked(transaction)
                    current = self._by_id.get(token.token_id)
                    if current != token:
                        return None
                    remaining = current.remaining_budget.token_count + cost.token_count
                    if remaining > current.budget.token_count:
                        return None
                    updated = current.model_copy(update={
                        "remaining_budget": BudgetCost(token_count=remaining)
                    })
                    record = updated.model_dump(mode="json")
                    record["type"] = "token_refunded"
                    record["timestamp"] = datetime.now(UTC).isoformat()
                    transaction.append_json(record)
                    self._update_indices(updated)
                    return updated
            except DurableIOError as exc:
                raise AuthorityStoreError(f"无法写入 AuthorityStore {self._path}: {exc}") from exc

    def get(self, token_id: str) -> AuthorityToken | None:
        self._refresh()
        return self._by_id.get(token_id)

    def list_all(self) -> list[AuthorityToken]:
        self._refresh()
        return list(self._by_id.values())

    def list_active(self) -> list[AuthorityToken]:
        self._refresh()
        now = datetime.now(UTC)
        return [t for t in self._by_id.values() if _is_active(t, now)]

    def list_by_task(self, task_id: str) -> list[AuthorityToken]:
        self._refresh()
        return [self._by_id[tid] for tid in self._by_task.get(task_id, set())]

    def _update_indices(self, token: AuthorityToken) -> None:
        self._by_id[token.token_id] = token
        self._by_task.setdefault(token.task_id, set()).add(token.token_id)


def _is_active(token: AuthorityToken, now: datetime) -> bool:
    return token.revoked_at is None and now < token.expires_at
