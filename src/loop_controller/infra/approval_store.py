"""审批请求与结果持久化（v0.3.0 Iteration 5）。

``JsonlApprovalStore`` 以追加方式记录 ``ApprovalRequest`` 与 ``ApprovalRecord``，
CLI 通过它查询待审批请求并写入人工审批结果；Runtime 通过它读取审批结果以
继续执行被拦截的动作。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from loop_controller.models import ApprovalRecord, ApprovalRequest


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _serialize_request(request: ApprovalRequest) -> dict:
    data = request.model_dump()
    data["type"] = "request"
    data["created_at"] = request.created_at.isoformat()
    return data


def _serialize_record(record: ApprovalRecord) -> dict:
    data = record.model_dump()
    data["type"] = "response"
    data["decided_at"] = record.decided_at.isoformat()
    return data


def _deserialize_request(record: dict) -> ApprovalRequest:
    record = dict(record)
    record.pop("type", None)
    created = record.get("created_at")
    if isinstance(created, str):
        record["created_at"] = datetime.fromisoformat(created)
    return ApprovalRequest(**record)


def _deserialize_record(record: dict) -> ApprovalRecord:
    record = dict(record)
    record.pop("type", None)
    decided = record.get("decided_at")
    if isinstance(decided, str):
        record["decided_at"] = datetime.fromisoformat(decided)
    return ApprovalRecord(**record)


@runtime_checkable
class ApprovalStore(Protocol):
    """审批请求与结果持久化协议。"""

    def submit_request(self, request: ApprovalRequest) -> None: ...
    def get_pending(self) -> list[ApprovalRequest]: ...
    def get_request(self, decision_id: str) -> ApprovalRequest | None: ...
    def record_response(self, record: ApprovalRecord) -> None: ...
    def get_record(self, decision_id: str) -> ApprovalRecord | None: ...


class JsonlApprovalStore:
    """JSONL 持久化 ApprovalStore。

    落盘格式（每行一个 JSON）：
    - ``{"type": "request", ...ApprovalRequest fields}``
    - ``{"type": "response", ...ApprovalRecord fields}``
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._requests: dict[str, ApprovalRequest] = {}
        self._responses: dict[str, ApprovalRecord] = {}
        self._load()

    def _load(self) -> None:
        """启动时重放全量日志。"""
        if not self._path.exists():
            return
        with self._path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rtype = record.get("type")
                if rtype == "request":
                    try:
                        request = _deserialize_request(record)
                    except (TypeError, ValueError):
                        continue
                    self._requests[request.decision_id] = request
                elif rtype == "response":
                    try:
                        response = _deserialize_record(record)
                    except (TypeError, ValueError):
                        continue
                    self._responses[response.decision_id] = response

    def submit_request(self, request: ApprovalRequest) -> None:
        """提交审批请求；以 decision_id 为键去重覆盖。"""
        self._requests[request.decision_id] = request
        self._append(_serialize_request(request))

    def get_pending(self) -> list[ApprovalRequest]:
        """返回尚未有审批结果的请求列表。"""
        return [
            req
            for decision_id, req in self._requests.items()
            if decision_id not in self._responses
        ]

    def get_request(self, decision_id: str) -> ApprovalRequest | None:
        return self._requests.get(decision_id)

    def record_response(self, record: ApprovalRecord) -> None:
        """记录审批结果；覆盖同一 decision_id 的旧结果。"""
        self._responses[record.decision_id] = record
        self._append(_serialize_record(record))

    def get_record(self, decision_id: str) -> ApprovalRecord | None:
        return self._responses.get(decision_id)

    def _append(self, record: dict) -> None:
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()
