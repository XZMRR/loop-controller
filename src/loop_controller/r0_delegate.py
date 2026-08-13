"""R0-delegate 审批模块.

R0-delegate 是 R0 授权的人类审批人，负责实时审批例外请求。
MVP 阶段不打真实 UI，只保留最小接口和配置化实现。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class ApprovalRequest:
    """提交给 R0-delegate 的审批请求.

    Attributes:
        decision_id: 关联的 R2 Decision.decision_id。
        call_id: 关联的 ActionProposal.call_id。
        task_id: 关联的任务 ID。
        agent_id: 执行 Agent ID。
        tool_name: 工具名。
        arguments_summary: 参数摘要（已脱敏）。
        reason: R2 要求审批的原因。
        requester_id: 任务发起人/请求者 ID。
        requested_at: 请求时间。
    """

    decision_id: str
    call_id: str
    task_id: str
    agent_id: str
    tool_name: str
    arguments_summary: str
    reason: str
    requester_id: str
    requested_at: datetime


@dataclass(frozen=True)
class ApprovalRecord:
    """R0-delegate 的审批结果.

    Attributes:
        approval_id: 审批记录唯一 ID。
        decision_id: 关联的 Decision ID。
        approver_id: 审批人 ID。
        approved: 是否批准。
        reason: 审批意见。
        responded_at: 审批响应时间。
    """

    approval_id: str
    decision_id: str
    approver_id: str
    approved: bool
    reason: str
    responded_at: datetime


@runtime_checkable
class R0Delegate(Protocol):
    """R0-delegate 接口."""

    def request_approval(self, request: ApprovalRequest) -> ApprovalRecord:
        """发起审批请求，返回审批结果（MVP 同步返回）。"""
        ...

    def get_decision(self, approval_id: str) -> ApprovalRecord | None:
        """查询审批结果."""
        ...


class ConfigR0Delegate:
    """基于配置的 R0-delegate 打桩实现.

    通过配置文件指定固定审批人，所有审批请求自动按预设策略返回。
    """

    def __init__(
        self,
        approver_id: str,
        auto_approve: bool = False,
        approver_reason: str = "Config-based approval",
    ) -> None:
        """初始化.

        Args:
            approver_id: 固定审批人 ID。
            auto_approve: 是否自动批准；False 则自动拒绝。
            approver_reason: 审批意见。
        """
        self._approver_id = approver_id
        self._auto_approve = auto_approve
        self._approver_reason = approver_reason
        self._records: dict[str, ApprovalRecord] = {}

    def request_approval(self, request: ApprovalRequest) -> ApprovalRecord:
        """返回配置化审批结果."""
        record = ApprovalRecord(
            approval_id=f"approval_{request.decision_id}",
            decision_id=request.decision_id,
            approver_id=self._approver_id,
            approved=self._auto_approve,
            reason=self._approver_reason,
            responded_at=datetime.now(timezone.utc),
        )
        self._records[record.approval_id] = record
        return record

    def get_decision(self, approval_id: str) -> ApprovalRecord | None:
        """查询已记录的审批结果."""
        return self._records.get(approval_id)
