"""R1 Agent 编排层：模仿真实的 agent 调用链。

对齐文档《05_mvp_core_abstractions.md》第 4 节时序图：

  R1 Agent：
    1. 解析任务，规划动作序列（MVP 打桩，未来可替换 LLM 规划）
    2. 工具调用前，轻量分类器预检（RuleBasedClassifier）生成 RiskSignal
    3. Agent 接收 RiskSignal，二次封装 ActionProposal（写入 risk_level 等申报元数据）
    4. 提交 R2 Checkpoint 校验（本模块提供 mock checkpoint 打桩）

注意：R3 审计不在本调用链内，由 R3 异步、只读地采集 AuditEvent。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any
from uuid import uuid4

from r1_classifier.classifier import LightweightClassifier
from r1_classifier.models import (
    ActionProposal,
    Agent,
    CapabilityProfile,
    RiskLevel,
    RiskSignal,
    Task,
)


@dataclass(frozen=True)
class PlannedAction:
    """Agent 规划出的一个候选动作（模拟 LLM 规划的输出）。"""

    tool_name: str
    arguments: dict[str, Any]
    reason: str


@dataclass(frozen=True)
class Decision:
    """R2 判定结果（MVP mock 打桩）。"""

    verdict: str  # allow / deny / require_approval
    reason: str
    decision_id: str


class ResearchAgent(Agent):
    """MVP 研究助手 Agent：规划 -> 预检 -> 封装 -> 申报 R2。"""

    def plan(self, task: Task) -> list[PlannedAction]:
        """根据任务规划动作序列。MVP 打桩由子类覆写，未来替换为 LLM 规划。"""
        raise NotImplementedError

    def run(
        self,
        task: Task,
        profile: CapabilityProfile,
        classifier: LightweightClassifier,
    ) -> list[tuple[ActionProposal, RiskSignal, Decision]]:
        """执行一次任务，返回每个动作的（申报单, 风险信号, R2 判定）。"""
        submissions: list[tuple[ActionProposal, RiskSignal, Decision]] = []
        for action in self.plan(task):
            # 1. 构造候选申报单（call_id 由 R1 生成，供 R2 防重放校验）
            proposal = ActionProposal(
                task_id=task.task_id,
                call_id=str(uuid4()),
                agent_id=self.agent_id,
                tool_name=action.tool_name,
                arguments=action.arguments,
                task_context=task.description,
                reason=action.reason,
            )

            # 2. 工具调用前：轻量分类器预检
            signal = classifier.classify(task, self, proposal, profile)

            # 3. Agent 二次封装：把 RiskSignal.risk_level 写入申报单
            proposal = replace(proposal, risk_level=signal.risk_level)

            # 4. 提交 R2 Checkpoint
            decision = mock_r2_checkpoint(proposal, profile)
            submissions.append((proposal, signal, decision))
        return submissions


def mock_r2_checkpoint(proposal: ActionProposal, profile: CapabilityProfile) -> Decision:
    """MVP 打桩：R2 最小校验，仅演示申报链路，不是真实策略引擎。

    真实 R2 需组合 PolicyEngine / CapabilityProfile / BudgetLedger 等。
    """
    perm = profile.tools.get(proposal.tool_name)
    if perm is None or not perm.allowed:
        return Decision("deny", f"工具 '{proposal.tool_name}' 未在 profile 中授权", str(uuid4()))
    if proposal.risk_level == RiskLevel.CRITICAL:
        return Decision(
            "require_approval",
            f"risk_level=critical 需人工审批（call_id={proposal.call_id}）",
            str(uuid4()),
        )
    return Decision("allow", f"已通过 MVP 打桩校验（call_id={proposal.call_id}）", str(uuid4()))
