"""JsonlDecisionStore 持久化与跨进程防重放测试（A7）。"""

from __future__ import annotations

from loop_controller.infra.decision_store import JsonlDecisionStore


def test_proposal_recorded_and_seen(tmp_path) -> None:
    path = tmp_path / "decisions.jsonl"
    store = JsonlDecisionStore(path)

    assert not store.is_call_id_seen("c1")
    store.record_proposal("t1", "c1")
    assert store.is_call_id_seen("c1")
    # v1.1 全局检测：call_id 不按 task_id 分区，全局唯一
    assert JsonlDecisionStore(path).is_call_id_seen("c1")


def test_decision_use_recorded_and_seen(tmp_path) -> None:
    path = tmp_path / "decisions.jsonl"
    store = JsonlDecisionStore(path)

    store.record_decision_use("d1")
    assert store.is_decision_used("d1")
    assert not store.is_decision_used("d2")


def test_persists_across_restarts(tmp_path) -> None:
    """A7 核心：重启进程后重放仍被拦截。"""
    path = tmp_path / "decisions.jsonl"

    first = JsonlDecisionStore(path)
    first.record_proposal("t1", "c1")
    first.record_decision_use("d1")

    # 模拟新进程：重新构造 store
    second = JsonlDecisionStore(path)
    assert second.is_call_id_seen("c1")
    assert second.is_decision_used("d1")


def test_append_only_format(tmp_path) -> None:
    path = tmp_path / "decisions.jsonl"
    store = JsonlDecisionStore(path)
    store.record_proposal("t1", "c1")
    store.record_decision_use("d1")

    lines = path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    assert "\"type\": \"proposal\"" in lines[0]
    assert "\"type\": \"decision_use\"" in lines[1]
