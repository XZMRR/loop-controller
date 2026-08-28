"""审批请求与结果持久化（v0.3.0 Iteration 5 / v0.29.0 跨进程可见性加固）。

``JsonlApprovalStore`` 以追加方式记录 ``ApprovalRequest`` 与 ``ApprovalRecord``，
CLI 通过它查询待审批请求并写入人工审批结果；Runtime 通过它读取审批结果以
继续执行被拦截的动作。

v0.29.0 新增：
- ``refresh()`` 增量重放，让运行中的 Runtime 进程看到其它进程写入的审批结果；
- ``record_response()`` 拒绝覆盖（相同内容幂等）；
- 写路径使用 ``portalocker`` 跨进程文件锁；
- 损坏/半行策略：中间损坏行 WARN 并跳过，末行半行忽略。
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

import portalocker

from loop_controller.infra.alert_store import AlertStore
from loop_controller.models import ApprovalRecord, ApprovalRequest, AuditAlert

logger = logging.getLogger(__name__)


class ApprovalStoreError(Exception):
    """ApprovalStore 损坏或操作冲突时抛出（fail-closed）。"""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _serialize_request(request: ApprovalRequest) -> dict:
    # v0.5.1：使用 mode="json" 递归序列化嵌套模型（Decision）和 datetime。
    data = request.model_dump(mode="json")
    data["type"] = "request"
    return data


def _serialize_record(record: ApprovalRecord) -> dict:
    data = record.model_dump(mode="json")
    data["type"] = "response"
    return data


def _deserialize_request(record: dict) -> ApprovalRequest:
    record = dict(record)
    record.pop("type", None)
    return ApprovalRequest.model_validate(record)


def _deserialize_record(record: dict) -> ApprovalRecord:
    record = dict(record)
    record.pop("type", None)
    return ApprovalRecord.model_validate(record)


@runtime_checkable
class ApprovalStore(Protocol):
    """审批请求与结果持久化协议。"""

    def submit_request(self, request: ApprovalRequest) -> None: ...
    def get_pending(self) -> list[ApprovalRequest]: ...
    def get_request(self, decision_id: str) -> ApprovalRequest | None: ...
    def get_request_by_id(self, request_id: str) -> ApprovalRequest | None: ...
    def record_response(self, record: ApprovalRecord) -> None: ...
    def get_record(self, decision_id: str) -> ApprovalRecord | None: ...
    def refresh(self) -> None: ...


class JsonlApprovalStore:
    """JSONL 持久化 ApprovalStore。

    落盘格式（每行一个 JSON）：
    - ``{"type": "request", ...ApprovalRequest fields}``
    - ``{"type": "response", ...ApprovalRecord fields}``

    v0.29.0 内部状态：
    - ``_requests`` / ``_responses``：内存索引；
    - ``_read_offset``：已读取到的文件字节偏移；
    - ``_lock``：线程安全锁（保护内存索引与偏移量）。
    """

    def __init__(self, path: str | Path, alert_store: AlertStore | None = None) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._alert_store = alert_store
        self._requests: dict[str, ApprovalRequest] = {}
        self._responses: dict[str, ApprovalRecord] = {}
        self._lock = threading.RLock()
        self._read_offset = 0
        self._load()

    def _load(self) -> None:
        """启动时全量重放日志。"""
        with self._lock:
            self._requests.clear()
            self._responses.clear()
            self._read_offset = 0
            if not self._path.exists():
                return
            records, offset = self._read_lines_from(0)
            for record in records:
                self._merge_record(record, keep_first_response=True)
            self._read_offset = offset

    def refresh(self) -> None:
        """增量读取自上次位置以来新增的行，合并进内存。"""
        with self._lock:
            self._refresh_unlocked()

    def _refresh_unlocked(self) -> None:
        if not self._path.exists():
            self._read_offset = 0
            return
        size = self._path.stat().st_size
        if size < self._read_offset:
            logger.warning(
                "审批存储 %s 文件被截断或重建（偏移 %d > 大小 %d），执行全量重放",
                self._path,
                self._read_offset,
                size,
            )
            self._load()
            return
        if size == self._read_offset:
            return
        records, offset = self._read_lines_from(self._read_offset)
        for record in records:
            self._merge_record(record, keep_first_response=True)
        self._read_offset = offset

    def _read_lines_from(self, start_offset: int) -> tuple[list[dict], int]:
        """从 ``start_offset`` 读取完整行，返回记录列表与新的字节偏移。

        末行若缺少换行符则视为不完整，不返回、不推进偏移。
        """
        records: list[dict] = []
        if not self._path.exists():
            return records, start_offset
        with self._path.open("r", encoding="utf-8") as fh:
            fh.seek(start_offset)
            lines: list[str] = []
            new_offset = start_offset
            while True:
                pos = fh.tell()
                line = fh.readline()
                if not line:
                    break
                if not line.endswith("\n"):
                    logger.warning(
                        "审批存储 %s 末行不完整（偏移 %d），等待后续写入",
                        self._path,
                        pos,
                    )
                    break
                lines.append(line)
                new_offset = fh.tell()
            for line in lines:
                text = line.strip()
                if not text:
                    continue
                try:
                    records.append(json.loads(text))
                except json.JSONDecodeError:
                    logger.warning(
                        "审批存储 %s 损坏行已跳过: %r",
                        self._path,
                        text[:200],
                    )
                    self._alert_for_bad_line(text, "invalid_json")
            return records, new_offset

    def _alert_for_bad_line(self, line_text: str, reason: str) -> None:
        """对损坏/非法审批行写入告警（如 alert_store 已注入）。"""
        if self._alert_store is None:
            return
        try:
            self._alert_store.save_alert(
                AuditAlert(
                    alert_id=uuid.uuid4().hex,
                    session_id="",
                    task_id=None,
                    rule_id="approval_store_corrupted_line",
                    severity="high",
                    title="审批存储损坏/非法行",
                    description=f"审批存储 {self._path} 遇到 reason={reason} 的损坏/非法行",
                    evidence=[line_text[:200]],
                )
            )
        except Exception as exc:
            logger.warning("审批存储损坏告警写入失败: %s", exc)

    def _merge_record(self, record: dict, *, keep_first_response: bool = True) -> None:
        rtype = record.get("type")
        if rtype == "request":
            try:
                request = _deserialize_request(record)
            except (TypeError, ValueError):
                logger.warning("审批存储 %s 非法 request 行已跳过", self._path)
                self._alert_for_bad_line(str(record)[:200], "invalid_request")
                return
            self._requests[request.decision_id] = request
        elif rtype == "response":
            try:
                response = _deserialize_record(record)
            except (TypeError, ValueError):
                logger.warning("审批存储 %s 非法 response 行已跳过", self._path)
                self._alert_for_bad_line(str(record)[:200], "invalid_response")
                return
            if keep_first_response:
                self._responses.setdefault(response.decision_id, response)
            else:
                self._responses[response.decision_id] = response

    def submit_request(self, request: ApprovalRequest) -> None:
        """提交审批请求；以 decision_id 为键去重覆盖。"""
        with self._lock:
            self._requests[request.decision_id] = request
            self._append(_serialize_request(request))

    def get_pending(self) -> list[ApprovalRequest]:
        """返回尚未有审批结果的请求列表。"""
        with self._lock:
            return [
                req
                for decision_id, req in self._requests.items()
                if decision_id not in self._responses
            ]

    def get_request(self, decision_id: str) -> ApprovalRequest | None:
        with self._lock:
            return self._requests.get(decision_id)

    def get_request_by_id(self, request_id: str) -> ApprovalRequest | None:
        """v0.13.0：按 request_id 查找原始审批请求。"""
        with self._lock:
            for req in self._requests.values():
                if req.request_id == request_id:
                    return req
            return None

    def record_response(self, record: ApprovalRecord) -> None:
        """记录审批结果；已存在结果时拒绝覆盖（相同内容幂等）。"""
        with self._lock:
            # 先刷新，避免其它进程已经写入的结果被覆盖。
            self._refresh_unlocked()
            existing = self._responses.get(record.decision_id)
            if existing is not None:
                if existing == record:
                    return
                raise ApprovalStoreError(f"decision {record.decision_id} 已有审批结果，不允许覆盖")
            self._responses[record.decision_id] = record
            self._append(_serialize_record(record))

    def get_record(self, decision_id: str) -> ApprovalRecord | None:
        with self._lock:
            return self._responses.get(decision_id)

    def _append(self, record: dict) -> None:
        line = json.dumps(record, ensure_ascii=False) + "\n"
        try:
            with portalocker.Lock(str(self._path), "a", encoding="utf-8", timeout=5) as fh:
                fh.write(line)
                fh.flush()
        except portalocker.LockException as exc:
            raise ApprovalStoreError(f"无法获取审批存储文件锁 {self._path}: {exc}") from exc
        except OSError as exc:
            raise ApprovalStoreError(f"无法写入审批存储 {self._path}: {exc}") from exc


class InMemoryApprovalStore:
    """内存版 ApprovalStore；适合测试与单进程内存场景。

    不持久化，refresh 为空操作。
    """

    def __init__(self) -> None:
        self._requests: dict[str, ApprovalRequest] = {}
        self._responses: dict[str, ApprovalRecord] = {}

    def refresh(self) -> None:
        """内存版无需刷新。"""

    def submit_request(self, request: ApprovalRequest) -> None:
        self._requests[request.decision_id] = request

    def get_pending(self) -> list[ApprovalRequest]:
        return [
            req for decision_id, req in self._requests.items() if decision_id not in self._responses
        ]

    def get_request(self, decision_id: str) -> ApprovalRequest | None:
        return self._requests.get(decision_id)

    def get_request_by_id(self, request_id: str) -> ApprovalRequest | None:
        for req in self._requests.values():
            if req.request_id == request_id:
                return req
        return None

    def record_response(self, record: ApprovalRecord) -> None:
        existing = self._responses.get(record.decision_id)
        if existing is not None and existing != record:
            raise ApprovalStoreError(f"decision {record.decision_id} 已有审批结果，不允许覆盖")
        self._responses[record.decision_id] = record

    def get_record(self, decision_id: str) -> ApprovalRecord | None:
        return self._responses.get(decision_id)
