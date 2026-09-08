"""签名证据链。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast, runtime_checkable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import BaseModel, ConfigDict

from loop_controller.infra.durable_io import durable_atomic_replace
from loop_controller.models import AuditEvent
from loop_controller.utils.canonical import canonical_json

GENESIS_HASH = ""


class SignedEvidence(BaseModel):
    """一条带哈希链接和签名的审计证据。"""

    model_config = ConfigDict(frozen=True)

    seq: int
    timestamp: str
    tenant_id: str | None = None
    event: AuditEvent
    prev_hash: str
    current_hash: str
    algorithm: str
    key_id: str
    signature: str


class EvidenceSigner(Protocol):
    @property
    def algorithm(self) -> str: ...

    @property
    def key_id(self) -> str: ...

    def sign(self, data: bytes) -> bytes: ...

    def verify(self, data: bytes, signature: bytes) -> bool: ...


class HMACEvidenceSigner:
    algorithm = "hmac-sha256"

    def __init__(self, key: bytes, *, key_id: str) -> None:
        if not key:
            raise ValueError("HMAC 签名密钥不能为空")
        if not key_id:
            raise ValueError("key_id 不能为空")
        self._key = key
        self.key_id = key_id

    def sign(self, data: bytes) -> bytes:
        return hmac.new(self._key, data, hashlib.sha256).digest()

    def verify(self, data: bytes, signature: bytes) -> bool:
        return hmac.compare_digest(self.sign(data), signature)


class Ed25519EvidenceSigner:
    algorithm = "ed25519"

    def __init__(self, private_key: bytes, *, key_id: str) -> None:
        if not key_id:
            raise ValueError("key_id 不能为空")
        self._private_key = Ed25519PrivateKey.from_private_bytes(private_key)
        self._public_key = self._private_key.public_key()
        self.key_id = key_id

    @classmethod
    def from_environment(
        cls,
        *,
        key_id: str,
        variable: str = "LOOP_CONTROLLER_EVIDENCE_PRIVATE_KEY",
    ) -> Ed25519EvidenceSigner:
        encoded = os.environ.get(variable)
        if not encoded:
            raise ValueError(f"环境变量 {variable} 未配置")
        try:
            private_key = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise ValueError(f"环境变量 {variable} 不是有效的 base64 私钥") from exc
        return cls(private_key, key_id=key_id)

    def sign(self, data: bytes) -> bytes:
        return self._private_key.sign(data)

    def verify(self, data: bytes, signature: bytes) -> bool:
        try:
            self._public_key.verify(signature, data)
        except InvalidSignature:
            return False
        return True


@runtime_checkable
class EvidenceBackend(Protocol):
    async def append(self, tenant_id: str | None, signed_evidence: SignedEvidence) -> None: ...

    async def tail_state(self, tenant_id: str | None) -> tuple[int, str] | None: ...

    def iter_evidence(self, tenant_id: str | None) -> AsyncIterator[SignedEvidence]: ...


def _hash_payload(
    *,
    seq: int,
    timestamp: str,
    tenant_id: str | None,
    event: AuditEvent,
    prev_hash: str,
    algorithm: str,
    key_id: str,
) -> str:
    payload = {
        "seq": seq,
        "timestamp": timestamp,
        "tenant_id": tenant_id,
        "event": event.model_dump(mode="json"),
        "prev_hash": prev_hash,
        "algorithm": algorithm,
        "key_id": key_id,
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


class EvidenceChain:
    """按租户维护序号和前序哈希的签名证据链。"""

    def __init__(
        self,
        backend: EvidenceBackend,
        signer: EvidenceSigner,
        *,
        seq_start: int = 0,
        checkpoint_path: str | Path | None = None,
    ) -> None:
        self._backend = backend
        self._signer = signer
        self._seq_start = seq_start
        self._checkpoint_path = Path(checkpoint_path) if checkpoint_path is not None else None
        self._seq_by_tenant: dict[str | None, int] = {}
        self._prev_hash_by_tenant: dict[str | None, str] = {}
        self._locks: dict[str | None, asyncio.Lock] = {}
        self.status = "healthy"
        self.degraded_reason: str | None = None

    @property
    def signer(self) -> EvidenceSigner:
        return self._signer

    def mark_degraded(self, reason: str) -> None:
        self.status = "degraded"
        self.degraded_reason = reason

    def checkpoint_path(self, tenant_id: str | None = None) -> Path | None:
        if self._checkpoint_path is None:
            return None
        if tenant_id is None:
            return self._checkpoint_path
        return self._checkpoint_path.with_name(
            f"{self._checkpoint_path.stem}-{tenant_id}{self._checkpoint_path.suffix}"
        )

    def write_checkpoint(self, payload: dict[str, Any], tenant_id: str | None = None) -> None:
        path = self.checkpoint_path(tenant_id)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            signed_payload = dict(payload)
            signed_payload.update(
                {
                    "updated_at": datetime.now(UTC).isoformat(),
                    "algorithm": self._signer.algorithm,
                    "key_id": self._signer.key_id,
                }
            )
            signature = self._signer.sign(canonical_json(signed_payload).encode("utf-8"))
            record = {**signed_payload, "signature": base64.b64encode(signature).decode("ascii")}
            durable_atomic_replace(
                path,
                (canonical_json(record) + "\n").encode("utf-8"),
            )
        except Exception as exc:
            self.mark_degraded(f"checkpoint write failed: {type(exc).__name__}")
            raise

    def read_checkpoint(self, tenant_id: str | None = None) -> dict[str, Any] | None:
        path = self.checkpoint_path(tenant_id)
        if path is None or not path.exists():
            return None
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("证据 checkpoint 必须是 JSON 对象")
        record: dict[str, Any] = loaded
        signature_text = record.pop("signature", "")
        if record.get("algorithm") != self._signer.algorithm or record.get("key_id") != self._signer.key_id:
            raise ValueError("证据 checkpoint 签名算法或 key_id 不匹配")
        try:
            signature = base64.b64decode(signature_text, validate=True)
        except ValueError as exc:
            raise ValueError("证据 checkpoint 签名格式无效") from exc
        if not self._signer.verify(canonical_json(record).encode("utf-8"), signature):
            raise ValueError("证据 checkpoint 签名验证失败")
        return record

    async def _recover(self, tenant_id: str | None) -> None:
        if tenant_id in self._seq_by_tenant:
            return
        state = await self._backend.tail_state(tenant_id)
        if state is None:
            state = (self._seq_start, GENESIS_HASH)
        self._seq_by_tenant[tenant_id], self._prev_hash_by_tenant[tenant_id] = state

    async def append(
        self,
        event: AuditEvent,
        *,
        tenant_id: str | None = None,
    ) -> SignedEvidence:
        lock = self._locks.setdefault(tenant_id, asyncio.Lock())
        async with lock:
            try:
                def build(state: tuple[int, str] | None) -> SignedEvidence:
                    if state is None:
                        state = (self._seq_start, GENESIS_HASH)
                    seq = state[0] + 1
                    prev_hash = state[1]
                    timestamp = datetime.now(UTC).isoformat()
                    current_hash = _hash_payload(
                        seq=seq,
                        timestamp=timestamp,
                        tenant_id=tenant_id,
                        event=event,
                        prev_hash=prev_hash,
                        algorithm=self._signer.algorithm,
                        key_id=self._signer.key_id,
                    )
                    signature = base64.b64encode(
                        self._signer.sign(current_hash.encode("ascii"))
                    ).decode("ascii")
                    return SignedEvidence(
                        seq=seq,
                        timestamp=timestamp,
                        tenant_id=tenant_id,
                        event=event,
                        prev_hash=prev_hash,
                        current_hash=current_hash,
                        algorithm=self._signer.algorithm,
                        key_id=self._signer.key_id,
                        signature=signature,
                    )

                append_from_tail = getattr(self._backend, "append_from_tail", None)
                append_impl = getattr(getattr(self._backend, "append", None), "__func__", None)
                if (
                    append_from_tail is not None
                    and getattr(append_impl, "__qualname__", "")
                    == "LocalFileEvidenceBackend.append"
                ):
                    evidence = cast(SignedEvidence, await append_from_tail(tenant_id, build))
                else:
                    await self._recover(tenant_id)
                    evidence = build(
                        (
                            self._seq_by_tenant[tenant_id],
                            self._prev_hash_by_tenant[tenant_id],
                        )
                    )
                    await self._backend.append(tenant_id, evidence)
                seq = evidence.seq
                current_hash = evidence.current_hash
            except Exception as exc:
                self.mark_degraded(f"evidence append failed: {type(exc).__name__}")
                raise
            self._seq_by_tenant[tenant_id] = seq
            self._prev_hash_by_tenant[tenant_id] = current_hash
            return evidence

    async def verify(self, tenant_id: str | None = None) -> bool:
        expected_seq = self._seq_start + 1
        expected_prev = GENESIS_HASH
        async for evidence in self._backend.iter_evidence(tenant_id):
            if evidence.seq != expected_seq or evidence.prev_hash != expected_prev:
                return False
            if evidence.tenant_id != tenant_id:
                return False
            if evidence.algorithm != self._signer.algorithm or evidence.key_id != self._signer.key_id:
                return False
            current_hash = _hash_payload(
                seq=evidence.seq,
                timestamp=evidence.timestamp,
                tenant_id=evidence.tenant_id,
                event=evidence.event,
                prev_hash=evidence.prev_hash,
                algorithm=evidence.algorithm,
                key_id=evidence.key_id,
            )
            if not hmac.compare_digest(evidence.current_hash, current_hash):
                return False
            try:
                signature = base64.b64decode(evidence.signature, validate=True)
            except ValueError:
                return False
            if not self._signer.verify(current_hash.encode("ascii"), signature):
                return False
            expected_seq += 1
            expected_prev = current_hash
        return True
