"""审批请求与结果持久化（v0.36.1 起支持敏感载荷 AES-256-GCM 加密）。"""

from __future__ import annotations

import json
import logging
import os  # noqa: F401  # 保留兼容故障注入：测试通过本模块替换 os.fsync
import shutil
import threading
import uuid
from pathlib import Path
from typing import Protocol, runtime_checkable

from loop_controller.infra.alert_store import AlertStore
from loop_controller.infra.approval_crypto import ApprovalCrypto, ApprovalCryptoError
from loop_controller.infra.durable_io import DurableIOError, DurableJsonlFile
from loop_controller.models import ApprovalRecord, ApprovalRequest, AuditAlert

logger = logging.getLogger(__name__)


class ApprovalStoreError(Exception):
    """ApprovalStore 损坏或操作冲突时抛出（fail-closed）。"""


class ApprovalStoreCorruptedError(ApprovalStoreError):
    """审批存储存在损坏或非法记录。"""


_ENCRYPTED_SCHEMA_VERSION = "1"
_SENSITIVE_FIELDS = ("tool_arguments", "original_decision")


def _aad_context(request_id: str, call_id: str, agent_id: str, tool_name: str) -> dict[str, str]:
    return {
        "request_id": request_id,
        "call_id": call_id,
        "agent_id": agent_id,
        "tool_name": tool_name,
        "schema_version": _ENCRYPTED_SCHEMA_VERSION,
    }


def _serialize_request(request: ApprovalRequest, crypto: ApprovalCrypto | None = None) -> dict:
    data = request.model_dump(mode="json")
    if crypto is not None:
        payload: dict[str, object] = {}
        has_sensitive = False
        for field in _SENSITIVE_FIELDS:
            value = data.pop(field, None)
            if value is not None:
                payload[field] = value
                has_sensitive = True
        if has_sensitive:
            aad = _aad_context(
                request_id=data["request_id"],
                call_id=data["call_id"],
                agent_id=data["agent_id"],
                tool_name=data["tool_name"],
            )
            data["encrypted_payload"] = {
                "ciphertext": crypto.encrypt(payload, aad),
                "schema_version": _ENCRYPTED_SCHEMA_VERSION,
            }
    data["type"] = "request"
    return data


def _serialize_record(record: ApprovalRecord) -> dict:
    return {**record.model_dump(mode="json"), "type": "response"}


def _deserialize_request(record: dict, crypto: ApprovalCrypto | None = None) -> ApprovalRequest:
    data = dict(record)
    data.pop("type", None)
    encrypted_payload = data.pop("encrypted_payload", None)
    if encrypted_payload is not None:
        if crypto is None:
            raise ApprovalStoreCorruptedError(
                "发现加密审批记录但缺少解密密钥，无法恢复原始参数"
            )
        if not isinstance(encrypted_payload, dict):
            raise ApprovalStoreCorruptedError("encrypted_payload 必须是对象")
        ciphertext = encrypted_payload.get("ciphertext")
        if not ciphertext or not isinstance(ciphertext, str):
            raise ApprovalStoreCorruptedError("encrypted_payload.ciphertext 缺失或类型错误")
        schema_version = str(encrypted_payload.get("schema_version", _ENCRYPTED_SCHEMA_VERSION))
        aad = _aad_context(
            request_id=data.get("request_id", ""),
            call_id=data.get("call_id", ""),
            agent_id=data.get("agent_id", ""),
            tool_name=data.get("tool_name", ""),
        )
        aad["schema_version"] = schema_version
        try:
            plaintext = crypto.decrypt(ciphertext, aad)
        except ApprovalCryptoError as exc:
            raise ApprovalStoreCorruptedError(f"审批敏感载荷解密失败：{exc}") from exc
        for field in _SENSITIVE_FIELDS:
            if field in plaintext:
                data[field] = plaintext[field]
    return ApprovalRequest.model_validate(data)


def _deserialize_record(record: dict) -> ApprovalRecord:
    data = dict(record)
    data.pop("type", None)
    return ApprovalRecord.model_validate(data)


def migrate_approval_store(
    path: str | Path,
    crypto: ApprovalCrypto,
    *,
    backup_suffix: str = ".plaintext-backup",
) -> None:
    """离线、幂等地把明文 Approval JSONL 迁移为加密格式。

    步骤：
    1. 若目标文件不存在，直接返回；
    2. 创建 ``path + backup_suffix`` 只读备份；
    3. 逐行读取旧文件，把 ``request`` 类型记录中的敏感字段加密后写入临时文件；
    4. ``response`` 类型记录原样复制；
    5. fsync 临时文件并原子替换原文件；
    6. 迁移失败不删除备份。

    幂等性：已加密记录会被重新解密再加密（ciphertext 会改变，语义等价）。
    """
    src = Path(path)
    if not src.exists():
        return
    backup_path = src.with_suffix(src.suffix + backup_suffix)
    shutil.copy2(src, backup_path)
    tmp_path = src.with_suffix(src.suffix + ".migrate-tmp")
    try:
        with tmp_path.open("w", encoding="utf-8", newline="") as f:
            with src.open("r", encoding="utf-8") as f_src:
                for line in f_src:
                    line = line.rstrip("\r\n")
                    if not line:
                        continue
                    record = json.loads(line)
                    if record.get("type") == "request":
                        request = _deserialize_request(record, crypto=crypto)
                        record = _serialize_request(request, crypto=crypto)
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        tmp_path.replace(src)
    except Exception:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


