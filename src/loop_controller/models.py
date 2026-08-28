"""统一核心抽象（唯一权威 Schema）.

本模块是 Loop Controller 全项目唯一 Schema 来源，对应《MVP 完备方案：纯工具调用版 v1.1》§3 与 §7.1。
任何其他模块需要数据结构改动，必须先改本文件并检查全文引用。

所有模型均为 Pydantic v2 模型，`frozen=True` 表达不可变语义；
需要"修改"时使用 `model_copy(update={...})`。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# 枚举类型（照抄方案，不自行发明取值）
# ---------------------------------------------------------------------------

RiskLevel = Literal["low", "medium", "high", "critical"]
Verdict = Literal["allow", "deny", "modify", "require_approval"]
AuditDecision = Literal["allow", "deny", "modify", "require_approval", "blocked"]
ToolResultStatus = Literal["success", "error", "blocked"]
ActorType = Literal["agent", "user", "r0_delegate", "system", "checkpoint"]
AuditAction = Literal[
    "task_start",
    "propose",
    "classify",
    "evaluate",
    "approve",
    "deny",
    "execute",
    "task_end",
    "seal",
    "planner_error",
    "approval_expired",
    "approval_consumed",
    "authority_granted",  # v0.11.0：动态权限提升授予
    "authority_used",  # v0.11.0：动态权限提升使用
    "authority_revoked",  # v0.11.0：动态权限提升撤销
    "authority_expired",  # v0.11.0：动态权限提升过期
    "admin_operation",
    "revocation_blocked",
    "anchor_bootstrap",  # v0.28.0：可信锚点 bootstrap
    "anchor_verify",  # v0.28.0：可信锚点管理校验
    "anchor_publish",  # v0.28.0：可信锚点管理发布
]
ApprovalVerdict = Literal["approve", "deny"]
ConversationRole = Literal["user", "agent"]
TaskRunStatus = Literal["completed", "needs_user_input", "needs_approval"]


def _utc_now() -> datetime:
    """统一的 timezone-aware UTC 当前时间（禁止 naive datetime，偏离 D16）。"""
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# §3.1 Task
# ---------------------------------------------------------------------------


class Task(BaseModel):
    """一次用户请求的上下文。

    v1.2 起废除 ``session_id == task_id`` 约定：session 为同一 ``(user_id, agent_id)``
    的连续任务流，由 ``SessionManager`` 分配与复用。
    ``description`` 原文不进入 Rego input，仅用于 R1 规划与 R3 审计。

    v0.6.0 新增 ``status`` 与 ``completed_at``，支持 ``JsonlTaskStore`` 持久化生命周期。
    """

    model_config = ConfigDict(frozen=True)

    task_id: str
    session_id: str
    user_id: str
    agent_id: str
    description: str
    tenant_id: str | None = None  # v0.22.0 多租户预留
    status: Literal["created", "completed"] = "created"
    created_at: datetime = Field(default_factory=_utc_now)
    completed_at: datetime | None = None


# ---------------------------------------------------------------------------
# §3.2 Agent
# ---------------------------------------------------------------------------


class Agent(BaseModel):
    """R1 执行实体，身份预先分配于 ``agents.yaml``，运行期不可变。"""

    model_config = ConfigDict(frozen=True)

    agent_id: str
    name: str
    profile_id: str  # MVP 一对一静态绑定，不支持运行时切换
    owner_id: str  # 所属人类用户/部门，用于审批路由
    identity: dict[str, Any] | None = None  # v0.20.0 外部身份元数据
    tenant_id: str | None = None  # v0.22.0 多租户预留


# ---------------------------------------------------------------------------
# §3.3 CapabilityProfile
# ---------------------------------------------------------------------------


class ToolPermission(BaseModel):
    """单个工具的精细化权限配置。

    ``allowed_args`` / ``denied_args`` 的值支持 POSIX glob（如 ``*@company.com``、``/data/kb/**``）。
    """

    model_config = ConfigDict(frozen=True)

    tool_name: str
    allowed: bool = False
    require_approval: bool = False
    allowed_args: dict[str, list[str]] = Field(default_factory=dict)
    denied_args: dict[str, list[str]] = Field(default_factory=dict)
    max_calls_per_task: int | None = None


class CapabilityProfile(BaseModel):
    """Agent 的岗位说明书。

    ``tools`` 中没有声明的工具名，R2 直接 deny（默认拒绝），不进入 Rego 查询。
    """

    model_config = ConfigDict(frozen=True)

    profile_id: str
    version: str = ""  # = 配置文件内容 SHA-256 前 12 位，由 PolicyStore/ConfigLoader 填充
    description: str = ""
    tools: dict[str, ToolPermission] = Field(default_factory=dict)
    max_budget_token: int = 1_000_000
    max_budget_payment: float = 0.0
    fixed_ceiling: dict[str, Any] = Field(default_factory=dict)  # Earned Authority post-MVP
    session_risk_threshold: float = Field(default=0.6, ge=0.0, le=1.0)  # v1.2 会话级风险门控阈值
    session_block_threshold: int = Field(default=5, ge=1)  # v0.4.0 连续 deny 熔断阈值


# ---------------------------------------------------------------------------
# §3.4 ActionProposal
# ---------------------------------------------------------------------------


class ActionProposal(BaseModel):
    """R1 向 R2 的动作申报。

    ``risk_level`` / ``risk_tags`` 由 R1 分类器写入，**只是 R2 的输入信号**，
    不构成任何判定效力；真正判定由 R2（Rego + 组合规则）完成。
    """

    model_config = ConfigDict(frozen=True)

    task_id: str
    call_id: str  # v1.1：run_task 框架统一生成的 UUID（Planner 不生成）；R2 校验全局唯一
    agent_id: str
    tool_name: str  # 规范化工具名
    arguments: dict[str, Any]
    task_context: str  # 由 Task.description 纯截断（前 200 字符）生成
    risk_level: RiskLevel = "low"
    risk_tags: list[str] = Field(default_factory=list)
    combination_risk_tags: list[str] = Field(default_factory=list)  # v0.10.0：能力组合风险标签
    combination_risk_score: int = 0  # v0.10.0：能力组合风险分数
    authority_token_ids: list[str] = Field(default_factory=list)  # v0.11.0：持有的动态权限令牌
    reason: str = ""  # R1 认为需要此动作的理由，供审批人与审计阅读


# ---------------------------------------------------------------------------
# §3.5 RiskSignal
# ---------------------------------------------------------------------------


class RiskSignal(BaseModel):
    """R1 轻量分类器输出的风险信号。

    ``suggestion`` 仅供 R1 自省与审计，不进入 R2 判定。
    """

    model_config = ConfigDict(frozen=True)

    risk_level: RiskLevel
    tags: list[str] = Field(default_factory=list)
    reason: str = ""
    suggestion: str | None = None


# ---------------------------------------------------------------------------
# §3.6 Decision
# ---------------------------------------------------------------------------


class Decision(BaseModel):
    """R2 对 ActionProposal 的权威判定。

    ``expires_at`` 的分档逻辑（allow/modify 5min、require_approval 15min、deny 立即过期）
    由 Checkpoint 的工厂方法集中处理，不在此模型内。
    """

    model_config = ConfigDict(frozen=True)

    decision_id: str  # R2 生成的 UUID
    call_id: str
    task_id: str
    verdict: Verdict
    reason: str  # 不允许为空字符串（审批可读性与审计可解释性底线）
    modified_args: dict[str, Any] | None = None  # verdict == "modify" 时回写
    escalation_target: str | None = None  # verdict == "require_approval" 时指向审批人
    policy_hits: list[str] = Field(default_factory=list)  # 由 OPA 返回，Checkpoint 透传
    policy_version: str = ""  # 判定时生效的策略版本
    profile_version: str = ""  # 判定时生效的 Profile 版本
    expires_at: datetime
    max_uses: int = 1  # MVP 固定为 1（deny 为 0）


# ---------------------------------------------------------------------------
# §3.7 Tool / ToolResult
# ---------------------------------------------------------------------------


class Tool(BaseModel):
    """MCP 工具元数据。"""

    model_config = ConfigDict(frozen=True)

    canonical_name: str  # Loop Controller 内部名，如 "read_file"
    mcp_name: str  # 真实 MCP server 工具名，如 "read_text_file"
    description: str
    input_schema: dict  # JSON Schema，与 MCP 协议对齐


class ToolResult(BaseModel):
    """工具调用结果；``blocked`` = 被治理链路拦截。"""

    model_config = ConfigDict(frozen=True)

    call_id: str
    task_id: str
    tool_name: str  # canonical_name
    status: ToolResultStatus
    content: Any
    error_code: str | None = None
    elapsed_ms: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)  # v0.25.0 Harness 透传元数据


# ---------------------------------------------------------------------------
# §3.8 BudgetCost
# ---------------------------------------------------------------------------


class BudgetCost(BaseModel):
    """单次动作成本估算。"""

    model_config = ConfigDict(frozen=True)

    token_count: int = 0
    payment_amount: float = 0.0  # MVP 恒为 0
    currency: str = "USD"


ReservationState = Literal[
    "pending",
    "pending_approval",
    "committed",
    "refunded",
    "expired",
]


class BudgetReservation(BaseModel):
    """v0.6.1：预算预留状态机实体。

    由 ``Checkpoint`` 在 ``evaluate()`` 成功后创建，并在执行/拒绝/审批/异常路径上
    统一流转状态。``reservation_id`` 与 ``call_id`` 一一对应。
    """

    model_config = ConfigDict(frozen=True)

    reservation_id: str
    task_id: str
    call_id: str
    tool_name: str
    cost: BudgetCost
    state: ReservationState
    created_at: datetime = Field(default_factory=_utc_now)
    expires_at: datetime | None = None


# ---------------------------------------------------------------------------
# §3.9 RiskProfile
# ---------------------------------------------------------------------------


class RiskProfile(BaseModel):
    """Session 级风险画像。"""

    model_config = ConfigDict(frozen=True)

    session_id: str
    cumulative_risk_score: float = 0.0
    recent_tags: list[str] = Field(default_factory=list)
    denied_count: int = 0
    approval_count: int = 0
    consecutive_deny_count: int = 0  # v0.4.0：连续 deny 计数，用于会话级硬熔断


# ---------------------------------------------------------------------------
# §3.10 ApprovalRequest / ApprovalRecord
# ---------------------------------------------------------------------------


class ApprovalRequest(BaseModel):
    """提交给 R0-delegate 的审批请求。

    ``decision_id`` 强绑定触发审批的 Decision，不允许为空。
    审批人看到的是掩码后参数（``arguments_masked``）。

    v0.5.1 新增：``tool_arguments`` 保存原始未掩码参数，``original_decision``
    保存触发审批的原始 Decision，用于 MCP Proxy 审批通过后重试时恢复执行。
    """

    model_config = ConfigDict(frozen=True)

    request_id: str
    decision_id: str
    call_id: str
    task_id: str
    agent_id: str
    tool_name: str
    arguments_masked: dict
    tool_arguments: dict[str, Any] = Field(default_factory=dict)
    original_decision: Decision | None = None
    reason: str  # R2 给出的升级理由
    requester_id: str  # 任务发起者 user_id
    approver_id: str  # 被指派的审批人
    created_at: datetime = Field(default_factory=_utc_now)


class ApprovalRecord(BaseModel):
    """R0-delegate 的审批结果（两态：approve / deny，escalate 移出 MVP）。"""

    model_config = ConfigDict(frozen=True)

    request_id: str
    decision_id: str  # 回指，强绑定
    verdict: ApprovalVerdict
    approver_id: str
    comment: str
    decided_at: datetime = Field(default_factory=_utc_now)


# ---------------------------------------------------------------------------
# §7.1 AuditEvent
# ---------------------------------------------------------------------------


class AuditEvent(BaseModel):
    """R3 审计最小日志单元。

    ``seq`` / ``prev_hash`` 由 AuditStore 分配（哈希链）；``args_hash`` 与
    ``args_mask`` 记录脱敏后的参数，原始参数永不落盘。
    """

    model_config = ConfigDict(frozen=True)

    schema_version: str = "1.0"
    event_id: str  # UUID
    seq: int = 0  # 全局单调递增序号，由 AuditStore 分配
    prev_hash: str = ""  # 上一条事件规范 JSON 的 SHA-256；首条为 "GENESIS"
    trace_id: str  # == Task.task_id
    session_id: str  # == Task.session_id
    call_id: str | None = None  # 动作级 ID；task_start/task_end 为空
    timestamp: datetime = Field(default_factory=_utc_now)
    actor_type: ActorType
    actor_id: str
    action: AuditAction
    target: str | None = None  # tool_name 或 "checkpoint"
    decision: AuditDecision | None = None
    args_hash: str | None = None  # 规范 JSON 的 SHA-256 / HMAC-SHA256
    hash_algo: str = "sha256"  # "sha256" | "hmac-sha256"；升级 HMAC 时改此字段，schema 不变
    key_id: str | None = None  # HMAC key 标识，为轮换留口
    args_mask: dict | None = None  # 掩码后的结构化参数
    reason: str | None = None
    policy_version: str | None = None
    profile_version: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)  # 分类器 suggestion、审批 comment 等


# ---------------------------------------------------------------------------
# §5.1 PlannedAction（ScriptedPlanner 的步骤产物）
# ---------------------------------------------------------------------------


class PlannedAction(BaseModel):
    """Planner 输出的动作草案（§5.1，v1.1 评审#7/#8）。

    不含 call_id/task_id/agent_id——这些身份字段由 run_task 框架在组装
    ``ActionProposal`` 时统一生成/填充，Planner（尤其是 LLMPlanner）无权自定。
    """

    model_config = ConfigDict(frozen=True)

    tool_name: str
    arguments: dict[str, Any]
    reason: str = ""


# ---------------------------------------------------------------------------
# v0.3.0 Iteration 4：动态会话上下文
# ---------------------------------------------------------------------------


class UserQuestion(BaseModel):
    """Planner 显式请求用户补充输入。

    不是 tool call，不进入 R2 治理链路；由 Runtime 截断并返回给外部调用方。
    """

    model_config = ConfigDict(frozen=True)

    question: str
    reason: str | None = None


class ConversationMessage(BaseModel):
    """session 级对话中的一条消息。"""

    model_config = ConfigDict(frozen=True)

    message_id: str
    session_id: str
    task_id: str
    role: ConversationRole
    content: str
    created_at: datetime = Field(default_factory=_utc_now)


class ConversationContext(BaseModel):
    """session 级对话上下文；绑定 session，不绑定单个 Task。"""

    model_config = ConfigDict(frozen=True)

    session_id: str
    messages: list[ConversationMessage] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=_utc_now)


class TaskRunResult(BaseModel):
    """``run_task`` / ``resume_task`` 的返回结果。"""

    model_config = ConfigDict(frozen=True)

    status: TaskRunStatus
    task_id: str
    session_id: str
    question: str | None = None  # status == "needs_user_input" 时非空
    decision_id: str | None = None  # status == "needs_approval" 时非空
    request_id: str | None = None  # status == "needs_approval" 时非空
    pending_decision: Decision | None = None  # needs_approval 时保存完整 Decision
    pending_proposal: ActionProposal | None = None  # needs_approval 时保存完整 ActionProposal


# ---------------------------------------------------------------------------
# v0.13.0 Agent 驱动治理接口
# ---------------------------------------------------------------------------


class EvaluationResult(BaseModel):
    """R1 + R2 对单次工具调用请求的判定结果，不含执行。"""

    model_config = ConfigDict(frozen=True)

    status: Literal["allow", "deny", "require_approval", "blocked"]
    decision: Decision | None = None
    request_id: str | None = None
    reason: str = ""
    risk_signal: RiskSignal | None = None


class GovernanceResult(BaseModel):
    """Agent 驱动模式下，Loop Controller 对单次工具调用的完整响应。"""

    model_config = ConfigDict(frozen=True)

    status: Literal["allow", "deny", "require_approval", "blocked", "error"]
    call_id: str
    tool_name: str
    arguments: dict[str, Any]
    decision: Decision | None = None  # allow/modify 时有
    request_id: str | None = None  # require_approval 时有
    reason: str = ""
    content: Any = None  # allow 后执行的结果内容
    error_code: str | None = None


# ---------------------------------------------------------------------------
# v0.11.0 Earned Authority Manager（动态权限提升）
# ---------------------------------------------------------------------------


class AuthorityRequest(BaseModel):
    """Agent 向治理系统申请临时能力的请求。"""

    model_config = ConfigDict(frozen=True)

    request_id: str
    agent_id: str
    task_id: str
    requested_capabilities: list[str]
    reason: str
    user_confirmation: bool = False


class AuthorityToken(BaseModel):
    """治理系统签发的临时能力令牌。"""

    model_config = ConfigDict(frozen=True)

    token_id: str
    request_id: str
    agent_id: str
    task_id: str
    granted_capabilities: list[str]
    budget: BudgetCost  # 令牌预算上限
    remaining_budget: BudgetCost  # 剩余预算
    expires_at: datetime
    created_at: datetime = Field(default_factory=_utc_now)
    revoked_at: datetime | None = None
    audit_record_id: str


class AuthorityConditions(BaseModel):
    """动态权限授予条件（声明式）。"""

    model_config = ConfigDict(frozen=True)

    user_confirmation: bool = False
    budget_remaining: int | None = None  # 任务剩余预算阈值
    no_recent_denials_within_steps: int | None = None
    require_task_context_regex: str | None = None


class AuthorityGrantRule(BaseModel):
    """单个能力的动态授予规则。"""

    model_config = ConfigDict(frozen=True)

    capability: str
    description: str
    conditions: AuthorityConditions
    max_duration_seconds: int
    budget_limit: BudgetCost


class AuthorityRules(BaseModel):
    """动态权限规则配置容器。"""

    model_config = ConfigDict(frozen=True)

    enabled: bool = True
    grants: dict[str, AuthorityGrantRule] = Field(default_factory=dict)


class AuthorityEvaluationContext(BaseModel):
    """评估动态权限提升请求时的上下文信息。"""

    model_config = ConfigDict(frozen=True)

    task_budget_remaining: int = 0
    recent_denial_count: int = 0
    task_context: str = ""
    history: list[ActionProposal] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# v0.12.0 R3 Asynchronous Audit Analyzer
# ---------------------------------------------------------------------------


class AuditAlert(BaseModel):
    """审计分析器生成的风险告警。"""

    model_config = ConfigDict(frozen=True)

    alert_id: str
    session_id: str
    task_id: str | None = None
    rule_id: str
    severity: Literal["low", "medium", "high", "critical"]
    title: str
    description: str
    evidence: list[str] = Field(default_factory=list)  # event_id 列表
    created_at: datetime = Field(default_factory=_utc_now)


class AuditReport(BaseModel):
    """审计分析报告。"""

    model_config = ConfigDict(frozen=True)

    report_id: str
    session_id: str
    task_id: str | None = None
    generated_at: datetime = Field(default_factory=_utc_now)
    summary: str
    alert_ids: list[str] = Field(default_factory=list)
    event_count: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class AuditRuleConditions(BaseModel):
    """审计规则条件（声明式）。"""

    model_config = ConfigDict(frozen=True)

    min_denies_count: int | None = None
    min_denies_within_seconds: int | None = None
    consecutive_denies: int | None = None
    action_sequence: list[str] | None = None
    has_any_action: list[str] | None = None
    has_all_actions: list[str] | None = None
    authority_token_exhausted: bool = False


class AuditRule(BaseModel):
    """单条审计分析规则。"""

    model_config = ConfigDict(frozen=True)

    rule_id: str
    description: str
    severity: Literal["low", "medium", "high", "critical"]
    conditions: AuditRuleConditions


class AuditRules(BaseModel):
    """审计规则配置容器。"""

    model_config = ConfigDict(frozen=True)

    enabled: bool = True
    rules: list[AuditRule] = Field(default_factory=list)

