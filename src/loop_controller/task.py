"""任务上下文定义.

一次用户请求被封装为一个 Task，包含任务 ID、原始用户、会话 ID 和请求描述。
Task 是 R1 Agent 执行和 R2 策略判定的基本上下文单元。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class Task:
    """一次用户请求的上下文.

    Attributes:
        task_id: 任务唯一标识。
        user_id: 发起任务的原始人类用户（original_principal）。
        session_id: 所属会话 ID，用于把多次任务关联到同一次交互。
        description: 用户原始请求描述。
        created_at: 任务创建时间（UTC）。
        metadata: 扩展字段，MVP 阶段保留为空。
    """

    task_id: str
    user_id: str
    session_id: str
    description: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict)
