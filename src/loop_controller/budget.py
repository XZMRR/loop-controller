"""预算记账。"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from loop_controller.infra.alert_store import AlertStore
from loop_controller.infra.durable_io import DurableIOError, DurableJsonlFile
from loop_controller.models import AuditAlert, BudgetCost

logger = logging.getLogger(__name__)
PathLike = str | Path


class BudgetLedgerError(Exception):
    """预算账本损坏或操作失败时抛出（fail-closed）。"""


@runtime_checkable
class BudgetLedger(Protocol):
    def set_budget(self, task_id: str, max_budget_token: int) -> None: ...
    def check_and_reserve(self, task_id: str, cost: BudgetCost) -> bool: ...
    def commit(self, task_id: str, cost: BudgetCost) -> None: ...
    def refund(self, task_id: str, cost: BudgetCost) -> None: ...


class InMemoryBudgetLedger:
    def __init__(self, default_max_budget_token: int = 1_000_000) -> None:
        self._default_max = default_max_budget_token
        self._max: dict[str, int] = {}
        self._reserved: dict[str, int] = defaultdict(int)
        self._committed: dict[str, int] = defaultdict(int)

    def set_budget(self, task_id: str, max_budget_token: int) -> None:
        self._max[task_id] = max_budget_token

    def check_and_reserve(self, task_id: str, cost: BudgetCost) -> bool:
        maximum = self._max.get(task_id, self._default_max)
        if self._committed[task_id] + self._reserved[task_id] + cost.token_count > maximum:
            return False
        self._reserved[task_id] += cost.token_count
        return True

    def commit(self, task_id: str, cost: BudgetCost) -> None:
        self._reserved[task_id] -= cost.token_count
        self._committed[task_id] += cost.token_count

    def refund(self, task_id: str, cost: BudgetCost) -> None:
        self._reserved[task_id] = max(0, self._reserved[task_id] - cost.token_count)


@dataclass
class JsonlBudgetLedger:
    path: PathLike
    default_max_budget_token: int = 1_000_000
    alert_store: AlertStore | None = None
    _path: Path = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._path = Path(str(self.path))
        self._durable = DurableJsonlFile(self._path)
        self._lock = threading.RLock()
        self._alert_store = self.alert_store
        self._max: dict[str, int] = {}
        self._reserved: dict[str, int] = defaultdict(int)
        self._committed: dict[str, int] = defaultdict(int)
        self._write_blocked = False
        self._refresh(emit_alerts=True)

    def _refresh_locked(self, transaction) -> None:
        maximum: dict[str, int] = {}
        reserved: dict[str, int] = defaultdict(int)
        committed: dict[str, int] = defaultdict(int)
        try:
            for raw in transaction.read_complete_lines():
                record = json.loads(raw)
                event_type = record.get("type")
                task_id = record.get("task_id", "")
                token_count = int(record.get("token_count", 0))
                if event_type == "set_budget" and isinstance(record.get("max_budget_token"), int):
                    maximum[task_id] = record["max_budget_token"]
                elif event_type == "reserve":
                    reserved[task_id] += token_count
                elif event_type == "commit":
                    reserved[task_id] -= token_count
                    committed[task_id] += token_count
                elif event_type == "refund":
                    reserved[task_id] = max(0, reserved[task_id] - token_count)
        except (TypeError, ValueError, json.JSONDecodeError, DurableIOError) as exc:
            raise BudgetLedgerError(f"BudgetLedger {self._path} 损坏: {exc}") from exc
        self._max, self._reserved, self._committed = maximum, reserved, committed

    def _refresh(self, *, emit_alerts: bool = False) -> None:
        with self._lock:
            try:
                with self._durable.transaction() as transaction:
                    self._refresh_locked(transaction)
            except DurableIOError as exc:
                raise BudgetLedgerError(f"无法读取 BudgetLedger {self._path}: {exc}") from exc
            if emit_alerts and self._alert_store is not None:
                for task_id, reserved in self._reserved.items():
                    if reserved > 0:
                        self._emit_orphan_alert(task_id, reserved)

    def _ensure_writable(self) -> None:
        if self._write_blocked:
            raise BudgetLedgerError(
                f"BudgetLedger {self._path} 写入结果不确定，已阻断后续写入"
            )

    def _write_event(self, record: dict, update) -> None:
        with self._lock:
            self._ensure_writable()
            try:
                with self._durable.transaction() as transaction:
                    self._refresh_locked(transaction)
                    transaction.append_json(record)
                    update()
            except DurableIOError as exc:
                self._write_blocked = True
                raise BudgetLedgerError(
                    f"无法确认 BudgetLedger {self._path} 写入持久化，已阻断后续写入: {exc}"
                ) from exc

    @property
    def write_blocked(self) -> bool:
        return self._write_blocked

    def set_budget(self, task_id: str, max_budget_token: int) -> None:
        self._write_event({
            "type": "set_budget", "task_id": task_id,
            "max_budget_token": max_budget_token, "timestamp": datetime.now(UTC).isoformat(),
        }, lambda: self._max.__setitem__(task_id, max_budget_token))

    def check_and_reserve(self, task_id: str, cost: BudgetCost) -> bool:
        with self._lock:
            self._ensure_writable()
            try:
                with self._durable.transaction() as transaction:
                    transaction.repair_incomplete_tail()
                    self._refresh_locked(transaction)
                    maximum = self._max.get(task_id, self.default_max_budget_token)
                    used = self._committed[task_id] + self._reserved[task_id]
                    if used + cost.token_count > maximum:
                        return False
                    transaction.append_json({
                        "type": "reserve", "task_id": task_id,
                        "token_count": cost.token_count, "timestamp": datetime.now(UTC).isoformat(),
                    })
                    self._reserved[task_id] += cost.token_count
                    return True
            except DurableIOError as exc:
                self._write_blocked = True
                raise BudgetLedgerError(
                    f"无法确认 BudgetLedger {self._path} 写入持久化，已阻断后续写入: {exc}"
                ) from exc

    def commit(self, task_id: str, cost: BudgetCost) -> None:
        def update() -> None:
            self._reserved[task_id] -= cost.token_count
            self._committed[task_id] += cost.token_count
        self._write_event({
            "type": "commit", "task_id": task_id,
            "token_count": cost.token_count, "timestamp": datetime.now(UTC).isoformat(),
        }, update)

    def refund(self, task_id: str, cost: BudgetCost) -> None:
        self._write_event({
            "type": "refund", "task_id": task_id,
            "token_count": cost.token_count, "timestamp": datetime.now(UTC).isoformat(),
        }, lambda: self._reserved.__setitem__(
            task_id, max(0, self._reserved[task_id] - cost.token_count)
        ))

    def _emit_orphan_alert(self, task_id: str, reserved: int) -> None:
        if self._alert_store is None:
            return
        try:
            self._alert_store.save_alert(AuditAlert(
                alert_id=uuid.uuid4().hex, session_id="", task_id=task_id,
                rule_id="budget_orphan_reserve", severity="high", title="未闭环预算预留",
                description=f"task {task_id} 有 {reserved} token 的 reserve 未匹配 commit/refund",
                evidence=[],
            ))
        except Exception as exc:
            logger.warning("orphan reserve 告警写入失败: %s", exc)
