"""JWT tenant 绑定校验测试（v0.52 P52-01）。

注册表优先（fail-closed）：agents.yaml 已填 tenant_id 时 claim tenant 必须一致；
注册表未填时仅在 allow_claim_tenant=True 时采用 claim tenant；roles claim 映射。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from loop_controller.identity import IdentityCredential
from loop_controller.identity.jwt import JWTIdentityProvider
from loop_controller.models import Agent


@pytest.fixture(scope="module")
def key_pair() -> tuple[str, str]:
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
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    return private_pem, public_pem


def _issue_token(private_key: str, claims: dict[str, Any]) -> str:
    import jwt as pyjwt

    now = datetime.now(UTC)
    payload = {
        "iss": "https://auth.company.com",
        "aud": "loop-controller",
        "iat": now,
        "exp": now + timedelta(hours=1),
        **claims,
    }
    return pyjwt.encode(payload, private_key, algorithm="RS256")


def _agents(tenant_id: str | None) -> dict[str, Agent]:
    return {
        "researcher_001": Agent(
            agent_id="researcher_001",
            name="RA",
            profile_id="research_assistant_v1",
            owner_id="zhang_manager",
            tenant_id=tenant_id,
        )
    }


def _provider(public_pem: str, agents: dict[str, Agent], **kwargs: Any) -> JWTIdentityProvider:
    return JWTIdentityProvider(
        agents=agents,
        users={"alice": "Alice"},
        issuer="https://auth.company.com",
        audience="loop-controller",
        public_key=public_pem,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_registry_tenant_wins_over_matching_claim(
    key_pair: tuple[str, str],
) -> None:
    private_key, public_pem = key_pair
    provider = _provider(public_pem, _agents("tenant-a"))
    token = _issue_token(
        private_key,
        {"agent_id": "researcher_001", "user_id": "alice", "tenant_id": "tenant-a"},
    )
    identity = await provider.verify(IdentityCredential(token=token))
    assert identity is not None
    assert identity.tenant_id == "tenant-a"


@pytest.mark.asyncio
async def test_claim_tenant_mismatch_rejected(key_pair: tuple[str, str]) -> None:
    private_key, public_pem = key_pair
    provider = _provider(public_pem, _agents("tenant-a"))
    token = _issue_token(
        private_key,
        {"agent_id": "researcher_001", "user_id": "alice", "tenant_id": "tenant-b"},
    )
    assert await provider.verify(IdentityCredential(token=token)) is None


@pytest.mark.asyncio
async def test_registry_tenant_kept_without_claim(key_pair: tuple[str, str]) -> None:
    private_key, public_pem = key_pair
    provider = _provider(public_pem, _agents("tenant-a"))
    token = _issue_token(private_key, {"agent_id": "researcher_001", "user_id": "alice"})
    identity = await provider.verify(IdentityCredential(token=token))
    assert identity is not None
    assert identity.tenant_id == "tenant-a"


@pytest.mark.asyncio
async def test_claim_tenant_requires_opt_in(key_pair: tuple[str, str]) -> None:
    private_key, public_pem = key_pair
    # 默认拒绝：注册表未填 tenant 时 claim tenant 被忽略
    strict = _provider(public_pem, _agents(None))
    token = _issue_token(
        private_key,
        {"agent_id": "researcher_001", "user_id": "alice", "tenant_id": "tenant-b"},
    )
    identity = await strict.verify(IdentityCredential(token=token))
    assert identity is not None
    assert identity.tenant_id is None

    relaxed = _provider(public_pem, _agents(None), allow_claim_tenant=True)
    identity = await relaxed.verify(IdentityCredential(token=token))
    assert identity is not None
    assert identity.tenant_id == "tenant-b"


@pytest.mark.asyncio
async def test_roles_claim_mapping(key_pair: tuple[str, str]) -> None:
    private_key, public_pem = key_pair
    provider = _provider(
        public_pem,
        _agents("tenant-a"),
        claim_mappings={
            "agent_id": "agent_id",
            "user_id": "user_id",
            "harness_id": "harness_id",
            "roles": "realm.roles",
        },
    )
    token = _issue_token(
        private_key,
        {
            "agent_id": "researcher_001",
            "user_id": "alice",
            "realm": {"roles": ["policy_creator", "policy_validator"]},
        },
    )
    identity = await provider.verify(IdentityCredential(token=token))
    assert identity is not None
    assert identity.roles == ("policy_creator", "policy_validator")


@pytest.mark.asyncio
async def test_roles_claim_comma_string(key_pair: tuple[str, str]) -> None:
    private_key, public_pem = key_pair
    provider = _provider(
        public_pem,
        _agents("tenant-a"),
        claim_mappings={
            "agent_id": "agent_id",
            "user_id": "user_id",
            "harness_id": "harness_id",
            "roles": "roles",
        },
    )
    token = _issue_token(
        private_key,
        {
            "agent_id": "researcher_001",
            "user_id": "alice",
            "roles": "policy_creator, policy_auditor",
        },
    )
    identity = await provider.verify(IdentityCredential(token=token))
    assert identity is not None
    assert identity.roles == ("policy_creator", "policy_auditor")


@pytest.mark.asyncio
async def test_http_jwks_rejected_without_opt_in() -> None:
    provider = JWTIdentityProvider(
        agents=_agents(None),
        users={},
        issuer="https://auth.company.com",
        audience="loop-controller",
        jwks_url="http://127.0.0.1:9999/jwks.json",
    )
    key = await provider._resolve_jwks_key("header.payload.sig")
    assert key is None


@pytest.mark.asyncio
async def test_jwks_cache_ttl_rebuilds_client(
    key_pair: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """cache_ttl 到期后必须重建 PyJWKClient（主动轮换语义）。"""

    class FakePyJWKClient:
        constructed = 0

        def __init__(self, url: str) -> None:
            FakePyJWKClient.constructed += 1
            self.url = url

        def get_signing_key_from_jwt(self, token: str) -> str:
            return "fake-key"

    monkeypatch.setattr("jwt.PyJWKClient", FakePyJWKClient)
    provider = JWTIdentityProvider(
        agents=_agents(None),
        users={},
        issuer="https://auth.company.com",
        audience="loop-controller",
        jwks_url="https://auth.company.com/jwks.json",
        jwks_cache_ttl_seconds=0.0,
        jwks_hard_ttl_seconds=3600.0,
    )
    assert await provider._resolve_jwks_key("t") == "fake-key"
    assert await provider._resolve_jwks_key("t") == "fake-key"
    assert FakePyJWKClient.constructed == 2
