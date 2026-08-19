"""MCP Proxy Server 测试（v0.5.0）。

验证 Loop Controller 作为 MCP Server 时，外部 Agent 的 tool call 能被 R2/R3 治理：
- tools/list 透传；
- allow 工具真实执行；
- deny 工具被拒绝；
- require_approval 直接返回 BLOCKED 并附带审批指引；
- 同一 Session 连续拒绝会触发 session 硬熔断。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from mcp_types import (  # type: ignore[import-not-found]
    CallToolRequestParams,
    PaginatedRequestParams,
)

from loop_controller.infra.config_loader import ConfigLoader
from loop_controller.proxy_server import LoopControllerProxyServer, ProxyIdentity
from loop_controller.runtime import build_runtime

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
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
  send_email:  {server: email_mock, mcp_name: send_email, cost_per_call: 800}
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
      send_email:
        allowed: true
        require_approval: true
        allowed_args:
          to: ["*@company.com"]
        max_calls_per_task: 1
""",
        encoding="utf-8",
    )
    return root


@pytest.fixture
def sent_emails_path(workdir: Path) -> Path:
    return workdir / "data" / "sent_emails.jsonl"


@pytest.fixture
async def proxy_ctx(workdir: Path, sent_emails_path: Path, opa_server: str):
    """构造并启动 Proxy Server 所需上下文，测试结束后自动清理。"""
    config = ConfigLoader().load(workdir / "config", opa_base_url=opa_server)
    runtime = build_runtime(
        config,
        opa_url=opa_server,
        env_extra={
            "PYTHONPATH": str(REPO_ROOT / "src"),
            "SENT_EMAILS_PATH": str(sent_emails_path),
        },
    )
    await runtime.start()
    try:
        proxy = LoopControllerProxyServer(
            runtime,
            ProxyIdentity(agent_id="researcher_001", user_id="alice"),
        )
        yield proxy
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_proxy_list_tools(proxy_ctx: LoopControllerProxyServer) -> None:
    """tools/list 返回按 Profile 过滤后的工具。"""
    result = await proxy_ctx._handle_list_tools(
        _fake_request_context(),  # type: ignore[arg-type]
        PaginatedRequestParams(),
    )
    names = {tool.name for tool in result.tools}
    assert names == {"web_search", "send_email"}


@pytest.mark.asyncio
async def test_proxy_allow_tool_executes(
    proxy_ctx: LoopControllerProxyServer,
    sent_emails_path: Path,
) -> None:
    """web_search 是低风险工具，Proxy 直接执行并返回结果。"""
    result = await proxy_ctx._handle_call_tool(
        _fake_request_context(),
        CallToolRequestParams(name="web_search", arguments={"query": "AI compliance"}),
    )
    assert not result.is_error
    assert len(result.content) == 1
    payload = json.loads(result.content[0].text)
    assert payload["status"] == "ok"


@pytest.mark.asyncio
async def test_proxy_deny_tool_rejected(proxy_ctx: LoopControllerProxyServer) -> None:
    """send_email 收件人不在 allowed_args 范围内，被 deny。"""
    result = await proxy_ctx._handle_call_tool(
        _fake_request_context(),
        CallToolRequestParams(
            name="send_email",
            arguments={
                "to": "attacker@gmail.com",
                "subject": "test",
                "body": "body",
            },
        ),
    )
    assert result.is_error
    assert result.content[0].text.startswith("[loop-controller] DENIED:")


@pytest.mark.asyncio
async def test_proxy_require_approval_blocked(proxy_ctx: LoopControllerProxyServer) -> None:
    """send_email 收件人合法但仍需审批；v0.5.0 直接 BLOCKED 并返回 decision_id。"""
    result = await proxy_ctx._handle_call_tool(
        _fake_request_context(),
        CallToolRequestParams(
            name="send_email",
            arguments={
                "to": "boss@company.com",
                "subject": "report",
                "body": "please review",
            },
        ),
    )
    assert result.is_error
    text = result.content[0].text
    assert text.startswith("[loop-controller] BLOCKED: requires human approval")
    assert "decision_id=" in text


@pytest.mark.asyncio
async def test_proxy_session_block_after_consecutive_denies(
    proxy_ctx: LoopControllerProxyServer,
) -> None:
    """同一 Session 连续两次 deny 后，第三次任何调用都被 session 硬熔断 deny。"""
    params = CallToolRequestParams(
        name="send_email",
        arguments={
            "to": "attacker@gmail.com",
            "subject": "test",
            "body": "body",
        },
    )
    first = await proxy_ctx._handle_call_tool(_fake_request_context(), params)
    assert first.is_error and "DENIED" in first.content[0].text

    second = await proxy_ctx._handle_call_tool(_fake_request_context(), params)
    assert second.is_error and "DENIED" in second.content[0].text

    third = await proxy_ctx._handle_call_tool(_fake_request_context(), params)
    assert third.is_error
    text = third.content[0].text
    assert "session blocked" in text.lower() or "consecutive" in text.lower()


def _fake_request_context() -> Any:
    """stdio 模式下 _resolve_identity 不使用 ctx.request，返回 None 即可。"""
    return None
