"""AuthorityToken 持久化存储（v0.11.0）。

``InMemoryAuthorityStore`` 为默认实现，适合测试与单进程内存场景；
``JsonlAuthorityStore`` 基于 append-only JSONL，支持 Runtime 重启后恢复。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from loop_controller.models import AuthorityToken

logger = logging.getLogger(__name__)

PathLike = str | Path


class AuthorityStoreError(Exception):
    """AuthorityStore 损坏或操作失败时抛出（fail-closed）。"""


class AuthorityStore(Protocol):
    """AuthorityToken 持久化存储协议。"""

    def save(self, token: AuthorityToken, event_type: str) -> None: ...
    def get(self, token_id: str) -> AuthorityToken | None: ...
    def list_all(self) -> list[AuthorityToken]: ...
    def list_active(self) -> list[AuthorityToken]: ...
    def list_by_task(self, task_id: str) -> list[AuthorityToken]: ...


class InMemoryAuthorityStore:
    """内存版 AuthorityStore；进程重启丢失。"""

    def __init__(self) -> None:
        self._tokens: dict[str, AuthorityToken] = {}
        self._task_index: dict[str, set[str]] = {}

    def save(self, token: AuthorityToken, event_type: str) -> None:
        self._tokens[token.token_id] = token
        self._task_index.setdefault(token.task_id, set()).add(token.token_id)

    def get(self, token_id: str) -> AuthorityToken | None:
        return self._tokens.get(token_id)

    def list_all(self) -> list[AuthorityToken]:
        return list(self._tokens.values())

    def list_active(self) -> list[AuthorityToken]:
        now = datetime.now(UTC)
        return [t for t in self._tokens.values() if _is_active(t, now)]

    def list_by_task(self, task_id: str) -> list[AuthorityToken]:
        return [
            self._tokens[tid]
            for tid in self._task_index.get(task_id, set())
            if tid in self._tokens
        ]


@dataclass
class JsonlAuthorityStore:
    """基于 JSONL 的 AuthorityToken 持久化存储。

    事件类型：
    - ``token_created``：创建 token；
    - ``token_used``：使用 token 并扣减预算；
    - ``token_revoked``：撤销 token；
    - ``token_expired``：标记 token 过期（运行期清理时写入）。

    启动时重放所有事件恢复内存索引。
    """

    path: PathLike
    _path: Path = field(init=False, repr=False)
    _by_id: dict[str, AuthorityToken] = field(init=False, repr=False, default_factory=dict)
    _by_task: dict[str, set[str]] = field(init=False, repr=False, default_factory=dict)

    def __post_init__(self) -> None:
        self._path = Path(str(self.path))
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._replay()

    def save(self, token: AuthorityToken, event_type: str) -> None:
        """持久化 token 状态变更。"""
        record = token.model_dump(mode="json")
        record["type"] = event_type
        record["timestamp"] = datetime.now(UTC).isoformat()
        self._append(record)
        self._update_indices(token)

    def get(self, token_id: str) -> AuthorityToken | None:
        """按 token_id 读取最新状态。"""
        return self._by_id.get(token_id)

    def list_all(self) -> list[AuthorityToken]:
        """列出所有 token（含过期/撤销）。"""
        return list(self._by_id.values())

    def list_active(self) -> list[AuthorityToken]:
        """列出当前所有有效 token。"""
        now = datetime.now(UTC)
        return [t for t in self._by_id.values() if _is_active(t, now)]

    def list_by_task(self, task_id: str) -> list[AuthorityToken]:
        """按 task_id 列出所有 token（含过期/撤销）。"""
        return [
            self._by_id[tid]
            for tid in self._by_task.get(task_id, set())
            if tid in self._by_id
        ]

    def _append(self, record: dict) -> None:
        try:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                fh.flush()
        except OSError as exc:
            raise AuthorityStoreError(f"无法写入 AuthorityStore {self._path}: {exc}") from exc

    def _update_indices(self, token: AuthorityToken) -> None:
        self._by_id[token.token_id] = token
        self._by_task.setdefault(token.task_id, set()).add(token.token_id)

    def _replay(self) -> None:
        """启动时重放事件恢复状态。"""
        if not self._path.exists():
            return
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise AuthorityStoreError(f"无法读取 AuthorityStore {self._path}: {exc}") from exc

        for lineno, line in enumerate(lines, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AuthorityStoreError(
                    f"AuthorityStore {self._path} 第 {lineno} 行非法 JSON: {exc}"
                ) from exc

            event_type = record.get("type")
            record.pop("type", None)
            record.pop("timestamp", None)
            token = AuthorityToken.model_validate(record)

            if event_type == "token_created":
                self._update_indices(token)
            elif event_type in ("token_used", "token_revoked", "token_expired"):
                self._update_indices(token)
            else:
                logger.warning("未知的 authority 事件类型 %s，跳过", event_type)


def _is_active(token: AuthorityToken, now: datetime) -> bool:
    """token 未撤销且未过期。"""
    if token.revoked_at is not None:
        return False
    return now < token.expires_at
