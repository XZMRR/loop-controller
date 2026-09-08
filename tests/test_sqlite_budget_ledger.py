"""SqliteBudgetLedger 持久化与并发测试（v0.43.0）。"""

from __future__ import annotations

from multiprocessing import Process, Queue
from pathlib import Path

from loop_controller.infra.alert_store import InMemoryAlertStore
from loop_controller.infra.sqlite_budget_ledger import SqliteBudgetLedger
from loop_controller.models import BudgetCost


def test_set_budget_and_reserve(tmp_path: Path) -> None:
    path = tmp_path / "budget.db"
    ledger = SqliteBudgetLedger.from_path(path)
    ledger.set_budget("t1", 3)

    assert ledger.check_and_reserve("t1", BudgetCost(token_count=2))
    assert ledger.check_and_reserve("t1", BudgetCost(token_count=1))
    assert not ledger.check_and_reserve("t1", BudgetCost(token_count=1))


def test_reserve_commit_refund(tmp_path: Path) -> None:
    path = tmp_path / "budget.db"
    ledger = SqliteBudgetLedger.from_path(path)
    ledger.set_budget("t1", 3)

    assert ledger.check_and_reserve("t1", BudgetCost(token_count=2))
    assert ledger.check_and_reserve("t1", BudgetCost(token_count=1))
    assert not ledger.check_and_reserve("t1", BudgetCost(token_count=1))

    ledger.commit("t1", BudgetCost(token_count=2))
    assert not ledger.check_and_reserve("t1", BudgetCost(token_count=1))

    ledger.refund("t1", BudgetCost(token_count=1))
    assert ledger.check_and_reserve("t1", BudgetCost(token_count=1))


def test_default_max_budget(tmp_path: Path) -> None:
    path = tmp_path / "budget.db"
    ledger = SqliteBudgetLedger.from_path(path, default_max_budget_token=2)
    assert ledger.check_and_reserve("t1", BudgetCost(token_count=2))
    assert not ledger.check_and_reserve("t1", BudgetCost(token_count=1))


def test_persists_across_restarts(tmp_path: Path) -> None:
    path = tmp_path / "budget.db"
    ledger1 = SqliteBudgetLedger.from_path(path, default_max_budget_token=10)
    ledger1.set_budget("t1", 5)
    ledger1.check_and_reserve("t1", BudgetCost(token_count=2))
    ledger1.commit("t1", BudgetCost(token_count=1))

    ledger2 = SqliteBudgetLedger.from_path(path, default_max_budget_token=10)
    # reserved=1, committed=1 -> 剩余额度 3
    assert ledger2.check_and_reserve("t1", BudgetCost(token_count=3))
    assert not ledger2.check_and_reserve("t1", BudgetCost(token_count=1))


def test_orphan_reserve_emits_alert(tmp_path: Path) -> None:
    path = tmp_path / "budget.db"
    ledger1 = SqliteBudgetLedger.from_path(path)
    ledger1.check_and_reserve("t1", BudgetCost(token_count=7))

    alert_store = InMemoryAlertStore()
    SqliteBudgetLedger.from_path(path, alert_store=alert_store)

    alerts = alert_store.list_alerts()
    assert len(alerts) == 1
    assert alerts[0].rule_id == "budget_orphan_reserve"
    assert alerts[0].task_id == "t1"
    assert "7" in alerts[0].description


def _reserve_worker(path: Path, task_id: str, token_count: int, result_queue: Queue) -> None:
    ledger = SqliteBudgetLedger.from_path(path)
    ok = ledger.check_and_reserve(task_id, BudgetCost(token_count=token_count))
    result_queue.put(ok)


def test_concurrent_reserve_cas(tmp_path: Path) -> None:
    """多进程并发 reserve 时，成功预留量不超过预算上限。"""
    path = tmp_path / "budget.db"
    ledger = SqliteBudgetLedger.from_path(path)
    ledger.set_budget("t1", 2)

    result_queue: Queue = Queue()
    processes = [
        Process(target=_reserve_worker, args=(path, "t1", 1, result_queue))
        for _ in range(3)
    ]
    for p in processes:
        p.start()
    for p in processes:
        p.join(timeout=10)

    results = [result_queue.get(timeout=10) for _ in processes]
    assert sum(1 for r in results if r) == 2

    # 预算已满，后续 reserve 应失败
    assert not SqliteBudgetLedger.from_path(path).check_and_reserve(
        "t1", BudgetCost(token_count=1)
    )
