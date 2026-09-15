"""基于 SQLite 的对话上下文存储（v0.49.0）。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from loop_controller.infra.state_db import StateDatabase
from loop_controller.models import ConversationContext, ConversationMessage


class SqliteConversationStore:
    """基于 ``StateDatabase`` 的 ``ConversationStore``。

    消息落 ``conversations`` 表；``get_context`` 用 ``list_messages`` 按插入
    顺序返回每个 session 最近 ``max_messages_per_session`` 条，保持与 JSONL
    后端一致的 FIFO 保留语义。底层 ``StateDatabaseError`` 沿用直抛。
    """

    def __init__(self, db: StateDatabase, max_messages_per_session: int = 100) -> None:
        self._db = db
        self._max_messages = max_messages_per_session

    @classmethod
    def from_path(
        cls, path: str | Path, max_messages_per_session: int = 100
    ) -> SqliteConversationStore:
        """便捷构造：从数据库路径直接创建。"""
        return cls(StateDatabase(path), max_messages_per_session=max_messages_per_session)

    def append_message(self, message: ConversationMessage) -> None:
        self._db.save_message(message.model_dump(mode="json"))

    def get_context(self, session_id: str) -> ConversationContext:
        rows = self._db.list_messages(session_id, self._max_messages)
        messages = [ConversationMessage.model_validate(row) for row in rows]
        if messages:
            updated_at = messages[-1].created_at
        else:
            updated_at = datetime.now(UTC)
        return ConversationContext(
            session_id=session_id,
            messages=messages,
            updated_at=updated_at,
        )
