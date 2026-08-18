"""checkpoint 判定流水线单元测试（开发指南 T1.6 / T2.2 / T2.3 / T2.5）.

本文件测 Python 侧流水线组装（步骤顺序、短路、forward 校验、审批、组合规则、
调用次数、预算）；Rego 策略逻辑由 test_policy_engine.py 覆盖，本文件用
FakePolicyEngine 注入判定。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from loop_controller.budget import InMemoryBudgetLedger
from loop_controller.checkpoint import Checkpoint, CheckpointError
from loop_controller.infra.config_loader import (
    MaskingRules,
    PermissionCondition,
    PermissionRule,
    ValuePattern,
)
from loop_controller.infra.identity import ConfigIdentityProvider
from loop_controller.masker import Masker
from loop_controller.models import (
    ActionProposal,
    Agent,
    ApprovalRecord,
    CapabilityProfile,
    Decision,
    Task,
    ToolPermission,
    ToolResult,
)
from loop_controller.permission_interaction import ConfigPermissionInteractionAnalyzer
from loop_controller.risk_state import RiskStateManager
from loop_controller.session import SessionManager

PACKAGE = "loop_controller.tool_permission"


class StubPolicyStore:
    def __init__(self, version: str = "0123456789ab") -> None:
        self._version = version

    def policy_path(self, name: str) -> str:
        return f"policies/{name}.rego"

    def current_version(self) -> str:
        return self._version

    def list_policies(self) -> list[str]:
        return ["default"]


class FakePolicyEngine:
    """返回固定判定；可按 tool_name 覆盖，并记录 input_doc。"""

    def __init__(
        self,
        decision: dict | None = None,
        by_tool: dict[str, dict] | None = None,
    ) -> None:
        self._default = decision or {
            "verdict": "allow",
            "reason": "web search allowed",
            "policy_hits": ["web_search_allow"],
        }
        self._by_tool = by_tool or {}
        self.calls: list[dict] = []

    async def evaluate(self, package: str, input_doc: dict) -> dict:
        assert package == PACKAGE
        self.calls.append(input_doc)
        return dict(self._by_tool.get(input_doc["tool_name"], self._default))


class FakeGateway:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def call_tool(
        self, tool_name: str, arguments: dict, call_id: str, task_id: str
    ) -> ToolResult:
        self.calls.append((tool_name, arguments, call_id, task_id))
        return ToolResult(
            call_id=call_id,
            task_id=task_id,
            tool_name=tool_name,
            status="success",
            content="ok",
        )


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def agent() -> Agent:
    return Agent(
        agent_id="researcher_001",
        name="RA",
        profile_id="p1",
        owner_id="zhang_manager",
    )


@pytest.fixture
def profile() -> CapabilityProfile:
    return CapabilityProfile(
        profile_id="p1",
        version="test-profile-v1",
        tools={
            "web_search": ToolPermission(
                tool_name="web_search", allowed=True, max_calls_per_task=10
            ),
            "read_file": ToolPermission(
                tool_name="read_file",
                allowed=True,
                allowed_args={"path": ["/data/kb/**"]},
                max_calls_per_task=20,
            ),
            "write_file": ToolPermission(
                tool_name="write_file",
                allowed=True,
                allowed_args={"path": ["/data/output/**"]},
                max_calls_per_task=5,
            ),
            "send_email": ToolPermission(
                tool_name="send_email",
                allowed=True,
                require_approval=True,
                allowed_args={"to": ["*@company.com"]},
                max_calls_per_task=1,
            ),
        },
        max_budget_token=100_000,
    )


@pytest.fixture
def task(agent: Agent) -> Task:
    return Task(
        task_id="t1",
        session_id="t1",
        user_id="alice",
        agent_id=agent.agent_id,
        description="调研 AI 合规",
    )


@pytest.fixture
def identity(agent: Agent) -> ConfigIdentityProvider:
    return ConfigIdentityProvider(
        agents={agent.agent_id: agent}, users={"alice": "Alice"}
    )


def make_checkpoint(
    profile: CapabilityProfile,
    identity: ConfigIdentityProvider,
    *,
    engine_decision: dict | None = None,
    engine_by_tool: dict[str, dict] | None = None,
    gateway: FakeGateway | None = None,
    now: datetime | None = None,
    budget_ledger=None,
    permission_analyzer=None,
    tool_costs: dict | None = None,
    masker=None,
    session_manager: SessionManager | None = None,
    risk_manager: RiskStateManager | None = None,
) -> tuple[Checkpoint, FakePolicyEngine, FakeGateway]:
    engine = FakePolicyEngine(
        decision=engine_decision,
        by_tool=engine_by_tool,
    )
    gw = gateway or FakeGateway()
    from loop_controller.budget import InMemoryBudgetLedger

    cp = Checkpoint(
        profiles={profile.profile_id: profile},
        policy_engine=engine,
        policy_store=StubPolicyStore(),
        gateway=gw,
        identity=identity,
        session_manager=session_manager,
        risk_manager=risk_manager or RiskStateManager(),
        budget_ledger=budget_ledger or InMemoryBudgetLedger(),
        permission_analyzer=permission_analyzer,
        tool_costs=tool_costs,
        masker=masker,
        now=(lambda: now) if now is not None else None,
    )
    return cp, engine, gw


def make_proposal(
    task: Task,
    agent: Agent,
    *,
    tool_name: str = "web_search",
    call_id: str | None = None,
    **overrides,
) -> ActionProposal:
    kwargs = {
        "task_id": task.task_id,
        "call_id": call_id or uuid.uuid4().hex,
        "agent_id": agent.agent_id,
        "tool_name": tool_name,
        "arguments": {"query": "test"},
        "task_context": task.description[:200],
    }
    kwargs.update(overrides)
    return ActionProposal(**kwargs)


# ---------------------------------------------------------------------------
# evaluate：allow 全链路
# ---------------------------------------------------------------------------


async def test_evaluate_allow_full_path(
    task: Task, agent: Agent, profile: CapabilityProfile, identity: ConfigIdentityProvider
) -> None:
    now = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    cp, engine, gw = make_checkpoint(profile, identity, now=now)
    proposal = make_proposal(task, agent)

    decision = await cp.evaluate(task, agent, proposal)

    assert decision.verdict == "allow"
    assert decision.call_id == proposal.call_id
    assert decision.task_id == task.task_id
    assert decision.reason == "web search allowed"
    assert decision.policy_hits == ["web_search_allow"]
    assert decision.policy_version == "0123456789ab"
    assert decision.profile_version == "test-profile-v1"
    # allow 分档：+5min、max_uses=1
    assert decision.expires_at == now + timedelta(minutes=5)
    assert decision.max_uses == 1
    # 步骤 1：call_id 已入账（v1.1 全局唯一性检测）
    assert cp._decision_store.is_call_id_seen(proposal.call_id)

    # forward 全链路：校验通过 → 网关转发（规范化工具名 + 原参数）
    result = await cp.forward(proposal, decision)
    assert result.status == "success"
    assert gw.calls == [(proposal.tool_name, proposal.arguments, proposal.call_id, task.task_id)]
    # 成功执行后记入 per-task 历史
    assert len(cp._history[task.task_id]) == 1


# ---------------------------------------------------------------------------
# evaluate：deny 分支
# ---------------------------------------------------------------------------


async def test_evaluate_deny_tool_not_in_profile(
    task: Task, agent: Agent, profile: CapabilityProfile, identity: ConfigIdentityProvider
) -> None:
    cp, engine, _ = make_checkpoint(profile, identity)
    proposal = make_proposal(task, agent, tool_name="delete_file")

    decision = await cp.evaluate(task, agent, proposal)

    assert decision.verdict == "deny"
    assert decision.reason == "tool not permitted"
    assert decision.max_uses == 0  # deny 分档：立即过期、不可执行
    # 默认拒绝前置在 Rego 之前：未发起 OPA 查询（步骤 2 短路）
    assert engine.calls == []


async def test_evaluate_deny_duplicate_call_id(
    task: Task, agent: Agent, profile: CapabilityProfile, identity: ConfigIdentityProvider
) -> None:
    cp, _, _ = make_checkpoint(profile, identity)
    proposal = make_proposal(task, agent)

    first = await cp.evaluate(task, agent, proposal)
    second = await cp.evaluate(task, agent, proposal)

    assert first.verdict == "allow"
    assert second.verdict == "deny"
    assert second.reason == "duplicate call_id"


async def test_evaluate_deny_call_id_global_across_tasks(
    task: Task, agent: Agent, profile: CapabilityProfile, identity: ConfigIdentityProvider
) -> None:
    """v1.1 全局唯一：同一 call_id 出现在另一 task 下同样被拒绝。"""
    cp, _, _ = make_checkpoint(profile, identity)
    same_call_id = uuid.uuid4().hex
    proposal = make_proposal(task, agent, call_id=same_call_id)
    other_task = Task(
        task_id="t2", session_id="t2", user_id="bob", agent_id="researcher_001", description="x"
    )
    other_proposal = make_proposal(other_task, agent, call_id=same_call_id)

    first = await cp.evaluate(task, agent, proposal)
    second = await cp.evaluate(other_task, agent, other_proposal)

    assert first.verdict == "allow"
    assert second.verdict == "deny"
    assert second.reason == "duplicate call_id"


async def test_evaluate_deny_identity_mismatch(
    task: Task, agent: Agent, profile: CapabilityProfile, identity: ConfigIdentityProvider
) -> None:
    cp, engine, _ = make_checkpoint(profile, identity)
    proposal = make_proposal(task, agent, agent_id="attacker_001")

    decision = await cp.evaluate(task, agent, proposal)

    assert decision.verdict == "deny"
    assert decision.reason == "identity mismatch"
    assert engine.calls == []


async def test_evaluate_deny_unknown_agent(
    task: Task, agent: Agent, profile: CapabilityProfile, identity: ConfigIdentityProvider
) -> None:
    # identity 中不存在 agent：构造一个不在身份表中的 agent
    stranger = Agent(agent_id="ghost", name="Ghost", profile_id="p1", owner_id="x")
    cp, engine, _ = make_checkpoint(profile, identity)
    proposal = make_proposal(task, stranger)

    decision = await cp.evaluate(task, stranger, proposal)

    assert decision.verdict == "deny"
    assert decision.reason == "unknown agent"
    assert engine.calls == []


# ---------------------------------------------------------------------------
# evaluate：require_approval + 审批组装/终结
# ---------------------------------------------------------------------------


async def test_evaluate_require_approval_returns_decision(
    task: Task, agent: Agent, profile: CapabilityProfile, identity: ConfigIdentityProvider
) -> None:
    cp, _, _ = make_checkpoint(
        profile,
        identity,
        engine_decision={
            "verdict": "require_approval",
            "reason": "send_email requires human approval",
            "policy_hits": ["send_email_approval"],
            "escalation_target": agent.owner_id,
        },
    )
    proposal = make_proposal(
        task, agent, tool_name="send_email", arguments={"to": "zhang@company.com"}
    )

    decision = await cp.evaluate(task, agent, proposal)

    assert decision.verdict == "require_approval"
    assert decision.reason == "send_email requires human approval"
    assert decision.policy_hits == ["send_email_approval"]
    assert decision.escalation_target == agent.owner_id
    assert decision.max_uses == 1


def test_build_approval_request_conflict(
    task: Task, agent: Agent, profile: CapabilityProfile, identity: ConfigIdentityProvider
) -> None:
    cp, _, _ = make_checkpoint(profile, identity)
    proposal = make_proposal(task, agent, tool_name="send_email")
    decision = Decision(
        decision_id="d1",
        call_id=proposal.call_id,
        task_id=task.task_id,
        verdict="require_approval",
        reason="approval",
        escalation_target=task.user_id,  # 与 requester 相同 → 冲突
        policy_version="v",
        profile_version="v",
        expires_at=datetime.now(UTC) + timedelta(minutes=15),
    )

    with pytest.raises(CheckpointError, match="审批人冲突"):
        cp.build_approval_request(decision, proposal, task)


def test_build_approval_request_uses_approval_request_mask_level(
    task: Task, agent: Agent, profile: CapabilityProfile, identity: ConfigIdentityProvider
) -> None:
    """审批请求视图只应用 field_name_blacklist，收件人与正文须对审批人可见（A13）。"""
    masker = Masker(
        MaskingRules(
            field_name_blacklist=["password"],
            value_patterns=[ValuePattern(name="email", pattern=r"[\w.+-]+@[\w-]+\.[\w.]+", replacement="***@***")],
            masking_applies_to={
                "audit_log": ["field_name_blacklist", "value_patterns"],
                "approval_request": ["field_name_blacklist"],
            },
        )
    )
    cp, _, _ = make_checkpoint(profile, identity, masker=masker)
    proposal = make_proposal(
        task,
        agent,
        tool_name="send_email",
        arguments={"to": "zhang@company.com", "body": "report", "password": "secret"},
    )
    decision = Decision(
        decision_id="d1",
        call_id=proposal.call_id,
        task_id=task.task_id,
        verdict="require_approval",
        reason="approval",
        escalation_target=agent.owner_id,
        policy_version="v",
        profile_version="v",
        expires_at=datetime.now(UTC) + timedelta(minutes=15),
    )

    request = cp.build_approval_request(decision, proposal, task)
    assert request.arguments_masked["to"] == "zhang@company.com"
    assert request.arguments_masked["body"] == "report"
    assert request.arguments_masked["password"] == "***"


def test_finalize_after_approval(
    task: Task, agent: Agent, profile: CapabilityProfile, identity: ConfigIdentityProvider
) -> None:
    cp, _, _ = make_checkpoint(profile, identity)
    proposal = make_proposal(task, agent, tool_name="send_email")
    decision = Decision(
        decision_id="d1",
        call_id=proposal.call_id,
        task_id=task.task_id,
        verdict="require_approval",
        reason="approval",
        escalation_target=agent.owner_id,
        policy_hits=["send_email_approval"],
        policy_version="v",
        profile_version="v",
        expires_at=datetime.now(UTC) + timedelta(minutes=15),
    )

    approved = cp.finalize_after_approval(
        decision,
        ApprovalRecord(
            request_id="r1",
            decision_id=decision.decision_id,
            verdict="approve",
            approver_id=agent.owner_id,
            comment="ok",
        ),
    )
    assert approved.verdict == "allow"
    assert "approval:granted" in approved.policy_hits

    denied = cp.finalize_after_approval(
        decision,
        ApprovalRecord(
            request_id="r1",
            decision_id=decision.decision_id,
            verdict="deny",
            approver_id=agent.owner_id,
            comment="no",
        ),
    )
    assert denied.verdict == "deny"
    assert denied.max_uses == 0


# ---------------------------------------------------------------------------
# evaluate：调用次数 / 预算 / 组合规则
# ---------------------------------------------------------------------------


async def test_evaluate_call_limit_exceeded(
    task: Task, agent: Agent, profile: CapabilityProfile, identity: ConfigIdentityProvider
) -> None:
    """send_email max_calls_per_task=1，第二次 evaluate 直接 deny（不查询 OPA）。"""
    cp, engine, _ = make_checkpoint(
        profile,
        identity,
        budget_ledger=InMemoryBudgetLedger(default_max_budget_token=10),
        engine_by_tool={
            "send_email": {
                "verdict": "require_approval",
                "reason": "send_email requires human approval",
                "policy_hits": ["send_email_approval"],
            }
        },
    )
    proposal1 = make_proposal(
        task, agent, tool_name="send_email", arguments={"to": "z@company.com"}
    )
    decision1 = await cp.evaluate(task, agent, proposal1)
    assert decision1.verdict == "require_approval"

    allowed = cp.finalize_after_approval(
        decision1,
        ApprovalRecord(
            request_id="r1",
            decision_id=decision1.decision_id,
            verdict="approve",
            approver_id=agent.owner_id,
            comment="ok",
        ),
    )
    await cp.forward(proposal1, allowed)

    proposal2 = make_proposal(
        task, agent, tool_name="send_email", arguments={"to": "z@company.com"}
    )
    decision2 = await cp.evaluate(task, agent, proposal2)

    assert decision2.verdict == "deny"
    assert decision2.reason == "call limit exceeded"
    assert len(engine.calls) == 1


async def test_evaluate_budget_exceeded(
    task: Task, agent: Agent, profile: CapabilityProfile, identity: ConfigIdentityProvider
) -> None:
    small_budget_profile = profile.model_copy(
        update={"max_budget_token": 0}
    )
    cp, engine, _ = make_checkpoint(small_budget_profile, identity)
    proposal = make_proposal(task, agent, tool_name="web_search")

    decision = await cp.evaluate(task, agent, proposal)

    assert decision.verdict == "deny"
    assert decision.reason == "budget exceeded"
    assert engine.calls == []


async def test_evaluate_budget_cost_per_call(
    task: Task, agent: Agent, profile: CapabilityProfile, identity: ConfigIdentityProvider
) -> None:
    """v1.1（评审#3）：按工具 cost_per_call 计费——大成本工具被拒、小成本工具放行。"""
    from loop_controller.models import BudgetCost

    tight_profile = profile.model_copy(update={"max_budget_token": 400})
    cp, engine, _ = make_checkpoint(
        tight_profile,
        identity,
        tool_costs={
            "web_search": BudgetCost(token_count=200),   # 低成本：400 额度内放行
            "send_email": BudgetCost(token_count=800),   # 800 > 400 → deny
        },
        engine_by_tool={"send_email": {"verdict": "allow", "reason": "ok"}},
    )
    cheap = await cp.evaluate(task, agent, make_proposal(task, agent, tool_name="web_search"))
    assert cheap.verdict == "allow"

    dear = await cp.evaluate(task, agent, make_proposal(task, agent, tool_name="send_email"))
    assert dear.verdict == "deny"
    assert dear.reason == "budget exceeded"
    # 低成本工具照常发起 OPA 查询；高成本工具在步骤 4 短路
    assert [c["tool_name"] for c in engine.calls] == ["web_search"]


