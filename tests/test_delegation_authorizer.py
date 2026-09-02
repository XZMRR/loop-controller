"""DelegationAuthorizer 与 DelegationAuthorizeEndpoint 单元测试（v0.37.0）。"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from loop_controller.delegation import (
    DelegationAuthorizeEndpoint,
    DelegationAuthorizer,
)
from loop_controller.go_kernel_bridge import AgentCard, AgentEntrypoint, GoKernelBridge
from loop_controller.models import ActionProposal


def _proposal(
    *,
    action_kind: str = "delegation",
    target_agent_id: str | None = "agent-b",
    tool_name: str = "read_file",
) -> ActionProposal:
    return ActionProposal(
        task_id="task-001",
        call_id="call-001",
        agent_id="agent-a",
        tool_name=tool_name,
        arguments={"path": "/tmp/x"},
        task_context="",
        action_kind=action_kind,
        target_agent_id=target_agent_id,
    )


@pytest.fixture
def bridge() -> GoKernelBridge:
    return GoKernelBridge(base_url="http://localhost:9999")


async def test_non_delegation_action_is_rejected(
    bridge: GoKernelBridge,
) -> None:
    controller: Any = object()
    authorizer = DelegationAuthorizer(controller, bridge=bridge)
    proposal = _proposal(action_kind="tool_call")
    result = await authorizer.authorize(proposal)
    assert result.status == "blocked"
    assert result.error_code == "invalid_action_kind"


async def test_missing_target_agent_id_is_rejected(
    bridge: GoKernelBridge,
) -> None:
    controller: Any = object()
    authorizer = DelegationAuthorizer(controller, bridge=bridge)
    proposal = _proposal(target_agent_id=None)
    result = await authorizer.authorize(proposal)
    assert result.status == "blocked"


async def test_unregistered_target_agent_is_rejected(
    bridge: GoKernelBridge,
) -> None:
    controller: Any = object()
    authorizer = DelegationAuthorizer(controller, bridge=bridge)
    bridge.get_agent = AsyncMock(return_value=None)  # type: ignore[method-assign]
    proposal = _proposal()
    result = await authorizer.authorize(proposal)
    assert result.status == "blocked"
    assert "not registered" in result.reason


async def test_target_without_delegate_execution_capability_is_rejected(
    bridge: GoKernelBridge,
) -> None:
    controller: Any = object()
    authorizer = DelegationAuthorizer(controller, bridge=bridge)
    bridge.get_agent = AsyncMock(  # type: ignore[method-assign]
        return_value=AgentCard(
            agent_id="agent-b",
            capabilities=["chat"],
            entrypoint=AgentEntrypoint("http", "http://agent-b"),
        )
    )
    proposal = _proposal()
    result = await authorizer.authorize(proposal)
    assert result.status == "blocked"
    assert "delegate_execution" in result.reason


async def test_r2_deny_verdict_is_rejected(
    bridge: GoKernelBridge,
) -> None:
    controller: Any = AsyncMock()
    controller.evaluate = AsyncMock(
        return_value=AsyncMock(
            decision=AsyncMock(
                verdict="deny",
                reason="policy denies delegation",
            ),
        )
    )
    authorizer = DelegationAuthorizer(controller, bridge=bridge)
    bridge.get_agent = AsyncMock(  # type: ignore[method-assign]
        return_value=AgentCard(
            agent_id="agent-b",
            capabilities=["delegate_execution"],
            entrypoint=AgentEntrypoint("http", "http://agent-b"),
        )
    )
    proposal = _proposal()
    result = await authorizer.authorize(proposal)
    assert result.status == "blocked"
    assert "policy denies delegation" in result.reason


async def test_r2_allow_verdict_returns_delegated(
    bridge: GoKernelBridge,
) -> None:
    from datetime import UTC, datetime, timedelta

    from loop_controller.models import Decision, EvaluationResult

    decision = Decision(
        decision_id="dec-001",
        call_id="call-001",
        task_id="task-001",
        verdict="allow",
        reason="policy allows delegation",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    controller: Any = AsyncMock()
    controller.evaluate = AsyncMock(
        return_value=EvaluationResult(
            status="allow",
            decision=decision,
        )
    )
    authorizer = DelegationAuthorizer(controller, bridge=bridge)
    bridge.get_agent = AsyncMock(  # type: ignore[method-assign]
        return_value=AgentCard(
            agent_id="agent-b",
            capabilities=["delegate_execution"],
            entrypoint=AgentEntrypoint("http", "http://agent-b"),
        )
    )
    proposal = _proposal()
    result = await authorizer.authorize(proposal)
    assert result.status == "delegated"
    assert result.reason == "R2 authorized delegation"
    assert result.decision is not None
    assert result.decision.action_kind == "delegation"


async def test_authorize_endpoint_protocol_version_fail_closed(
    bridge: GoKernelBridge,
) -> None:
    controller: Any = object()
    authorizer = DelegationAuthorizer(controller, bridge=bridge)
    endpoint = DelegationAuthorizeEndpoint(authorizer)
    resp = await endpoint.handle({"protocol_version": "0.35.0"})
    assert resp["allowed"] is False
    assert resp["verdict"] == "deny"


async def test_authorize_endpoint_missing_fields_is_denied(
    bridge: GoKernelBridge,
) -> None:
    controller: Any = object()
    authorizer = DelegationAuthorizer(controller, bridge=bridge)
    endpoint = DelegationAuthorizeEndpoint(authorizer)
    resp = await endpoint.handle({"protocol_version": "0.37.0"})
    assert resp["allowed"] is False
    assert "missing required delegation fields" in resp["reason"]
