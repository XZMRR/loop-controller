"""DecisionStore：判定持久化与跨进程防重放（§4.5 / 开发指南 T2.1）.

``JsonlDecisionStore`` 启动时全量加载 ``decisions.jsonl`` 进内存，
运行期"查内存 + 追加落盘"；进程重启后仍能通过重放日志恢复已见 call_id、
已签发 Decision 及其使用次数，从而满足 A7 跨重启防重放。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from loop_controller.checkpoint import DecisionStore
from loop_controller.models import Decision


class DecisionStoreError(Exception):
    """DecisionStore 自身完整性错误（如日志损坏）。"""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _serialize_decision(decision: Decision) -> dict:
    """把 Decision 序列化为可落盘的字典。"""
    data = decision.model_dump()
    data["type"] = "decision"
    data["expires_at"] = decision.expires_at.isoformat()
    return data


def _deserialize_decision(record: dict) -> Decision:
    """从 JSONL 记录反序列化 Decision。"""
    record = dict(record)
    record.pop("type", None)
    expires = record.get("expires_at")
    if isinstance(expires, str):
        record["expires_at"] = datetime.fromisoformat(expires)
    return Decision(**record)


class JsonlDecisionStore(DecisionStore):
    """JSONL 持久化 DecisionStore。

    落盘格式（每行一个 JSON）：
    - ``{"type": "proposal", "task_id": ..., "call_id": ..., "ts": "..."}``
    - ``{"type": "decision", "decision_id": ..., "expires_at": "...", ...Decision fields}``
    - ``{"type": "decision_use", "decision_id": ..., "ts": "..."}``
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._call_ids: set[str] = set()
        self._decisions: dict[str, Decision] = {}
        self._used_counts: dict[str, int] = {}
        self._load()

    def _load(self) -> None:
        """启动时重放全量日志，恢复内存状态；损坏行直接失败（P1 fail-closed）。"""
        if not self._path.exists():
            return
        with self._path.open("r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise DecisionStoreError(
                        f"decision log 第 {lineno} 行 JSON 损坏：{self._path}"
                    ) from exc
                rtype = record.get("type")
                if rtype == "proposal":
                    self._call_ids.add(record.get("call_id"))
                elif rtype == "decision":
                    try:
                        decision = _deserialize_decision(record)
                    except (TypeError, ValueError) as exc:
                        raise DecisionStoreError(
                            f"decision log 第 {lineno} 行 Decision 反序列化失败：{self._path}"
                        ) from exc
                    self._decisions[decision.decision_id] = decision
                    self._used_counts.setdefault(decision.decision_id, 0)
                elif rtype == "decision_use":
                    decision_id = record.get("decision_id")
                    if decision_id:
                        self._used_counts[decision_id] = self._used_counts.get(decision_id, 0) + 1

    def is_call_id_seen(self, call_id: str) -> bool:
        """call_id 全局唯一，跨 task 也防重放（v1.1 决策）。"""
        return call_id in self._call_ids

    def record_proposal(self, task_id: str, call_id: str) -> None:
        self._call_ids.add(call_id)
        self._append({
            "type": "proposal",
            "task_id": task_id,
            "call_id": call_id,
            "ts": _utc_now().isoformat(),
        })

    def record_decision(self, decision: Decision) -> None:
        """记录完整 Decision 及其有效期/最大使用次数。"""
        self._decisions[decision.decision_id] = decision
        self._used_counts.setdefault(decision.decision_id, 0)
        self._append(_serialize_decision(decision))

    def get_decision(self, decision_id: str) -> Decision | None:
        return self._decisions.get(decision_id)

    def use_decision(self, decision_id: str, now: datetime) -> bool:
        """原子检查决策是否存在、未过期、未超次数，通过后增加使用次数并落盘。"""
        decision = self._decisions.get(decision_id)
        if decision is None:
            return False
        if now >= decision.expires_at:
            return False
        if self._used_counts.get(decision_id, 0) >= decision.max_uses:
            return False
        self._used_counts[decision_id] = self._used_counts.get(decision_id, 0) + 1
        self._append({
            "type": "decision_use",
            "decision_id": decision_id,
            "ts": now.isoformat(),
        })
        return True

    def _append(self, record: dict) -> None:
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()
