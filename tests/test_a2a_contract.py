import json
from pathlib import Path

import pytest

from loop_controller.go_kernel_bridge import (
    CURRENT_PROTOCOL_VERSION,
    A2AMessage,
    AgentCard,
    DelegationRequest,
    DelegationResponse,
    check_protocol_version,
)
from loop_controller.utils.canonical import canonical_json

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = PROJECT_ROOT / "contract" / "a2a_v0.40.0.json"
OPENAPI = PROJECT_ROOT / "openapi" / "a2a_v0.40.0.yaml"
TASK_PATHS = PROJECT_ROOT / "openapi" / "paths" / "tasks.yaml"
TASK_SCHEMA = PROJECT_ROOT / "openapi" / "schemas" / "task.yaml"


@pytest.fixture
def contract() -> dict:
    with FIXTURE.open("r", encoding="utf-8") as f:
        return json.load(f)


def test_current_protocol_version_matches_fixture(contract: dict) -> None:
    assert contract["protocol_version"] == CURRENT_PROTOCOL_VERSION


@pytest.mark.parametrize(
    ("version", "should_raise"),
    [
        ("0.40.0", False),
        ("0.40.1", False),
        ("0.40.99", False),
        ("", True),
        ("0.39.1", True),
        ("0.41.0", True),
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
        arguments=fixture["arguments"],
        session_id=fixture["session_id"],
        task_id=fixture["task_id"],
        risk_level=fixture["risk_level"],
        protocol_version=fixture["protocol_version"],
    )
    assert req.to_dict() == fixture


def test_delegation_response_roundtrip(contract: dict) -> None:
    fixture = contract["delegation_response"]
    resp = DelegationResponse.from_dict(fixture)
    assert resp.to_dict() == fixture


def test_delegation_response_default_protocol_version() -> None:
    resp = DelegationResponse(allowed=True)
    assert resp.protocol_version == "0.40.0"


def test_task_fixture_is_canonical_and_has_stable_timestamps(contract: dict) -> None:
    fixture = contract["task"]
    assert fixture["task_id"] == "task-001"
    assert fixture["status"] == "pending"
    assert fixture["created_at"].endswith("Z")
    assert canonical_json(json.loads(canonical_json(fixture))) == canonical_json(fixture)


def test_all_roundtrip_fixtures_have_stable_canonical_json(contract: dict) -> None:
    for name in (
        "agent_card",
        "task",
        "message",
        "delegation_request",
        "delegation_response",
        "error_response",
        "sse_event",
        "task_event",
    ):
        fixture = contract[name]
        assert canonical_json(json.loads(json.dumps(fixture))) == canonical_json(fixture)


def test_error_response_fixture(contract: dict) -> None:
    fixture = contract["error_response"]
    assert fixture == {
        "error": "protocol version 0.39.0 is incompatible",
        "code": "incompatible_protocol_version",
    }


def test_sse_event_fixture(contract: dict) -> None:
    fixture = contract["sse_event"]
    event = fixture["data"]
    assert fixture["id"] == event["event_id"]
    assert fixture["event"] == event["event_type"]
    assert event == contract["task_event"]
    assert event["protocol_version"] == CURRENT_PROTOCOL_VERSION
    assert event["task_id"] == contract["task"]["task_id"]


def test_openapi_task_token_and_status_contract() -> None:
    import yaml

    with OPENAPI.open("r", encoding="utf-8") as f:
        openapi = yaml.safe_load(f)
    with TASK_PATHS.open("r", encoding="utf-8") as f:
        task_paths = yaml.safe_load(f)
    with TASK_SCHEMA.open("r", encoding="utf-8") as f:
        task_schema = yaml.safe_load(f)

    entrypoint_paths = (
        "/a2a/v1/entrypoint/tasks",
        "/a2a/v1/entrypoint/tasks/{id}/accept",
        "/a2a/v1/entrypoint/tasks/{id}/start",
        "/a2a/v1/entrypoint/tasks/{id}/cancel",
        "/a2a/v1/entrypoint/tasks/{id}",
        "/a2a/v1/entrypoint/tasks/{id}/results",
    )
    assert openapi["components"]["securitySchemes"]["TaskDelegationToken"] == {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "JWT",
        "description": "Task-scoped delegation token issued by the A2A kernel.",
    }
    for path in entrypoint_paths:
        assert path in openapi["paths"]
        operation = task_paths[path]["get" if path.endswith("{id}") else "post"]
        assert operation["security"] == [{"TaskDelegationToken": []}]

    assert task_schema["properties"]["status"]["enum"] == [
        "pending",
        "accepted",
        "running",
        "completed",
        "failed",
        "cancelled",
        "outcome_unknown",
    ]
    result_status = task_paths["/a2a/v1/entrypoint/tasks/{id}/results"]["post"][
        "requestBody"
    ]["content"]["application/json"]["schema"]["properties"]["status"]["enum"]
    assert result_status == ["completed", "failed"]


def classify_message_error(message: dict) -> str | None:
    required = {"message_id", "task_id", "from_agent_id", "to_agent_id", "parts"}
    if not required.issubset(message):
        return "invalid_request"
    try:
        check_protocol_version(message.get("protocol_version", ""))
    except ValueError:
        return "incompatible_protocol_version"
    if any(part.get("type") not in {"text", "data"} for part in message["parts"]):
        return "invalid_message_parts"
    return None


@pytest.mark.parametrize("case_index", range(3))
def test_error_cases_have_expected_category(contract: dict, case_index: int) -> None:
    case = contract["error_cases"][case_index]
    assert classify_message_error(case["message"]) == case["category"]
