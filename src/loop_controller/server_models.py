"""HTTP 服务请求/响应模型（v0.17.0 / v0.18.0）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from loop_controller.executors.base import ExecutionReceipt
from loop_controller.identity import KillSwitchConfig, RevocationEntry, RevocationType


class GovernToolRequest(BaseModel):
    """POST /v1/govern/tool-call 请求体。"""

    agent_id: str = Field(..., description="Agent 身份标识")
    user_id: str | None = Field(default=None, description="可选真实用户身份；不得由 Agent 身份推导")
    tool_name: str = Field(..., description="Loop Controller 内部 canonical_name")
    request_id: str | None = None
    interaction_id: str | None = None
    decision_id: str | None = None
    call_id: str | None = None
    delegation_jti: str | None = None
    delegation_token: str | None = Field(default=None, exclude=True)
    tenant_id: str | None = None
    target_workload_id: str | None = None
    target_instance_id: str | None = None
    required_security_capabilities: list[str] | None = None
    arguments: dict = Field(default_factory=dict, description="工具参数")
    task_context: str = Field(default="", description="任务上下文")
    session_id: str | None = Field(default=None, description="可选 Session ID")
    task_id: str | None = Field(default=None, description="可选 Task ID")
    allowed_tools: list[str] | None = Field(default=None, description="委托 token 允许的工具")
    allowed_capabilities: list[str] | None = Field(
        default=None, description="委托 token 允许的能力"
    )
    allow_redelegation: bool = Field(default=False, description="是否允许再次委托")
    deadline: datetime | None = Field(default=None, description="任务执行截止时间")


class ResumeApprovalRequest(BaseModel):
    """POST /v1/govern/resume-after-approval 请求体。"""

    request_id: str = Field(..., description="审批请求 ID")


class GovernResponse(BaseModel):
    """治理接口统一响应体。"""

    status: str = Field(..., description="治理结果状态：allow / deny / require_approval / error / blocked / pending")
    result: Any = Field(..., description="工具结果或治理说明")
    request_id: str | None = Field(default=None, description="require_approval / pending 时返回的 request_id")
    error_code: str | None = Field(default=None, description="error / blocked 时的错误码")
    terminal_status: str | None = None
    supported_security_capabilities: list[str] | None = None
    execution_receipt: ExecutionReceipt | None = None


class WaitApprovalResponse(BaseModel):
    """GET /v1/wait-for-approval 响应体。"""

    status: str = Field(..., description="allow / deny / require_approval / error / blocked / pending")
    result: str = Field(default="", description="给 Agent 的自然语言结果")
    request_id: str | None = Field(default=None, description="审批请求 ID")
    error_code: str | None = Field(default=None, description="error / blocked 时的错误码")


class HealthResponse(BaseModel):
    """GET /health 响应体。"""

    status: str = Field(..., description="服务状态")
    opa_reachable: bool = Field(default=False, description="OPA 是否可达")
    gateway_ready: bool = Field(default=False, description="MCP Gateway 是否就绪")
    evidence_status: str = Field(
        default="disabled", description="签名证据链状态：healthy / degraded / disabled"
    )
    anchor_status: str = Field(default="disabled", description="可信锚点状态")
    anchor_stream_id: str | None = Field(default=None, description="锚点流 ID")
    anchor_last_success_seq: int = Field(default=0, description="最近成功锚定序号")
    anchor_lag_events: int = Field(default=0, description="尚未被锚定的本地事件数")
    anchor_last_error_code: str | None = Field(default=None, description="净化后的稳定错误码")
    persistence: dict[str, object] = Field(default_factory=dict, description="持久化探测状态")
    durability: str = Field(default="safe", description="持久化 durability：safe / unsafe")
    uptime_seconds: float = Field(default=0.0, description="服务运行秒数")
    harness_backends: list[dict[str, Any]] = Field(
        default_factory=list, description="Harness 后端状态摘要（v0.31.0）"
    )
    degraded_backends: list[str] = Field(
        default_factory=list, description="显式启用的降级治理后端"
    )
    policy: dict[str, Any] = Field(default_factory=dict, description="OPA 策略加载状态")
    execution_security: dict[str, Any] = Field(
        default_factory=dict, description="净化后的执行安全状态"
    )


class PendingApprovalItem(BaseModel):
    """待审批请求列表项。"""

    request_id: str = Field(..., description="审批请求 ID")
    decision_id: str = Field(..., description="关联 Decision ID")
    tool_name: str = Field(..., description="工具名")
    requester_id: str = Field(..., description="请求者 ID")
    reason: str = Field(default="", description="审批原因")


class PendingApprovalsResponse(BaseModel):
    """GET /v1/admin/approvals/pending 响应体。"""

    approvals: list[PendingApprovalItem] = Field(default_factory=list, description="待审批请求列表")


class RevokeRequest(BaseModel):
    type: RevocationType
    id: str
    reason: str
    expires_at: datetime | None = None
    tenant_id: str | None = None

    def to_entry(self) -> RevocationEntry:
        return RevocationEntry(**self.model_dump())


class RevocationListResponse(BaseModel):
    revocations: list[RevocationEntry] = Field(default_factory=list)
    kill_switch: KillSwitchConfig = Field(default_factory=KillSwitchConfig)


class AuditQueryResponse(BaseModel):
    """GET /v1/admin/audit 响应体。"""

    events: list[dict] = Field(default_factory=list, description="审计事件列表")
