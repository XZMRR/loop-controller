"""InMemoryBudgetLedger 单元测试（T2.4 / §3.8 / A10）。"""

from __future__ import annotations

from loop_controller.budget import InMemoryBudgetLedger
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
