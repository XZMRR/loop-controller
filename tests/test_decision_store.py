"""JsonlDecisionStore 持久化与跨进程防重放测试（A7）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from loop_controller.infra.decision_store import DecisionStoreError, JsonlDecisionStore
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


def test_proposal_recorded_and_seen(tmp_path) -> None:
    path = tmp_path / "decisions.jsonl"
    store = JsonlDecisionStore(path)

    assert not store.is_call_id_seen("c1")
    store.record_proposal("t1", "c1")
    assert store.is_call_id_seen("c1")
    # v1.1 全局检测：call_id 不按 task_id 分区，全局唯一
    assert JsonlDecisionStore(path).is_call_id_seen("c1")


def test_decision_recorded_and_use(tmp_path) -> None:
    path = tmp_path / "decisions.jsonl"
    store = JsonlDecisionStore(path)

    decision = _make_decision("d1", "allow")
    store.record_decision(decision)
    assert store.get_decision("d1") is not None
    now = datetime.now(UTC)
    assert store.use_decision("d1", now)
    assert not store.use_decision("d1", now)  # max_uses=1，第二次失败


def test_use_decision_expired(tmp_path) -> None:
    path = tmp_path / "decisions.jsonl"
    store = JsonlDecisionStore(path)

    decision = _make_decision("d1", "allow", expired=True)
    store.record_decision(decision)
    now = datetime.now(UTC)
    assert not store.use_decision("d1", now)


def test_persists_across_restarts(tmp_path) -> None:
    """A7 核心：重启进程后重放仍被拦截。"""
    path = tmp_path / "decisions.jsonl"

    first = JsonlDecisionStore(path)
    first.record_proposal("t1", "c1")
    decision = _make_decision("d1", "allow")
    first.record_decision(decision)
    first.use_decision("d1", datetime.now(UTC))

    # 模拟新进程：重新构造 store
    second = JsonlDecisionStore(path)
    assert second.is_call_id_seen("c1")
    assert second.get_decision("d1") is not None
    assert not second.use_decision("d1", datetime.now(UTC))


def test_append_only_format(tmp_path) -> None:
    path = tmp_path / "decisions.jsonl"
    store = JsonlDecisionStore(path)
    store.record_proposal("t1", "c1")
    decision = _make_decision("d1", "allow")
    store.record_decision(decision)

    lines = path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    assert "\"type\": \"proposal\"" in lines[0]
    assert "\"type\": \"decision\"" in lines[1]


def test_corrupt_log_fail_closed(tmp_path) -> None:
    """P1：日志损坏时必须阻止启动并报告行号，不能 fail-open 跳过。"""
    path = tmp_path / "decisions.jsonl"
    store = JsonlDecisionStore(path)
    store.record_proposal("t1", "c1")

    # 追加一行非法 JSON
    with path.open("a", encoding="utf-8") as fh:
        fh.write("this is not json\n")

    with pytest.raises(DecisionStoreError, match=r"decision log 第 2 行"):
        JsonlDecisionStore(path)
