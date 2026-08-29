"""LoopController 单元测试（v0.13.0）。

验证 Agent 驱动治理接口：evaluate / evaluate_and_execute / resume_after_approval。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from loop_controller.controller import build_controller
from loop_controller.infra.approval_store import JsonlApprovalStore
from loop_controller.infra.config_loader import ConfigLoader
from loop_controller.models import ActionProposal, ApprovalRecord

REPO_ROOT = Path(__file__).resolve().parent.parent


def _env_extra() -> dict[str, str]:
    return {"PYTHONPATH": str(REPO_ROOT / "src")}


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    shutil.copytree(REPO_ROOT / "config", root / "config")
    shutil.copytree(REPO_ROOT / "policies", root / "policies")
    (root / "data").mkdir()
    # 使用最小 profile，只保留 web_search，避免触发需要 npx 的工具
    (root / "config" / "profiles.yaml").write_text(
        """
profiles:
  - profile_id: research_assistant_v1
    description: 研究助手岗位说明书
    max_budget_token: 100000
    max_budget_payment: 0.0
    session_block_threshold: 10
    session_risk_threshold: 0.95
    tools:
      web_search:
        allowed: true
        max_calls_per_task: 10
""",
        encoding="utf-8",
    )
    (root / "config" / "mcp_servers.yaml").write_text(
        """
servers:
  email_mock:
    command: ["python", "-m", "loop_controller.mocks.email_server"]
    transport: stdio

tool_mapping:
  web_search: {server: email_mock, mcp_name: web_search, cost_per_call: 200}
""",
        encoding="utf-8",
    )
    (root / "config" / "harness_tools.yaml").write_text(
        "execution:\n"
        "  default_mode: trusted_local\n"
        "trusted_local_tools:\n"
        "  - web_search\n"
        "  - send_email\n",
        encoding="utf-8",
    )
    return root


@pytest.fixture
def approvals_path(workdir: Path) -> Path:
    return workdir / "data" / "approvals.jsonl"


@pytest.mark.asyncio
async def test_evaluate_allow_low_risk(workdir: Path, opa_server: str):
    """低风险工具调用应返回 allow。"""
    config = ConfigLoader().load(workdir / "config", opa_base_url=opa_server)
    controller = await build_controller(config, opa_url=opa_server, env_extra=_env_extra())
    await controller.start()
    try:
        result = await controller.evaluate(
            agent_id="researcher_001",
            user_id="alice",
            tool_name="web_search",
            arguments={"query": "AI compliance"},
            task_context="搜索公开资料",
        )
        assert result.status == "allow"
        assert result.decision is not None
        assert result.decision.verdict == "allow"
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_evaluate_deny_unknown_tool(workdir: Path, opa_server: str):
    """未在 Profile 中声明的工具应返回 deny。"""
    config = ConfigLoader().load(workdir / "config", opa_base_url=opa_server)
    controller = await build_controller(config, opa_url=opa_server, env_extra=_env_extra())
    await controller.start()
    try:
        result = await controller.evaluate(
            agent_id="researcher_001",
            user_id="alice",
            tool_name="send_email",
            arguments={"to": "zhang@company.com"},
            task_context="发送邮件",
        )
        assert result.status == "deny"
        assert "not permitted" in result.reason
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_evaluate_and_execute_allow(workdir: Path, opa_server: str):
    """evaluate_and_execute 对 allow 的工具调用应返回执行结果。"""
    config = ConfigLoader().load(workdir / "config", opa_base_url=opa_server)
    controller = await build_controller(config, opa_url=opa_server, env_extra=_env_extra())
    await controller.start()
    try:
        result = await controller.evaluate_and_execute(
            agent_id="researcher_001",
            user_id="alice",
            tool_name="web_search",
            arguments={"query": "AI compliance"},
            task_context="搜索公开资料",
        )
        assert result.status == "allow"
        assert result.content is not None
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_evaluate_and_execute_require_approval(
    workdir: Path, opa_server: str, approvals_path: Path
):
    """高风险工具调用应返回 require_approval，审批后可恢复执行。"""
    # 覆盖 profile 让 send_email 需要审批
    (workdir / "config" / "profiles.yaml").write_text(
        """