async def test_permission_interaction_deny_short_circuit(
    task: Task, agent: Agent, profile: CapabilityProfile, identity: ConfigIdentityProvider
) -> None:
    rule = PermissionRule(
        id="kb_read_plus_external_email",
        description="读取知识库后向外部邮箱发信",
        when_all=[
            PermissionCondition(
                history_tool="read_file",
                history_arg_match={"path": "/data/kb/**"},
            ),
            PermissionCondition(
                current_tool="send_email",
                current_arg_not_match={"to": "*@company.com"},
            ),
        ],
        action="deny",
        reason="内部知识库内容禁止外发",
    )
    analyzer = ConfigPermissionInteractionAnalyzer([rule])
    cp, engine, _ = make_checkpoint(profile, identity, permission_analyzer=analyzer)

    read_proposal = make_proposal(
        task, agent, tool_name="read_file", arguments={"path": "/data/kb/doc.md"}
    )
    read_decision = await cp.evaluate(task, agent, read_proposal)
    assert read_decision.verdict == "allow"
    await cp.forward(read_proposal, read_decision)

    email_proposal = make_proposal(
        task, agent, tool_name="send_email", arguments={"to": "x@gmail.com"}
    )
    email_decision = await cp.evaluate(task, agent, email_proposal)

    assert email_decision.verdict == "deny"
    assert email_decision.reason == "内部知识库内容禁止外发"
    assert "kb_read_plus_external_email" in email_decision.policy_hits
    # OPA 只被查询一次（read_file），send_email 未进入 Rego
    assert len(engine.calls) == 1


