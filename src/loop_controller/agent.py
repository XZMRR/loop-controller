"""Agent 执行实体定义.

Agent 是 R1 层的执行单元，具有身份、岗位画像（CapabilityProfile）和所有者。
Agent 本身不直接调用外部工具，而是生成 ActionProposal 并向 R2 申报。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Agent:
    """R1 执行实体.

    Attributes:
        agent_id: Agent 唯一标识。
        name: 可读名称。
        profile_id: 关联的 CapabilityProfile ID。
        owner_id: Agent 所属用户/管理员的 ID。
        metadata: 扩展字段，MVP 阶段保留为空。
    """

    agent_id: str
    name: str
    profile_id: str
    owner_id: str
    metadata: dict[str, Any] = field(default_factory=dict)
