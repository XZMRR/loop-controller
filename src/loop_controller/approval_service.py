"""审批业务逻辑共享层（v0.29.0）。

把 CLI 与 HTTP Admin 端点重复的审批校验逻辑抽取到本模块，避免双份实现漂移。
"""

from __future__ import annotations

import hmac
import os
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from loop_controller.models import ApprovalRecord, ApprovalRequest


class ApprovalServiceError(Exception):
    """审批业务校验失败；调用方负责把 message 展示给管理员或返回客户端。"""


class ApprovalAuthenticationError(Exception):
    """审批凭证缺失或无效。"""


class ApprovalAuthorizationError(Exception):
    """审批 principal 已认证但不在授权名单。"""


def resolve_approver_principal(
    credential: str | None,
    auth_config: dict[str, Any],
    *,
    environ: dict[str, str] | None = None,
) -> str:
    """从独立审批凭证映射中解析可信 principal。"""
    if not credential:
        raise ApprovalAuthenticationError("审批凭证缺失")
    env = os.environ if environ is None else environ
    credentials = auth_config.get("credentials") or []
    if not isinstance(credentials, list):
        raise ApprovalAuthenticationError("审批认证未配置")
    authenticated_principal: str | None = None
    for entry in credentials:
        if not isinstance(entry, dict):
            continue
        principal = str(entry.get("principal", "")).strip()
        token_env = str(entry.get("token_env", "")).strip()
        expected = env.get(token_env, "").strip() if token_env else ""
        if principal and expected and hmac.compare_digest(credential, expected):
            authenticated_principal = principal
            break
    if authenticated_principal is None:
        raise ApprovalAuthenticationError("审批凭证无效")
    allowlist = auth_config.get("allowlist") or []
    if not isinstance(allowlist, list) or authenticated_principal not in allowlist:
        raise ApprovalAuthorizationError("审批 principal 无权限")
    return authenticated_principal


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
    - 可信审批 principal 必须等于请求指定的 approver_id；
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

    if approver_id != request.approver_id:
        raise ApprovalAuthorizationError(
            f"审批 principal {approver_id} 无权审批指定给 {request.approver_id} 的请求"
        )

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
        principal=approver_id,
        action_summary=f"approval_{verdict}",
        decided_at=now,
    )
