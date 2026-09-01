"""异步审批管理器（v0.3.0 Iteration 5）。

``AsyncApprovalManager`` 替代 ``ConfigR0Delegate``，把审批请求持久化到
``ApprovalStore`` 后返回 ``needs_approval`` 暂停态；审批人通过 CLI 写入
``ApprovalRecord`` 后，任务可 ``resume_task`` 继续执行。
"""

from __future__ import annotations

from typing import Any

from loop_controller.infra.approval_store import ApprovalStore, ApprovalStoreError
from loop_controller.models import ApprovalRecord, ApprovalRequest, Decision


class AsyncApprovalManager:
    """异步审批管理器。

    - ``submit``：把 ``ApprovalRequest`` 落盘；
    - ``check``：查询指定 ``decision_id`` 是否已有审批结果；
    - CLI 通过同一个 ``ApprovalStore`` 写入 ``ApprovalRecord``。
    """

    def __init__(self, store: ApprovalStore) -> None:
        self._store = store

    async def submit(self, request: ApprovalRequest) -> None:
        """提交审批请求。"""
        self._store.submit_request(request)

    def check(self, decision_id: str) -> ApprovalRecord | None:
        """查询审批结果；未审批返回 None。"""
        self._store.refresh()
        return self._store.get_record(decision_id)

    def get_request(self, decision_id: str) -> ApprovalRequest | None:
        """查询原始审批请求（用于 resume 时强绑定校验）。"""
        self._store.refresh()
        return self._store.get_request(decision_id)

    def get_request_by_id(self, request_id: str) -> ApprovalRequest | None:
        """v0.13.0：按 request_id 查找原始审批请求。"""
        self._store.refresh()
        return self._store.get_request_by_id(request_id)

    def get_decision(self, decision_id: str) -> Decision | None:
        """v0.5.1：查询审批请求绑定的原始 Decision（MCP Proxy 重试用）。"""
        self._store.refresh()
        request = self._store.get_request(decision_id)
        return request.original_decision if request is not None else None

    def list_recent(self, limit: int = 100) -> list[dict[str, Any]]:
        """v0.32.0：返回最近的审批请求摘要（按提交时间倒序）。"""
        self._store.refresh()
        requests = getattr(self._store, "_requests", {})
        records = getattr(self._store, "_records", {})
        items: list[dict[str, Any]] = []
        for decision_id, request in requests.items():
            record = records.get(decision_id)
            items.append(
                {
                    "decision_id": decision_id,
                    "request_id": request.request_id,
                    "tool_name": request.tool_name,
                    "status": record.verdict if record is not None else "pending",
                    "created_at": request.created_at.isoformat(),
                }
            )
        items.sort(key=lambda d: d["created_at"], reverse=True)
        return items[:limit]

    async def cancel_request(self, request_id: str) -> ApprovalRecord | None:
        """因超时等原因取消待审批请求；若已有审批结果则返回该结果。"""
        self._store.refresh()
        request = self._store.get_request_by_id(request_id)
        if request is None:
            return None
        existing = self._store.get_record(request.decision_id)
        if existing is not None:
            return existing
        record = ApprovalRecord(
            request_id=request_id,
            decision_id=request.decision_id,
            verdict="deny",
            approver_id="system",
            comment="cancelled by wait_for_approval timeout",
        )
        try:
            self._store.record_response(record)
        except ApprovalStoreError:
            # 竞争：取消前已有审批结果写入
            return self._store.get_record(request.decision_id)
        return record

    def get_decision_status(self, decision_id: str) -> dict[str, Any] | None:
        """v0.32.0：返回 decision 状态摘要。"""
        self._store.refresh()
        record = self._store.get_record(decision_id)
        if record is not None:
            return {"decision_id": decision_id, "status": record.verdict, "reason": record.comment}
        request = self._store.get_request(decision_id)
        if request is not None:
            return {"decision_id": decision_id, "status": "pending", "reason": ""}
        return None
