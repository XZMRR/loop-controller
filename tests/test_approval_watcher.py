"""ApprovalWatcher 测试（v0.19.0）。"""

from __future__ import annotations

import asyncio

import pytest

from loop_controller.approval_watcher import ApprovalWatcher


@pytest.mark.asyncio
async def test_wait_notified() -> None:
    watcher = ApprovalWatcher()
    task = asyncio.create_task(watcher.wait("req-1", timeout=5.0))
    await asyncio.sleep(0.05)
    watcher.notify("req-1")
    result = await task
    assert result is True


@pytest.mark.asyncio
async def test_wait_timeout() -> None:
    watcher = ApprovalWatcher()
    result = await watcher.wait("req-1", timeout=0.1)
    assert result is False


@pytest.mark.asyncio
async def test_multiple_waiters_same_request() -> None:
    watcher = ApprovalWatcher()
    tasks = [
        asyncio.create_task(watcher.wait("req-1", timeout=5.0)),
        asyncio.create_task(watcher.wait("req-1", timeout=5.0)),
    ]
    await asyncio.sleep(0.05)
    watcher.notify("req-1")
    results = await asyncio.gather(*tasks)
    assert all(results)


@pytest.mark.asyncio
async def test_notify_only_wakes_matching_request() -> None:
    watcher = ApprovalWatcher()
    task1 = asyncio.create_task(watcher.wait("req-1", timeout=5.0))
    task2 = asyncio.create_task(watcher.wait("req-2", timeout=5.0))
    await asyncio.sleep(0.05)
    watcher.notify("req-1")
    r1 = await task1
    assert r1 is True
    watcher.notify("req-2")
    r2 = await task2
    assert r2 is True
