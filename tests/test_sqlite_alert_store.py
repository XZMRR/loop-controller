"""SqliteAlertStore 持久化测试（v0.44.0）。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from loop_controller.infra.sqlite_alert_store import SqliteAlertStore
from loop_controller.models import AuditAlert, AuditReport


def _alert(
    alert_id: str = "al1",
    session_id: str = "s1",
    task_id: str | None = "t1",
    rule_id: str = "r1",
    severity: str = "medium",
    title: str | None = None,
    evidence: list[str] | None = None,
    created_at: datetime | None = None,
) -> AuditAlert:
    return AuditAlert(
        alert_id=alert_id,
        session_id=session_id,
        task_id=task_id,
        rule_id=rule_id,
        severity=severity,  # type: ignore[arg-type]
        title=title if title is not None else "title-" + alert_id,
        description="desc-" + alert_id,
        evidence=evidence if evidence is not None else ["e1", "e2"],
        created_at=created_at or datetime.now(UTC),
    )


def _report(
    report_id: str = "r1",
    session_id: str = "s1",
    task_id: str | None = "t1",
    alert_ids: list[str] | None = None,
    event_count: int = 3,
    metadata: dict | None = None,
    generated_at: datetime | None = None,
) -> AuditReport:
    return AuditReport(
        report_id=report_id,
        session_id=session_id,
        task_id=task_id,
        summary="summary-" + report_id,
        alert_ids=alert_ids if alert_ids is not None else ["al1", "al2"],
        event_count=event_count,
        metadata=metadata if metadata is not None else {"k": "v"},
        generated_at=generated_at or datetime.now(UTC),
    )


def test_save_and_list_alert(tmp_path: Path) -> None:
    store = SqliteAlertStore.from_path(tmp_path / "alerts.db")
    store.save_alert(_alert())

    alerts = store.list_alerts()
    assert len(alerts) == 1
    assert alerts[0].alert_id == "al1"
    assert alerts[0].evidence == ["e1", "e2"]


def test_list_alerts_filters(tmp_path: Path) -> None:
    store = SqliteAlertStore.from_path(tmp_path / "alerts.db")
    store.save_alert(_alert(alert_id="al1", session_id="s1", task_id="t1"))
    store.save_alert(_alert(alert_id="al2", session_id="s2", task_id="t2"))

    assert [a.alert_id for a in store.list_alerts(session_id="s1")] == ["al1"]
    assert [a.alert_id for a in store.list_alerts(task_id="t2")] == ["al2"]
    assert store.list_alerts(session_id="s1", task_id="t2") == []


def test_save_alert_idempotent(tmp_path: Path) -> None:
    store = SqliteAlertStore.from_path(tmp_path / "alerts.db")
    store.save_alert(_alert(alert_id="al1", title="first"))
    store.save_alert(_alert(alert_id="al1", title="second"))

    alerts = store.list_alerts()
    assert len(alerts) == 1
    assert alerts[0].title == "second"


def test_save_and_get_report(tmp_path: Path) -> None:
    store = SqliteAlertStore.from_path(tmp_path / "alerts.db")
    store.save_report(_report())

    report = store.get_report("r1")
    assert report is not None
    assert report.summary == "summary-r1"
    assert report.alert_ids == ["al1", "al2"]
    assert report.metadata == {"k": "v"}


def test_get_report_missing_returns_none(tmp_path: Path) -> None:
    store = SqliteAlertStore.from_path(tmp_path / "alerts.db")
    assert store.get_report("missing") is None


def test_list_reports_filters(tmp_path: Path) -> None:
    store = SqliteAlertStore.from_path(tmp_path / "alerts.db")
    store.save_report(_report(report_id="r1", session_id="s1"))
    store.save_report(_report(report_id="r2", session_id="s2"))

    assert [r.report_id for r in store.list_reports(session_id="s1")] == ["r1"]
    assert [r.report_id for r in store.list_reports(task_id="t1")] == ["r1", "r2"]


def test_save_report_idempotent(tmp_path: Path) -> None:
    store = SqliteAlertStore.from_path(tmp_path / "alerts.db")
    store.save_report(_report(report_id="r1", event_count=1))
    store.save_report(_report(report_id="r1", event_count=9))

    reports = store.list_reports()
    assert len(reports) == 1
    assert reports[0].event_count == 9


def test_persists_across_restarts(tmp_path: Path) -> None:
    path = tmp_path / "alerts.db"
    first = SqliteAlertStore.from_path(path)
    first.save_alert(_alert())
    first.save_report(_report())

    second = SqliteAlertStore.from_path(path)
    assert [a.alert_id for a in second.list_alerts()] == ["al1"]
    assert second.get_report("r1") is not None


def test_datetime_roundtrip(tmp_path: Path) -> None:
    created = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    generated = datetime(2026, 1, 2, 12, 0, 0, tzinfo=UTC)
    store = SqliteAlertStore.from_path(tmp_path / "alerts.db")
    store.save_alert(_alert(created_at=created))
    store.save_report(_report(generated_at=generated))

    assert store.list_alerts()[0].created_at == created
    assert store.get_report("r1").generated_at == generated
