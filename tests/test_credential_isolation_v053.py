from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from loop_controller.executors.base import ExecutionContext
from loop_controller.executors.http_executor import HTTPExecutor
from loop_controller.executors.http_models import HTTPAuthConfig, HTTPToolSpec
from loop_controller.secrets import (
    CredentialInjection,
    SecretRef,
    SecretScope,
    ToolCredentialRef,
    ToolCredentialResolver,
)
from loop_controller.secrets.exceptions import SecretNotFoundError
from loop_controller.secrets.memory_backend import MemorySecretBackend


def _ref(**updates):
    values = dict(name="canary", scope=SecretScope.GLOBAL, injection=CredentialInjection.HEADER, allowed_tools=("send",), tenant_allowlist=("tenant-a",))
    values.update(updates)
    return ToolCredentialRef(**values)


@pytest.mark.asyncio
async def test_global_credential_policy_and_missing_context_fail_closed() -> None:
    backend = MemorySecretBackend()
    backend.put("canary", "unique-secret")
    resolver = ToolCredentialResolver(backend)
    with pytest.raises(SecretNotFoundError):
        await resolver.resolve(_ref())
    with pytest.raises(SecretNotFoundError):
        await resolver.resolve(_ref(), "other", "tenant-a")
    with pytest.raises(SecretNotFoundError):
        await resolver.resolve(_ref(), "send", "tenant-b")
    resolved, _ = await resolver.resolve(_ref(), "send", "tenant-a")
    assert (await resolver.revalidate(resolved, "send", "tenant-a")).value == "unique-secret"
    with pytest.raises(ValidationError):
        _ref(allowed_tools=())


@pytest.mark.asyncio
async def test_http_revalidates_immediately_before_injection() -> None:
    backend = MemorySecretBackend()
    backend.put("canary", "v1-secret")
    resolver = ToolCredentialResolver(backend)
    original = resolver.revalidate

    async def revoke_before_revalidate(ref, tool_name=None, tenant_id=None):
        backend._global.pop("canary")
        return await original(ref, tool_name, tenant_id)

    resolver.revalidate = revoke_before_revalidate  # type: ignore[method-assign]
    client = AsyncMock()
    spec = HTTPToolSpec(
        tool_name="send",
        base_url="https://api.example.com",
        allowed_hosts=["api.example.com"],
        require_dns_resolution=False,
        auth=HTTPAuthConfig(
            type="bearer_token", secret_ref=SecretRef(name="canary")
        ),
        protected_credential_ref=_ref(),
    )
    executor = HTTPExecutor(client, {"send": spec}, security_mode="strict", credential_resolver=resolver)
    context = ExecutionContext(call_id="c", task_id="t", agent_id="a", user_id="u", request_id="r", decision_id="d", tenant_id="tenant-a")
    result = await executor.execute("send", {}, context)
    assert result.error_code == "http_auth_error"
    client.request.assert_not_called()
