"""DecisionStore：判定持久化与跨进程防重放。"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path

from loop_controller.checkpoint import DecisionStore
from loop_controller.infra.durable_io import DurableIOError, DurableJsonlFile
from loop_controller.models import Decision


class DecisionStoreError(Exception):
    """DecisionStore 自身完整性错误（如日志损坏）。"""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _serialize_decision(decision: Decision) -> dict:
    data = decision.model_dump(mode="json")
    data["type"] = "decision"
    return data


def _deserialize_decision(record: dict) -> Decision:
    data = dict(record)
    data.pop("type", None)
    return Decision.model_validate(data)


class JsonlDecisionStore(DecisionStore):
    """使用 DurableJsonlFile 的跨进程安全 DecisionStore。"""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._durable = DurableJsonlFile(self._path)
        self._lock = threading.RLock()
        self._call_ids: set[str] = set()
        self._decisions: dict[str, Decision] = {}
        self._used_counts: dict[str, int] = {}
        self._finalized: set[str] = set()
        self._refresh()

    def _refresh_locked(self, transaction) -> None:
        call_ids: set[str] = set()
        decisions: dict[str, Decision] = {}
        used_counts: dict[str, int] = {}
        finalized: set[str] = set()
        for lineno, raw in enumerate(transaction.read_complete_lines(), start=1):
            try:
                record = json.loads(raw)
                rtype = record.get("type")
                if rtype == "proposal":
                    call_id = record.get("call_id")
                    if call_id:
                        call_ids.add(call_id)
                elif rtype == "decision":
                    decision = _deserialize_decision(record)
                    decisions[decision.decision_id] = decision
                    used_counts.setdefault(decision.decision_id, 0)
                elif rtype == "decision_use":
                    decision_id = record.get("decision_id")
                    if decision_id:
                        used_counts[decision_id] = used_counts.get(decision_id, 0) + 1
                elif rtype == "finalized":
                    decision_id = record.get("decision_id")
                    if decision_id:
                        finalized.add(decision_id)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise DecisionStoreError(
                    f"decision log 第 {lineno} 行记录损坏：{self._path}"
                ) from exc
        self._call_ids = call_ids
        self._decisions = decisions
        self._used_counts = used_counts
        self._finalized = finalized

    def _refresh(self) -> None:
        with self._lock:
            try:
                with self._durable.transaction() as transaction:
                    transaction.repair_incomplete_tail()
                    self._refresh_locked(transaction)
            except DurableIOError as exc:
                message = str(exc)
                if "corrupted JSONL record at line " in message:
                    line = message.rsplit(" ", 1)[-1]
                    raise DecisionStoreError(
                        f"decision log 第 {line} 行 JSON 损坏：{self._path}"
                    ) from exc
                raise DecisionStoreError(f"无法读取 DecisionStore {self._path}: {exc}") from exc

    def is_call_id_seen(self, call_id: str) -> bool:
        self._refresh()
        return call_id in self._call_ids

    def record_proposal(self, task_id: str, call_id: str) -> None:
        with self._lock:
            try:
                with self._durable.transaction() as transaction:
                    transaction.repair_incomplete_tail()
                    self._refresh_locked(transaction)
                    if call_id in self._call_ids:
                        raise DecisionStoreError(f"call_id {call_id} 已存在，不允许重复记录")
                    transaction.append_json({
                        "type": "proposal",
                        "task_id": task_id,
                        "call_id": call_id,
                        "ts": _utc_now().isoformat(),
                    })
                    self._call_ids.add(call_id)
            except DurableIOError as exc:
                raise DecisionStoreError(f"无法写入 DecisionStore {self._path}: {exc}") from exc

    def record_decision(self, decision: Decision) -> None:
        with self._lock:
            try:
                with self._durable.transaction() as transaction:
                    transaction.repair_incomplete_tail()
                    self._refresh_locked(transaction)
                    transaction.append_json(_serialize_decision(decision))
                    self._decisions[decision.decision_id] = decision
                    self._used_counts.setdefault(decision.decision_id, 0)
            except DurableIOError as exc:
                raise DecisionStoreError(f"无法写入 DecisionStore {self._path}: {exc}") from exc

    def get_decision(self, decision_id: str) -> Decision | None:
        self._refresh()
        return self._decisions.get(decision_id)

    def use_decision(self, decision_id: str, now: datetime) -> bool:
        with self._lock:
            try:
                with self._durable.transaction() as transaction:
                    transaction.repair_incomplete_tail()
                    self._refresh_locked(transaction)
                    decision = self._decisions.get(decision_id)
                    if decision is None or now >= decision.expires_at:
                        return False
                    used = self._used_counts.get(decision_id, 0)
                    if used >= decision.max_uses:
                        return False
                    transaction.append_json({
                        "type": "decision_use",
                        "decision_id": decision_id,
                        "ts": now.isoformat(),
                    })
                    self._used_counts[decision_id] = used + 1
                    return True
            except DurableIOError as exc:
                raise DecisionStoreError(f"无法写入 DecisionStore {self._path}: {exc}") from exc

    def record_finalized(self, decision_id: str) -> None:
        with self._lock:
            try:
                with self._durable.transaction() as transaction:
                    transaction.repair_incomplete_tail()
                    self._refresh_locked(transaction)
                    if decision_id in self._finalized:
                        return
                    transaction.append_json({
                        "type": "finalized",
                        "decision_id": decision_id,
                        "finalized_at": _utc_now().isoformat(),
                    })
                    self._finalized.add(decision_id)
            except DurableIOError as exc:
                raise DecisionStoreError(f"无法写入 DecisionStore {self._path}: {exc}") from exc

    def is_decision_finalized(self, decision_id: str) -> bool:
        self._refresh()
        return decision_id in self._finalized
