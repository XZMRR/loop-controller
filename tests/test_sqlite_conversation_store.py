"""SqliteConversationStore 持久化测试（v0.49.0）。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from loop_controller.infra.sqlite_conversation_store import SqliteConversationStore
from loop_controller.models import ConversationMessage


@pytest.fixture
def store(tmp_path: Path) -> SqliteConversationStore:
    return SqliteConversationStore.from_path(
        tmp_path / "conversations.db", max_messages_per_session=3
    )


def test_empty_context(store: SqliteConversationStore) -> None:
    ctx = store.get_context("s1")
    assert ctx.session_id == "s1"
    assert ctx.messages == []


def test_append_and_retrieve(store: SqliteConversationStore) -> None:
    store.append_message(
        ConversationMessage(message_id="m1", session_id="s1", task_id="t1", role="user", content="hello")
    )
    store.append_message(
        ConversationMessage(message_id="m2", session_id="s1", task_id="t1", role="agent", content="hi")
    )
    ctx = store.get_context("s1")
    assert [m.message_id for m in ctx.messages] == ["m1", "m2"]


def test_session_isolation(store: SqliteConversationStore) -> None:
    store.append_message(
        ConversationMessage(message_id="m1", session_id="s1", task_id="t1", role="user", content="a")
    )
    store.append_message(
        ConversationMessage(message_id="m2", session_id="s2", task_id="t2", role="user", content="b")
    )
    assert len(store.get_context("s1").messages) == 1
    assert len(store.get_context("s2").messages) == 1


def test_fifo_eviction(store: SqliteConversationStore) -> None:
    for i in range(5):
        store.append_message(
            ConversationMessage(
                message_id=f"m{i}",
                session_id="s1",
                task_id="t1",
                role="user",
                content=str(i),
            )
        )
    ctx = store.get_context("s1")
    assert len(ctx.messages) == 3
    assert [m.message_id for m in ctx.messages] == ["m2", "m3", "m4"]


def test_persistence_and_replay(tmp_path: Path) -> None:
    path = tmp_path / "conversations.db"
    store1 = SqliteConversationStore.from_path(path, max_messages_per_session=10)
    store1.append_message(
        ConversationMessage(message_id="m1", session_id="s1", task_id="t1", role="user", content="u1")
    )
    store1.append_message(
        ConversationMessage(message_id="m2", session_id="s1", task_id="t1", role="agent", content="a1")
    )

    store2 = SqliteConversationStore.from_path(path, max_messages_per_session=10)
    ctx = store2.get_context("s1")
    assert len(ctx.messages) == 2
    assert ctx.messages[0].content == "u1"
    assert ctx.messages[1].role == "agent"


def test_datetime_roundtrip(store: SqliteConversationStore) -> None:
    created = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    store.append_message(
        ConversationMessage(
            message_id="m1",
            session_id="s1",
            task_id="t1",
            role="user",
            content="hello",
            created_at=created,
        )
    )
    ctx = store.get_context("s1")
    assert ctx.messages[0].created_at == created
    assert ctx.updated_at == created
