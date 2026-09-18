"""LoopController 单元测试（v0.13.0）。

验证 Agent 驱动治理接口：evaluate / evaluate_and_execute / resume_after_approval。
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from loop_controller.controller import build_controller
from loop_controller.execution_security import ExecutionRequestContext, current_execution_request
from loop_controller.executors.base import (
    ExecutionContext,
    ExecutionReceiptType,
    ExecutionTerminalStatus,
    WorkloadAuthMethod,
    WorkloadIdentity,
    issue_execution_receipt,
)
from loop_controller.infra.approval_store import JsonlApprovalStore
from loop_controller.infra.config_loader import ConfigLoader
from loop_controller.models import ActionProposal, ApprovalRecord, CapabilityProfile, ToolResult

REPO_ROOT = Path(__file__).resolve().parent.parent


class _StrictReceiptExecutor:
    supports_strict_security = True
    security_egress_type = "protected_http"
    security_capabilities = frozenset({"execution_receipt_v1"})

    def __init__(self, terminal: ExecutionTerminalStatus) -> None:
        self.terminal = terminal
        self.calls = 0

    def secret_refs_for(self, tool_name: str) -> list[str]:
        return []

    async def execute(
        self, tool_name: str, arguments: dict[str, Any], context: ExecutionContext
    ) -> ToolResult:
        self.calls += 1
        content = {"terminal": self.terminal.value}
        receipt = issue_execution_receipt(
            receipt_type=ExecutionReceiptType.CONTROLLER_EXECUTION_RECORD,
            context=context,
            attester_workload_id="executor",
            executor="http",
            backend="protected",
            status=self.terminal,
            result=content,
        )
        return ToolResult(
            call_id=context.call_id,
            task_id=context.task_id,
            tool_name=tool_name,
            status="success" if self.terminal == ExecutionTerminalStatus.SUCCESS else "error",
            content=content,
            terminal_status=self.terminal.value,
            error_code=(
                None
                if self.terminal == ExecutionTerminalStatus.SUCCESS
                else f"http_{self.terminal.value}"
            ),
            execution_receipt=receipt,
        )

    async def list_tools(self, profile: CapabilityProfile) -> list[Any]:
        return []


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
        "  trusted_local_tools:\n"
        "    - web_search\n",
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


@pytest.mark.parametrize(
    "terminal",
    [
        ExecutionTerminalStatus.SUCCESS,
        ExecutionTerminalStatus.TIMEOUT,
        ExecutionTerminalStatus.CANCELLED,
    ],
)
@pytest.mark.asyncio
async def test_strict_approval_resume_preserves_terminal_status_and_receipt_without_bypass(
    terminal: ExecutionTerminalStatus,
    workdir: Path,
    opa_server: str,
    approvals_path: Path,
) -> None:
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
    executor = _StrictReceiptExecutor(terminal)
    controller._runtime.checkpoint._executor_registry.register("send_email", executor)

    await controller.start()
    try:
        pending = await controller.evaluate_and_execute(
            agent_id="researcher_001",
            user_id="alice",
            tool_name="send_email",
            arguments={"to": "zhang@company.com"},
            task_context="发送报告",
        )
        assert pending.status == "require_approval"
        assert pending.request_id is not None
        assert pending.decision is not None
        assert executor.calls == 0

        request = controller._runtime.approval_manager.get_request_by_id(pending.request_id)
        controller._runtime.approval_manager._store.record_response(
            ApprovalRecord(
                request_id=request.request_id,
                decision_id=request.decision_id,
                verdict="approve",
                approver_id=request.approver_id,
                comment="approved for strict execution",
            )
        )
        context = ExecutionRequestContext(
            workload_identity=WorkloadIdentity(
                workload_id="kernel",
                service="go-kernel",
                principal="kernel",
                tenant_id="tenant",
                authenticated_at=datetime.now(UTC),
                auth_method=WorkloadAuthMethod.MTLS,
            ),
            request_id=pending.request_id,
            interaction_id="interaction-1",
            decision_id=pending.decision.decision_id,
            task_id=pending.decision.task_id,
            call_id=pending.decision.call_id,
            delegation_jti="jti-1",
            tenant_id="tenant",
            security_capabilities=frozenset({"execution_receipt_v1"}),
        )
        token = current_execution_request.set(context)
        try:
            final = await controller.resume_after_approval(pending.request_id)
        finally:
            current_execution_request.reset(token)

        assert executor.calls == 1
        assert final.status == ("allow" if terminal == ExecutionTerminalStatus.SUCCESS else "error")
        assert final.terminal_status == terminal.value
        assert final.execution_receipt is not None
        assert final.execution_receipt.status == terminal
        assert final.execution_receipt.receipt_id
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
