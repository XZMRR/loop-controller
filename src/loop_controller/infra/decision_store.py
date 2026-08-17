"""DecisionStore：判定持久化与跨进程防重放（§4.5 / 开发指南 T2.1）.

``JsonlDecisionStore`` 启动时全量加载 ``decisions.jsonl`` 进两个内存 set，
运行期"查内存 + 追加落盘"；进程重启后仍能通过重放日志恢复已见 call_id 与已用
decision_id，从而满足 A7 跨重启防重放。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from loop_controller.checkpoint import DecisionStore


class JsonlDecisionStore(DecisionStore):
    """JSONL 持久化 DecisionStore。

    落盘格式（每行一个 JSON）：
    - ``{"type": "proposal", "task_id": ..., "call_id": ..., "ts": "..."}``
    - ``{"type": "decision_use", "decision_id": ..., "ts": "..."}``
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._call_ids: set[str] = set()
        self._used_decision_ids: set[str] = set()
        self._load()

    def _load(self) -> None:
        """启动时重放全量日志，恢复内存 set。"""
        if not self._path.exists():
            return
        with self._path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("type") == "proposal":
                    self._call_ids.add(record.get("call_id"))
                elif record.get("type") == "decision_use":
                    self._used_decision_ids.add(record.get("decision_id"))

    def is_call_id_seen(self, call_id: str) -> bool:
        """call_id 全局唯一，跨 task 也防重放（v1.1 决策）。"""
        return call_id in self._call_ids

    def is_decision_used(self, decision_id: str) -> bool:
        return decision_id in self._used_decision_ids

    def record_proposal(self, task_id: str, call_id: str) -> None:
        self._call_ids.add(call_id)
        self._append({
            "type": "proposal",
            "task_id": task_id,
            "call_id": call_id,
            "ts": datetime.now(timezone.utc).isoformat(),
        })

    def record_decision_use(self, decision_id: str) -> None:
        self._used_decision_ids.add(decision_id)
        self._append({
            "type": "decision_use",
            "decision_id": decision_id,
            "ts": datetime.now(timezone.utc).isoformat(),
        })

    def _append(self, record: dict) -> None:
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()
