"""Shell 执行器测试（v0.24.0）。"""

from __future__ import annotations

import time

import pytest

from loop_controller.executors import ExecutionContext, ShellExecutor
from loop_controller.executors.shell_models import ShellToolSpec
from loop_controller.models import CapabilityProfile


def _fake_context() -> ExecutionContext:
    return ExecutionContext(
        call_id="c1",
        task_id="t1",
        agent_id="a1",
        user_id="u1",
    )


@pytest.fixture
def executor() -> ShellExecutor:
    specs = {
        "echo": ShellToolSpec(
            tool_name="echo",
            description="输出固定前缀的消息",
            command_template=["python", "-c", "print('hello, {name}')"],
            allowed_args={"name": ["alice", "bob"]},
            sandbox={"timeout_seconds": 5, "max_output_bytes": 4096},
        ),
        "sleep": ShellToolSpec(
            tool_name="sleep",
            description="睡眠指定秒数",
            command_template=["python", "-c", "import time; time.sleep({seconds})"],
            allowed_args={"seconds": ["2"]},
            sandbox={"timeout_seconds": 0.5, "max_output_bytes": 4096},
        ),
        "huge_output": ShellToolSpec(
            tool_name="huge_output",
            description="输出大量数据",
            command_template=["python", "-c", "print('x' * {size})"],
            allowed_args={"size": ["1024", "2048"]},
            sandbox={"timeout_seconds": 5, "max_output_bytes": 1024},
        ),
    }
    return ShellExecutor(specs)


@pytest.mark.asyncio
async def test_shell_execute_success(executor: ShellExecutor) -> None:
    result = await executor.execute("echo", {"name": "alice"}, _fake_context())
    assert result.status == "success"
    assert result.content.strip() == "hello, alice"


@pytest.mark.asyncio
async def test_shell_execute_not_in_allowed_list(executor: ShellExecutor) -> None:
    result = await executor.execute("echo", {"name": "mallory"}, _fake_context())
    assert result.status == "error"
    assert result.error_code == "shell_arg_not_allowed"


@pytest.mark.asyncio
async def test_shell_execute_missing_argument(executor: ShellExecutor) -> None:
    result = await executor.execute("echo", {}, _fake_context())
    assert result.status == "error"
    assert result.error_code == "shell_arg_not_allowed"


@pytest.mark.asyncio
async def test_shell_execute_command_injection_blocked(executor: ShellExecutor) -> None:
    # 试图通过参数注入 ; rm -rf /
    result = await executor.execute(
        "echo", {"name": "alice; rm -rf /"}, _fake_context()
    )
    assert result.status == "error"
    assert result.error_code == "shell_injection_blocked"


@pytest.mark.asyncio
async def test_shell_execute_timeout(executor: ShellExecutor) -> None:
    start = time.monotonic()
    result = await executor.execute("sleep", {"seconds": "2"}, _fake_context())
    elapsed = time.monotonic() - start
    assert result.status == "error"
    assert result.error_code == "shell_timeout"
    assert elapsed < 2.0


@pytest.mark.asyncio
async def test_shell_execute_output_too_large(executor: ShellExecutor) -> None:
    result = await executor.execute("huge_output", {"size": "2048"}, _fake_context())
    assert result.status == "error"
    assert result.error_code == "shell_output_too_large"


@pytest.mark.asyncio
async def test_shell_execute_unknown_tool(executor: ShellExecutor) -> None:
    result = await executor.execute("unknown", {}, _fake_context())
    assert result.status == "error"
    assert result.error_code == "shell_command_not_found"


@pytest.mark.asyncio
async def test_shell_list_tools_filtered_by_profile(executor: ShellExecutor) -> None:
    profile = CapabilityProfile(
        profile_id="p1",
        tools={"echo": {"tool_name": "echo", "allowed": True}},
    )
    tools = await executor.list_tools(profile)
    assert len(tools) == 1
    assert tools[0].canonical_name == "echo"


@pytest.mark.asyncio
async def test_shell_no_shell_interpretation() -> None:
    """验证 command_template 按字面执行，不经过 shell 解析。"""
    specs = {
        "echo_two": ShellToolSpec(
            tool_name="echo_two",
            command_template=["python", "-c", "print('{a}')"],
            allowed_args={"a": ["one two"]},
            sandbox={"timeout_seconds": 5, "max_output_bytes": 4096},
        )
    }
    shell_exec = ShellExecutor(specs)
    result = await shell_exec.execute("echo_two", {"a": "one two"}, _fake_context())
    assert result.status == "success"
    assert result.content.strip() == "one two"
