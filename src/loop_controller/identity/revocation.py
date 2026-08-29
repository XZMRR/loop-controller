"""全局吊销列表与 Kill Switch。"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from loop_controller.identity.models import AgentIdentity
from loop_controller.infra.durable_io import durable_atomic_replace, durable_locked_read


class RevocationType(StrEnum):
    AGENT = "agent"
    USER = "user"
    TOOL = "tool"
    SECRET = "secret"


class RevocationEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: RevocationType
    id: str
    reason: str = ""
    revoked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime | None = None
    tenant_id: str | None = None

    @field_validator("revoked_at", "expires_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("datetime must include timezone")
        return value.astimezone(UTC)


class KillSwitchConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    enabled: bool = False
    reason: str = ""
    except_tools: list[str] = Field(default_factory=list)
    except_agents: list[str] = Field(default_factory=list)


class RevocationMatch(BaseModel):
    """一次结构化吊销匹配结果。"""

    model_config = ConfigDict(frozen=True)

    revoked: bool
    reason: str | None = None
    type: RevocationType | Literal["kill_switch"] | None = None
    id: str | None = None


class RevocationList:
    """线程安全的内存吊销快照，可选同步持久化到 YAML。"""

    def __init__(
        self,
        entries: list[RevocationEntry] | None = None,
        kill_switch: KillSwitchConfig | None = None,
        path: str | Path | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._entries: dict[tuple[RevocationType, str, str | None], RevocationEntry] = {}
        self._kill_switch = kill_switch or KillSwitchConfig()
        self._path = Path(path) if path is not None else None
        for entry in entries or []:
            self._entries[self._key(entry.type, entry.id, entry.tenant_id)] = entry

    @classmethod
    def from_file(cls, path: str | Path) -> RevocationList:
        target = Path(path)
        current = durable_locked_read(target)
        if current is None:
            return cls(path=target)
        entries, kill_switch = cls._parse(current)
        return cls(entries=entries, kill_switch=kill_switch, path=target)

    @staticmethod
    def _parse(content: bytes) -> tuple[list[RevocationEntry], KillSwitchConfig]:
        data = yaml.safe_load(content)
        if not isinstance(data, dict) or not data:
            raise ValueError("revocation config must be a non-empty mapping")
        return (
            [RevocationEntry.model_validate(item) for item in data.get("revocations", [])],
            KillSwitchConfig.model_validate(data.get("kill_switch") or {}),
        )

    @staticmethod
    def _key(
        entry_type: RevocationType, entry_id: str, tenant_id: str | None
    ) -> tuple[RevocationType, str, str | None]:
        return entry_type, entry_id, tenant_id

    @property
    def entries(self) -> list[RevocationEntry]:
        with self._lock:
            return list(self._entries.values())

    @property
    def kill_switch(self) -> KillSwitchConfig:
        with self._lock:
            return self._kill_switch

    def check_kill_switch(
        self, identity: AgentIdentity, tool_name: str
    ) -> tuple[bool, str | None]:
        with self._lock:
            config = self._kill_switch
        if not config.enabled:
            return False, None
        if tool_name in config.except_tools or identity.agent_id in config.except_agents:
            return False, None
        return True, config.reason or "global kill switch enabled"

    def match(
        self,
        identity: AgentIdentity,
        tool_name: str,
        secret_refs: list[str] | None = None,
    ) -> RevocationMatch:
        with self._lock:
            try:
                self._refresh_from_disk()
                refresh_error: Exception | None = None
            except Exception as exc:
                refresh_error = exc
            config = self._kill_switch
            entries = list(self._entries.values())
        if config.enabled and (
            tool_name not in config.except_tools
            and identity.agent_id not in config.except_agents
        ):
            return RevocationMatch(
                revoked=True,
                reason=config.reason or "global kill switch enabled",
                type="kill_switch",
                id="global",
            )
        now = datetime.now(UTC)
        refs = set(secret_refs or [])
        for entry in entries:
            if entry.expires_at is not None and entry.expires_at <= now:
                continue
            if entry.tenant_id is not None and entry.tenant_id != identity.tenant_id:
                continue
            matched = (
                entry.type == RevocationType.AGENT
                and entry.id == identity.agent_id
                or entry.type == RevocationType.USER
                and entry.id == identity.user_id
                or entry.type == RevocationType.TOOL
                and entry.id == tool_name
                or entry.type == RevocationType.SECRET
                and entry.id in refs
            )
            if matched:
                return RevocationMatch(
                    revoked=True,
                    reason=entry.reason or f"{entry.type.value} {entry.id} revoked",
                    type=entry.type,
                    id=entry.id,
                )
        if refresh_error is not None:
            return RevocationMatch(
                revoked=True,
                reason=f"revocation config unavailable: {refresh_error}",
            )
        return RevocationMatch(revoked=False)

    def is_revoked(
        self,
        identity: AgentIdentity,
        tool_name: str,
        secret_refs: list[str] | None = None,
    ) -> tuple[bool, str | None]:
        """兼容旧调用方的二元组接口。"""
        match = self.match(identity, tool_name, secret_refs)
        return match.revoked, match.reason

    def add(self, entry: RevocationEntry) -> None:
        with self._lock:
            self._replace(lambda entries, config: (
                {**entries, self._key(entry.type, entry.id, entry.tenant_id): entry},
                config,
            ))

    def remove(
        self, entry_type: RevocationType | str, entry_id: str, tenant_id: str | None = None
    ) -> bool:
        normalized = RevocationType(entry_type)
        key = self._key(normalized, entry_id, tenant_id)
        removed = False

        def remove_entry(
            entries: dict[tuple[RevocationType, str, str | None], RevocationEntry],
            config: KillSwitchConfig,
        ) -> tuple[
            dict[tuple[RevocationType, str, str | None], RevocationEntry], KillSwitchConfig
        ]:
            nonlocal removed
            removed = key in entries
            updated = dict(entries)
            updated.pop(key, None)
            return updated, config

        with self._lock:
            self._replace(remove_entry)
        return removed

    def set_kill_switch(self, config: KillSwitchConfig) -> None:
        with self._lock:
            self._replace(lambda entries, _current: (entries, config))

    def reload(self) -> None:
        with self._lock:
            self._refresh_from_disk(require_exists=True)

    def _refresh_from_disk(self, *, require_exists: bool = False) -> None:
        if self._path is None:
            return
        current = durable_locked_read(self._path)
        if current is None:
            if require_exists:
                raise FileNotFoundError(self._path)
            return
        entries, kill_switch = self._parse(current)
        self._entries = {
            self._key(entry.type, entry.id, entry.tenant_id): entry for entry in entries
        }
        self._kill_switch = kill_switch

    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "kill_switch": self._kill_switch.model_dump(mode="json"),
                "revocations": [entry.model_dump(mode="json") for entry in self._entries.values()],
            }

    def _replace(
        self,
        update: Callable[
            [dict[tuple[RevocationType, str, str | None], RevocationEntry], KillSwitchConfig],
            tuple[dict[tuple[RevocationType, str, str | None], RevocationEntry], KillSwitchConfig],
        ],
    ) -> None:
        if self._path is None:
            self._entries, self._kill_switch = update(dict(self._entries), self._kill_switch)
            return

        result: tuple[
            dict[tuple[RevocationType, str, str | None], RevocationEntry], KillSwitchConfig
        ] | None = None

        def merge(current: bytes | None) -> bytes:
            nonlocal result
            if current is None:
                entries: dict[
                    tuple[RevocationType, str, str | None], RevocationEntry
                ] = {}
                config = KillSwitchConfig()
            else:
                data = yaml.safe_load(current)
                if not isinstance(data, dict) or not data:
                    raise ValueError("revocation config must be a non-empty mapping")
                parsed = [
                    RevocationEntry.model_validate(item) for item in data.get("revocations", [])
                ]
                entries = {
                    self._key(entry.type, entry.id, entry.tenant_id): entry for entry in parsed
                }
                config = KillSwitchConfig.model_validate(data.get("kill_switch") or {})
            result = update(entries, config)
            updated_entries, updated_config = result
            payload = {
                "kill_switch": updated_config.model_dump(mode="json"),
                "revocations": [
                    entry.model_dump(mode="json") for entry in updated_entries.values()
                ],
            }
            return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).encode("utf-8")

        durable_atomic_replace(self._path, merge)
        assert result is not None
        self._entries, self._kill_switch = result
