"""Secret Broker 协议（v0.22.0）。"""

from __future__ import annotations

from typing import Protocol

from loop_controller.secrets.models import SecretRef, SecretScope, SecretValue


class SecretBroker(Protocol):
    """统一 secret 读取接口。"""

    async def get(self, ref: SecretRef) -> SecretValue | None:
        """按引用读取 secret；不存在、过期或无权时返回 None。"""
        ...

    async def list(
        self, scope: SecretScope, tenant_id: str | None = None
    ) -> list[str]:
        """列出某作用域下的 secret 名称。"""
        ...

    async def reload(self) -> None:
        """重新加载缓存（热更新触发）。"""
        ...
