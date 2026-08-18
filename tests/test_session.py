"""SessionManager 单元测试（v1.2 §3.1）。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from loop_controller.models import Task
from loop_controller.session import InMemorySessionBackend, SessionManager


def _fixed_now(start: datetime):
    """返回固定时间序列生成器（测试用）。"""
    times = [start + timedelta(minutes=i * 31) for i in range(10)]
    idx = 0

    def _now() -> datetime:
        nonlocal idx
        t = times[idx]
        idx += 1
        return t

    return _now


class TestSessionManager:
    def test_create_new_session(self):
        manager = SessionManager()
        session = manager.get_or_create_session("alice", "agent_1")
        assert session.user_id == "alice"
        assert session.agent_id == "agent_1"
        assert len(session.session_id) == 32  # uuid hex
        assert session.active is True

    def test_reuse_active_session(self):
        manager = SessionManager()
        s1 = manager.get_or_create_session("alice", "agent_1")
        s2 = manager.get_or_create_session("alice", "agent_1")
        assert s1.session_id == s2.session_id

    def test_different_user_agent_get_different_sessions(self):
        manager = SessionManager()
        s1 = manager.get_or_create_session("alice", "agent_1")
        s2 = manager.get_or_create_session("bob", "agent_1")
        s3 = manager.get_or_create_session("alice", "agent_2")
        assert s1.session_id != s2.session_id
        assert s1.session_id != s3.session_id
        assert s2.session_id != s3.session_id

    def test_timeout_creates_new_session(self):
        base = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        now_fn = _fixed_now(base)
        manager = SessionManager(session_timeout_minutes=30, now=now_fn)

        s1 = manager.get_or_create_session("alice", "agent_1")  # 0 min
        s2 = manager.get_or_create_session("alice", "agent_1")  # 31 min -> timeout
        assert s1.session_id != s2.session_id

    def test_validate_and_touch_updates_last_task_at(self):
        base = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        manager = SessionManager(now=lambda: base)
        session = manager.get_or_create_session("alice", "agent_1")

        task = Task(
            task_id="t1",
            session_id=session.session_id,
            user_id="alice",
            agent_id="agent_1",
            description="test",
        )
        assert manager.validate_and_touch(task) is True
        assert session.last_task_at == base

    def test_validate_and_touch_fails_when_session_not_found(self):
        manager = SessionManager()
        task = Task(
            task_id="t1",
            session_id=uuid.uuid4().hex,
            user_id="alice",
            agent_id="agent_1",
            description="test",
        )
        with pytest.raises(ValueError, match="不存在"):
            manager.validate_and_touch(task)

    def test_validate_and_touch_fails_when_binding_mismatch(self):
        manager = SessionManager()
        session = manager.get_or_create_session("alice", "agent_1")

        task = Task(
            task_id="t1",
            session_id=session.session_id,
            user_id="bob",  # 与 session 绑定不一致
            agent_id="agent_1",
            description="test",
        )
        with pytest.raises(ValueError, match="不一致"):
            manager.validate_and_touch(task)

    def test_validate_and_touch_fails_when_session_closed(self):
        manager = SessionManager()
        session = manager.get_or_create_session("alice", "agent_1")
        manager.close_session(session.session_id)

        task = Task(
            task_id="t1",
            session_id=session.session_id,
            user_id="alice",
            agent_id="agent_1",
            description="test",
        )
        with pytest.raises(ValueError, match="已结束"):
            manager.validate_and_touch(task)

    def test_close_session_idempotent_error(self):
        manager = SessionManager()
        session = manager.get_or_create_session("alice", "agent_1")
        manager.close_session(session.session_id)
        assert manager.is_session_active(session.session_id) is False

    def test_custom_backend(self):
        backend = InMemorySessionBackend()
        manager = SessionManager(backend=backend)
        session = manager.get_or_create_session("alice", "agent_1")
        assert backend.get_active("alice", "agent_1") == session
