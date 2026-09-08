"""SqliteRiskStateStore 持久化与多进程并发测试（v0.34.0）。"""

from __future__ import annotations

from multiprocessing import Process, Queue
from pathlib import Path

import pytest

from loop_controller.infra.sqlite_risk_state_store import SqliteRiskStateStore
from loop_controller.risk_state import RiskEvent, RiskStateManager


def test_load_all_empty(tmp_path: Path) -> None:
    path = tmp_path / "risk.db"
    store = SqliteRiskStateStore.from_path(path)
    assert store.load_all() == []


def test_append_and_load_events(tmp_path: Path) -> None:
    path = tmp_path / "risk.db"
    store = SqliteRiskStateStore.from_path(path)

    event = RiskEvent(
        session_id="s1",
        event_type="deny",
        score_delta=0.2,
        tag="deny",
    )
    store.append_event(event)
    loaded = store.load_all()
    assert len(loaded) == 1
    assert loaded[0].session_id == "s1"
    assert loaded[0].event_type == "deny"


def test_manager_with_sqlite_store(tmp_path: Path) -> None:
    """RiskStateManager 使用 SqliteRiskStateStore 时仍正确算分。"""
    path = tmp_path / "risk.db"
    store = SqliteRiskStateStore.from_path(path)
    manager = RiskStateManager(store)

    manager.update("s1", "deny")
    profile = manager.get_profile("s1")
    assert profile.denied_count == 1
    assert profile.cumulative_risk_score == pytest.approx(0.2)


def test_persists_across_restarts(tmp_path: Path) -> None:
    path = tmp_path / "risk.db"
    first = SqliteRiskStateStore.from_path(path)
    first.append_event(
        RiskEvent(session_id="s1", event_type="deny", score_delta=0.2, tag="deny")
    )

    second = RiskStateManager(SqliteRiskStateStore.from_path(path))
    profile = second.get_profile("s1")
    assert profile.denied_count == 1


def _worker(path: Path, session_id: str, result_queue: Queue) -> None:
    store = SqliteRiskStateStore.from_path(path)
    event = RiskEvent(
        session_id=session_id,
        event_type="deny",
        score_delta=0.2,
        tag="deny",
    )
    store.append_event(event)
    result_queue.put("ok")


def test_concurrent_appends_safe(tmp_path: Path) -> None:
    """多进程并发追加风险事件不丢数据。"""
    path = tmp_path / "risk.db"
    result_queue: Queue = Queue()
    processes = [
        Process(target=_worker, args=(path, f"s-{i}", result_queue))
        for i in range(5)
    ]
    for p in processes:
        p.start()
    for p in processes:
        p.join(timeout=10)

    results = [result_queue.get(timeout=10) for _ in processes]
    assert all(r == "ok" for r in results)

    store = SqliteRiskStateStore.from_path(path)
    assert len(store.load_all()) == 5
