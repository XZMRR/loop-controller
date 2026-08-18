"""build_governance_context 单元测试（v0.3.0 Iteration 4）。"""

from __future__ import annotations

import pytest

from loop_controller.governance_context import build_context_meta, build_governance_context
from loop_controller.models import ConversationContext, ConversationMessage, Task


@pytest.fixture
def task() -> Task:
    return Task(
        task_id="t1",
        session_id="s1",
        user_id="alice",
        agent_id="researcher_001",
        description="写一份 AI 合规报告",
    )


def test_task_description_always_first(task: Task) -> None:
    ctx = ConversationContext(session_id="s1")
    text = build_governance_context(task, ctx)
    assert text.startswith("当前任务：写一份 AI 合规报告")


def test_includes_recent_user_messages(task: Task) -> None:
    messages = [
        ConversationMessage(message_id="m1", session_id="s1", task_id="t1", role="user", content="主题是什么？"),
        ConversationMessage(message_id="m2", session_id="s1", task_id="t1", role="user", content="GDPR"),
    ]
    ctx = ConversationContext(session_id="s1", messages=messages)
    text = build_governance_context(task, ctx)
    assert "主题是什么？" in text
    assert "GDPR" in text


def test_only_current_task_messages(task: Task) -> None:
    messages = [
        ConversationMessage(message_id="m1", session_id="s1", task_id="t1", role="user", content="当前任务消息"),
        ConversationMessage(message_id="m2", session_id="s1", task_id="t2", role="user", content="其他任务消息"),
    ]
    ctx = ConversationContext(session_id="s1", messages=messages)
    text = build_governance_context(task, ctx)
    assert "当前任务消息" in text
    assert "其他任务消息" not in text


def test_user_message_limit(task: Task) -> None:
    messages = [
        ConversationMessage(message_id=f"m{i}", session_id="s1", task_id="t1", role="user", content=f"u{i}")
        for i in range(10)
    ]
    ctx = ConversationContext(session_id="s1", messages=messages)
    text = build_governance_context(task, ctx, max_user_messages=5)
    assert "u0" not in text
    assert "u4" not in text
    assert "u5" in text
    assert "u9" in text


def test_agent_message_limit(task: Task) -> None:
    messages = [
        ConversationMessage(message_id=f"m{i}", session_id="s1", task_id="t1", role="agent", content=f"a{i}")
        for i in range(10)
    ]
    ctx = ConversationContext(session_id="s1", messages=messages)
    text = build_governance_context(task, ctx, max_agent_messages=3)
    assert "a0" not in text
    assert "a6" not in text
    assert "a7" in text
    assert "a9" in text


def test_truncation_annotation(task: Task) -> None:
    messages = [
        ConversationMessage(message_id="m1", session_id="s1", task_id="t1", role="user", content="x" * 5000),
    ]
    ctx = ConversationContext(session_id="s1", messages=messages)
    text = build_governance_context(task, ctx, r2_limit=100)
    assert "[truncated," in text
    assert len(text) <= 140


def test_context_meta(task: Task) -> None:
    messages = [
        ConversationMessage(message_id="m1", session_id="s1", task_id="t1", role="user", content="u1"),
    ]
    ctx = ConversationContext(session_id="s1", messages=messages)
    gov = build_governance_context(task, ctx)
    meta = build_context_meta(task, ctx, gov)
    assert meta["session_id"] == "s1"
    assert meta["message_count"] == 1
    assert meta["context_length"] == len(gov)
    assert isinstance(meta["context_hash"], str)
    assert len(meta["context_hash"]) == 64
