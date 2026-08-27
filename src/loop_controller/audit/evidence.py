"""签名证据链。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import BaseModel, ConfigDict

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

    async def last_hash(self, tenant_id: str | None) -> str | None: ...

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
    ) -> None:
        self._backend = backend
        self._signer = signer
        self._seq_start = seq_start
        self._seq_by_tenant: dict[str | None, int] = {}
        self._prev_hash_by_tenant: dict[str | None, str] = {}
        self._locks: dict[str | None, asyncio.Lock] = {}

    async def _recover(self, tenant_id: str | None) -> None:
        if tenant_id in self._seq_by_tenant:
            return
        seq = self._seq_start
        async for evidence in self._backend.iter_evidence(tenant_id):
            seq = evidence.seq
        self._seq_by_tenant[tenant_id] = seq
        self._prev_hash_by_tenant[tenant_id] = await self._backend.last_hash(tenant_id) or GENESIS_HASH

    async def append(
        self,
        event: AuditEvent,
        *,
        tenant_id: str | None = None,
    ) -> SignedEvidence:
        lock = self._locks.setdefault(tenant_id, asyncio.Lock())
        async with lock:
            await self._recover(tenant_id)
            seq = self._seq_by_tenant[tenant_id] + 1
            prev_hash = self._prev_hash_by_tenant[tenant_id]
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
            signature = base64.b64encode(self._signer.sign(current_hash.encode("ascii"))).decode("ascii")
            evidence = SignedEvidence(
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
            await self._backend.append(tenant_id, evidence)
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
