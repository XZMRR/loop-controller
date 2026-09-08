"""SqliteDecisionStore 持久化与多进程并发测试（v0.34.0）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from multiprocessing import Process, Queue
from pathlib import Path

import pytest

from loop_controller.infra.sqlite_decision_store import SqliteDecisionStore
from loop_controller.models import Decision


def _make_decision(decision_id: str, verdict: str, *, expired: bool = False) -> Decision:
    now = datetime.now(UTC)
    expires = now - timedelta(seconds=1) if expired else now + timedelta(minutes=5)
    return Decision(
        decision_id=decision_id,
        call_id=f"c-{decision_id}",
        task_id="t1",
        verdict=verdict,
        reason="test",
        expires_at=expires,
        max_uses=1,
    )


def test_proposal_recorded_and_seen(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    store = SqliteDecisionStore.from_path(path)

    assert not store.is_call_id_seen("c1")
    store.record_proposal("t1", "c1")
    assert store.is_call_id_seen("c1")
    # 新实例应能读取已持久化的 proposal
    assert SqliteDecisionStore.from_path(path).is_call_id_seen("c1")


def test_duplicate_call_id_rejected(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    store = SqliteDecisionStore.from_path(path)
    store.record_proposal("t1", "c1")

    from loop_controller.infra.decision_store import DecisionStoreError

    with pytest.raises(DecisionStoreError, match="已存在"):
        store.record_proposal("t1", "c1")


def test_decision_recorded_and_use(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    store = SqliteDecisionStore.from_path(path)

    decision = _make_decision("d1", "allow")
    store.record_decision(decision)
    assert store.get_decision("d1") is not None
    now = datetime.now(UTC)
    assert store.use_decision("d1", now)
    assert not store.use_decision("d1", now)  # max_uses=1


def test_use_decision_expired(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    store = SqliteDecisionStore.from_path(path)

    decision = _make_decision("d1", "allow", expired=True)
    store.record_decision(decision)
    now = datetime.now(UTC)
    assert not store.use_decision("d1", now)


def test_persists_across_restarts(tmp_path: Path) -> None:
    path = tmp_path / "state.db"

    first = SqliteDecisionStore.from_path(path)
    first.record_proposal("t1", "c1")
    decision = _make_decision("d1", "allow")
    first.record_decision(decision)
    first.use_decision("d1", datetime.now(UTC))

    second = SqliteDecisionStore.from_path(path)
    assert second.is_call_id_seen("c1")
    assert second.get_decision("d1") is not None
    assert not second.use_decision("d1", datetime.now(UTC))


def test_finalized_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    first = SqliteDecisionStore.from_path(path)
    # finalized 针对已存在的 decision，需要先写入 decision
    decision = _make_decision("d1", "allow")
    first.record_decision(decision)
    first.record_finalized("d1")

    second = SqliteDecisionStore.from_path(path)
    assert second.is_decision_finalized("d1")


def _worker(path: Path, call_id: str, result_queue: Queue) -> None:
    store = SqliteDecisionStore.from_path(path)
    try:
        store.record_proposal("t1", call_id)
        result_queue.put(("ok", call_id))
    except Exception as exc:
        result_queue.put(("error", str(exc)))


def test_concurrent_proposals_safe(tmp_path: Path) -> None:
    """多进程并发写入相同 call_id 时，仅一个成功，另一个被拒绝。"""
    path = tmp_path / "state.db"
    result_queue: Queue = Queue()
    processes = [
        Process(target=_worker, args=(path, f"c-{i}", result_queue))
        for i in range(5)
    ]
    for p in processes:
        p.start()
    for p in processes:
        p.join(timeout=10)

    results = [result_queue.get(timeout=10) for _ in processes]
    assert all(status == "ok" for status, _ in results)
    # 再次尝试写入相同 call_id 应被拒绝
    store = SqliteDecisionStore.from_path(path)
    from loop_controller.infra.decision_store import DecisionStoreError

    with pytest.raises(DecisionStoreError, match="已存在"):
        store.record_proposal("t1", "c-0")
