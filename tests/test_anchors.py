from __future__ import annotations

import base64
import hashlib
from typing import Protocol

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from loop_controller.audit.anchor_backends import EvidenceAnchorBackend
from loop_controller.audit.anchors import (
    AnchorPayload,
    AnchorReceipt,
    AnchorReceiptVerifier,
    anchor_idempotency_key,
    canonical_anchor_payload,
    verify_anchor_receipt,
)
from loop_controller.utils.canonical import canonical_json


def _payload(**changes: object) -> AnchorPayload:
    values = {
        "stream_id": "deployment-01/default",
        "audit_seq": 1,
        "audit_hash": "a" * 64,
        "evidence_seq": 1,
        "evidence_hash": "b" * 64,
        "evidence_algorithm": "ed25519",
        "evidence_key_id": "evidence-2026-01",
    }
    values.update(changes)
    return AnchorPayload.model_validate(values)


def _signed_receipt(
    private_key: Ed25519PrivateKey,
    *,
    payload: AnchorPayload | None = None,
    anchored_at: str = "2026-08-28T12:00:01.000000Z",
    service_key_id: str = "anchor-service-01",
) -> AnchorReceipt:
    unsigned = {
        "receipt_id": "rcpt-1",
        "payload": (payload or _payload()).model_dump(mode="json"),
        "anchored_at": anchored_at,
        "service_key_id": service_key_id,
        "algorithm": "ed25519",
    }
    signature = private_key.sign(canonical_json(unsigned).encode("utf-8"))
    return AnchorReceipt.model_validate(
        {**unsigned, "signature": base64.b64encode(signature).decode("ascii")}
    )


def test_payload_canonical_json_and_idempotency_are_stable() -> None:
    first = _payload()
    reordered = AnchorPayload.model_validate(dict(reversed(list(first.model_dump().items()))))

    expected = canonical_json(first.model_dump(mode="json"))
    assert canonical_anchor_payload(first) == expected
    assert canonical_anchor_payload(reordered) == expected
    assert anchor_idempotency_key(first) == hashlib.sha256(expected.encode("utf-8")).hexdigest()
    assert anchor_idempotency_key(reordered) == anchor_idempotency_key(first)


@pytest.mark.parametrize(
    "changes",
    [
        {"stream_id": ""},
        {"stream_id": " deployment-01/default"},
        {"audit_seq": -1, "evidence_seq": -1},
        {"audit_seq": 1, "evidence_seq": 2},
        {"audit_hash": "A" * 64},
        {"evidence_hash": "not-a-hash"},
        {"schema_version": "2"},
    ],
)
def test_payload_rejects_invalid_values(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _payload(**changes)


def test_payload_genesis_and_extra_field_rules() -> None:
    genesis = _payload(audit_seq=0, evidence_seq=0, audit_hash="", evidence_hash="")
    assert genesis.audit_seq == 0
    with pytest.raises(ValidationError):
        _payload(audit_seq=0, evidence_seq=0)
    with pytest.raises(ValidationError):
        AnchorPayload.model_validate({**_payload().model_dump(), "unexpected": True})


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-08-28T12:00:01Z",
        "2026-08-28T12:00:01.000000+00:00",
        "2026-08-28T15:00:01.000000+03:00",
        "2026-08-28 12:00:01.000000Z",
        "2026-02-30T12:00:01.000000Z",
    ],
)
def test_receipt_rejects_noncanonical_time(timestamp: str) -> None:
    key = Ed25519PrivateKey.generate()
    with pytest.raises(ValidationError):
        _signed_receipt(key, anchored_at=timestamp)


@pytest.mark.parametrize(
    "anchored_at",
    [
        "2026-08-28T12:00:01.000000",       # 缺少 Z
        "2026-08-28T12:00:01.000000+00:00",  # UTC 偏移时区
        "2026-08-28T12:00:01.000000+03:00",  # 正偏移时区
        "2026-08-28T12:00:01.000Z",         # 3 位毫秒而非 6 位微秒
        "2026-08-28T12:00:01.000000000Z",    # 9 位纳秒而非 6 位微秒
        "2026-08-28T12:00:01Z",              # 完全无微秒
        "2026-08-28T12:00:01.000000z",       # 小写 z
    ],
)
def test_receipt_rejects_noncanonical_anchored_at_utc_format(anchored_at: str) -> None:
    key = Ed25519PrivateKey.generate()
    with pytest.raises(ValidationError):
        _signed_receipt(key, anchored_at=anchored_at)


def test_receipt_round_trip_preserves_canonical_time() -> None:
    receipt = _signed_receipt(Ed25519PrivateKey.generate())
    assert receipt.model_dump(mode="json")["anchored_at"] == "2026-08-28T12:00:01.000000Z"


@pytest.mark.parametrize(
    "signature",
    ["not base64!", "YQ==", base64.urlsafe_b64encode(b"x" * 64).decode("ascii").rstrip("=")],
)
def test_receipt_requires_canonical_base64_ed25519_signature(signature: str) -> None:
    receipt = _signed_receipt(Ed25519PrivateKey.generate()).model_dump(mode="json")
    receipt["signature"] = signature
    with pytest.raises(ValidationError):
        AnchorReceipt.model_validate(receipt)


def test_ed25519_receipt_verification_and_key_selection() -> None:
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    raw_public_key = public_key.public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    receipt = _signed_receipt(private_key)

    assert verify_anchor_receipt(receipt, public_key, service_key_id="anchor-service-01")
    assert verify_anchor_receipt(receipt, raw_public_key, service_key_id="anchor-service-01")
    assert not verify_anchor_receipt(receipt, public_key, service_key_id="unknown")
    assert AnchorReceiptVerifier({"anchor-service-01": public_key}).verify(receipt)
    assert not AnchorReceiptVerifier({"other": public_key}).verify(receipt)


@pytest.mark.parametrize(
    "field,value",
    [
        ("stream_id", "other/default"),
        ("audit_seq", 2),
        ("audit_hash", "c" * 64),
        ("evidence_hash", "d" * 64),
        ("evidence_algorithm", "hmac-sha256"),
        ("evidence_key_id", "other-key"),
    ],
)
def test_tampered_payload_fails_receipt_verification(field: str, value: object) -> None:
    private_key = Ed25519PrivateKey.generate()
    receipt = _signed_receipt(private_key)
    data = receipt.model_dump(mode="json")
    payload = dict(data["payload"])
    payload[field] = value
    if field == "audit_seq":
        payload["evidence_seq"] = value
    data["payload"] = payload
    tampered = AnchorReceipt.model_validate(data)

    assert not verify_anchor_receipt(
        tampered,
        private_key.public_key(),
        service_key_id="anchor-service-01",
    )


class _Backend:
    def publish(self, payload: AnchorPayload, *, idempotency_key: str) -> AnchorReceipt:
        raise NotImplementedError

    def latest(self, stream_id: str) -> AnchorReceipt | None:
        return None

    def close(self) -> None:
        return None


def test_evidence_anchor_backend_is_runtime_checkable_protocol() -> None:
    backend: EvidenceAnchorBackend = _Backend()
    assert isinstance(backend, EvidenceAnchorBackend)
    assert issubclass(EvidenceAnchorBackend, Protocol)
