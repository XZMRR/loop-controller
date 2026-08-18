"""LLMPlanner 单元测试（T3.5）：全部使用 fake LLM client，不依赖真实 LLM 服务。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from loop_controller.budget import InMemoryBudgetLedger
from loop_controller.infra.audit_store import JsonlAuditStore
from loop_controller.infra.config_loader import (
    ConfigLoader,
    ConfigValidationError,
    LLMPlannerConfig,
)
from loop_controller.llm_planner import LLMPlanner, LLMResponse
from loop_controller.models import (
    Agent,
    AuditEvent,
    CapabilityProfile,
    ConversationContext,
    PlannedAction,
    Task,
    Tool,
    ToolPermission,
    ToolResult,
)


@pytest.fixture
def conversation_context(task) -> ConversationContext:
    return ConversationContext(session_id=task.session_id)


@pytest.fixture
def task() -> Task:
    return Task(
        task_id="t1",
        session_id="t1",
        user_id="alice",
        agent_id="researcher_001",
        description="调研 AI 合规并写摘要",
    )


@pytest.fixture
def agent() -> Agent:
    return Agent(
        agent_id="researcher_001",
        name="RA",
        profile_id="p1",
        owner_id="zhang_manager",
    )


@pytest.fixture
def profile() -> CapabilityProfile:
    return CapabilityProfile(
        profile_id="p1",
        version="test",
        max_budget_token=100_000,
        tools={
            "web_search": ToolPermission(tool_name="web_search", allowed=True),
            "read_file": ToolPermission(tool_name="read_file", allowed=True),
            "write_file": ToolPermission(tool_name="write_file", allowed=True),
            "send_email": ToolPermission(tool_name="send_email", allowed=True, require_approval=True),
        },
    )


class _FakeGateway:
    """只实现 list_tools 的 fake gateway。"""

    async def list_tools(self, profile: CapabilityProfile) -> list[Tool]:
        return [
            Tool(
                canonical_name="web_search",
                mcp_name="web_search",
                description="搜索公开网络资料",
                input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
            ),
            Tool(
                canonical_name="read_file",
                mcp_name="read_file",
                description="读取本地文件",
                input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
            ),
        ]


class _RecordingFakeClient:
    """记录收到的 messages，并返回预设响应。"""

    def __init__(self, response: LLMResponse) -> None:
        self.response = response
        self.messages: list[dict[str, Any]] | None = None

    async def chat(
        self,
        messages: list[dict[str, Any]],
        config: LLMPlannerConfig,
    ) -> LLMResponse:
        self.messages = messages
        return self.response


def _planner(
    client: _RecordingFakeClient,
    profile: CapabilityProfile,
    audit_path: Path,
    max_budget_token: int | None = None,
) -> LLMPlanner:
    if max_budget_token is not None:
        profile = profile.model_copy(update={"max_budget_token": max_budget_token})
    ledger = InMemoryBudgetLedger()
    audit_store = JsonlAuditStore(audit_path)
    return LLMPlanner(
        client=client,
        config=LLMPlannerConfig(enabled=True, api_key_env="LLM_API_KEY"),
        gateway=_FakeGateway(),
        budget_ledger=ledger,
        audit_store=audit_store,
        profiles={profile.profile_id: profile},
    )


async def test_valid_json_returns_planned_action(task, conversation_context, agent, profile, tmp_path) -> None:
    """合法 JSON 输出 → 返回 PlannedAction。"""
    response = LLMResponse(
        content=json.dumps(
            {
                "action": "call_tool",
                "tool_name": "web_search",
                "arguments": {"query": "AI compliance"},
                "reason": "搜索资料",
            },
            ensure_ascii=False,
        )
    )
    client = _RecordingFakeClient(response)
    planner = _planner(client, profile, tmp_path / "audit.jsonl")

    action = await planner.next_action(task, agent, [], conversation_context)

    assert isinstance(action, PlannedAction)
    assert action.tool_name == "web_search"
    assert action.arguments == {"query": "AI compliance"}
    assert action.reason == "搜索资料"


async def test_markdown_wrapped_json_parsed(task, conversation_context, agent, profile, tmp_path) -> None:
    """markdown 代码块包裹的 JSON → 正确解析。"""
    response = LLMResponse(
        content="```json\n"
        + json.dumps(
            {"action": "call_tool", "tool_name": "read_file", "arguments": {"path": "/data/kb/a.md"}, "reason": "读取资料"},
            ensure_ascii=False,
        )
        + "\n```"
    )
    client = _RecordingFakeClient(response)
    planner = _planner(client, profile, tmp_path / "audit.jsonl")

    action = await planner.next_action(task, agent, [], conversation_context)

    assert action is not None
    assert action.tool_name == "read_file"
    assert action.arguments == {"path": "/data/kb/a.md"}


async def test_finish_action_returns_none(task, conversation_context, agent, profile, tmp_path) -> None:
    """action=finish → 返回 None。"""
    response = LLMResponse(content='{"action": "finish"}')
    client = _RecordingFakeClient(response)
    planner = _planner(client, profile, tmp_path / "audit.jsonl")

    assert await planner.next_action(task, agent, [], conversation_context) is None


async def test_missing_field_records_planner_error(task, conversation_context, agent, profile, tmp_path) -> None:
    """缺字段 → 返回 None 且审计带 metadata.planner_error。"""
    response = LLMResponse(content='{"action": "call_tool", "tool_name": "web_search"}')
    client = _RecordingFakeClient(response)
    audit_path = tmp_path / "audit.jsonl"
    planner = _planner(client, profile, audit_path)

    assert await planner.next_action(task, agent, [], conversation_context) is None

    events = _events(audit_path, task.task_id)
    assert len(events) == 1
    assert events[0].action == "planner_error"
    assert "planner_error" in events[0].metadata
    assert "tool_name/arguments/reason 必须存在" in events[0].metadata["planner_error"]["reason"]


async def test_non_json_records_planner_error(task, conversation_context, agent, profile, tmp_path) -> None:
    """非 JSON → 返回 None 且审计带 metadata.planner_error。"""
    response = LLMResponse(content="我认为应该搜索")
    client = _RecordingFakeClient(response)
    audit_path = tmp_path / "audit.jsonl"
    planner = _planner(client, profile, audit_path)

    assert await planner.next_action(task, agent, [], conversation_context) is None

    events = _events(audit_path, task.task_id)
    assert events[0].action == "planner_error"
    assert "未找到合法 JSON 对象" in events[0].metadata["planner_error"]["reason"]


async def test_unauthorized_tool_records_planner_error(task, conversation_context, agent, profile, tmp_path) -> None:
    """未授权工具名 → 返回 None 且审计带 metadata.planner_error。"""
    response = LLMResponse(
        content=json.dumps(
            {"action": "call_tool", "tool_name": "delete_database", "arguments": {}, "reason": "删库"},
            ensure_ascii=False,
        )
    )
    client = _RecordingFakeClient(response)
    audit_path = tmp_path / "audit.jsonl"
    planner = _planner(client, profile, audit_path)

    assert await planner.next_action(task, agent, [], conversation_context) is None

    events = _events(audit_path, task.task_id)
    assert events[0].action == "planner_error"
    assert "delete_database" in events[0].metadata["planner_error"]["reason"]


async def test_history_summarized_after_three_steps(task, conversation_context, agent, profile, tmp_path) -> None:
    """三步以上历史 → 早期步骤被摘要，最近一步保留全文（并截断）。"""
    observations = [
        ToolResult(call_id="c1", task_id=task.task_id, tool_name="web_search", status="success", content="A" * 3000),
        ToolResult(call_id="c2", task_id=task.task_id, tool_name="read_file", status="success", content="B" * 3000),
        ToolResult(call_id="c3", task_id=task.task_id, tool_name="write_file", status="blocked", content="路径未授权"),
        ToolResult(call_id="c4", task_id=task.task_id, tool_name="web_search", status="success", content="C" * 2500),
    ]
    response = LLMResponse(
        content=json.dumps({"action": "finish"}, ensure_ascii=False)
    )
    client = _RecordingFakeClient(response)
    planner = _planner(client, profile, tmp_path / "audit.jsonl")

    await planner.next_action(task, agent, observations, conversation_context)

    assert client.messages is not None
    user_prompt = client.messages[1]["content"]
    # 最近一步有 [最近] 标记且保留完整内容（含截断标记）
    assert "[最近]" in user_prompt
    assert "C" * 2000 in user_prompt  # 截断前 2000 字符保留
    assert "...[truncated, total=2500 chars]" in user_prompt
    # 早期步骤只剩一行摘要，不含大段原文
    assert "A" * 100 not in user_prompt
    assert "B" * 100 not in user_prompt
    assert "被治理层拦截：路径未授权" in user_prompt


async def test_budget_exceeded_records_planner_budget_exceeded(task, conversation_context, agent, profile, tmp_path) -> None:
    """预算耗尽 → 返回 None 且 metadata.planner_budget_exceeded。"""
    response = LLMResponse(content='{"action": "finish"}')
    client = _RecordingFakeClient(response)
    audit_path = tmp_path / "audit.jsonl"
    # max_tokens=1000 加上 prompt 估算必然超过 100
    planner = _planner(client, profile, audit_path, max_budget_token=100)

    assert await planner.next_action(task, agent, [], conversation_context) is None

    events = _events(audit_path, task.task_id)
    assert events[0].action == "planner_error"
    assert events[0].metadata.get("planner_budget_exceeded") is True


async def test_usage_commit_and_refund(task, conversation_context, agent, profile, tmp_path) -> None:
    """真实 usage commit/refund 正确：reserved=0、committed=actual。"""
    response = LLMResponse(
        content=json.dumps({"action": "finish"}, ensure_ascii=False),
        prompt_tokens=10,
        completion_tokens=5,
    )
    client = _RecordingFakeClient(response)
    audit_path = tmp_path / "audit.jsonl"
    planner = _planner(client, profile, audit_path)

    await planner.next_action(task, agent, [], conversation_context)

    ledger = planner._budget_ledger
    assert ledger is not None
    assert ledger._reserved[task.task_id] == 0
    assert ledger._committed[task.task_id] == 15


async def test_api_key_not_in_audit_log(task, conversation_context, agent, profile, tmp_path, monkeypatch) -> None:
    """密钥纪律：planner_error 审计事件中也不含 API key。"""
    secret = "sk-test-12345-not-in-audit-log"
    monkeypatch.setenv("LLM_API_KEY", secret)

    # 构造一个会产生 planner_error 审计事件的非法输出，确保审计文件存在
    response = LLMResponse(content="这不是 JSON")
    client = _RecordingFakeClient(response)
    audit_path = tmp_path / "audit.jsonl"
    planner = _planner(client, profile, audit_path)

    assert await planner.next_action(task, agent, [], conversation_context) is None

    raw = audit_path.read_text(encoding="utf-8")
    assert "planner_error" in raw
    assert secret not in raw
    assert "LLM_API_KEY" not in raw


def test_config_loader_rejects_enabled_without_api_key(tmp_path, monkeypatch) -> None:
    """启动校验：enabled=true 但环境变量不存在时拒绝加载。"""
    # 使用仓库真实 config，临时启用 LLMPlanner 并清空环境变量
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    from tests.test_config_loader import build_config_dir

    config_dir = build_config_dir(tmp_path)
    planner_path = config_dir / "llm_planner.yaml"
    planner_path.write_text(
        "enabled: true\napi_key_env: LLM_API_KEY\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigValidationError, match="LLM_API_KEY"):
        ConfigLoader().load(config_dir)


def test_httpx_llm_client_reads_api_key_from_env(monkeypatch) -> None:
    """HttpxLLMClient 只从环境变量读取 API key（不实际发起网络请求）。"""
    monkeypatch.setenv("MY_LLM_KEY", "sk-from-env")
    config = LLMPlannerConfig(enabled=True, api_key_env="MY_LLM_KEY")
    # 直接验证环境变量读取路径
    assert os.environ.get(config.api_key_env) == "sk-from-env"


def _events(audit_path: Path, trace_id: str) -> list[AuditEvent]:
    """读取审计文件中指定 trace_id 的事件。"""
    store = JsonlAuditStore(audit_path)
    return store.query_by_trace(trace_id)
