"""Secret Broker 数据模型（v0.22.0）。"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SecretScope(str, Enum):
    """Secret 作用域。"""

    GLOBAL = "global"
    TENANT = "tenant"


class SecretValue(BaseModel):
    """Secret 值。"""

    model_config = ConfigDict(frozen=True)

    value: Any
    scope: SecretScope = SecretScope.GLOBAL
    tenant_id: str | None = None
    version: str = "1"
    expires_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def is_expired(self, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        now = now or datetime.now(UTC)
        if now.tzinfo is None and self.expires_at.tzinfo is not None:
            now = now.replace(tzinfo=UTC)
        return now >= self.expires_at


class SecretRef(BaseModel):
    """Secret 引用。"""

    model_config = ConfigDict(frozen=True)

    name: str
    key: str | None = None
    version: str | None = None
    tenant_id: str | None = None
