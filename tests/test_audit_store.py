"""JsonlAuditStore 哈希链与查询测试（T3.1 / A12）。"""

from __future__ import annotations

import json

import pytest

from loop_controller.infra.audit_store import JsonlAuditStore
from loop_controller.models import AuditEvent


def _make_event(seq: int = 0) -> AuditEvent:
    return AuditEvent(
        event_id="e1",
        seq=seq,
        prev_hash="",
        trace_id="t1",
        session_id="t1",
        actor_type="agent",
        actor_id="researcher_001",
        action="task_start",
    )


def test_chain_initially_passes(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    store = JsonlAuditStore(path)

    store.append(_make_event())
    store.append(_make_event())

    assert store.verify_chain()
    assert store._seq == 2


def test_seq_and_prev_hash_assigned(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    store = JsonlAuditStore(path)

    store.append(_make_event())
    store.append(_make_event())

    lines = path.read_text(encoding="utf-8").strip().split("\n")
    first = json.loads(lines[0])
    second = json.loads(lines[1])
    assert first["seq"] == 1
    assert first["prev_hash"] == "GENESIS"
    assert second["seq"] == 2
    assert second["prev_hash"] != "GENESIS"


def test_detects_deleted_line(tmp_path) -> None:
    """删除中间行会破坏后续行的 prev_hash 链接。"""
    path = tmp_path / "audit.jsonl"
    store = JsonlAuditStore(path)
    store.append(_make_event())
    store.append(_make_event())
    store.append(_make_event())

    lines = path.read_text(encoding="utf-8").strip().split("\n")
    # 删除第二行：第三行的 prev_hash 将指向不存在的行
    path.write_text("\n".join([lines[0], lines[2]]) + "\n", encoding="utf-8")

    assert not JsonlAuditStore(path).verify_chain()


def test_detects_modified_line(tmp_path) -> None:
    """修改中间行会破坏下一行的 prev_hash 链接。"""
    path = tmp_path / "audit.jsonl"
    store = JsonlAuditStore(path)
    store.append(_make_event())
    store.append(_make_event())
    store.append(_make_event())

    lines = path.read_text(encoding="utf-8").strip().split("\n")
    record = json.loads(lines[1])
    record["reason"] = "tampered"
    lines[1] = json.dumps(record, sort_keys=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert not JsonlAuditStore(path).verify_chain()


def test_detects_inserted_line(tmp_path) -> None:
    """插入一行会破坏 seq 连续性或 prev_hash 链接。"""
    path = tmp_path / "audit.jsonl"
    store = JsonlAuditStore(path)
    store.append(_make_event())
    store.append(_make_event())
    store.append(_make_event())

    lines = path.read_text(encoding="utf-8").strip().split("\n")
    fake = json.loads(lines[0])
    fake["seq"] = 3
    lines.insert(1, json.dumps(fake, sort_keys=True))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert not JsonlAuditStore(path).verify_chain()


def test_detects_swapped_lines(tmp_path) -> None:
    """交换相邻行会破坏 seq 连续性。"""
    path = tmp_path / "audit.jsonl"
    store = JsonlAuditStore(path)
    store.append(_make_event())
    store.append(_make_event())
    store.append(_make_event())

    lines = path.read_text(encoding="utf-8").strip().split("\n")
    lines[0], lines[1] = lines[1], lines[0]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert not JsonlAuditStore(path).verify_chain()


def test_resumes_chain_after_restart(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    first = JsonlAuditStore(path)
    first.append(_make_event())

    second = JsonlAuditStore(path)
    second.append(_make_event())

    assert second.verify_chain()
    lines = path.read_text(encoding="utf-8").strip().split("\n")
    assert json.loads(lines[0])["seq"] == 1
    assert json.loads(lines[1])["seq"] == 2
    assert json.loads(lines[1])["prev_hash"] != "GENESIS"


def test_query_by_trace(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    store = JsonlAuditStore(path)
    store.append(
        AuditEvent(
            event_id="e1",
            trace_id="t1",
            session_id="t1",
            actor_type="agent",
            actor_id="a1",
            action="task_start",
        )
    )
    store.append(
        AuditEvent(
            event_id="e2",
            trace_id="t2",
            session_id="t2",
            actor_type="agent",
            actor_id="a2",
            action="task_start",
        )
    )

    assert len(store.query_by_trace("t1")) == 1
    assert store.query_by_trace("t1")[0].event_id == "e1"
    assert len(store.query_by_trace("t2")) == 1
    assert len(store.query_by_trace("t3")) == 0
