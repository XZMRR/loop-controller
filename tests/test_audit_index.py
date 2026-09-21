"""AuditIndex SQLite 索引测试（v0.34.0）。"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from loop_controller.infra.audit_index import AuditIndex
from loop_controller.models import AuditEvent


def _make_event(
    seq: int,
    trace_id: str = "trace-1",
    session_id: str = "s1",
    action: str = "task_start",
    metadata: dict | None = None,
) -> AuditEvent:
    return AuditEvent(
        event_id=f"e{seq}",
        trace_id=trace_id,
        session_id=session_id,
        actor_type="agent",
        actor_id="agent_001",
        action=action,
        target="tool",
        reason="test",
        seq=seq,
        timestamp=datetime.now(UTC),
        metadata=metadata or {},
    )


def test_index_empty(tmp_path: Path) -> None:
    index = AuditIndex(tmp_path / "audit.index.db")
    assert index.list_recent() == []
    assert index.last_seq() == 0


def test_append_and_list_recent(tmp_path: Path) -> None:
    index = AuditIndex(tmp_path / "audit.index.db")
    for i in range(1, 6):
        index.append(_make_event(i))

    recent = index.list_recent(limit=3)
    assert len(recent) == 3
    assert [e.seq for e in recent] == [5, 4, 3]


def test_list_recent_filters_real_agent_and_tool_before_limit(tmp_path: Path) -> None:
    index = AuditIndex(tmp_path / "audit.index.db")
    index.append(_make_event(1).model_copy(update={
        "actor_id": "agent-a", "target": "send_email",
    }))
    index.append(_make_event(2).model_copy(update={
        "actor_id": "agent-b", "target": "web_search",
    }))

    assert [event.seq for event in index.list_recent(
        limit=1, agent_id="agent-a", tool_name="send_email"
    )] == [1]


def test_query_by_trace(tmp_path: Path) -> None:
    index = AuditIndex(tmp_path / "audit.index.db")
    index.append(_make_event(1, trace_id="trace-a"))
    index.append(_make_event(2, trace_id="trace-b"))
    index.append(_make_event(3, trace_id="trace-a"))

    results = index.query_by_trace("trace-a")
    assert len(results) == 2
    assert {e.seq for e in results} == {1, 3}


def test_query_by_session(tmp_path: Path) -> None:
    index = AuditIndex(tmp_path / "audit.index.db")
    index.append(_make_event(1, session_id="s1"))
    index.append(_make_event(2, session_id="s2"))

    assert len(index.query_by_session("s1")) == 1
    assert len(index.query_by_session("s2")) == 1


def test_rebuild_from_jsonl(tmp_path: Path) -> None:
    jsonl_path = tmp_path / "audit.jsonl"
    index_path = tmp_path / "audit.index.db"

    event = _make_event(1)
    line = json.dumps(
        {
            "seq": 1,
            "prev_hash": "GENESIS",
            "event": event.model_dump(mode="json", exclude_none=True),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    jsonl_path.write_text(line + "\n", encoding="utf-8")

    index = AuditIndex(index_path)
    count = index.rebuild_from_jsonl(jsonl_path)
    assert count == 1
    assert index.last_seq() == 1
    assert len(index.list_recent()) == 1


def test_interaction_index_preserves_verdict_and_filters(tmp_path: Path) -> None:
    index = AuditIndex(tmp_path / "audit.index.db")
    index.append(
        _make_event(
            1,
            metadata={
                "interaction_id": "int-1",
                "request_id": "req-1",
                "source_agent_id": "agent-a",
                "target_agent_id": "agent-b",
                "verdict": "modify",
                "policy_hits": ["p1"],
            },
        )
    )
    index.append(_make_event(2))
    index.append(
        _make_event(
            3,
            metadata={
                "interaction_id": "int-2",
                "source_agent_id": "agent-a",
                "target_agent_id": "agent-c",
                "verdict": "deny",
            },
        )
    )

    assert [e.seq for e in index.query_interactions(source_agent_id="agent-a")] == [3, 1]
    assert [e.seq for e in index.query_interactions(interaction_id="int-1")] == [1]
    assert [e.seq for e in index.query_interactions(target_agent_id="agent-c")] == [3]
    assert [e.seq for e in index.query_interactions(verdict="modify")] == [1]
    assert index.query_interactions(verdict="allow") == []


def test_rebuild_restores_interaction_index(tmp_path: Path) -> None:
    jsonl_path = tmp_path / "audit.jsonl"
    event = _make_event(
        1,
        metadata={
            "interaction_id": "int-rebuild",
            "source_agent_id": "agent-a",
            "target_agent_id": "agent-b",
            "verdict": "allow",
        },
    )
    jsonl_path.write_text(
        json.dumps({"event": event.model_dump(mode="json", exclude_none=True)}) + "\n",
        encoding="utf-8",
    )
    index = AuditIndex(tmp_path / "audit.index.db")
    assert index.rebuild_from_jsonl(jsonl_path) == 1
    assert [e.seq for e in index.query_interactions(interaction_id="int-rebuild")] == [1]


def test_degraded_recover(tmp_path: Path) -> None:
    index = AuditIndex(tmp_path / "audit.index.db")
    index.mark_degraded("manual")
    assert index.degraded
    index.reset_degraded()
    assert not index.degraded


def test_status_report(tmp_path: Path) -> None:
    index = AuditIndex(tmp_path / "audit.index.db")
    index.append(_make_event(1))
    status = index.status()
    assert status.healthy
    assert status.indexed_count == 1


def test_old_schema_migrates_idempotently(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE audit_events (
            seq INTEGER PRIMARY KEY, event_id TEXT NOT NULL UNIQUE,
            timestamp REAL NOT NULL, trace_id TEXT, session_id TEXT,
            action TEXT, json_payload TEXT NOT NULL, created_at TEXT NOT NULL)""")
    AuditIndex(path).init_schema()
    AuditIndex(path).init_schema()
    with sqlite3.connect(path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(audit_events)")}
    assert {"request_id", "decision_id", "task_id", "delegation_jti", "receipt_id"} <= columns


def test_correlation_query_has_order_and_no_duplicates(tmp_path: Path) -> None:
    index = AuditIndex(tmp_path / "audit.index.db")
    shared = "corr-1"
    index.append(_make_event(1).model_copy(update={"request_id": shared, "call_id": shared}))
    index.append(_make_event(2).model_copy(update={"receipt_id": shared}))
    assert [event.seq for event in index.query_by_correlation(shared)] == [1, 2]


def test_correlation_query_expands_transitive_closure(tmp_path: Path) -> None:
    index = AuditIndex(tmp_path / "audit.index.db")
    index.append(_make_event(1).model_copy(update={"request_id": "request", "decision_id": "decision"}))
    index.append(_make_event(2).model_copy(update={"decision_id": "decision", "call_id": "call"}))
    index.append(_make_event(3).model_copy(update={"call_id": "call", "receipt_id": "receipt"}))
    assert [event.seq for event in index.query_by_correlation("receipt")] == [1, 2, 3]


def test_scoped_queries_apply_tenant_time_before_limit(tmp_path: Path) -> None:
    index = AuditIndex(tmp_path / "audit.index.db")
    base = datetime(2026, 9, 19, 9, tzinfo=UTC)
    for seq, tenant, hour in ((1, "a", 9), (2, "b", 10), (3, "a", 10), (4, "a", 11)):
        index.append(_make_event(seq).model_copy(update={
            "tenant_id": tenant,
            "timestamp": base.replace(hour=hour),
            "request_id": "corr",
            "interaction_id": f"int-{seq}",
            "metadata": {"interaction_id": f"int-{seq}", "source_agent_id": "agent",
                         "verdict": "allow"},
        }))
    scope = {"tenant_id": "a", "start_time": base.replace(hour=10),
             "end_time": base.replace(hour=11)}
    assert [e.seq for e in index.list_recent(limit=2, **scope)] == [4, 3]
    assert [e.seq for e in index.query_by_session("s1", limit=2, **scope)] == [3, 4]
    assert [e.seq for e in index.query_by_task("trace-1", limit=2, **scope)] == [3, 4]
    assert [e.seq for e in index.query_by_correlation("corr", limit=2, **scope)] == [3, 4]
    assert [e.seq for e in index.query_interactions(source_agent_id="agent", limit=2, **scope)] == [4, 3]


def test_correlation_scope_applies_after_transitive_closure(tmp_path: Path) -> None:
    index = AuditIndex(tmp_path / "audit.index.db")
    index.append(_make_event(1).model_copy(update={
        "tenant_id": "b", "request_id": "root", "decision_id": "bridge"
    }))
    index.append(_make_event(2).model_copy(update={
        "tenant_id": "a", "decision_id": "bridge"
    }))
    assert [e.seq for e in index.query_by_correlation(
        "root", tenant_id="a", limit=1
    )] == [2]


def test_rebuild_twice_and_metadata_backfill(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    event = _make_event(1, trace_id="legacy-task").model_copy(
        update={"metadata": {"request_id": "legacy-request", "decision_id": "legacy-decision"}}
    )
    path.write_text(json.dumps(event.model_dump(mode="json", exclude_none=True)) + "\n", encoding="utf-8")
    index = AuditIndex(tmp_path / "audit.index.db")
    assert index.rebuild_from_jsonl(path) == 1
    assert index.rebuild_from_jsonl(path) == 1
    assert index.query_by_correlation("legacy-request")[0].task_id == "legacy-task"
    assert index.query_by_task("legacy-task")[0].decision_id == "legacy-decision"