async def test_permission_interaction_require_approval_then_rego_deny(
    task: Task, agent: Agent, profile: CapabilityProfile, identity: ConfigIdentityProvider
) -> None:
    """组合规则 require_approval + Rego deny → 最终 deny（deny 优先原则）。"""
    rule = PermissionRule(
        id="contact_plus_external_email",
        description="读取联系人后外发邮件",
        when_all=[
            PermissionCondition(
                history_tool="read_file",
                history_arg_match={"path": "**/*contact*"},
            ),
            PermissionCondition(
                current_tool="send_email",
                current_arg_not_match={"to": "*@company.com"},
            ),
        ],
        action="require_approval",
        reason="组合规则 require_approval",
    )
    analyzer = ConfigPermissionInteractionAnalyzer([rule])
    cp, engine, _ = make_checkpoint(
        profile,
        identity,
        engine_by_tool={
            "read_file": {
                "verdict": "allow",
                "reason": "read within allowed directories",
                "policy_hits": ["read_file_allow"],
            },
            "send_email": {
                "verdict": "deny",
                "reason": "recipient outside allowed patterns",
                "policy_hits": ["send_email_deny_external"],
            },
        },
        permission_analyzer=analyzer,
    )

    history_proposal = make_proposal(
        task, agent, tool_name="read_file", arguments={"path": "/data/kb/contacts.csv"}
    )
    history_decision = await cp.evaluate(task, agent, history_proposal)
    await cp.forward(history_proposal, history_decision)

    email_proposal = make_proposal(
        task, agent, tool_name="send_email", arguments={"to": "x@gmail.com"}
    )
    email_decision = await cp.evaluate(task, agent, email_proposal)

    assert email_decision.verdict == "deny"
    assert "send_email_deny_external" in email_decision.policy_hits
    assert engine.calls[-1]["tool_name"] == "send_email"


