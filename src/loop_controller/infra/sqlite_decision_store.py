"""基于 SQLite 的 DecisionStore 实现（v0.34.0）。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from loop_controller.checkpoint import DecisionStore
from loop_controller.infra.decision_store import DecisionStoreError
from loop_controller.infra.state_db import DecisionRecord, StateDatabase, StateDatabaseError
from loop_controller.models import Decision


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _decision_to_record(decision: Decision) -> DecisionRecord:
    return DecisionRecord(
        decision_id=decision.decision_id,
        call_id=decision.call_id,
        task_id=decision.task_id,
        verdict=decision.verdict,
        reason=decision.reason,
        modified_args=decision.policy_modified_args or decision.modified_args,
        original_args=decision.original_args,
        policy_modified_args=decision.policy_modified_args or decision.modified_args,
        effective_args=decision.effective_args,
        escalation_target=decision.escalation_target,
        policy_hits=list(decision.policy_hits),
        policy_version=decision.policy_version,
        profile_version=decision.profile_version,
        expires_at=decision.expires_at,
        max_uses=decision.max_uses,
        finalized=False,
        used_count=0,
        created_at=_utc_now(),
    )


def _record_to_decision(record: DecisionRecord) -> Decision:
    return Decision(
        decision_id=record.decision_id,
        call_id=record.call_id,
        task_id=record.task_id,
        verdict=record.verdict,  # type: ignore[arg-type]
        reason=record.reason,
        modified_args=record.policy_modified_args or record.modified_args,
        original_args=record.original_args,
        policy_modified_args=record.policy_modified_args,
        effective_args=record.effective_args,
        escalation_target=record.escalation_target,
        policy_hits=record.policy_hits,
        policy_version=record.policy_version,
        profile_version=record.profile_version,
        expires_at=record.expires_at,
        max_uses=record.max_uses,
    )


class SqliteDecisionStore(DecisionStore):
    """基于 ``StateDatabase`` 的 DecisionStore。

    提供 O(1) 的 ``is_call_id_seen``、``get_decision``、``use_decision``
    与 ``is_decision_finalized``，并通过 SQLite 事务保证多进程并发安全。
    """

    def __init__(self, db: StateDatabase) -> None:
        self._db = db

    @classmethod
    def from_path(cls, path: str | Path) -> SqliteDecisionStore:
        """便捷构造：从数据库路径直接创建。"""
        return cls(StateDatabase(path))

    def is_call_id_seen(self, call_id: str) -> bool:
        try:
            return self._db.is_call_id_seen(call_id)
        except StateDatabaseError as exc:
            raise DecisionStoreError(str(exc)) from exc

    def record_proposal(self, task_id: str, call_id: str) -> None:
        try:
            self._db.record_proposal(call_id, task_id)
        except StateDatabaseError as exc:
            raise DecisionStoreError(str(exc)) from exc

    def record_decision(self, decision: Decision) -> None:
        try:
            self._db.record_decision(_decision_to_record(decision))
        except StateDatabaseError as exc:
            raise DecisionStoreError(str(exc)) from exc

    def get_decision(self, decision_id: str) -> Decision | None:
        try:
            record = self._db.get_decision(decision_id)
            return _record_to_decision(record) if record else None
        except StateDatabaseError as exc:
            raise DecisionStoreError(str(exc)) from exc

    def use_decision(self, decision_id: str, now: datetime) -> bool:
        try:
            return self._db.use_decision(decision_id, now)
        except StateDatabaseError as exc:
            raise DecisionStoreError(str(exc)) from exc

    def record_finalized(self, decision_id: str) -> None:
        try:
            self._db.record_finalized(decision_id)
        except StateDatabaseError as exc:
            raise DecisionStoreError(str(exc)) from exc

    def is_decision_finalized(self, decision_id: str) -> bool:
        try:
            return self._db.is_decision_finalized(decision_id)
        except StateDatabaseError as exc:
            raise DecisionStoreError(str(exc)) from exc
