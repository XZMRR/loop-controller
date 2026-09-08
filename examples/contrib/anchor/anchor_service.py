"""非生产 SQLite Anchor Service 参考实现（v0.28 契约）。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
import urllib.parse
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, model_validator
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

_MAX_BODY_BYTES = 16 * 1024
_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_IDEMPOTENCY_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class AnchorPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal["1"] = "1"
    stream_id: str = Field(min_length=1, max_length=256)
    audit_seq: StrictInt = Field(ge=0)
    audit_hash: str
    evidence_seq: StrictInt = Field(ge=0)
    evidence_hash: str
    evidence_algorithm: str = Field(min_length=1, max_length=64)
    evidence_key_id: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def validate_contract(self) -> AnchorPayload:
        if self.stream_id != self.stream_id.strip():
            raise ValueError("stream_id 格式无效")
        if ".." in self.stream_id or "\\" in self.stream_id or "\x00" in self.stream_id:
            raise ValueError("stream_id 格式无效")
        if self.audit_seq != self.evidence_seq:
            raise ValueError("audit_seq 与 evidence_seq 必须一致")
        if self.audit_seq == 0:
            if self.audit_hash or self.evidence_hash:
                raise ValueError("genesis hash 必须为空")
        elif not _HASH_PATTERN.fullmatch(self.audit_hash) or not _HASH_PATTERN.fullmatch(
            self.evidence_hash
        ):
            raise ValueError("非 genesis hash 必须是 64 位小写十六进制")
        return self


class AnchorService:
    def __init__(
        self,
        database_path: str | Path,
        *,
        bearer_token: str,
        private_key: Ed25519PrivateKey,
        service_key_id: str,
        max_body_bytes: int = _MAX_BODY_BYTES,
    ) -> None:
        if not bearer_token:
            raise ValueError("bearer_token 不能为空")
        if not service_key_id:
            raise ValueError("service_key_id 不能为空")
        if max_body_bytes < 1:
            raise ValueError("max_body_bytes 必须为正数")
        self.database_path = str(database_path)
        self._bearer_token = bearer_token
        self._private_key = private_key
        self._service_key_id = service_key_id
        self._max_body_bytes = max_body_bytes
        self._initialize_database()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize_database(self) -> None:
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS anchors (
                    stream_id TEXT PRIMARY KEY,
                    seq INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    receipt_json TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS idempotency_keys (
                    idempotency_key TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    receipt_json TEXT NOT NULL
                )
                """
            )

    def authenticate(self, request: Request) -> JSONResponse | None:
        authorization = request.headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            return _error("anchor_auth_required", "缺少 Bearer 认证", 401)
        supplied = authorization[7:]
        if not supplied or not hmac.compare_digest(supplied, self._bearer_token):
            return _error("anchor_auth_failed", "Anchor Service 认证失败", 403)
        return None

    async def read_payload(self, request: Request) -> tuple[AnchorPayload | None, JSONResponse | None]:
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            return None, _error("anchor_unsupported_media_type", "请求必须使用 application/json", 415)
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > self._max_body_bytes:
                    return None, _error("anchor_request_too_large", "请求体超过限制", 413)
            except ValueError:
                return None, _error("anchor_invalid_request", "Content-Length 无效", 400)
        chunks: list[bytes] = []
        body_size = 0
        async for chunk in request.stream():
            body_size += len(chunk)
            if body_size > self._max_body_bytes:
                return None, _error("anchor_request_too_large", "请求体超过限制", 413)
            chunks.append(chunk)
        try:
            decoded = json.loads(b"".join(chunks))
            payload = AnchorPayload.model_validate(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError):
            return None, _error("anchor_invalid_payload", "AnchorPayload 校验失败", 400)
        return payload, None

    def publish(self, payload: AnchorPayload, idempotency_key: str) -> tuple[dict[str, Any], int]:
        payload_json = _canonical_json(payload.model_dump(mode="json"))
        expected_key = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        if not _IDEMPOTENCY_PATTERN.fullmatch(idempotency_key) or not hmac.compare_digest(
            idempotency_key, expected_key
        ):
            return _error_body("anchor_invalid_idempotency_key", "Idempotency-Key 无效"), 400

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            idempotent = connection.execute(
                "SELECT payload_json, receipt_json FROM idempotency_keys WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if idempotent is not None:
                if idempotent["payload_json"] != payload_json:
                    connection.rollback()
                    return _error_body("anchor_idempotency_conflict", "幂等键已绑定其他请求"), 409
                receipt = json.loads(idempotent["receipt_json"])
                connection.commit()
                return receipt, 200

            current = connection.execute(
                "SELECT seq, payload_json, receipt_json FROM anchors WHERE stream_id = ?",
                (payload.stream_id,),
            ).fetchone()
            if current is not None:
                if payload.audit_seq < current["seq"]:
                    connection.rollback()
                    return _error_body("anchor_rollback_rejected", "锚点序号不能回退"), 409
                if payload.audit_seq == current["seq"]:
                    if payload_json != current["payload_json"]:
                        connection.rollback()
                        return _error_body("anchor_conflict", "同序号锚点内容冲突"), 409
                    receipt_json = current["receipt_json"]
                    connection.execute(
                        "INSERT INTO idempotency_keys VALUES (?, ?, ?)",
                        (idempotency_key, payload_json, receipt_json),
                    )
                    connection.commit()
                    return json.loads(receipt_json), 200

            receipt = self._sign_receipt(payload)
            receipt_json = _canonical_json(receipt)
            connection.execute(
                """
                INSERT INTO anchors(stream_id, seq, payload_json, receipt_json)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(stream_id) DO UPDATE SET
                    seq = excluded.seq,
                    payload_json = excluded.payload_json,
                    receipt_json = excluded.receipt_json
                """,
                (payload.stream_id, payload.audit_seq, payload_json, receipt_json),
            )
            connection.execute(
                "INSERT INTO idempotency_keys VALUES (?, ?, ?)",
                (idempotency_key, payload_json, receipt_json),
            )
            connection.commit()
            return receipt, 201
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def latest(self, stream_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT receipt_json FROM anchors WHERE stream_id = ?", (stream_id,)
            ).fetchone()
        return None if row is None else json.loads(row["receipt_json"])

    def _sign_receipt(self, payload: AnchorPayload) -> dict[str, Any]:
        unsigned = {
            "receipt_id": f"rcpt_{uuid.uuid4().hex}",
            "payload": payload.model_dump(mode="json"),
            "anchored_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "service_key_id": self._service_key_id,
            "algorithm": "ed25519",
        }
        signature = self._private_key.sign(_canonical_json(unsigned).encode("utf-8"))
        return {**unsigned, "signature": base64.b64encode(signature).decode("ascii")}


def _error_body(code: str, message: str) -> dict[str, str]:
    return {"error_code": code, "message": message}


def _error(code: str, message: str, status_code: int) -> JSONResponse:
    return JSONResponse(_error_body(code, message), status_code=status_code)


def create_app(service: AnchorService) -> Starlette:
    async def publish(request: Request) -> JSONResponse:
        auth_error = service.authenticate(request)
        if auth_error is not None:
            return auth_error
        payload, payload_error = await service.read_payload(request)
        if payload_error is not None:
            return payload_error
        assert payload is not None
        stream_id = urllib.parse.unquote(request.path_params["stream_id"])
        if stream_id != payload.stream_id:
            return _error("anchor_stream_mismatch", "路径与 payload 的 stream_id 不一致", 400)
        idempotency_key = request.headers.get("idempotency-key", "").strip()
        body, status = service.publish(payload, idempotency_key)
        return JSONResponse(body, status_code=status)

    async def latest(request: Request) -> JSONResponse:
        auth_error = service.authenticate(request)
        if auth_error is not None:
            return auth_error
        receipt = service.latest(urllib.parse.unquote(request.path_params["stream_id"]))
        if receipt is None:
            return _error("anchor_not_found", "未找到锚点", 404)
        return JSONResponse(receipt)

    return Starlette(
        routes=[
            Route("/v1/anchors/{stream_id:path}/latest", latest, methods=["GET"]),
            Route("/v1/anchors/{stream_id:path}", publish, methods=["PUT"]),
        ]
    )


def _app_from_environment() -> Starlette:
    token = os.environ.get("ANCHOR_BEARER_TOKEN")
    encoded_key = os.environ.get("ANCHOR_ED25519_PRIVATE_KEY")
    if not token or not encoded_key:
        raise RuntimeError("必须配置 ANCHOR_BEARER_TOKEN 和 ANCHOR_ED25519_PRIVATE_KEY")
    try:
        key_bytes = base64.b64decode(encoded_key, validate=True)
        private_key = Ed25519PrivateKey.from_private_bytes(key_bytes)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("ANCHOR_ED25519_PRIVATE_KEY 配置无效") from exc
    service = AnchorService(
        os.environ.get("ANCHOR_SQLITE_PATH", "anchor-service.sqlite3"),
        bearer_token=token,
        private_key=private_key,
        service_key_id=os.environ.get("ANCHOR_SERVICE_KEY_ID", "anchor-service-01"),
        max_body_bytes=int(os.environ.get("ANCHOR_MAX_BODY_BYTES", str(_MAX_BODY_BYTES))),
    )
    return create_app(service)


app = _app_from_environment() if os.environ.get("ANCHOR_SERVICE_AUTOCONFIGURE") == "1" else Starlette()
