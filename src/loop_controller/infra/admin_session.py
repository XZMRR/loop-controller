"""最小 Admin Session 登录（长期运行准备）。

验证 Admin API Key 后签发随机 token，管理端点接受
``Authorization: Bearer <token>`` 作为 API Key 的替代凭据，
避免前端长期持有明文 API Key。

进程内存存储：重启后全部 session 失效（最小实现取舍，
后续可平滑替换为 JSONL / SQLite 持久化，接口不变）。
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass


@dataclass
class AdminSession:
    token: str
    created_at: float
    expires_at: float

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at


class AdminSessionStore:
    """进程内 Admin Session 存储（线程安全，惰性清理过期项）。"""

    def __init__(self, ttl_seconds: int = 8 * 3600) -> None:
        self._ttl = ttl_seconds
        self._sessions: dict[str, AdminSession] = {}
        self._lock = threading.Lock()

    def create(self) -> AdminSession:
        session = AdminSession(
            token=secrets.token_urlsafe(32),
            created_at=time.time(),
            expires_at=time.time() + self._ttl,
        )
        with self._lock:
            self._sessions[session.token] = session
        return session

    def validate(self, token: str) -> bool:
        if not token:
            return False
        with self._lock:
            session = self._sessions.get(token)
            if session is None:
                return False
            if session.expired:
                del self._sessions[token]
                return False
        return True

    def revoke(self, token: str) -> bool:
        with self._lock:
            return self._sessions.pop(token, None) is not None

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)
