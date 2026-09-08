"""基于 SQLite 的 Session 后端（v0.48.0）。"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from pathlib import Path

from loop_controller.infra.state_db import StateDatabase, StateDatabaseError
from loop_controller.session import Session


class SqliteSessionBackend:
    """基于 ``StateDatabase`` 的 ``SessionBackend``。

    ``Session`` 的生命周期落 ``sessions`` 表；``get_or_create_active`` 依赖
    ``BEGIN IMMEDIATE`` 保证跨进程 check-then-act 原子性，``close`` 将
    ``active`` 置为终态。底层 ``StateDatabaseError`` 统一映射为 ``ValueError``，
    对齐内存/JSONL 后端的域错误契约。
    """

    def __init__(self, db: StateDatabase) -> None:
        self._db = db

    @classmethod
    def from_path(cls, path: str | Path) -> SqliteSessionBackend:
        """便捷构造：从数据库路径直接创建。"""
        return cls(StateDatabase(path))

    def get_active(self, user_id: str, agent_id: str) -> Session | None:
        try:
            row = self._db.get_active_session(user_id, agent_id)
        except StateDatabaseError as exc:
            raise ValueError(str(exc)) from exc
        return Session.from_dict(row) if row else None

    def get_by_id(self, session_id: str) -> Session | None:
        try:
            row = self._db.get_session(session_id)
        except StateDatabaseError as exc:
            raise ValueError(str(exc)) from exc
        return Session.from_dict(row) if row else None

    def put(self, session: Session) -> None:
        try:
            self._db.save_session(session.to_dict())
        except StateDatabaseError as exc:
            raise ValueError(str(exc)) from exc

    def touch(
        self,
        session_id: str,
        last_task_at: datetime,
        *,
        user_id: str | None = None,
        agent_id: str | None = None,
    ) -> Session:
        try:
            row = self._db.touch_session(
                session_id,
                last_task_at.isoformat(),
                user_id=user_id,
                agent_id=agent_id,
            )
        except StateDatabaseError as exc:
            raise ValueError(str(exc)) from exc
        return Session.from_dict(row)

    def close(self, session_id: str) -> Session:
        try:
            row = self._db.close_session(session_id)
        except StateDatabaseError as exc:
            raise ValueError(str(exc)) from exc
        return Session.from_dict(row)

    def get_or_create_active(
        self, user_id: str, agent_id: str, now: datetime, timeout: timedelta
    ) -> Session:
        new_session = Session(
            session_id=uuid.uuid4().hex,
            user_id=user_id,
            agent_id=agent_id,
            created_at=now,
            last_task_at=now,
        )
        try:
            row = self._db.get_or_create_active_session(
                user_id,
                agent_id,
                now.isoformat(),
                timeout.total_seconds(),
                new_session.to_dict(),
            )
        except StateDatabaseError as exc:
            raise ValueError(str(exc)) from exc
        return Session.from_dict(row)
