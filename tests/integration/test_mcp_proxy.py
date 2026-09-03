"""MCP Proxy 端到端集成测试。

通过 stdio 启动 `python -m loop_controller.cli proxy`，
用真实 MCP client 连接后调用工具，验证 Loop Controller 治理链路。
"""

from __future__ import annotations

import contextlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from tests.conftest import write_trusted_local_harness_config
from tests.controller_helpers import env_extra

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _disable_interaction_config(config_dir: Path) -> None:
    (config_dir / "interaction_profiles.yaml").write_text(
        "interaction_profiles: []\n", encoding="utf-8"
    )
    (config_dir / "agent_trust.yaml").write_text("agent_trust: []\n", encoding="utf-8")
    (config_dir / "delegation_policies.yaml").write_text(
        "delegation_policies: []\n", encoding="utf-8"
    )


@pytest.fixture
def mcp_proxy_workdir(tmp_path: Path) -> Path:
    """准备 MCP Proxy 集成测试配置。"""
    root = tmp_path / "project"
    root.mkdir()
    shutil.copytree(REPO_ROOT / "config", root / "config")
    shutil.copytree(REPO_ROOT / "policies", root / "policies")
    shutil.copytree(REPO_ROOT / "src", root / "src")
    (root / "data").mkdir()

    (root / "config" / "mcp_servers.yaml").write_text(
        f"""
servers:
  email_mock:
    command: [{json.dumps(sys.executable)}, "-m", "loop_controller.mocks.email_server"]
    transport: stdio

tool_mapping:
  web_search:  {{server: email_mock, mcp_name: web_search, cost_per_call: 200}}
""",
        encoding="utf-8",
    )
    (root / "config" / "profiles.yaml").write_text(
        """
profiles:
  - profile_id: integration_profile
    description: MCP Proxy 集成测试
    max_budget_token: 100000
    max_budget_payment: 0.0
    tools:
      web_search:
        allowed: true
        max_calls_per_task: 10
""",
        encoding="utf-8",
    )
    (root / "config" / "agents.yaml").write_text(
        """
agents:
  - agent_id: mcp_agent
    name: MCP Agent
    profile_id: integration_profile
    owner_id: alice
    identity:
      issuer: https://test.local
      subject: agent://mcp_agent/test

users:
  - user_id: alice
    display_name: Alice
  - user_id: zhang_manager
    display_name: 张经理
""",
        encoding="utf-8",
    )
    (root / "config" / "entrypoints.yaml").write_text(
        """
entrypoints:
  mcp_proxy_stdio:
    require_auth: false
admin:
  agent_profiles:
    - integration_profile
""",
        encoding="utf-8",
    )
    write_trusted_local_harness_config(root / "config", ["web_search"])
    _disable_interaction_config(root / "config")
    return root


