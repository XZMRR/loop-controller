"""审批请求与结果持久化。"""

from __future__ import annotations

import json
import logging
import os  # noqa: F401  # 保留兼容故障注入：测试通过本模块替换 os.fsync
import threading
import uuid
from pathlib import Path
from typing import Protocol, runtime_checkable

from loop_controller.infra.alert_store import AlertStore
from loop_controller.infra.durable_io import DurableIOError, DurableJsonlFile
from loop_controller.models import ApprovalRecord, ApprovalRequest, AuditAlert

logger = logging.getLogger(__name__)


class ApprovalStoreError(Exception):
    """ApprovalStore 损坏或操作冲突时抛出（fail-closed）。"""


def _serialize_request(request: ApprovalRequest) -> dict:
    return {**request.model_dump(mode="json"), "type": "request"}


def _serialize_record(record: ApprovalRecord) -> dict:
    return {**record.model_dump(mode="json"), "type": "response"}


def _deserialize_request(record: dict) -> ApprovalRequest:
    data = dict(record)
    data.pop("type", None)
    return ApprovalRequest.model_validate(data)


def _deserialize_record(record: dict) -> ApprovalRecord:
    data = dict(record)
    data.pop("type", None)
    return ApprovalRecord.model_validate(data)


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
    def __init__(self, path: str | Path, alert_store: AlertStore | None = None) -> None:
        self._path = Path(path)
        self._durable = DurableJsonlFile(self._path)
        self._alert_store = alert_store
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
                    request = _deserialize_request(record)
                    requests[request.decision_id] = request
                elif record.get("type") == "response":
                    response = _deserialize_record(record)
                    responses.setdefault(response.decision_id, response)
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
                self._alert_for_bad_line(raw.decode("utf-8", errors="replace"), "invalid_json")
                raise ApprovalStoreError(f"审批存储 {self._path} 存在损坏的完整记录") from exc
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
                    transaction.append_json(_serialize_request(request))
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
