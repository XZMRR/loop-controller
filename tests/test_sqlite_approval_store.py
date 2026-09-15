"""SQLite ApprovalStore 与原子 notification outbox 专项测试。"""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from loop_controller.infra.approval_crypto import ApprovalCrypto
from loop_controller.infra.approval_store import (
    ApprovalStoreError,
    JsonlApprovalStore,
    SqliteApprovalStore,
    build_approval_store,
)
from loop_controller.models import ApprovalRecord, ApprovalRequest, Decision


def _request(
    decision_id: str = "d1", request_id: str = "r1", created_at: datetime | None = None
) -> ApprovalRequest:
    decision = Decision(
        decision_id=decision_id,
        call_id=f"c-{decision_id}",
        task_id="t1",
        verdict="require_approval",
        reason="risk",
        policy_hits=["sensitive-policy"],
        policy_version="v1",
        profile_version="v1",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    return ApprovalRequest(
        request_id=request_id,
        decision_id=decision_id,
        call_id=f"c-{decision_id}",
        task_id="t1",
        agent_id="agent-1",
        tool_name="send_email",
        arguments_masked={"to": "a@example.com"},
        tool_arguments={"secret_token": "sqlite-secret"},
        original_decision=decision,
        reason="risk",
        requester_id="alice",
        approver_id="manager",
        created_at=created_at or datetime.now(UTC),
    )


def _response(
    verdict: str = "approve", decision_id: str = "d1", request_id: str = "r1"
) -> ApprovalRecord:
    return ApprovalRecord(
        request_id=request_id,
        decision_id=decision_id,
        verdict=verdict,
        approver_id="manager",
        comment="done",
    )


def test_store_factory_selects_backend_without_dual_write(tmp_path) -> None:
    sqlite_path = tmp_path / "approval.db"
    jsonl_path = tmp_path / "approval.jsonl"
    sqlite_store = build_approval_store(sqlite_path)
    assert isinstance(sqlite_store, SqliteApprovalStore)
    sqlite_store.submit_request(_request())
    assert sqlite_path.exists()
    assert not jsonl_path.exists()
    assert isinstance(build_approval_store(jsonl_path), JsonlApprovalStore)


def test_persistence_encryption_idempotency_and_restart(tmp_path) -> None:
    path = tmp_path / "approvals.db"
    crypto = ApprovalCrypto(os.urandom(32))
    store = SqliteApprovalStore(path, crypto=crypto, notification_destination="webhook")
    request = _request()

    store.submit_request(request)
    store.submit_request(request)
    assert b"sqlite-secret" not in path.read_bytes()
    assert b"sensitive-policy" not in path.read_bytes()
    assert len(store.list_notifications()) == 1

    restarted = SqliteApprovalStore(
        path, crypto=crypto, notification_destination="webhook"
    )
    assert restarted.get_request("d1") == request
    assert restarted.get_request_by_id("r1") == request
    assert restarted.get_pending() == [request]

    response = _response()
    restarted.record_response(response)
    restarted.record_response(response)
    final = SqliteApprovalStore(
        path, crypto=crypto, notification_destination="webhook"
    )
    assert final.get_record("d1") == response
    assert final.get_pending() == []
    notifications = final.list_notifications()
    assert [row["event_type"] for row in notifications] == ["created", "approved"]
    assert len({row["delivery_id"] for row in notifications}) == 2

    with pytest.raises(ApprovalStoreError, match="已有审批结果"):
        final.record_response(_response("deny"))


def test_sqlite_list_recent_includes_pending_and_terminal(tmp_path) -> None:
    store = SqliteApprovalStore(tmp_path / "approvals.db", notification_destination=None)
    older = _request("d1", "r1")
    newer = _request("d2", "r2", older.created_at + timedelta(seconds=1))
    store.submit_request(older)
    store.submit_request(newer)
    store.record_response(_response("deny", "d1", "r1"))

    recent = store.list_recent(limit=2)
    assert [item["decision_id"] for item in recent] == ["d2", "d1"]
    assert [item["status"] for item in recent] == ["pending", "deny"]


def test_request_and_outbox_are_atomic(tmp_path) -> None:
    path = tmp_path / "approvals.db"
    store = SqliteApprovalStore(path, notification_destination="webhook")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TRIGGER reject_approval_outbox BEFORE INSERT ON approval_notification_outbox "
            "BEGIN SELECT RAISE(ABORT, 'outbox failure'); END"
        )

    with pytest.raises(ApprovalStoreError, match="outbox failure"):
        store.submit_request(_request())
    assert store.get_request("d1") is None
    assert store.list_notifications() == []


def test_claim_lease_attempts_retry_and_ack_fencing(tmp_path) -> None:
    store = SqliteApprovalStore(
        tmp_path / "approvals.sqlite", notification_destination="webhook"
    )
    store.submit_request(_request())
    now = datetime.now(UTC)

    first = store.claim_notifications(now=now, lease_seconds=10, claim_token="old")
    assert len(first) == 1
    assert first[0]["attempts"] == 1
    delivery_id = first[0]["delivery_id"]
    assert store.claim_notifications(now=now, claim_token="other") == []

    reclaimed = store.claim_notifications(
        now=now + timedelta(seconds=11), lease_seconds=10, claim_token="new"
    )
    assert reclaimed[0]["delivery_id"] == delivery_id
    assert reclaimed[0]["attempts"] == 2
    assert store.ack_notification(delivery_id, "old") is False

    retry_at = now + timedelta(minutes=1)
    assert store.fail_notification(
        delivery_id, "new", next_attempt_at=retry_at, last_error="temporary"
    )
    assert store.claim_notifications(now=retry_at - timedelta(seconds=1)) == []
    third = store.claim_notifications(now=retry_at, claim_token="final")
    assert third[0]["attempts"] == 3
    assert third[0]["last_error"] == "temporary"
    assert store.ack_notification(delivery_id, "wrong") is False
    assert store.ack_notification(delivery_id, "final") is True
    assert store.claim_notifications(now=retry_at + timedelta(hours=1)) == []