# ---------------------------------------------------------------------------
# forward：modify 复核失败
# ---------------------------------------------------------------------------


async def test_forward_modify_recheck_failed_on_value(
    task: Task, agent: Agent, profile: CapabilityProfile, identity: ConfigIdentityProvider
) -> None:
    cp, _, gw = make_checkpoint(profile, identity)
    proposal = make_proposal(
        task, agent, tool_name="read_file", arguments={"path": "/data/kb/doc.md"}
    )
    decision = Decision(
        decision_id=uuid.uuid4().hex,
        call_id=proposal.call_id,
        task_id=task.task_id,
        verdict="modify",
        reason="modified",
        modified_args={"path": "/etc/passwd"},  # 越出 /data/kb/** 白名单
        policy_hits=["modify_rule"],
        policy_version="v",
        profile_version="v",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )

    cp._decision_store.record_decision(decision)
    result = await cp.forward(proposal, decision)

    assert result.status == "blocked"
    assert result.error_code == "modify_recheck_failed"
    # 复核失败：不转发执行
    assert gw.calls == []


async def test_forward_modify_recheck_failed_on_structure(
    task: Task, agent: Agent, profile: CapabilityProfile, identity: ConfigIdentityProvider
) -> None:
    cp, _, gw = make_checkpoint(profile, identity)
    proposal = make_proposal(
        task, agent, tool_name="read_file", arguments={"path": "/data/kb/doc.md"}
    )
    decision = Decision(
        decision_id=uuid.uuid4().hex,
        call_id=proposal.call_id,
        task_id=task.task_id,
        verdict="modify",
        reason="modified",
        modified_args={"path": "/data/kb/doc.md", "extra": True},  # 键集合变化
        policy_version="v",
        profile_version="v",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )

    cp._decision_store.record_decision(decision)
    result = await cp.forward(proposal, decision)

    assert result.status == "blocked"
    assert result.error_code == "modify_recheck_failed"
    assert gw.calls == []


