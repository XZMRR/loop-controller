"""预算记账.

分别跟踪 Token 级运行预算和现实财务支付预算。
MVP 阶段主要实现 Token 预算接口，支付预算保留字段但不启用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from loop_controller.action_proposal import ActionProposal


@dataclass(frozen=True)
class BudgetCost:
    """单次动作的成本估算.

    Attributes:
        token_count: 预计消耗的 Token 数量。
        estimated_payment: 预计财务支付金额，研究助手场景通常为 0。
    """

    token_count: int = 0
    estimated_payment: float = 0.0


@runtime_checkable
class BudgetLedger(Protocol):
    """预算账本接口.

    check_and_reserve 先冻结预算，执行成功后 commit，失败则 refund。
    """

    def check_and_reserve(self, proposal: ActionProposal, cost: BudgetCost) -> bool:
        """检查预算是否充足并预留."""
        ...

    def commit(self, proposal: ActionProposal, cost: BudgetCost) -> None:
        """执行成功后确认消耗."""
        ...

    def refund(self, proposal: ActionProposal, cost: BudgetCost) -> None:
        """执行失败或拒绝后释放预留."""
        ...


class InMemoryBudgetLedger:
    """MVP 内存版预算账本.

    只按 task_id 维度简单记账，不区分 agent 或 tool。
    """

    def __init__(self, default_token_budget: int = 100_000) -> None:
        """初始化.

        Args:
            default_token_budget: 默认每个任务 Token 预算上限。
        """
        self._default_token_budget = default_token_budget
        self._committed: dict[str, int] = {}
        self._reserved: dict[str, int] = {}

    def check_and_reserve(self, proposal: ActionProposal, cost: BudgetCost) -> bool:
        """检查剩余预算是否足够，并预留 cost.token_count."""
        task_id = proposal.task_id
        committed = self._committed.get(task_id, 0)
        reserved = self._reserved.get(task_id, 0)
        if committed + reserved + cost.token_count > self._default_token_budget:
            return False
        self._reserved[task_id] = reserved + cost.token_count
        return True

    def commit(self, proposal: ActionProposal, cost: BudgetCost) -> None:
        """从预留转入已消耗."""
        task_id = proposal.task_id
        reserved = self._reserved.get(task_id, 0)
        self._reserved[task_id] = max(0, reserved - cost.token_count)
        self._committed[task_id] = self._committed.get(task_id, 0) + cost.token_count

    def refund(self, proposal: ActionProposal, cost: BudgetCost) -> None:
        """释放预留."""
        task_id = proposal.task_id
        self._reserved[task_id] = max(0, self._reserved.get(task_id, 0) - cost.token_count)
