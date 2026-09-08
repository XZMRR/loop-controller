"""远程证据锚点的数据模型和收据验证。"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, field_serializer, field_validator, model_validator

from loop_controller.utils.canonical import canonical_json

_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_TIMESTAMP_PATTERN = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6})Z$"
)


class AnchorPayload(BaseModel):
    """已经完成本地提交的 Audit/Evidence 联合链尾。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"] = "1"
    stream_id: str
    audit_seq: int
    audit_hash: str
    evidence_seq: int
    evidence_hash: str
    evidence_algorithm: str
    evidence_key_id: str

    @field_validator("stream_id", "evidence_algorithm", "evidence_key_id")
    @classmethod
    def _non_empty_string(cls, value: str) -> str:
        if not value or value != value.strip():
            raise ValueError("值必须是无首尾空白的非空字符串")
        return value

    @model_validator(mode="after")
    def _validate_tail(self) -> AnchorPayload:
        if self.audit_seq < 0 or self.evidence_seq < 0:
            raise ValueError("锚点序号不能为负数")
        if self.audit_seq != self.evidence_seq:
            raise ValueError("audit_seq 必须等于 evidence_seq")
        if self.audit_seq == 0:
            if self.audit_hash or self.evidence_hash:
                raise ValueError("genesis 锚点的 hash 必须为空")
        elif not _HASH_PATTERN.fullmatch(self.audit_hash) or not _HASH_PATTERN.fullmatch(
            self.evidence_hash
        ):
            raise ValueError("非 genesis 锚点的 hash 必须是 64 位小写十六进制摘要")
        return self


class AnchorReceipt(BaseModel):
    """远程锚点服务签发的 Ed25519 收据。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    receipt_id: str
    payload: AnchorPayload
    anchored_at: datetime
    service_key_id: str
    algorithm: Literal["ed25519"]
    signature: str

    @field_validator("receipt_id", "service_key_id")
    @classmethod
    def _non_empty_string(cls, value: str) -> str:
        if not value or value != value.strip():
            raise ValueError("值必须是无首尾空白的非空字符串")
        return value

    @field_validator("anchored_at", mode="before")
    @classmethod
    def _strict_anchored_at(cls, value: object) -> datetime:
        if not isinstance(value, str):
            raise ValueError("anchored_at 必须是规范 UTC RFC3339 字符串")
        match = _TIMESTAMP_PATTERN.fullmatch(value)
        if match is None:
            raise ValueError("anchored_at 必须使用固定 6 位微秒并以 Z 结尾")
        try:
            parsed = datetime.strptime(match.group("date"), "%Y-%m-%dT%H:%M:%S.%f").replace(
                tzinfo=UTC
            )
        except ValueError as exc:
            raise ValueError("anchored_at 不是有效时间") from exc
        if format_anchor_time(parsed) != value:
            raise ValueError("anchored_at 不是规范 UTC RFC3339 时间")
        return parsed

    @field_serializer("anchored_at")
    def _serialize_anchored_at(self, value: datetime) -> str:
        return format_anchor_time(value)

    def signing_bytes(self) -> bytes:
        return receipt_signing_payload(self)

    @field_validator("signature")
    @classmethod
    def _strict_signature(cls, value: str) -> str:
        try:
            decoded = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("signature 不是有效的标准 Base64") from exc
        if len(decoded) != 64:
            raise ValueError("Ed25519 signature 必须是 64 字节")
        if base64.b64encode(decoded).decode("ascii") != value:
            raise ValueError("signature 必须使用规范的标准 Base64 编码")
        return value


def format_anchor_time(value: datetime) -> str:
    """将 UTC datetime 格式化为收据唯一允许的时间表示。"""
    offset = value.utcoffset()
    if value.tzinfo is None or offset is None or offset.total_seconds() != 0:
        raise ValueError("锚点时间必须是 UTC aware datetime")
    return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def canonical_anchor_payload(payload: AnchorPayload) -> str:
    return canonical_json(payload.model_dump(mode="json"))


def anchor_idempotency_key(payload: AnchorPayload) -> str:
    return hashlib.sha256(canonical_anchor_payload(payload).encode("utf-8")).hexdigest()


def receipt_signing_payload(receipt: AnchorReceipt) -> bytes:
    unsigned = {
        "receipt_id": receipt.receipt_id,
        "payload": receipt.payload.model_dump(mode="json"),
        "anchored_at": format_anchor_time(receipt.anchored_at),
        "service_key_id": receipt.service_key_id,
        "algorithm": receipt.algorithm,
    }
    return canonical_json(unsigned).encode("utf-8")


def verify_anchor_receipt(
    receipt: AnchorReceipt,
    public_key: Ed25519PublicKey | bytes,
    *,
    service_key_id: str,
) -> bool:
    """验证 key ID 和 Ed25519 签名；任何签名不匹配均返回 False。"""
    if receipt.service_key_id != service_key_id:
        return False
    try:
        key = (
            Ed25519PublicKey.from_public_bytes(public_key)
            if isinstance(public_key, bytes)
            else public_key
        )
        signature = base64.b64decode(receipt.signature, validate=True)
        key.verify(signature, receipt_signing_payload(receipt))
    except (ValueError, binascii.Error, InvalidSignature):
        return False
    return True


class AnchorReceiptVerifier:
    """按服务 key ID 选择可信 Ed25519 公钥并验证收据。"""

    def __init__(self, public_keys: Mapping[str, Ed25519PublicKey | bytes]) -> None:
        self._public_keys = dict(public_keys)

    def verify(self, receipt: AnchorReceipt) -> bool:
        public_key = self._public_keys.get(receipt.service_key_id)
        if public_key is None:
            return False
        return verify_anchor_receipt(
            receipt,
            public_key,
            service_key_id=receipt.service_key_id,
        )
