import json
from pathlib import Path

import pytest

from loop_controller.go_kernel_bridge import (
    CURRENT_PROTOCOL_VERSION,
    A2AMessage,
    AgentCard,
    ApprovalActionRequest,
    DelegationApproval,
    DelegationRequest,
    DelegationResponse,
    check_protocol_version,
)
from loop_controller.utils.canonical import canonical_json

PROJECT_ROOT = Path(__file__).resolve().parents[1]
V053_FIXTURE = PROJECT_ROOT / "contract" / "a2a_v0.53.0.json"
V054_FIXTURE = PROJECT_ROOT / "contract" / "a2a_v0.54.0.json"
OPENAPI = PROJECT_ROOT / "openapi" / "a2a_v0.53.0.yaml"
V054_OPENAPI = PROJECT_ROOT / "openapi" / "a2a_v0.54.0.yaml"
TASK_PATHS = PROJECT_ROOT / "openapi" / "paths" / "tasks.yaml"
TASK_SCHEMA = PROJECT_ROOT / "openapi" / "schemas" / "task.yaml"


@pytest.fixture
def contract() -> dict:
    with V053_FIXTURE.open("r", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def current_contract() -> dict:
    with V054_FIXTURE.open("r", encoding="utf-8") as f:
        return json.load(f)


def test_dual_version_authorities(contract: dict, current_contract: dict) -> None:
    assert contract["protocol_version"] == "0.53.0"
    assert current_contract["protocol_version"] == CURRENT_PROTOCOL_VERSION
    assert current_contract["compatibility"]["accepted_wire_versions"] == [
        "0.53.x",
        "0.54.x",
    ]


@pytest.mark.parametrize(
    ("version", "should_raise"),
    [
        ("0.53.0", False),
        ("0.53.1", False),
        ("0.53.99", False),
        ("0.54.0", False),
        ("0.54.99", False),
        ("", True),
        ("0.52", True),
        ("0.51.0", True),
        ("0.51.1", True),
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
        allowed_tools=fixture["allowed_tools"],
        allowed_capabilities=fixture["allowed_capabilities"],
        allow_redelegation=fixture["allow_redelegation"],
        parent_task_id=fixture["parent_task_id"],
        budget=fixture["budget"],
        deadline=fixture["deadline"],
        protocol_version=fixture["protocol_version"],
        tenant_id=fixture["tenant_id"],
        target_workload_id=fixture["target_workload_id"],
        target_instance_id=fixture["target_instance_id"],
    )
    assert req.to_dict() == fixture


def test_delegation_response_roundtrip(contract: dict) -> None:
    fixture = contract["delegation_response"]
    resp = DelegationResponse.from_dict(fixture)
    assert resp.to_dict() == fixture


def test_delegation_approval_roundtrip_does_not_expose_arguments(contract: dict) -> None:
    fixture = contract["delegation_approval"]
    approval = DelegationApproval.from_dict(fixture)
    assert approval.to_dict() == fixture
    assert "effective_args" not in approval.to_dict()
    action = ApprovalActionRequest(**contract["approval_action_request"])
    assert action.to_dict() == contract["approval_action_request"]


def test_v054_delegation_approval_retains_nonempty_identity_fields(
    current_contract: dict,
) -> None:
    fixture = current_contract["fixtures"]["delegation_approval"]
    approval = DelegationApproval.from_dict(fixture)
    assert approval.to_dict() == fixture
    assert approval.to_dict()["tenant_id"] == "tenant-a"


def test_delegation_response_default_protocol_version() -> None:
    resp = DelegationResponse(allowed=True)
    assert resp.protocol_version == CURRENT_PROTOCOL_VERSION


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
        "delegation_approval",
        "approval_action_request",
        "error_response",
        "sse_event",
        "task_event",
    ):
        fixture = contract[name]
        assert canonical_json(json.loads(json.dumps(fixture))) == canonical_json(fixture)


def test_error_response_fixture(contract: dict) -> None:
    fixture = contract["error_response"]
    assert fixture == {
        "error": "protocol version 0.40.0 is incompatible",
        "code": "incompatible_protocol_version",
    }


def test_sse_event_fixture(contract: dict) -> None:
    fixture = contract["sse_event"]
    event = fixture["data"]
    assert fixture["id"] == event["event_id"]
    assert fixture["event"] == event["event_type"]
    assert event == contract["task_event"]
    assert event["protocol_version"] == "0.53.0"
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


@pytest.mark.parametrize(
    "openapi_path",
    [OPENAPI, V054_OPENAPI],
    ids=["v0.53.0", "v0.54.0"],
)
def test_openapi_external_refs_resolve(openapi_path: Path) -> None:
    import yaml

    loaded: dict[Path, object] = {}
    seen: set[tuple[Path, str]] = set()

    def load(path: Path) -> object:
        if path not in loaded:
            with path.open("r", encoding="utf-8") as f:
                loaded[path] = yaml.safe_load(f)
        return loaded[path]

    def resolve(path: Path, pointer: str) -> object:
        node = load(path)
        if pointer:
            assert pointer.startswith("/")
            for raw_part in pointer[1:].split("/"):
                part = raw_part.replace("~1", "/").replace("~0", "~")
                assert isinstance(node, dict) and part in node, f"unresolved $ref: {path}#{pointer}"
                node = node[part]
        return node

    def walk(node: object, base: Path) -> None:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str):
                target, _, pointer = ref.partition("#")
                target_path = (base.parent / target).resolve() if target else base
                key = (target_path, pointer)
                assert target_path.is_file(), f"missing $ref file: {target_path}"
                if key not in seen:
                    seen.add(key)
                    walk(resolve(target_path, pointer), target_path)
            for value in node.values():
                walk(value, base)
        elif isinstance(node, list):
            for value in node:
                walk(value, base)

    walk(load(openapi_path), openapi_path)


