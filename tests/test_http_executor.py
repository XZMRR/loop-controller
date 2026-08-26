"""HTTP Executor 测试（v0.21.0）。

覆盖模板渲染、认证注入、响应映射、错误处理、SSRF 拦截。
使用 pytest-httpx 或纯 unittest.mock 模拟 httpx。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from loop_controller.executors import ExecutionContext, HTTPClient, HTTPExecutor
from loop_controller.executors.http_models import (
    HTTPAuthConfig,
    HTTPResponseMapping,
    HTTPToolSpec,
    extract_jsonpath,
    resolve_env_refs,
)
from loop_controller.secrets import MemorySecretBackend, SecretRef

_DEFAULT_BODY = object()


def _fake_context() -> ExecutionContext:
    return ExecutionContext(
        call_id="c1",
        task_id="t1",
        agent_id="a1",
        user_id="u1",
    )


def _create_spec(
    *,
    tool_name: str = "create_jira_ticket",
    base_url: str = "https://api.example.com",
    path: str = "/issues",
    method: str = "POST",
    auth: HTTPAuthConfig | None = None,
    response_mapping: HTTPResponseMapping | None = None,
    allowed_hosts: list[str] | None = None,
    body_template: Any = _DEFAULT_BODY,
    require_dns_resolution: bool = False,
    **kwargs: Any,
) -> HTTPToolSpec:
    body = body_template if body_template is not _DEFAULT_BODY else {"title": "{title}"}
    return HTTPToolSpec(
        tool_name=tool_name,
        base_url=base_url,
        path=path,
        method=method,  # type: ignore[arg-type]
        headers={"Accept": "application/json"},
        body_template=body,
        auth=auth or HTTPAuthConfig(),
        response_mapping=response_mapping or HTTPResponseMapping(),
        allowed_hosts=allowed_hosts or ["api.example.com"],
        require_dns_resolution=require_dns_resolution,
        **kwargs,
    )


class TestHTTPToolSpec:
    """HTTP 工具规格模型测试。"""

    def test_base_url_strip_trailing_slash(self) -> None:
        spec = _create_spec(base_url="https://api.example.com/")
        assert spec.base_url == "https://api.example.com"

    def test_derive_allowed_hosts_from_base_url(self) -> None:
        spec = _create_spec(allowed_hosts=[])
        assert spec.allowed_hosts == ["api.example.com"]

    async def test_render_path_and_body(self) -> None:
        spec = _create_spec(path="/issues/{id}")
        url, headers, body = await spec.build_request({"id": "123", "title": "bug"})
        assert url == "https://api.example.com/issues/123"
        assert body == {"title": "bug"}

    async def test_render_query(self) -> None:
        spec = _create_spec(
            path="/issues",
            query_template={"project": "{project}", "limit": 10},
        )
        url, _headers, _body = await spec.build_request({"project": "PROJ", "title": "x"})
        assert "project=PROJ" in url
        assert "limit=10" in url

    async def test_bearer_auth_injected(self) -> None:
        spec = _create_spec(
            auth=HTTPAuthConfig(type="bearer_token", token="secret123")
        )
        _url, headers, _body = await spec.build_request({"title": "x"})
        assert headers["Authorization"] == "Bearer secret123"

    async def test_basic_auth_injected(self) -> None:
        spec = _create_spec(
            auth=HTTPAuthConfig(type="basic", username="u", password="p")
        )
        _url, headers, _body = await spec.build_request({"title": "x"})
        assert headers["Authorization"].startswith("Basic ")

    def test_env_ref_resolved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JIRA_TOKEN", "token-from-env")
        resolved = resolve_env_refs({"token": "${JIRA_TOKEN}"})
        assert resolved["token"] == "token-from-env"

    def test_env_ref_missing_raises(self) -> None:
        with pytest.raises(ValueError, match="JIRA_TOKEN"):
            resolve_env_refs({"token": "${JIRA_TOKEN}"})


class TestExtractJsonpath:
    """极简 JSONPath 测试。"""

    def test_simple_path(self) -> None:
        data = {"issue": {"key": "PROJ-1", "self": "https://x/1"}}
        assert extract_jsonpath(data, "$.issue.key") == "PROJ-1"

    def test_array_index(self) -> None:
        data = {"items": [{"id": 1}, {"id": 2}]}
        assert extract_jsonpath(data, "$.items[1].id") == 2

    def test_missing_returns_none(self) -> None:
        assert extract_jsonpath({"a": 1}, "$.b") is None


@pytest.fixture
def mock_client() -> AsyncMock:
    mock = AsyncMock(spec=HTTPClient)
    mock.request = AsyncMock(return_value=(200, {}, '{"key":"PROJ-1"}', 100.0))
    return mock


class TestHTTPExecutor:
    """HTTPExecutor 执行测试。"""

    @pytest.fixture
    def http_client(self) -> HTTPClient:
        return HTTPClient()

    @pytest.mark.asyncio
    async def test_success_response_mapping(self, mock_client: AsyncMock) -> None:
        spec = _create_spec(
            response_mapping=HTTPResponseMapping(
                success_status=[200, 201],
                extract={"key": "$.key"},
            )
        )
        executor = HTTPExecutor(mock_client, {"create_jira_ticket": spec})
        result = await executor.execute(
            "create_jira_ticket",
            {"title": "bug"},
            _fake_context(),
        )
        assert result.status == "success"
        assert result.content == {"key": "PROJ-1"}
        assert result.error_code is None

    @pytest.mark.asyncio
    async def test_error_status_mapping(self, mock_client: AsyncMock) -> None:
        mock_client.request = AsyncMock(return_value=(404, {}, "not found", 50.0))
        spec = _create_spec(
            response_mapping=HTTPResponseMapping(
                error_codes={404: "issue_not_found"}
            )
        )
        executor = HTTPExecutor(mock_client, {"create_jira_ticket": spec})
        result = await executor.execute(
            "create_jira_ticket",
            {"title": "bug"},
            _fake_context(),
        )
        assert result.status == "error"
        assert result.error_code == "issue_not_found"

    @pytest.mark.asyncio
    async def test_ssrf_blocked(self, mock_client: AsyncMock) -> None:
        spec = _create_spec(
            base_url="http://localhost:8080",
            path="/api",
            allowed_hosts=["localhost"],
            body_template=None,
        )
        executor = HTTPExecutor(mock_client, {"bad_tool": spec})
        result = await executor.execute(
            "bad_tool",
            {},
            _fake_context(),
        )
        assert result.status == "error"
        assert result.error_code == "http_security_blocked"
        mock_client.request.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_argument_error(self, mock_client: AsyncMock) -> None:
        spec = _create_spec(path="/issues/{id}")
        executor = HTTPExecutor(mock_client, {"create_jira_ticket": spec})
        result = await executor.execute(
            "create_jira_ticket",
            {"title": "bug"},  # 缺少 id
            _fake_context(),
        )
        assert result.status == "error"
        assert result.error_code == "http_missing_argument"

    @pytest.mark.asyncio
    async def test_list_tools_filtered_by_profile(self, mock_client: AsyncMock) -> None:
        from loop_controller.models import CapabilityProfile

        spec_a = _create_spec(tool_name="tool_a", body_template=None)
        spec_b = _create_spec(tool_name="tool_b", body_template=None)
        executor = HTTPExecutor(mock_client, {"tool_a": spec_a, "tool_b": spec_b})
        profile = CapabilityProfile(
            profile_id="p1",
            tools={"tool_a": {"tool_name": "tool_a", "allowed": True}},
        )
        tools = await executor.list_tools(profile)
        assert len(tools) == 1
        assert tools[0].canonical_name == "tool_a"

    @pytest.mark.asyncio
    async def test_network_error_handling(self, mock_client: AsyncMock) -> None:
        from loop_controller.executors.http_security import HTTPSecurityError

        mock_client.request = AsyncMock(
            side_effect=HTTPSecurityError("timeout", "http_timeout")
        )
        spec = _create_spec()
        executor = HTTPExecutor(mock_client, {"create_jira_ticket": spec})
        result = await executor.execute(
            "create_jira_ticket",
            {"title": "bug"},
            _fake_context(),
        )
        assert result.status == "error"
        assert result.error_code == "http_timeout"


class TestHTTPSecretBroker:
    """HTTPExecutor 与 Secret Broker 集成测试。"""

    @pytest.fixture
    def secret_broker(self) -> MemorySecretBackend:
        backend = MemorySecretBackend()
        backend.put("jira_token", "secret-from-broker")
        backend.put("api_creds", {"username": "u", "password": "p"})
        return backend

    @pytest.mark.asyncio
    async def test_bearer_secret_from_broker(
        self, secret_broker: MemorySecretBackend, mock_client: AsyncMock
    ) -> None:
        spec = _create_spec(
            auth=HTTPAuthConfig(
                type="bearer_token",
                secret_ref=SecretRef(name="jira_token"),
            )
        )
        executor = HTTPExecutor(
            mock_client, {"create_jira_ticket": spec}, secret_broker=secret_broker
        )
        result = await executor.execute(
            "create_jira_ticket",
            {"title": "bug"},
            _fake_context(),
        )
        assert result.status == "success"
        args = mock_client.request.call_args
        headers = args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer secret-from-broker"

    @pytest.mark.asyncio
    async def test_basic_secret_from_broker(
        self, secret_broker: MemorySecretBackend, mock_client: AsyncMock
    ) -> None:
        spec = _create_spec(
            auth=HTTPAuthConfig(
                type="basic",
                secret_ref=SecretRef(name="api_creds"),
            )
        )
        executor = HTTPExecutor(
            mock_client, {"create_jira_ticket": spec}, secret_broker=secret_broker
        )
        result = await executor.execute(
            "create_jira_ticket",
            {"title": "bug"},
            _fake_context(),
        )
        assert result.status == "success"
        args = mock_client.request.call_args
        headers = args.kwargs["headers"]
        assert headers["Authorization"].startswith("Basic ")

    @pytest.mark.asyncio
    async def test_secret_not_found_returns_auth_error(
        self, mock_client: AsyncMock
    ) -> None:
        backend = MemorySecretBackend()
        spec = _create_spec(
            auth=HTTPAuthConfig(
                type="bearer_token",
                secret_ref=SecretRef(name="missing"),
            )
        )
        executor = HTTPExecutor(
            mock_client, {"create_jira_ticket": spec}, secret_broker=backend
        )
        result = await executor.execute(
            "create_jira_ticket",
            {"title": "bug"},
            _fake_context(),
        )
        assert result.status == "error"
        assert result.error_code == "http_auth_error"
        mock_client.request.assert_not_called()

    @pytest.mark.asyncio
    async def test_tenant_secret_preferred_over_global(
        self, mock_client: AsyncMock
    ) -> None:
        backend = MemorySecretBackend()
        backend.put("jira_token", "global-token")
        backend.put("jira_token", "tenant-token", tenant_id="acme")

        spec = _create_spec(
            auth=HTTPAuthConfig(
                type="bearer_token",
                secret_ref=SecretRef(name="jira_token"),
            )
        )
        executor = HTTPExecutor(
            mock_client, {"create_jira_ticket": spec}, secret_broker=backend
        )
        ctx = ExecutionContext(
            call_id="c1",
            task_id="t1",
            agent_id="a1",
            user_id="u1",
            tenant_id="acme",
        )
        result = await executor.execute(
            "create_jira_ticket",
            {"title": "bug"},
            ctx,
        )
        assert result.status == "success"
        args = mock_client.request.call_args
        assert args.kwargs["headers"]["Authorization"] == "Bearer tenant-token"

    @pytest.mark.asyncio
    async def test_secret_ref_takes_priority_over_direct_token(
        self, secret_broker: MemorySecretBackend, mock_client: AsyncMock
    ) -> None:
        spec = _create_spec(
            auth=HTTPAuthConfig(
                type="bearer_token",
                token="direct-token",
                secret_ref=SecretRef(name="jira_token"),
            )
        )
        executor = HTTPExecutor(
            mock_client, {"create_jira_ticket": spec}, secret_broker=secret_broker
        )
        result = await executor.execute(
            "create_jira_ticket",
            {"title": "bug"},
            _fake_context(),
        )
        assert result.status == "success"
        args = mock_client.request.call_args
        # secret_ref 优先
        assert args.kwargs["headers"]["Authorization"] == "Bearer secret-from-broker"

    @pytest.mark.asyncio
    async def test_empty_bearer_token_rejected(
        self, mock_client: AsyncMock
    ) -> None:
        """空字符串 bearer token 不应被发送出去。"""
        spec = _create_spec(
            auth=HTTPAuthConfig(type="bearer_token", token="   "),
        )
        executor = HTTPExecutor(mock_client, {"create_jira_ticket": spec})
        result = await executor.execute(
            "create_jira_ticket",
            {"title": "bug"},
            _fake_context(),
        )
        assert result.status == "error"
        assert result.error_code == "http_auth_error"
        mock_client.request.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_api_key_header_token_rejected(
        self, mock_client: AsyncMock
    ) -> None:
        """Secret Broker 返回空字符串时 api_key_header 不应被发送出去。"""
        backend = MemorySecretBackend()
        backend.put("empty_key", "")
        spec = _create_spec(
            auth=HTTPAuthConfig(
                type="api_key_header",
                key_name="X-Api-Key",
                secret_ref=SecretRef(name="empty_key"),
            ),
        )
        executor = HTTPExecutor(
            mock_client, {"create_jira_ticket": spec}, secret_broker=backend
        )
        result = await executor.execute(
            "create_jira_ticket",
            {"title": "bug"},
            _fake_context(),
        )
        assert result.status == "error"
        assert result.error_code == "http_auth_error"
        mock_client.request.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_api_key_query_token_rejected(
        self, mock_client: AsyncMock
    ) -> None:
        """Secret Broker 返回空字符串时 api_key_query 不应被发送出去。"""
        backend = MemorySecretBackend()
        backend.put("empty_key", "")
        spec = _create_spec(
            auth=HTTPAuthConfig(
                type="api_key_query",
                key_name="api_key",
                secret_ref=SecretRef(name="empty_key"),
            ),
        )
        executor = HTTPExecutor(
            mock_client, {"create_jira_ticket": spec}, secret_broker=backend
        )
        result = await executor.execute(
            "create_jira_ticket",
            {"title": "bug"},
            _fake_context(),
        )
        assert result.status == "error"
        assert result.error_code == "http_auth_error"
        mock_client.request.assert_not_called()



