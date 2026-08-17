"""R0-delegate 同步打桩（§7.5 / v1.1 评审#4）.

``ConfigR0Delegate`` 按 ``approval.yaml`` 的配置立即返回审批结果，用于演示审批链路。
v1.1 起接口为 **async、实现立即返回**——语义仍是"返回即终局"，MVP 无超时概念；
post-MVP 异步化（通知→人工→回调）只改实现，``run_task`` 等调用方代码不变。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from loop_controller.infra.config_loader import ApprovalConfig
from loop_controller.models import ApprovalRecord, ApprovalRequest


@runtime_checkable
class R0Delegate(Protocol):
    """R0-delegate 接口（v1.1：async 签名 + 立即返回）。"""

    async def request_approval(self, request: ApprovalRequest) -> ApprovalRecord: ...


class ConfigR0Delegate:
    """基于配置的同步审批打桩（async 接口，方法内立即返回）。"""

    def __init__(self, config: ApprovalConfig) -> None:
        self._config = config

    def _lookup_behavior(self, tool_name: str) -> str:
        for rule in self._config.rules:
            if rule.tool_name == tool_name:
                return rule.behavior
        return "approve"  # 默认放行，便于演示；生产环境应默认 deny

    async def request_approval(self, request: ApprovalRequest) -> ApprovalRecord:
        behavior = self._lookup_behavior(request.tool_name)
        return ApprovalRecord(
            request_id=request.request_id,
            decision_id=request.decision_id,  # 强绑定，不允许为空
            verdict="approve" if behavior == "approve" else "deny",
            approver_id=request.approver_id,
            comment=f"MVP stub: configured behavior={behavior}",
            decided_at=datetime.now(timezone.utc),
        )