def test_v053_security_contract_components_and_fixture(contract: dict) -> None:
    import yaml

    with OPENAPI.open("r", encoding="utf-8") as f:
        openapi = yaml.safe_load(f)
    schemas = openapi["components"]["schemas"]
    required = {
        "WorkloadIdentity", "DelegatedSubject", "ToolCredentialRef",
        "ResolvedToolCredentialRef", "ExecutionReceipt", "SecurityCapabilities",
        "EntrypointTaskRequest", "EntrypointResultRequest", "GovernToolRequest",
        "GovernToolResponse", "ExecutionSecurityStatus", "HealthResponse",
        "ReadinessResponse", "ProtectedMCPMeta", "ProtectedMCPEnvelope",
        "ProtectedMCPReceipt",
    }
    assert required <= schemas.keys()
    for name in ("DelegationRequest", "EntrypointTaskRequest", "EntrypointResultRequest",
                 "GovernToolRequest", "GovernToolResponse", "ExecutionReceipt"):
        assert schemas[name]["additionalProperties"] is False

    strict_bindings = {"request_id", "interaction_id", "decision_id", "task_id",
                       "tenant_id", "target_workload_id"}
    assert strict_bindings <= set(schemas["EntrypointTaskRequest"]["required"])
    govern_required = set(schemas["GovernToolRequest"]["required"])
    assert strict_bindings | {"call_id", "delegation_jti", "delegation_token",
                              "required_security_capabilities"} <= govern_required
    assert "execution_receipt" in schemas["EntrypointResultRequest"]["required"]
    receipt_required = set(schemas["ExecutionReceipt"]["required"])
    assert {"interaction_id", "delegation_jti", "credential_ref_digest",
            "resolved_credential_version", "resolved_credential_digest"} <= receipt_required

    govern_path = openapi["paths"]["/v1/govern/tool-call"]["post"]
    assert govern_path["security"] == [{"WorkloadMTLS": []}]
    assert govern_path["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/GovernToolRequest"
    }
    assert "io.loop-controller/protected-mcp-v1" in schemas["ProtectedMCPMeta"]["properties"]
    assert contract["task"]["request_id"] == contract["delegation_request"]["request_id"]
    assert contract["task"]["tenant_id"] == contract["delegation_request"]["tenant_id"]
    assert contract["execution_receipt"] == contract["entrypoint_result_request"]["execution_receipt"]
    assert contract["execution_receipt"] == contract["govern_tool_response"]["execution_receipt"]
    assert contract["govern_tool_request"]["target_workload_id"] == contract["task"]["target_workload_id"]


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


