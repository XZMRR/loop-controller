"""预算记账（§3.8 / 开发指南 T2.4）.

``InMemoryBudgetLedger`` 为每个 ``task_id`` 独立维护已 reserve / 已 commit 的 token 计数；
``check_and_reserve`` 在额度内预占，``commit`` 在 forward 成功后确认消耗，
``refund`` 在 forward 异常时返还预占额度。
"""

from __future__ import annotations

from collections import defaultdict

from loop_controller.checkpoint import BudgetLedger
from loop_controller.models import BudgetCost


class InMemoryBudgetLedger(BudgetLedger):
    """内存版预算记账：per-task 计数，进程重启清零（MVP 可接受）。"""

    def __init__(self, default_max_budget_token: int = 1_000_000) -> None:
        self._default_max = default_max_budget_token
        self._max: dict[str, int] = {}
        self._reserved: dict[str, int] = defaultdict(int)
        self._committed: dict[str, int] = defaultdict(int)

    def set_budget(self, task_id: str, max_budget_token: int) -> None:
        """为指定任务设置/更新预算上限（运行期调用，非 Protocol 方法）。"""
        self._max[task_id] = max_budget_token

    def check_and_reserve(self, task_id: str, cost: BudgetCost) -> bool:
        """预占预算；成功返回 True，超支返回 False。"""
        max_budget = self._max.get(task_id, self._default_max)
        used = self._committed[task_id] + self._reserved[task_id]
        if used + cost.token_count > max_budget:
            return False
        self._reserved[task_id] += cost.token_count
        return True

    def commit(self, task_id: str, cost: BudgetCost) -> None:
        """确认消耗：把预占额度移入已提交。"""
        self._reserved[task_id] -= cost.token_count
        self._committed[task_id] += cost.token_count

    def refund(self, task_id: str, cost: BudgetCost) -> None:
        """返还预占额度（forward 异常路径，防止只进不出）。"""
        self._reserved[task_id] -= cost.token_count
        # 不允许 reserve 为负；但这里按正确调用路径不会出现负值。
        if self._reserved[task_id] < 0:
            self._reserved[task_id] = 0
