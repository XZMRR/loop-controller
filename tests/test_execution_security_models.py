"""v0.53 第一阶段执行安全模型测试。"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from loop_controller.executors.base import (
    DelegatedSubject,
    ExecutionContext,
    ExecutionReceipt,
    ExecutionReceiptType,
    ExecutionTerminalStatus,
    WorkloadAuthMethod,
    WorkloadIdentity,
)
from loop_controller.secrets import (
    CredentialInjection,
    CredentialVersionMode,
    ResolvedToolCredentialRef,
    SecretScope,
    ToolCredentialRef,
)


def test_tool_credential_ref_strict_validation() -> None:
    with pytest.raises(ValidationError):
        ToolCredentialRef(
            name="token",
            scope=SecretScope.TENANT,
            injection=CredentialInjection.HEADER,
        )
    with pytest.raises(ValidationError):
        ToolCredentialRef(
            name="token",
            scope=SecretScope.GLOBAL,
            tenant_id="acme",
            injection=CredentialInjection.HEADER,
        )
    with pytest.raises(ValidationError):
        ToolCredentialRef(
            name="token",
            scope=SecretScope.GLOBAL,
            version_mode=CredentialVersionMode.PINNED,
            injection=CredentialInjection.HEADER,
        )
    with pytest.raises(ValidationError):
        ToolCredentialRef(
            name="token",
            scope=SecretScope.GLOBAL,
            version_mode=CredentialVersionMode.CURRENT,
            version="7",
            injection=CredentialInjection.HEADER,
        )


def test_resolved_ref_is_frozen_and_contains_no_secret() -> None:
    ref = ResolvedToolCredentialRef(
        name="token",
        scope=SecretScope.TENANT,
        tenant_id="acme",
        key="access_token",
        version_mode=CredentialVersionMode.PINNED,
        resolved_version="7",
        injection=CredentialInjection.QUERY,
    )
    assert set(ref.model_dump()) == {
        "name",
        "scope",
        "tenant_id",
        "key",
        "version_mode",
        "resolved_version",
        "injection",
        "allowed_tools",
        "tenant_allowlist",
    }
    with pytest.raises(ValidationError):
        ref.resolved_version = "8"  # type: ignore[misc]


def test_execution_context_security_fields_are_backward_compatible() -> None:
    old = ExecutionContext(call_id="c", task_id="t", agent_id="a", user_id="u")
    assert old.request_id is None
    assert old.resolved_credentials == ()
    assert old.security_capabilities == frozenset()

    now = datetime.now(UTC)
    workload = WorkloadIdentity(
        workload_id="spiffe://example/executor",
        service="executor",
        principal="executor.example",
        authenticated_at=now,
        auth_method=WorkloadAuthMethod.MTLS,
    )
    subject = DelegatedSubject(
        agent_id="agent",
        tenant_id="acme",
        request_id="r",
        task_id="t",
        call_id="c",
        decision_id="d",
    )
    context = ExecutionContext(
        call_id="c",
        task_id="t",
        agent_id="agent",
        user_id="user",
        request_id="r",
        workload_identity=workload,
        delegated_subject=subject,
        security_capabilities=frozenset({"workload_identity_v1"}),
    )
    assert context.workload_identity == workload
    assert context.delegated_subject == subject


def test_receipt_deterministic_json_hash_key_order_and_unicode() -> None:
    first = {"z": "Привет", "a": {"β": 1}}
    second = {"a": {"β": 1}, "z": "Привет"}
    first_hash = ExecutionReceipt.hash_result(first)
    second_hash = ExecutionReceipt.hash_result(second)
    canonical = json.dumps(
        first,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    assert first_hash == second_hash
    assert first_hash == (
        hashlib.sha256(canonical).hexdigest(),
        "application/json",
        "utf-8",
    )


def test_receipt_text_and_bytes_hash_raw_payload() -> None:
    text = "Привет"
    raw = b"\x00\xffpayload"
    assert ExecutionReceipt.hash_result(text) == (
        hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "text/plain",
        "utf-8",
    )
    assert ExecutionReceipt.hash_result(raw) == (
        hashlib.sha256(raw).hexdigest(),
        "application/octet-stream",
        None,
    )


def test_result_sha256_shared_cross_language_vectors() -> None:
    fixture = json.loads(
        (Path(__file__).parents[1] / "contract" / "result_sha256_vectors.json").read_text(
            encoding="utf-8"
        )
    )
    for vector in fixture["accepted"]:
        result = vector["wire_result"]
        if vector["media_type"] == "application/octet-stream":
            result = base64.b64decode(result, validate=True)
        assert ExecutionReceipt.hash_result(result) == (
            vector["sha256"],
            vector["media_type"],
            vector["encoding"],
        )

    for vector in fixture["rejected_json"]:
        with pytest.raises(ValueError):
            ExecutionReceipt.hash_result(json.loads(vector["wire_json"]))


def test_receipt_accepts_three_types_and_four_terminal_states() -> None:
    now = datetime.now(UTC)
    for receipt_type in ExecutionReceiptType:
        for status in ExecutionTerminalStatus:
            receipt = ExecutionReceipt(
                receipt_id=f"{receipt_type}-{status}",
                type=receipt_type,
                attester_workload_id="spiffe://example/executor",
                request_id="r",
                task_id="t",
                call_id="c",
                decision_id="d",
                executor="http",
                backend="protected-http",
                status=status,
                result_sha256="0" * 64,
                media_type="application/json",
                encoding="utf-8",
                issued_at=now,
            )
            assert receipt.status == status