@pytest.fixture
def mcp_proxy_approval_workdir(tmp_path: Path) -> Path:
    """准备含审批工具的 MCP Proxy 集成测试配置。"""
    root = tmp_path / "project"
    root.mkdir()
    shutil.copytree(REPO_ROOT / "config", root / "config")
    shutil.copytree(REPO_ROOT / "policies", root / "policies")
    shutil.copytree(REPO_ROOT / "src", root / "src")
    (root / "data").mkdir()

    (root / "config" / "mcp_servers.yaml").write_text(
        f"""
servers:
  email_mock:
    command: [{json.dumps(sys.executable)}, "-m", "loop_controller.mocks.email_server"]
    transport: stdio

tool_mapping:
  web_search:  {{server: email_mock, mcp_name: web_search, cost_per_call: 200}}
  send_email: {{server: email_mock, mcp_name: send_email, cost_per_call: 500}}
""",
        encoding="utf-8",
    )
    (root / "config" / "profiles.yaml").write_text(
        """
profiles:
  - profile_id: integration_profile
    description: MCP Proxy 审批集成测试
    max_budget_token: 100000
    max_budget_payment: 0.0
    tools:
      web_search:
        allowed: true
        max_calls_per_task: 10
      send_email:
        allowed: true
        require_approval: true
        max_calls_per_task: 1
        allowed_args:
          to:
            - "*@company.com"
""",
        encoding="utf-8",
    )
    (root / "config" / "agents.yaml").write_text(
        """
agents:
  - agent_id: mcp_agent
    name: MCP Agent
    profile_id: integration_profile
    owner_id: zhang_manager
    identity:
      issuer: https://test.local
      subject: agent://mcp_agent/test

users:
  - user_id: alice
    display_name: Alice
  - user_id: zhang_manager
    display_name: 张经理
""",
        encoding="utf-8",
    )
    (root / "config" / "entrypoints.yaml").write_text(
        """
entrypoints:
  mcp_proxy_stdio:
    require_auth: false
admin:
  agent_profiles:
    - integration_profile
""",
        encoding="utf-8",
    )
    write_trusted_local_harness_config(root / "config", ["web_search", "send_email"])
    _disable_interaction_config(root / "config")
    return root


async def _open_session(params: StdioServerParameters) -> Any:
    """手动管理 stdio_client + ClientSession，忽略 Windows anyio 清理竞态异常。"""
    stdio_cm = stdio_client(params)
    read_stream: Any = None
    write_stream: Any = None
    session_cm: Any = None
    session: Any = None
    try:
        read_stream, write_stream = await stdio_cm.__aenter__()
        session_cm = ClientSession(read_stream, write_stream)
        session = await session_cm.__aenter__()
        await session.initialize()
        return session, stdio_cm, session_cm
    except Exception:
        if session_cm is not None:
            with contextlib.suppress(Exception):
                await session_cm.__aexit__(*sys.exc_info())
        with contextlib.suppress(Exception):
            await stdio_cm.__aexit__(*sys.exc_info())
        raise


@pytest.fixture
async def mcp_proxy_session(mcp_proxy_workdir: Path, opa_server: str):
    """启动 MCP Proxy 子进程并建立 ClientSession。"""
    server_params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "loop_controller.cli",
            "--config-dir",
            str(mcp_proxy_workdir / "config"),
            "proxy",
            "--agent-id",
            "mcp_agent",
            "--user-id",
            "alice",
            "--transport",
            "stdio",
            "--opa-url",
            opa_server,
        ],
        env={**env_extra(), "LOOP_CONTROLLER_AUDIT_HMAC_KEY": "a" * 64},
    )

    session, stdio_cm, session_cm = await _open_session(server_params)
    try:
        yield session
    finally:
        with contextlib.suppress(Exception):
            await session_cm.__aexit__(None, None, None)
        with contextlib.suppress(Exception):
            await stdio_cm.__aexit__(None, None, None)


