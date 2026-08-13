"""能力画像定义.

CapabilityProfile 是 Agent 的岗位说明书，描述 Agent 能使用哪些工具、
在什么条件下使用、预算上限等。R2 在策略判定时必须参考该画像。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolPermission:
    """单个工具的精细化权限配置.

    Attributes:
        tool_name: Loop Controller 内部规范化工具名。
        allowed: 是否允许使用。
        allowed_args: 允许的参数取值白名单，如 {"to": ["@company.com"]}。
        denied_args: 禁用的参数取值黑名单。
        require_approval: 是否需要人工审批。
        max_calls_per_task: 每个任务最多调用次数。
    """

    tool_name: str
    allowed: bool = False
    allowed_args: dict[str, list[str]] | None = None
    denied_args: dict[str, list[str]] | None = None
    require_approval: bool = False
    max_calls_per_task: int | None = None


@dataclass(frozen=True)
class CapabilityProfile:
    """Agent 的岗位说明书.

    Attributes:
        profile_id: 画像唯一标识。
        agent_id: 绑定到具体 Agent 时可选，否则可被多个 Agent 复用。
        allowed_tools: 允许使用的工具名列表。
        tool_permissions: 按工具名细化的权限配置。
        denied_args: 全局禁用参数取值。
        max_budget_token: Token 级运行预算上限。
        max_budget_payment: 财务支付预算上限，研究助手场景通常为 0。
        fixed_ceiling: Earned Authority 的固定天花板，MVP 保留为空。
    """

    profile_id: str
    agent_id: str | None = None
    allowed_tools: list[str] = field(default_factory=list)
    tool_permissions: dict[str, ToolPermission] = field(default_factory=dict)
    denied_args: dict[str, list[str]] = field(default_factory=dict)
    max_budget_token: int = 0
    max_budget_payment: float = 0.0
    fixed_ceiling: dict[str, Any] = field(default_factory=dict)
