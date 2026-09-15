"""Secret Broker 数据模型（v0.22.0）。"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SecretScope(StrEnum):
    """Secret 作用域。"""

    GLOBAL = "global"
    TENANT = "tenant"


class CredentialVersionMode(StrEnum):
    PINNED = "pinned"
    CURRENT = "current"


class CredentialInjection(StrEnum):
    HEADER = "header"
    QUERY = "query"
    BODY = "body"
    MTLS = "mtls"


class ToolCredentialRef(BaseModel):
    """不含明文的工具凭证策略引用。"""

    model_config = ConfigDict(frozen=True)

    name: str
    scope: SecretScope
    tenant_id: str | None = None
    key: str | None = None
    version_mode: CredentialVersionMode = CredentialVersionMode.CURRENT
    version: str | None = None
    injection: CredentialInjection
    allowed_tools: tuple[str, ...] = ()
    tenant_allowlist: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_scope_and_version(self) -> ToolCredentialRef:
        if self.scope == SecretScope.TENANT and self.tenant_id is None:
            raise ValueError("tenant scope requires tenant_id")
        if self.scope == SecretScope.GLOBAL:
            if self.tenant_id is not None:
                raise ValueError("global scope forbids tenant_id")
            if not self.allowed_tools or any(not item for item in self.allowed_tools):
                raise ValueError("global credential requires at least one allowed tool")
            if not self.tenant_allowlist or any(not item for item in self.tenant_allowlist):
                raise ValueError("global credential requires an explicit tenant allowlist")
        elif self.allowed_tools or self.tenant_allowlist:
            raise ValueError("tenant credential policy is bound by tenant_id")
        if len(set(self.allowed_tools)) != len(self.allowed_tools):
            raise ValueError("allowed_tools must not contain duplicates")
        if len(set(self.tenant_allowlist)) != len(self.tenant_allowlist):
            raise ValueError("tenant_allowlist must not contain duplicates")
        if "*" in self.tenant_allowlist and len(self.tenant_allowlist) != 1:
            raise ValueError("tenant wildcard must be the only tenant allowlist entry")
        if self.version_mode == CredentialVersionMode.PINNED:
            if self.version is None or self.version == CredentialVersionMode.CURRENT:
                raise ValueError("pinned credential requires a concrete version")
        elif self.version not in (None, CredentialVersionMode.CURRENT):
            raise ValueError("current credential version must be None or 'current'")
        return self


class ResolvedToolCredentialRef(BaseModel):
    """调用开始时固定到具体版本的不含明文凭证引用。"""

    model_config = ConfigDict(frozen=True)

    name: str
    scope: SecretScope
    tenant_id: str | None = None
    key: str | None = None
    version_mode: CredentialVersionMode
    resolved_version: str
    injection: CredentialInjection
    allowed_tools: tuple[str, ...] = ()
    tenant_allowlist: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_scope_and_version(self) -> ResolvedToolCredentialRef:
        if self.scope == SecretScope.TENANT and self.tenant_id is None:
            raise ValueError("tenant scope requires tenant_id")
        if self.scope == SecretScope.GLOBAL:
            if self.tenant_id is not None:
                raise ValueError("global scope forbids tenant_id")
            if not self.allowed_tools or not self.tenant_allowlist:
                raise ValueError("resolved global credential requires tool and tenant policy")
        elif self.allowed_tools or self.tenant_allowlist:
            raise ValueError("tenant credential policy is bound by tenant_id")
        if not self.resolved_version or self.resolved_version == CredentialVersionMode.CURRENT:
            raise ValueError("resolved_version must be concrete")
        return self


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
