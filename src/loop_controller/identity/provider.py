"""身份 Provider 协议。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from loop_controller.identity.models import AgentIdentity, IdentityCredential
from loop_controller.models import Agent


@runtime_checkable
class IdentityProvider(Protocol):
    """身份提供者：验证凭证并返回内部 AgentIdentity。

    同时保留 v0.19.0 之前的 get_agent / get_user 查询能力，供 Checkpoint
    做 profile / owner 查找。
    """

    async def verify(self, credential: IdentityCredential) -> AgentIdentity | None:
        """验证凭证；成功返回 AgentIdentity，失败返回 None。"""
        ...

    def get_agent(self, agent_id: str) -> Agent | None:
        """按 agent_id 返回已注册的 Agent（用于策略/审批路由）。"""
        ...

    def get_user(self, user_id: str) -> str | None:
        """按 user_id 返回显示名；未知返回 None。"""
        ...
