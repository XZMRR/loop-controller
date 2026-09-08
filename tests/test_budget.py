"""BudgetLedger 单元测试（T2.4 / §3.8 / A10 / v0.6.0）。"""

from __future__ import annotations

import json

import pytest

from loop_controller.budget import BudgetLedgerError, InMemoryBudgetLedger, JsonlBudgetLedger
from loop_controller.infra.alert_store import InMemoryAlertStore
from loop_controller.models import BudgetCost


def test_reserve_commit_refund() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.set_budget("t1", 3)

    # reserve 阶段：预占 2 + 1 = 3
    assert ledger.check_and_reserve("t1", BudgetCost(token_count=2))  # r=2 c=0
    assert ledger.check_and_reserve("t1", BudgetCost(token_count=1))  # r=3 c=0
    assert not ledger.check_and_reserve("t1", BudgetCost(token_count=1))  # 超限

    # commit 2：已消耗 2，剩余 1 额度但已全部被 reserve，无法再 reserve
    ledger.commit("t1", BudgetCost(token_count=2))  # r=1 c=2
    assert not ledger.check_and_reserve("t1", BudgetCost(token_count=1))  # used=3

    # refund 1：返还预占，可再 reserve 1
    ledger.refund("t1", BudgetCost(token_count=1))  # r=0 c=2
    assert ledger.check_and_reserve("t1", BudgetCost(token_count=1))  # r=1 c=2
    assert not ledger.check_and_reserve("t1", BudgetCost(token_count=1))  # 又满


def test_exceeded_returns_false() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.set_budget("t1", 1)

    assert ledger.check_and_reserve("t1", BudgetCost(token_count=1))
    assert not ledger.check_and_reserve("t1", BudgetCost(token_count=1))


def test_default_max_budget() -> None:
    ledger = InMemoryBudgetLedger(default_max_budget_token=2)
    assert ledger.check_and_reserve("t1", BudgetCost(token_count=2))
    assert not ledger.check_and_reserve("t1", BudgetCost(token_count=1))


def test_refund_never_negative() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.set_budget("t1", 10)
    ledger.refund("t1", BudgetCost(token_count=5))
    assert ledger._reserved["t1"] == 0  # noqa: SLF001


# v0.6.0：JsonlBudgetLedger 持久化测试


def test_jsonl_ledger_reserve_commit_refund(tmp_path) -> None:
    """JsonlBudgetLedger 事件序列后余额正确。"""
    path = tmp_path / "budget.jsonl"
    ledger = JsonlBudgetLedger(path, default_max_budget_token=3)

    assert ledger.check_and_reserve("t1", BudgetCost(token_count=2))
    assert ledger.check_and_reserve("t1", BudgetCost(token_count=1))
    assert not ledger.check_and_reserve("t1", BudgetCost(token_count=1))

    ledger.commit("t1", BudgetCost(token_count=2))
    ledger.refund("t1", BudgetCost(token_count=1))
    assert ledger.check_and_reserve("t1", BudgetCost(token_count=1))


def test_jsonl_ledger_persistence_across_reconstruction(tmp_path) -> None:
    """重建对象后状态恢复。"""
    path = tmp_path / "budget.jsonl"
    ledger1 = JsonlBudgetLedger(path, default_max_budget_token=10)
    ledger1.check_and_reserve("t1", BudgetCost(token_count=3))
    ledger1.commit("t1", BudgetCost(token_count=2))
    ledger1.refund("t1", BudgetCost(token_count=1))

    ledger2 = JsonlBudgetLedger(path, default_max_budget_token=10)
    # 初始 reserve 3，commit 2，refund 1 -> reserved=0, committed=2
    assert ledger2._reserved["t1"] == 0  # noqa: SLF001
    assert ledger2._committed["t1"] == 2  # noqa: SLF001
    assert ledger2._max.get("t1") is None
    # 已用 2，额度 10，还能 reserve 8
    assert ledger2.check_and_reserve("t1", BudgetCost(token_count=8))
    assert not ledger2.check_and_reserve("t1", BudgetCost(token_count=1))


def test_jsonl_ledger_set_budget_persisted(tmp_path) -> None:
    """set_budget 事件重启后恢复。"""
    path = tmp_path / "budget.jsonl"
    ledger1 = JsonlBudgetLedger(path)
    ledger1.set_budget("t1", 5)
    assert ledger1.check_and_reserve("t1", BudgetCost(token_count=5))

    ledger2 = JsonlBudgetLedger(path)
    assert not ledger2.check_and_reserve("t1", BudgetCost(token_count=1))


def test_jsonl_ledger_corrupted_file_fail_closed(tmp_path) -> None:
    """损坏文件 fail-closed。"""
    path = tmp_path / "budget.jsonl"
    path.write_text("not json\n", encoding="utf-8")
    with pytest.raises(BudgetLedgerError):  # noqa: PT012
        JsonlBudgetLedger(path)


def test_commit_fsync_uncertainty_raises_and_blocks_retry(tmp_path, monkeypatch) -> None:
    path = tmp_path / "budget.jsonl"
    ledger = JsonlBudgetLedger(path, default_max_budget_token=10)
    cost = BudgetCost(token_count=3)
    assert ledger.check_and_reserve("t1", cost)

    from loop_controller.infra import durable_io

    calls = 0

    def fail_fsync(fd: int) -> None:
        nonlocal calls
        calls += 1
        raise OSError("fsync result unknown")

    monkeypatch.setattr(durable_io.os, "fsync", fail_fsync)
    with pytest.raises(BudgetLedgerError, match="已阻断后续写入"):
        ledger.commit("t1", cost)

    assert ledger.write_blocked
    assert calls == 1
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert sum(record["type"] == "commit" for record in records) == 1
    assert ledger._committed["t1"] == 0

    with pytest.raises(BudgetLedgerError, match="写入结果不确定"):
        ledger.commit("t1", cost)
    assert calls == 1
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert sum(record["type"] == "commit" for record in records) == 1


def test_replay_orphan_reserve_emits_alert(tmp_path) -> None:
    """v0.29.0：JsonlBudgetLedger._replay 对未闭环 reserve 产生 alert_store 告警（不 fail）。"""
    path = tmp_path / "budget.jsonl"
    alert_store = InMemoryAlertStore()

    # 直接写入一条只有 reserve、没有 commit/refund 的事件
    record = {
        "type": "reserve",
        "task_id": "t1",
        "token_count": 7,
    }
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    ledger = JsonlBudgetLedger(path, alert_store=alert_store)
    assert ledger._reserved["t1"] == 7  # noqa: SLF001

    alerts = alert_store.list_alerts()
    assert len(alerts) == 1
    assert alerts[0].rule_id == "budget_orphan_reserve"
    assert alerts[0].task_id == "t1"
    assert "7" in alerts[0].description
