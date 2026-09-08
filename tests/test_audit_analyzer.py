"""R3 Asynchronous Audit Analyzer 单元测试（v0.12.0）。"""

from __future__ import annotations

import pytest

from loop_controller.audit_analyzer import RuleBasedAuditAnalyzer
from loop_controller.infra.alert_store import InMemoryAlertStore
from loop_controller.infra.audit_store import AuditStore
from loop_controller.models import (
    AuditEvent,
    AuditRule,
    AuditRuleConditions,
    AuditRules,
)


class _MemoryAuditStore(AuditStore):
    """内存版 AuditStore，仅用于测试。"""

    def __init__(self, events: list[AuditEvent]) -> None:
        self._events = events

    def append(self, event: AuditEvent) -> None:
        self._events.append(event)

    def verify_chain(self) -> bool:
        return True

    def query_by_trace(self, trace_id: str) -> list[AuditEvent]:
        return [e for e in self._events if e.trace_id == trace_id]

    def query_by_session(self, session_id: str) -> list[AuditEvent]:
        return [e for e in self._events if e.session_id == session_id]

    def query_by_task(self, task_id: str) -> list[AuditEvent]:
        return [e for e in self._events if e.trace_id == task_id]


def _event(action: str, task_id: str = "t1", session_id: str = "s1", timestamp_offset: int = 0) -> AuditEvent:
    from datetime import UTC, datetime, timedelta

    return AuditEvent(
        event_id=f"e-{action}-{task_id}-{timestamp_offset}",
        trace_id=task_id,
        session_id=session_id,
        actor_type="agent",
        actor_id="a1",
        action=action,  # type: ignore[arg-type]
        timestamp=datetime.now(UTC) + timedelta(seconds=timestamp_offset),
    )


def _rules() -> AuditRules:
    return AuditRules(
        enabled=True,
        rules=[
            AuditRule(
                rule_id="rapid_denies",
                description="3 denies within 60s",
                severity="medium",
                conditions=AuditRuleConditions(
                    min_denies_count=3,
                    min_denies_within_seconds=60,
                ),
            ),
            AuditRule(
                rule_id="consecutive_denies",
                description="3 consecutive denies",
                severity="high",
                conditions=AuditRuleConditions(consecutive_denies=3),
            ),
            AuditRule(
                rule_id="has_any_deny",
                description="has deny",
                severity="low",
                conditions=AuditRuleConditions(has_any_action=["deny"]),
            ),
            AuditRule(
                rule_id="seq_propose_execute",
                description="propose then execute",
                severity="medium",
                conditions=AuditRuleConditions(action_sequence=["propose", "execute"]),
            ),
        ],
    )


@pytest.mark.anyio
async def test_disabled_returns_empty_report() -> None:
    store = _MemoryAuditStore([_event("deny")])
    analyzer = RuleBasedAuditAnalyzer(
        rules=AuditRules(enabled=False),
        audit_store=store,
        alert_store=InMemoryAlertStore(),
    )
    report = await analyzer.analyze_task("t1")
    assert report.alert_ids == []
    assert "disabled" in report.summary


@pytest.mark.anyio
async def test_rapid_denies() -> None:
    events = [
        _event("deny", timestamp_offset=0),
        _event("deny", timestamp_offset=10),
        _event("deny", timestamp_offset=20),
    ]
    analyzer = RuleBasedAuditAnalyzer(
        rules=_rules(),
        audit_store=_MemoryAuditStore(events),
        alert_store=InMemoryAlertStore(),
    )
    report = await analyzer.analyze_task("t1")
    assert any(a.rule_id == "rapid_denies" for a in analyzer._alert_store.list_alerts())
    assert "rapid_denies(medium)" in report.summary


@pytest.mark.anyio
async def test_consecutive_denies() -> None:
    events = [
        _event("propose"),
        _event("deny"),
        _event("deny"),
        _event("deny"),
        _event("execute"),
    ]
    analyzer = RuleBasedAuditAnalyzer(
        rules=_rules(),
        audit_store=_MemoryAuditStore(events),
        alert_store=InMemoryAlertStore(),
    )
    report = await analyzer.analyze_task("t1")
    assert any(a.rule_id == "consecutive_denies" for a in analyzer._alert_store.list_alerts())
    assert "consecutive_denies(high)" in report.summary


@pytest.mark.anyio
async def test_has_any_action() -> None:
    events = [_event("deny")]
    analyzer = RuleBasedAuditAnalyzer(
        rules=_rules(),
        audit_store=_MemoryAuditStore(events),
        alert_store=InMemoryAlertStore(),
    )
    await analyzer.analyze_task("t1")
    assert any(a.rule_id == "has_any_deny" for a in analyzer._alert_store.list_alerts())


@pytest.mark.anyio
async def test_action_sequence() -> None:
    events = [_event("propose"), _event("execute")]
    analyzer = RuleBasedAuditAnalyzer(
        rules=_rules(),
        audit_store=_MemoryAuditStore(events),
        alert_store=InMemoryAlertStore(),
    )
    await analyzer.analyze_task("t1")
    assert any(a.rule_id == "seq_propose_execute" for a in analyzer._alert_store.list_alerts())


@pytest.mark.anyio
async def test_session_analysis() -> None:
    events = [
        _event("deny", task_id="t1", session_id="s1"),
        _event("deny", task_id="t2", session_id="s1"),
    ]
    analyzer = RuleBasedAuditAnalyzer(
        rules=_rules(),
        audit_store=_MemoryAuditStore(events),
        alert_store=InMemoryAlertStore(),
    )
    report = await analyzer.analyze_session("s1")
    assert report.session_id == "s1"
    # has_any_deny 会命中，因为 session 内存在 deny
    assert len(analyzer._alert_store.list_alerts(session_id="s1")) >= 1


@pytest.mark.anyio
async def test_authority_token_exhausted() -> None:
    events = [
        AuditEvent(
            event_id="e1",
            trace_id="t1",
            session_id="s1",
            actor_type="system",
            actor_id="authority",
            action="authority_used",
            metadata={"remaining_budget": {"token_count": 0}},
        )
    ]
    rules = AuditRules(
        enabled=True,
        rules=[
            AuditRule(
                rule_id="token_exhausted",
                description="token exhausted",
                severity="low",
                conditions=AuditRuleConditions(authority_token_exhausted=True),
            )
        ],
    )
    analyzer = RuleBasedAuditAnalyzer(
        rules=rules,
        audit_store=_MemoryAuditStore(events),
        alert_store=InMemoryAlertStore(),
    )
    await analyzer.analyze_task("t1")
    assert any(a.rule_id == "token_exhausted" for a in analyzer._alert_store.list_alerts())
