"""证据锚点后端接口与 HTTP 实现。"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable
from urllib.parse import quote

if TYPE_CHECKING:
    from loop_controller.infra.alert_store import AlertStore

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import ValidationError

from loop_controller.audit.anchors import (
    AnchorPayload,
    AnchorReceipt,
    anchor_idempotency_key,
    canonical_anchor_payload,
    verify_anchor_receipt,
)
from loop_controller.models import AuditAlert

ANCHOR_ALERT_RULE_IDS = frozenset(
    {
        "trusted_anchor_publish_failed",
        "trusted_anchor_unavailable",
        "trusted_anchor_verification_failed",
        "trusted_anchor_receipt_invalid",
        "trusted_anchor_bootstrap_required",
        "trusted_anchor_rollback_detected",
        "trusted_anchor_conflict",
    }
)

_ANCHOR_ALERT_RULES = {
    "anchor_receipt_invalid": "trusted_anchor_receipt_invalid",
    "anchor_rollback_rejected": "trusted_anchor_rollback_detected",
    "anchor_conflict": "trusted_anchor_conflict",
    "anchor_unavailable": "trusted_anchor_unavailable",
}


@dataclass(frozen=True)
class HTTPAnchorConfig:
    stream_id: str
    base_url: str
    token: str
    public_key: Ed25519PublicKey | bytes
    service_key_id: str
    connect_timeout_seconds: float = 1.0
    request_timeout_seconds: float = 3.0
    verify: bool = True
    ca_file: str | None = None
    client_cert_file: str | None = None
    client_key_file: str | None = None
    unavailable_policy: str = "degrade"
    conflict_policy: str = "block_writes"


class AnchorBackendError(RuntimeError):
    """不暴露响应正文与认证信息的稳定后端错误。"""

    def __init__(self, code: str, *, retryable: bool, status_code: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.status_code = status_code


@runtime_checkable
class EvidenceAnchorBackend(Protocol):
    """同步、有序的远程证据锚点后端。"""

    def publish(
        self,
        payload: AnchorPayload,
        *,
        idempotency_key: str,
    ) -> AnchorReceipt: ...

    def latest(self, stream_id: str) -> AnchorReceipt | None: ...

    def close(self) -> None: ...


class HTTPAnchorBackend:
    """实现 v0.28 PUT/latest 契约的同步 HTTP 客户端。"""

    def __init__(
        self,
        base_url: str | HTTPAnchorConfig,
        *,
        bearer_token: str | None = None,
        receipt_public_key: Ed25519PublicKey | bytes | None = None,
        service_key_id: str | None = None,
        connect_timeout_seconds: float = 1.0,
        request_timeout_seconds: float = 3.0,
        verify: bool | str = True,
        client_cert_file: str | None = None,
        client_key_file: str | None = None,
        client: httpx.Client | None = None,
        alert_store: AlertStore | None = None,
    ) -> None:
        self.config: HTTPAnchorConfig | None
        configured_stream_id: str | None
        if isinstance(base_url, HTTPAnchorConfig):
            config = base_url
            self.config = config
            configured_stream_id = config.stream_id
            base_url = config.base_url
            bearer_token = config.token
            receipt_public_key = config.public_key
            service_key_id = config.service_key_id
            connect_timeout_seconds = config.connect_timeout_seconds
            request_timeout_seconds = config.request_timeout_seconds
            verify = config.ca_file or config.verify
            client_cert_file = config.client_cert_file
            client_key_file = config.client_key_file
            self.unavailable_policy = config.unavailable_policy
            self.conflict_policy = config.conflict_policy
        else:
            self.config = None
            configured_stream_id = None
            self.unavailable_policy = "degrade"
            self.conflict_policy = "block_writes"
        self.stream_id = configured_stream_id
        self.status = "healthy"
        self.last_success_seq = 0
        self.local_seq = 0
        self.last_error_code: str | None = None
        self._alert_store = alert_store
        if not bearer_token:
            raise ValueError("Anchor Bearer token 不能为空")
        if not service_key_id:
            raise ValueError("Anchor service_key_id 不能为空")
        if receipt_public_key is None:
            raise ValueError("Anchor receipt public key 不能为空")
        if connect_timeout_seconds <= 0 or request_timeout_seconds <= 0:
            raise ValueError("Anchor timeout 必须大于零")
        if bool(client_cert_file) != bool(client_key_file):
            raise ValueError("Anchor mTLS 证书和私钥必须成对配置")
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {bearer_token}", "Accept": "application/json"}
        self._public_key = receipt_public_key
        self._service_key_id = service_key_id
        cert = (
            (client_cert_file, client_key_file)
            if client_cert_file is not None and client_key_file is not None
            else None
        )
        self._owns_client = client is None
        self._client = client or httpx.Client(
            verify=verify,
            cert=cert,
            timeout=httpx.Timeout(request_timeout_seconds, connect=connect_timeout_seconds),
        )

    @staticmethod
    def _stream_segment(stream_id: str) -> str:
        if not stream_id:
            raise ValueError("Anchor stream_id 不能为空")
        return quote(stream_id, safe="")

    def _url(self, stream_id: str, *, latest: bool = False) -> str:
        suffix = "/latest" if latest else ""
        return f"{self._base_url}/v1/anchors/{self._stream_segment(stream_id)}{suffix}"

    def _parse_receipt(
        self, response: httpx.Response, *, expected_stream_id: str
    ) -> AnchorReceipt:
        try:
            receipt = AnchorReceipt.model_validate(response.json())
        except (json.JSONDecodeError, ValidationError) as exc:
            raise AnchorBackendError("anchor_receipt_invalid", retryable=False) from exc
        if not verify_anchor_receipt(
            receipt, self._public_key, service_key_id=self._service_key_id
        ):
            raise AnchorBackendError("anchor_receipt_invalid", retryable=False)
        if receipt.payload.stream_id != expected_stream_id:
            raise AnchorBackendError("anchor_conflict", retryable=False)
        return receipt

    @staticmethod
    def _http_error(response: httpx.Response) -> AnchorBackendError:
        status = response.status_code
        if status == 409:
            code = "anchor_conflict"
            try:
                body = response.json()
                remote_code = body.get("error_code") if isinstance(body, dict) else None
            except json.JSONDecodeError:
                remote_code = None
            if remote_code == "anchor_rollback_rejected":
                code = "anchor_rollback_rejected"
            return AnchorBackendError(code, retryable=False, status_code=status)
        if status in {401, 403}:
            return AnchorBackendError("anchor_authentication_failed", retryable=False, status_code=status)
        if status == 429:
            return AnchorBackendError("anchor_rate_limited", retryable=True, status_code=status)
        if status >= 500:
            return AnchorBackendError("anchor_unavailable", retryable=True, status_code=status)
        return AnchorBackendError("anchor_http_error", retryable=False, status_code=status)

    @staticmethod
    def _transport_error(exc: httpx.HTTPError) -> AnchorBackendError:
        code = "anchor_timeout" if isinstance(exc, httpx.TimeoutException) else "anchor_unavailable"
        return AnchorBackendError(code, retryable=True)

    def latest(self, stream_id: str) -> AnchorReceipt | None:
        try:
            response = self._client.get(self._url(stream_id, latest=True), headers=self._headers)
        except httpx.HTTPError as exc:
            raise self._transport_error(exc) from exc
        if response.status_code == 404:
            return None
        if not response.is_success:
            raise self._http_error(response)
        return self._parse_receipt(response, expected_stream_id=stream_id)

    def _put(self, payload: AnchorPayload, *, idempotency_key: str) -> AnchorReceipt:
        headers = {
            **self._headers,
            "Content-Type": "application/json",
            "Idempotency-Key": idempotency_key,
        }
        response = self._client.put(
            self._url(payload.stream_id),
            headers=headers,
            content=canonical_anchor_payload(payload).encode("utf-8"),
        )
        if not response.is_success:
            raise self._http_error(response)
        receipt = self._parse_receipt(response, expected_stream_id=payload.stream_id)
        if receipt.payload != payload:
            raise AnchorBackendError("anchor_conflict", retryable=False)
        return receipt

    def publish(self, payload: AnchorPayload, *, idempotency_key: str) -> AnchorReceipt:
        if idempotency_key != anchor_idempotency_key(payload):
            raise ValueError("Anchor Idempotency-Key 与 payload 不匹配")
        self.local_seq = payload.audit_seq
        started = time.monotonic()
        try:
            receipt = self._publish_resolving_uncertainty(payload, idempotency_key=idempotency_key)
        except AnchorBackendError as exc:
            self._record_failure(exc, payload, time.monotonic() - started)
            raise
        self.status = "healthy"
        self.last_success_seq = receipt.payload.audit_seq
        self.last_error_code = None
        self._observe_publish("success", None, time.monotonic() - started)
        self._set_metrics()
        return receipt

    def _publish_resolving_uncertainty(
        self, payload: AnchorPayload, *, idempotency_key: str
    ) -> AnchorReceipt:
        try:
            return self._put(payload, idempotency_key=idempotency_key)
        except httpx.HTTPError as first_error:
            try:
                remote = self.latest(payload.stream_id)
            except AnchorBackendError:
                raise self._transport_error(first_error) from first_error
            if remote is not None:
                if remote.payload == payload:
                    return remote
                if remote.payload.audit_seq >= payload.audit_seq:
                    raise AnchorBackendError("anchor_conflict", retryable=False) from first_error
            try:
                return self._put(payload, idempotency_key=idempotency_key)
            except httpx.HTTPError as retry_error:
                try:
                    remote = self.latest(payload.stream_id)
                except AnchorBackendError:
                    raise self._transport_error(retry_error) from retry_error
                if remote is not None and remote.payload == payload:
                    return remote
                raise self._transport_error(retry_error) from retry_error

    @property
    def lag_events(self) -> int:
        return max(0, self.local_seq - self.last_success_seq)

    def sanitized_status(self) -> dict[str, object]:
        return {
            "anchor_status": self.status,
            "anchor_stream_id": self.stream_id,
            "anchor_last_success_seq": self.last_success_seq,
            "anchor_lag_events": self.lag_events,
            "anchor_last_error_code": self.last_error_code,
        }

    def _record_failure(
        self, error: AnchorBackendError, payload: AnchorPayload, duration: float
    ) -> None:
        self.last_error_code = error.code
        if error.code in {"anchor_conflict", "anchor_rollback_rejected", "anchor_receipt_invalid"}:
            self.status = "anchor_conflict"
        else:
            self.status = "degraded"
        self._observe_publish("error", error.code, duration)
        self._set_metrics(conflict=self.status == "anchor_conflict")
        if self._alert_store is None:
            return
        rule_id = _ANCHOR_ALERT_RULES.get(error.code, "trusted_anchor_publish_failed")
        self._alert_store.save_alert(
            AuditAlert(
                alert_id=uuid.uuid4().hex,
                session_id=payload.stream_id,
                rule_id=rule_id,
                severity="critical" if self.status == "anchor_conflict" else "high",
                title=rule_id,
                description=(
                    f"error_code={error.code}; exception_type={type(error).__name__}; "
                    f"stream_id={payload.stream_id}; local_seq={payload.audit_seq}; "
                    f"remote_seq={self.last_success_seq}"
                ),
            )
        )

    @staticmethod
    def _observe_publish(status: str, error_code: str | None, duration: float) -> None:
        try:
            from loop_controller.metrics import observe_anchor_publish
        except ImportError:
            return
        observe_anchor_publish(status, error_code, duration)

    def _set_metrics(self, *, conflict: bool = False) -> None:
        try:
            from loop_controller.metrics import observe_anchor_conflict, set_anchor_state
        except ImportError:
            return
        set_anchor_state(self.status, self.last_success_seq, self.lag_events)
        if conflict:
            observe_anchor_conflict()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


__all__ = ["AnchorBackendError", "EvidenceAnchorBackend", "HTTPAnchorBackend", "HTTPAnchorConfig"]
