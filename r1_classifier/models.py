"""R1 轻量分类器的数据模型。

精简版（dataclass 实现，不依赖外部包），仅包含分类器所需的字段，
对应文档《05_mvp_core_abstractions.md》3.1-3.5 的最小集合。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal


class RiskLevel(StrEnum):
    """风险等级，四档判定标准。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, RiskLevel):
            return NotImplemented
        return self.priority() < other.priority()

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, RiskLevel):
            return NotImplemented
        return self.priority() > other.priority()

    def __le__(self, other: object) -> bool:
        if not isinstance(other, RiskLevel):
            return NotImplemented
        return self.priority() <= other.priority()

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, RiskLevel):
            return NotImplemented
        return self.priority() >= other.priority()

    def priority(self) -> int:
        return _LEVEL_PRIORITY[self]


_LEVEL_PRIORITY = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
    RiskLevel.CRITICAL: 3,
}


@dataclass(frozen=True)
class RiskSignal:
    """分类器输出：风险信号，只供 R1 自检参考，不做最终判定。"""

    risk_level: RiskLevel
    tags: list[str] = field(default_factory=list)
    reason: str = ""
    suggestion: str | None = None


@dataclass(frozen=True)
class Task:
    """一次用户请求的上下文，审计的顶层追踪单元。"""

    task_id: str
    user_id: str
    agent_id: str
    description: str


@dataclass(frozen=True)
class Agent:
    """R1 执行实体。"""

    agent_id: str
    name: str
    profile_id: str
    owner_id: str


@dataclass(frozen=True)
class ToolPermission:
    """单个工具的权限配置（分类器只关心是否授权）。"""

    tool_name: str
    allowed: bool = False


@dataclass(frozen=True)
class CapabilityProfile:
    """Agent 的岗位说明书。"""

    profile_id: str
    version: str
    description: str
    tools: dict[str, ToolPermission] = field(default_factory=dict)

    def is_tool_authorized(self, tool_name: str) -> bool:
        perm = self.tools.get(tool_name)
        return perm is not None and perm.allowed


@dataclass(frozen=True)
class ActionProposal:
    """R1 向 R2 申报的动作（分类器预检的对象）。"""

    task_id: str
    call_id: str
    agent_id: str
    type: Literal["tool_call", "inter_agent"] = "tool_call"
    tool_name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    task_context: str = ""
    risk_level: RiskLevel = RiskLevel.LOW
    reason: str = ""
