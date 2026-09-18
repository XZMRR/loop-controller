"""JWT 身份 Provider（生产用，v0.52 起支持 tenant/roles claim 与 JWKS 轮换语义）。"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Any

from loop_controller.identity.models import AgentIdentity, IdentityCredential
from loop_controller.models import Agent

logger = logging.getLogger(__name__)


class JWTIdentityProvider:
    """验证 JWT 并映射为内部 AgentIdentity。

    支持从 `jwks_url` 拉取公钥，或直接用 `public_key` 验证。

    v0.52 扩展：
    - claim_mappings 可选映射 `tenant_id` 与 `roles`（roles 支持点路径与列表/字符串）；
    - tenant 绑定校验（fail-closed）：agent 注册表已填 tenant_id 时 claim tenant 必须一致，
      注册表未填时仅在 `allow_claim_tenant=True` 时采用 claim tenant；
    - JWKS 缓存 TTL / 硬过期语义：`cache_ttl_seconds` 到期主动重建 client，
      硬过期后刷新失败即 fail-closed；未知 kid 由 PyJWKClient 内部刷新；
    - jwks_url 必须 https，除非显式 `allow_http_jwks=True`（仅本地开发）。
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
        allow_claim_tenant: bool = False,
        jwks_cache_ttl_seconds: float = 300.0,
        jwks_hard_ttl_seconds: float = 3600.0,
        allow_http_jwks: bool = False,
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
        self._allow_claim_tenant = allow_claim_tenant
        self._jwks_cache_ttl = jwks_cache_ttl_seconds
        self._jwks_hard_ttl = jwks_hard_ttl_seconds
        self._allow_http_jwks = allow_http_jwks
        self._jwt: Any | None = None
        self._jwks_client: Any | None = None
        self._jwks_fetched_monotonic: float | None = None
        self._jwks_lock: asyncio.Lock | None = None

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
                key = await self._resolve_jwks_key(credential.token)
                if key is None:
                    logger.warning("JWTIdentityProvider 无法从 jwks_url 获取公钥")
                    return None
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

        # v0.52 tenant 绑定校验（fail-closed）：注册表优先，claim 仅作受限补充。
        claim_tenant = self._extract_claim(payload, "tenant_id")
        if agent.tenant_id is not None:
            if claim_tenant is not None and claim_tenant != agent.tenant_id:
                logger.warning(
                    "JWT claim tenant %s 与注册表 tenant %s 不一致，拒绝",
                    claim_tenant,
                    agent.tenant_id,
                )
                return None
            tenant_id: str | None = agent.tenant_id
        elif self._allow_claim_tenant:
            tenant_id = claim_tenant
        else:
            if claim_tenant is not None:
                logger.warning(
                    "JWT claim tenant %s 未被 allow_claim_tenant 允许，忽略",
                    claim_tenant,
                )
            tenant_id = None

        roles = self._extract_roles(payload)

        exp = payload.get("exp")
        expires_at = datetime.fromtimestamp(exp, UTC) if exp else None
        return AgentIdentity(
            agent_id=agent_id,
            user_id=user_id,
            harness_id=harness_id,
            profile_id=agent.profile_id,
            tenant_id=tenant_id,
            roles=roles,
            expires_at=expires_at,
        )

    def _extract_claim(self, payload: dict[str, Any], name: str) -> str | None:
        claim_name = self._claim_mappings.get(name, name)
        value = payload.get(claim_name)
        return str(value) if value is not None else None

    def _extract_roles(self, payload: dict[str, Any]) -> tuple[str, ...]:
        """按映射提取 roles claim，支持点路径；列表/元组/空白或逗号分隔字符串均可。"""
        claim_name = self._claim_mappings.get("roles")
        if not claim_name:
            return ()
        value: Any = payload
        for part in claim_name.split("."):
            if not isinstance(value, dict):
                return ()
            value = value.get(part)
        if value is None:
            return ()
        if isinstance(value, (list, tuple)):
            return tuple(str(item) for item in value if item)
        if isinstance(value, str):
            return tuple(item for item in value.replace(",", " ").split() if item)
        return ()

    async def _resolve_jwks_key(self, token: str) -> Any:
        """带 TTL / 硬过期语义的 JWKS key 解析（单飞去重）。

        - cache_ttl 到期：重建 PyJWKClient 触发重新拉取（主动轮换）；
        - 硬过期：必须重新拉取成功才可用，否则 fail-closed；
        - 已知 kid 命中缓存时不走网络（PyJWKClient 内部缓存，可用性优先）；
        - 未知 kid：PyJWKClient 内部自动刷新，失败向上抛出为 None。
        """
        jwks_url = self._jwks_url
        if jwks_url is None:
            return None
        if jwks_url.startswith("http://") and not self._allow_http_jwks:
            logger.warning("jwks_url 必须使用 https（或显式 allow_http_jwks=True）")
            return None
        if self._jwks_lock is None:
            self._jwks_lock = asyncio.Lock()
        async with self._jwks_lock:
            now = time.monotonic()
            stale_age = (
                now - self._jwks_fetched_monotonic
                if self._jwks_fetched_monotonic is not None
                else None
            )
            hard_expired = (
                stale_age is not None and stale_age > self._jwks_hard_ttl
            )
            needs_refresh = (
                self._jwks_client is None
                or hard_expired
                or (stale_age is not None and stale_age > self._jwks_cache_ttl)
            )
            if not needs_refresh:
                client = self._jwks_client
                if client is None:
                    return None
                try:
                    return client.get_signing_key_from_jwt(token)
                except Exception as exc:  # noqa: BLE001
                    # 已知 kid 命中缓存不会走到这里；未知 kid 触发网络刷新，
                    # 硬过期内按可用性优先继续信任既有缓存，否则 fail-closed。
                    logger.warning("JWKS 缓存内 key 解析失败：%s", exc)
                    return None
            client = await self._fetch_jwks()
            if client is None:
                return None
            try:
                key = client.get_signing_key_from_jwt(token)
            except Exception as exc:  # noqa: BLE001
                logger.warning("JWKS 刷新后仍无法解析签名 key：%s", exc)
                return None
            self._jwks_client = client
            self._jwks_fetched_monotonic = now
            return key

    async def _fetch_jwks(self) -> Any:
        """从 JWKS URL 构造 PyJWKClient（v0.20.0 基础实现，缓存 client 实例）。"""
        if not self._jwks_url:
            return None
        try:
            from jwt import PyJWKClient

            return PyJWKClient(self._jwks_url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("构造 JWKS client 失败：%s", exc)
            return None

    def get_agent(self, agent_id: str) -> Agent | None:
        return self._agents.get(agent_id)

    def get_user(self, user_id: str) -> str | None:
        return self._users.get(user_id)
