from __future__ import annotations

import base64
from pathlib import Path

import pytest

from loop_controller.audit.anchors import AnchorPayload, AnchorReceipt
from loop_controller.audit.evidence import EvidenceChain, HMACEvidenceSigner, SignedEvidence
from loop_controller.audit.evidence_backends import LocalFileEvidenceBackend
from loop_controller.infra.audit_store import JsonlAuditStore
from loop_controller.models import AuditEvent

STREAM_ID = "test-stream"


def _dummy_signature() -> str:
    """返回一个格式合法但内容无意义的 Ed25519 签名占位符。"""
    return base64.b64encode(bytes(64)).decode("ascii")


def _receipt(payload: AnchorPayload, receipt_id: str | None = None) -> AnchorReceipt:
    return AnchorReceipt(
        receipt_id=receipt_id or f"rcpt-{payload.audit_seq}",
        payload=payload,
        anchored_at="2026-08-28T12:00:01.000000Z",
        service_key_id="anchor-svc-1",
        algorithm="ed25519",
        signature=_dummy_signature(),
    )


class _AlwaysValidVerifier:
    """不依赖真实密码学的收据验证器，仅用于状态分支测试。"""

    def verify(self, receipt: AnchorReceipt) -> bool:
        return True


class _MemoryAnchorBackend:
    """最小内存 EvidenceAnchorBackend 实现：可注入 latest 收据并记录 publish 调用。"""

    def __init__(self, latest_receipt: AnchorReceipt | None = None) -> None:
        self._latest = latest_receipt
        self.publishes: list[AnchorPayload] = []

    def latest(self, stream_id: str) -> AnchorReceipt | None:
        return self._latest

    def publish(self, payload: AnchorPayload, *, idempotency_key: str) -> AnchorReceipt:
        self.publishes.append(payload)
        return _receipt(payload)

    def close(self) -> None:
        return None


def _event(number: int) -> AuditEvent:
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


def _open_store(
    tmp_path: Path,
    *,
    anchor_backend: _MemoryAnchorBackend | None = None,
) -> JsonlAuditStore:
    signer = HMACEvidenceSigner(b"test-key", key_id="hmac-1")
    backend = LocalFileEvidenceBackend(tmp_path / "evidence")
    chain = EvidenceChain(backend, signer)
    return JsonlAuditStore(
        tmp_path / "audit.jsonl",
        evidence_chain=chain,
        anchor_backend=anchor_backend,
        anchor_stream_id=STREAM_ID,
        anchor_receipt_verifier=_AlwaysValidVerifier(),  # type: ignore[arg-type]
    )


async def _build_local_chain(tmp_path: Path, count: int) -> JsonlAuditStore:
    """先在没有 anchor_backend 的情况下写入 count 条事件，构造纯本地链。"""
    store = _open_store(tmp_path)
    for i in range(1, count + 1):
        await store.append_async(_event(i))
    return store


async def _collect_evidence(store: JsonlAuditStore) -> list[SignedEvidence]:
    return await store._collect_evidence()


async def _local_payload(store: JsonlAuditStore) -> AnchorPayload:
    evidence = await _collect_evidence(store)
    seq = evidence[-1].seq if evidence else 0
    evidence_hash = evidence[-1].current_hash if evidence else ""
    return store._anchor_payload(seq, evidence_hash)


async def _historical_payload(store: JsonlAuditStore, seq: int) -> AnchorPayload:
    evidence = await _collect_evidence(store)
    evidence_hash = evidence[seq - 1].current_hash if seq else ""
    audit_hash = store._audit_hash_at_seq(seq)
    signer = store._evidence_chain.signer
    return AnchorPayload(
        stream_id=STREAM_ID,
        audit_seq=seq,
        audit_hash=audit_hash,
        evidence_seq=seq,
        evidence_hash=evidence_hash,
        evidence_algorithm=signer.algorithm,
        evidence_key_id=signer.key_id,
    )


@pytest.mark.asyncio
async def test_remote_none_local_empty_is_healthy(tmp_path: Path) -> None:
    """远端无锚点 + 本地空 => healthy, 写入未阻断。"""
    backend = _MemoryAnchorBackend(latest_receipt=None)
    store = _open_store(tmp_path, anchor_backend=backend)

    status = await store.verify_anchor_startup()

    assert status == "healthy"
    assert store.anchor_status == "healthy"
    assert not store.write_blocked
    summary = store.anchor_summary()
    assert summary["anchor_status"] == "healthy"
    assert summary["anchor_stream_id"] == STREAM_ID
    assert summary["anchor_last_success_seq"] == 0
    assert summary["anchor_lag_events"] == 0
    assert summary["anchor_last_error_code"] is None
    assert backend.publishes == []


