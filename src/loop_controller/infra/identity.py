"""身份管理（§4.2）.

IdentityProvider 回答"这个 agent_id 是谁"。MVP 中可信配置 = 本地 ``agents.yaml``
（由 ConfigLoader 加载为 ``AppConfig.agents``），运行期只读、不支持动态申领。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from loop_controller.models import Agent


@runtime_checkable
class IdentityProvider(Protocol):
    """身份提供者接口."""

    def get_agent(self, agent_id: str) -> Agent | None:
        """按 ID 返回 Agent；未知 ID 返回 None."""
        ...

    def get_user(self, user_id: str) -> str | None:
        """返回用户显示名；MVP 仅校验存在性."""
        ...


class ConfigIdentityProvider:
    """基于 ConfigLoader 加载的身份字典的实现（只读）。"""

    def __init__(self, agents: dict[str, Agent], users: dict[str, str]) -> None:
        """初始化.

        Args:
            agents: agent_id -> Agent（来自 agents.yaml）。
            users: user_id -> display_name（来自 agents.yaml）。
        """
        self._agents = agents
        self._users = users

    def get_agent(self, agent_id: str) -> Agent | None:
        return self._agents.get(agent_id)

    def get_user(self, user_id: str) -> str | None:
        return self._users.get(user_id)