@pytest.fixture
async def mcp_proxy_approval_session(mcp_proxy_approval_workdir: Path, opa_server: str):
    """启动含审批配置的 MCP Proxy 子进程并建立 ClientSession。"""
    server_params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "loop_controller.cli",
            "--config-dir",
            str(mcp_proxy_approval_workdir / "config"),
            "proxy",
            "--agent-id",
            "mcp_agent",
            "--user-id",
            "alice",
            "--transport",
            "stdio",
            "--opa-url",
            opa_server,
        ],
        env={**env_extra(), "LOOP_CONTROLLER_AUDIT_HMAC_KEY": "a" * 64},
    )

    session, stdio_cm, session_cm = await _open_session(server_params)
    try:
        yield session
    finally:
        with contextlib.suppress(Exception):
            await session_cm.__aexit__(None, None, None)
        with contextlib.suppress(Exception):
            await stdio_cm.__aexit__(None, None, None)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mcp_proxy_lists_governed_tools(
    mcp_proxy_session: Any,
) -> None:
    """MCP Proxy 暴露被治理的真实 MCP 工具。"""
    session = mcp_proxy_session
    tools = await session.list_tools()
    names = {tool.name for tool in tools.tools}
    assert "web_search" in names


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mcp_proxy_executes_governed_tool(
    mcp_proxy_session: Any,
    mcp_proxy_workdir: Path,
) -> None:
    """MCP Client 调用 web_search，Loop Controller 治理后返回结果。"""
    session = mcp_proxy_session
    result = await session.call_tool("web_search", {"query": "AI governance"})

    assert not result.isError
    text = result.content[0].text
    payload = json.loads(text)
    assert payload.get("status") == "ok"

    # 验证审计记录：从 proxy 子进程的审计日志中读取
    audit_path = mcp_proxy_workdir / "data" / "audit.jsonl"
    if audit_path.exists():
        events = [
            json.loads(line)
            for line in audit_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert any(e.get("target") == "web_search" for e in events)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mcp_proxy_admin_tool_status(
    mcp_proxy_session: Any,
) -> None:
    """MCP Client 可调用 admin 工具查询 backend 状态。"""
    session = mcp_proxy_session
    result = await session.call_tool("harness_backend_status", {})

    assert not result.isError
    text = result.content[0].text
    payload = json.loads(text)
    assert isinstance(payload, list)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mcp_proxy_require_approval_returns_decision(
    mcp_proxy_approval_session: Any,
) -> None:
    """敏感工具触发审批，MCP Proxy 返回结构化 require_approval JSON。"""
    session = mcp_proxy_approval_session
    result = await session.call_tool(
        "send_email",
        {"to": "bob@company.com", "subject": "test", "body": "hello"},
    )

    assert result.isError
    text = result.content[0].text
    payload = json.loads(text)
    assert payload["status"] == "require_approval"
    assert payload["tool_name"] == "send_email"
    assert "decision_id" in payload
    assert "request_id" in payload
    assert "retry_instruction" in payload


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mcp_proxy_approval_status_pending(
    mcp_proxy_approval_session: Any,
) -> None:
    """require_approval 后可用 loop_controller_approval_status 查询 pending 状态。"""
    session = mcp_proxy_approval_session
    approval_result = await session.call_tool(
        "send_email",
        {"to": "bob@company.com", "subject": "test", "body": "hello"},
    )
    assert approval_result.isError
    decision_id = json.loads(approval_result.content[0].text)["decision_id"]

    status_result = await session.call_tool(
        "loop_controller_approval_status",
        {"decision_id": decision_id},
    )
    assert not status_result.isError
    payload = json.loads(status_result.content[0].text)
    assert payload["status"] == "pending"
    assert payload["decision_id"] == decision_id
    assert payload["can_retry"] is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mcp_proxy_deny_unknown_tool(
    mcp_proxy_session: Any,
) -> None:
    """调用 Profile 未授权的工具时返回 deny。"""
    session = mcp_proxy_session
    result = await session.call_tool("not_allowed_tool", {"x": 1})

    assert result.isError
    text = result.content[0].text
    assert "DENIED" in text or "deny" in text.lower()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mcp_proxy_deny_external_recipient(
    mcp_proxy_approval_session: Any,
) -> None:
    """send_email 收件人不在白名单内时返回 deny。"""
    session = mcp_proxy_approval_session
    result = await session.call_tool(
        "send_email",
        {"to": "bob@gmail.com", "subject": "test", "body": "hello"},
    )

    assert result.isError
    text = result.content[0].text
    assert "DENIED" in text or "deny" in text.lower()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mcp_proxy_invalid_arguments_error(
    mcp_proxy_approval_session: Any,
) -> None:
    """缺少必要参数时底层 MCP server 返回错误，Proxy 透传错误。"""
    session = mcp_proxy_approval_session
    result = await session.call_tool("send_email", {})

    assert result.isError
