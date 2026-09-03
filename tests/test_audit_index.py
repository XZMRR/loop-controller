"""AuditIndex SQLite 索引测试（v0.34.0）。"""

from __future__ import annotations

import json
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
