"""BudgetReservation 持久化存储（v0.6.1 / v0.8.0）。

``InMemoryReservationStore`` 为默认实现，适合测试与单进程内存场景；
``JsonlReservationStore`` 基于 append-only JSONL，支持 Runtime/Proxy 重启后恢复。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from loop_controller.models import BudgetReservation

logger = logging.getLogger(__name__)

PathLike = str | Path


class ReservationStoreError(Exception):
    """ReservationStore 损坏或操作失败时抛出（fail-closed）。"""


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


@dataclass
class JsonlReservationStore:
    """基于 JSONL 的 BudgetReservation 持久化存储。

    事件类型：
    - ``reservation_created``：创建 reservation；
    - ``reservation_transitioned``：状态流转（state / expires_at 变更）。

    启动时重放所有事件恢复内存索引。
    """

    path: PathLike
    _path: Path = field(init=False, repr=False)
    _by_id: dict[str, BudgetReservation] = field(init=False, repr=False, default_factory=dict)
    _by_call_id: dict[str, str] = field(init=False, repr=False, default_factory=dict)
    _by_task: dict[str, set[str]] = field(init=False, repr=False, default_factory=dict)

    def __post_init__(self) -> None:
        self._path = Path(str(self.path))
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._replay()

    def save(self, reservation: BudgetReservation) -> None:
        """持久化 reservation。"""
        # 根据状态决定事件类型：已存在则 transition，否则 created
        event_type = (
            "reservation_transitioned"
            if reservation.reservation_id in self._by_id
            else "reservation_created"
        )
        record = reservation.model_dump(mode="json")
        record["type"] = event_type
        if event_type == "reservation_transitioned":
            record["timestamp"] = datetime.now(UTC).isoformat()
        self._append(record)
        self._update_indices(reservation)

    def get(self, reservation_id: str) -> BudgetReservation | None:
        """按 reservation_id 读取。"""
        return self._by_id.get(reservation_id)

    def get_by_call_id(self, call_id: str) -> BudgetReservation | None:
        """按 call_id 读取最新 reservation。"""
        reservation_id = self._by_call_id.get(call_id)
        if reservation_id is None:
            return None
        return self._by_id.get(reservation_id)

    def list_by_task(self, task_id: str) -> list[BudgetReservation]:
        """按 task_id 列出所有 reservation。"""
        reservation_ids = self._by_task.get(task_id, set())
        return [self._by_id[r] for r in reservation_ids if r in self._by_id]

    def _append(self, record: dict) -> None:
        try:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                fh.flush()
        except OSError as exc:
            raise ReservationStoreError(f"无法写入 ReservationStore {self._path}: {exc}") from exc

    def _update_indices(self, reservation: BudgetReservation) -> None:
        self._by_id[reservation.reservation_id] = reservation
        self._by_call_id[reservation.call_id] = reservation.reservation_id
        self._by_task.setdefault(reservation.task_id, set()).add(reservation.reservation_id)

    def _replay(self) -> None:
        """启动时重放事件恢复状态。"""
        if not self._path.exists():
            return
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise ReservationStoreError(f"无法读取 ReservationStore {self._path}: {exc}") from exc

        for lineno, line in enumerate(lines, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ReservationStoreError(
                    f"ReservationStore {self._path} 第 {lineno} 行非法 JSON: {exc}"
                ) from exc

            event_type = record.get("type")
            record.pop("type", None)
            record.pop("timestamp", None)

            if event_type == "reservation_created":
                reservation = BudgetReservation.model_validate(record)
                self._update_indices(reservation)
            elif event_type == "reservation_transitioned":
                reservation_id = record.get("reservation_id")
                existing = self._by_id.get(reservation_id)
                if existing is None:
                    continue
                update: dict = {"state": record.get("state", existing.state)}
                expires_at = record.get("expires_at")
                if expires_at is not None:
                    update["expires_at"] = expires_at
                reservation = existing.model_copy(update=update)
                self._update_indices(reservation)
