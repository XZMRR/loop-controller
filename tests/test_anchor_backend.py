from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from loop_controller.audit.anchor_backends import AnchorBackendError, HTTPAnchorBackend
from loop_controller.audit.anchors import (
    AnchorPayload,
    AnchorReceipt,
    anchor_idempotency_key,
    receipt_signing_payload,
)
from loop_controller.utils.canonical import canonical_json


def _payload(seq: int = 1) -> AnchorPayload:
    return AnchorPayload(
        stream_id="deployment-01/default 空间",
        audit_seq=seq,
        audit_hash=f"{seq:064x}",
        evidence_seq=seq,
        evidence_hash=f"{seq + 1:064x}",
        evidence_algorithm="ed25519",
        evidence_key_id="evidence-1",
    )


def _receipt(payload: AnchorPayload, private_key: Ed25519PrivateKey) -> dict[str, object]:
    unsigned = AnchorReceipt(
        receipt_id=f"rcpt-{payload.audit_seq}",
        payload=payload,
        anchored_at="2026-08-28T12:00:01.000000Z",
        service_key_id="service-1",
        algorithm="ed25519",
        signature=base64.b64encode(bytes(64)).decode("ascii"),
    )
    signature = private_key.sign(receipt_signing_payload(unsigned))
    return {
        **unsigned.model_dump(mode="json"),
        "anchored_at": datetime(2026, 8, 28, 12, 0, 1, tzinfo=UTC).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        ),
        "signature": base64.b64encode(signature).decode("ascii"),
    }


def _backend(handler, private_key: Ed25519PrivateKey) -> HTTPAnchorBackend:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return HTTPAnchorBackend(
        "https://anchor.example/root",
        bearer_token="secret-token",
        receipt_public_key=private_key.public_key(),
        service_key_id="service-1",
        client=client,
    )


def test_publish_uses_put_encoded_segment_bearer_and_canonical_body() -> None:
    private_key = Ed25519PrivateKey.generate()
    payload = _payload()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert request.url.raw_path == b"/root/v1/anchors/deployment-01%2Fdefault%20%E7%A9%BA%E9%97%B4"
        assert request.headers["Authorization"] == "Bearer secret-token"
        assert request.headers["Accept"] == "application/json"
        assert request.headers["Content-Type"] == "application/json"
        assert request.headers["Idempotency-Key"] == anchor_idempotency_key(payload)
        assert request.content.decode() == canonical_json(payload.model_dump(mode="json"))
        return httpx.Response(200, json=_receipt(payload, private_key))

    backend = _backend(handler, private_key)
    receipt = backend.publish(payload, idempotency_key=anchor_idempotency_key(payload))

    assert receipt.payload == payload


def test_latest_uses_encoded_segment_and_returns_none_for_404() -> None:
    private_key = Ed25519PrivateKey.generate()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.raw_path.endswith(
            b"/deployment-01%2Fdefault%20%E7%A9%BA%E9%97%B4/latest"
        )
        return httpx.Response(404)

    assert _backend(handler, private_key).latest(_payload().stream_id) is None


def test_uncertain_put_is_resolved_by_verified_latest_without_retry() -> None:
    private_key = Ed25519PrivateKey.generate()
    payload = _payload()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if request.method == "PUT":
            raise httpx.ReadTimeout("response lost", request=request)
        return httpx.Response(200, json=_receipt(payload, private_key))

    receipt = _backend(handler, private_key).publish(
        payload, idempotency_key=anchor_idempotency_key(payload)
    )

    assert receipt.payload == payload
    assert calls == ["PUT", "GET"]


def test_uncertain_put_retries_same_key_when_latest_is_older() -> None:
    private_key = Ed25519PrivateKey.generate()
    payload = _payload(2)
    calls: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.headers.get("Idempotency-Key")))
        if len(calls) == 1:
            raise httpx.ConnectTimeout("uncertain", request=request)
        if request.method == "GET":
            return httpx.Response(200, json=_receipt(_payload(1), private_key))
        return httpx.Response(200, json=_receipt(payload, private_key))

    receipt = _backend(handler, private_key).publish(
        payload, idempotency_key=anchor_idempotency_key(payload)
    )

    assert receipt.payload == payload
    assert calls == [
        ("PUT", anchor_idempotency_key(payload)),
        ("GET", None),
        ("PUT", anchor_idempotency_key(payload)),
    ]


def test_invalid_receipt_signature_has_stable_sanitized_error() -> None:
    private_key = Ed25519PrivateKey.generate()
    payload = _payload()
    response = _receipt(payload, private_key)
    response["signature"] = base64.b64encode(b"forged").decode()

    backend = _backend(lambda request: httpx.Response(200, json=response), private_key)

    with pytest.raises(AnchorBackendError) as caught:
        backend.latest(payload.stream_id)
    assert caught.value.code == "anchor_receipt_invalid"
    assert str(caught.value) == "anchor_receipt_invalid"
    assert "forged" not in str(caught.value)


@pytest.mark.parametrize(
    ("status", "body", "code", "retryable"),
    [
        (401, {"secret": "leak"}, "anchor_authentication_failed", False),
        (403, {}, "anchor_authentication_failed", False),
        (409, {"error_code": "anchor_conflict"}, "anchor_conflict", False),
        (
            409,
            {"error_code": "anchor_rollback_rejected"},
            "anchor_rollback_rejected",
            False,
        ),
        (429, {}, "anchor_rate_limited", True),
        (500, {"detail": "internal secret"}, "anchor_unavailable", True),
    ],
)
def test_http_errors_are_stable_and_do_not_include_response_body(
    status: int, body: dict[str, str], code: str, retryable: bool
) -> None:
    private_key = Ed25519PrivateKey.generate()
    backend = _backend(lambda request: httpx.Response(status, json=body), private_key)

    with pytest.raises(AnchorBackendError) as caught:
        backend.latest(_payload().stream_id)

    assert caught.value.code == code
    assert caught.value.retryable is retryable
    assert caught.value.status_code == status
    assert str(caught.value) == code
    assert json.dumps(body) not in str(caught.value)


def test_payload_rejects_extra_fields_and_noncanonical_receipt_timestamp() -> None:
    payload_data = _payload().model_dump()
    with pytest.raises(ValueError):
        AnchorPayload.model_validate({**payload_data, "unexpected": True})

    private_key = Ed25519PrivateKey.generate()
    receipt = _receipt(_payload(), private_key)
    receipt["anchored_at"] = "2026-08-28T12:00:01Z"
    with pytest.raises(ValueError):
        AnchorReceipt.model_validate(receipt)


def test_mtls_pair_and_idempotency_key_are_validated() -> None:
    private_key = Ed25519PrivateKey.generate()
    with pytest.raises(ValueError, match="必须成对配置"):
        HTTPAnchorBackend(
            "https://anchor.example",
            bearer_token="token",
            receipt_public_key=private_key.public_key(),
            service_key_id="service-1",
            client_cert_file="client.pem",
        )

    payload = _payload()
    backend = _backend(lambda request: httpx.Response(500), private_key)
    with pytest.raises(ValueError, match="Idempotency-Key"):
        backend.publish(payload, idempotency_key="wrong")
