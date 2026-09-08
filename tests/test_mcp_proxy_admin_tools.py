"""MCP Proxy admin/审计工具测试（v0.32.0）。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from loop_controller.infra.config_loader import ConfigLoader
from loop_controller.proxy_server import LoopControllerProxyServer, ProxyIdentity
from loop_controller.runtime import build_runtime

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def admin_workdir(tmp_path: Path, opa_server: str) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    shutil.copytree(REPO_ROOT / "config", root / "config")
    shutil.copytree(REPO_ROOT / "policies", root / "policies")
    (root / "data").mkdir()

    (root / "config" / "mcp_servers.yaml").write_text(
        """
servers:
  email_mock:
    command: ["python", "-m", "loop_controller.mocks.email_server"]
    transport: stdio

tool_mapping:
  web_search:  {server: email_mock, mcp_name: web_search, cost_per_call: 200}
  harness_echo: {server: email_mock, mcp_name: web_search, cost_per_call: 10}
""",
        encoding="utf-8",
    )
    (root / "config" / "profiles.yaml").write_text(
        """
profiles:
  - profile_id: research_assistant_v1
    description: 研究助手岗位说明书
    max_budget_token: 100000
    max_budget_payment: 0.0
    session_block_threshold: 2
    tools:
      web_search:
        allowed: true
        max_calls_per_task: 10
      harness_echo:
        allowed: true
        max_calls_per_task: 10
""",
        encoding="utf-8",
    )
    (root / "config" / "harness_tools.yaml").write_text(
        """
execution:
  default_mode: trusted_local
  trusted_local_tools: [web_search]

backends:
  http_harness:
    type: http
    base_url: https://harness.internal.example
    timeout_seconds: 30
    max_concurrent_calls: 3
    acquire_timeout_seconds: 2
    health:
      enabled: true
      path: /health
      startup_required: false
      interval_seconds: 15
      timeout_seconds: 3
      unhealthy_threshold: 3

tools:
  harness_echo:
    harness: http_harness
    description: echo
    default_risk: high
    cost_per_call: 10
    input_schema:
      type: object
      properties:
        message: {type: string}
      required: [message]
""",
        encoding="utf-8",
    )
    (root / "config" / "entrypoints.yaml").write_text(
        """
entrypoints:
  mcp_proxy_stdio:
    auth: none
    require_auth: false
  mcp_proxy_sse:
    auth: none
    require_auth: false
admin:
  agent_profiles:
    - research_assistant_v1
""",
        encoding="utf-8",
    )
    return root


@pytest.fixture
async def admin_proxy_ctx(admin_workdir: Path, opa_server: str):
    config = ConfigLoader().load(admin_workdir / "config", opa_base_url=opa_server)
    runtime = build_runtime(
        config,
        opa_url=opa_server,
        env_extra={"PYTHONPATH": str(REPO_ROOT / "src")},
    )
    await runtime.start()
    try:
        proxy = LoopControllerProxyServer(
            runtime,
            ProxyIdentity(agent_id="researcher_001", user_id="alice"),
            entrypoints_config=config.entrypoints_config,
        )
        yield proxy
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_proxy_list_tools_includes_admin_tools(
    admin_proxy_ctx: LoopControllerProxyServer,
) -> None:
    result = await admin_proxy_ctx._handle_list_tools_impl()
    names = {tool.name for tool in result.tools}
    for name in (
        "harness_backend_status",
        "harness_backend_drain",
        "harness_backend_reset",
        "list_recent_decisions",
        "get_decision_status",
        "list_recent_audit_events",
        "trigger_kill_switch",
        "revoke_decision",
    ):
        assert name in names


@pytest.mark.asyncio
async def test_admin_harness_status_returns_backends(
    admin_proxy_ctx: LoopControllerProxyServer,
) -> None:
    result = await admin_proxy_ctx._handle_call_tool_impl(
        "harness_backend_status", {}
    )
    assert not result.isError
    payload = json.loads(result.content[0].text)
    assert len(payload) == 1
    assert payload[0]["name"] == "http_harness"
    assert payload[0]["type"] == "http"


@pytest.mark.asyncio
async def test_admin_harness_drain_and_reset(
    admin_proxy_ctx: LoopControllerProxyServer,
) -> None:
    drain = await admin_proxy_ctx._handle_call_tool_impl(
        "harness_backend_drain", {"name": "http_harness"}
    )
    assert not drain.isError
    payload = json.loads(drain.content[0].text)
    assert payload["drained"] is True

    reset = await admin_proxy_ctx._handle_call_tool_impl(
        "harness_backend_reset", {"name": "http_harness"}
    )
    assert not reset.isError
    payload = json.loads(reset.content[0].text)
    assert payload["reset"] is True


@pytest.mark.asyncio
async def test_admin_harness_drain_missing_backend_errors(
    admin_proxy_ctx: LoopControllerProxyServer,
) -> None:
    result = await admin_proxy_ctx._handle_call_tool_impl(
        "harness_backend_drain", {"name": "missing"}
    )
    assert result.isError
    payload = json.loads(result.content[0].text)
    assert payload["error"] == "not_found"
    assert "missing" in payload["message"]


@pytest.mark.asyncio
async def test_admin_recent_decisions_and_status(
    admin_proxy_ctx: LoopControllerProxyServer,
) -> None:
    # 先触发一次 require_approval 调用以产生 decision
    result = await admin_proxy_ctx._handle_call_tool_impl(
        "harness_echo", {"message": "hello"}
    )
    # harness_echo 在 trusted_local 模式下未声明 trusted，会被 deny
    assert result.isError

    recent = await admin_proxy_ctx._handle_call_tool_impl(
        "list_recent_decisions", {"limit": 5}
    )
    assert not recent.isError
    items = json.loads(recent.content[0].text)
    assert isinstance(items, list)

    if items:
        decision_id = items[0]["decision_id"]
        status = await admin_proxy_ctx._handle_call_tool_impl(
            "get_decision_status", {"decision_id": decision_id}
        )
        assert not status.isError
        payload = json.loads(status.content[0].text)
        assert payload["decision_id"] == decision_id


@pytest.mark.asyncio
async def test_admin_recent_audit_events(
    admin_proxy_ctx: LoopControllerProxyServer,
) -> None:
    result = await admin_proxy_ctx._handle_call_tool_impl(
        "list_recent_audit_events", {"limit": 5}
    )
    assert not result.isError
    items = json.loads(result.content[0].text)
    assert isinstance(items, list)


@pytest.mark.asyncio
async def test_admin_trigger_kill_switch(
    admin_proxy_ctx: LoopControllerProxyServer,
) -> None:
    result = await admin_proxy_ctx._handle_call_tool_impl(
        "trigger_kill_switch",
        {"enabled": True, "reason": "test", "except_tools": [], "except_agents": []},
    )
    assert not result.isError
    payload = json.loads(result.content[0].text)
    assert payload["kill_switch"] is True


@pytest.mark.asyncio
async def test_admin_revoke_decision(
    admin_proxy_ctx: LoopControllerProxyServer,
) -> None:
    result = await admin_proxy_ctx._handle_call_tool_impl(
        "revoke_decision",
        {"type": "agent", "id": "researcher_001", "reason": "test"},
    )
    assert not result.isError
    payload = json.loads(result.content[0].text)
    assert payload["revoked"] is True
    assert payload["type"] == "agent"
