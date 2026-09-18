"""SQLite 审批通知 outbox 的可靠 webhook dispatcher。"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx

from loop_controller.infra.approval_store import ApprovalStoreError, SqliteApprovalStore
from loop_controller.infra.config_loader import ApprovalWebhookConfig

logger = logging.getLogger("loop_controller.approval_webhook")


class ApprovalWebhookDispatcher:
    def __init__(
        self,
        store: SqliteApprovalStore,
        config: ApprovalWebhookConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._store = store
        self._config = config
        self._client = client
        self._owns_client = client is None
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if not self._config.enabled or self._task is not None:
            return
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._config.timeout_seconds)
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="approval-webhook-dispatcher")

    async def stop(self) -> None:
        task, self._task = self._task, None
        self._stop.set()
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def dispatch_once(self) -> int:
        if not self._config.enabled:
            return 0
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._config.timeout_seconds)
        token = uuid.uuid4().hex
        rows = self._store.claim_notifications(
            limit=self._config.batch_size,
            lease_seconds=self._config.lease_seconds,
            claim_token=token,
            destination="webhook",
        )
        for row in rows:
            await self._deliver(row, token)
        return len(rows)

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                claimed = await self.dispatch_once()
            except ApprovalStoreError as exc:
                logger.warning("claim 审批 webhook 通知失败: %s", exc)
                claimed = 0
            if claimed == 0:
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self._config.poll_interval_seconds
                    )
                except TimeoutError:
                    pass

    async def _deliver(self, row: dict[str, Any], claim_token: str) -> None:
        delivery_id = str(row["delivery_id"])
        body = {"delivery_id": delivery_id, **cast(dict[str, Any], row["payload"])}
        headers = {"Content-Type": "application/json"}
        if self._config.auth_header_value:
            headers[self._config.auth_header_name] = self._config.auth_header_value
        try:
            assert self._client is not None
            response = await self._client.post(
                self._config.url,
                json=body,
                headers=headers,
                timeout=self._config.timeout_seconds,
            )
            response.raise_for_status()
        except (httpx.HTTPError, RuntimeError) as exc:
            attempts = int(row["attempts"])
            delay = min(
                self._config.retry_max_seconds,
                self._config.retry_base_seconds * (2 ** max(0, attempts - 1)),
            )
            accepted = self._store.fail_notification(
                delivery_id,
                claim_token,
                next_attempt_at=datetime.now(UTC) + timedelta(seconds=delay),
                last_error=str(exc)[:1000],
            )
            if not accepted:
                logger.warning("审批 webhook 失败结果因 claim fencing 被拒绝: %s", delivery_id)
            return
        if not self._store.ack_notification(delivery_id, claim_token):
            logger.warning("审批 webhook ACK 因 claim fencing 被拒绝: %s", delivery_id)
