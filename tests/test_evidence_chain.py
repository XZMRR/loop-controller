from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from loop_controller.audit.evidence import (
    Ed25519EvidenceSigner,
    EvidenceChain,
    HMACEvidenceSigner,
)
from loop_controller.audit.evidence_backends import LocalFileEvidenceBackend
from loop_controller.infra.alert_store import InMemoryAlertStore
from loop_controller.infra.audit_store import JsonlAuditStore
from loop_controller.models import AuditEvent
from loop_controller.runtime import Runtime, _build_evidence_chain


def _evidence_config(tmp_path: Path, evidence_config: dict) -> SimpleNamespace:
    return SimpleNamespace(
        evidence_config=evidence_config,
        audit_key_id="default",
        policy_dir=str(tmp_path / "policies"),
    )


def _event(number: int = 1) -> AuditEvent:
    return AuditEvent(
        event_id=f"event-{number}",
        trace_id="trace-1",
        session_id="session-1",
        actor_type="agent",
        actor_id="agent-1",
        action="execute",
        target="web_search",
        reason=f"event {number}",
    )


async def _records(backend: LocalFileEvidenceBackend):
    return [record async for record in backend.iter_evidence(None)]


@pytest.mark.parametrize(
    "evidence_config",
    [
        {},
        {"evidence": {"backend": "local", "signing": {"algorithm": "ed25519"}}},
        {"evidence": {"enabled": False, "signing": {"algorithm": "ed25519"}}},
    ],
)
def test_evidence_missing_or_not_enabled_does_not_require_key(
    tmp_path: Path, monkeypatch, evidence_config: dict
) -> None:
    monkeypatch.delenv("LOOP_CONTROLLER_EVIDENCE_PRIVATE_KEY", raising=False)

    assert _build_evidence_chain(_evidence_config(tmp_path, evidence_config)) is None


def test_enabled_evidence_without_private_key_fails(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("LOOP_CONTROLLER_EVIDENCE_PRIVATE_KEY", raising=False)
    config = _evidence_config(
        tmp_path,
        {"evidence": {"enabled": True, "signing": {"algorithm": "ed25519"}}},
    )

    with pytest.raises(ValueError, match="LOOP_CONTROLLER_EVIDENCE_PRIVATE_KEY 未配置"):
        _build_evidence_chain(config)


@pytest.mark.asyncio
async def test_hmac_signature_chain_verifies(tmp_path: Path) -> None:
    backend = LocalFileEvidenceBackend(tmp_path / "evidence")
    chain = EvidenceChain(backend, HMACEvidenceSigner(b"test-key", key_id="hmac-1"))

    first = await chain.append(_event(1))
    second = await chain.append(_event(2))

    assert first.seq == 1
    assert second.seq == 2
    assert second.prev_hash == first.current_hash
    assert await chain.verify()


@pytest.mark.asyncio
async def test_ed25519_signature_chain_verifies(tmp_path: Path, monkeypatch) -> None:
    private_key = Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    monkeypatch.setenv("TEST_EVIDENCE_PRIVATE_KEY", base64.b64encode(private_key).decode())
    signer = Ed25519EvidenceSigner.from_environment(
        key_id="ed25519-1", variable="TEST_EVIDENCE_PRIVATE_KEY"
    )
    chain = EvidenceChain(LocalFileEvidenceBackend(tmp_path / "evidence"), signer)

    await chain.append(_event())

    assert await chain.verify()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("seq", 9),
        ("prev_hash", "forged"),
        ("current_hash", "0" * 64),
        ("signature", base64.b64encode(b"forged").decode()),
        ("key_id", "other-key"),
        ("algorithm", "ed25519"),
    ],
)
async def test_tampering_with_evidence_field_fails_verification(
    tmp_path: Path, field: str, replacement: object
) -> None:
    backend = LocalFileEvidenceBackend(tmp_path / "evidence")
    chain = EvidenceChain(backend, HMACEvidenceSigner(b"test-key", key_id="hmac-1"))
    await chain.append(_event())
    path = tmp_path / "evidence" / "default.jsonl"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload[field] = replacement
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    assert not await chain.verify()


