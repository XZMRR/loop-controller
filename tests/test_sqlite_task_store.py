"""SqliteTaskStore 持久化测试（v0.43.0）。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from loop_controller.infra.sqlite_task_store import SqliteTaskStore
from loop_controller.models import Task


def test_save_and_get(tmp_path: Path) -> None:
    store = SqliteTaskStore.from_path(tmp_path / "tasks.db")
    task = Task(
        task_id="t1",
        session_id="s1",
        user_id="alice",
        agent_id="a1",
        description="test",
    )
    store.save(task)
    got = store.get("t1")
    assert got is not None
    assert got.task_id == "t1"
    assert got.status == "created"


def test_complete_returns_none(tmp_path: Path) -> None:
    store = SqliteTaskStore.from_path(tmp_path / "tasks.db")
    task = Task(
        task_id="t1",
        session_id="s1",
        user_id="alice",
        agent_id="a1",
        description="test",
    )
    store.save(task)
    store.complete("t1")
    assert store.get("t1") is None


def test_latest_wins(tmp_path: Path) -> None:
    store = SqliteTaskStore.from_path(tmp_path / "tasks.db")
    task1 = Task(
        task_id="t1",
        session_id="s1",
        user_id="alice",
        agent_id="a1",
        description="v1",
    )
    store.save(task1)
    task2 = task1.model_copy(update={"description": "v2"})
    store.save(task2)

    got = store.get("t1")
    assert got is not None
    assert got.description == "v2"


def test_missing_returns_none(tmp_path: Path) -> None:
    store = SqliteTaskStore.from_path(tmp_path / "tasks.db")
    assert store.get("missing") is None


def test_persists_across_restarts(tmp_path: Path) -> None:
    path = tmp_path / "tasks.db"
    first = SqliteTaskStore.from_path(path)
    first.save(Task(
        task_id="t1",
        session_id="s1",
        user_id="alice",
        agent_id="a1",
        description="test",
    ))

    second = SqliteTaskStore.from_path(path)
    got = second.get("t1")
    assert got is not None
    assert got.description == "test"


def test_task_datetime_roundtrip(tmp_path: Path) -> None:
    created = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    task = Task(
        task_id="t1",
        session_id="s1",
        user_id="alice",
        agent_id="a1",
        description="test",
        created_at=created,
    )
    store = SqliteTaskStore.from_path(tmp_path / "tasks.db")
    store.save(task)

    got = store.get("t1")
    assert got is not None
    assert got.created_at == created
