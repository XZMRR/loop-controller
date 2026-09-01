import json
from pathlib import Path

import pytest

from loop_controller.go_kernel_bridge import (
    CURRENT_PROTOCOL_VERSION,
    AgentCard,
    AgentEntrypoint,
    A2AMessage,
    DelegationRequest,
    DelegationResponse,
    check_protocol_version,
)

FIXTURE = Path(__file__).resolve().parents[1] / "contract" / "a2a_v0.36.1.json"


@pytest.fixture
def contract() -> dict:
    with FIXTURE.open("r", encoding="utf-8") as f:
        return json.load(f)


def test_current_protocol_version_matches_fixture(contract: dict) -> None:
    assert contract["protocol_version"] == CURRENT_PROTOCOL_VERSION


@pytest.mark.parametrize(
    ("version", "should_raise"),
    [
        ("0.36.1", False),
        ("0.36.0", False),
        ("0.36.99", False),
        ("", False),
        ("0.35.0", True),
        ("0.37.0", True),
        ("not-a-version", True),
    ],
)
def test_check_protocol_version(version: str, should_raise: bool) -> None:
    if should_raise:
        with pytest.raises(ValueError):
            check_protocol_version(version)
    else:
        check_protocol_version(version)


def test_agent_card_roundtrip(contract: dict) -> None:
    fixture = contract["agent_card"]
    card = AgentCard.from_dict(fixture)
    assert card.to_dict() == fixture


def test_message_roundtrip(contract: dict) -> None:
    fixture = contract["message"]
    parts = fixture["parts"]
    msg = A2AMessage(
        message_id=fixture["message_id"],
        task_id=fixture["task_id"],
        from_agent_id=fixture["from_agent_id"],
        to_agent_id=fixture["to_agent_id"],
        role=fixture["role"],
        parts=parts,
        timestamp=fixture["timestamp"],
        protocol_version=fixture["protocol_version"],
    )
    assert msg.to_dict() == fixture


def test_delegation_request_roundtrip(contract: dict) -> None:
    fixture = contract["delegation_request"]
    req = DelegationRequest(
        request_id=fixture["request_id"],
        initiator_agent_id=fixture["initiator_agent_id"],
        target_agent_id=fixture["target_agent_id"],
        tool_name=fixture["tool_name"],
        arguments=json.loads(fixture["arguments_json"]),
        session_id=fixture["session_id"],
        task_id=fixture["task_id"],
        risk_level=fixture["risk_level"],
        protocol_version=fixture["protocol_version"],
    )
    assert req.to_dict()["request_id"] == fixture["request_id"]
    assert req.to_dict()["protocol_version"] == fixture["protocol_version"]


def test_delegation_response_roundtrip(contract: dict) -> None:
    fixture = contract["delegation_response"]
    resp = DelegationResponse.from_dict(fixture)
    assert resp.to_dict() == fixture


def test_delegation_response_default_protocol_version() -> None:
    resp = DelegationResponse(allowed=True)
    assert resp.protocol_version == "0.36.1"
