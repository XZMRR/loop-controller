"""BudgetReservation 持久化存储（v0.6.1）。

``InMemoryReservationStore`` 为默认实现，适合测试与单进程场景；
生产环境可替换为 ``JsonlReservationStore``（P1）。
"""

from __future__ import annotations

from typing import Protocol

from loop_controller.models import BudgetReservation


class ReservationStore(Protocol):
    """BudgetReservation 持久化存储协议。"""

    def save(self, reservation: BudgetReservation) -> None: ...
    def get(self, reservation_id: str) -> BudgetReservation | None: ...
    def get_by_call_id(self, call_id: str) -> BudgetReservation | None: ...
    def list_by_task(self, task_id: str) -> list[BudgetReservation]: ...


class InMemoryReservationStore:
    """内存版 ReservationStore；进程重启丢失。"""

    def __init__(self) -> None:
        self._reservations: dict[str, BudgetReservation] = {}
        # call_id -> reservation_id 索引
        self._call_id_index: dict[str, str] = {}
        # task_id -> reservation_ids 索引
        self._task_index: dict[str, set[str]] = {}

    def save(self, reservation: BudgetReservation) -> None:
        self._reservations[reservation.reservation_id] = reservation
        self._call_id_index[reservation.call_id] = reservation.reservation_id
        self._task_index.setdefault(reservation.task_id, set()).add(reservation.reservation_id)

    def get(self, reservation_id: str) -> BudgetReservation | None:
        return self._reservations.get(reservation_id)

    def get_by_call_id(self, call_id: str) -> BudgetReservation | None:
        reservation_id = self._call_id_index.get(call_id)
        if reservation_id is None:
            return None
        return self._reservations.get(reservation_id)

    def list_by_task(self, task_id: str) -> list[BudgetReservation]:
        reservation_ids = self._task_index.get(task_id, set())
        return [self._reservations[r] for r in reservation_ids if r in self._reservations]
