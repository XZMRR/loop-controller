"""动作申报定义.

ActionProposal 是 R1 向 R2 提交的拟执行动作。R2 收到后，
会基于 Policy、CapabilityProfile、预算等给出最终 Decision。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class ActionProposal:
    """R1 向 R2 的动作申报.

    Attributes:
        task_id: 所属任务 ID。
        call_id: R1 生成的候选调用 ID，R2 会校验其唯一性。
        agent_id: 执行 Agent ID。
        tool_name: Loop Controller 内部规范化工具名。
        arguments: 工具调用参数。
        task_context: 当前任务上下文，便于 R2 做策略判定。
        type: 动作类型；MVP 只处理 tool_call，inter_agent 结构预留。
        risk_level: R1 轻量分类器输出的风险等级，仅作参考。
        reason: Agent 说明为什么要做这个动作。
    """

    task_id: str
    call_id: str
    agent_id: str
    tool_name: str
    arguments: dict[str, Any]
    task_context: str
    type: Literal["tool_call", "inter_agent"] = "tool_call"
    risk_level: Literal["low", "medium", "high", "critical"] = "low"
    reason: str = ""
