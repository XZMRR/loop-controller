"""MCP Proxy 安全加固测试（v0.33.0）。

不依赖 OPA，直接测试 Proxy 的限流、请求体限制与 admin 权限隔离。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

pytest.importorskip("mcp")
pytest.importorskip("starlette")

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from loop_controller.models import CapabilityProfile
from loop_controller.proxy_server import (
    DEFAULT_MAX_MCP_BODY_SIZE,
    LoopControllerProxyServer,
    ProxyIdentity,
    _MCPBodySizeMiddleware,
    _MCPRateLimitMiddleware,
)


def _make_proxy(entrypoints_config: dict[str, Any] | None = None) -> LoopControllerProxyServer:
    """构造一个最小 mock 的 Proxy Server。"""
    runtime = MagicMock()
    profile = CapabilityProfile(
        profile_id="research_assistant_v1",
        max_budget_token=100000,
        max_budget_payment=0.0,
        tools={},
    )
    runtime.profiles = {"research_assistant_v1": profile}
    runtime.checkpoint._identity.get_agent.return_value = MagicMock(
        agent_id="researcher_001",
        profile_id="research_assistant_v1",
        tenant_id="t1",
        identity={"harness_id": "h1"},
    )
    identity = ProxyIdentity(agent_id="researcher_001", user_id="alice")
    return LoopControllerProxyServer(
        runtime,
        identity,
        entrypoints_config=entrypoints_config or {},
    )


def _build_middleware_app(middleware: list[Middleware]) -> Starlette:
    async def app_endpoint(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    return Starlette(
        middleware=middleware,
        routes=[Route("/messages/", app_endpoint, methods=["POST"])],
    )


def test_mcp_body_size_limit_returns_413() -> None:
    """MCP Proxy POST /messages/ 请求体超过上限时返回 413。"""
    app = _build_middleware_app([Middleware(_MCPBodySizeMiddleware)])
    client = TestClient(app)
    resp = client.post(
        "/messages/",
        headers={"Content-Length": str(DEFAULT_MAX_MCP_BODY_SIZE + 1)},
    )
    assert resp.status_code == 413
    assert resp.json()["error"] == "payload_too_large"


def test_mcp_body_size_invalid_content_length_returns_400() -> None:
    """MCP Proxy Content-Length 非法时返回 400。"""
    app = _build_middleware_app([Middleware(_MCPBodySizeMiddleware)])
    client = TestClient(app)
    resp = client.post("/messages/", headers={"Content-Length": "not-a-number"})
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_parameter"


def test_mcp_rate_limit_blocks_excessive_requests() -> None:
    """MCP Proxy 限流中间件对超额请求返回 429。"""
    app = _build_middleware_app(
        [Middleware(_MCPRateLimitMiddleware, requests_per_minute=1, burst=1)]
    )
    client = TestClient(app)
    for i in range(3):
        resp = client.post("/messages/")
        if i < 2:
            assert resp.status_code == 200
        else:
            assert resp.status_code == 429
            assert resp.json()["error"] == "rate_limited"


@pytest.mark.asyncio
async def test_admin_tool_requires_admin_profile() -> None:
    """非 admin profile 调用 admin 工具返回 admin_forbidden。"""
    proxy = _make_proxy(entrypoints_config={"admin": {"agent_profiles": ["admin_profile"]}})
    result = await proxy._handle_call_tool_impl(
        "trigger_kill_switch",
        {"enabled": True, "reason": "test"},
    )
    assert result.isError
    payload = result.content[0].text
    assert "admin_forbidden" in payload


@pytest.mark.asyncio
async def test_admin_tool_allows_admin_profile() -> None:
    """admin profile 调用 admin kill_switch 成功执行。"""
    from unittest.mock import AsyncMock

    proxy = _make_proxy(entrypoints_config={"admin": {"agent_profiles": ["admin_profile"]}})
    runtime = proxy._runtime
    runtime.checkpoint._identity.get_agent.return_value = MagicMock(
        agent_id="researcher_001",
        profile_id="admin_profile",
        tenant_id="t1",
        identity={"harness_id": "h1"},
    )
    runtime.revocation_list = MagicMock()
    runtime.audit_store = MagicMock()
    runtime.audit_store.append_async = AsyncMock()

    result = await proxy._handle_call_tool_impl(
        "trigger_kill_switch",
        {"enabled": True, "reason": "test"},
    )
    assert not result.isError
    runtime.revocation_list.set_kill_switch.assert_called_once()


@pytest.mark.asyncio
async def test_error_response_does_not_leak_internal_exception() -> None:
    """MCP Proxy 内部异常不暴露原始错误信息。"""
    proxy = _make_proxy()
    proxy._runtime.create_task.side_effect = RuntimeError("sensitive internal detail")
    result = await proxy._handle_call_tool_impl(
        "send_email",
        {"to": "bob@company.com"},
    )
    assert result.isError
    payload = result.content[0].text
    assert "internal_error" in payload
    assert "sensitive internal detail" not in payload
