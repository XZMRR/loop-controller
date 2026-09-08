"""基于 SQLite 的 TaskStore 实现（v0.43.0）。"""

from __future__ import annotations

from pathlib import Path

from loop_controller.infra.state_db import StateDatabase, StateDatabaseError
from loop_controller.infra.task_store import TaskStoreError
from loop_controller.models import Task


class SqliteTaskStore:
    """基于 ``StateDatabase`` 的 TaskStore。

    ``tasks`` 以 ``status`` / ``completed_at`` 直接表达生命周期；``get`` 对
    ``completed`` 状态返回 ``None``，对齐 JSONL 反向扫描语义。
    """

    def __init__(self, db: StateDatabase) -> None:
        self._db = db

    @classmethod
    def from_path(cls, path: str | Path) -> SqliteTaskStore:
        """便捷构造：从数据库路径直接创建。"""
        return cls(StateDatabase(path))

    def save(self, task: Task) -> None:
        try:
            self._db.save_task(task.model_dump(mode="json"))
        except StateDatabaseError as exc:
            raise TaskStoreError(str(exc)) from exc

    def get(self, task_id: str) -> Task | None:
        try:
            row = self._db.get_task(task_id)
        except StateDatabaseError as exc:
            raise TaskStoreError(str(exc)) from exc
        return Task.model_validate(row) if row else None

    def complete(self, task_id: str) -> None:
        try:
            self._db.complete_task(task_id)
        except StateDatabaseError as exc:
            raise TaskStoreError(str(exc)) from exc
