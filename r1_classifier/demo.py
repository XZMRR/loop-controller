"""演示：模拟一次真实的 R1 Agent 调用链。

对应文档《05_mvp_core_abstractions.md》第 2.1 节研究助手场景：
"调研 OpenAI 最新模型在企业合规方面的争议，读取内部知识库《AI 合规 checklist》，
写一份 500 字摘要，发邮件给张经理。"

运行：python -m r1_classifier.demo
"""

from __future__ import annotations

from r1_classifier.agent import PlannedAction, ResearchAgent
from r1_classifier.classifier import RuleBasedClassifier
from r1_classifier.models import CapabilityProfile, Task, ToolPermission


class ScriptedResearchAgent(ResearchAgent):
    """规划打桩：按 MVP 场景硬编码动作序列，模拟 LLM 规划的输出。"""

    def plan(self, task: Task) -> list[PlannedAction]:
        return [
            PlannedAction(
                "web_search",
                {"query": "OpenAI 合规争议"},
                "调研 OpenAI 最新模型在企业合规方面的争议",
            ),
            PlannedAction(
                "read_file",
                {"path": "C:/kb/ai_checklist.md"},
                "读取内部知识库《AI 合规 checklist》",
            ),
            PlannedAction(
                "write_file",
                {"path": "C:/kb/summary.md"},
                "写入 500 字摘要",
            ),
            PlannedAction(
                "send_email",
                {"to": "zhang@company.com"},
                "发送摘要给张经理",
            ),
        ]


def main() -> None:
    task = Task(
        task_id="t1",
        user_id="u1",
        agent_id="researcher_001",
        description="调研 OpenAI 合规争议，读 checklist，写摘要并发邮件给张经理",
    )
    agent = ScriptedResearchAgent(
        agent_id="researcher_001",
        name="Research Assistant",
        profile_id="profile_researcher",
        owner_id="o1",
    )
    profile = CapabilityProfile(
        profile_id="profile_researcher",
        version="1.0",
        description="研究助手岗位说明书",
        tools={
            "web_search": ToolPermission(tool_name="web_search", allowed=True),
            "read_file": ToolPermission(tool_name="read_file", allowed=True),
            "write_file": ToolPermission(tool_name="write_file", allowed=True),
            "send_email": ToolPermission(tool_name="send_email", allowed=True),
        },
    )
    classifier = RuleBasedClassifier()

    print("User")
    print(f'  │ "{task.description}"')
    print("  ▼")
    print(f"Task(task_id={task.task_id}, user_id={task.user_id}, agent_id={agent.agent_id})")
    print("  ▼")
    print(f"R1 Agent ({agent.name})")
    print("  │ 1. 解析任务，规划动作序列")
    print("  │ 2. 轻量分类器预检（RuleBasedClassifier）生成 RiskSignal")
    print("  │ 3. Agent 二次封装 ActionProposal（写入 risk_level）")
    print("  │ 4. 提交 R2 Checkpoint")

    submissions = agent.run(task, profile, classifier)
    for proposal, signal, decision in submissions:
        print("  │")
        print(
            f"  ▼ ActionProposal(call_id={proposal.call_id[:8]}…, tool={proposal.tool_name}, "
            f"args={proposal.arguments}, risk_level={proposal.risk_level.value})"
        )
        print(f"     RiskSignal(tags={signal.tags}, reason='{signal.reason}')")
        print(f"     R2 {decision.verdict}: {decision.reason}")

    print("  ▼")
    print("R3 Audit：异步采集全流程 AuditEvent（task_start → propose → classify → evaluate → …）")


if __name__ == "__main__":
    main()
