"""基于 SQLite 的 BudgetReservation 存储实现（v0.43.0）。"""

from __future__ import annotations

from pathlib import Path

from loop_controller.infra.reservation_store import ReservationStoreError
from loop_controller.infra.state_db import StateDatabase, StateDatabaseError
from loop_controller.models import BudgetReservation


class SqliteReservationStore:
    """基于 ``StateDatabase`` 的 ReservationStore。

    ``budget_reservations`` 以 ``reservation_id`` 为主键物化状态机，
    终态（committed/refunded/expired）后拒绝被不同对象覆盖，对齐 JSONL 语义。
    """

    def __init__(self, db: StateDatabase) -> None:
        self._db = db

    @classmethod
    def from_path(cls, path: str | Path) -> SqliteReservationStore:
        """便捷构造：从数据库路径直接创建。"""
        return cls(StateDatabase(path))

    def save(self, reservation: BudgetReservation) -> None:
        try:
            self._db.save_reservation(reservation.model_dump(mode="json"))
        except StateDatabaseError as exc:
            raise ReservationStoreError(str(exc)) from exc

    def get(self, reservation_id: str) -> BudgetReservation | None:
        try:
            row = self._db.get_reservation(reservation_id)
        except StateDatabaseError as exc:
            raise ReservationStoreError(str(exc)) from exc
        return BudgetReservation.model_validate(row) if row else None

    def get_by_call_id(self, call_id: str) -> BudgetReservation | None:
        try:
            row = self._db.get_reservation_by_call_id(call_id)
        except StateDatabaseError as exc:
            raise ReservationStoreError(str(exc)) from exc
        return BudgetReservation.model_validate(row) if row else None

    def list_by_task(self, task_id: str) -> list[BudgetReservation]:
        try:
            rows = self._db.list_reservations_by_task(task_id)
        except StateDatabaseError as exc:
            raise ReservationStoreError(str(exc)) from exc
        return [BudgetReservation.model_validate(row) for row in rows]

    def list_all(self) -> list[BudgetReservation]:
        try:
            rows = self._db.list_all_reservations()
        except StateDatabaseError as exc:
            raise ReservationStoreError(str(exc)) from exc
        return [BudgetReservation.model_validate(row) for row in rows]