async def test_forward_modify_allowed_value_passes(
    task: Task, agent: Agent, profile: CapabilityProfile, identity: ConfigIdentityProvider
) -> None:
    cp, _, gw = make_checkpoint(profile, identity)
    proposal = make_proposal(
        task, agent, tool_name="read_file", arguments={"path": "/data/kb/doc.md"}
    )
    decision = Decision(
        decision_id=uuid.uuid4().hex,
        call_id=proposal.call_id,
        task_id=task.task_id,
        verdict="modify",
        reason="modified",
        modified_args={"path": "/data/kb/other.md"},  # 仍在白名单内 → 复核通过
        policy_version="v",
        profile_version="v",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )

    cp._decision_store.record_decision(decision)
    result = await cp.forward(proposal, decision)

    assert result.status == "success"
    # 转发的是修改后的参数
    assert gw.calls[0][1] == {"path": "/data/kb/other.md"}


# ---------------------------------------------------------------------------
# forward：前置校验失败（抛异常）
# ---------------------------------------------------------------------------


async def test_forward_call_id_mismatch(
    task: Task, agent: Agent, profile: CapabilityProfile, identity: ConfigIdentityProvider
) -> None:
    cp, _, gw = make_checkpoint(profile, identity)
    proposal = make_proposal(task, agent)
    decision = Decision(
        decision_id=uuid.uuid4().hex,
        call_id="another-call-id",  # 与 proposal.call_id 不一致
        task_id=task.task_id,
        verdict="allow",
        reason="ok",
        policy_version="v",
        profile_version="v",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )

    with pytest.raises(CheckpointError):
        await cp.forward(proposal, decision)
    assert gw.calls == []


