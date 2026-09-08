"""BudgetReservation 持久化存储。"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from loop_controller.infra.durable_io import DurableIOError, DurableJsonlFile
from loop_controller.models import BudgetReservation

PathLike = str | Path
_TERMINAL_STATES = {"committed", "refunded", "expired"}


class ReservationStoreError(Exception):
    """ReservationStore 损坏或操作失败时抛出（fail-closed）。"""


class ReservationStore(Protocol):
    def save(self, reservation: BudgetReservation) -> None: ...
    def get(self, reservation_id: str) -> BudgetReservation | None: ...
    def get_by_call_id(self, call_id: str) -> BudgetReservation | None: ...
    def list_by_task(self, task_id: str) -> list[BudgetReservation]: ...
    def list_all(self) -> list[BudgetReservation]: ...


class InMemoryReservationStore:
    def __init__(self) -> None:
        self._reservations: dict[str, BudgetReservation] = {}
        self._call_id_index: dict[str, str] = {}
        self._task_index: dict[str, set[str]] = {}

    def save(self, reservation: BudgetReservation) -> None:
        self._reservations[reservation.reservation_id] = reservation
        self._call_id_index[reservation.call_id] = reservation.reservation_id
        self._task_index.setdefault(reservation.task_id, set()).add(reservation.reservation_id)

    def get(self, reservation_id: str) -> BudgetReservation | None:
        return self._reservations.get(reservation_id)

    def get_by_call_id(self, call_id: str) -> BudgetReservation | None:
        reservation_id = self._call_id_index.get(call_id)
        return self._reservations.get(reservation_id) if reservation_id else None

    def list_by_task(self, task_id: str) -> list[BudgetReservation]:
        return [self._reservations[r] for r in self._task_index.get(task_id, set())]

    def list_all(self) -> list[BudgetReservation]:
        return list(self._reservations.values())


@dataclass
class JsonlReservationStore:
    path: PathLike
    _path: Path = field(init=False, repr=False)
    _by_id: dict[str, BudgetReservation] = field(init=False, repr=False, default_factory=dict)
    _by_call_id: dict[str, str] = field(init=False, repr=False, default_factory=dict)
    _by_task: dict[str, set[str]] = field(init=False, repr=False, default_factory=dict)

    def __post_init__(self) -> None:
        self._path = Path(str(self.path))
        self._durable = DurableJsonlFile(self._path)
        self._lock = threading.RLock()
        self._refresh()

    def _refresh_locked(self, transaction) -> None:
        by_id: dict[str, BudgetReservation] = {}
        by_call: dict[str, str] = {}
        by_task: dict[str, set[str]] = {}
        try:
            for raw in transaction.read_complete_lines():
                record = json.loads(raw)
                event_type = record.pop("type", None)
                record.pop("timestamp", None)
                reservation = BudgetReservation.model_validate(record)
                if event_type == "reservation_transitioned" and reservation.reservation_id not in by_id:
                    continue
                if event_type in {"reservation_created", "reservation_transitioned"}:
                    by_id[reservation.reservation_id] = reservation
                    by_call[reservation.call_id] = reservation.reservation_id
                    by_task.setdefault(reservation.task_id, set()).add(reservation.reservation_id)
        except (TypeError, ValueError, json.JSONDecodeError, DurableIOError) as exc:
            raise ReservationStoreError(f"ReservationStore {self._path} 损坏: {exc}") from exc
        self._by_id, self._by_call_id, self._by_task = by_id, by_call, by_task

    def _refresh(self) -> None:
        with self._lock:
            try:
                with self._durable.transaction() as transaction:
                    self._refresh_locked(transaction)
            except DurableIOError as exc:
                raise ReservationStoreError(f"无法读取 ReservationStore {self._path}: {exc}") from exc

    def save(self, reservation: BudgetReservation) -> None:
        with self._lock:
            try:
                with self._durable.transaction() as transaction:
                    transaction.repair_incomplete_tail()
                    self._refresh_locked(transaction)
                    existing = self._by_id.get(reservation.reservation_id)
                    if existing is not None and existing.state in _TERMINAL_STATES:
                        if existing == reservation:
                            return
                        raise ReservationStoreError(
                            f"reservation {reservation.reservation_id} 已处于终态 {existing.state}"
                        )
                    event_type = "reservation_transitioned" if existing else "reservation_created"
                    record = reservation.model_dump(mode="json")
                    record["type"] = event_type
                    if existing:
                        record["timestamp"] = datetime.now(UTC).isoformat()
                    transaction.append_json(record)
                    self._update_indices(reservation)
            except DurableIOError as exc:
                raise ReservationStoreError(f"无法写入 ReservationStore {self._path}: {exc}") from exc

    def get(self, reservation_id: str) -> BudgetReservation | None:
        self._refresh()
        return self._by_id.get(reservation_id)

    def get_by_call_id(self, call_id: str) -> BudgetReservation | None:
        self._refresh()
        reservation_id = self._by_call_id.get(call_id)
        return self._by_id.get(reservation_id) if reservation_id else None

    def list_by_task(self, task_id: str) -> list[BudgetReservation]:
        self._refresh()
        return [self._by_id[r] for r in self._by_task.get(task_id, set())]

    def list_all(self) -> list[BudgetReservation]:
        self._refresh()
        return list(self._by_id.values())

    def _update_indices(self, reservation: BudgetReservation) -> None:
        self._by_id[reservation.reservation_id] = reservation
        self._by_call_id[reservation.call_id] = reservation.reservation_id
        self._by_task.setdefault(reservation.task_id, set()).add(reservation.reservation_id)
