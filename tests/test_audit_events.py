"""审计埋点核对（T3.3）：run_task 产生完整事件序列与字段。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from loop_controller.budget import InMemoryBudgetLedger
from loop_controller.checkpoint import Checkpoint, InMemoryDecisionStore
from loop_controller.classifier import RuleBasedClassifier
from loop_controller.infra.audit_store import JsonlAuditStore
from loop_controller.infra.config_loader import MaskingRules, ValuePattern
from loop_controller.infra.identity import ConfigIdentityProvider
from loop_controller.infra.policy_store import PolicyStore
from loop_controller.masker import Masker
from loop_controller.models import (
    Agent,
    AuditEvent,
    BudgetCost,
    CapabilityProfile,
    PlannedAction,
    Task,
    ToolPermission,
    ToolResult,
)
from loop_controller.mcp_gateway import MCPGateway
from loop_controller.planner import Planner, ScriptedPlanner
from loop_controller.infra.config_loader import ApprovalConfig, ApprovalRule
from loop_controller.r0_delegate import ConfigR0Delegate
from loop_controller.runtime import Runtime, run_task


class _FakePolicyEngine:
    async def evaluate(self, package: str, input_doc: dict) -> dict:
        if input_doc.get("tool_name") == "send_email":
            return {"verdict": "require_approval", "reason": "send_email needs approval"}
        return {"verdict": "allow", "reason": "allowed"}


class _StubPolicyStore(PolicyStore):
    def policy_path(self, name: str) -> str:
        return ""

    def current_version(self) -> str:
        return "test-policy-v1"

    def list_policies(self) -> list[str]:
        return []


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
            content="ok",
        )


def _build_runtime(audit_path: Path, steps: list) -> Runtime:
    agent = Agent(
        agent_id="researcher_001",
        name="RA",
        profile_id="p1",
        owner_id="zhang_manager",
    )
    identity = ConfigIdentityProvider(
        agents={agent.agent_id: agent},
        users={"alice": "Alice", "zhang_manager": "张经理"},
    )
    profile = CapabilityProfile(
        profile_id="p1",
        version="test-profile-v1",
        tools={
            "web_search": ToolPermission(tool_name="web_search", allowed=True),
            "send_email": ToolPermission(tool_name="send_email", allowed=True, require_approval=True),
        },
    )
    gateway = _FakeGateway()
    checkpoint = Checkpoint(
        profiles={profile.profile_id: profile},
        policy_engine=_FakePolicyEngine(),
        policy_store=_StubPolicyStore(),
        gateway=gateway,
        identity=identity,
        decision_store=InMemoryDecisionStore(),
        budget_ledger=InMemoryBudgetLedger(),
        tool_costs={"web_search": BudgetCost(token_count=1)},
        masker=Masker(
            MaskingRules(
                field_name_blacklist=["password"],
                value_patterns=[ValuePattern(name="email", pattern=r"[\w.+-]+@[\w-]+\.[\w.]+", replacement="***@***")],
                masking_applies_to={
                    "audit_log": ["field_name_blacklist", "value_patterns"],
                    "approval_request": ["field_name_blacklist"],
                },
            )
        ),
    )
    audit_store = JsonlAuditStore(audit_path)
    planner: Planner = ScriptedPlanner(steps)
    r0 = ConfigR0Delegate(
        ApprovalConfig(
            default="zhang_manager",
            rules=[ApprovalRule(tool_name="send_email", approver="zhang_manager", behavior="approve")],
        )
    )
    return Runtime(
        planner=planner,
        classifier=RuleBasedClassifier(),
        checkpoint=checkpoint,
        gateway=gateway,
        r0_delegate=r0,
        audit_store=audit_store,
        masker=checkpoint._masker,
        profiles={profile.profile_id: profile},
    )


def test_audit_event_sequence_and_fields(tmp_path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    runtime = _build_runtime(
        audit_path,
        [
            PlannedAction(
                tool_name="web_search",
                arguments={"query": "OpenAI compliance", "password": "secret"},
                reason="search",
            ),
            PlannedAction(
                tool_name="send_email",
                arguments={"to": "zhang@company.com", "body": "done"},
                reason="notify",
            ),
        ],
    )
    agent = runtime.checkpoint._identity.get_agent("researcher_001")
    task = Task(
        task_id="trace-001",
        session_id="trace-001",
        user_id="alice",
        agent_id=agent.agent_id,
        description="test audit",
    )

    asyncio.run(run_task(task, agent, runtime))

    events = runtime.audit_store.query_by_trace(task.task_id)
    actions = [e.action for e in events]
    assert actions == [
        "task_start",
        "propose", "evaluate", "execute",
        "propose", "evaluate", "approve", "execute",
        "task_end",
    ]

    # task_start / task_end 无 call_id
    assert events[0].call_id is None
    assert events[-1].call_id is None

    # propose 由 agent 产生
    propose = events[1]
    assert propose.actor_type == "agent"
    assert propose.args_hash is not None
    assert propose.args_mask is not None
    assert propose.hash_algo == "sha256"
    # audit_log 档会掩码邮箱与 password
    assert propose.args_mask["password"] == "***"
    assert "@" not in str(propose.args_mask.get("query", ""))

    # evaluate 携带 policy/profile 版本
    evaluate = events[2]
    assert evaluate.decision == "allow"
    assert evaluate.policy_version == "test-policy-v1"
    assert evaluate.profile_version == "test-profile-v1"

    # send_email 审批路径：approve 事件记录审批结果
    approve = events[6]
    assert approve.action == "approve"
    assert approve.args_mask is not None
    # 审计事件中的 args_mask 使用 audit_log 档，邮箱也被掩码
    assert approve.args_mask["to"] == "***@***"

    # 审计链完整
    assert runtime.audit_store.verify_chain()
