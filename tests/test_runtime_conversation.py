"""Runtime 会话与对话上下文测试（v0.14.0）。

验证 LoopController 在多次工具调用中复用 Session，并正确维护对话历史。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from loop_controller.approval_manager import AsyncApprovalManager
from loop_controller.budget import InMemoryBudgetLedger
from loop_controller.checkpoint import Checkpoint, InMemoryDecisionStore
from loop_controller.classifier import RuleBasedClassifier
from loop_controller.controller import LoopController
from loop_controller.infra.approval_store import JsonlApprovalStore
from loop_controller.infra.audit_store import JsonlAuditStore
from loop_controller.infra.config_loader import MaskingRules
from loop_controller.infra.conversation_store import JsonlConversationStore
from loop_controller.infra.identity import ConfigIdentityProvider
from loop_controller.infra.policy_store import PolicyStore
from loop_controller.masker import Masker
from loop_controller.mcp_gateway import MCPGateway
from loop_controller.models import (
    Agent,
    BudgetCost,
    CapabilityProfile,
    ToolPermission,
    ToolResult,
)
from loop_controller.risk_state import RiskStateManager
from loop_controller.runtime import Runtime
from loop_controller.session import Session, SessionManager


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

    async def call_tool(
        self, tool_name: str, arguments: dict, call_id: str, task_id: str, **kwargs: Any
    ) -> ToolResult:
        return ToolResult(
            call_id=call_id,
            task_id=task_id,
            tool_name=tool_name,
            status="success",
            content="ok",
        )


def _build_controller(audit_path: Path) -> LoopController:
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
    approval_store_path = audit_path.parent / "approvals.jsonl"
    r0 = AsyncApprovalManager(JsonlApprovalStore(approval_store_path))
    runtime = Runtime(
        classifier=RuleBasedClassifier(),
        checkpoint=checkpoint,
        gateway=gateway,
        approval_manager=r0,
        audit_store=audit_store,
        masker=checkpoint._masker,
        profiles={profile.profile_id: profile},
        session_manager=session_manager,
        risk_manager=risk_manager,
        conversation_store=conversation_store,
    )
    return LoopController(runtime)


@pytest.mark.asyncio
async def test_multi_turn_conversation_reuses_session(tmp_path: Path) -> None:
    """同一 session_id 的多次工具调用共享 Session 与对话历史。"""
    audit_path = tmp_path / "audit.jsonl"
    controller = _build_controller(audit_path)
    await controller.start()
    try:
        session_id = "session-001"
        now = datetime.now(UTC)
        controller._runtime.session_manager._backend.put(
            Session(
                session_id=session_id,
                user_id="alice",
                agent_id="researcher_001",
                created_at=now,
                last_task_at=now,
                active=True,
            )
        )

        # 第一轮：Agent 调用 web_search，复用已创建的 Session
        result1 = await controller.evaluate_and_execute(
            agent_id="researcher_001",
            user_id="alice",
            tool_name="web_search",
            arguments={"query": "GDPR"},
            session_id=session_id,
            task_context="写合规报告",
        )
        assert result1.status == "allow"

        session = controller._runtime.session_manager.get_session(session_id)
        assert session is not None
        assert session.user_id == "alice"
        assert session.agent_id == "researcher_001"

        # 模拟 Agent 向用户报告初步结果，以及用户补充输入
        controller._runtime.add_agent_message(session_id, result1.call_id or "", "已找到 GDPR 相关摘要")
        controller._runtime.add_user_message(session_id, result1.call_id or "", "请再查一下 AI 合规")

        # 第二轮：同一 Session 再次调用，对话历史应保留
        result2 = await controller.evaluate_and_execute(
            agent_id="researcher_001",
            user_id="alice",
            tool_name="web_search",
            arguments={"query": "AI compliance"},
            session_id=session_id,
            task_context="继续写合规报告",
        )
        assert result2.status == "allow"

        ctx = controller._runtime.get_conversation_context(session_id)
        assert len(ctx.messages) == 4
        assert ctx.messages[0].role == "user"
        assert "写合规报告" in ctx.messages[0].content
        assert ctx.messages[1].role == "agent"
        assert ctx.messages[2].role == "user"
        assert ctx.messages[2].content == "请再查一下 AI 合规"
        assert ctx.messages[3].role == "user"
        assert "继续写合规报告" in ctx.messages[3].content
    finally:
        await controller.aclose()
