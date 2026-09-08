"""mTLS 证书身份 Provider（生产用）。"""

from __future__ import annotations

import logging
import re

from loop_controller.identity.models import AgentIdentity, IdentityCredential
from loop_controller.models import Agent

logger = logging.getLogger(__name__)


class MTLSIdentityProvider:
    """把 mTLS 客户端证书 CN/SAN 映射为内部 AgentIdentity。"""

    def __init__(
        self,
        agents: dict[str, Agent],
        users: dict[str, str],
        *,
        cert_mappings: list[dict[str, str]] | None = None,
        cert_subject_template: str | None = None,
    ) -> None:
        self._agents = agents
        self._users = users
        self._cert_mappings = cert_mappings or []
        self._cert_subject_template = cert_subject_template

    async def verify(self, credential: IdentityCredential) -> AgentIdentity | None:
        subject = credential.cert_subject or credential.cert_cn
        if not subject:
            logger.debug("mTLS 凭证缺少证书 subject/CN")
            return None

        # 优先显式映射表
        for mapping in self._cert_mappings:
            if mapping.get("subject") == subject or mapping.get("cn") == credential.cert_cn:
                agent_id = mapping.get("agent_id")
                harness_id = mapping.get("harness_id")
                if not agent_id:
                    continue
                return self._build_identity(agent_id, harness_id)

        # 其次模板映射
        if self._cert_subject_template:
            identity = self._match_template(subject)
            if identity is not None:
                return identity

        return None

    def _template_to_regex(self, pattern: str) -> str:
        """把 cert_subject_template 转换为带命名捕获组的正则表达式。

        支持 ``{agent_id}`` 与 ``{harness_id}`` 占位符；所有占位符均使用非贪婪
        匹配，并在末尾强制锚定到字符串结尾，避免多余后缀被吞入最后一个字段。
        """
        regex = re.escape(pattern)
        regex = regex.replace(r"\{agent_id\}", r"(?P<agent_id>[^/]+?)")
        regex = regex.replace(r"\{harness_id\}", r"(?P<harness_id>[^/]+?)")

        # 强制锚定到字符串结尾，避免 agent-001-extra 被误识别为 agent_id="001-extra"
        return regex + r"\Z"

    def _match_template(self, subject: str) -> AgentIdentity | None:
        """按 cert_subject_template 解析 agent_id 和 harness_id。

        模板支持 ``{agent_id}`` 与 ``{harness_id}`` 两个占位符；
        至少包含 ``{agent_id}`` 才能从证书推导内部身份。
        """
        pattern = self._cert_subject_template
        if pattern is None:
            return None

        # 将占位符替换为命名捕获组；分隔符区域严格转义。
        # 最后一个占位符使用贪婪匹配，避免末尾非贪婪只匹配最少字符。
        regex = self._template_to_regex(pattern)

        match = re.match(regex, subject)
        if not match:
            return None

        groups = match.groupdict()
        agent_id = groups.get("agent_id")
        if not agent_id:
            logger.warning("mTLS 模板匹配结果缺少 agent_id")
            return None
        harness_id = groups.get("harness_id")
        return self._build_identity(agent_id, harness_id)

    def _build_identity(self, agent_id: str, harness_id: str | None) -> AgentIdentity | None:
        agent = self._agents.get(agent_id)
        if agent is None:
            logger.warning("mTLS 证书映射到未知 agent_id: %s", agent_id)
            return None
        return AgentIdentity(
            agent_id=agent_id,
            user_id=agent.agent_id,  # mTLS 场景下 user_id 回退到 agent_id
            harness_id=harness_id,
            profile_id=agent.profile_id,
        )

    def get_agent(self, agent_id: str) -> Agent | None:
        return self._agents.get(agent_id)

    def get_user(self, user_id: str) -> str | None:
        return self._users.get(user_id)
