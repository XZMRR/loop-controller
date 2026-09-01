"""Python Runtime 与 Go 交互治理内核集成测试（v0.36.0）。"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from loop_controller.approval_manager import AsyncApprovalManager
from loop_controller.budget import InMemoryBudgetLedger
from loop_controller.checkpoint import Checkpoint, InMemoryDecisionStore
from loop_controller.classifier import RuleBasedClassifier
from loop_controller.controller import LoopController
from loop_controller.go_kernel_bridge import (
    AgentCard,
    AgentEntrypoint,
    DelegationRequest,
    GoKernelBridge,
)
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
from loop_controller.session import SessionManager

REPO_ROOT = Path(__file__).resolve().parent.parent


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
    def __init__(self) -> None:
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


def _go_bin() -> str:
    found = shutil.which("go")
    if found:
        return found
    raise RuntimeError("go executable not found in PATH")


@pytest.fixture(scope="module")
def kernel_url() -> str:
    port = 18081
    url = f"http://127.0.0.1:{port}"
    go_root = REPO_ROOT / "go"
    proc = subprocess.Popen(
        [_go_bin(), "run", "./cmd/kernel", "-addr", f":{port}", "-secret", "test-secret"],
        cwd=go_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    deadline = time.perf_counter() + 15.0
    while time.perf_counter() < deadline:
        try:
            resp = httpx.get(f"{url}/health", timeout=1.0)
            if resp.status_code == 200:
                break
        except httpx.RequestError:
            pass
        time.sleep(0.2)
    else:
        proc.terminate()
        proc.wait(timeout=5.0)
        raise RuntimeError("Go kernel did not start in time")

    yield url

    proc.terminate()
    try:
        proc.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5.0)


def _build_controller_with_bridge(audit_path: Path, bridge: GoKernelBridge) -> LoopController:
    agent = Agent(
        agent_id="planner_001",
        name="Planner",
        profile_id="p1",
        owner_id="manager",
    )
    identity = ConfigIdentityProvider(
        agents={agent.agent_id: agent},
        users={"alice": "Alice", "manager": "Manager"},
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
        go_kernel_bridge=bridge,
    )
    return LoopController(runtime)


@pytest.mark.asyncio
async def test_loop_controller_delegates_to_agent(kernel_url: str, tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    bridge = GoKernelBridge(base_url=kernel_url, timeout=5.0)

    # Register executor agent in Go kernel.
    ok = await bridge.register_agent(
        AgentCard(
            agent_id="executor_001",
            name="Executor",
            entrypoint=AgentEntrypoint("http", "http://executor:8080"),
            capabilities=["delegate_execution"],
        )
    )
    assert ok, "failed to register executor agent"

    controller = _build_controller_with_bridge(audit_path, bridge)
    await controller.start()
    try:
        result = await controller.evaluate_and_execute(
            agent_id="planner_001",
            user_id="alice",
            tool_name="web_search",
            arguments={"query": "A2A", "__target_agent_id": "executor_001"},
        )
        assert result.status == "allow"
        assert result.content.get("delegated") is True
        assert result.content.get("target_agent_id") == "executor_001"
        assert result.content.get("delegation_token")
        assert result.content.get("target_entrypoint") == "http://executor:8080"
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_loop_controller_rejects_unknown_target_agent(kernel_url: str, tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    bridge = GoKernelBridge(base_url=kernel_url, timeout=5.0)
    controller = _build_controller_with_bridge(audit_path, bridge)
    await controller.start()
    try:
        result = await controller.evaluate_and_execute(
            agent_id="planner_001",
            user_id="alice",
            tool_name="web_search",
            arguments={"query": "A2A", "__target_agent_id": "unknown_agent"},
        )
        assert result.status == "blocked"
        assert result.error_code == "delegation_denied"
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_stream_task_updates(kernel_url: str) -> None:
    bridge = GoKernelBridge(base_url=kernel_url, timeout=5.0)
    await bridge.register_agent(
        AgentCard(
            agent_id="executor_002",
            name="Executor",
            entrypoint=AgentEntrypoint("http", "http://executor:8080"),
            capabilities=["delegate_execution"],
        )
    )

    resp = await bridge.request_delegation(
        DelegationRequest(
            request_id="req-stream",
            initiator_agent_id="planner_001",
            target_agent_id="executor_002",
            tool_name="echo",
        )
    )
    assert resp.allowed
    task_id = resp.task_id

    # Collect first SSE event in background.
    events: list[dict] = []

    async def collect() -> None:
        async for ev in bridge.stream_task(task_id, timeout=3.0):
            events.append(ev)
            break

    task = asyncio.create_task(collect())
    await asyncio.sleep(0.1)
    # Trigger another event by re-requesting delegation on same task.
    await bridge.request_delegation(
        DelegationRequest(
            request_id="req-stream-2",
            initiator_agent_id="planner_001",
            target_agent_id="executor_002",
            tool_name="echo",
            task_id=task_id,
        )
    )
    await asyncio.wait_for(task, timeout=3.0)

    assert len(events) >= 1
    assert events[0].get("task_id") == task_id