@pytest.mark.asyncio
async def test_remote_none_local_nonempty_requires_bootstrap(tmp_path: Path) -> None:
    """远端无锚点 + 本地非空 => bootstrap_required, 写入阻断。"""
    await _build_local_chain(tmp_path, 2)
    backend = _MemoryAnchorBackend(latest_receipt=None)
    store = _open_store(tmp_path, anchor_backend=backend)

    status = await store.verify_anchor_startup()

    assert status == "bootstrap_required"
    assert store.anchor_status == "bootstrap_required"
    assert store.write_blocked
    summary = store.anchor_summary()
    assert summary["anchor_status"] == "bootstrap_required"
    assert summary["anchor_lag_events"] == 2
    assert summary["anchor_last_error_code"] is None
    assert backend.publishes == []


@pytest.mark.asyncio
async def test_remote_ahead_detects_rollback(tmp_path: Path) -> None:
    """远程序号 > 本地 => rollback_detected, 写入阻断。"""
    await _build_local_chain(tmp_path, 1)
    remote_payload = AnchorPayload(
        stream_id=STREAM_ID,
        audit_seq=2,
        audit_hash="a" * 64,
        evidence_seq=2,
        evidence_hash="b" * 64,
        evidence_algorithm="hmac-sha256",
        evidence_key_id="hmac-1",
    )
    backend = _MemoryAnchorBackend(latest_receipt=_receipt(remote_payload))
    store = _open_store(tmp_path, anchor_backend=backend)

    status = await store.verify_anchor_startup()

    assert status == "rollback_detected"
    assert store.anchor_status == "rollback_detected"
    assert store.write_blocked
    summary = store.anchor_summary()
    assert summary["anchor_status"] == "rollback_detected"
    assert summary["anchor_lag_events"] == 1
    assert backend.publishes == []


@pytest.mark.asyncio
async def test_remote_same_seq_different_hash_conflicts(tmp_path: Path) -> None:
    """远程序号 == 本地但 hash 不同 => anchor_conflict, 写入阻断。"""
    build_store = await _build_local_chain(tmp_path, 1)
    local_payload = await _local_payload(build_store)
    remote_payload = local_payload.model_copy(
        update={"audit_hash": "c" * 64, "evidence_hash": "d" * 64}
    )
    backend = _MemoryAnchorBackend(latest_receipt=_receipt(remote_payload))
    store = _open_store(tmp_path, anchor_backend=backend)

    status = await store.verify_anchor_startup()

    assert status == "anchor_conflict"
    assert store.anchor_status == "anchor_conflict"
    assert store.write_blocked
    summary = store.anchor_summary()
    assert summary["anchor_status"] == "anchor_conflict"
    assert summary["anchor_lag_events"] == 1
    assert backend.publishes == []


@pytest.mark.asyncio
async def test_remote_behind_history_matches_catches_up(tmp_path: Path) -> None:
    """远程序号 < 本地且历史匹配 => healthy, 自动发布本地尾部。"""
    build_store = await _build_local_chain(tmp_path, 3)
    remote_payload = await _historical_payload(build_store, seq=1)
    backend = _MemoryAnchorBackend(latest_receipt=_receipt(remote_payload))
    store = _open_store(tmp_path, anchor_backend=backend)
    expected_tail = await _local_payload(store)

    status = await store.verify_anchor_startup()

    assert status == "healthy"
    assert store.anchor_status == "healthy"
    assert not store.write_blocked
    summary = store.anchor_summary()
    assert summary["anchor_status"] == "healthy"
    assert summary["anchor_last_success_seq"] == 3
    assert summary["anchor_lag_events"] == 0
    assert summary["anchor_last_error_code"] is None
    assert backend.publishes == [expected_tail]


@pytest.mark.asyncio
async def test_remote_behind_history_mismatch_conflicts(tmp_path: Path) -> None:
    """远程序号 < 本地但历史不匹配 => anchor_conflict, 写入阻断, 不发布。"""
    build_store = await _build_local_chain(tmp_path, 3)
    remote_payload = (await _historical_payload(build_store, seq=1)).model_copy(
        update={"audit_hash": "0" * 64}
    )
    backend = _MemoryAnchorBackend(latest_receipt=_receipt(remote_payload))
    store = _open_store(tmp_path, anchor_backend=backend)

    status = await store.verify_anchor_startup()

    assert status == "anchor_conflict"
    assert store.anchor_status == "anchor_conflict"
    assert store.write_blocked
    summary = store.anchor_summary()
    assert summary["anchor_status"] == "anchor_conflict"
    assert summary["anchor_lag_events"] == 3
    assert backend.publishes == []