profiles:
  - profile_id: research_assistant_v1
    description: 研究助手岗位说明书
    max_budget_token: 100000
    max_budget_payment: 0.0
    session_block_threshold: 10
    session_risk_threshold: 0.95
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
    # 覆盖 mcp_servers.yaml 加入 send_email 映射
    (workdir / "config" / "mcp_servers.yaml").write_text(
        """
servers:
  email_mock:
    command: ["python", "-m", "loop_controller.mocks.email_server"]
    transport: stdio

tool_mapping:
  web_search: {server: email_mock, mcp_name: web_search, cost_per_call: 200}
  send_email: {server: email_mock, mcp_name: send_email, cost_per_call: 800}
""",
        encoding="utf-8",
    )
    config = ConfigLoader().load(workdir / "config", opa_base_url=opa_server)
    controller = await build_controller(config, opa_url=opa_server, env_extra=_env_extra())
    # 注入指定 approvals 路径，便于写审批结果
    controller._runtime.approval_manager._store = JsonlApprovalStore(str(approvals_path))

    await controller.start()
    try:
        result = await controller.evaluate_and_execute(
            agent_id="researcher_001",
            user_id="alice",
            tool_name="send_email",
            arguments={"to": "zhang@company.com", "subject": "摘要", "body": "请查收"},
            task_context="发送报告",
        )
        assert result.status == "require_approval"
        request_id = result.request_id
        assert request_id is not None

        # 模拟 CLI 审批
        store = controller._runtime.approval_manager._store
        request = store.get_request(result.decision.decision_id)
        record = ApprovalRecord(
            request_id=request.request_id,
            decision_id=request.decision_id,
            verdict="approve",
            approver_id=request.approver_id,
            comment="approved for test",
        )
        store.record_response(record)

        final = await controller.resume_after_approval(request_id)
        assert final.status == "allow"
        assert final.content is not None
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_execute_without_arguments_raises(workdir: Path, opa_server: str):
    """``execute(decision)`` 因缺少 arguments 必须抛出 NotImplementedError。"""
    config = ConfigLoader().load(workdir / "config", opa_base_url=opa_server)
    controller = await build_controller(config, opa_url=opa_server, env_extra=_env_extra())
    await controller.start()
    try:
        result = await controller.evaluate(
            agent_id="researcher_001",
            user_id="alice",
            tool_name="web_search",
            arguments={"query": "AI compliance"},
            task_context="搜索公开资料",
        )
        assert result.status == "allow"
        assert result.decision is not None

        with pytest.raises(NotImplementedError):
            await controller.execute(
                agent_id="researcher_001",
                decision=result.decision,
            )
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_execute_with_proposal(workdir: Path, opa_server: str):
    """先 evaluate 拿到 Decision，再用 execute_with_proposal 执行。"""
    config = ConfigLoader().load(workdir / "config", opa_base_url=opa_server)
    controller = await build_controller(config, opa_url=opa_server, env_extra=_env_extra())
    await controller.start()
    try:
        task, _session = controller._runtime.create_task(
            user_id="alice",
            agent_id="researcher_001",
            description="搜索公开资料",
        )
        eval_result = await controller.evaluate(
            agent_id="researcher_001",
            user_id="alice",
            tool_name="web_search",
            arguments={"query": "AI compliance"},
            task_id=task.task_id,
            task_context="搜索公开资料",
        )
        assert eval_result.status == "allow"
        assert eval_result.decision is not None

        # 复用 Decision 的 call_id/task_id，保证与 checkpoint 预留的 reservation 一致
        proposal = ActionProposal(
            task_id=task.task_id,
            call_id=eval_result.decision.call_id,
            agent_id="researcher_001",
            tool_name="web_search",
            arguments={"query": "AI compliance"},
            task_context="搜索公开资料",
        )
        result = await controller.execute_with_proposal(
            agent_id="researcher_001",
            decision=eval_result.decision,
            proposal=proposal,
        )
        assert result.status == "success"
        assert result.content is not None
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_resume_sees_external_response(
    workdir: Path, opa_server: str, approvals_path: Path
):
    """v0.29.0：外部进程直接写入 approval store 后，controller.resume_after_approval 成功。"""
    # 覆盖 profile 让 send_email 需要审批
    (workdir / "config" / "profiles.yaml").write_text(
        """
profiles:
  - profile_id: research_assistant_v1
    description: 研究助手岗位说明书
    max_budget_token: 100000
    max_budget_payment: 0.0
    session_block_threshold: 10
    session_risk_threshold: 0.95
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
    (workdir / "config" / "mcp_servers.yaml").write_text(
        """
servers:
  email_mock:
    command: ["python", "-m", "loop_controller.mocks.email_server"]
    transport: stdio

tool_mapping:
  web_search: {server: email_mock, mcp_name: web_search, cost_per_call: 200}
  send_email: {server: email_mock, mcp_name: send_email, cost_per_call: 800}
""",
        encoding="utf-8",
    )
    config = ConfigLoader().load(workdir / "config", opa_base_url=opa_server)
    controller = await build_controller(config, opa_url=opa_server, env_extra=_env_extra())
    controller._runtime.approval_manager._store = JsonlApprovalStore(str(approvals_path))

    await controller.start()
    try:
        result = await controller.evaluate_and_execute(
            agent_id="researcher_001",
            user_id="alice",
            tool_name="send_email",
            arguments={"to": "zhang@company.com", "subject": "摘要", "body": "请查收"},
            task_context="发送报告",
        )
        assert result.status == "require_approval"
        request_id = result.request_id
        assert request_id is not None

        request = controller._runtime.approval_manager._store.get_request(
            result.decision.decision_id
        )
        record = ApprovalRecord(
            request_id=request.request_id,
            decision_id=request.decision_id,
            verdict="approve",
            approver_id=request.approver_id,
            comment="approved externally",
        )
        # 模拟外部进程（如 CLI）直接操作同一个 approval store 文件写入审批结果
        external_store = JsonlApprovalStore(str(approvals_path))
        external_store.record_response(record)

        final = await controller.resume_after_approval(request_id)
        assert final.status == "allow"
        assert final.content is not None
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_resume_twice_returns_already_consumed(
    workdir: Path, opa_server: str, approvals_path: Path
):
    """v0.29.0：重复 resume 返回 decision_already_consumed。"""
    (workdir / "config" / "profiles.yaml").write_text(
        """
profiles:
  - profile_id: research_assistant_v1
    description: 研究助手岗位说明书
    max_budget_token: 100000
    max_budget_payment: 0.0
    session_block_threshold: 10
    session_risk_threshold: 0.95
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
    (workdir / "config" / "mcp_servers.yaml").write_text(
        """
servers:
  email_mock:
    command: ["python", "-m", "loop_controller.mocks.email_server"]
    transport: stdio

tool_mapping:
  web_search: {server: email_mock, mcp_name: web_search, cost_per_call: 200}
  send_email: {server: email_mock, mcp_name: send_email, cost_per_call: 800}
""",
        encoding="utf-8",
    )
    config = ConfigLoader().load(workdir / "config", opa_base_url=opa_server)
    controller = await build_controller(config, opa_url=opa_server, env_extra=_env_extra())
    controller._runtime.approval_manager._store = JsonlApprovalStore(str(approvals_path))

    await controller.start()
    try:
        result = await controller.evaluate_and_execute(
            agent_id="researcher_001",
            user_id="alice",
            tool_name="send_email",
            arguments={"to": "zhang@company.com", "subject": "摘要", "body": "请查收"},
            task_context="发送报告",
        )
        assert result.status == "require_approval"
        request_id = result.request_id

        request = controller._runtime.approval_manager._store.get_request(
            result.decision.decision_id
        )
        record = ApprovalRecord(
            request_id=request.request_id,
            decision_id=request.decision_id,
            verdict="approve",
            approver_id=request.approver_id,
            comment="approved externally",
        )
        external_store = JsonlApprovalStore(str(approvals_path))
        external_store.record_response(record)

        first = await controller.resume_after_approval(request_id)
        assert first.status == "allow"
        assert first.content is not None

        second = await controller.resume_after_approval(request_id)
        assert second.status == "error"
        assert second.error_code == "decision_already_consumed"
    finally:
        await controller.aclose()
