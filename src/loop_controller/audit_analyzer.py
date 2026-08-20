"""R3 Asynchronous Audit Analyzer（v0.12.0）。

消费审计日志，按声明式规则检测异常模式，生成 AuditAlert 与 AuditReport。
分析器不修改审计链，只读取；所有异常内部捕获，避免影响主治理链路。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from loop_controller.infra.alert_store import AlertStore, InMemoryAlertStore
from loop_controller.infra.audit_store import AuditStore
from loop_controller.models import (
    AuditAlert,
    AuditEvent,
    AuditReport,
    AuditRule,
    AuditRules,
)

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


@runtime_checkable
class AuditAnalyzer(Protocol):
    """审计分析器接口。"""

    async def analyze_session(self, session_id: str) -> AuditReport: ...
    async def analyze_task(self, task_id: str) -> AuditReport: ...


class NoopAuditAnalyzer:
    """恒返回空报告的占位实现。"""

    async def analyze_session(self, session_id: str) -> AuditReport:
        return AuditReport(
            report_id=uuid.uuid4().hex,
            session_id=session_id,
            summary="audit analyzer disabled",
        )

    async def analyze_task(self, task_id: str) -> AuditReport:
        return AuditReport(
            report_id=uuid.uuid4().hex,
            session_id="",
            task_id=task_id,
            summary="audit analyzer disabled",
        )


class RuleBasedAuditAnalyzer:
    """基于声明式规则的异步审计分析器。"""

    def __init__(
        self,
        rules: AuditRules,
        audit_store: AuditStore,
        alert_store: AlertStore | None = None,
        now: datetime | None = None,
    ) -> None:
        self._rules = rules
        self._audit_store = audit_store
        self._alert_store = alert_store or InMemoryAlertStore()
        self._now = now or _utc_now()

    async def analyze_session(self, session_id: str) -> AuditReport:
        """分析整个 session 的审计事件。"""
        try:
            events = self._audit_store.query_by_session(session_id)
            return self._analyze(events, session_id=session_id)
        except Exception as exc:  # noqa: BLE001 - 审计分析不得影响主链路
            logger.exception("session %s audit analysis failed: %s", session_id, exc)
            return self._error_report(session_id=session_id, error=str(exc))

    async def analyze_task(self, task_id: str) -> AuditReport:
        """分析单个 task 的审计事件。"""
        try:
            events = self._audit_store.query_by_task(task_id)
            return self._analyze(events, task_id=task_id)
        except Exception as exc:  # noqa: BLE001 - 审计分析不得影响主链路
            logger.exception("task %s audit analysis failed: %s", task_id, exc)
            return self._error_report(task_id=task_id, error=str(exc))

    def _analyze(
        self,
        events: list[AuditEvent],
        session_id: str | None = None,
        task_id: str | None = None,
    ) -> AuditReport:
        if not self._rules.enabled:
            return AuditReport(
                report_id=uuid.uuid4().hex,
                session_id=session_id or "",
                task_id=task_id,
                summary="audit analyzer disabled by configuration",
                event_count=len(events),
            )

        alerts: list[AuditAlert] = []
        for rule in self._rules.rules:
            matched = self._match_rule(events, rule)
            for evidence in matched:
                alert = AuditAlert(
                    alert_id=uuid.uuid4().hex,
                    session_id=session_id or (events[0].session_id if events else ""),
                    task_id=task_id,
                    rule_id=rule.rule_id,
                    severity=rule.severity,
                    title=rule.description,
                    description=f"Rule {rule.rule_id!r} matched {len(evidence)} event(s).",
                    evidence=[e.event_id for e in evidence],
                    created_at=self._now,
                )
                self._alert_store.save_alert(alert)
                alerts.append(alert)

        report = AuditReport(
            report_id=uuid.uuid4().hex,
            session_id=session_id or "",
            task_id=task_id,
            summary=self._build_summary(events, alerts),
            alert_ids=[a.alert_id for a in alerts],
            event_count=len(events),
            metadata={"rule_count": len(self._rules.rules), "alert_count": len(alerts)},
        )
        self._alert_store.save_report(report)
        return report

    def _match_rule(
        self, events: Sequence[AuditEvent], rule: AuditRule
    ) -> list[list[AuditEvent]]:
        """返回所有命中该规则的证据事件集合（每条规则可能命中多次）。"""
        cond = rule.conditions
        hits: list[list[AuditEvent]] = []

        if cond.min_denies_count is not None and cond.min_denies_within_seconds is not None:
            hits.extend(self._match_rapid_denies(events, cond.min_denies_count, cond.min_denies_within_seconds))

        if cond.consecutive_denies is not None:
            hits.extend(self._match_consecutive_denies(events, cond.consecutive_denies))

        if cond.action_sequence is not None:
            hits.extend(self._match_action_sequence(events, cond.action_sequence))

        if cond.has_any_action is not None:
            if self._match_has_any_action(events, cond.has_any_action):
                hits.append(self._collect_events(events, cond.has_any_action))

        if cond.has_all_actions is not None:
            if self._match_has_all_actions(events, cond.has_all_actions):
                hits.append(self._collect_events(events, cond.has_all_actions))

        if cond.authority_token_exhausted:
            hits.extend(self._match_authority_token_exhausted(events))

        return hits

    def _match_rapid_denies(
        self, events: Sequence[AuditEvent], min_count: int, within_seconds: int
    ) -> list[list[AuditEvent]]:
        """滑动窗口：窗口内 deny 数量 >= min_count 则命中一次。"""
        deny_events = [e for e in events if e.action == "deny"]
        if len(deny_events) < min_count:
            return []
        results: list[list[AuditEvent]] = []
        for i in range(len(deny_events)):
            window = [deny_events[i]]
            for j in range(i + 1, len(deny_events)):
                delta = (deny_events[j].timestamp - deny_events[i].timestamp).total_seconds()
                if delta <= within_seconds:
                    window.append(deny_events[j])
                else:
                    break
            if len(window) >= min_count:
                results.append(window)
        return results

    def _match_consecutive_denies(
        self, events: Sequence[AuditEvent], count: int
    ) -> list[list[AuditEvent]]:
        """连续 N 个 deny 命中。"""
        results: list[list[AuditEvent]] = []
        run: list[AuditEvent] = []
        for event in events:
            if event.action == "deny":
                run.append(event)
                if len(run) >= count:
                    results.append(list(run[-count:]))
            else:
                run = []
        return results

    def _match_action_sequence(
        self, events: Sequence[AuditEvent], sequence: list[str]
    ) -> list[list[AuditEvent]]:
        """动作序列子串匹配。"""
        if not sequence:
            return []
        actions = [e.action for e in events]
        results: list[list[AuditEvent]] = []
        for i in range(len(actions) - len(sequence) + 1):
            if actions[i : i + len(sequence)] == sequence:
                results.append(list(events[i : i + len(sequence)]))
        return results

    def _match_has_any_action(
        self, events: Sequence[AuditEvent], actions: list[str]
    ) -> bool:
        """事件中至少包含指定动作之一。"""
        return any(e.action in actions for e in events)

    def _match_has_all_actions(
        self, events: Sequence[AuditEvent], actions: list[str]
    ) -> bool:
        """事件中包含所有指定动作。"""
        present = {e.action for e in events}
        return set(actions).issubset(present)

    def _match_authority_token_exhausted(
        self, events: Sequence[AuditEvent]
    ) -> list[list[AuditEvent]]:
        """检查 authority_used 事件后 remaining_budget 是否为 0。"""
        results: list[list[AuditEvent]] = []
        for event in events:
            if event.action == "authority_used":
                metadata = event.metadata or {}
                remaining = metadata.get("remaining_budget")
                if isinstance(remaining, dict) and remaining.get("token_count") == 0:
                    results.append([event])
                elif isinstance(remaining, int) and remaining == 0:
                    results.append([event])
        return results

    def _collect_events(
        self, events: Sequence[AuditEvent], actions: list[str]
    ) -> list[AuditEvent]:
        return [e for e in events if e.action in actions]

    def _build_summary(self, events: Sequence[AuditEvent], alerts: list[AuditAlert]) -> str:
        counts: dict[str, int] = {}
        for event in events:
            counts[event.action] = counts.get(event.action, 0) + 1
        summary = f"Analyzed {len(events)} event(s); actions: {counts}."
        if alerts:
            summary += f" Generated {len(alerts)} alert(s): " + ", ".join(
                f"{a.rule_id}({a.severity})" for a in alerts
            )
        else:
            summary += " No alerts."
        return summary

    def _error_report(
        self,
        session_id: str | None = None,
        task_id: str | None = None,
        error: str = "",
    ) -> AuditReport:
        return AuditReport(
            report_id=uuid.uuid4().hex,
            session_id=session_id or "",
            task_id=task_id,
            summary=f"audit analysis failed: {error}",
            metadata={"error": error},
        )
