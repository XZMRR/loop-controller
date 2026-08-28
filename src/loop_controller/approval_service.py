"""审批业务逻辑共享层（v0.29.0）。

把 CLI 与 HTTP Admin 端点重复的审批校验逻辑抽取到本模块，避免双份实现漂移。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from loop_controller.models import ApprovalRecord, ApprovalRequest


class ApprovalServiceError(Exception):
    """审批业务校验失败；调用方负责把 message 展示给管理员或返回客户端。"""


def build_approval_record(
    request: ApprovalRequest | None,
    existing_record: ApprovalRecord | None,
    approver_id: str,
    verdict: str,
    comment: str,
    *,
    approver_exists: Callable[[str], bool],
    now: datetime | None = None,
) -> ApprovalRecord:
    """校验并构造一条待写入的 ApprovalRecord。

    校验项：
    - request 必须存在；
    - 未存在审批结果（幂等重复除外）；
    - Decision 未过期（使用 ApprovalRequest.original_decision.expires_at）；
    - 审批人不能是请求者本人；
    - 审批人不能是执行 Agent；
    - 审批人必须存在于用户列表；
    - deny 必须提供非空 comment；
    - verdict 只能是 "approve" 或 "deny"。

    返回：构造好的 ApprovalRecord。
    抛出：ApprovalServiceError（message 已本地化，可直接展示）。
    """
    if now is None:
        now = datetime.now(UTC)

    if request is None:
        raise ApprovalServiceError("未找到对应 decision_id 的审批请求")

    if existing_record is not None:
        raise ApprovalServiceError(
            f"decision_id={request.decision_id} 已有审批结果，不允许覆盖"
        )

    original = request.original_decision
    if original is not None and original.expires_at is not None and original.expires_at < now:
        raise ApprovalServiceError("Decision 已过期，无法审批")

    if approver_id == request.requester_id:
        raise ApprovalServiceError("审批人不能是请求者本人")
    if approver_id == request.agent_id:
        raise ApprovalServiceError("审批人不能是执行 Agent")
    if not approver_exists(approver_id):
        raise ApprovalServiceError(f"审批人 {approver_id} 不存在")

    if verdict not in {"approve", "deny"}:
        raise ApprovalServiceError(f"无效审批结论：{verdict}")

    if verdict == "deny" and not str(comment).strip():
        raise ApprovalServiceError("deny 必须提供审批意见")

    return ApprovalRecord(
        request_id=request.request_id,
        decision_id=request.decision_id,
        verdict=verdict,  # type: ignore[arg-type]
        approver_id=approver_id,
        comment=comment,
        decided_at=now,
    )
