"""Runtime 组装 + R1 执行循环（§5.2）。

``Runtime`` 是运行时依赖容器；``run_task`` 是单任务执行循环。
迭代 2 已接通 DecisionStore 持久化、R0-delegate 审批打桩；
T3.1/T3.2 补全哈希链、args_hash/args_mask 与分级掩码。
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loop_controller.budget import InMemoryBudgetLedger
from loop_controller.checkpoint import Checkpoint
from loop_controller.classifier import LightweightClassifier, RuleBasedClassifier
from loop_controller.infra.audit_store import AuditStore, JsonlAuditStore
from loop_controller.infra.config_loader import AppConfig
from loop_controller.infra.decision_store import JsonlDecisionStore
from loop_controller.infra.identity import ConfigIdentityProvider
from loop_controller.infra.policy_store import FilePolicyStore
from loop_controller.masker import Masker
from loop_controller.mcp_gateway import MCPGateway
from loop_controller.permission_interaction import ConfigPermissionInteractionAnalyzer
from loop_controller.models import (
    ActionProposal,
    Agent,
    AuditAction,
    AuditEvent,
    BudgetCost,
    Decision,
    RiskSignal,
    Task,
    ToolResult,
)
from loop_controller.planner import Planner, ScriptedPlanner
from loop_controller.policy_engine import OPAPolicyEngine
from loop_controller.r0_delegate import ConfigR0Delegate, R0Delegate
from loop_controller.utils.canonical import canonical_json

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Runtime：运行时依赖容器
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Runtime:
    """R1/R2/R3 运行时依赖容器（§5.2）。"""

    planner: Planner
    classifier: LightweightClassifier
    checkpoint: Checkpoint
    gateway: MCPGateway
    r0_delegate: R0Delegate
    audit_store: AuditStore
    masker: Masker
    profiles: dict[str, Any]  # CapabilityProfile

    async def start(self) -> None:
        """拉起 MCP server 子进程等异步初始化。"""
        await self.gateway.start()

    async def aclose(self) -> None:
        """关闭 MCP server 子进程。"""
        await self.gateway.aclose()


# ---------------------------------------------------------------------------
# Runtime 工厂
# ---------------------------------------------------------------------------


def build_runtime(
    config: AppConfig,
    *,
    opa_url: str = "http://127.0.0.1:8181",
    planner_yaml: str | Path | None = None,
) -> Runtime:
    """从 ``AppConfig`` 组装 Runtime。

    Args:
        config: 经 ``ConfigLoader.load`` 加载并校验后的配置。
        opa_url: OPA sidecar HTTP 地址。
        planner_yaml: ScriptedPlanner 脚本路径；缺省使用 ``config/scriptured_plan.yaml``。
    """
    identity = ConfigIdentityProvider(config.agents, config.users)
    policy_store = FilePolicyStore(config.policy_dir)
    policy_engine = OPAPolicyEngine(base_url=opa_url, timeout=2.0)

    project_root = Path(config.policy_dir).parent
    gateway = MCPGateway(
        mcp_servers=dict(config.mcp_servers),
        tool_mapping=config.tool_mapping,
        env_extra={"PYTHONPATH": str(project_root / "src")},
        cwd=str(project_root),
    )
    masker = Masker(config.masking_rules)
    checkpoint = Checkpoint(
        profiles=config.profiles,
        policy_engine=policy_engine,
        policy_store=policy_store,
        gateway=gateway,
        identity=identity,
        decision_store=JsonlDecisionStore(config.decision_log_path),
        budget_ledger=InMemoryBudgetLedger(),
        permission_analyzer=ConfigPermissionInteractionAnalyzer(config.permission_rules),
        tool_costs={
            name: BudgetCost(token_count=entry.cost_per_call)
            for name, entry in config.tool_mapping.items()
        },
        masker=masker,
    )
    audit_store = JsonlAuditStore(config.audit_log_path)

    if planner_yaml is None:
        planner_yaml = project_root / "config" / "scripted_plan.yaml"
    planner = ScriptedPlanner.from_yaml(planner_yaml)

    return Runtime(
        planner=planner,
        classifier=RuleBasedClassifier(),
        checkpoint=checkpoint,
        gateway=gateway,
        r0_delegate=ConfigR0Delegate(config.approval),
        audit_store=audit_store,
        masker=masker,
        profiles=config.profiles,
    )


# ---------------------------------------------------------------------------
# 执行循环（§5.2）
# ---------------------------------------------------------------------------


def _audit_event(
    task: Task,
    *,
    action: AuditAction,
    proposal: ActionProposal | None = None,
    decision: Decision | None = None,
    result: ToolResult | None = None,
    signal: RiskSignal | None = None,
    record=None,
    reason: str | None = None,
    masker: Masker | None = None,
) -> AuditEvent:
    """构造审计事件；含 args_hash（原始参数规范 JSON 摘要）与 args_mask（审计档掩码）。"""
    target = proposal.tool_name if proposal else None
    metadata: dict[str, Any] = {}
    if signal and signal.suggestion:
        metadata["classifier_suggestion"] = signal.suggestion
    if record and hasattr(record, "comment"):
        metadata["approval_comment"] = record.comment

    args_hash: str | None = None
    args_mask: dict | None = None
    if proposal is not None:
        args_hash = hashlib.sha256(
            canonical_json(proposal.arguments).encode("utf-8")
        ).hexdigest()
        if masker is not None:
            args_mask = masker.mask(proposal.arguments, "audit_log")

    return AuditEvent(
        event_id=uuid.uuid4().hex,
        trace_id=task.task_id,
        session_id=task.session_id,
        call_id=proposal.call_id if proposal else None,
        actor_type="agent" if action in ("propose",) else "checkpoint",
        actor_id=task.agent_id,
        action=action,
        target=target,
        decision=decision.verdict if decision else None,
        reason=reason
        or (decision.reason if decision else None)
        or (result.content if result else None),
        args_hash=args_hash,
        args_mask=args_mask,
        policy_version=decision.policy_version if decision else None,
        profile_version=decision.profile_version if decision else None,
        metadata=metadata,
    )


def _blocked_result(proposal: ActionProposal, decision: Decision) -> ToolResult:
    """被治理链路拦截的结果。"""
    return ToolResult(
        call_id=proposal.call_id,
        task_id=proposal.task_id,
        tool_name=proposal.tool_name,
        status="blocked",
        content=decision.reason,
        error_code="denied_by_checkpoint",
    )


def _blocked_decision(decision: Decision, proposal: ActionProposal, reason: str) -> Decision:
    """审批请求组装失败（如审批人冲突）时生成 deny Decision。"""
    return decision.model_copy(
        update={
            "verdict": "deny",
            "reason": reason,
            "expires_at": decision.expires_at,  # 保持原过期时间，但 max_uses=0 不可执行
            "max_uses": 0,
        }
    )


async def run_task(task: Task, agent: Agent, runtime: Runtime) -> None:
    """R1 执行循环（§5.2）：Planner → Classifier → Checkpoint.evaluate → forward/拦截。"""
    audit = runtime.audit_store
    profile = runtime.profiles[agent.profile_id]
    observations: list[ToolResult] = []

    audit.append(_audit_event(task, action="task_start", masker=runtime.masker))
    try:
        while True:
            # 1. 规划下一步动作：Planner 只产出草案，框架组装 ActionProposal 并统一生成 call_id
            #    （v1.1 评审#7/#8：Planner 无权自定身份标识）
            planned = runtime.planner.next_action(task, agent, observations)
            if planned is None:
                break
            proposal = ActionProposal(
                task_id=task.task_id,
                call_id=uuid.uuid4().hex,  # run_task 框架统一生成
                agent_id=agent.agent_id,
                tool_name=planned.tool_name,
                arguments=dict(planned.arguments),
                task_context=task.description[:200],  # §5.3 纯截断，不做摘要改写
                reason=planned.reason,
            )

            # 2. R1 自检：轻量分类器，只产出信号
            signal = runtime.classifier.classify(task, agent, proposal, profile)
            proposal = proposal.model_copy(
                update={"risk_level": signal.risk_level, "risk_tags": signal.tags}
            )
            audit.append(
                _audit_event(
                    task,
                    action="propose",
                    proposal=proposal,
                    signal=signal,
                    reason=signal.reason,
                    masker=runtime.masker,
                )
            )

            # 3. R2 判定
            decision = await runtime.checkpoint.evaluate(task, agent, proposal)
            audit.append(
                _audit_event(
                    task,
                    action="evaluate",
                    proposal=proposal,
                    decision=decision,
                    masker=runtime.masker,
                )
            )

            # 4. 需要审批 → 请求 R0-delegate（async 接口，MVP 实现立即返回）
            if decision.verdict == "require_approval":
                try:
                    request = runtime.checkpoint.build_approval_request(decision, proposal, task)
                except Exception as exc:  # noqa: BLE001 - 冲突校验失败按 deny 处理
                    decision = _blocked_decision(decision, proposal, str(exc))
                else:
                    record = await runtime.r0_delegate.request_approval(request)
                    audit.append(
                        _audit_event(
                            task,
                            action="approve" if record.verdict == "approve" else "deny",
                            proposal=proposal,
                            record=record,
                            masker=runtime.masker,
                        )
                    )
                    decision = runtime.checkpoint.finalize_after_approval(decision, record)

            # 5. 执行或被拦截，结果都进入 observations 供下一步规划
            if decision.verdict in ("allow", "modify"):
                result = await runtime.checkpoint.forward(proposal, decision)
            else:
                result = _blocked_result(proposal, decision)
            observations.append(result)
            audit.append(
                _audit_event(
                    task,
                    action="execute",
                    proposal=proposal,
                    result=result,
                    masker=runtime.masker,
                )
            )
    finally:
        audit.append(_audit_event(task, action="task_end", masker=runtime.masker))
        runtime.checkpoint.forget_task(task.task_id)
