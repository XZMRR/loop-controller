"""端到端验收测试（T3.4）：A5/A12/A13/A14 + 完整事件序列。

依赖 OPA sidecar（由 module 级 fixture 启动）与真实配置文件。
为避免 CI/沙箱对 npx/MCP server 的依赖，MCPGateway 用 FakeGateway 替换；
配置加载、OPA 策略、R0-delegate、审计链、掩码均走真实实现。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from loop_controller.approval_manager import AsyncApprovalManager
from loop_controller.budget import InMemoryBudgetLedger
from loop_controller.checkpoint import Checkpoint, InMemoryDecisionStore
from loop_controller.classifier import RuleBasedClassifier
from loop_controller.infra.approval_store import JsonlApprovalStore
from loop_controller.infra.audit_store import JsonlAuditStore
from loop_controller.infra.config_loader import ConfigLoader
from loop_controller.infra.conversation_store import JsonlConversationStore
from loop_controller.infra.identity import ConfigIdentityProvider
from loop_controller.infra.policy_store import FilePolicyStore
from loop_controller.masker import Masker
from loop_controller.mcp_gateway import MCPGateway
from loop_controller.models import (
    Agent,
    ApprovalRecord,
    AuditEvent,
    Task,
    TaskRunResult,
    ToolResult,
)
from loop_controller.permission_interaction import ConfigPermissionInteractionAnalyzer
from loop_controller.planner import ScriptedPlanner
from loop_controller.policy_engine import OPAPolicyEngine
from loop_controller.risk_state import JsonlRiskStateStore, RiskStateManager
from loop_controller.runtime import Runtime, resume_task, run_task
from loop_controller.session import SessionManager

REPO_ROOT = Path(__file__).resolve().parent.parent
OPA_BIN = Path(os.environ.get("OPA_PATH", REPO_ROOT / "tools" / "opa.exe"))


class _FakeGateway(MCPGateway):
    def __init__(self) -> None:  # noqa: D107
        pass

    async def start(self) -> None:
        pass

    async def aclose(self) -> None:
        pass

    async def list_tools(self, profile):
        return []

    async def call_tool(self, tool_name: str, arguments: dict, call_id: str, task_id: str) -> ToolResult:
        return ToolResult(
            call_id=call_id,
            task_id=task_id,
            tool_name=tool_name,
            status="success",
            content=f"ok:{tool_name}",
        )


@pytest.fixture(scope="module")
def opa_server() -> str:
    """启动 OPA 并加载仓库真实 policies/。"""
    if not OPA_BIN.exists():
        pytest.skip("opa.exe 不存在")
    policy_dir = REPO_ROOT / "policies"
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen(
        [str(OPA_BIN), "run", "--server", "--bundle", str(policy_dir), "--addr", f"127.0.0.1:{port}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 20
    ready = False
    while time.time() < deadline:
        try:
            if httpx.get(f"{base_url}/health", timeout=1, trust_env=False).status_code == 200:
                ready = True
                break
        except Exception:
            time.sleep(0.3)
    if not ready:
        proc.terminate()
        pytest.skip("OPA 无法启动")
    yield base_url
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    """复制 config/policies 到临时目录；创建 data 目录。"""
    root = tmp_path / "project"
    root.mkdir()
    shutil.copytree(REPO_ROOT / "config", root / "config")
    shutil.copytree(REPO_ROOT / "policies", root / "policies")
    (root / "data").mkdir()
    return root


def _build_task(agent: Agent) -> Task:
    return Task(
        task_id="e2e-research-001",
        session_id="e2e-research-001",
        user_id="alice",
        agent_id=agent.agent_id,
        description="调研 AI 合规并发送摘要邮件",
    )


def _runtime_from_config(workdir: Path, opa_url: str, plan_path: Path | None = None) -> Runtime:
    """用真实配置加载 + 真实 OPA + FakeGateway 构造 Runtime。"""
    config = ConfigLoader().load(workdir / "config", opa_base_url=opa_url)
    identity = ConfigIdentityProvider(config.agents, config.users)
    policy_store = FilePolicyStore(config.policy_dir)
    policy_engine = OPAPolicyEngine(base_url=opa_url, timeout=2.0)
    gateway = _FakeGateway()
    masker = Masker(config.masking_rules)
    session_manager = SessionManager()
    risk_manager = RiskStateManager(JsonlRiskStateStore(workdir / "data" / "risk_state.jsonl"))
    checkpoint = Checkpoint(
        profiles=config.profiles,
        policy_engine=policy_engine,
        policy_store=policy_store,
        gateway=gateway,
        identity=identity,
        session_manager=session_manager,
        risk_manager=risk_manager,
        decision_store=InMemoryDecisionStore(),
        budget_ledger=InMemoryBudgetLedger(),
        permission_analyzer=ConfigPermissionInteractionAnalyzer(config.permission_rules),
        tool_costs={
            name: __import__("loop_controller.models", fromlist=["BudgetCost"]).BudgetCost(token_count=entry.cost_per_call)
            for name, entry in config.tool_mapping.items()
        },
        masker=masker,
    )
    audit_store = JsonlAuditStore(workdir / "data" / "audit.jsonl")
    conversation_store = JsonlConversationStore(workdir / "data" / "conversations.jsonl")
    approval_store_path = workdir / "data" / "approvals.jsonl"
    if plan_path is None:
        plan_path = workdir / "config" / "scripted_plan.yaml"
    planner = ScriptedPlanner.from_yaml(plan_path)
    return Runtime(
        planner=planner,
        classifier=RuleBasedClassifier(),
        checkpoint=checkpoint,
        gateway=gateway,
        approval_manager=AsyncApprovalManager(JsonlApprovalStore(approval_store_path)),
        audit_store=audit_store,
        masker=masker,
        profiles=config.profiles,
        session_manager=session_manager,
        risk_manager=risk_manager,
        conversation_store=conversation_store,
    )


async def _run(workdir: Path, opa_url: str, plan_path: Path | None = None) -> tuple[TaskRunResult, Runtime]:
    runtime = _runtime_from_config(workdir, opa_url, plan_path)
    agent = runtime.checkpoint._identity.get_agent("researcher_001")
    task = runtime.create_task(
        user_id="alice",
        agent_id=agent.agent_id,
        description="调研 AI 合规并发送摘要邮件",
    )
    result = await run_task(task, agent, runtime)
    return result, runtime


async def _run_and_resume(
    workdir: Path,
    opa_url: str,
    plan_path: Path | None = None,
    *,
    verdict: str = "approve",
) -> Runtime:
    """运行任务直到需要审批，然后写入审批结果并恢复，返回 Runtime 用于后续断言。"""
    result, runtime = await _run(workdir, opa_url, plan_path)
    assert result.status == "needs_approval"
    runtime.approval_manager._store.record_response(
        ApprovalRecord(
            request_id=result.request_id or "",
            decision_id=result.decision_id or "",
            verdict=verdict,
            approver_id="zhang_manager",
            comment=f"{verdict} by e2e test",
            decided_at=datetime.now(UTC),
        )
    )
    agent = runtime.checkpoint._identity.get_agent("researcher_001")
    # 复用 result 中的 task_id：从 runtime 历史中没有持久化 task 对象，因此重新构造一个等效 Task
    task = Task(
        task_id=result.task_id,
        session_id=result.session_id,
        user_id="alice",
        agent_id=agent.agent_id,
        description="调研 AI 合规并发送摘要邮件",
    )
    resume = await resume_task(task, agent, runtime, pending=result)
    assert resume.status == "completed"
    return runtime


def _events(workdir: Path) -> list[AuditEvent]:
    path = workdir / "data" / "audit.jsonl"
    events: list[AuditEvent] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                events.append(AuditEvent(**json.loads(line)))
    return events


async def test_e2e_approval_expired_after_timeout(opa_server, workdir) -> None:
    """A6：审批通过后的 Decision 超期后不能执行，并写入 approval_expired 审计事件。"""
    result, runtime = await _run(workdir, opa_server)
    assert result.status == "needs_approval"

    runtime.approval_manager._store.record_response(
        ApprovalRecord(
            request_id=result.request_id or "",
            decision_id=result.decision_id or "",
            verdict="approve",
            approver_id="zhang_manager",
            comment="approved by e2e test",
            decided_at=datetime.now(UTC),
        )
    )

    # 让审批通过生成的 allow Decision 立即过期
    decision_id = result.decision_id or ""
    old = runtime.checkpoint._decision_store._decisions[decision_id]
    runtime.checkpoint._decision_store._decisions[decision_id] = old.model_copy(
        update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)}
    )

    agent = runtime.checkpoint._identity.get_agent("researcher_001")
    task = Task(
        task_id=result.task_id,
        session_id=result.session_id,
        user_id="alice",
        agent_id=agent.agent_id,
        description="调研 AI 合规并发送摘要邮件",
    )

    with pytest.raises(Exception):  # CheckpointError：decision 已过期
        await resume_task(task, agent, runtime, pending=result)

    actions = [e.action for e in _events(workdir)]
    assert "approval_expired" in actions


async def test_e2e_approve_path_event_sequence(opa_server, workdir) -> None:
    """A5 + A14：approve 路径下完整事件序列；web_search 映射到本地 mock（断网可运行）。"""
    runtime = await _run_and_resume(workdir, opa_server)

    actions = [e.action for e in _events(workdir)]
    assert actions == [
        "task_start",
        "propose", "evaluate", "execute",   # web_search
        "propose", "evaluate", "execute",   # read_file
        "propose", "evaluate", "execute",   # write_file
        "propose", "evaluate",               # send_email 被 require_approval 拦截
        "task_start",                         # resume_task 再写一次 task_start
        "approve", "approval_consumed", "execute",  # send_email 审批通过、消费、执行
        "task_end",
    ]
    assert runtime.audit_store.verify_chain()


async def test_e2e_deny_path(opa_server, workdir) -> None:
    """A5：审批 deny 时 send_email 被拦截，任务仍能结束。"""
    runtime = await _run_and_resume(workdir, opa_server, verdict="deny")

    actions = [e.action for e in _events(workdir)]
    assert "deny" in actions
    assert actions[-1] == "task_end"
    assert runtime.audit_store.verify_chain()


async def test_e2e_tamper_detection(opa_server, workdir) -> None:
    """A12：篡改 audit.jsonl 任意一行后 verify_chain 返回 False。"""
    await _run_and_resume(workdir, opa_server)

    audit_path = workdir / "data" / "audit.jsonl"
    lines = audit_path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) >= 3
    record = json.loads(lines[1])
    record["reason"] = "tampered"
    lines[1] = json.dumps(record, sort_keys=True)
    audit_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert not JsonlAuditStore(audit_path).verify_chain()


async def test_e2e_masking(opa_server, workdir) -> None:
    """A13：审计日志中邮箱/密码不可检索；审批请求中收件人可见。"""
    plan_path = workdir / "plan.yaml"
    plan_path.write_text(
        "steps:\n"
        "  - tool_name: send_email\n"
        "    arguments:\n"
        "      to: zhang@company.com\n"
        "      subject: report\n"
        "      password: secret123\n"
        "    reason: send report\n",
        encoding="utf-8",
    )
    await _run_and_resume(workdir, opa_server, plan_path)

    audit_path = workdir / "data" / "audit.jsonl"
    raw = audit_path.read_text(encoding="utf-8")
    # 审计日志中不应出现原始邮箱和密码
    assert "zhang@company.com" not in raw
    assert "secret123" not in raw
    # 但应出现掩码值，证明事件已记录
    assert "***@***" in raw
    assert "***" in raw

    # 审批请求视图：收件人与正文必须可见，凭证类字段被掩码
    runtime = _runtime_from_config(workdir, opa_server, plan_path)
    from datetime import datetime, timedelta

    from loop_controller.models import ActionProposal, Decision

    agent = runtime.checkpoint._identity.get_agent("researcher_001")
    proposal = ActionProposal(
        task_id="t",
        call_id="c",
        agent_id=agent.agent_id,
        tool_name="send_email",
        arguments={"to": "zhang@company.com", "subject": "report", "password": "secret123"},
        task_context="x",
    )
    decision = Decision(
        decision_id="d",
        call_id="c",
        task_id="t",
        verdict="require_approval",
        reason="approval",
        escalation_target=agent.owner_id,
        policy_version="v",
        profile_version="v",
        expires_at=datetime.now(UTC) + timedelta(minutes=15),
    )
    task = _build_task(agent)
    request = runtime.checkpoint.build_approval_request(decision, proposal, task)
    assert request.arguments_masked["to"] == "zhang@company.com"
    assert request.arguments_masked["subject"] == "report"
    assert request.arguments_masked["password"] == "***"
