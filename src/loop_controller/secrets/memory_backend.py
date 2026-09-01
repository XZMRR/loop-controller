"""MemorySecretBackend：内存 secret 后端，供测试与本地开发使用（v0.22.0）。"""

from __future__ import annotations

from typing import Any

from loop_controller.secrets.exceptions import SecretNotFoundError
from loop_controller.secrets.models import SecretRef, SecretScope, SecretValue


class MemorySecretBackend:
    """内存 secret 后端。"""

    def __init__(self, secrets: dict[str, SecretValue] | None = None) -> None:
        self._global: dict[str, SecretValue] = {}
        self._tenant: dict[str, dict[str, SecretValue]] = {}
        if secrets is not None:
            for name, value in secrets.items():
                self._store(name, value)

    def _store(self, name: str, value: SecretValue) -> None:
        if value.scope == SecretScope.TENANT and value.tenant_id is not None:
            self._tenant.setdefault(value.tenant_id, {})[name] = value
        else:
            self._global[name] = value

    def put(self, name: str, value: Any, *, tenant_id: str | None = None) -> None:
        """测试辅助：写入 secret。"""
        scope = SecretScope.TENANT if tenant_id is not None else SecretScope.GLOBAL
        secret = SecretValue(value=value, scope=scope, tenant_id=tenant_id)
        self._store(name, secret)

    # ------------------------------------------------------------------
    # SecretBroker Protocol
    # ------------------------------------------------------------------

    async def get(self, ref: SecretRef) -> SecretValue | None:
        if ref.tenant_id is not None:
            cache = self._tenant.get(ref.tenant_id, {})
            secret = cache.get(ref.name)
            if secret is not None:
                return self._resolve_key(secret, ref)
        secret = self._global.get(ref.name)
        return self._resolve_key(secret, ref)

    async def list(
        self, scope: SecretScope, tenant_id: str | None = None
    ) -> list[str]:
        if scope == SecretScope.TENANT:
            if tenant_id is None:
                return []
            return sorted(self._tenant.get(tenant_id, {}).keys())
        return sorted(self._global.keys())

    async def reload(self) -> None:
        """内存后端无需从磁盘重载。"""

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _resolve_key(
        self, secret: SecretValue | None, ref: SecretRef
    ) -> SecretValue | None:
        if secret is None:
            return None
        if ref.version is not None and secret.version != ref.version:
            return None
        if ref.key is None:
            return secret
        if not isinstance(secret.value, dict):
            raise SecretNotFoundError(
                f"secret {ref.name} 不是对象，无法提取 key {ref.key}",
                ref_name=ref.name,
            )
        if ref.key not in secret.value:
            raise SecretNotFoundError(
                f"secret {ref.name} 缺少 key {ref.key}",
                ref_name=ref.name,
            )
        return secret.model_copy(update={"value": secret.value[ref.key]})
