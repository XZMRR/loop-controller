"""Runtime 多轮对话 e2e 测试（v0.3.0 Iteration 4）。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from loop_controller.budget import InMemoryBudgetLedger
from loop_controller.checkpoint import Checkpoint, InMemoryDecisionStore
from loop_controller.classifier import RuleBasedClassifier
from loop_controller.infra.audit_store import JsonlAuditStore
from loop_controller.infra.config_loader import MaskingRules, ValuePattern
from loop_controller.infra.conversation_store import JsonlConversationStore
from loop_controller.infra.identity import ConfigIdentityProvider
from loop_controller.infra.policy_store import PolicyStore
from loop_controller.masker import Masker
from loop_controller.models import (
    Agent,
    AuditEvent,
    BudgetCost,
    CapabilityProfile,
    ConversationContext,
    PlannedAction,
    Task,
    ToolPermission,
    ToolResult,
    UserQuestion,
)
from loop_controller.mcp_gateway import MCPGateway
from loop_controller.planner import Planner
from loop_controller.infra.config_loader import ApprovalConfig, ApprovalRule
from loop_controller.r0_delegate import ConfigR0Delegate
from loop_controller.risk_state import RiskStateManager
from loop_controller.runtime import Runtime, resume_task, run_task
from loop_controller.session import SessionManager


class _FakePolicyEngine:
    async def evaluate(self, package: str, input_doc: dict) -> dict:
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


class _AskThenSearchPlanner:
    """先 ask_user，第二次调用时执行 web_search。"""

    def __init__(self) -> None:
        self._calls = 0

    async def next_action(
        self,
        task: Task,
        agent: Agent,
        observations: list[ToolResult],
        conversation_context: ConversationContext,
    ) -> PlannedAction | UserQuestion | None:
        self._calls += 1
        if self._calls == 1:
            return UserQuestion(question="需要什么主题的合规报告？")
        if self._calls == 2:
            # 验证 conversation 已包含用户回复
            user_msgs = [m for m in conversation_context.messages if m.role == "user"]
            assert len(user_msgs) == 1, "resume 后应能看到用户补充消息"
            assert user_msgs[0].content == "GDPR"
            return PlannedAction(tool_name="web_search", arguments={"query": "GDPR"}, reason="按用户补充搜索")
        return None


def _build_runtime(audit_path: Path, planner: Planner) -> Runtime:
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
        },
    )
    gateway = _FakeGateway()
    session_manager = SessionManager()
    risk_manager = RiskStateManager()
    checkpoint = Checkpoint(
        profiles={profile.profile_id: profile},
        policy_engine=_FakePolicyEngine(),
        policy_store=_StubPolicyStore(),
        gateway=gateway,
        identity=identity,
        session_manager=session_manager,
        risk_manager=risk_manager,
        decision_store=InMemoryDecisionStore(),
        budget_ledger=InMemoryBudgetLedger(),
        tool_costs={"web_search": BudgetCost(token_count=1)},
        masker=Masker(
            MaskingRules(
                field_name_blacklist=[],
                value_patterns=[],
                masking_applies_to={"audit_log": [], "approval_request": []},
            )
        ),
    )
    audit_store = JsonlAuditStore(audit_path)
    conversation_store = JsonlConversationStore(audit_path.parent / "conversations.jsonl")
    r0 = ConfigR0Delegate(
        ApprovalConfig(
            default="zhang_manager",
            rules=[ApprovalRule(tool_name="web_search", approver="zhang_manager", behavior="approve")],
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
        session_manager=session_manager,
        risk_manager=risk_manager,
        conversation_store=conversation_store,
    )


def test_multi_turn_conversation(tmp_path: Path) -> None:
    """Planner ask_user → Runtime 暂停 → 用户回复 → resume → 任务完成。"""
    audit_path = tmp_path / "audit.jsonl"
    runtime = _build_runtime(audit_path, _AskThenSearchPlanner())
    agent = runtime.checkpoint._identity.get_agent("researcher_001")
    task = Task(
        task_id="trace-001",
        session_id=runtime.session_manager.get_or_create_session("alice", agent.agent_id).session_id,
        user_id="alice",
        agent_id=agent.agent_id,
        description="写合规报告",
    )

    # 第一轮：Planner 请求用户补充
    result = asyncio.run(run_task(task, agent, runtime))
    assert result.status == "needs_user_input"
    assert result.question == "需要什么主题的合规报告？"

    # conversation_store 应写入一条 agent 消息
    ctx = runtime.get_conversation_context(task.session_id)
    assert len(ctx.messages) == 1
    assert ctx.messages[0].role == "agent"
    assert "请求用户补充" in ctx.messages[0].content

    # 外部调用方写入用户回复
    runtime.add_user_message(task.session_id, task.task_id, "GDPR")

    # 第二轮：resume 后继续并完成任务
    result2 = asyncio.run(resume_task(task, agent, runtime))
    assert result2.status == "completed"

    # 审计链完整
    events = runtime.audit_store.query_by_trace(task.task_id)
    actions = [e.action for e in events]
    assert actions == ["task_start", "task_start", "propose", "evaluate", "execute", "task_end"]