async def test_forward_expired_decision(
    task: Task, agent: Agent, profile: CapabilityProfile, identity: ConfigIdentityProvider
) -> None:
    cp, _, gw = make_checkpoint(profile, identity)
    proposal = make_proposal(task, agent)
    decision = Decision(
        decision_id=uuid.uuid4().hex,
        call_id=proposal.call_id,
        task_id=task.task_id,
        verdict="allow",
        reason="ok",
        policy_version="v",
        profile_version="v",
        expires_at=datetime.now(UTC) - timedelta(seconds=1),  # 已过期
    )

    with pytest.raises(CheckpointError):
        await cp.forward(proposal, decision)
    assert gw.calls == []


async def test_forward_deny_verdict_not_executable(
    task: Task, agent: Agent, profile: CapabilityProfile, identity: ConfigIdentityProvider
) -> None:
    cp, _, _ = make_checkpoint(profile, identity)
    proposal = make_proposal(task, agent)
    decision = Decision(
        decision_id=uuid.uuid4().hex,
        call_id=proposal.call_id,
        task_id=task.task_id,
        verdict="deny",
        reason="denied",
        policy_version="v",
        profile_version="v",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )

    with pytest.raises(CheckpointError):
        await cp.forward(proposal, decision)


async def test_forward_decision_reuse(
    task: Task, agent: Agent, profile: CapabilityProfile, identity: ConfigIdentityProvider
) -> None:
    cp, _, gw = make_checkpoint(profile, identity)
    proposal = make_proposal(task, agent)
    decision = Decision(
        decision_id=uuid.uuid4().hex,
        call_id=proposal.call_id,
        task_id=task.task_id,
        verdict="allow",
        reason="ok",
        policy_version="v",
        profile_version="v",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )

    cp._decision_store.record_decision(decision)
    first = await cp.forward(proposal, decision)
    assert first.status == "success"
    assert len(gw.calls) == 1

    # 同一 decision 二次 forward → 防重放抛异常，不产生第二次调用
    with pytest.raises(CheckpointError):
        await cp.forward(proposal, decision)
    assert len(gw.calls) == 1


