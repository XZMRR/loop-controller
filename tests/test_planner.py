"""ScriptedPlanner 单元测试（§5.1）：依序产出草案、序列耗尽返回 None、YAML 加载。"""

from __future__ import annotations

import pytest

from loop_controller.models import Agent, PlannedAction, Task
from loop_controller.planner import ScriptedPlanner


@pytest.fixture
def task() -> Task:
    return Task(task_id="t1", session_id="t1", user_id="alice", agent_id="researcher_001",
                description="调研 AI 合规")


@pytest.fixture
def agent() -> Agent:
    return Agent(agent_id="researcher_001", name="RA", profile_id="p1", owner_id="zhang_manager")


def make_steps() -> list[PlannedAction]:
    return [
        PlannedAction(tool_name="web_search", arguments={"query": "q1"}, reason="调研公开资料"),
        PlannedAction(tool_name="send_email", arguments={"to": "x@y.com"}, reason="发送报告"),
    ]


async def test_next_action_in_order(task, agent) -> None:
    planner = ScriptedPlanner(make_steps())

    first = await planner.next_action(task, agent, [])
    assert first is not None
    assert first.tool_name == "web_search"
    assert first.arguments == {"query": "q1"}
    assert first.reason == "调研公开资料"
    # v1.1（评审#7/#8）：Planner 只输出草案，不含身份字段（由 run_task 统一填充）
    assert not hasattr(first, "call_id")
    assert not hasattr(first, "task_id")
    assert not hasattr(first, "agent_id")
    assert not hasattr(first, "task_context")

    second = await planner.next_action(task, agent, [])
    assert second is not None
    assert second.tool_name == "send_email"

    # 序列耗尽 → None（任务完成）
    assert await planner.next_action(task, agent, []) is None


async def test_from_yaml(tmp_path, task, agent) -> None:
    plan_file = tmp_path / "plan.yaml"
    plan_file.write_text(
        "steps:\n"
        "  - tool_name: web_search\n"
        "    arguments: {query: q1}\n"
        "    reason: r1\n",
        encoding="utf-8",
    )

    planner = ScriptedPlanner.from_yaml(plan_file)
    action = await planner.next_action(task, agent, [])

    assert action is not None
    assert action.tool_name == "web_search"
    assert await planner.next_action(task, agent, []) is None
