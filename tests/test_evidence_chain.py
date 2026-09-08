from __future__ import annotations

import asyncio
import base64
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from loop_controller.audit.anchor_backends import AnchorBackendError
from loop_controller.audit.anchors import AnchorReceipt, AnchorReceiptVerifier
from loop_controller.audit.evidence import (
    Ed25519EvidenceSigner,
    EvidenceChain,
    HMACEvidenceSigner,
)
from loop_controller.audit.evidence_backends import LocalFileEvidenceBackend
from loop_controller.infra.alert_store import InMemoryAlertStore
from loop_controller.infra.audit_store import JsonlAuditStore
from loop_controller.models import AuditEvent
from loop_controller.runtime import Runtime, _build_evidence_anchor, _build_evidence_chain
from loop_controller.utils.canonical import canonical_json


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


class _RecordingAnchor:
    def __init__(self, key: Ed25519PrivateKey) -> None:
        self.key = key
        self.payloads = []
        self.failures: list[AnchorBackendError] = []

    def publish(self, payload, *, idempotency_key):
        self.payloads.append(payload)
        if self.failures:
            raise self.failures.pop(0)
        unsigned = {
            "receipt_id": f"receipt-{payload.audit_seq}",
            "payload": payload.model_dump(mode="json"),
            "anchored_at": "2026-08-28T12:00:01.000000Z",
            "service_key_id": "service-1",
            "algorithm": "ed25519",
        }
        signature = self.key.sign(canonical_json(unsigned).encode("utf-8"))
        return AnchorReceipt.model_validate(
            {**unsigned, "signature": base64.b64encode(signature).decode("ascii")}
        )

    def latest(self, stream_id):
        return None

    def close(self):
        return None


def _anchored_store(tmp_path: Path, anchor: _RecordingAnchor, *, alert_store=None):
    chain = EvidenceChain(
        LocalFileEvidenceBackend(tmp_path / "evidence"),
        HMACEvidenceSigner(b"test-key", key_id="hmac-1"),
        checkpoint_path=tmp_path / "checkpoint.json",
    )
    return JsonlAuditStore(
        tmp_path / "audit.jsonl",
        evidence_chain=chain,
        alert_store=alert_store,
        anchor_backend=anchor,
        anchor_stream_id="deployment/default",
        anchor_receipt_verifier=AnchorReceiptVerifier({"service-1": anchor.key.public_key()}),
    )


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


