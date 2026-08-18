"""会话上下文存储（v0.3.0 Iteration 4）：按 session 持久化用户/Agent 消息。

``JsonlConversationStore`` 追加写入 ``conversations.jsonl``，启动时重放，
为每个 session 维护最近 N 条消息。它只被 Runtime 写入/读取，Agent 不直接访问。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Protocol, runtime_checkable

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
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._max_messages = max_messages_per_session
        self._messages: dict[str, list[ConversationMessage]] = {}
        self._load()

    def _load(self) -> None:
        """启动时重放 JSONL，恢复每个 session 的最近消息。"""
        if not self._path.exists():
            return
        raw_lines = self._path.read_text(encoding="utf-8").splitlines(keepends=True)
        for line_no, raw in enumerate(raw_lines, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                message = ConversationMessage(**data)
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                is_last = line_no == len(raw_lines)
                if is_last:
                    logger.warning(
                        "conversations.jsonl 末行（第 %d 行）不完整，已忽略：%s",
                        line_no,
                        exc,
                    )
                else:
                    logger.warning(
                        "conversations.jsonl 第 %d 行解析失败（%s），已忽略",
                        line_no,
                        exc,
                    )
                continue
            self._add_to_memory(message, persist=False)

    def _add_to_memory(
        self, message: ConversationMessage, *, persist: bool = True
    ) -> None:
        """把消息加入内存缓冲；可选同时落盘。"""
        session_messages = self._messages.setdefault(message.session_id, [])
        session_messages.append(message)
        if len(session_messages) > self._max_messages:
            session_messages.pop(0)
        if persist:
            self._append_to_disk(message)

    def _append_to_disk(self, message: ConversationMessage) -> None:
        """追加单条消息到 JSONL。"""
        line = message.model_dump_json() + "\n"
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()

    def append_message(self, message: ConversationMessage) -> None:
        """写入一条消息；超过上限时淘汰最旧的一条。"""
        self._add_to_memory(message, persist=True)

    def get_context(self, session_id: str) -> ConversationContext:
        """获取指定 session 的当前上下文。"""
        messages = list(self._messages.get(session_id, []))
        if messages:
            updated_at = messages[-1].created_at
        else:
            from datetime import datetime, timezone

            updated_at = datetime.now(timezone.utc)
        return ConversationContext(
            session_id=session_id,
            messages=messages,
            updated_at=updated_at,
        )
