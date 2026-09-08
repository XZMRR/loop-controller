"""Isolated Subprocess Harness backend 测试（v0.32.0）。"""

from __future__ import annotations

import pytest

from loop_controller.executors.base import ExecutionContext
from loop_controller.executors.harness_models import (
    HarnessSandboxConfig,
    IsolatedSubprocessBackendConfig,
    ResourceLimitsConfig,
)
from loop_controller.executors.isolated_subprocess_harness import IsolatedSubprocessHarnessBackend


@pytest.fixture
def backend() -> IsolatedSubprocessHarnessBackend:
    return IsolatedSubprocessHarnessBackend(
        IsolatedSubprocessBackendConfig(name="local", type="isolated_subprocess")
    )


@pytest.fixture
def context() -> ExecutionContext:
    return ExecutionContext(
        call_id="c1",
        task_id="t1",
        agent_id="a1",
        user_id="u1",
    )


@pytest.mark.asyncio
async def test_isolated_subprocess_echo(backend, context) -> None:
    await backend.start()
    result = await backend.execute(
        "echo",
        {"text": "hello"},
        context,
        HarnessSandboxConfig(),
    )
    assert result.status == "success"
    assert result.content == "hello"
    assert result.error_code is None


@pytest.mark.asyncio
async def test_isolated_subprocess_add(backend, context) -> None:
    await backend.start()
    result = await backend.execute(
        "add",
        {"a": 2, "b": 3},
        context,
        HarnessSandboxConfig(),
    )
    assert result.status == "success"
    assert result.content == 5


@pytest.mark.asyncio
async def test_isolated_subprocess_unknown_tool(backend, context) -> None:
    await backend.start()
    result = await backend.execute(
        "unknown",
        {},
        context,
        HarnessSandboxConfig(),
    )
    assert result.status == "error"
    assert result.error_code == "harness_tool_not_found"


@pytest.mark.asyncio
async def test_isolated_subprocess_network_policy_deny_all_required(backend, context) -> None:
    await backend.start()
    sandbox = HarnessSandboxConfig(network_policy="allow_list", allowed_hosts=["example.com"])
    result = await backend.execute("echo", {"text": "x"}, context, sandbox)
    assert result.status == "error"
    assert result.error_code == "harness_sandbox_unsupported"


@pytest.mark.asyncio
async def test_isolated_subprocess_applies_resource_limits(backend, context) -> None:
    await backend.start()
    sandbox = HarnessSandboxConfig(
        resource_limits=ResourceLimitsConfig(
            max_memory_bytes=256 * 1024 * 1024,
            cpu_seconds=10.0,
        )
    )
    result = await backend.execute("add", {"a": 1, "b": 2}, context, sandbox)
    assert result.status == "success"
    assert result.content == 3


@pytest.mark.asyncio
async def test_isolated_subprocess_truncates_large_output(backend, context) -> None:
    await backend.start()
    sandbox = HarnessSandboxConfig(max_output_bytes=1024)
    result = await backend.execute("echo", {"text": "x" * 2048}, context, sandbox)
    # 输出超过 max_output_bytes 后被截断，JSON 解析失败 -> fail-closed 返回错误
    assert result.status == "error"
    assert result.error_code == "harness_invalid_response"
