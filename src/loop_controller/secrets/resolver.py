"""工具凭证引用的 strict 解析与注入前复验。"""

from __future__ import annotations

from loop_controller.secrets.broker import SecretBroker
from loop_controller.secrets.exceptions import SecretNotFoundError
from loop_controller.secrets.models import (
    CredentialVersionMode,
    ResolvedToolCredentialRef,
    SecretRef,
    SecretScope,
    SecretValue,
    ToolCredentialRef,
)


class ToolCredentialResolver:
    def __init__(self, broker: SecretBroker) -> None:
        self._broker = broker

    @staticmethod
    def _authorize(
        ref: ToolCredentialRef | ResolvedToolCredentialRef,
        tool_name: str | None,
        tenant_id: str | None,
    ) -> None:
        if not tool_name or not tenant_id:
            raise SecretNotFoundError(
                "credential resolution requires trusted tool and tenant context",
                ref_name=ref.name,
            )
        if ref.scope == SecretScope.TENANT:
            if ref.tenant_id != tenant_id:
                raise SecretNotFoundError("credential tenant mismatch", ref_name=ref.name)
            return
        if tool_name not in ref.allowed_tools:
            raise SecretNotFoundError("credential tool is not allowed", ref_name=ref.name)
        if "*" not in ref.tenant_allowlist and tenant_id not in ref.tenant_allowlist:
            raise SecretNotFoundError("credential tenant is not allowed", ref_name=ref.name)

    async def resolve(
        self,
        ref: ToolCredentialRef,
        tool_name: str | None = None,
        tenant_id: str | None = None,
    ) -> tuple[ResolvedToolCredentialRef, SecretValue]:
        self._authorize(ref, tool_name, tenant_id)
        requested_version = (
            ref.version if ref.version_mode == CredentialVersionMode.PINNED else None
        )
        secret_ref = SecretRef(
            name=ref.name,
            key=ref.key,
            version=requested_version,
            tenant_id=ref.tenant_id,
        )
        secret = await self._broker.get_exact(secret_ref, ref.scope)
        if secret is None or secret.is_expired():
            raise SecretNotFoundError(
                f"credential {ref.name} 不存在、版本不匹配或已过期",
                ref_name=ref.name,
            )
        resolved = ResolvedToolCredentialRef(
            name=ref.name,
            scope=ref.scope,
            tenant_id=ref.tenant_id,
            key=ref.key,
            version_mode=ref.version_mode,
            resolved_version=secret.version,
            injection=ref.injection,
            allowed_tools=ref.allowed_tools,
            tenant_allowlist=ref.tenant_allowlist,
        )
        return resolved, secret

    async def revalidate(
        self,
        ref: ResolvedToolCredentialRef,
        tool_name: str | None = None,
        tenant_id: str | None = None,
    ) -> SecretValue:
        """按调用开始时固定的具体版本复验，并返回待注入值。"""
        self._authorize(ref, tool_name, tenant_id)
        secret = await self._broker.get_exact(
            SecretRef(
                name=ref.name,
                key=ref.key,
                version=ref.resolved_version,
                tenant_id=ref.tenant_id,
            ),
            ref.scope,
        )
        if (
            secret is None
            or secret.is_expired()
            or secret.version != ref.resolved_version
        ):
            raise SecretNotFoundError(
                f"credential {ref.name} 已撤销、版本已变化或已过期",
                ref_name=ref.name,
            )
        return secret
