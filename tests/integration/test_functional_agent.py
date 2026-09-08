"""函数式 Agent 集成测试：验证 @governed 与 hook_tool_registry 端到端行为。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from loop_controller.agent_sdk import (
    GovernanceDeniedError,
    GovernanceRuntime,
    governed,
)
from loop_controller.models import ApprovalRecord, ApprovalRequest, GovernanceResult
from loop_controller.session import Session


@pytest.mark.integration
@pytest.mark.asyncio
async def test_governed_routes_call_to_controller(simple_controller: Any) -> None:
    """@governed 把工具调用路由到 Loop Controller 并返回执行结果。"""
    rt = GovernanceRuntime(simple_controller, agent_id="integration_agent", user_id="alice")
    GovernanceRuntime.set_current(rt)

    @governed(tool_name="add")
    async def add(a: int, b: int) -> int:
        # 注意：当前实现下，Loop Controller 会执行 local_functions.yaml 注册的函数，
        # 这里的函数体仅作为 Agent 侧的类型化接口。
        return -1

    try:
        result = await add(1, 2)
        assert result == 3
    finally:
        await rt.aclose()


@pytest.mark.integration
def test_governed_sync_denied_for_unknown_tool(simple_controller: Any) -> None:
    """未在 profiles 中注册的工具会被拒绝。"""
    rt = GovernanceRuntime(simple_controller, agent_id="integration_agent", user_id="alice")
    GovernanceRuntime.set_current(rt)

    @governed(tool_name="not_allowed_tool")
    def not_allowed_tool(x: str) -> str:
        return x

    try:
        with pytest.raises(GovernanceDeniedError) as exc_info:
            not_allowed_tool("hello")
        assert exc_info.value.result.status == "deny"
    finally:
        # aclose 是 async，同步测试里手动重置即可
        GovernanceRuntime.reset_current()


@pytest.mark.integration
def test_hook_tool_registry_dict(simple_controller: Any) -> None:
    """hook_tool_registry 能为字典注册表批量包装工具。"""
    rt = GovernanceRuntime(simple_controller, agent_id="integration_agent", user_id="alice")
    GovernanceRuntime.set_current(rt)

    def add(a: int, b: int) -> int:
        return -1

    registry: dict[str, Any] = {"tools": {"add": add}}
    rt.hook_tool_registry(registry)

    try:
        assert registry["tools"]["add"](2, 3) == 5
    finally:
        GovernanceRuntime.reset_current()


@pytest.mark.integration
def test_hook_tool_registry_object(simple_controller: Any) -> None:
    """hook_tool_registry 支持面向对象注册表（list_tools/get）。"""
    rt = GovernanceRuntime(simple_controller, agent_id="integration_agent", user_id="alice")
    GovernanceRuntime.set_current(rt)

    class ToolRegistry:
        def __init__(self) -> None:
            self._tools: dict[str, Any] = {}

        def register(self, name: str, fn: Any) -> None:
            self._tools[name] = fn

        def list_tools(self) -> list[str]:
            return list(self._tools.keys())

        def get(self, name: str) -> Any:
            return self._tools[name]

    def echo(text: str) -> str:
        return "placeholder"

    registry = ToolRegistry()
    registry.register("echo", echo)
    rt.hook_tool_registry(registry)

    try:
        assert registry.get("echo")("hello") == "hello"
    finally:
        GovernanceRuntime.reset_current()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_approval_flow_with_governed(approval_controller: Any) -> None:
    """敏感工具触发审批，审批通过后返回结果。"""
    rt = GovernanceRuntime(approval_controller, agent_id="integration_agent", user_id="alice")
    GovernanceRuntime.set_current(rt)

    @governed(tool_name="send_email")
    async def send_email(to: str, subject: str, body: str) -> dict[str, str]:
        return {"status": "unsent"}

    try:
        result = await send_email("bob@company.com", "hi", "body")
        assert isinstance(result, GovernanceResult)
        assert result.status == "require_approval"

        # 模拟审批
        store = approval_controller._runtime.approval_manager._store
        request = store.get_request(result.decision.decision_id)
        store.record_response(
            ApprovalRecord(
                request_id=request.request_id,
                decision_id=request.decision_id,
                verdict="approve",
                approver_id="zhang_manager",
                comment="approved",
            )
        )

        final = await approval_controller.resume_after_approval(result.request_id)
        assert final.status == "allow"
        assert final.content["status"] == "sent"
    finally:
        await rt.aclose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_audit_records_tool_invocation(simple_controller: Any) -> None:
    """工具调用后审计日志包含 propose/evaluate/execute 事件。"""
    rt = GovernanceRuntime(simple_controller, agent_id="integration_agent", user_id="alice")
    GovernanceRuntime.set_current(rt)

    @governed(tool_name="echo")
    async def echo(text: str) -> str:
        return text

    try:
        await echo("audit-me")
        # 直接读取底层 audit store
        events = list(simple_controller._runtime.audit_store.list_recent(limit=20))
        targets = [e.target for e in events if e.target == "echo"]
        assert len(targets) >= 1
        # 至少包含 propose 事件
        assert any(e.action == "propose" for e in events if e.target == "echo")
    finally:
        await rt.aclose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_agent_multi_step_workflow(simple_controller: Any) -> None:
    """模拟真实 Agent 连续调用多个 @governed 工具，验证治理与审计贯穿全程。"""
    rt = GovernanceRuntime(simple_controller, agent_id="integration_agent", user_id="alice")
    GovernanceRuntime.set_current(rt)

    @governed(tool_name="add")
    async def add(a: int, b: int) -> int:
        return -1

    @governed(tool_name="echo")
    async def echo(text: str) -> str:
        return text

    try:
        # Agent 第一步：计算
        sum_result = await add(2, 3)
        assert sum_result == 5

        # Agent 第二步：基于上一步结果继续调用
        echo_result = await echo(f"sum is {sum_result}")
        assert echo_result == "sum is 5"

        # 验证两次调用都留下审计记录
        events = list(simple_controller._runtime.audit_store.list_recent(limit=50))
        targets = {e.target for e in events}
        assert "add" in targets
        assert "echo" in targets

        # 验证至少包含 propose 与执行阶段事件
        add_events = [e for e in events if e.target == "add"]
        assert any(e.action == "propose" for e in add_events)
        assert any(e.action == "execution_authorized" for e in add_events)
        assert any(e.action == "execution_completed" for e in add_events)
    finally:
        await rt.aclose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_governed_session_isolation(simple_controller: Any) -> None:
    """不同 session_id 的工具调用在审计中可区分。"""
    rt = GovernanceRuntime(simple_controller, agent_id="integration_agent", user_id="alice")
    GovernanceRuntime.set_current(rt)

    # 预先创建两个 session，供后续工具调用复用
    backend = simple_controller._runtime.session_manager._backend
    now = datetime.now(UTC)
    session_a = Session(
        session_id="session-a",
        user_id="alice",
        agent_id="integration_agent",
        created_at=now,
        last_task_at=now,
    )
    session_b = Session(
        session_id="session-b",
        user_id="alice",
        agent_id="integration_agent",
        created_at=now,
        last_task_at=now,
    )
    backend.put(session_a)
    backend.put(session_b)

    @governed(tool_name="echo")
    async def echo(text: str) -> str:
        return text

    try:
        await echo("session-a", _loop_controller_session_id=session_a.session_id)
        await echo("session-b", _loop_controller_session_id=session_b.session_id)

        events = list(simple_controller._runtime.audit_store.list_recent(limit=50))
        sessions = {e.session_id for e in events if e.target == "echo"}
        assert session_a.session_id in sessions
        assert session_b.session_id in sessions
    finally:
        await rt.aclose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_governed_local_function_raises_error(boundary_controller: Any) -> None:
    """本地函数抛异常时，@governed 抛出 GovernanceDeniedError 并携带错误原因。"""
    rt = GovernanceRuntime(boundary_controller, agent_id="integration_agent", user_id="alice")
    GovernanceRuntime.set_current(rt)

    @governed(tool_name="raise_error")
    async def raise_error(message: str) -> str:
        return "placeholder"

    try:
        with pytest.raises(GovernanceDeniedError) as exc_info:
            await raise_error("boom")
        assert exc_info.value.result.status in {"error", "blocked"}
        assert "boom" in exc_info.value.result.reason or "boom" in str(
            exc_info.value.result.content
        )
    finally:
        await rt.aclose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_governed_local_function_timeout(boundary_controller: Any) -> None:
    """本地函数执行超时返回 error 状态。"""
    rt = GovernanceRuntime(boundary_controller, agent_id="integration_agent", user_id="alice")
    GovernanceRuntime.set_current(rt)

    @governed(tool_name="hang_forever")
    async def hang_forever(seconds: float) -> str:
        return "placeholder"

    try:
        with pytest.raises(GovernanceDeniedError) as exc_info:
            await hang_forever(30.0)
        assert exc_info.value.result.status in {"error", "blocked"}
        reason = str(exc_info.value.result.reason).lower()
        content = str(exc_info.value.result.content).lower()
        assert "timeout" in reason or "timeout" in content or "超时" in reason or "超时" in content
    finally:
        await rt.aclose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_governed_wait_for_approval_auto_retry(approval_controller: Any) -> None:
    """@governed(wait_for_approval=True) 自动等待审批并返回执行结果。"""
    rt = GovernanceRuntime(approval_controller, agent_id="integration_agent", user_id="alice")
    GovernanceRuntime.set_current(rt)

    @governed(tool_name="send_email", wait_for_approval=True)
    async def send_email(to: str, subject: str, body: str) -> dict[str, str]:
        return {"status": "unsent"}

    approval_manager = approval_controller._runtime.approval_manager
    original_submit = approval_manager.submit

    async def approve_after_submit(request: ApprovalRequest) -> None:
        await asyncio.sleep(0.05)
        approval_manager._store.record_response(
            ApprovalRecord(
                request_id=request.request_id,
                decision_id=request.decision_id,
                verdict="approve",
                approver_id="zhang_manager",
                comment="approved",
            )
        )

    async def patched_submit(request: ApprovalRequest) -> None:
        await original_submit(request)
        asyncio.create_task(approve_after_submit(request))

    try:
        approval_manager.submit = patched_submit
        result = await send_email("bob@company.com", "hi", "body")
        assert result["status"] == "sent"
    finally:
        approval_manager.submit = original_submit
        await rt.aclose()
