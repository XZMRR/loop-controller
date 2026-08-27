"""全局吊销列表与 Kill Switch。"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from loop_controller.identity.models import AgentIdentity


class RevocationType(StrEnum):
    AGENT = "agent"
    USER = "user"
    TOOL = "tool"
    SECRET = "secret"


class RevocationEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: RevocationType
    id: str
    reason: str
    revoked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime | None = None
    tenant_id: str | None = None


class KillSwitchConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    enabled: bool = False
    reason: str = ""
    except_tools: list[str] = Field(default_factory=list)
    except_agents: list[str] = Field(default_factory=list)


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
        if not target.exists():
            return cls(path=target)
        data = yaml.safe_load(target.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not data:
            raise ValueError("revocation config must be a non-empty mapping")
        return cls(
            entries=[RevocationEntry.model_validate(item) for item in data.get("revocations", [])],
            kill_switch=KillSwitchConfig.model_validate(data.get("kill_switch") or {}),
            path=target,
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

    def is_revoked(
        self,
        identity: AgentIdentity,
        tool_name: str,
        secret_refs: list[str] | None = None,
    ) -> tuple[bool, str | None]:
        killed, reason = self.check_kill_switch(identity, tool_name)
        if killed:
            return killed, reason
        now = datetime.now(UTC)
        refs = set(secret_refs or [])
        with self._lock:
            entries = list(self._entries.values())
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
                return True, entry.reason or f"{entry.type.value} {entry.id} revoked"
        return False, None

    def add(self, entry: RevocationEntry) -> None:
        with self._lock:
            updated = dict(self._entries)
            updated[self._key(entry.type, entry.id, entry.tenant_id)] = entry
            self._persist(entries=updated, kill_switch=self._kill_switch)
            self._entries = updated

    def remove(
        self, entry_type: RevocationType | str, entry_id: str, tenant_id: str | None = None
    ) -> bool:
        normalized = RevocationType(entry_type)
        with self._lock:
            key = self._key(normalized, entry_id, tenant_id)
            if key not in self._entries:
                return False
            updated = dict(self._entries)
            del updated[key]
            self._persist(entries=updated, kill_switch=self._kill_switch)
            self._entries = updated
            return True

    def set_kill_switch(self, config: KillSwitchConfig) -> None:
        with self._lock:
            self._persist(entries=self._entries, kill_switch=config)
            self._kill_switch = config

    def reload(self) -> None:
        if self._path is None:
            return
        if not self._path.exists():
            raise FileNotFoundError(self._path)
        loaded = self.from_file(self._path)
        with self._lock:
            self._entries = loaded._entries
            self._kill_switch = loaded._kill_switch

    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "kill_switch": self._kill_switch.model_dump(mode="json"),
                "revocations": [entry.model_dump(mode="json") for entry in self._entries.values()],
            }

    def _persist(
        self,
        *,
        entries: dict[tuple[RevocationType, str, str | None], RevocationEntry],
        kill_switch: KillSwitchConfig,
    ) -> None:
        if self._path is None:
            return
        data = {
            "kill_switch": kill_switch.model_dump(mode="json"),
            "revocations": [entry.model_dump(mode="json") for entry in entries.values()],
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(f"{self._path.suffix}.tmp")
        temporary.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        temporary.replace(self._path)
