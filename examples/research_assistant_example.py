"""研究助手端到端示例.

本示例演示 Loop Controller 的 R0-R3 最小闭环：
1. 用户输入研究任务；
2. R1 规划动作序列；
3. R1 轻量分类器对每个 ActionProposal 预检；
4. R2 Checkpoint 对每个 ActionProposal 做策略判定；
5. require_approval 的动作提交给 R0-delegate 审批；
6. allow 的动作由 R2 通过 MCP Gateway 代理转发执行；
7. R3 审计日志记录全流程。

注意：本示例中的 `ResearchAssistant` 是一个实现层面的编排辅助类，
用于把用户任务拆成 ActionProposal 序列。它不是架构文档中定义的
核心抽象；真正的 Agent 执行循环由上层应用实现。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from uuid import uuid4

from loop_controller import (
    ActionProposal,
    Agent,
    CapabilityProfile,
    Checkpoint,
    CheckpointConfig,
    ConfigR0Delegate,
    JsonlAuditLogger,
    MockPolicyEngine,
    OPAPolicyEngine,
    RuleBasedClassifier,
    Task,
)


class ResearchAssistant:
    """MVP 研究助手编排辅助类.

    仅用于把用户任务拆成动作序列并提交给 R2；不直接调用外部工具。
    """

    def __init__(self, agent: Agent, classifier: RuleBasedClassifier, checkpoint: Checkpoint) -> None:
        """初始化.

        Args:
            agent: 执行 Agent。
            classifier: R1 轻量分类器。
            checkpoint: R2 Checkpoint。
        """
        self.agent = agent
        self.classifier = classifier
        self.checkpoint = checkpoint

    def plan_actions(self, task: Task) -> list[ActionProposal]:
        """根据任务规划动作序列（MVP 硬编码模拟）."""
        return [
            ActionProposal(
                task_id=task.task_id,
                call_id=f"{task.task_id}-call-1",
                agent_id=self.agent.agent_id,
                tool_name="read_file",
                arguments={"path": "/allowed/ai_compliance_checklist.md"},
                task_context="读取内部合规检查清单",
            ),
            ActionProposal(
                task_id=task.task_id,
                call_id=f"{task.task_id}-call-2",
                agent_id=self.agent.agent_id,
                tool_name="web_search",
                arguments={"query": "OpenAI enterprise compliance controversy 2026"},
                task_context="搜索公开资料",
            ),
            ActionProposal(
                task_id=task.task_id,
                call_id=f"{task.task_id}-call-3",
                agent_id=self.agent.agent_id,
                tool_name="write_file",
                arguments={"path": "/tmp/summary.md", "content": "# 研究摘要\n\n（此处为占位内容）"},
                task_context="将搜索结果写入本地摘要文件",
            ),
            ActionProposal(
                task_id=task.task_id,
                call_id=f"{task.task_id}-call-4",
                agent_id=self.agent.agent_id,
                tool_name="send_email",
                arguments={"to": "zhang@company.com", "subject": "AI 合规研究摘要", "body": "请查收附件"},
                task_context="将摘要发送给张经理",
            ),
        ]

    def execute_task(self, task: Task, profile: CapabilityProfile) -> None:
        """执行任务闭环."""
        proposals = self.plan_actions(task)

        for proposal in proposals:
            print(f"\n[Propose] {proposal.tool_name}: {proposal.task_context}")

            # R1：轻量分类器预检
            signal = self.classifier.classify(task, self.agent, proposal, profile)
            print(f"[Classify] risk_level={signal.risk_level}, tags={signal.tags}")

            # R2：策略判定
            decision = self.checkpoint.evaluate(task, self.agent, proposal)
            print(f"[Evaluate] verdict={decision.verdict}, reason={decision.reason}")

            if decision.verdict == "require_approval":
                # R0-delegate 审批
                decision = self.checkpoint.request_and_apply_approval(
                    task, self.agent, proposal, decision
                )
                print(f"[Approval] final verdict={decision.verdict}, reason={decision.reason}")

            if decision.verdict in ("allow", "modify"):
                # R2 代理转发工具调用
                result = self.checkpoint.forward(proposal, decision)
                print(f"[Execute] status={result.status}, content={result.content}")
            else:
                print(f"[Blocked] reason={decision.reason}")


def main() -> None:
    """运行研究助手端到端示例."""
    # 策略引擎选择：默认 Mock，环境变量 LOOP_CONTROLLER_POLICY_ENGINE=opa 时使用 OPA
    policy_engine_name = os.getenv("LOOP_CONTROLLER_POLICY_ENGINE", "mock").lower()
    if policy_engine_name == "opa":
        policy_engine = OPAPolicyEngine()
        print("[Config] Using OPA policy engine at http://127.0.0.1:8181")
    else:
        policy_engine = MockPolicyEngine()
        print("[Config] Using Mock policy engine")

    # R0 配置：固定审批人自动批准（MVP 打桩）
    r0_delegate = ConfigR0Delegate(approver_id="r0_boss", auto_approve=True)

    # R3 配置：JSONL 审计日志
    log_path = Path(tempfile.gettempdir()) / "loop_controller_audit.jsonl"
    audit_logger = JsonlAuditLogger(log_path)

    # R2 配置：PolicyEngine + Checkpoint
    # 风险阈值等可通过环境变量配置，如 LOOP_CONTROLLER_RISK_DENIED_THRESHOLD=3
    checkpoint = Checkpoint(
        policy_engine=policy_engine,
        profile_store={},
        r0_delegate=r0_delegate,
        audit_logger=audit_logger,
        config=CheckpointConfig.from_env(),
    )

    # R1 配置：Agent + CapabilityProfile + 轻量分类器
    profile = CapabilityProfile(
        profile_id="researcher_profile",
        allowed_tools=["read_file", "web_search", "write_file", "send_email"],
    )
    checkpoint.profile_store["researcher_profile"] = profile

    agent = Agent(
        agent_id="researcher_001",
        name="Research Assistant",
        profile_id="researcher_profile",
        owner_id="user_alice",
    )
    classifier = RuleBasedClassifier()
    assistant = ResearchAssistant(agent, classifier, checkpoint)

    # 用户任务
    task = Task(
        task_id=str(uuid4()),
        user_id="user_alice",
        session_id=str(uuid4()),
        description="调研 OpenAI 企业合规争议，读取内部检查清单，写摘要发给张经理",
    )

    print("=" * 60)
    print(f"Task: {task.description}")
    print("=" * 60)

    assistant.execute_task(task, profile)

    print("\n" + "=" * 60)
    print(f"Audit log written to: {log_path}")
    events = audit_logger.read_events()
    print(f"Total audit events: {len(events)}")
    for event in events:
        print(f"  - {event['action']} {event['target']} -> {event['decision']}")


if __name__ == "__main__":
    main()
