"""预算记账（§3.8 / 开发指南 T2.4）。

``InMemoryBudgetLedger`` 为每个 ``task_id`` 独立维护已 reserve / 已 commit 的 token 计数；
``check_and_reserve`` 在额度内预占，``commit`` 在 forward 成功后确认消耗，
``refund`` 在 forward 异常时返还预占额度。

v0.6.0 新增 ``JsonlBudgetLedger``：通过 append-only JSONL 持久化预算事件，
启动时重放事件恢复状态。
"""

from __future__ import annotations

import json
import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from loop_controller.infra.alert_store import AlertStore
from loop_controller.models import AuditAlert, BudgetCost

logger = logging.getLogger(__name__)

PathLike = str | Path


class BudgetLedgerError(Exception):
    """预算账本损坏或操作失败时抛出（fail-closed）。"""


@runtime_checkable
class BudgetLedger(Protocol):
    """预算记账协议。"""

    def set_budget(self, task_id: str, max_budget_token: int) -> None: ...
    def check_and_reserve(self, task_id: str, cost: BudgetCost) -> bool: ...
    def commit(self, task_id: str, cost: BudgetCost) -> None: ...
    def refund(self, task_id: str, cost: BudgetCost) -> None: ...


class InMemoryBudgetLedger:
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


@dataclass
class JsonlBudgetLedger:
    """基于 JSONL 的持久化预算记账。

    事件类型：
    - ``set_budget``：设置 task 预算上限；
    - ``reserve``：预占额度；
    - ``commit``：确认消耗；
    - ``refund``：返还预占。

    启动时重放所有事件恢复内存状态；重放发现未闭环 reserve 时写入 alert_store（不 fail）。
    """

    path: PathLike
    default_max_budget_token: int = 1_000_000
    alert_store: AlertStore | None = None
    _path: Path = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._path = Path(str(self.path))
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._alert_store = self.alert_store
        self._max: dict[str, int] = {}
        self._reserved: dict[str, int] = defaultdict(int)
        self._committed: dict[str, int] = defaultdict(int)
        self._replay()

    def set_budget(self, task_id: str, max_budget_token: int) -> None:
        """为指定任务设置/更新预算上限。"""
        self._max[task_id] = max_budget_token
        self._append({
            "type": "set_budget",
            "task_id": task_id,
            "max_budget_token": max_budget_token,
            "timestamp": datetime.now(UTC).isoformat(),
        })

    def check_and_reserve(self, task_id: str, cost: BudgetCost) -> bool:
        """预占预算；成功返回 True，超支返回 False。"""
        max_budget = self._max.get(task_id, self.default_max_budget_token)
        used = self._committed[task_id] + self._reserved[task_id]
        if used + cost.token_count > max_budget:
            return False
        self._reserved[task_id] += cost.token_count
        self._append({
            "type": "reserve",
            "task_id": task_id,
            "token_count": cost.token_count,
            "timestamp": datetime.now(UTC).isoformat(),
        })
        return True

    def commit(self, task_id: str, cost: BudgetCost) -> None:
        """确认消耗：把预占额度移入已提交。"""
        self._reserved[task_id] -= cost.token_count
        self._committed[task_id] += cost.token_count
        self._append({
            "type": "commit",
            "task_id": task_id,
            "token_count": cost.token_count,
            "timestamp": datetime.now(UTC).isoformat(),
        })

    def refund(self, task_id: str, cost: BudgetCost) -> None:
        """返还预占额度。"""
        self._reserved[task_id] -= cost.token_count
        if self._reserved[task_id] < 0:
            self._reserved[task_id] = 0
        self._append({
            "type": "refund",
            "task_id": task_id,
            "token_count": cost.token_count,
            "timestamp": datetime.now(UTC).isoformat(),
        })

    def _append(self, record: dict) -> None:
        try:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                fh.flush()
        except OSError as exc:
            raise BudgetLedgerError(f"无法写入 BudgetLedger {self._path}: {exc}") from exc

    def _replay(self) -> None:
        """启动时重放事件恢复状态。"""
        if not self._path.exists():
            return
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise BudgetLedgerError(f"无法读取 BudgetLedger {self._path}: {exc}") from exc

        for lineno, line in enumerate(lines, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise BudgetLedgerError(
                    f"BudgetLedger {self._path} 第 {lineno} 行非法 JSON: {exc}"
                ) from exc
            event_type = record.get("type")
            task_id = record.get("task_id", "")
            token_count = int(record.get("token_count", 0))
            max_budget_token = record.get("max_budget_token")

            if event_type == "set_budget" and isinstance(max_budget_token, int):
                self._max[task_id] = max_budget_token
            elif event_type == "reserve":
                self._reserved[task_id] += token_count
            elif event_type == "commit":
                self._reserved[task_id] -= token_count
                self._committed[task_id] += token_count
            elif event_type == "refund":
                self._reserved[task_id] -= token_count
                if self._reserved[task_id] < 0:
                    self._reserved[task_id] = 0

        # v0.29.0：未闭环 reserve 仅告警，不阻断启动。
        if self._alert_store is not None:
            for task_id, reserved in self._reserved.items():
                if reserved > 0:
                    try:
                        self._alert_store.save_alert(
                            AuditAlert(
                                alert_id=uuid.uuid4().hex,
                                session_id="",
                                task_id=task_id,
                                rule_id="budget_orphan_reserve",
                                severity="high",
                                title="未闭环预算预留",
                                description=f"task {task_id} 有 {reserved} token 的 reserve 未匹配 commit/refund",
                                evidence=[],
                            )
                        )
                    except Exception as exc:
                        logger.warning("orphan reserve 告警写入失败: %s", exc)
