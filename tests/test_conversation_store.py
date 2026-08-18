"""ConversationStore 单元测试（v0.3.0 Iteration 4）：持久化、重放、FIFO。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from loop_controller.infra.conversation_store import JsonlConversationStore
from loop_controller.models import ConversationMessage


@pytest.fixture
def store(tmp_path: Path) -> JsonlConversationStore:
    return JsonlConversationStore(tmp_path / "conversations.jsonl", max_messages_per_session=3)


def test_empty_context(store: JsonlConversationStore) -> None:
    ctx = store.get_context("s1")
    assert ctx.session_id == "s1"
    assert ctx.messages == []


def test_append_and_retrieve(store: JsonlConversationStore) -> None:
    m1 = ConversationMessage(message_id="m1", session_id="s1", task_id="t1", role="user", content="hello")
    m2 = ConversationMessage(message_id="m2", session_id="s1", task_id="t1", role="agent", content="hi")
    store.append_message(m1)
    store.append_message(m2)

    ctx = store.get_context("s1")
    assert [m.message_id for m in ctx.messages] == ["m1", "m2"]


def test_session_isolation(store: JsonlConversationStore) -> None:
    store.append_message(
        ConversationMessage(message_id="m1", session_id="s1", task_id="t1", role="user", content="a")
    )
    store.append_message(
        ConversationMessage(message_id="m2", session_id="s2", task_id="t2", role="user", content="b")
    )

    assert len(store.get_context("s1").messages) == 1
    assert len(store.get_context("s2").messages) == 1


def test_fifo_eviction(store: JsonlConversationStore) -> None:
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
    path = tmp_path / "conversations.jsonl"
    store1 = JsonlConversationStore(path, max_messages_per_session=10)
    m1 = ConversationMessage(message_id="m1", session_id="s1", task_id="t1", role="user", content="u1")
    m2 = ConversationMessage(message_id="m2", session_id="s1", task_id="t1", role="agent", content="a1")
    store1.append_message(m1)
    store1.append_message(m2)

    store2 = JsonlConversationStore(path, max_messages_per_session=10)
    ctx = store2.get_context("s1")
    assert len(ctx.messages) == 2
    assert ctx.messages[0].content == "u1"
    assert ctx.messages[1].role == "agent"


def test_replay_ignores_corrupted_last_line(tmp_path: Path) -> None:
    path = tmp_path / "conversations.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    m1 = ConversationMessage(message_id="m1", session_id="s1", task_id="t1", role="user", content="u1")
    path.write_text(
        m1.model_dump_json() + "\n{not valid json",
        encoding="utf-8",
    )
    store = JsonlConversationStore(path, max_messages_per_session=10)
    assert len(store.get_context("s1").messages) == 1


def test_file_contains_only_valid_lines(store: JsonlConversationStore, tmp_path: Path) -> None:
    m1 = ConversationMessage(message_id="m1", session_id="s1", task_id="t1", role="user", content="u1")
    store.append_message(m1)

    lines = [
        json.loads(line)
        for line in (tmp_path / "conversations.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(lines) == 1
    assert lines[0]["message_id"] == "m1"
    assert lines[0]["role"] == "user"
