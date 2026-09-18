"""SqliteSessionBackend 持久化测试（v0.49.0）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from loop_controller.infra.sqlite_session_backend import SqliteSessionBackend
from loop_controller.session import Session


@pytest.fixture
def backend(tmp_path: Path) -> SqliteSessionBackend:
    return SqliteSessionBackend.from_path(tmp_path / "sessions.db")


def _now() -> datetime:
    return datetime(2026, 1, 1, 0, 0, tzinfo=UTC)


def test_get_or_create_reuses_active(backend: SqliteSessionBackend) -> None:
    s1 = backend.get_or_create_active("alice", "agent_1", _now(), timedelta(minutes=30))
    s2 = backend.get_or_create_active("alice", "agent_1", _now(), timedelta(minutes=30))
    assert s1.session_id == s2.session_id


def test_get_or_create_timeout_creates_new(backend: SqliteSessionBackend) -> None:
    base = _now()
    s1 = backend.get_or_create_active("alice", "agent_1", base, timedelta(minutes=30))
    later = base + timedelta(minutes=31)
    s2 = backend.get_or_create_active("alice", "agent_1", later, timedelta(minutes=30))
    assert s1.session_id != s2.session_id


def test_touch_updates_last_task_at(backend: SqliteSessionBackend) -> None:
    session = backend.get_or_create_active("alice", "agent_1", _now(), timedelta(minutes=30))
    later = _now() + timedelta(minutes=5)
    updated = backend.touch(session.session_id, later)
    assert updated.last_task_at == later
    assert backend.get_by_id(session.session_id).last_task_at == later


def test_touch_missing_raises(backend: SqliteSessionBackend) -> None:
    with pytest.raises(ValueError, match="不存在"):
        backend.touch("missing", _now())


def test_touch_closed_raises(backend: SqliteSessionBackend) -> None:
    session = backend.get_or_create_active("alice", "agent_1", _now(), timedelta(minutes=30))
    backend.close(session.session_id)
    with pytest.raises(ValueError, match="已结束"):
        backend.touch(session.session_id, _now())


def test_touch_binding_mismatch_raises(backend: SqliteSessionBackend) -> None:
    session = backend.get_or_create_active("alice", "agent_1", _now(), timedelta(minutes=30))
    with pytest.raises(ValueError, match="不一致"):
        backend.touch(session.session_id, _now(), user_id="bob", agent_id="agent_1")


def test_close_is_terminal(backend: SqliteSessionBackend) -> None:
    session = backend.get_or_create_active("alice", "agent_1", _now(), timedelta(minutes=30))
    closed = backend.close(session.session_id)
    assert closed.active is False
    assert backend.get_active("alice", "agent_1") is None
    assert backend.get_by_id(session.session_id).active is False


def test_put_rejects_reopen(backend: SqliteSessionBackend) -> None:
    session = backend.get_or_create_active("alice", "agent_1", _now(), timedelta(minutes=30))
    backend.close(session.session_id)
    reopen = Session(
        session_id=session.session_id,
        user_id=session.user_id,
        agent_id=session.agent_id,
        created_at=session.created_at,
        last_task_at=session.last_task_at,
        active=True,
    )
    with pytest.raises(ValueError, match="已结束"):
        backend.put(reopen)


def test_persists_across_restarts(tmp_path: Path) -> None:
    path = tmp_path / "sessions.db"
    first = SqliteSessionBackend.from_path(path)
    s1 = first.get_or_create_active("alice", "agent_1", _now(), timedelta(minutes=30))

    second = SqliteSessionBackend.from_path(path)
    assert second.get_by_id(s1.session_id) is not None
    assert second.get_active("alice", "agent_1").session_id == s1.session_id


def test_session_datetime_roundtrip(backend: SqliteSessionBackend) -> None:
    created = datetime(2026, 2, 3, 4, 5, 6, tzinfo=UTC)
    session = Session(
        session_id="s1",
        user_id="alice",
        agent_id="agent_1",
        created_at=created,
        last_task_at=created,
    )
    backend.put(session)
    got = backend.get_by_id("s1")
    assert got is not None
    assert got.created_at == created
    assert got.last_task_at == created
