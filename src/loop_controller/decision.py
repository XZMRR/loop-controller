"""R2 判定结果定义.

Decision 是 R2 Checkpoint 对 ActionProposal 的权威判定，
包含 allow / deny / modify / require_approval 四种状态。
真正的外部工具调用必须持有有效的 Decision。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal


@dataclass(frozen=True)
class Decision:
    """R2 对 ActionProposal 的判定结果.

    Attributes:
        decision_id: R2 签发的判定唯一 ID，用于防止伪造和重放。
        call_id: 关联的 ActionProposal.call_id。
        task_id: 关联的任务 ID。
        verdict: 判定结果，allow / deny / modify / require_approval。
        modified_args: verdict 为 modify 时回写的安全参数。
        reason: 判定原因，用于审计和解释。
        expires_at: Decision 过期时间。
        max_uses: Decision 最多可使用次数，MVP 为 1。
        escalation_target: require_approval 时指向 R0-delegate。
    """

    decision_id: str
    call_id: str
    task_id: str
    verdict: Literal["allow", "deny", "modify", "require_approval"]
    reason: str
    expires_at: datetime
    modified_args: dict[str, Any] | None = None
    max_uses: int = 1
    escalation_target: str | None = None
