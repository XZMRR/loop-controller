"""旧 DelegationAuthorizer API 到 IIGE 的兼容测试（v0.39.0）。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from loop_controller.delegation import DelegationAuthorizeEndpoint, DelegationAuthorizer
from loop_controller.interaction.models import InteractionDecision
from loop_controller.models import ActionProposal


def _proposal(
    *,
    action_kind: str = "delegation",
    target_agent_id: str | None = "agent-b",
) -> ActionProposal:
    return ActionProposal(
        task_id="task-001",
        call_id="call-001",
        agent_id="agent-a",
        tool_name="read_file",
        arguments={"path": "/tmp/x"},
        task_context="",
        action_kind=action_kind,
        target_agent_id=target_agent_id,
    )


def _decision(verdict: str, reason: str) -> InteractionDecision:
    return InteractionDecision(
        decision_id="dec-001",
        interaction_id="call-001",
        request_id="call-001",
        verdict=verdict,
        reason=reason,
        target_entrypoint={"type": "http", "url": "http://agent-b"},
    )


def _authorizer(decision: InteractionDecision) -> tuple[DelegationAuthorizer, AsyncMock]:
    engine = MagicMock()
    engine.evaluate = AsyncMock(return_value=decision)
    authorizer = DelegationAuthorizer(object(), engine=engine)  # type: ignore[arg-type]
    return authorizer, engine


async def test_non_delegation_action_is_rejected() -> None:
    authorizer, engine = _authorizer(_decision("allow", "allowed"))
    result = await authorizer.authorize(_proposal(action_kind="tool_call"))
    assert result.status == "blocked"
    assert result.error_code == "invalid_action_kind"
    engine.evaluate.assert_not_awaited()


async def test_missing_target_agent_id_is_rejected() -> None:
    authorizer, engine = _authorizer(_decision("allow", "allowed"))
    result = await authorizer.authorize(_proposal(target_agent_id=None))
    assert result.status == "blocked"
    engine.evaluate.assert_not_awaited()


async def test_iige_deny_verdict_is_rejected() -> None:
    authorizer, engine = _authorizer(_decision("deny", "interaction policy denies"))
    result = await authorizer.authorize(_proposal())
    assert result.status == "blocked"
    assert result.reason == "interaction policy denies"
    engine.evaluate.assert_awaited_once()


async def test_iige_allow_verdict_returns_delegated() -> None:
    authorizer, engine = _authorizer(_decision("allow", "IIGE authorized delegation"))
    result = await authorizer.authorize(_proposal())
    assert result.status == "delegated"
    assert result.reason == "IIGE authorized delegation"
    assert result.content["decision_id"] == "dec-001"
    assert result.content["target_agent_id"] == "agent-b"
    interaction_proposal = engine.evaluate.await_args.args[0]
    assert interaction_proposal.source_agent_id == "agent-a"
    assert interaction_proposal.target_agent_id == "agent-b"


async def test_iige_require_approval_is_preserved() -> None:
    authorizer, _ = _authorizer(_decision("require_approval", "approval required"))
    result = await authorizer.authorize(_proposal())
    assert result.status == "require_approval"
    assert result.request_id == "dec-001"


async def test_legacy_endpoint_maps_initiator_to_source() -> None:
    authorizer, engine = _authorizer(_decision("allow", "allowed"))
    endpoint = DelegationAuthorizeEndpoint(authorizer)
    response = await endpoint.handle(
        {
            "protocol_version": "0.39.0",
            "request_id": "req-1",
            "initiator_agent_id": "agent-a",
            "target_agent_id": "agent-b",
            "tool_name": "read_file",
            "arguments": {"path": "/tmp/x"},
        }
    )
    assert response["allowed"] is True
    proposal = engine.evaluate.await_args.args[0]
    assert proposal.source_agent_id == "agent-a"


async def test_legacy_endpoint_protocol_version_fail_closed() -> None:
    authorizer, engine = _authorizer(_decision("allow", "allowed"))
    endpoint = DelegationAuthorizeEndpoint(authorizer)
    response = await endpoint.handle({"protocol_version": "0.38.0"})
    assert response["allowed"] is False
    assert response["verdict"] == "deny"
    engine.evaluate.assert_not_awaited()


async def test_legacy_endpoint_missing_fields_is_denied() -> None:
    authorizer, engine = _authorizer(_decision("allow", "allowed"))
    endpoint = DelegationAuthorizeEndpoint(authorizer)
    response = await endpoint.handle({"protocol_version": "0.39.0"})
    assert response["allowed"] is False
    assert "missing required delegation fields" in response["reason"]
    engine.evaluate.assert_not_awaited()
