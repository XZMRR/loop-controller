import hashlib
import hmac
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from loop_controller.deployment_proof import DeploymentProof
from loop_controller.execution_security import (
    ExecutionSecurityPolicy,
    WorkloadRegistration,
    WorkloadRegistry,
    supports_security_capabilities,
)
from loop_controller.executors.base import WorkloadAuthMethod, WorkloadIdentity


def test_security_capability_subset() -> None:
    assert supports_security_capabilities(["a"], ["a", "b"])
    assert not supports_security_capabilities(["a", "c"], ["a", "b"])


def test_workload_registry_binds_agent_tenant_and_instance() -> None:
    registry = WorkloadRegistry(
        [
            WorkloadRegistration(
                workload_id="spiffe://example/kernel",
                service="kernel",
                principal="kernel",
                agent_ids=frozenset({"agent-b"}),
                tenant_ids=frozenset({"tenant-a"}),
                authenticated_instance_ids=frozenset({"instance-1"}),
                lifecycle_kernel=True,
            )
        ]
    )
    identity = WorkloadIdentity(
        workload_id="spiffe://example/kernel",
        service="kernel",
        principal="kernel",
        tenant_id="tenant-a",
        authenticated_at=datetime.now(UTC),
        auth_method=WorkloadAuthMethod.MTLS,
        authenticated_instance_id="instance-1",
    )
    assert registry.authorize(
        identity,
        agent_id="agent-b",
        tenant_id="tenant-a",
        target_instance_id="instance-1",
        lifecycle=True,
    )
    assert not registry.authorize(identity, agent_id="agent-a", tenant_id="tenant-a")
    assert not registry.authorize(identity, agent_id="agent-b", tenant_id="tenant-b")
    assert ExecutionSecurityPolicy(mode="strict").strict


def test_compatibility_runtime_status_is_not_strict() -> None:
    from loop_controller.infra.config_loader import ExecutionSecurityConfig
    from loop_controller.runtime import _security_runtime_status

    config = SimpleNamespace(execution_security=ExecutionSecurityConfig())
    status = _security_runtime_status(config, {})
    assert status.status == "not_strict"
    assert status.mode == "compatibility"


def test_windows_strict_runtime_is_unknown() -> None:
    from loop_controller.infra.config_loader import ExecutionSecurityConfig
    from loop_controller.runtime import _security_runtime_status

    executor = SimpleNamespace(
        security_egress_type="protected_http",
        supports_strict_security=True,
        security_capabilities=frozenset(
            {"execution_receipt_v1", "tenant_secret_no_fallback_v1"}
        ),
    )
    security = ExecutionSecurityConfig(
        mode="strict",
        supported_egress_types=frozenset({"protected_http"}),
        required_security_capabilities=frozenset({"execution_receipt_v1"}),
        supported_security_capabilities=frozenset({"execution_receipt_v1"}),
    )
    with patch("loop_controller.runtime.platform.system", return_value="Windows"):
        status = _security_runtime_status(
            SimpleNamespace(execution_security=security), {"http": executor}
        )
    assert status.status == "unknown"
    assert status.runtime_assurance == "unknown"
    assert "windows_not_strict" in status.reasons


def test_runtime_requires_verified_deployment_proof(tmp_path, monkeypatch) -> None:
    from loop_controller.infra.config_loader import (
        ExecutionSecurityConfig,
        RuntimeObservationConfig,
    )
    from loop_controller.runtime import _security_runtime_status

    monkeypatch.setenv("LC_PROOF_KEY", "proof-key")
    now = datetime.now(UTC)
    proof = DeploymentProof(
        provider="release-ci",
        workload="loop-controller",
        instance="lc-1",
        profile_sha256="a" * 64,
        environment_digest="b" * 64,
        issued_at=now - timedelta(seconds=1),
        expires_at=now + timedelta(minutes=5),
        signature="",
    )
    proof = replace(
        proof,
        signature=hmac.new(b"proof-key", proof.payload(), hashlib.sha256).hexdigest(),
    )
    proof_path = tmp_path / "proof.json"
    proof_path.write_text(
        json.dumps(
            {
                **proof.__dict__,
                "issued_at": proof.issued_at.isoformat(),
                "expires_at": proof.expires_at.isoformat(),
            }
        ),
        encoding="utf-8",
    )
    observation = RuntimeObservationConfig(
        external_attestation_provider="release-ci",
        deployment_proof_path=str(proof_path),
        deployment_proof_key_env="LC_PROOF_KEY",
        workload="loop-controller",
        instance="lc-1",
        profile_sha256="a" * 64,
        environment_digest="b" * 64,
    )
    security = ExecutionSecurityConfig(
        mode="strict",
        required_security_capabilities=frozenset({"deployment_proof_v1"}),
        supported_security_capabilities=frozenset({"deployment_proof_v1"}),
        require_external_deployment_attestation=True,
        runtime_observation=observation,
    )
    config = SimpleNamespace(
        execution_security=security,
        policy_dir=str(tmp_path / "policies"),
    )
    with patch("loop_controller.runtime.platform.system", return_value="Linux"):
        status = _security_runtime_status(config, {})
    assert status.status == "ready"
    assert status.runtime_assurance == "externally_attested"

    proof_path.unlink()
    with patch("loop_controller.runtime.platform.system", return_value="Linux"):
        missing = _security_runtime_status(config, {})
    assert missing.status == "degraded"
    assert missing.runtime_assurance != "externally_attested"
    assert "deployment_proof_missing_or_invalid" in missing.reasons
    assert "configured_capability_not_implemented" in missing.reasons
    assert "required_capability_unavailable" in missing.reasons


def test_require_execution_ready_only_enforces_security_in_strict() -> None:
    from loop_controller.runtime import Runtime, SecurityRuntimeStatus

    runtime = object.__new__(Runtime)
    object.__setattr__(runtime, "persistence_status", SimpleNamespace(status="healthy"))
    object.__setattr__(runtime, "audit_store", object())
    object.__setattr__(runtime, "execution_security_policy", ExecutionSecurityPolicy())
    object.__setattr__(
        runtime,
        "security_status",
        SecurityRuntimeStatus("not_strict", "compatibility", "unknown"),
    )
    runtime.require_execution_ready()

    object.__setattr__(
        runtime, "execution_security_policy", ExecutionSecurityPolicy(mode="strict")
    )
    with pytest.raises(RuntimeError, match="strict execution security"):
        runtime.require_execution_ready()
