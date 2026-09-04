"""基于 SQLite 的 AlertStore 实现（v0.44.0）。"""

from __future__ import annotations

from pathlib import Path

from loop_controller.infra.alert_store import AlertStoreError
from loop_controller.infra.state_db import StateDatabase, StateDatabaseError
from loop_controller.models import AuditAlert, AuditReport


class SqliteAlertStore:
    """基于 ``StateDatabase`` 的 AlertStore。

    ``AuditAlert`` / ``AuditReport`` 分别落 ``alerts`` / ``reports`` 表；
    ``save_alert`` / ``save_report`` 以主键幂等覆盖，``list_*`` 支持
    ``session_id`` / ``task_id`` 可选过滤。
    """

    def __init__(self, db: StateDatabase) -> None:
        self._db = db

    @classmethod
    def from_path(cls, path: str | Path) -> SqliteAlertStore:
        """便捷构造：从数据库路径直接创建。"""
        return cls(StateDatabase(path))

    def save_alert(self, alert: AuditAlert) -> None:
        try:
            self._db.save_alert(alert.model_dump(mode="json"))
        except StateDatabaseError as exc:
            raise AlertStoreError(str(exc)) from exc

    def list_alerts(
        self, session_id: str | None = None, task_id: str | None = None
    ) -> list[AuditAlert]:
        try:
            rows = self._db.list_alerts(session_id=session_id, task_id=task_id)
        except StateDatabaseError as exc:
            raise AlertStoreError(str(exc)) from exc
        return [AuditAlert.model_validate(row) for row in rows]

    def save_report(self, report: AuditReport) -> None:
        try:
            self._db.save_report(report.model_dump(mode="json"))
        except StateDatabaseError as exc:
            raise AlertStoreError(str(exc)) from exc

    def get_report(self, report_id: str) -> AuditReport | None:
        try:
            row = self._db.get_report(report_id)
        except StateDatabaseError as exc:
            raise AlertStoreError(str(exc)) from exc
        return AuditReport.model_validate(row) if row else None

    def list_reports(
        self, session_id: str | None = None, task_id: str | None = None
    ) -> list[AuditReport]:
        try:
            rows = self._db.list_reports(session_id=session_id, task_id=task_id)
        except StateDatabaseError as exc:
            raise AlertStoreError(str(exc)) from exc
        return [AuditReport.model_validate(row) for row in rows]
