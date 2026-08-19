"""会话管理器（v1.2 §3.1 / v0.4.0）：分配、复用、持久化与校验 Session。

SessionManager 维护活跃 session 表，支持内存后端（测试/P1）和 JSONL 持久化后端（v0.4.0）。
按可配置的超时窗口判定 session 是否过期；入口层通过 Runtime.create_task 分配/复用 session_id；
底层 run_task 进入时必须校验 task.session_id 存在、活跃且绑定一致——不一致则 fail-closed。
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


class SessionStoreError(Exception):
    """Session 存储完整性错误（如日志损坏）。"""


@dataclass
class Session:
    """治理侧会话对象。"""

    session_id: str
    user_id: str
    agent_id: str
    created_at: datetime
    last_task_at: datetime
    active: bool = True

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "agent_id": self.agent_id,
            "created_at": self.created_at.isoformat(),
            "last_task_at": self.last_task_at.isoformat(),
            "active": self.active,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Session:
        return cls(
            session_id=data["session_id"],
            user_id=data["user_id"],
            agent_id=data["agent_id"],
            created_at=datetime.fromisoformat(data["created_at"]),
            last_task_at=datetime.fromisoformat(data["last_task_at"]),
            active=data.get("active", True),
        )


@runtime_checkable
class SessionBackend(Protocol):
    """Session 持久化/共享后端协议（P1 为内存实现，P2 可按需替换为 Redis 等）。"""

    def get_active(self, user_id: str, agent_id: str) -> Session | None:
        """查询 (user_id, agent_id) 的活跃 session；不存在或已过期返回 None。"""
        ...

    def get_by_id(self, session_id: str) -> Session | None:
        """按 session_id 查询 session；不存在返回 None。"""
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

    def get_by_id(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def put(self, session: Session) -> None:
        self._sessions[session.session_id] = session
        self._index[(session.user_id, session.agent_id)] = session.session_id


class JsonlSessionBackend:
    """JSONL 追加写 + 启动重放的 Session 后端（v0.4.0）。

    - 父目录不存在时自动创建；
    - 初始化时检查父目录可写；
    - 启动重放时中间行非法 JSON 抛 SessionStoreError（fail-closed），末行忽略并 WARNING；
    - 每次 put 后 flush。
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._ensure_writable()
        self._sessions: dict[str, Session] = {}
        self._index: dict[tuple[str, str], str] = {}
        self._load()

    def _ensure_writable(self) -> None:
        """检查并确保父目录可写（启动校验）。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        probe = self._path.parent / ".write_probe_session"
        try:
            probe.write_text("", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            raise PermissionError(f"session 父目录 {self._path.parent} 不可写：{exc}") from exc
        if self._path.exists():
            try:
                with self._path.open("a", encoding="utf-8"):
                    pass
            except OSError as exc:
                raise PermissionError(f"session 文件 {self._path} 不可追加：{exc}") from exc

    def _load(self) -> None:
        """启动时重放全量日志，恢复内存状态。"""
        if not self._path.exists():
            return
        raw_lines = self._path.read_text(encoding="utf-8").splitlines(keepends=True)
        for line_no, raw in enumerate(raw_lines, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                session = Session.from_dict(data)
            except (json.JSONDecodeError, KeyError, ValueError) as exc:
                is_last = line_no == len(raw_lines)
                if is_last:
                    logger.warning(
                        "sessions.jsonl 末行（第 %d 行）不完整，已忽略：%s",
                        line_no,
                        exc,
                    )
                    continue
                raise SessionStoreError(
                    f"sessions.jsonl 第 {line_no} 行损坏：{self._path}"
                ) from exc
            self._sessions[session.session_id] = session
            if session.active:
                self._index[(session.user_id, session.agent_id)] = session.session_id

    def get_active(self, user_id: str, agent_id: str) -> Session | None:
        session_id = self._index.get((user_id, agent_id))
        if session_id is None:
            return None
        return self._sessions.get(session_id)

    def get_by_id(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def put(self, session: Session) -> None:
        """追加写 session 状态，并更新内存索引。"""
        self._sessions[session.session_id] = session
        if session.active:
            self._index[(session.user_id, session.agent_id)] = session.session_id
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(session.to_dict(), ensure_ascii=False) + "\n")
            fh.flush()


class SessionManager:
    """治理侧会话分配、复用与校验入口。

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
        self._now = now or (lambda: datetime.now(UTC))

    def get_session(self, session_id: str) -> Session | None:
        """按 session_id 查询 session；不存在返回 None。"""
        return self._backend.get_by_id(session_id)

    def is_session_active(self, session_id: str) -> bool:
        """按 session_id 查询是否仍活跃。"""
        session = self._backend.get_by_id(session_id)
        if session is None:
            return False
        if not session.active:
            return False
        return (self._now() - session.last_task_at) <= self._timeout

    def is_session_expired(self, session_id: str) -> bool:
        """按 session_id 查询是否已过期（不存在视为已过期）。"""
        return not self.is_session_active(session_id)

    def get_or_create_session(self, user_id: str, agent_id: str) -> Session:
        """查询活跃 session；不存在或上一任务结束超过 timeout 则创建新 session_id（uuid hex）。"""
        now = self._now()
        session = self._backend.get_active(user_id, agent_id)
        if session is not None and session.active and (now - session.last_task_at) <= self._timeout:
            return session
        # 超时或不活跃则新建，旧 session 在持久化中保留（审计可回放），但索引会被新 session 覆盖。
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

    def validate_and_touch(self, task) -> bool:
        """校验 task.session_id 存在且活跃，且绑定 (user_id, agent_id) 与 task 一致。

        验证通过更新 last_task_at；不一致则抛 ValueError（fail-closed）。
        """
        session = self._backend.get_by_id(task.session_id)
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

    def touch_session(self, session_id: str) -> Session:
        """刷新 session 活跃时间；不存在则抛 ValueError。"""
        session = self._backend.get_by_id(session_id)
        if session is None:
            raise ValueError(f"session {session_id} 不存在")
        session.last_task_at = self._now()
        self._backend.put(session)
        return session

    def close_session(self, session_id: str) -> None:
        """标记 session 结束（任务正常结束时调用，或保留等待自然过期）。"""
        session = self._backend.get_by_id(session_id)
        if session is None:
            raise ValueError(f"session {session_id} 不存在")
        session.active = False
        self._backend.put(session)
