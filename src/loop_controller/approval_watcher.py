"""审批事件通知器（v0.19.0）。

为 SSE 与 gRPC server-streaming 提供统一的等待/通知抽象。
审批权威状态仍在 ApprovalStore；本模块只负责在状态可能变化时唤醒 waiter。
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("loop_controller.approval_watcher")


class ApprovalWatcher:
    """基于 asyncio.Event 的按 request_id 等待器。

    用法：

        watcher = ApprovalWatcher()

        # waiter
        await watcher.wait("req-1", timeout=60.0)

        # notifier（在 approve/deny 后调用）
        watcher.notify("req-1")
    """

    def __init__(self) -> None:
        self._events: dict[str, list[asyncio.Event]] = {}

    def notify(self, request_id: str) -> None:
        """唤醒所有等待 ``request_id`` 的 waiter。"""
        events = self._events.pop(request_id, [])
        for event in events:
            event.set()
        if events:
            logger.debug("notified %d waiter(s) for request_id=%s", len(events), request_id)

    async def wait(self, request_id: str, timeout: float | None = None) -> bool:
        """等待 ``request_id`` 被通知或超时。

        Returns:
            True 表示被 notify 唤醒；False 表示超时。
        """
        event = asyncio.Event()
        self._events.setdefault(request_id, []).append(event)
        try:
            if timeout is None:
                await event.wait()
                return True
            try:
                await asyncio.wait_for(event.wait(), timeout=timeout)
                return True
            except TimeoutError:
                return False
        finally:
            events = self._events.get(request_id, [])
            try:
                events.remove(event)
            except ValueError:
                pass
            if not events and request_id in self._events:
                del self._events[request_id]
