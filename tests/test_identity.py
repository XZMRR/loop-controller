"""可信身份 Provider 单元测试（v0.20.0）。

覆盖 ConfigIdentityProvider / JWTIdentityProvider / MTLSIdentityProvider。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from loop_controller.identity import (
    ConfigIdentityProvider,
    IdentityCredential,
    MTLSIdentityProvider,
)
from loop_controller.identity.jwt import JWTIdentityProvider
from loop_controller.models import Agent


@pytest.fixture
def sample_agent() -> Agent:
    return Agent(
        agent_id="researcher_001",
        name="RA",
        profile_id="research_assistant_v1",
        owner_id="zhang_manager",
    )


@pytest.fixture
def agents(sample_agent: Agent) -> dict[str, Agent]:
    return {"researcher_001": sample_agent}


@pytest.fixture
def users() -> dict[str, str]:
    return {"alice": "Alice", "zhang_manager": "张经理"}


class TestConfigIdentityProvider:
    """静态 token Provider 测试。"""

    @pytest.fixture
    def provider(
        self, agents: dict[str, Agent], users: dict[str, str]
    ) -> ConfigIdentityProvider:
        return ConfigIdentityProvider(
            agents=agents,
            users=users,
            allowed_tokens=[
                {
                    "token": "dev-token-researcher-001",
                    "agent_id": "researcher_001",
                    "user_id": "alice",
                }
            ],
        )

    @pytest.mark.asyncio
    async def test_verify_valid_token(
        self, provider: ConfigIdentityProvider
    ) -> None:
        identity = await provider.verify(IdentityCredential(token="dev-token-researcher-001"))
        assert identity is not None
        assert identity.agent_id == "researcher_001"
        assert identity.user_id == "alice"
        assert identity.profile_id == "research_assistant_v1"

    @pytest.mark.asyncio
    async def test_verify_unknown_token(
        self, provider: ConfigIdentityProvider
    ) -> None:
        identity = await provider.verify(IdentityCredential(token="unknown"))
        assert identity is None

    @pytest.mark.asyncio
    async def test_verify_missing_token(
        self, provider: ConfigIdentityProvider
    ) -> None:
        identity = await provider.verify(IdentityCredential())
        assert identity is None

    def test_get_agent(self, provider: ConfigIdentityProvider) -> None:
        agent = provider.get_agent("researcher_001")
        assert agent is not None
        assert agent.agent_id == "researcher_001"

    def test_get_agent_unknown(self, provider: ConfigIdentityProvider) -> None:
        assert provider.get_agent("ghost") is None

    def test_get_user(self, provider: ConfigIdentityProvider) -> None:
        assert provider.get_user("alice") == "Alice"
        assert provider.get_user("ghost") is None

    @pytest.mark.asyncio
    async def test_default_ttl_sets_expires_at(
        self, agents: dict[str, Agent], users: dict[str, str]
    ) -> None:
        """P2：default_ttl_seconds 应体现在 AgentIdentity.expires_at 上。"""
        provider = ConfigIdentityProvider(
            agents=agents,
            users=users,
            allowed_tokens=[
                {
                    "token": "dev-token",
                    "agent_id": "researcher_001",
                    "user_id": "alice",
                }
            ],
            default_ttl_seconds=1800,
        )
        identity = await provider.verify(IdentityCredential(token="dev-token"))
        assert identity is not None
        assert identity.expires_at is not None
        now = datetime.now(UTC)
        assert now <= identity.expires_at <= now + timedelta(seconds=1900)


class TestJWTIdentityProvider:
    """JWT Provider 测试。"""

    @pytest.fixture(scope="class")
    def key_pair(self) -> tuple[str, str]:
        """生成 RSA 密钥对（公钥/私钥 PEM）。"""
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        private_key = rsa.generate_private_key(
            public_exponent=65537, key_size=2048, backend=default_backend()
        )
        private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("utf-8")
        public_pem = (
            private_key.public_key()
            .public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode("utf-8")
        )
        return private_pem, public_pem

    @pytest.fixture
    def provider(
        self,
        agents: dict[str, Agent],
        users: dict[str, str],
        key_pair: tuple[str, str],
    ) -> JWTIdentityProvider:
        _, public_pem = key_pair
        return JWTIdentityProvider(
            agents=agents,
            users=users,
            issuer="https://auth.company.com",
            audience="loop-controller",
            public_key=public_pem,
        )

    def _issue_token(
        self,
        private_key: str,
        claims: dict[str, Any],
        expired: bool = False,
    ) -> str:
        import jwt

        now = datetime.now(UTC)
        payload = {
            "iss": "https://auth.company.com",
            "aud": "loop-controller",
            "iat": now,
            **claims,
        }
        if "exp" not in payload:
            delta = timedelta(seconds=-1) if expired else timedelta(hours=1)
            payload["exp"] = now + delta
        return jwt.encode(payload, private_key, algorithm="RS256")

    @pytest.mark.asyncio
    async def test_verify_valid_jwt(
        self,
        provider: JWTIdentityProvider,
        key_pair: tuple[str, str],
    ) -> None:
        private_key, _ = key_pair
        token = self._issue_token(
            private_key,
            {"agent_id": "researcher_001", "user_id": "alice"},
        )
        identity = await provider.verify(IdentityCredential(token=token))
        assert identity is not None
        assert identity.agent_id == "researcher_001"
        assert identity.user_id == "alice"
        assert identity.profile_id == "research_assistant_v1"

    @pytest.mark.asyncio
    async def test_verify_with_harness_id(
        self,
        provider: JWTIdentityProvider,
        key_pair: tuple[str, str],
    ) -> None:
        private_key, _ = key_pair
        token = self._issue_token(
            private_key,
            {"agent_id": "researcher_001", "user_id": "alice", "harness_id": "prod-001"},
        )
        identity = await provider.verify(IdentityCredential(token=token))
        assert identity is not None
        assert identity.harness_id == "prod-001"

    @pytest.mark.asyncio
    async def test_verify_expired_jwt(
        self,
        provider: JWTIdentityProvider,
        key_pair: tuple[str, str],
    ) -> None:
        private_key, _ = key_pair
        token = self._issue_token(
            private_key,
            {"agent_id": "researcher_001", "user_id": "alice"},
            expired=True,
        )
        identity = await provider.verify(IdentityCredential(token=token))
        assert identity is None

    @pytest.mark.asyncio
    async def test_verify_wrong_issuer(
        self,
        agents: dict[str, Agent],
        users: dict[str, str],
        key_pair: tuple[str, str],
    ) -> None:
        private_key, public_pem = key_pair
        provider = JWTIdentityProvider(
            agents=agents,
            users=users,
            issuer="https://other.com",
            audience="loop-controller",
            public_key=public_pem,
        )
        import jwt

        token = jwt.encode(
            {
                "iss": "https://auth.company.com",
                "aud": "loop-controller",
                "iat": datetime.now(UTC),
                "exp": datetime.now(UTC) + timedelta(hours=1),
                "agent_id": "researcher_001",
                "user_id": "alice",
            },
            private_key,
            algorithm="RS256",
        )
        identity = await provider.verify(IdentityCredential(token=token))
        assert identity is None

    @pytest.mark.asyncio
    async def test_verify_unknown_agent(
        self,
        provider: JWTIdentityProvider,
        key_pair: tuple[str, str],
    ) -> None:
        private_key, _ = key_pair
        token = self._issue_token(
            private_key,
            {"agent_id": "ghost", "user_id": "alice"},
        )
        identity = await provider.verify(IdentityCredential(token=token))
        assert identity is None

    @pytest.mark.asyncio
    async def test_verify_missing_claims(
        self,
        provider: JWTIdentityProvider,
        key_pair: tuple[str, str],
    ) -> None:
        private_key, _ = key_pair
        token = self._issue_token(private_key, {"user_id": "alice"})
        identity = await provider.verify(IdentityCredential(token=token))
        assert identity is None

    @pytest.mark.asyncio
    async def test_verify_jwks_url(
        self,
        agents: dict[str, Agent],
        users: dict[str, str],
        key_pair: tuple[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """JWKS URL 模式应能正确拉取/构造公钥并验证 token。"""
        private_key, public_pem = key_pair
        token = self._issue_token(
            private_key,
            {"agent_id": "researcher_001", "user_id": "alice"},
        )

        class FakePyJWKClient:
            def __init__(self, url: str) -> None:
                self.url = url

            def get_signing_key_from_jwt(self, _token: str) -> str:
                return public_pem

        monkeypatch.setattr("jwt.PyJWKClient", FakePyJWKClient)

        provider = JWTIdentityProvider(
            agents=agents,
            users=users,
            issuer="https://auth.company.com",
            audience="loop-controller",
            jwks_url="https://auth.company.com/.well-known/jwks.json",
        )
        identity = await provider.verify(IdentityCredential(token=token))
        assert identity is not None
        assert identity.agent_id == "researcher_001"
        assert identity.user_id == "alice"


class TestMTLSIdentityProvider:
    """mTLS 证书身份 Provider 测试。"""

    @pytest.fixture
    def provider(
        self, agents: dict[str, Agent], users: dict[str, str]
    ) -> MTLSIdentityProvider:
        return MTLSIdentityProvider(
            agents=agents,
            users=users,
            cert_subject_template="agent-{agent_id}-prod-{harness_id}",
        )

    @pytest.mark.asyncio
    async def test_template_match(
        self, provider: MTLSIdentityProvider
    ) -> None:
        identity = await provider.verify(
            IdentityCredential(cert_cn="agent-researcher_001-prod-001")
        )
        assert identity is not None
        assert identity.agent_id == "researcher_001"
        assert identity.harness_id == "001"

    @pytest.mark.asyncio
    async def test_template_no_match(
        self, provider: MTLSIdentityProvider
    ) -> None:
        identity = await provider.verify(
            IdentityCredential(cert_cn="agent-unknown-prod-001")
        )
        assert identity is None

    @pytest.mark.asyncio
    async def test_template_anchored_to_end(
        self, agents: dict[str, Agent], users: dict[str, str]
    ) -> None:
        """P2：模板必须完整匹配到字符串结尾，中间占位符不能吞掉固定后缀。"""
        provider = MTLSIdentityProvider(
            agents=agents,
            users=users,
            cert_subject_template="CN={agent_id},O=company",
        )
        identity = await provider.verify(
            IdentityCredential(cert_subject="CN=researcher_001-extra,O=company")
        )
        assert identity is None

    @pytest.mark.asyncio
    async def test_template_agent_id_only(
        self, agents: dict[str, Agent], users: dict[str, str]
    ) -> None:
        provider = MTLSIdentityProvider(
            agents=agents,
            users=users,
            cert_subject_template="agent-{agent_id}",
        )
        identity = await provider.verify(
            IdentityCredential(cert_cn="agent-researcher_001")
        )
        assert identity is not None
        assert identity.agent_id == "researcher_001"
        assert identity.harness_id is None

    @pytest.mark.asyncio
    async def test_explicit_mapping(
        self, agents: dict[str, Agent], users: dict[str, str]
    ) -> None:
        provider = MTLSIdentityProvider(
            agents=agents,
            users=users,
            cert_mappings=[
                {
                    "subject": "CN=agent-researcher-prod-001,O=company",
                    "agent_id": "researcher_001",
                    "harness_id": "prod-001",
                }
            ],
        )
        identity = await provider.verify(
            IdentityCredential(cert_subject="CN=agent-researcher-prod-001,O=company")
        )
        assert identity is not None
        assert identity.agent_id == "researcher_001"
        assert identity.harness_id == "prod-001"

    @pytest.mark.asyncio
    async def test_missing_cert_info(
        self, provider: MTLSIdentityProvider
    ) -> None:
        identity = await provider.verify(IdentityCredential())
        assert identity is None

    def test_get_agent(self, provider: MTLSIdentityProvider) -> None:
        assert provider.get_agent("researcher_001") is not None
