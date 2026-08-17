"""会话管理器（v1.2 §3.1）：分配、复用与校验同一 (user_id, agent_id) 的连续任务流。

SessionManager 维护活跃 session 内存表，按可配置的超时窗口判定 session 是否过期。
入口层应通过 Runtime.create_task 自动分配 session_id；底层 run_task 仍保留，但
进入时必须校验 task.session_id 与 (user_id, agent_id) 绑定一致——不一致则 fail-closed。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable


@dataclass
class Session:
    """治理侧会话对象。"""

    session_id: str
    user_id: str
    agent_id: str
    created_at: datetime
    last_task_at: datetime
    active: bool = True


@runtime_checkable
class SessionBackend(Protocol):
    """Session 持久化/共享后端协议（P1 为内存实现，P2 可按需替换为 Redis 等）。"""

    def get_active(self, user_id: str, agent_id: str) -> Session | None:
        """查询 (user_id, agent_id) 的活跃 session；不存在或已过期返回 None。"""
        ...

    def put(self, session: Session) -> None:
        """保存或更新 session。"""
        ...


class InMemorySessionBackend:
    """内存版 Session 后端（P1 单进程假设）。"""

    def __init__(self) -> None:
        # 以 session_id 为键，便于按 id 快速关闭；同时保留 (user_id, agent_id) -> session_id 索引。
        self._sessions: dict[str, Session] = {}
        self._index: dict[tuple[str, str], str] = {}

    def get_active(self, user_id: str, agent_id: str) -> Session | None:
        session_id = self._index.get((user_id, agent_id))
        if session_id is None:
            return None
        return self._sessions.get(session_id)

    def put(self, session: Session) -> None:
        self._sessions[session.session_id] = session
        self._index[(session.user_id, session.agent_id)] = session.session_id


class SessionManager:
    """治理侧会话分配与校验入口。

    Args:
        session_timeout_minutes: 上一任务结束后超过该窗口则开新 session，默认 30 分钟。
        backend: 持久化后端，默认内存实现。
        now: 可注入的时间源（测试用）。
    """

    def __init__(
        self,
        session_timeout_minutes: int = 30,
        backend: SessionBackend | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._timeout = timedelta(minutes=session_timeout_minutes)
        self._backend = backend or InMemorySessionBackend()
        self._now = now or (lambda: datetime.now(timezone.utc))

    def get_or_create_session(self, user_id: str, agent_id: str) -> Session:
        """查询活跃 session；不存在或上一任务结束超过 timeout 则创建新 session_id（uuid hex）。"""
        now = self._now()
        session = self._backend.get_active(user_id, agent_id)
        if session is not None and session.active and (now - session.last_task_at) <= self._timeout:
            return session
        # 超时或不活跃则新建，旧 session 在内存中保留（审计可回放），但索引会被新 session 覆盖。
        new_session = Session(
            session_id=uuid.uuid4().hex,
            user_id=user_id,
            agent_id=agent_id,
            created_at=now,
            last_task_at=now,
            active=True,
        )
        self._backend.put(new_session)
        return new_session

    def is_session_active(self, session_id: str) -> bool:
        """按 session_id 查询是否仍活跃。"""
        session = getattr(self._backend, "_sessions", {}).get(session_id)
        if session is None:
            return False
        return session.active

    def validate_and_touch(self, task) -> bool:
        """校验 task.session_id 存在且活跃，且绑定 (user_id, agent_id) 与 task 一致。

        验证通过更新 last_task_at；不一致则抛 ValueError（fail-closed）。
        """
        session = getattr(self._backend, "_sessions", {}).get(task.session_id)
        if session is None:
            raise ValueError(f"session {task.session_id} 不存在")
        if not session.active:
            raise ValueError(f"session {task.session_id} 已结束")
        if session.user_id != task.user_id or session.agent_id != task.agent_id:
            raise ValueError(
                f"session {task.session_id} 绑定 ({session.user_id}, {session.agent_id}) "
                f"与 task 的 ({task.user_id}, {task.agent_id}) 不一致"
            )
        session.last_task_at = self._now()
        self._backend.put(session)
        return True

    def close_session(self, session_id: str) -> None:
        """标记 session 结束（任务正常结束时调用，或保留等待自然过期）。"""
        sessions = getattr(self._backend, "_sessions", {})
        session = sessions.get(session_id)
        if session is None:
            raise ValueError(f"session {session_id} 不存在")
        session.active = False
        self._backend.put(session)
