"""真实 MCP 组件的端到端测试（v0.14.0）。

使用 ``LoopController`` + 真实 ``MCPGateway`` 拉起本地 ``email_mock`` server，
验证邮件真的通过 MCP 工具发出并持久化到 ``sent_emails.jsonl``。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from loop_controller.models import ApprovalRecord
from tests.controller_helpers import controller_for

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    shutil.copytree(REPO_ROOT / "config", root / "config")
    shutil.copytree(REPO_ROOT / "policies", root / "policies")
    (root / "data").mkdir()
    # 覆盖 mcp_servers.yaml：只保留本地 email_mock，移除需要 npx 的 filesystem server
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
    # 覆盖 profiles.yaml：仅声明 email_mock 提供的工具
    (root / "config" / "profiles.yaml").write_text(
        """
profiles:
  - profile_id: research_assistant_v1
    description: 研究助手岗位说明书
    max_budget_token: 100000
    max_budget_payment: 0.0
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


@pytest.mark.asyncio
async def test_real_mcp_email_sent_after_approval(
    workdir: Path,
    sent_emails_path: Path,
    opa_server: str,
) -> None:
    """真实 MCP 链路：web_search 直接执行，send_email 需审批，审批后真实发出。"""
    controller = await controller_for(
        workdir,
        opa_server,
        extra_env={"SENT_EMAILS_PATH": str(sent_emails_path)},
    )

    try:
        search_result = await controller.evaluate_and_execute(
            agent_id="researcher_001",
            user_id="alice",
            tool_name="web_search",
            arguments={"query": "AI compliance"},
            task_context="真实 MCP E2E：搜索并发送邮件",
        )
        assert search_result.status == "allow"

        email_result = await controller.evaluate_and_execute(
            agent_id="researcher_001",
            user_id="alice",
            tool_name="send_email",
            arguments={"to": "zhang@company.com", "subject": "摘要", "body": "请查收"},
            task_context="真实 MCP E2E：搜索并发送邮件",
        )
        assert email_result.status == "require_approval"
        assert email_result.decision is not None

        store = controller._runtime.approval_manager._store
        request = store.get_request(email_result.decision.decision_id)
        record = ApprovalRecord(
            request_id=request.request_id,
            decision_id=email_result.decision.decision_id,
            verdict="approve",
            approver_id=request.approver_id or "zhang_manager",
            comment="approved for real mcp e2e",
        )
        store.record_response(record)

        final = await controller.resume_after_approval(email_result.request_id)
        assert final.status == "allow"
    finally:
        await controller.aclose()

    # 验证 email_mock 真的写入了发出邮件
    assert sent_emails_path.exists()
    lines = sent_emails_path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["tool"] == "send_email"
    assert payload["arguments"]["to"] == "zhang@company.com"
    assert payload["arguments"]["subject"] == "摘要"
