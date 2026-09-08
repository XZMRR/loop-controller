"""静态 token 身份 Provider（仅开发/测试）。"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from loop_controller.identity.models import AgentIdentity, IdentityCredential
from loop_controller.models import Agent

logger = logging.getLogger(__name__)


class ConfigIdentityProvider:
    """基于静态 agents.yaml + 可选静态 token 表的身份 Provider。

    - get_agent / get_user 直接查配置表；
    - verify 仅在 token 与 allowed_tokens 中某一项匹配时通过。

    仅用于开发和测试环境，生产环境必须改用 JWT / mTLS。
    """

    def __init__(
        self,
        agents: dict[str, Agent],
        users: dict[str, str],
        allowed_tokens: list[dict[str, str]] | None = None,
        default_ttl_seconds: int = 3600,
    ) -> None:
        self._agents = agents
        self._users = users
        self._allowed_tokens = allowed_tokens or []
        self._default_ttl = timedelta(seconds=default_ttl_seconds)

    def get_agent(self, agent_id: str) -> Agent | None:
        return self._agents.get(agent_id)

    def get_user(self, user_id: str) -> str | None:
        return self._users.get(user_id)

    async def verify(self, credential: IdentityCredential) -> AgentIdentity | None:
        if not credential.token:
            return None
        for entry in self._allowed_tokens:
            if entry.get("token") == credential.token:
                agent_id = entry.get("agent_id")
                user_id = entry.get("user_id")
                if not agent_id or not user_id:
                    logger.warning("静态 token 表项缺少 agent_id 或 user_id")
                    return None
                agent = self._agents.get(agent_id)
                if agent is None:
                    logger.warning("静态 token 映射到未知 agent_id: %s", agent_id)
                    return None
                return AgentIdentity(
                    agent_id=agent_id,
                    user_id=user_id,
                    profile_id=agent.profile_id,
                    expires_at=datetime.now(UTC) + self._default_ttl,
                )
        return None
