"""基于 SQLite 的 RiskStateStore 实现（v0.34.0）。"""

from __future__ import annotations

from pathlib import Path

from loop_controller.infra.state_db import StateDatabase, StateDatabaseError
from loop_controller.risk_state import RiskEvent, RiskStateStore, RiskStateStoreError


class SqliteRiskStateStore(RiskStateStore):
    """基于 ``StateDatabase`` 的 RiskStateStore。

    事件写入 SQLite 事务，支持多进程并发安全；
    ``load_all`` 仍返回全部历史事件供 ``RiskStateManager`` 启动重放，
    但 SQLite 顺序扫描比 JSONL 追加文件更稳定，且 WAL 模式不阻塞写入。
    """

    def __init__(self, db: StateDatabase) -> None:
        self._db = db

    @classmethod
    def from_path(cls, path: str | Path) -> SqliteRiskStateStore:
        """便捷构造：从数据库路径直接创建。"""
        return cls(StateDatabase(path))

    def load_all(self) -> list[RiskEvent]:
        try:
            rows = self._db.load_risk_events()
        except StateDatabaseError as exc:
            raise RiskStateStoreError(str(exc)) from exc
        return [RiskEvent.from_dict(row) for row in rows]

    def append_event(self, event: RiskEvent) -> list[RiskEvent]:
        try:
            self._db.append_risk_event(event.to_dict())
        except StateDatabaseError as exc:
            raise RiskStateStoreError(str(exc)) from exc
        return self.load_all()
