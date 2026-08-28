"""v0.28 非生产 SQLite Anchor Service 契约测试。"""

from __future__ import annotations

import base64
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from examples.contrib.anchor.anchor_service import AnchorPayload, AnchorService, create_app

_TOKEN = "test-anchor-token"
_STREAM = "deployment-01/default"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _payload(seq: int, marker: str = "a") -> dict[str, Any]:
    digest = marker * 64
    return {
        "schema_version": "1",
        "stream_id": _STREAM,
        "audit_seq": seq,
        "audit_hash": digest if seq else "",
        "evidence_seq": seq,
        "evidence_hash": digest if seq else "",
        "evidence_algorithm": "ed25519",
        "evidence_key_id": "evidence-key-01",
    }


def _key(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode()).hexdigest()


@pytest.fixture
def private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


@pytest.fixture
def database(tmp_path: Path) -> Path:
    return tmp_path / "anchors.sqlite3"


def _service(database: Path, private_key: Ed25519PrivateKey, **kwargs: Any) -> AnchorService:
    return AnchorService(
        database,
        bearer_token=_TOKEN,
        private_key=private_key,
        service_key_id="anchor-service-01",
        **kwargs,
    )


async def _request(
    service: AnchorService,
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    token: str | None = _TOKEN,
    idempotency_key: str | None = None,
    content: bytes | None = None,
) -> httpx.Response:
    headers: dict[str, str] = {}
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    if idempotency_key is not None:
        headers["idempotency-key"] = idempotency_key
    transport = httpx.ASGITransport(app=create_app(service))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        if content is not None:
            headers["content-type"] = "application/json"
            return await client.request(method, path, headers=headers, content=content)
        return await client.request(method, path, headers=headers, json=payload)


@pytest.mark.asyncio
async def test_bearer_auth_and_latest_not_found(
    database: Path, private_key: Ed25519PrivateKey
) -> None:
    service = _service(database, private_key)
    missing = await _request(service, "GET", "/v1/anchors/deployment-01/default/latest", token=None)
    wrong = await _request(
        service, "GET", "/v1/anchors/deployment-01/default/latest", token="wrong"
    )
    absent = await _request(service, "GET", "/v1/anchors/deployment-01/default/latest")
    assert (missing.status_code, missing.json()["error_code"]) == (401, "anchor_auth_required")
    assert (wrong.status_code, wrong.json()["error_code"]) == (403, "anchor_auth_failed")
    assert (absent.status_code, absent.json()["error_code"]) == (404, "anchor_not_found")


@pytest.mark.asyncio
async def test_payload_body_and_idempotency_validation(
    database: Path, private_key: Ed25519PrivateKey
) -> None:
    service = _service(database, private_key, max_body_bytes=512)
    payload = _payload(1)
    extra = {**payload, "unexpected": "rejected"}
    invalid_payload = await _request(
        service,
        "PUT",
        "/v1/anchors/deployment-01/default",
        payload=extra,
        idempotency_key=_key(extra),
    )
    mismatched_stream = await _request(
        service,
        "PUT",
        "/v1/anchors/other",
        payload=payload,
        idempotency_key=_key(payload),
    )
    invalid_key = await _request(
        service,
        "PUT",
        "/v1/anchors/deployment-01/default",
        payload=payload,
        idempotency_key="0" * 64,
    )
    too_large = await _request(
        service,
        "PUT",
        "/v1/anchors/deployment-01/default",
        content=b"{" + b" " * 512 + b"}",
        idempotency_key="0" * 64,
    )
    assert invalid_payload.json()["error_code"] == "anchor_invalid_payload"
    assert mismatched_stream.json()["error_code"] == "anchor_stream_mismatch"
    assert invalid_key.json()["error_code"] == "anchor_invalid_idempotency_key"
    assert (too_large.status_code, too_large.json()["error_code"]) == (
        413,
        "anchor_request_too_large",
    )


@pytest.mark.asyncio
async def test_monotonic_cas_idempotency_signature_and_restart(
    database: Path, private_key: Ed25519PrivateKey
) -> None:
    service = _service(database, private_key)
    first_payload = _payload(1)
    first = await _request(
        service,
        "PUT",
        "/v1/anchors/deployment-01/default",
        payload=first_payload,
        idempotency_key=_key(first_payload),
    )
    retry = await _request(
        service,
        "PUT",
        "/v1/anchors/deployment-01/default",
        payload=first_payload,
        idempotency_key=_key(first_payload),
    )
    assert first.status_code == 201
    assert retry.status_code == 200
    assert retry.json() == first.json()

    receipt = first.json()
    signature = base64.b64decode(receipt.pop("signature"), validate=True)
    private_key.public_key().verify(signature, _canonical_json(receipt).encode())
    with pytest.raises(InvalidSignature):
        private_key.public_key().verify(
            signature, _canonical_json({**receipt, "receipt_id": "x"}).encode()
        )

    conflict_payload = _payload(1, "b")
    conflict = await _request(
        service,
        "PUT",
        "/v1/anchors/deployment-01/default",
        payload=conflict_payload,
        idempotency_key=_key(conflict_payload),
    )
    newer_payload = _payload(2, "c")
    newer = await _request(
        service,
        "PUT",
        "/v1/anchors/deployment-01/default",
        payload=newer_payload,
        idempotency_key=_key(newer_payload),
    )
    rollback = await _request(
        service,
        "PUT",
        "/v1/anchors/deployment-01/default",
        payload=first_payload,
        idempotency_key=_key(first_payload),
    )
    assert conflict.json()["error_code"] == "anchor_conflict"
    assert newer.status_code == 201
    assert rollback.status_code == 200  # 原始幂等请求仍返回其持久化 receipt。

    restarted = _service(database, private_key)
    latest = await _request(restarted, "GET", "/v1/anchors/deployment-01/default/latest")
    assert latest.json() == newer.json()
    older_with_new_key_payload = _payload(0)
    rejected = await _request(
        restarted,
        "PUT",
        "/v1/anchors/deployment-01/default",
        payload=older_with_new_key_payload,
        idempotency_key=_key(older_with_new_key_payload),
    )
    assert rejected.json()["error_code"] == "anchor_rollback_rejected"


def test_concurrent_cas_has_single_valid_winner(
    database: Path, private_key: Ed25519PrivateKey
) -> None:
    service = _service(database, private_key)
    left = AnchorPayload.model_validate(_payload(1, "a"))
    right = AnchorPayload.model_validate(_payload(1, "b"))

    def publish(payload: AnchorPayload) -> tuple[dict[str, Any], int]:
        dumped = payload.model_dump(mode="json")
        return service.publish(payload, _key(dumped))

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(publish, (left, right)))
    statuses = sorted(status for _, status in results)
    assert statuses == [201, 409]
    assert sum(body.get("error_code") == "anchor_conflict" for body, _ in results) == 1
    latest = service.latest(_STREAM)
    assert latest is not None
    assert latest["payload"] in (_payload(1, "a"), _payload(1, "b"))
