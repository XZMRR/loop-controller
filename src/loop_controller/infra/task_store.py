"""Task 持久化存储（v0.6.0）。

``JsonlTaskStore`` 以 append-only JSONL 保存 ``Task`` 生命周期事件，
启动时按需从尾部向前查找最新状态。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from loop_controller.infra.durable_io import DurableIOError, DurableJsonlFile
from loop_controller.models import Task

logger = logging.getLogger(__name__)

PathLike = str | Path


class TaskStoreError(Exception):
    """TaskStore 损坏或操作失败时抛出（fail-closed）。"""


@runtime_checkable
class TaskStore(Protocol):
    """Task 持久化存储协议。"""

    def save(self, task: Task) -> None: ...
    def get(self, task_id: str) -> Task | None: ...
    def complete(self, task_id: str) -> None: ...


class InMemoryTaskStore:
    """内存版 TaskStore；进程重启丢失，适合测试。"""

    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}

    def save(self, task: Task) -> None:
        self._tasks[task.task_id] = task

    def get(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    def complete(self, task_id: str) -> None:
        task = self._tasks.get(task_id)
        if task is not None:
            self._tasks[task_id] = task.model_copy(
                update={"status": "completed", "completed_at": datetime.now(UTC)}
            )


@dataclass
class JsonlTaskStore:
    """基于 JSONL 的 Task 持久化存储。

    - 每次 ``save`` 追加 ``{"type": "task", ...}``；
    - ``complete`` 追加 ``{"type": "task_complete", ...}``；
    - ``get`` 从文件尾部向前扫描，返回最新的 Task 状态。
    """

    path: PathLike
    _path: Path = field(init=False, repr=False)
    _durable: DurableJsonlFile = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._path = Path(str(self.path))
        self._durable = DurableJsonlFile(self._path)

    def save(self, task: Task) -> None:
        """持久化 Task。"""
        record = task.model_dump(mode="json")
        record["type"] = "task"
        self._append(record)

    def get(self, task_id: str) -> Task | None:
        """读取指定 task_id 的最新 Task 状态；不存在返回 None。"""
        if not self._path.exists():
            return None
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise TaskStoreError(f"无法读取 TaskStore {self._path}: {exc}") from exc

        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise TaskStoreError(
                    f"TaskStore {self._path} 包含非法 JSON: {exc}"
                ) from exc
            if record.get("task_id") != task_id:
                continue
            record_type = record.get("type")
            if record_type == "task":
                return Task.model_validate(record)
            if record_type == "task_complete":
                # 发现 task_complete 后停止扫描，其前一条 task 即为最终状态
                return None
        return None

    def complete(self, task_id: str) -> None:
        """标记 Task 完成。"""
        record = {
            "type": "task_complete",
            "task_id": task_id,
            "completed_at": datetime.now(UTC).isoformat(),
        }
        self._append(record)

    def _append(self, record: dict) -> None:
        try:
            with self._durable.transaction() as transaction:
                transaction.repair_incomplete_tail()
                transaction.read_complete_lines()
                transaction.append_json(record)
        except DurableIOError as exc:
            raise TaskStoreError(f"无法写入 TaskStore {self._path}: {exc}") from exc
