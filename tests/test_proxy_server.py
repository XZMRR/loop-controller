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

from loop_controller.infra.config_loader import ConfigLoader
from loop_controller.proxy_server import LoopControllerProxyServer, ProxyIdentity
from loop_controller.runtime import build_runtime
from tests.conftest import write_trusted_local_harness_config

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
    write_trusted_local_harness_config(
        root / "config",
        ["web_search", "send_email"],
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
    """tools/list 返回按 Profile 过滤后的工具，并注入内部工具。"""
    result = await proxy_ctx._handle_list_tools_impl()
    names = {tool.name for tool in result.tools}
    assert names == {"web_search", "send_email", "loop_controller_approval_status"}


@pytest.mark.asyncio
async def test_proxy_allow_tool_executes(
    proxy_ctx: LoopControllerProxyServer,
    sent_emails_path: Path,
) -> None:
    """web_search 是低风险工具，Proxy 直接执行并返回结果。"""
    result = await proxy_ctx._handle_call_tool_impl(
        name="web_search", arguments={"query": "AI compliance"}
    )
    assert not result.isError
    assert len(result.content) == 1
    payload = json.loads(result.content[0].text)
    assert payload["status"] == "ok"


@pytest.mark.asyncio
async def test_proxy_deny_tool_rejected(proxy_ctx: LoopControllerProxyServer) -> None:
    """send_email 收件人不在 allowed_args 范围内，被 deny。"""
    result = await proxy_ctx._handle_call_tool_impl(
        name="send_email",
        arguments={
            "to": "attacker@gmail.com",
            "subject": "test",
            "body": "body",
        },
    )
    assert result.isError
    assert result.content[0].text.startswith("[loop-controller] DENIED:")


@pytest.mark.asyncio
async def test_proxy_require_approval_blocked(proxy_ctx: LoopControllerProxyServer) -> None:
    """send_email 收件人合法但仍需审批；v0.5.1 返回结构化 JSON。"""
    result = await proxy_ctx._handle_call_tool_impl(
        name="send_email",
        arguments={
            "to": "boss@company.com",
            "subject": "report",
            "body": "please review",
        },
    )
    assert result.isError
    payload = json.loads(result.content[0].text)
    assert payload["status"] == "require_approval"
    assert payload["tool_name"] == "send_email"
    assert "decision_id" in payload
    assert "request_id" in payload
    assert "retry_instruction" in payload


@pytest.mark.asyncio
async def test_proxy_retry_approved_executes(
    proxy_ctx: LoopControllerProxyServer,
    sent_emails_path: Path,
) -> None:
    """审批通过后携带 decision_id 重试，应成功执行原工具调用。"""
    # 1. 第一次调用：require_approval
    first = await proxy_ctx._handle_call_tool_impl(
        name="send_email",
        arguments={
            "to": "boss@company.com",
            "subject": "report",
            "body": "please review",
        },
    )
    assert first.isError
    pending = json.loads(first.content[0].text)
    decision_id = pending["decision_id"]
    request_id = pending["request_id"]

    # 2. 人工审批通过
    from loop_controller.models import ApprovalRecord

    runtime = proxy_ctx._runtime
    runtime.approval_manager._store.record_response(
        ApprovalRecord(
            request_id=request_id,
            decision_id=decision_id,
            verdict="approve",
            approver_id="zhang_manager",
            comment="approved",
        )
    )

    # 3. 携带 decision_id 重试
    retry = await proxy_ctx._handle_call_tool_impl(
        name="send_email",
        arguments={
            "_loop_controller_decision_id": decision_id,
            "to": "boss@company.com",
            "subject": "report",
            "body": "please review",
        },
    )
    assert not retry.isError
    text = retry.content[0].text
    assert "queued" in text.lower()


@pytest.mark.asyncio
async def test_proxy_retry_other_user_denied(
    proxy_ctx: LoopControllerProxyServer,
) -> None:
    first = await proxy_ctx._handle_call_tool_impl(
        name="send_email",
        arguments={
            "to": "boss@company.com",
            "subject": "report",
            "body": "please review",
        },
    )
    pending = json.loads(first.content[0].text)
    decision_id = pending["decision_id"]
    request_id = pending["request_id"]

    from loop_controller.models import ApprovalRecord

    runtime = proxy_ctx._runtime
    runtime.approval_manager._store.record_response(
        ApprovalRecord(
            request_id=request_id,
            decision_id=decision_id,
            verdict="approve",
            approver_id="zhang_manager",
            comment="approved",
        )
    )
    agent = runtime.checkpoint._identity.get_agent("researcher_001")
    retry = await proxy_ctx._handle_retry(
        decision_id,
        "send_email",
        {
            "to": "boss@company.com",
            "subject": "report",
            "body": "please review",
        },
        ProxyIdentity(agent_id="researcher_001", user_id="mallory"),
        agent,
    )

    assert retry.isError
    assert "does not belong" in retry.content[0].text


@pytest.mark.asyncio
async def test_proxy_retry_param_mismatch_denied(
    proxy_ctx: LoopControllerProxyServer,
) -> None:
    """携带 decision_id 但参数不一致，应被拒绝。"""
    first = await proxy_ctx._handle_call_tool_impl(
        name="send_email",
        arguments={
            "to": "boss@company.com",
            "subject": "report",
            "body": "please review",
        },
    )
    pending = json.loads(first.content[0].text)
    decision_id = pending["decision_id"]
    request_id = pending["request_id"]

    from loop_controller.models import ApprovalRecord

    runtime = proxy_ctx._runtime
    runtime.approval_manager._store.record_response(
        ApprovalRecord(
            request_id=request_id,
            decision_id=decision_id,
            verdict="approve",
            approver_id="zhang_manager",
            comment="approved",
        )
    )

    retry = await proxy_ctx._handle_call_tool_impl(
        name="send_email",
        arguments={
            "_loop_controller_decision_id": decision_id,
            "to": "other@company.com",  # 参数不一致
            "subject": "report",
            "body": "please review",
        },
    )
    assert retry.isError
    assert "mismatch" in retry.content[0].text.lower()


@pytest.mark.asyncio
async def test_proxy_retry_not_approved_still_blocked(
    proxy_ctx: LoopControllerProxyServer,
) -> None:
    """未审批时携带 decision_id 重试，仍返回 require_approval。"""
    first = await proxy_ctx._handle_call_tool_impl(
        name="send_email",
        arguments={
            "to": "boss@company.com",
            "subject": "report",
            "body": "please review",
        },
    )
    pending = json.loads(first.content[0].text)
    decision_id = pending["decision_id"]

    retry = await proxy_ctx._handle_call_tool_impl(
        name="send_email",
        arguments={
            "_loop_controller_decision_id": decision_id,
            "to": "boss@company.com",
            "subject": "report",
            "body": "please review",
        },
    )
    assert retry.isError
    assert "not approved" in retry.content[0].text.lower()


@pytest.mark.asyncio
async def test_proxy_approval_status_pending(
    proxy_ctx: LoopControllerProxyServer,
) -> None:
    """v0.7.0：未审批时查询返回 pending。"""
    first = await proxy_ctx._handle_call_tool_impl(
        name="send_email",
        arguments={
            "to": "boss@company.com",
            "subject": "report",
            "body": "please review",
        },
    )
    pending = json.loads(first.content[0].text)
    decision_id = pending["decision_id"]

    result = await proxy_ctx._handle_call_tool_impl(
        name="loop_controller_approval_status",
        arguments={"decision_id": decision_id},
    )
    assert not result.isError
    payload = json.loads(result.content[0].text)
    assert payload["status"] == "pending"
    assert payload["can_retry"] is False


@pytest.mark.asyncio
async def test_proxy_approval_status_approved(
    proxy_ctx: LoopControllerProxyServer,
) -> None:
    """v0.7.0：审批后查询返回 approved。"""
    first = await proxy_ctx._handle_call_tool_impl(
        name="send_email",
        arguments={
            "to": "boss@company.com",
            "subject": "report",
            "body": "please review",
        },
    )
    pending = json.loads(first.content[0].text)
    decision_id = pending["decision_id"]
    request_id = pending["request_id"]

    from loop_controller.models import ApprovalRecord

    runtime = proxy_ctx._runtime
    runtime.approval_manager._store.record_response(
        ApprovalRecord(
            request_id=request_id,
            decision_id=decision_id,
            verdict="approve",
            approver_id="zhang_manager",
            comment="approved",
        )
    )

    result = await proxy_ctx._handle_call_tool_impl(
        name="loop_controller_approval_status",
        arguments={"decision_id": decision_id},
    )
    assert not result.isError
    payload = json.loads(result.content[0].text)
    assert payload["status"] == "approved"
    assert payload["can_retry"] is True


@pytest.mark.asyncio
async def test_proxy_approval_status_denied(
    proxy_ctx: LoopControllerProxyServer,
) -> None:
    """v0.7.0：审批拒绝后查询返回 denied。"""
    first = await proxy_ctx._handle_call_tool_impl(
        name="send_email",
        arguments={
            "to": "boss@company.com",
            "subject": "report",
            "body": "please review",
        },
    )
    pending = json.loads(first.content[0].text)
    decision_id = pending["decision_id"]
    request_id = pending["request_id"]

    from loop_controller.models import ApprovalRecord

    runtime = proxy_ctx._runtime
    runtime.approval_manager._store.record_response(
        ApprovalRecord(
            request_id=request_id,
            decision_id=decision_id,
            verdict="deny",
            approver_id="zhang_manager",
            comment="denied",
        )
    )

    result = await proxy_ctx._handle_call_tool_impl(
        name="loop_controller_approval_status",
        arguments={"decision_id": decision_id},
    )
    assert not result.isError
    payload = json.loads(result.content[0].text)
    assert payload["status"] == "denied"
    assert payload["can_retry"] is False


@pytest.mark.asyncio
async def test_proxy_approval_status_not_found(
    proxy_ctx: LoopControllerProxyServer,
) -> None:
    """v0.7.0：不存在的 decision_id 返回 not_found。"""
    result = await proxy_ctx._handle_call_tool_impl(
        name="loop_controller_approval_status",
        arguments={"decision_id": "nonexistent"},
    )
    assert not result.isError
    payload = json.loads(result.content[0].text)
    assert payload["status"] == "not_found"
    assert payload["can_retry"] is False


@pytest.mark.asyncio
async def test_proxy_session_block_after_consecutive_denies(
    proxy_ctx: LoopControllerProxyServer,
) -> None:
    """同一 Session 连续两次 deny 后，第三次任何调用都被 session 硬熔断 deny。"""
    params = {
        "to": "attacker@gmail.com",
        "subject": "test",
        "body": "body",
    }
    first = await proxy_ctx._handle_call_tool_impl(name="send_email", arguments=params)
    assert first.isError and "DENIED" in first.content[0].text

    second = await proxy_ctx._handle_call_tool_impl(name="send_email", arguments=params)
    assert second.isError and "DENIED" in second.content[0].text

    third = await proxy_ctx._handle_call_tool_impl(name="send_email", arguments=params)
    assert third.isError
    text = third.content[0].text
    assert "session blocked" in text.lower() or "consecutive" in text.lower()


@pytest.mark.asyncio
async def test_proxy_retry_survives_runtime_restart(
    workdir: Path,
    sent_emails_path: Path,
    opa_server: str,
) -> None:
    """v0.6.0：新 Runtime 使用同一数据目录时，能恢复 Task 并完成审批后重试。

    注：Windows 上完整关闭并立即重启 MCP stdio 子进程会触发 anyio cancel scope
    竞态，因此本测试保持 Runtime A 不关闭，用 Runtime B 模拟"新进程读取持久化数据"的场景。
    """
    config = ConfigLoader().load(workdir / "config", opa_base_url=opa_server)

    runtime_a = build_runtime(
        config,
        opa_url=opa_server,
        env_extra={
            "PYTHONPATH": str(REPO_ROOT / "src"),
            "SENT_EMAILS_PATH": str(sent_emails_path),
        },
    )
    await runtime_a.start()
    try:
        proxy_a = LoopControllerProxyServer(
            runtime_a,
            ProxyIdentity(agent_id="researcher_001", user_id="alice"),
        )
        first = await proxy_a._handle_call_tool_impl(
            name="send_email",
            arguments={
                "to": "boss@company.com",
                "subject": "report",
                "body": "please review",
            },
        )
        assert first.isError
        pending = json.loads(first.content[0].text)
        decision_id = pending["decision_id"]
        request_id = pending["request_id"]

        # 在 Runtime A 内审批通过
        from loop_controller.models import ApprovalRecord

        runtime_a.approval_manager._store.record_response(
            ApprovalRecord(
                request_id=request_id,
                decision_id=decision_id,
                verdict="approve",
                approver_id="zhang_manager",
                comment="approved",
            )
        )

        # Runtime B 使用同一数据目录启动；Proxy B 重试
        runtime_b = build_runtime(
            config,
            opa_url=opa_server,
            env_extra={
                "PYTHONPATH": str(REPO_ROOT / "src"),
                "SENT_EMAILS_PATH": str(sent_emails_path),
            },
        )
        await runtime_b.start()
        try:
            proxy_b = LoopControllerProxyServer(
                runtime_b,
                ProxyIdentity(agent_id="researcher_001", user_id="alice"),
            )
            retry = await proxy_b._handle_call_tool_impl(
                name="send_email",
                arguments={
                    "_loop_controller_decision_id": decision_id,
                    "to": "boss@company.com",
                    "subject": "report",
                    "body": "please review",
                },
            )
            assert not retry.isError
            assert "queued" in retry.content[0].text.lower()
        finally:
            await runtime_b.aclose()
    finally:
        await runtime_a.aclose()


def _fake_request_context() -> Any:
    """stdio 模式下 _resolve_identity 不使用 ctx.request，返回 None 即可。"""
    return None