# ---------------------------------------------------------------------------
# v1.2 session_risk 集成
# ---------------------------------------------------------------------------


async def test_evaluate_includes_session_risk_in_policy_input(
    task: Task, agent: Agent, profile: CapabilityProfile, identity: ConfigIdentityProvider
) -> None:
    """v1.2：evaluate 应将 session_risk 结构传入 build_policy_input。"""
    risk_manager = RiskStateManager()
    risk_manager.update(task.session_id, "deny")
    cp, engine, _ = make_checkpoint(profile, identity, risk_manager=risk_manager)
    proposal = make_proposal(task, agent)

    await cp.evaluate(task, agent, proposal)

    assert len(engine.calls) == 1
    input_doc = engine.calls[0]
    assert "session_risk" in input_doc
    assert input_doc["session_risk"]["score"] == pytest.approx(0.20)
    assert input_doc["session_risk"]["threshold"] == profile.session_risk_threshold
    assert input_doc["session_risk"]["denied_count"] == 1
    assert input_doc["session_risk"]["recent_tags"] == ["deny"]
    assert input_doc["session_risk"]["session_id"] == task.session_id


async def test_evaluate_updates_risk_manager_on_deny(
    task: Task, agent: Agent, profile: CapabilityProfile, identity: ConfigIdentityProvider
) -> None:
    """verdict=deny 时应写入 risk_manager。"""
    risk_manager = RiskStateManager()
    cp, _, _ = make_checkpoint(
        profile,
        identity,
        risk_manager=risk_manager,
        engine_decision={"verdict": "deny", "reason": "policy deny", "policy_hits": ["deny_rule"]},
    )
    proposal = make_proposal(task, agent)

    await cp.evaluate(task, agent, proposal)

    profile_after = risk_manager.get_profile(task.session_id)
    assert profile_after.denied_count == 1
    assert profile_after.recent_tags == ["deny"]
    assert profile_after.cumulative_risk_score == pytest.approx(0.20)