@pytest.mark.asyncio
async def test_event_and_timestamp_tampering_fail_verification(tmp_path: Path) -> None:
    backend = LocalFileEvidenceBackend(tmp_path / "evidence")
    signer = HMACEvidenceSigner(b"test-key", key_id="hmac-1")
    chain = EvidenceChain(backend, signer)
    await chain.append(_event())
    path = tmp_path / "evidence" / "default.jsonl"
    original = json.loads(path.read_text(encoding="utf-8"))

    modified = dict(original)
    modified["event"] = dict(modified["event"], actor_id="attacker")
    path.write_text(json.dumps(modified) + "\n", encoding="utf-8")
    assert not await EvidenceChain(backend, signer).verify()

    modified = dict(original)
    modified["timestamp"] = "2000-01-01T00:00:00+00:00"
    path.write_text(json.dumps(modified) + "\n", encoding="utf-8")
    assert not await EvidenceChain(backend, signer).verify()


@pytest.mark.asyncio
async def test_restart_recovers_tail_and_continues_chain(tmp_path: Path) -> None:
    backend = LocalFileEvidenceBackend(tmp_path / "evidence")
    signer = HMACEvidenceSigner(b"test-key", key_id="hmac-1")
    first = await EvidenceChain(backend, signer).append(_event(1))

    restarted = EvidenceChain(LocalFileEvidenceBackend(tmp_path / "evidence"), signer)
    second = await restarted.append(_event(2))

    assert second.seq == first.seq + 1
    assert second.prev_hash == first.current_hash
    assert await restarted.verify()


def test_audit_append_synchronously_generates_evidence(tmp_path: Path) -> None:
    backend = LocalFileEvidenceBackend(tmp_path / "evidence")
    chain = EvidenceChain(backend, HMACEvidenceSigner(b"test-key", key_id="hmac-1"))
    store = JsonlAuditStore(tmp_path / "audit.jsonl", evidence_chain=chain)

    store.append(_event())

    assert (tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip()
    evidence = (tmp_path / "evidence" / "default.jsonl").read_text(encoding="utf-8")
    assert json.loads(evidence)["event"]["seq"] == 1


def test_evidence_failure_alerts_before_preserving_audit_event(tmp_path: Path) -> None:
    calls: list[str] = []

    class FailingChain:
        async def append(self, event: AuditEvent) -> None:
            calls.append("evidence")
            assert not (tmp_path / "audit.jsonl").exists()
            raise RuntimeError("signing failed")

    alert_store = InMemoryAlertStore()
    store = JsonlAuditStore(
        tmp_path / "audit.jsonl",
        evidence_chain=FailingChain(),  # type: ignore[arg-type]
        alert_store=alert_store,
    )

    store.append(_event())
    calls.append("audit")

    assert calls == ["evidence", "audit"]
    assert len(store.query_by_trace("trace-1")) == 1
    alert = alert_store.list_alerts()[0]
    assert alert.rule_id == "evidence_chain_append_failed"
    assert alert.evidence == ["event-1"]


@pytest.mark.asyncio
async def test_runtime_start_verifies_evidence_and_alerts_without_blocking(tmp_path: Path) -> None:
    class InvalidChain:
        async def verify(self) -> bool:
            return False

    alert_store = InMemoryAlertStore()
    audit_store = JsonlAuditStore(
        tmp_path / "audit.jsonl",
        evidence_chain=InvalidChain(),  # type: ignore[arg-type]
        alert_store=alert_store,
    )
    gateway = Mock()
    gateway.start = AsyncMock()
    runtime = Runtime(
        classifier=Mock(),
        checkpoint=Mock(),
        gateway=gateway,
        approval_manager=Mock(),
        audit_store=audit_store,
        masker=Mock(),
        profiles={},
        session_manager=Mock(),
        risk_manager=Mock(),
        conversation_store=Mock(),
    )

    await runtime.start()

    gateway.start.assert_awaited_once()
    alert = alert_store.list_alerts()[0]
    assert alert.rule_id == "evidence_chain_verification_failed"
    assert alert.severity == "critical"
