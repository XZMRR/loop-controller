"""AlertStore（v0.12.0）：持久化 AuditAlert 与 AuditReport。

``InMemoryAlertStore`` 用于测试；``JsonlAlertStore`` 基于 append-only JSONL，
启动时重放恢复索引。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from loop_controller.infra.durable_io import DurableIOError, DurableJsonlFile
from loop_controller.models import AuditAlert, AuditReport

logger = logging.getLogger(__name__)

PathLike = str | Path


class AlertStoreError(Exception):
    """AlertStore 损坏或操作失败时抛出（fail-closed）。"""


class AlertStore(Protocol):
    """告警与报告持久化存储协议。"""

    def save_alert(self, alert: AuditAlert) -> None: ...
    def list_alerts(
        self, session_id: str | None = None, task_id: str | None = None
    ) -> list[AuditAlert]: ...
    def save_report(self, report: AuditReport) -> None: ...
    def get_report(self, report_id: str) -> AuditReport | None: ...
    def list_reports(
        self, session_id: str | None = None, task_id: str | None = None
    ) -> list[AuditReport]: ...


class InMemoryAlertStore:
    """内存版 AlertStore；进程重启丢失。"""

    def __init__(self) -> None:
        self._alerts: dict[str, AuditAlert] = {}
        self._reports: dict[str, AuditReport] = {}

    def save_alert(self, alert: AuditAlert) -> None:
        self._alerts[alert.alert_id] = alert

    def list_alerts(
        self, session_id: str | None = None, task_id: str | None = None
    ) -> list[AuditAlert]:
        return [
            a
            for a in self._alerts.values()
            if (session_id is None or a.session_id == session_id)
            and (task_id is None or a.task_id == task_id)
        ]

    def save_report(self, report: AuditReport) -> None:
        self._reports[report.report_id] = report

    def get_report(self, report_id: str) -> AuditReport | None:
        return self._reports.get(report_id)

    def list_reports(
        self, session_id: str | None = None, task_id: str | None = None
    ) -> list[AuditReport]:
        return [
            r
            for r in self._reports.values()
            if (session_id is None or r.session_id == session_id)
            and (task_id is None or r.task_id == task_id)
        ]


@dataclass
class JsonlAlertStore:
    """基于 JSONL 的 Alert / Report 持久化存储。

    使用单文件追加，通过 ``type`` 字段区分 ``alert`` 与 ``report``。
    """

    path: PathLike
    _path: Path = field(init=False, repr=False)
    _alerts: dict[str, AuditAlert] = field(init=False, repr=False, default_factory=dict)
    _reports: dict[str, AuditReport] = field(init=False, repr=False, default_factory=dict)
    _durable: DurableJsonlFile = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._path = Path(str(self.path))
        self._durable = DurableJsonlFile(self._path)
        self._replay()

    def save_alert(self, alert: AuditAlert) -> None:
        record = alert.model_dump(mode="json")
        record["type"] = "alert"
        self._append(record)
        self._alerts[alert.alert_id] = alert

    def list_alerts(
        self, session_id: str | None = None, task_id: str | None = None
    ) -> list[AuditAlert]:
        return [
            a
            for a in self._alerts.values()
            if (session_id is None or a.session_id == session_id)
            and (task_id is None or a.task_id == task_id)
        ]

    def save_report(self, report: AuditReport) -> None:
        record = report.model_dump(mode="json")
        record["type"] = "report"
        self._append(record)
        self._reports[report.report_id] = report

    def get_report(self, report_id: str) -> AuditReport | None:
        return self._reports.get(report_id)

    def list_reports(
        self, session_id: str | None = None, task_id: str | None = None
    ) -> list[AuditReport]:
        return [
            r
            for r in self._reports.values()
            if (session_id is None or r.session_id == session_id)
            and (task_id is None or r.task_id == task_id)
        ]

    def _append(self, record: dict) -> None:
        try:
            with self._durable.transaction() as transaction:
                transaction.repair_incomplete_tail()
                transaction.read_complete_lines()
                transaction.append_json(record)
        except DurableIOError as exc:
            raise AlertStoreError(f"无法写入 AlertStore {self._path}: {exc}") from exc

    def _replay(self) -> None:
        if not self._path.exists():
            return
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise AlertStoreError(f"无法读取 AlertStore {self._path}: {exc}") from exc

        for lineno, line in enumerate(lines, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AlertStoreError(
                    f"AlertStore {self._path} 第 {lineno} 行非法 JSON: {exc}"
                ) from exc
            record_type = record.pop("type", None)
            if record_type == "alert":
                alert = AuditAlert.model_validate(record)
                self._alerts[alert.alert_id] = alert
            elif record_type == "report":
                report = AuditReport.model_validate(record)
                self._reports[report.report_id] = report
            else:
                logger.warning("未知的 alert store 记录类型 %s，跳过", record_type)
