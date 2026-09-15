"""Python 工具审批可靠 webhook dispatcher 专项测试。"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from loop_controller.approval_webhook import ApprovalWebhookDispatcher
from loop_controller.infra.approval_store import SqliteApprovalStore, build_approval_store
from loop_controller.infra.config_loader import ApprovalWebhookConfig
from loop_controller.models import ApprovalRequest, Decision


def _request() -> ApprovalRequest:
    return ApprovalRequest(
        request_id="request-1",
        decision_id="decision-1",
        call_id="call-1",
        task_id="task-1",
        agent_id="agent-1",
        tool_name="send_email",
        arguments_masked={"to": "masked@example.com"},
        tool_arguments={"password": "plaintext-secret"},
        original_decision=Decision(
            decision_id="decision-1",
            call_id="call-1",
            task_id="task-1",
            verdict="require_approval",
            reason="risk",
            policy_hits=[],
            policy_version="v1",
            profile_version="v1",
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        ),
        reason="risk",
        requester_id="alice",
        approver_id="manager",
    )


def _config(**overrides: object) -> ApprovalWebhookConfig:
    values = {
        "enabled": True,
        "url": "https://notify.example/approvals",
        "auth_header_name": "X-Webhook-Key",
        "auth_header_value": "auth-secret",
        "retry_base_seconds": 60.0,
        "retry_max_seconds": 300.0,
    }
    values.update(overrides)
    return ApprovalWebhookConfig(**values)


@pytest.mark.asyncio
async def test_success_auth_header_and_redacted_payload(tmp_path) -> None:
    received: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received.append(request)
        return httpx.Response(204)

    store = SqliteApprovalStore(tmp_path / "approvals.db", notification_destination="webhook")
    store.submit_request(_request())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        dispatcher = ApprovalWebhookDispatcher(store, _config(), client=client)
        assert await dispatcher.dispatch_once() == 1

    assert received[0].headers["X-Webhook-Key"] == "auth-secret"
    body = json.loads(received[0].content)
    assert body["delivery_id"]
    assert body["tool_name"] == "send_email"
    assert "plaintext-secret" not in received[0].content.decode()
    assert "tool_arguments" not in body and "arguments_masked" not in body
    assert store.list_notifications()[0]["delivered_at"] is not None


@pytest.mark.asyncio
async def test_failure_exponential_retry_then_success(tmp_path) -> None:
    statuses = iter([500, 502, 204])
    store = SqliteApprovalStore(tmp_path / "approvals.db", notification_destination="webhook")
    store.submit_request(_request())
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(next(statuses)))
    ) as client:
        dispatcher = ApprovalWebhookDispatcher(store, _config(), client=client)
        await dispatcher.dispatch_once()
        first = store.list_notifications()[0]
        assert first["attempts"] == 1 and first["last_error"]

        first_retry = datetime.fromisoformat(first["next_attempt_at"])
        claimed = store.claim_notifications(now=first_retry, claim_token="manual")
        assert claimed[0]["attempts"] == 2
        await dispatcher._deliver(claimed[0], "manual")
        second = store.list_notifications()[0]
        second_retry = datetime.fromisoformat(second["next_attempt_at"])
        assert second_retry > first_retry

        claimed = store.claim_notifications(now=second_retry, claim_token="final")
        await dispatcher._deliver(claimed[0], "final")
    assert store.list_notifications()[0]["delivered_at"] is not None


@pytest.mark.asyncio
async def test_pause_and_restart_recover_pending_delivery(tmp_path) -> None:
    path = tmp_path / "approvals.db"
    paused = build_approval_store(path, notification_destination=None)
    assert isinstance(paused, SqliteApprovalStore)
    paused.submit_request(_request())
    assert paused.list_notifications() == []

    queued = SqliteApprovalStore(path, notification_destination="webhook")
    request = _request().model_copy(update={"request_id": "request-2", "decision_id": "decision-2"})
    request = request.model_copy(
        update={"original_decision": request.original_decision.model_copy(update={"decision_id": "decision-2"})}
    )
    queued.submit_request(request)
    claimed = queued.claim_notifications(lease_seconds=0.01, claim_token="crashed")
    assert len(claimed) == 1
    await asyncio.sleep(0.02)

    restarted = SqliteApprovalStore(path, notification_destination="webhook")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200))
    ) as client:
        dispatcher = ApprovalWebhookDispatcher(restarted, _config(), client=client)
        assert await dispatcher.dispatch_once() == 1
    row = restarted.list_notifications()[0]
    assert row["attempts"] == 2 and row["delivered_at"] is not None
