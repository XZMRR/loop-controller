"""基于 SQLite 的 BudgetLedger 实现（v0.43.0）。"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from loop_controller.budget import BudgetLedgerError
from loop_controller.infra.alert_store import AlertStore
from loop_controller.infra.state_db import StateDatabase, StateDatabaseError
from loop_controller.models import AuditAlert, BudgetCost

logger = logging.getLogger(__name__)


class SqliteBudgetLedger:
    """基于 ``StateDatabase`` 的 BudgetLedger。

    用 ``budget_ledger`` 表中的 ``max_budget_token / reserved / committed``
    当前值替代 JSONL 事件重放，``check_and_reserve`` 通过单条 ``UPDATE``
    原子 CAS，保证多进程并发下的预算预留安全。
    """

    def __init__(
        self,
        db: StateDatabase,
        default_max_budget_token: int = 1_000_000,
        alert_store: AlertStore | None = None,
    ) -> None:
        self._db = db
        self._default_max_budget_token = default_max_budget_token
        self._alert_store = alert_store
        self._emit_orphan_alerts()

    @classmethod
    def from_path(
        cls,
        path: str | Path,
        default_max_budget_token: int = 1_000_000,
        alert_store: AlertStore | None = None,
    ) -> SqliteBudgetLedger:
        """便捷构造：从数据库路径直接创建。"""
        return cls(StateDatabase(path), default_max_budget_token, alert_store)

    def set_budget(self, task_id: str, max_budget_token: int) -> None:
        try:
            self._db.set_budget(task_id, max_budget_token)
        except StateDatabaseError as exc:
            raise BudgetLedgerError(str(exc)) from exc

    def check_and_reserve(self, task_id: str, cost: BudgetCost) -> bool:
        try:
            return self._db.check_and_reserve(
                task_id, cost.token_count, self._default_max_budget_token
            )
        except StateDatabaseError as exc:
            raise BudgetLedgerError(str(exc)) from exc

    def commit(self, task_id: str, cost: BudgetCost) -> None:
        try:
            self._db.commit_budget(task_id, cost.token_count)
        except StateDatabaseError as exc:
            raise BudgetLedgerError(str(exc)) from exc

    def refund(self, task_id: str, cost: BudgetCost) -> None:
        try:
            self._db.refund_budget(task_id, cost.token_count)
        except StateDatabaseError as exc:
            raise BudgetLedgerError(str(exc)) from exc

    def _emit_orphan_alerts(self) -> None:
        """启动时对 ``reserved > 0`` 的任务发出未闭环预留告警。"""
        if self._alert_store is None:
            return
        try:
            for task_id, reserved in self._db.iter_reserved_budget():
                self._alert_store.save_alert(AuditAlert(
                    alert_id=uuid.uuid4().hex,
                    session_id="",
                    task_id=task_id,
                    rule_id="budget_orphan_reserve",
                    severity="high",
                    title="未闭环预算预留",
                    description=f"task {task_id} 有 {reserved} token 的 reserve 未匹配 commit/refund",
                    evidence=[],
                ))
        except StateDatabaseError as exc:
            logger.warning("orphan reserve 告警枚举失败: %s", exc)
        except Exception as exc:  # noqa: BLE001 - 告警失败不应阻断启动
            logger.warning("orphan reserve 告警写入失败: %s", exc)
