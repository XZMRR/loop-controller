"""HTTP 服务请求/响应模型（v0.17.0 / v0.18.0）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from loop_controller.identity import KillSwitchConfig, RevocationEntry, RevocationType


class GovernToolRequest(BaseModel):
    """POST /v1/govern/tool-call 请求体。"""

    agent_id: str = Field(..., description="Agent 身份标识")
    user_id: str = Field(..., description="用户身份标识")
    tool_name: str = Field(..., description="Loop Controller 内部 canonical_name")
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
    result: str = Field(..., description="给 Agent 的自然语言结果")
    request_id: str | None = Field(default=None, description="require_approval / pending 时返回的 request_id")
    error_code: str | None = Field(default=None, description="error / blocked 时的错误码")


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


# ---------------------------------------------------------------------------
# 管理控制台最小可行接口（GET /v1/admin/agents|profiles + POST govern/evaluate）
# ---------------------------------------------------------------------------


class AdminAgentItem(BaseModel):
    """GET /v1/admin/agents 列表项。"""

    agent_id: str = Field(..., description="Agent 身份标识")
    name: str = Field(..., description="显示名称")
    profile_id: str = Field(..., description="绑定的 CapabilityProfile")
    owner_id: str = Field(..., description="所属用户/部门 ID")
    owner_name: str | None = Field(default=None, description="所属用户显示名")
    tenant_id: str | None = Field(default=None, description="租户 ID")
    revoked: bool = Field(default=False, description="是否已被吊销")


class AdminAgentsResponse(BaseModel):
    """GET /v1/admin/agents 响应体。"""

    agents: list[AdminAgentItem] = Field(default_factory=list)


class AdminProfilesResponse(BaseModel):
    """GET /v1/admin/profiles 响应体（CapabilityProfile 原样序列化）。"""

    profiles: list[dict[str, Any]] = Field(default_factory=list)


class AdminGovernEvaluateRequest(BaseModel):
    """POST /v1/admin/govern/evaluate 请求体（只读判定调试）。"""

    agent_id: str = Field(..., description="Agent 身份标识")
    user_id: str = Field(..., description="用户身份标识")
    tool_name: str = Field(..., description="Loop Controller 内部 canonical_name")
    arguments: dict[str, Any] = Field(default_factory=dict, description="工具参数")
    task_context: str = Field(default="", description="任务上下文")


class AdminGovernEvaluateResponse(BaseModel):
    """POST /v1/admin/govern/evaluate 响应体。"""

    verdict: str = Field(..., description="allow / deny / modify / require_approval / blocked")
    reason: str = Field(default="", description="判定原因")
    policy_hits: list[str] = Field(default_factory=list, description="命中的策略规则")
    risk_level: str | None = Field(default=None, description="R1 分类的风险等级")
    risk_tags: list[str] = Field(default_factory=list, description="R1 分类的风险标签")
    policy_version: str = Field(default="", description="判定时生效的策略版本")
    profile_version: str = Field(default="", description="判定时生效的 Profile 版本")
    dry_run: bool = Field(default=True, description="标识该判定未真实执行")