async def test_evaluate_updates_risk_manager_on_require_approval(
    task: Task, agent: Agent, profile: CapabilityProfile, identity: ConfigIdentityProvider
) -> None:
    """verdict=require_approval 时应写入 risk_manager（tag 但无分数）。"""
    risk_manager = RiskStateManager()
    cp, _, _ = make_checkpoint(
        profile,
        identity,
        risk_manager=risk_manager,
        engine_decision={
            "verdict": "require_approval",
            "reason": "needs approval",
            "policy_hits": ["approval_rule"],
            "escalation_target": agent.owner_id,
        },
    )
    proposal = make_proposal(task, agent, tool_name="send_email", arguments={"to": "z@company.com"})

    await cp.evaluate(task, agent, proposal)

    profile_after = risk_manager.get_profile(task.session_id)
    assert profile_after.recent_tags == ["require_approval"]
    assert profile_after.cumulative_risk_score == pytest.approx(0.0)


async def test_forward_low_risk_success_updates_risk_manager(
    task: Task, agent: Agent, profile: CapabilityProfile, identity: ConfigIdentityProvider
) -> None:
    """allow + risk_level=low 执行成功后应衰减风险分。"""
    risk_manager = RiskStateManager()
    risk_manager.update(task.session_id, "deny")
    cp, _, _ = make_checkpoint(profile, identity, risk_manager=risk_manager)
    proposal = make_proposal(task, agent, risk_level="low")
    decision = Decision(
        decision_id=uuid.uuid4().hex,
        call_id=proposal.call_id,
        task_id=task.task_id,
        verdict="allow",
        reason="ok",
        policy_version="v",
        profile_version="v",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )

    cp._decision_store.record_decision(decision)
    await cp.forward(proposal, decision, session_id=task.session_id)

    profile_after = risk_manager.get_profile(task.session_id)
    # deny: 0.20；low_risk_success: 0.20*0.9 - 0.05 = 0.13
    assert profile_after.cumulative_risk_score == pytest.approx(0.13)


async def test_evaluate_modify_upgraded_when_session_risk_high(
    task: Task, agent: Agent, profile: CapabilityProfile, identity: ConfigIdentityProvider
) -> None:
    """v1.2：Reg 返回 modify 但 session_risk 超过阈值时，升级为 require_approval。"""
    risk_manager = RiskStateManager()
    # 让 score 超过默认阈值 0.6：4 次 deny → 0.2*4=0.8
    for _ in range(4):
        risk_manager.update(task.session_id, "deny")
    cp, _, _ = make_checkpoint(
        profile,
        identity,
        risk_manager=risk_manager,
        engine_decision={
            "verdict": "modify",
            "reason": "auto modified",
            "policy_hits": ["modify_rule"],
            "modified_args": {"query": "safe query"},
        },
    )
    proposal = make_proposal(task, agent, arguments={"query": "safe query"})

    decision = await cp.evaluate(task, agent, proposal)

    assert decision.verdict == "require_approval"
    assert "session_risk_gate" in decision.policy_hits


async def test_evaluate_deny_unchanged_when_session_risk_high(
    task: Task, agent: Agent, profile: CapabilityProfile, identity: ConfigIdentityProvider
) -> None:
    """v1.2：session_risk 高时 deny 仍保持 deny，不会被升级。"""
    risk_manager = RiskStateManager()
    for _ in range(4):
        risk_manager.update(task.session_id, "deny")
    cp, _, _ = make_checkpoint(
        profile,
        identity,
        risk_manager=risk_manager,
        engine_decision={"verdict": "deny", "reason": "policy deny", "policy_hits": ["deny_rule"]},
    )
    proposal = make_proposal(task, agent)

    decision = await cp.evaluate(task, agent, proposal)

    assert decision.verdict == "deny"