@runtime_checkable
class ApprovalStore(Protocol):
    def submit_request(self, request: ApprovalRequest) -> None: ...
    def get_pending(self) -> list[ApprovalRequest]: ...
    def get_request(self, decision_id: str) -> ApprovalRequest | None: ...
    def get_request_by_id(self, request_id: str) -> ApprovalRequest | None: ...
    def record_response(self, record: ApprovalRecord) -> None: ...
    def get_record(self, decision_id: str) -> ApprovalRecord | None: ...
    def refresh(self) -> None: ...


class JsonlApprovalStore:
    def __init__(
        self,
        path: str | Path,
        alert_store: AlertStore | None = None,
        crypto: ApprovalCrypto | None = None,
    ) -> None:
        self._path = Path(path)
        self._durable = DurableJsonlFile(self._path)
        self._alert_store = alert_store
        self._crypto = crypto
        self._requests: dict[str, ApprovalRequest] = {}
        self._responses: dict[str, ApprovalRecord] = {}
        self._lock = threading.RLock()
        self.refresh()

    def _alert_for_bad_line(self, line_text: str, reason: str) -> None:
        if self._alert_store is None:
            return
        self._alert_store.save_alert(AuditAlert(
            alert_id=uuid.uuid4().hex,
            session_id="",
            task_id=None,
            rule_id="approval_store_corrupted_line",
            severity="high",
            title="审批存储损坏/非法行",
            description=f"审批存储 {self._path} 遇到 reason={reason} 的损坏/非法行",
            evidence=[line_text[:200]],
        ))

    def _refresh_locked(self, transaction) -> None:
        requests: dict[str, ApprovalRequest] = {}
        responses: dict[str, ApprovalRecord] = {}
        transaction._stream.seek(0)
        content = transaction._stream.read()
        for raw in content.splitlines(keepends=True):
            if not raw.endswith(b"\n"):
                logger.warning("审批存储 %s 末行不完整，等待后续写入", self._path)
                break
            raw = raw.rstrip(b"\r\n")
            try:
                record = json.loads(raw)
                if record.get("type") == "request":
                    request = _deserialize_request(record, crypto=self._crypto)
                    requests[request.decision_id] = request
                elif record.get("type") == "response":
                    response = _deserialize_record(record)
                    responses.setdefault(response.decision_id, response)
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
                self._alert_for_bad_line(raw.decode("utf-8", errors="replace"), "invalid_json")
                raise ApprovalStoreCorruptedError(
                    f"审批存储 {self._path} 存在损坏的完整记录"
                ) from exc
        self._requests, self._responses = requests, responses

    def refresh(self) -> None:
        with self._lock:
            try:
                with self._durable.transaction() as transaction:
                    self._refresh_locked(transaction)
            except DurableIOError as exc:
                raise ApprovalStoreError(f"无法读取审批存储 {self._path}: {exc}") from exc

    def submit_request(self, request: ApprovalRequest) -> None:
        with self._lock:
            try:
                with self._durable.transaction() as transaction:
                    transaction.repair_incomplete_tail()
                    self._refresh_locked(transaction)
                    existing = self._requests.get(request.decision_id)
                    if existing == request:
                        return
                    transaction.append_json(_serialize_request(request, crypto=self._crypto))
                    self._requests[request.decision_id] = request
            except DurableIOError as exc:
                raise ApprovalStoreError(f"无法写入审批存储 {self._path}: {exc}") from exc

    def get_pending(self) -> list[ApprovalRequest]:
        self.refresh()
        return [r for decision_id, r in self._requests.items() if decision_id not in self._responses]

    def get_request(self, decision_id: str) -> ApprovalRequest | None:
        return self._requests.get(decision_id)

    def get_request_by_id(self, request_id: str) -> ApprovalRequest | None:
        return next((r for r in self._requests.values() if r.request_id == request_id), None)

    def record_response(self, record: ApprovalRecord) -> None:
        with self._lock:
            try:
                with self._durable.transaction() as transaction:
                    transaction.repair_incomplete_tail()
                    self._refresh_locked(transaction)
                    existing = self._responses.get(record.decision_id)
                    if existing is not None:
                        if existing == record:
                            return
                        raise ApprovalStoreError(
                            f"decision {record.decision_id} 已有审批结果，不允许覆盖"
                        )
                    transaction.append_json(_serialize_record(record))
                    self._responses[record.decision_id] = record
            except DurableIOError as exc:
                raise ApprovalStoreError(f"无法写入审批存储 {self._path}: {exc}") from exc

    def get_record(self, decision_id: str) -> ApprovalRecord | None:
        return self._responses.get(decision_id)


class InMemoryApprovalStore:
    def __init__(self) -> None:
        self._requests: dict[str, ApprovalRequest] = {}
        self._responses: dict[str, ApprovalRecord] = {}

    def refresh(self) -> None:
        pass

    def submit_request(self, request: ApprovalRequest) -> None:
        self._requests[request.decision_id] = request

    def get_pending(self) -> list[ApprovalRequest]:
        return [r for decision_id, r in self._requests.items() if decision_id not in self._responses]

    def get_request(self, decision_id: str) -> ApprovalRequest | None:
        return self._requests.get(decision_id)

    def get_request_by_id(self, request_id: str) -> ApprovalRequest | None:
        return next((r for r in self._requests.values() if r.request_id == request_id), None)

    def record_response(self, record: ApprovalRecord) -> None:
        existing = self._responses.get(record.decision_id)
        if existing is not None and existing != record:
            raise ApprovalStoreError(f"decision {record.decision_id} 已有审批结果，不允许覆盖")
        self._responses[record.decision_id] = record

    def get_record(self, decision_id: str) -> ApprovalRecord | None:
        return self._responses.get(decision_id)