def test_v054_authority_covers_real_public_surface(current_contract: dict) -> None:
    import yaml

    with V054_OPENAPI.open("r", encoding="utf-8") as f:
        openapi = yaml.safe_load(f)
    expected_paths = {
        "/a2a/v1/agents",
        "/a2a/v1/agents/{id}",
        "/a2a/v1/tasks",
        "/a2a/v1/tasks/{id}",
        "/a2a/v1/tasks/{id}/snapshot",
        "/a2a/v1/tasks/{id}/stream",
        "/a2a/v1/tasks/{id}/cancel",
        "/a2a/v1/messages",
        "/a2a/v1/delegations",
        "/a2a/v1/delegation-approvals/{id}",
        "/a2a/v1/delegation-approvals/{id}/approve",
        "/a2a/v1/delegation-approvals/{id}/reject",
        "/a2a/v1/delegation-approvals/{id}/cancel",
        "/a2a/v1/dead-letters",
        "/a2a/v1/dead-letters/{id}/replay",
        "/a2a/v1/task-graphs",
        "/a2a/v1/task-graphs/{id}",
        "/a2a/v1/task-graphs/{id}/cancel",
        "/a2a/v1/entrypoint/tasks",
        "/a2a/v1/entrypoint/tasks/{id}/accept",
        "/a2a/v1/entrypoint/tasks/{id}/start",
        "/a2a/v1/entrypoint/tasks/{id}/cancel",
        "/a2a/v1/entrypoint/tasks/{id}",
        "/a2a/v1/entrypoint/tasks/{id}/results",
        "/health",
        "/ready",
        "/metrics",
    }
    assert set(openapi["paths"]) == expected_paths
    assert not any("scheduler" in path or "assignment" in path for path in openapi["paths"])
    schemas = openapi["components"]["schemas"]
    for name in ("AgentSpec", "AgentStatus", "SchedulingRequest", "SchedulingDecision"):
        assert schemas[name]["x-loop-controller-scope"] == "internal-non-public"
    assert schemas["ErrorResponse"]["required"] == ["protocol_version", "error", "code"]
    assert schemas["TaskEvent"]["required"][:3] == [
        "protocol_version",
        "schema_version",
        "sequence",
    ]
    assert current_contract["fixtures"]["sse_event"]["id"] != current_contract["fixtures"][
        "task_event"
    ]["event_id"]


def test_v054_fixtures_and_http_cases_are_strict(current_contract: dict) -> None:
    import yaml
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT202012

    with V054_OPENAPI.open("r", encoding="utf-8") as f:
        openapi = yaml.safe_load(f)
    fixtures = current_contract["fixtures"]
    base_uri = V054_OPENAPI.resolve().as_uri()
    registry = Registry().with_resource(
        base_uri,
        Resource.from_contents(openapi, default_specification=DRAFT202012),
    )
    for fixture_name, schema_name in {
        "agent_card": "AgentCard",
        "agent_spec": "AgentSpec",
        "agent_status": "AgentStatus",
        "scheduling_request": "SchedulingRequest",
        "scheduling_decision": "SchedulingDecision",
        "retry_policy": "RetryPolicy",
        "task_assignment": "TaskAssignment",
        "execution_attempt": "ExecutionAttempt",
        "task": "Task",
        "task_graph": "TaskGraph",
        "task_snapshot": "TaskSnapshot",
        "task_event": "TaskEvent",
        "delegation_response": "DelegationResponse",
        "delegation_approval": "DelegationApproval",
        "error_response": "ErrorResponse",
    }.items():
        Draft202012Validator(
            {"$ref": f"{base_uri}#/components/schemas/{schema_name}"},
            registry=registry,
        ).validate(fixtures[fixture_name])
    assert {case["name"] for case in current_contract["http_cases"]} >= {
        "register_agent",
        "list_agents",
        "message_rejected",
        "delegation_polymorphic",
        "approval_cancel_polymorphic",
        "snapshot",
        "sse_resume",
        "sse_bad_cursor",
        "sse_expired_cursor",
        "sse_closed",
        "dead_letter_list",
        "dead_letter_replay",
        "dag_create",
        "entrypoint_create",
        "health",
        "unknown_field",
    }
