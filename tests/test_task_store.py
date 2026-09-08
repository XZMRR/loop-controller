"""JsonlTaskStore 测试（v0.6.0）。"""

from datetime import UTC, datetime

import pytest

from loop_controller.infra.task_store import JsonlTaskStore, TaskStoreError
from loop_controller.models import Task


def test_save_and_get(tmp_path) -> None:
    """保存 Task 后能读到。"""
    store = JsonlTaskStore(tmp_path / "tasks.jsonl")
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


def test_complete_updates_status(tmp_path) -> None:
    """complete 后读不到该 task。"""
    store = JsonlTaskStore(tmp_path / "tasks.jsonl")
    task = Task(
        task_id="t1",
        session_id="s1",
        user_id="alice",
        agent_id="a1",
        description="test",
    )
    store.save(task)
    store.complete("t1")
    got = store.get("t1")
    assert got is None


def test_latest_wins(tmp_path) -> None:
    """多次 save 同 task_id 返回最新。"""
    store = JsonlTaskStore(tmp_path / "tasks.jsonl")
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


def test_missing_returns_none(tmp_path) -> None:
    """不存在的 task_id 返回 None。"""
    store = JsonlTaskStore(tmp_path / "tasks.jsonl")
    assert store.get("missing") is None


def test_corrupted_file_fail_closed(tmp_path) -> None:
    """损坏文件抛 TaskStoreError。"""
    path = tmp_path / "tasks.jsonl"
    path.write_text("not json\n", encoding="utf-8")
    store = JsonlTaskStore(path)
    with pytest.raises(TaskStoreError):
        store.get("t1")


def test_task_datetime_roundtrip(tmp_path) -> None:
    """Task 的 datetime 字段可正确序列化/反序列化。"""
    created = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    task = Task(
        task_id="t1",
        session_id="s1",
        user_id="alice",
        agent_id="a1",
        description="test",
        created_at=created,
    )
    store = JsonlTaskStore(tmp_path / "tasks.jsonl")
    store.save(task)
    got = store.get("t1")
    assert got is not None
    assert got.created_at == created
