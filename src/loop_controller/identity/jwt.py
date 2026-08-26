"""JWT 身份 Provider（生产用）。"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from loop_controller.identity.models import AgentIdentity, IdentityCredential
from loop_controller.models import Agent

logger = logging.getLogger(__name__)


class JWTIdentityProvider:
    """验证 JWT 并映射为内部 AgentIdentity。

    支持从 `jwks_url` 拉取公钥，或直接用 `public_key` 验证。
    """

    def __init__(
        self,
        agents: dict[str, Agent],
        users: dict[str, str],
        *,
        issuer: str,
        audience: str,
        jwks_url: str | None = None,
        public_key: str | None = None,
        claim_mappings: dict[str, str] | None = None,
    ) -> None:
        self._agents = agents
        self._users = users
        self._issuer = issuer
        self._audience = audience
        self._jwks_url = jwks_url
        self._public_key = public_key
        self._claim_mappings = claim_mappings or {
            "agent_id": "agent_id",
            "user_id": "user_id",
            "harness_id": "harness_id",
        }
        self._jwt: Any | None = None
        self._jwks_client: Any | None = None

    def _load_jwt(self) -> Any:
        """延迟加载 PyJWT，失败时给出明确提示。"""
        if self._jwt is None:
            try:
                import jwt as _jwt
            except ImportError as exc:  # pragma: no cover
                raise ImportError(
                    "JWTIdentityProvider 需要 PyJWT：uv pip install pyjwt"
                ) from exc
            self._jwt = _jwt
        return self._jwt

    async def verify(self, credential: IdentityCredential) -> AgentIdentity | None:
        if not credential.token:
            return None
        try:
            jwt = self._load_jwt()
            key = self._public_key
            if key is None and self._jwks_url:
                jwks_client = await self._fetch_jwks()
                if jwks_client is None:
                    logger.warning("JWTIdentityProvider 无法从 jwks_url 获取公钥")
                    return None
                key = jwks_client.get_signing_key_from_jwt(credential.token)
            if key is None:
                logger.warning("JWTIdentityProvider 未配置 public_key 或 jwks_url")
                return None
            payload = jwt.decode(
                credential.token,
                key,
                algorithms=["RS256"],
                issuer=self._issuer,
                audience=self._audience,
            )
        except Exception as exc:  # noqa: BLE001 - 验证失败只记日志
            logger.debug("JWT 验证失败：%s", exc)
            return None

        agent_id = self._extract_claim(payload, "agent_id")
        user_id = self._extract_claim(payload, "user_id")
        harness_id = self._extract_claim(payload, "harness_id")
        if not agent_id or not user_id:
            logger.warning("JWT 缺少 agent_id 或 user_id claim")
            return None

        agent = self._agents.get(agent_id)
        if agent is None:
            logger.warning("JWT 映射到未知 agent_id: %s", agent_id)
            return None

        exp = payload.get("exp")
        expires_at = datetime.fromtimestamp(exp, UTC) if exp else None
        return AgentIdentity(
            agent_id=agent_id,
            user_id=user_id,
            harness_id=harness_id,
            profile_id=agent.profile_id,
            expires_at=expires_at,
        )

    def _extract_claim(self, payload: dict[str, Any], name: str) -> str | None:
        claim_name = self._claim_mappings.get(name, name)
        value = payload.get(claim_name)
        return str(value) if value is not None else None

    async def _fetch_jwks(self) -> Any:
        """从 JWKS URL 构造 PyJWKClient（v0.20.0 基础实现，缓存 client 实例）。"""
        if self._jwks_client is not None:
            return self._jwks_client
        if not self._jwks_url:
            return None
        try:
            from jwt import PyJWKClient

            self._jwks_client = PyJWKClient(self._jwks_url)
            return self._jwks_client
        except Exception as exc:  # noqa: BLE001
            logger.warning("构造 JWKS client 失败：%s", exc)
            return None

    def get_agent(self, agent_id: str) -> Agent | None:
        return self._agents.get(agent_id)

    def get_user(self, user_id: str) -> str | None:
        return self._users.get(user_id)
