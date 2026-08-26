"""审计埋点核对（v0.14.0）：LoopController 产生完整事件序列与字段。"""

from __future__ import annotations

import json
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
from loop_controller.infra.config_loader import (
    MaskingRules,
    ValuePattern,
)
from loop_controller.infra.conversation_store import JsonlConversationStore
from loop_controller.infra.identity import ConfigIdentityProvider
from loop_controller.infra.policy_store import PolicyStore
from loop_controller.masker import Masker
from loop_controller.mcp_gateway import MCPGateway
from loop_controller.models import (
    Agent,
    ApprovalRecord,
    AuditEvent,
    BudgetCost,
    CapabilityProfile,
    ToolPermission,
    ToolResult,
)
from loop_controller.risk_state import RiskStateManager
from loop_controller.runtime import Runtime
from loop_controller.session import SessionManager


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

    async def call_tool(self, tool_name: str, arguments: dict, call_id: str, task_id: str, **kwargs: Any) -> ToolResult:
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
            "send_email": ToolPermission(tool_name="send_email", allowed=True, require_approval=True),
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
async def test_audit_event_sequence_and_fields(tmp_path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    controller = _build_controller(audit_path)
    await controller.start()
    try:
        result1 = await controller.evaluate_and_execute(
            agent_id="researcher_001",
            user_id="alice",
            tool_name="web_search",
            arguments={"query": "OpenAI compliance", "password": "secret"},
            task_context="test audit",
        )
        assert result1.status == "allow"

        result2 = await controller.evaluate_and_execute(
            agent_id="researcher_001",
            user_id="alice",
            tool_name="send_email",
            arguments={"to": "zhang@company.com", "body": "done"},
            task_context="test audit",
        )
        assert result2.status == "require_approval"
        assert result2.request_id is not None

        # 模拟 CLI 审批通过
        store = controller._runtime.approval_manager._store
        request = store.get_request(result2.decision.decision_id)
        store.record_response(
            ApprovalRecord(
                request_id=request.request_id,
                decision_id=result2.decision.decision_id,
                verdict="approve",
                approver_id="zhang_manager",
                comment="approved by test",
                decided_at=datetime.now(UTC),
            )
        )

        resume_result = await controller.resume_after_approval(result2.request_id)
        assert resume_result.status == "allow"

        events = [
            AuditEvent(**json.loads(line))
            for line in controller._runtime.audit_store._path.read_text(encoding="utf-8").strip().split("\n")
            if line.strip()
        ]
        actions = [e.action for e in events]
        assert actions == [
            "propose", "evaluate", "execute",  # web_search
            "propose", "evaluate",             # send_email 被 require_approval 拦截
            "approve", "approval_consumed", "execute",  # send_email 审批后执行
        ]

        # approve 事件由审批人触发（actor_type 使用 r0_delegate）
        approve_event = events[-3]
        assert approve_event.action == "approve"
        assert approve_event.actor_type == "r0_delegate"
        assert approve_event.actor_id == "zhang_manager"
        assert approve_event.decision == "allow"

        # approval_consumed 事件由 checkpoint 触发
        consumed_event = events[-2]
        assert consumed_event.action == "approval_consumed"
        assert consumed_event.actor_type == "checkpoint"

        # propose 由 agent 产生
        propose = events[0]
        assert propose.actor_type == "agent"
        assert propose.args_hash is not None
        assert propose.args_mask is not None
        # audit_log 档会掩码邮箱与 password
        assert propose.args_mask["password"] == "***"
        assert "@" not in str(propose.args_mask.get("query", ""))

        # evaluate 携带 policy/profile 版本
        evaluate = events[1]
        assert evaluate.decision == "allow"
        assert evaluate.policy_version == "test-policy-v1"
        assert evaluate.profile_version == "test-profile-v1"

        # 审计链完整
        assert controller._runtime.audit_store.verify_chain()
    finally:
        await controller.aclose()
