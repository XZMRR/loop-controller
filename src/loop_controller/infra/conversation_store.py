"""会话上下文存储（v0.3.0 Iteration 4）：按 session 持久化用户/Agent 消息。

``JsonlConversationStore`` 追加写入 ``conversations.jsonl``，启动时重放，
为每个 session 维护最近 N 条消息。它只被 Runtime 写入/读取，Agent 不直接访问。
"""

from __future__ import annotations

import json
import logging
from datetime import UTC
from pathlib import Path
from typing import Protocol, runtime_checkable

from loop_controller.infra.durable_io import DurableJsonlFile
from loop_controller.models import ConversationContext, ConversationMessage

logger = logging.getLogger(__name__)


@runtime_checkable
class ConversationStore(Protocol):
    """会话上下文存储协议。"""

    def append_message(self, message: ConversationMessage) -> None: ...
    def get_context(self, session_id: str) -> ConversationContext: ...


class JsonlConversationStore:
    """JSONL 持久化 ConversationStore。

    - append-only；
    - 启动时重放全部消息；
    - 每个 session 只保留最近 ``max_messages_per_session`` 条；
    - 最后一行损坏时忽略并 WARNING；
    - 遵循单进程 Runtime 假设（与 DecisionStore/RiskStateStore 一致）。
    """

    def __init__(self, path: str | Path, max_messages_per_session: int = 100) -> None:
        self._path = Path(path)
        self._durable = DurableJsonlFile(self._path)
        self._max_messages = max_messages_per_session
        self._messages: dict[str, list[ConversationMessage]] = {}
        self._load()

    def _load(self) -> None:
        """启动时重放 JSONL，恢复每个 session 的最近消息。"""
        self._messages.clear()
        if not self._path.exists():
            return
        with self._durable.transaction() as transaction:
            transaction.repair_incomplete_tail()
            raw_lines = transaction.read_complete_lines()
        for raw in raw_lines:
            data = json.loads(raw)
            self._add_to_memory(ConversationMessage(**data), persist=False)

    def _add_to_memory(
        self, message: ConversationMessage, *, persist: bool = True
    ) -> None:
        """把消息加入内存缓冲；可选同时落盘。"""
        if persist:
            self._append_to_disk(message)
        session_messages = self._messages.setdefault(message.session_id, [])
        session_messages.append(message)
        if len(session_messages) > self._max_messages:
            session_messages.pop(0)

    def _append_to_disk(self, message: ConversationMessage) -> None:
        """追加单条消息到 JSONL。"""
        with self._durable.transaction() as transaction:
            transaction.repair_incomplete_tail()
            transaction.read_complete_lines()
            transaction.append_json(message.model_dump(mode="json"))

    def append_message(self, message: ConversationMessage) -> None:
        """写入一条消息；超过上限时淘汰最旧的一条。"""
        self._add_to_memory(message, persist=True)

    def get_context(self, session_id: str) -> ConversationContext:
        """获取指定 session 的当前上下文。"""
        self._load()
        messages = list(self._messages.get(session_id, []))
        if messages:
            updated_at = messages[-1].created_at
        else:
            from datetime import datetime

            updated_at = datetime.now(UTC)
        return ConversationContext(
            session_id=session_id,
            messages=messages,
            updated_at=updated_at,
        )