def test_build_evidence_anchor_connects_validated_config(tmp_path: Path, monkeypatch) -> None:
    public_key_path = tmp_path / "anchor.pub"
    public_key_path.write_bytes(
        Ed25519PrivateKey.generate().public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    monkeypatch.setenv("TEST_ANCHOR_TOKEN", "secret-token")
    config = _evidence_config(
        tmp_path,
        {
            "evidence": {
                "enabled": True,
                "anchor": {
                    "enabled": True,
                    "stream_id": "deployment/default",
                    "base_url": "https://anchor.example",
                    "connect_timeout_seconds": 1,
                    "request_timeout_seconds": 3,
                    "auth": {"token_env": "TEST_ANCHOR_TOKEN"},
                    "tls": {"verify": True},
                    "receipt": {
                        "service_key_id": "service-1",
                        "public_key_file": str(public_key_path),
                    },
                    "startup": {
                        "unavailable_policy": "degrade",
                        "conflict_policy": "block_writes",
                    },
                },
            }
        },
    )

    backend = _build_evidence_anchor(config)

    assert backend is not None
    assert backend.config.stream_id == "deployment/default"
    assert backend.config.token == "secret-token"
    backend.close()


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


@pytest.mark.asyncio
async def test_disabled_evidence_still_verifies_audit_chain(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    store = JsonlAuditStore(path)
    await store.append_async(_event(1))
    content = path.read_text(encoding="utf-8")
    path.write_text(content.replace('"seq":1', '"seq":2'), encoding="utf-8")

    assert not await store.verify_evidence_chain()
    assert store.write_blocked


def test_audit_append_synchronously_generates_evidence(tmp_path: Path) -> None:
    backend = LocalFileEvidenceBackend(tmp_path / "evidence")
    chain = EvidenceChain(backend, HMACEvidenceSigner(b"test-key", key_id="hmac-1"))
    store = JsonlAuditStore(tmp_path / "audit.jsonl", evidence_chain=chain)

    store.append(_event())

    assert (tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip()
    evidence = (tmp_path / "evidence" / "default.jsonl").read_text(encoding="utf-8")
    assert json.loads(evidence)["event"]["seq"] == 1


@pytest.mark.asyncio
async def test_sync_append_rejected_in_running_event_loop(tmp_path: Path) -> None:
    store = JsonlAuditStore(tmp_path / "audit.jsonl")

    with pytest.raises(RuntimeError, match=r"await append_async\(\)"):
        store.append(_event())

    assert not (tmp_path / "audit.jsonl").exists()


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


def test_anchor_publishes_after_checkpoint_and_seal_uses_same_path(tmp_path: Path, monkeypatch) -> None:
    anchor = _RecordingAnchor(Ed25519PrivateKey.generate())
    store = _anchored_store(tmp_path, anchor)
    original_publish = anchor.publish

    def assert_checkpoint_then_publish(payload, *, idempotency_key):
        checkpoint = store._evidence_chain.read_checkpoint()
        assert checkpoint["audit_seq"] == payload.audit_seq
        assert checkpoint["audit_hash"] == payload.audit_hash
        assert checkpoint["evidence_hash"] == payload.evidence_hash
        return original_publish(payload, idempotency_key=idempotency_key)

    monkeypatch.setattr(anchor, "publish", assert_checkpoint_then_publish)
    store.append(_event(1))
    store.seal()

    assert [payload.audit_seq for payload in anchor.payloads] == [1, 2]
    assert store.anchor_status == "healthy"
    assert store.anchor_last_success_seq == 2


def test_anchor_network_failure_retries_latest_tail_on_next_commit(tmp_path: Path) -> None:
    anchor = _RecordingAnchor(Ed25519PrivateKey.generate())
    anchor.failures.append(AnchorBackendError("anchor_unavailable", retryable=True))
    store = _anchored_store(tmp_path, anchor)

    store.append(_event(1))
    assert store.anchor_status == "degraded"
    assert store.anchor_last_success_seq == 0
    assert not store.write_blocked

    store.append(_event(2))
    assert [payload.audit_seq for payload in anchor.payloads] == [1, 2]
    assert store.anchor_status == "healthy"
    assert store.anchor_last_success_seq == 2


def test_anchor_conflict_blocks_following_writes_and_alerts(tmp_path: Path) -> None:
    anchor = _RecordingAnchor(Ed25519PrivateKey.generate())
    anchor.failures.append(AnchorBackendError("anchor_conflict", retryable=False))
    alerts = InMemoryAlertStore()
    store = _anchored_store(tmp_path, anchor, alert_store=alerts)

    store.append(_event(1))

    assert store.anchor_status == "anchor_conflict"
    assert store.write_blocked
    assert alerts.list_alerts()[0].rule_id == "trusted_anchor_conflict"
    with pytest.raises(RuntimeError, match="已阻断"):
        store.append(_event(2))


@pytest.mark.asyncio
async def test_runtime_evidence_failure_degrades_and_success_does_not_recover(
    tmp_path: Path,
) -> None:
    class FailsOnceBackend(LocalFileEvidenceBackend):
        def __init__(self, path: Path) -> None:
            super().__init__(path)
            self.fail = True

        async def append(self, tenant_id, signed_evidence) -> None:
            if self.fail:
                self.fail = False
                raise OSError("evidence unavailable")
            await super().append(tenant_id, signed_evidence)

    alert_store = InMemoryAlertStore()
    backend = FailsOnceBackend(tmp_path / "evidence")
    chain = EvidenceChain(backend, HMACEvidenceSigner(b"test-key", key_id="hmac-1"))
    store = JsonlAuditStore(
        tmp_path / "audit.jsonl", evidence_chain=chain, alert_store=alert_store
    )

    await store.append_async(_event(1))

    assert store.evidence_status == "degraded"
    assert len(store.query_by_trace("trace-1")) == 1
    alert = alert_store.list_alerts()[0]
    assert alert.rule_id == "evidence_chain_append_failed"
    assert alert.severity == "critical"

    with pytest.raises(RuntimeError, match="已阻断"):
        await store.append_async(_event(2))

    assert store.evidence_status == "degraded"
    assert len(store.query_by_trace("trace-1")) == 1
    assert len(await _records(backend)) == 0


@pytest.mark.asyncio
async def test_success_after_evidence_failure_does_not_overwrite_checkpoint(tmp_path: Path) -> None:
    class FailsOnceBackend(LocalFileEvidenceBackend):
        failed = False

        async def append(self, tenant_id, signed_evidence) -> None:
            if not self.failed:
                self.failed = True
                raise OSError("evidence unavailable")
            await super().append(tenant_id, signed_evidence)

    checkpoint_path = tmp_path / "checkpoint.json"
    backend = FailsOnceBackend(tmp_path / "evidence")
    chain = EvidenceChain(
        backend,
        HMACEvidenceSigner(b"test-key", key_id="hmac-1"),
        checkpoint_path=checkpoint_path,
    )
    store = JsonlAuditStore(tmp_path / "audit.jsonl", evidence_chain=chain)

    await store.append_async(_event(1))
    with pytest.raises(RuntimeError, match="已阻断"):
        await store.append_async(_event(2))

    assert store.evidence_status == "degraded"
    assert not checkpoint_path.exists()
    assert len(await _records(backend)) == 0
    assert [event.seq async for event in store.iter_events()] == [1]


@pytest.mark.asyncio
async def test_checkpoint_failure_degrades_alerts_and_does_not_propagate(
    tmp_path: Path, monkeypatch
) -> None:
    backend = LocalFileEvidenceBackend(tmp_path / "evidence")
    alert_store = InMemoryAlertStore()
    chain = EvidenceChain(
        backend,
        HMACEvidenceSigner(b"test-key", key_id="hmac-1"),
        checkpoint_path=tmp_path / "checkpoint.json",
    )
    store = JsonlAuditStore(
        tmp_path / "audit.jsonl", evidence_chain=chain, alert_store=alert_store
    )

    def fail_replace(source, destination) -> None:
        raise OSError("checkpoint replace failed")

    monkeypatch.setattr("loop_controller.audit.evidence.os.replace", fail_replace)

    await store.append_async(_event())

    assert len(store.query_by_trace("trace-1")) == 1
    assert len(await _records(backend)) == 1
    assert store.evidence_status == "degraded"
    alert = alert_store.list_alerts()[0]
    assert alert.rule_id == "evidence_checkpoint_write_failed"
    assert alert.severity == "critical"
    assert alert.evidence == ["event-1"]


@pytest.mark.asyncio
async def test_degraded_chain_does_not_advance_checkpoint_on_later_success(tmp_path: Path) -> None:
    checkpoint_path = tmp_path / "checkpoint.json"
    backend = LocalFileEvidenceBackend(tmp_path / "evidence")
    chain = EvidenceChain(
        backend,
        HMACEvidenceSigner(b"test-key", key_id="hmac-1"),
        checkpoint_path=checkpoint_path,
    )
    store = JsonlAuditStore(tmp_path / "audit.jsonl", evidence_chain=chain)
    await store.append_async(_event(1))
    checkpoint = checkpoint_path.read_text(encoding="utf-8")
    chain.mark_degraded("prior failure")

    await store.append_async(_event(2))

    assert checkpoint_path.read_text(encoding="utf-8") == checkpoint
    assert json.loads(checkpoint)["audit_seq"] == 1
    assert len(await _records(backend)) == 2


@pytest.mark.asyncio
async def test_audit_failure_after_evidence_commit_degrades_alerts_and_blocks(
    tmp_path: Path, monkeypatch
) -> None:
    backend = LocalFileEvidenceBackend(tmp_path / "evidence")
    alert_store = InMemoryAlertStore()
    chain = EvidenceChain(backend, HMACEvidenceSigner(b"test-key", key_id="hmac-1"))
    store = JsonlAuditStore(
        tmp_path / "audit.jsonl", evidence_chain=chain, alert_store=alert_store
    )

    monkeypatch.setattr(store, "_write_audit_line", Mock(side_effect=OSError("secret-token")))

    with pytest.raises(RuntimeError, match="^审计记录写入失败$") as exc_info:
        await store.append_async(_event(1))

    assert exc_info.value.__cause__ is None
    assert "secret-token" not in str(exc_info.value)
    assert store.evidence_status == "degraded"
    assert len(await _records(backend)) == 1
    assert store.query_by_trace("trace-1") == []
    alert = alert_store.list_alerts()[0]
    assert alert.rule_id == "audit_write_failed_after_evidence_commit"
    assert alert.severity == "critical"
    assert "secret-token" not in alert.description

    with pytest.raises(RuntimeError, match="已阻断"):
        await store.append_async(_event(2))
    assert len(await _records(backend)) == 1


@pytest.mark.asyncio
async def test_restart_verification_failure_keeps_writes_blocked(tmp_path: Path, monkeypatch) -> None:
    evidence_path = tmp_path / "evidence"
    audit_path = tmp_path / "audit.jsonl"
    signer = HMACEvidenceSigner(b"test-key", key_id="hmac-1")
    backend = LocalFileEvidenceBackend(evidence_path)
    store = JsonlAuditStore(audit_path, evidence_chain=EvidenceChain(backend, signer))
    monkeypatch.setattr(store, "_write_audit_line", Mock(side_effect=OSError("disk failed")))

    with pytest.raises(RuntimeError, match="^审计记录写入失败$"):
        await store.append_async(_event(1))

    restarted = JsonlAuditStore(
        audit_path,
        evidence_chain=EvidenceChain(LocalFileEvidenceBackend(evidence_path), signer),
    )
    assert not await restarted.verify_evidence_chain()

    with pytest.raises(RuntimeError, match="已阻断"):
        await restarted.append_async(_event(2))
    assert len(await _records(backend)) == 1
    assert restarted.query_by_trace("trace-1") == []


@pytest.mark.asyncio
async def test_evidence_append_failure_does_not_advance_checkpoint(tmp_path: Path) -> None:
    class ToggleBackend(LocalFileEvidenceBackend):
        fail = False

        async def append(self, tenant_id, signed_evidence) -> None:
            if self.fail:
                raise OSError("secret-evidence-error")
            await super().append(tenant_id, signed_evidence)

    checkpoint_path = tmp_path / "checkpoint.json"
    backend = ToggleBackend(tmp_path / "evidence")
    chain = EvidenceChain(
        backend,
        HMACEvidenceSigner(b"test-key", key_id="hmac-1"),
        checkpoint_path=checkpoint_path,
    )
    store = JsonlAuditStore(tmp_path / "audit.jsonl", evidence_chain=chain)
    await store.append_async(_event(1))
    checkpoint = checkpoint_path.read_text(encoding="utf-8")

    backend.fail = True
    await store.append_async(_event(2))

    assert checkpoint_path.read_text(encoding="utf-8") == checkpoint
    assert json.loads(checkpoint)["audit_seq"] == 1
    assert len(await _records(backend)) == 1
    assert [event.seq async for event in store.iter_events()] == [1, 2]


@pytest.mark.asyncio
async def test_evidence_failures_redact_exception_details(
    tmp_path: Path, caplog, monkeypatch
) -> None:
    secret = "secret-token-from-exception"
    alert_store = InMemoryAlertStore()
    chain = EvidenceChain(
        LocalFileEvidenceBackend(tmp_path / "evidence"),
        HMACEvidenceSigner(b"test-key", key_id="hmac-1"),
    )
    store = JsonlAuditStore(
        tmp_path / "audit.jsonl", evidence_chain=chain, alert_store=alert_store
    )
    monkeypatch.setattr(chain, "verify", AsyncMock(side_effect=OSError(secret)))

    assert not await store.verify_evidence_chain()

    alert = alert_store.list_alerts()[0]
    assert secret not in alert.description
    assert secret not in (chain.degraded_reason or "")
    assert secret not in caplog.text
    assert "OSError" in alert.description


@pytest.mark.asyncio
async def test_lagging_checkpoint_validates_history_and_rebuilds(tmp_path: Path) -> None:
    backend = LocalFileEvidenceBackend(tmp_path / "evidence")
    checkpoint_path = tmp_path / "checkpoint.json"
    signer = HMACEvidenceSigner(b"test-key", key_id="hmac-1")
    chain = EvidenceChain(backend, signer, checkpoint_path=checkpoint_path)
    store = JsonlAuditStore(tmp_path / "audit.jsonl", evidence_chain=chain)
    await store.append_async(_event(1))
    historical_checkpoint = checkpoint_path.read_text(encoding="utf-8")
    await store.append_async(_event(2))
    checkpoint_path.write_text(historical_checkpoint, encoding="utf-8")

    restarted = JsonlAuditStore(
        tmp_path / "audit.jsonl",
        evidence_chain=EvidenceChain(backend, signer, checkpoint_path=checkpoint_path),
    )

    assert await restarted.verify_evidence_chain()
    assert restarted.evidence_status == "healthy"
    assert json.loads(checkpoint_path.read_text(encoding="utf-8"))["audit_seq"] == 2


@pytest.mark.asyncio
async def test_lagging_checkpoint_with_wrong_historical_anchor_stays_degraded(
    tmp_path: Path,
) -> None:
    backend = LocalFileEvidenceBackend(tmp_path / "evidence")
    checkpoint_path = tmp_path / "checkpoint.json"
    signer = HMACEvidenceSigner(b"test-key", key_id="hmac-1")
    chain = EvidenceChain(backend, signer, checkpoint_path=checkpoint_path)
    store = JsonlAuditStore(tmp_path / "audit.jsonl", evidence_chain=chain)
    await store.append_async(_event(1))
    first_evidence = (await _records(backend))[0]
    chain.write_checkpoint(
        {
            "audit_seq": 1,
            "audit_hash": "wrong",
            "evidence_seq": 1,
            "evidence_hash": first_evidence.current_hash,
        }
    )
    bad_checkpoint = checkpoint_path.read_text(encoding="utf-8")
    await store.append_async(_event(2))
    checkpoint_path.write_text(bad_checkpoint, encoding="utf-8")

    assert not await store.verify_evidence_chain()
    assert store.evidence_status == "degraded"
    assert checkpoint_path.read_text(encoding="utf-8") == bad_checkpoint


@pytest.mark.asyncio
async def test_lagging_checkpoint_rebuild_failure_stays_degraded(
    tmp_path: Path, monkeypatch
) -> None:
    backend = LocalFileEvidenceBackend(tmp_path / "evidence")
    checkpoint_path = tmp_path / "checkpoint.json"
    signer = HMACEvidenceSigner(b"test-key", key_id="hmac-1")
    chain = EvidenceChain(backend, signer, checkpoint_path=checkpoint_path)
    store = JsonlAuditStore(tmp_path / "audit.jsonl", evidence_chain=chain)
    await store.append_async(_event(1))
    historical_checkpoint = checkpoint_path.read_text(encoding="utf-8")
    await store.append_async(_event(2))
    checkpoint_path.write_text(historical_checkpoint, encoding="utf-8")
    monkeypatch.setattr(chain, "write_checkpoint", Mock(side_effect=OSError("replace failed")))

    assert not await store.verify_evidence_chain()
    assert store.evidence_status == "degraded"
    assert json.loads(checkpoint_path.read_text(encoding="utf-8"))["audit_seq"] == 1


@pytest.mark.asyncio
async def test_async_concurrent_append_is_ordered_and_checkpointed(tmp_path: Path) -> None:
    backend = LocalFileEvidenceBackend(tmp_path / "evidence")
    chain = EvidenceChain(
        backend,
        HMACEvidenceSigner(b"test-key", key_id="hmac-1"),
        checkpoint_path=tmp_path / "checkpoint.json",
    )
    store = JsonlAuditStore(tmp_path / "audit.jsonl", evidence_chain=chain)

    await asyncio.gather(*(store.append_async(_event(number)) for number in range(1, 51)))

    events = [event async for event in store.iter_events()]
    evidence = await _records(backend)
    assert [event.seq for event in events] == list(range(1, 51))
    assert [record.seq for record in evidence] == list(range(1, 51))
    assert store.verify_chain()
    assert await store.verify_evidence_chain()
    assert json.loads((tmp_path / "checkpoint.json").read_text(encoding="utf-8"))["audit_seq"] == 50


@pytest.mark.asyncio
async def test_sync_and_async_concurrent_append_share_one_ordered_writer(tmp_path: Path) -> None:
    backend = LocalFileEvidenceBackend(tmp_path / "evidence")
    chain = EvidenceChain(
        backend,
        HMACEvidenceSigner(b"test-key", key_id="hmac-1"),
        checkpoint_path=tmp_path / "checkpoint.json",
    )
    store = JsonlAuditStore(tmp_path / "audit.jsonl", evidence_chain=chain)

    sync_writes = [asyncio.to_thread(store.append, _event(number)) for number in range(1, 51)]
    async_writes = [store.append_async(_event(number)) for number in range(51, 101)]
    await asyncio.gather(*sync_writes, *async_writes)

    events = [event async for event in store.iter_events()]
    evidence = await _records(backend)
    assert [event.seq for event in events] == list(range(1, 101))
    assert [record.seq for record in evidence] == list(range(1, 101))
    assert [(event.event_id, event.seq) for event in events] == [
        (record.event.event_id, record.event.seq) for record in evidence
    ]
    assert store.verify_chain()
    assert await store.verify_evidence_chain()
    assert json.loads((tmp_path / "checkpoint.json").read_text(encoding="utf-8"))["audit_seq"] == 100


@pytest.mark.asyncio
async def test_blocking_evidence_file_io_does_not_block_heartbeat(
    tmp_path: Path, monkeypatch
) -> None:
    original = LocalFileEvidenceBackend._append_line

    def slow_append(path: Path, line: str) -> None:
        time.sleep(0.05)
        original(path, line)

    monkeypatch.setattr(LocalFileEvidenceBackend, "_append_line", staticmethod(slow_append))
    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        for _ in range(5):
            await asyncio.sleep(0.01)
            ticks += 1

    chain = EvidenceChain(
        LocalFileEvidenceBackend(tmp_path / "evidence"), HMACEvidenceSigner(b"k", key_id="k")
    )
    store = JsonlAuditStore(tmp_path / "audit.jsonl", evidence_chain=chain)
    await asyncio.gather(store.append_async(_event()), heartbeat())
    assert ticks == 5


@pytest.mark.asyncio
async def test_audit_writer_does_not_starve_default_executor(tmp_path: Path, monkeypatch) -> None:
    store = JsonlAuditStore(tmp_path / "audit.jsonl")
    original = store._write_audit_line

    def slow_write(line: str) -> None:
        time.sleep(0.05)
        original(line)

    first_write_started = threading.Event()

    def slow_write_with_signal(line: str) -> None:
        first_write_started.set()
        slow_write(line)

    monkeypatch.setattr(store, "_write_audit_line", slow_write_with_signal)
    loop = asyncio.get_running_loop()
    default_executor = loop._default_executor
    isolated_default = ThreadPoolExecutor(max_workers=1)
    loop.set_default_executor(isolated_default)
    try:
        writes = [asyncio.create_task(store.append_async(_event(number))) for number in range(1, 6)]
        assert await asyncio.to_thread(first_write_started.wait, 1)
        started = time.perf_counter()
        await asyncio.to_thread(lambda: None)
        elapsed = time.perf_counter() - started
        await asyncio.gather(*writes)
    finally:
        loop._default_executor = default_executor
        isolated_default.shutdown(wait=True)

    assert elapsed < 0.1


@pytest.mark.asyncio
async def test_restart_recovers_large_tail_with_one_scan(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "evidence"
    signer = HMACEvidenceSigner(b"test-key", key_id="hmac-1")
    chain = EvidenceChain(LocalFileEvidenceBackend(path), signer)
    last = None
    for number in range(1, 101):
        last = await chain.append(_event(number))
    assert last is not None

    backend = LocalFileEvidenceBackend(path)
    original = backend._read_records_sync
    scans = 0

    def counted_read(file_path: Path):
        nonlocal scans
        scans += 1
        return original(file_path)

    monkeypatch.setattr(backend, "_read_records_sync", counted_read)
    recovered = await EvidenceChain(backend, signer).append(_event(101))

    assert scans == 1
    assert recovered.seq == 101
    assert recovered.prev_hash == last.current_hash


@pytest.mark.asyncio
@pytest.mark.parametrize(("audit_kept", "evidence_kept"), [(True, False), (False, True)])
async def test_cross_validation_detects_one_sided_loss(
    tmp_path: Path, audit_kept: bool, evidence_kept: bool
) -> None:
    backend = LocalFileEvidenceBackend(tmp_path / "evidence")
    chain = EvidenceChain(
        backend,
        HMACEvidenceSigner(b"test-key", key_id="hmac-1"),
        checkpoint_path=tmp_path / "checkpoint.json",
    )
    store = JsonlAuditStore(tmp_path / "audit.jsonl", evidence_chain=chain)
    await store.append_async(_event(1))
    await store.append_async(_event(2))
    if not audit_kept:
        (tmp_path / "audit.jsonl").write_text(
            (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()[0] + "\n",
            encoding="utf-8",
        )
    if not evidence_kept:
        (tmp_path / "evidence" / "default.jsonl").write_text(
            (tmp_path / "evidence" / "default.jsonl").read_text(encoding="utf-8").splitlines()[0] + "\n",
            encoding="utf-8",
        )

    restarted = JsonlAuditStore(tmp_path / "audit.jsonl", evidence_chain=EvidenceChain(
        LocalFileEvidenceBackend(tmp_path / "evidence"),
        HMACEvidenceSigner(b"test-key", key_id="hmac-1"),
        checkpoint_path=tmp_path / "checkpoint.json",
    ))
    assert not await restarted.verify_evidence_chain()
    assert restarted.evidence_status == "degraded"


@pytest.mark.asyncio
async def test_missing_checkpoint_with_data_is_degraded(tmp_path: Path) -> None:
    chain = EvidenceChain(
        LocalFileEvidenceBackend(tmp_path / "evidence"),
        HMACEvidenceSigner(b"test-key", key_id="hmac-1"),
        checkpoint_path=tmp_path / "checkpoint.json",
    )
    store = JsonlAuditStore(tmp_path / "audit.jsonl", evidence_chain=chain)
    await store.append_async(_event())
    (tmp_path / "checkpoint.json").unlink()

    assert not await store.verify_evidence_chain()
    assert store.evidence_status == "degraded"


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

    gateway.start.assert_not_awaited()
    assert audit_store.write_blocked
    alert = alert_store.list_alerts()[0]
    assert alert.rule_id == "evidence_chain_verification_failed"
    assert alert.severity == "critical"
