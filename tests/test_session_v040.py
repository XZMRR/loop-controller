"""v0.4.0 Session 持久化与跨 Task 风险状态测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from loop_controller.budget import InMemoryBudgetLedger
from loop_controller.checkpoint import Checkpoint
from loop_controller.infra.config_loader import ConfigLoader
from loop_controller.infra.identity import ConfigIdentityProvider
from loop_controller.models import (
    ActionProposal,
    Agent,
    CapabilityProfile,
    Task,
    ToolPermission,
    ToolResult,
)
from loop_controller.permission_interaction import ConfigPermissionInteractionAnalyzer
from loop_controller.risk_state import JsonlRiskStateStore, RiskStateManager
from loop_controller.session import (
    JsonlSessionBackend,
    Session,
    SessionManager,
    SessionStoreError,
)


class StubPolicyStore:
    def __init__(self, version: str = "0123456789ab") -> None:
        self._version = version

    def policy_path(self, name: str) -> str:
        return f"policies/{name}.rego"

    def current_version(self) -> str:
        return self._version


class FakePolicyEngine:
    """返回固定 allow 判定；可记录 input_doc。"""

    def __init__(self, decision: dict | None = None) -> None:
        self._default = decision or {
            "verdict": "allow",
            "reason": "allowed",
            "policy_hits": ["fake_allow"],
        }
        self.calls: list[dict] = []

    async def evaluate(self, package: str, input_doc: dict) -> dict:
        self.calls.append(input_doc)
        return dict(self._default)


class FakeGateway:
    async def call_tool(
        self,
        tool_name: str,
        arguments: dict,
        call_id: str,
        task_id: str,
        **kwargs: Any,
    ) -> ToolResult:
        return ToolResult(
            call_id=call_id,
            task_id=task_id,
            tool_name=tool_name,
            status="success",
            content="ok",
        )


def _make_minimal_config(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "policies").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "agents.yaml").write_text(
        'agents:\n  - {agent_id: "a1", name: "a1", profile_id: "p1", owner_id: "owner"}\n'
        'users:\n  - {user_id: "alice", display_name: "Alice"}\n',
        encoding="utf-8",
    )
    (tmp_path / "config" / "profiles.yaml").write_text(
        'profiles:\n  - {profile_id: "p1", tools: {}}\n',
        encoding="utf-8",
    )
    (tmp_path / "config" / "mcp_servers.yaml").write_text(
        "servers: {}\ntool_mapping: {}\n",
        encoding="utf-8",
    )
    (tmp_path / "config" / "permission_rules.yaml").write_text(
        "rules: []\n", encoding="utf-8"
    )
    (tmp_path / "config" / "masking_rules.yaml").write_text(
        "rules: []\n", encoding="utf-8"
    )
    (tmp_path / "config" / "approval.yaml").write_text(
        'default: "owner"\nrules: []\n', encoding="utf-8"
    )
    (tmp_path / "config" / "llm_planner.yaml").write_text(
        "enabled: false\n", encoding="utf-8"
    )
    (tmp_path / "config" / "scripted_plan.yaml").write_text(
        "steps: []\n", encoding="utf-8"
    )


def test_jsonl_session_backend_persists_and_reloads(tmp_path: Path) -> None:
    """S1：Session 持久化后端能写入并在新建后端时恢复。"""
    path = tmp_path / "sessions.jsonl"
    backend = JsonlSessionBackend(path)
    session = Session(
        session_id="s-001",
        user_id="alice",
        agent_id="agent_1",
        created_at=datetime.now(UTC),
        last_task_at=datetime.now(UTC),
        active=True,
    )
    backend.put(session)

    backend2 = JsonlSessionBackend(path)
    loaded = backend2.get_by_id("s-001")
    assert loaded is not None
    assert loaded.user_id == "alice"
    assert loaded.active is True


def test_jsonl_session_backend_corrupt_middle_line_fail_closed(tmp_path: Path) -> None:
    """S8：中间行损坏时启动抛 SessionStoreError。"""
    path = tmp_path / "sessions.jsonl"
    path.write_text(
        '{"session_id": "s-001", "user_id": "alice", "agent_id": "a1", "created_at": "2026-08-19T10:00:00Z", "last_task_at": "2026-08-19T10:00:00Z", "active": true}\n'
        "this is not json\n"
        '{"session_id": "s-002", "user_id": "bob", "agent_id": "a1", "created_at": "2026-08-19T10:00:00Z", "last_task_at": "2026-08-19T10:00:00Z", "active": true}\n',
        encoding="utf-8",
    )
    with pytest.raises(SessionStoreError, match=r"sessions\.jsonl 第 2 行损坏"):
        JsonlSessionBackend(path)


def test_session_manager_is_session_expired() -> None:
    """S3：超过 TTL 的 Session 被视为过期。"""
    base = datetime(2026, 8, 19, 10, 0, 0, tzinfo=UTC)
    manager = SessionManager(
        session_timeout_minutes=30,
        now=lambda: base + timedelta(minutes=31),
    )
    session = manager.get_or_create_session("alice", "agent_1")
    # 手动把 last_task_at 设回过去
    session = Session(
        session_id=session.session_id,
        user_id=session.user_id,
        agent_id=session.agent_id,
        created_at=base,
        last_task_at=base,
        active=True,
    )
    backend = manager._backend
    backend.put(session)
    assert manager.is_session_expired(session.session_id) is True


def test_risk_state_manager_tracks_consecutive_denies() -> None:
    """S4/S6：连续 deny 计数增加，成功动作后归零。"""
    manager = RiskStateManager()
    sid = "s-001"

    manager.update(sid, "deny")
    manager.update(sid, "deny")
    profile = manager.get_profile(sid)
    assert profile.consecutive_deny_count == 2

    manager.update(sid, "low_risk_success")
    profile = manager.get_profile(sid)
    assert profile.consecutive_deny_count == 0


@pytest.mark.asyncio
async def test_checkpoint_session_consecutive_deny_block() -> None:
    """S5：连续 deny 达到 threshold 后 evaluate 直接 deny。"""
    now = datetime.now(UTC)
    agent = Agent(agent_id="a1", name="a1", profile_id="p1", owner_id="owner")
    profile = CapabilityProfile(
        profile_id="p1",
        tools={"t1": ToolPermission(tool_name="t1", allowed=True, max_calls_per_task=10)},
        session_block_threshold=3,
    )
    identity = ConfigIdentityProvider({"a1": agent}, {"alice": "Alice"})
    checkpoint = Checkpoint(
        profiles={"p1": profile},
        policy_engine=FakePolicyEngine(),
        policy_store=StubPolicyStore(),
        gateway=FakeGateway(),
        identity=identity,
        budget_ledger=InMemoryBudgetLedger(),
        permission_analyzer=ConfigPermissionInteractionAnalyzer([]),
    )

    # 预置连续 3 次 deny
    checkpoint._risk_manager.update("s-block", "deny")
    checkpoint._risk_manager.update("s-block", "deny")
    checkpoint._risk_manager.update("s-block", "deny")

    task = Task(
        task_id="t1",
        session_id="s-block",
        user_id="alice",
        agent_id="a1",
        description="test",
        created_at=now,
    )
    proposal = ActionProposal(
        task_id="t1",
        call_id="c1",
        agent_id="a1",
        tool_name="t1",
        arguments={},
        task_context="",
    )

    decision = await checkpoint.evaluate(task, agent, proposal)
    assert decision.verdict == "deny"
    assert "session_consecutive_deny_block" in decision.policy_hits


def test_runtime_create_task_reuses_session(tmp_path: Path) -> None:
    """S2：Runtime.create_task 可通过 session_id 复用 Session。"""
    from loop_controller.runtime import build_runtime

    _make_minimal_config(tmp_path)
    config = ConfigLoader().load(tmp_path / "config", opa_base_url=None)

    runtime = build_runtime(config)
    task1, session1 = runtime.create_task("alice", "a1", "first task")
    assert task1.session_id == session1.session_id

    task2, session2 = runtime.create_task(
        "alice", "a1", "second task", session_id=session1.session_id
    )
    assert session2.session_id == session1.session_id
    assert task2.session_id == session1.session_id


def test_runtime_create_task_rejects_agent_mismatch_before_writes(tmp_path: Path) -> None:
    from loop_controller.runtime import build_runtime

    _make_minimal_config(tmp_path)
    config = ConfigLoader().load(tmp_path / "config", opa_base_url=None)
    runtime = build_runtime(config)
    _, session = runtime.create_task("alice", "a1", "first task")
    tasks_before = Path(config.task_store_path).read_bytes()
    conversations_before = Path(config.conversation_path).read_bytes()

    with pytest.raises(ValueError, match="不一致"):
        runtime.create_task(
            "alice",
            "a2",
            "must not persist",
            session_id=session.session_id,
        )

    assert Path(config.task_store_path).read_bytes() == tasks_before
    assert Path(config.conversation_path).read_bytes() == conversations_before


def test_risk_state_persists_across_restart(tmp_path: Path) -> None:
    """S7：风险状态在进程重启后通过 JSONL 恢复。"""
    path = tmp_path / "risk_state.jsonl"
    store1 = JsonlRiskStateStore(path)
    manager1 = RiskStateManager(store1)
    manager1.update("s-restart", "deny")
    manager1.update("s-restart", "deny")

    store2 = JsonlRiskStateStore(path)
    manager2 = RiskStateManager(store2)
    profile = manager2.get_profile("s-restart")
    assert profile.consecutive_deny_count == 2
