"""JsonlApprovalStore 持久化、增量刷新与幂等性测试。"""

from __future__ import annotations

import json

import pytest

from loop_controller.infra.approval_store import ApprovalStoreError, JsonlApprovalStore
from loop_controller.models import ApprovalRecord, ApprovalRequest


def _make_request(decision_id: str, request_id: str = "r1") -> ApprovalRequest:
    return ApprovalRequest(
        request_id=request_id,
        decision_id=decision_id,
        call_id=f"c-{decision_id}",
        task_id="t1",
        agent_id="agent-1",
        tool_name="test_tool",
        arguments_masked={},
        tool_arguments={},
        original_decision=None,
        reason="test",
        requester_id="user-1",
        approver_id="approver-1",
    )


def _make_record(decision_id: str, verdict: str, request_id: str = "r1") -> ApprovalRecord:
    return ApprovalRecord(
        request_id=request_id,
        decision_id=decision_id,
        verdict=verdict,
        approver_id="approver-1",
        comment="test",
    )


def test_refresh_reads_new_rows(tmp_path) -> None:
    path = tmp_path / "approvals.jsonl"
    store_a = JsonlApprovalStore(path)
    request = _make_request("d1")
    store_a.submit_request(request)

    store_b = JsonlApprovalStore(path)
    record = _make_record("d1", "approve")
    store_b.record_response(record)

    assert store_a.get_record("d1") is None
    store_a.refresh()
    assert store_a.get_record("d1") == record


def test_refresh_ignores_existing_response(tmp_path) -> None:
    path = tmp_path / "approvals.jsonl"
    store_a = JsonlApprovalStore(path)
    request = _make_request("d1")
    store_a.submit_request(request)
    record1 = _make_record("d1", "approve")
    store_a.record_response(record1)

    # 直接追加一条不同 verdict 的 response，模拟其它进程写入冲突结果
    record2 = _make_record("d1", "deny")
    line = json.dumps({**record2.model_dump(mode="json"), "type": "response"}, ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)

    store_a.refresh()
    assert store_a.get_record("d1") == record1


def test_refresh_offset_after_truncation(tmp_path) -> None:
    path = tmp_path / "approvals.jsonl"
    store_a = JsonlApprovalStore(path)
    old_request = _make_request("old")
    store_a.submit_request(old_request)
    store_a.refresh()

    # 外部进程清空文件并写入一个明显更短的新请求，确保 size < _read_offset 触发全量重放
    new_request = _make_request("n", request_id="r2")
    line = json.dumps({**new_request.model_dump(mode="json"), "type": "request"}, ensure_ascii=False) + "\n"
    path.write_text(line)

    store_a.refresh()
    assert store_a.get_request("old") is None
    assert store_a.get_request("n") == new_request


def test_refresh_tail_partial_line_warning(tmp_path, caplog) -> None:
    path = tmp_path / "approvals.jsonl"
    store_a = JsonlApprovalStore(path)
    request = _make_request("d1")
    store_a.submit_request(request)
    store_a.refresh()

    # 追加一条不完整的半行
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"type": "response", "decision_id": "d1"')

    store_a.refresh()
    assert store_a.get_request("d1") == request
    assert "不完整" in caplog.text


def test_record_response_rejects_overwrite(tmp_path) -> None:
    path = tmp_path / "approvals.jsonl"
    store = JsonlApprovalStore(path)
    request = _make_request("d1")
    store.submit_request(request)
    store.record_response(_make_record("d1", "approve"))

    with pytest.raises(ApprovalStoreError, match="已有审批结果"):
        store.record_response(_make_record("d1", "deny"))


def test_record_response_idempotent(tmp_path) -> None:
    path = tmp_path / "approvals.jsonl"
    store = JsonlApprovalStore(path)
    request = _make_request("d1")
    store.submit_request(request)
    record = _make_record("d1", "approve")
    store.record_response(record)

    # 相同内容再次写入不应抛异常
    store.record_response(record)
    assert store.get_record("d1") == record


def test_replay_keeps_first_response(tmp_path) -> None:
    path = tmp_path / "approvals.jsonl"
    record1 = _make_record("d1", "approve")
    record2 = _make_record("d1", "deny")

    line1 = json.dumps({**record1.model_dump(mode="json"), "type": "response"}, ensure_ascii=False) + "\n"
    line2 = json.dumps({**record2.model_dump(mode="json"), "type": "response"}, ensure_ascii=False) + "\n"

    with path.open("w", encoding="utf-8") as fh:
        fh.write(line1)
        fh.write(line2)

    store = JsonlApprovalStore(path)
    assert store.get_record("d1") == record1
